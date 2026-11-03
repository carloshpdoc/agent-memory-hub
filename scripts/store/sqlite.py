"""
agent-memory-hub — local SQLite backend (MEM_BACKEND=sqlite). Same operations as the
Supabase backend, read and written in one file per machine (MEM_DB, default
~/.local/share/agent-memory-hub/mem.db, WAL mode).

Search is ported from sql/03, 09 and 14 with the same constants (rrf_k = 50, pool = 30):
FTS5 (bm25) replaces ts_rank, and cosine distance runs in C through sqlite-vec when the
extension loads, otherwise through a Python function with the same name (correct, slower:
the Python 3.9 from Apple cannot load extensions). Query vectors still come from the embed
Edge Function until local embeddings exist (plan, phase 3). Pure stdlib.
"""
import json
import os
import re
import sqlite3
import threading
import uuid
from array import array
from datetime import datetime, timezone
from math import sqrt

from . import config
from .schema import TABLE_ORDER, primary_key  # noqa: F401  (TABLE_ORDER: quem copia)
from .timefmt import parse_ts

SCHEMA_DIR = os.path.join(config.REPO, "sql", "sqlite")
SCHEMA_VERSION = 1
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".local", "share", "agent-memory-hub", "mem.db")

RRF_K, POOL = 50, 30            # mesmas constantes do hybrid_search de sql/09
HEAD_CHARS = 2000               # hybrid_search_head (sql/14)
EMBED_CHARS = 2000              # MAX_CHARS da Edge Function: o vetor da sessão cobre o começo
SUBAGENT_SEP = "\n\n--- subagent "

# colunas que guardam JSON (texto) e vetores (BLOB float32), por tabela
JSON_COLS = {"metadata", "usage", "payload", "evidence", "files", "code_check"}
VECTOR_COLS = {"embedding"}
TS_COLS = {"started_at", "ended_at", "facts_extracted_at", "valid_from", "valid_until", "created_at",
           "reviewed_at", "updated_at", "retired_at", "decided_at", "installed_at"}
COLUMNS = {
    "sessions": ("id", "session_id", "tool", "machine", "project", "started_at", "ended_at", "content",
                 "metadata", "summary", "embedding", "facts_extracted_at", "chunked_len", "usage"),
    "facts": ("id", "fact", "kind", "scope", "source_session_id", "machine", "embedding", "valid_from",
              "valid_until", "superseded_by", "confidence", "created_at", "code_check"),
    "profile_patterns": ("id", "pattern", "category", "evidence", "confidence", "status", "proposed_rule",
                         "created_at", "reviewed_at"),
    "ops_status": ("machine", "updated_at", "payload"),
    "session_chunks": ("session_fk", "chunk_idx", "char_start", "char_end", "embedding"),
    "config_items": ("id", "kind", "name", "files", "hash", "source_machine", "updated_at", "retired_at"),
    "config_assignments": ("item_id", "machine", "status", "installed_hash", "decided_at", "installed_at",
                           "note"),
}


# ---- conversões ------------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ts(value):
    """Qualquer ISO-8601 (com Z, sem fração, só a data, fração de 1 a 9 dígitos) em UTC com
    microssegundos: assim a ordem do texto é a ordem do tempo. None continua None."""
    if value is None:
        return None
    return parse_ts(value).isoformat(timespec="microseconds")


def vec_blob(v):
    """Vetor (lista ou o texto '[...]' do PostgREST) em BLOB float32; None continua None."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    if isinstance(v, str):
        v = json.loads(v)
    if not isinstance(v, (list, tuple)) or not v:
        raise ValueError("vetor vazio ou inválido")
    return array("f", v).tobytes()


def blob_vec(b):
    a = array("f")
    a.frombytes(b)
    return list(a)


def _py_cosine_distance(a, b):
    """vec_distance_cosine em Python (mesma definição: 1 - cosseno), para quando o sqlite-vec
    não carrega. NULL em qualquer lado dá NULL, como no Postgres."""
    if a is None or b is None:
        return None
    x, y = array("f"), array("f")
    x.frombytes(a)
    y.frombytes(b)
    if len(x) != len(y):
        raise ValueError(f"dimensões diferentes: {len(x)} x {len(y)}")
    dot = sum(p * q for p, q in zip(x, y))
    nx, ny = sqrt(sum(p * p for p in x)), sqrt(sum(q * q for q in y))
    if not nx or not ny:
        return None
    return 1.0 - dot / (nx * ny)


def fts_query(text):
    """websearch_to_tsquery em FTS5: termos com E implícito, "frase", OR, -exclusão. Cada termo
    vai entre aspas (nada do texto vira sintaxe do FTS5). None quando não sobra termo positivo."""
    if not text or not text.strip():
        return None
    tokens = re.findall(r'-?"[^"]*"?|\S+', text)
    groups, current, negs, pending_or = [], [], [], False
    for tok in tokens:
        if tok.lower() == "or":
            pending_or = bool(current)
            continue
        neg = tok.startswith("-") and len(tok) > 1
        word = tok[1:] if neg else tok
        word = word.strip('"')
        if not re.search(r"\w", word):
            continue
        quoted = '"' + word.replace('"', '""') + '"'
        if neg:
            negs.append(quoted)
            continue
        if pending_or and current:
            current[-1] = f"{current[-1]} OR {quoted}"
        else:
            current.append(quoted)
        pending_or = False
    if not current:
        return None
    expr = " AND ".join(f"({t})" if " OR " in t else t for t in current)
    for n in negs:
        expr = f"({expr}) NOT {n}"
    return expr


class SqliteStore:
    name = "sqlite"

    def __init__(self, path=None, embedder=None):
        self.path = path or os.path.expanduser(config.get("MEM_DB") or DEFAULT_DB)
        self.url, self.key = self.path, "sqlite"         # quem só confere "está configurado"
        self.embed_key = config.get("EMBED_KEY")
        self._embedder = embedder
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.vec_backend = None                           # "sqlite-vec" ou "python", após conectar

    @property
    def configured(self):
        return True

    # ---- conexão ---------------------------------------------------------------------------
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self.path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma foreign_keys = on")
            conn.execute("pragma busy_timeout = 10000")
            if self.path != ":memory:":
                conn.execute("pragma journal_mode = wal")
                conn.execute("pragma synchronous = normal")
            self._load_vec(conn)
            with self._init_lock:
                self._migrate(conn)
            self._local.conn = conn
        return conn

    def _load_vec(self, conn):
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self.vec_backend = "sqlite-vec"
        except (ImportError, AttributeError, sqlite3.OperationalError):
            conn.create_function("vec_distance_cosine", 2, _py_cosine_distance, deterministic=True)
            self.vec_backend = "python"

    def _migrate(self, conn):
        version = conn.execute("pragma user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        with open(os.path.join(SCHEMA_DIR, "001.sql")) as f:
            conn.executescript(f.read())

    def _q(self, sql, params=()):
        return [self._out(r) for r in self._conn().execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _scalar(self, sql, params=()):
        return self._conn().execute(sql, params).fetchone()[0]

    @staticmethod
    def _out(row):
        d = dict(row)
        for k, v in d.items():
            if k in JSON_COLS and isinstance(v, str):
                d[k] = json.loads(v)
        return d

    # ---- escrita genérica (colunas sempre da lista fixa: nada do chamador vira SQL) ---------
    @staticmethod
    def _cols(table, fields):
        bad = set(fields) - set(COLUMNS[table])
        if bad:
            raise ValueError(f"colunas desconhecidas em {table}: {sorted(bad)}")
        return list(fields)

    @staticmethod
    def _val(col, v):
        if col in VECTOR_COLS:
            return vec_blob(v)
        if col in JSON_COLS:
            return None if v is None else json.dumps(v, ensure_ascii=False)
        if col in TS_COLS:
            return ts(v)
        return v

    def _insert(self, table, row, conflict=None, merge=True):
        cols = self._cols(table, row)
        sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join('?' * len(cols))})")
        if conflict:
            upd = [c for c in cols if c not in conflict]
            sql += f" on conflict ({', '.join(conflict)}) do "
            sql += (f"update set {', '.join(f'{c} = excluded.{c}' for c in upd)}" if merge and upd
                    else "nothing")
        self._conn().execute(sql, [self._val(c, row[c]) for c in cols])

    def _update(self, table, fields, where, params):
        if not fields:
            return
        cols = self._cols(table, fields)
        self._conn().execute(f"update {table} set {', '.join(f'{c} = ?' for c in cols)} where {where}",
                             [self._val(c, fields[c]) for c in cols] + list(params))

    # ---- embeddings ------------------------------------------------------------------------
    @property
    def embedder(self):
        """Quem gera os vetores. Até a fase 3 (modelo local), a Edge Function do Supabase."""
        if self._embedder is None:
            from .supabase import SupabaseStore
            self._embedder = SupabaseStore()
        return self._embedder

    def embed(self, text):
        return self.embedder.embed(text)

    def embed_texts(self, texts, **kw):
        return self.embedder.embed_texts(texts, **kw)

    def embed_pending_sessions(self, limit):
        """Vetor das sessões sem embedding (o começo do content, como a Edge Function no modo
        `limit`). Devolve {"embedded": n, "scanned": n}."""
        rows = self._q("select id, content from sessions where embedding is null limit ?", (limit,))
        if not rows:
            return {"embedded": 0, "scanned": 0}
        embs = self.embed_texts([(r["content"] or "")[:EMBED_CHARS] for r in rows])
        for r, e in zip(rows, embs):
            self._update("sessions", {"embedding": e}, "id = ?", (r["id"],))
        return {"embedded": len(rows), "scanned": len(rows)}

    # ---- recall e passagem de contexto ------------------------------------------------------
    RECALL_SESSION_FIELDS = "session_id, started_at, ended_at, machine, tool, project, summary"

    def recall_bundle(self, project):
        """O mesmo dicionário do backend Supabase; chave que falhou vem None."""
        sel = self.RECALL_SESSION_FIELDS

        def run(sql, params=()):
            try:
                return self._q(sql, params)
            except sqlite3.Error:
                return None

        scope = "(scope = ? or scope is null)" if project else "scope is null"
        return {
            "proj": run(f"select {sel} from sessions where project = ? order by started_at desc limit 6",
                        (project,)) if project else None,
            "recent": run(f"select {sel} from sessions order by started_at desc limit 4"),
            "facts": run("select fact, kind, scope, confidence, valid_from, code_check from facts "
                         f"where valid_until is null and {scope} order by created_at desc limit 12",
                         (project,) if project else ()),
            "pending": run("select pattern, confidence, evidence from profile_patterns "
                           "where status = 'proposed' order by confidence desc limit 3"),
            "last": run(f"select {sel} from sessions where project = ? "
                        "order by ended_at desc nulls last limit 1", (project,)) if project else None,
        }

    def session_tail(self, session_id, chars=20000):
        row = self._one("select content from sessions where session_id = ?", (session_id,))
        if row is None:
            return None
        main = (row["content"] or "").split(SUBAGENT_SEP, 1)[0]
        return main[-chars:] if chars > 0 else ""

    def handoff_detail(self, session_id):
        row = self._one("select metadata from sessions where session_id = ?", (session_id,))
        return {"content": self.session_tail(session_id) or "",
                "metadata": (row or {}).get("metadata") or {}}

    def session_ids(self):
        return {r["session_id"] for r in self._q("select session_id from sessions where session_id is not null")}

    def upsert_session(self, row, timeout=15):
        """Grava ou mescla pela session_id (só as colunas enviadas mudam). Devolve 201."""
        row = dict(row)
        exists = row.get("session_id") and self._one("select id from sessions where session_id = ?",
                                                     (row["session_id"],))
        if not exists:
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("started_at", now_iso())
            row.setdefault("metadata", {})
        else:
            row.pop("id", None)
        self._insert("sessions", row, conflict=("session_id",))
        return 201

    def put_ops_status(self, row, timeout=10):
        row = dict(row)
        row.setdefault("updated_at", now_iso())
        self._insert("ops_status", row, conflict=("machine",))
        return 201

    # ---- fatos -----------------------------------------------------------------------------
    def reset_facts_extraction(self, mode, terms=()):
        if mode == "all":
            ids = [r["id"] for r in self._q("select id from sessions where facts_extracted_at is not null")]
        else:
            if not terms:
                return 0
            cond = " or ".join("lower(content) like ?" for _ in terms)
            ids = [r["id"] for r in self._q(f"select id from sessions where {cond}",
                                            [f"%{t.lower()}%" for t in terms])]
        self.update_rows("sessions", ids, {"facts_extracted_at": None})
        return len(ids)

    def project_keys(self):
        return {r["project"] for r in self._q("select distinct project from sessions where project is not null")}

    def pending_fact_sessions(self, limit, exclude_ids=()):
        ex = sorted(exclude_ids)
        skip = f" and id not in ({', '.join('?' * len(ex))})" if ex else ""
        return self._q("select id, session_id, project, machine, content from sessions "
                       f"where facts_extracted_at is null{skip} order by started_at desc limit ?", ex + [limit])

    def match_facts(self, embedding, count, scope=None):
        q = vec_blob(embedding)
        return self._q(
            "select id, fact, kind, scope, confidence, 1 - vec_distance_cosine(embedding, ?) as similarity "
            "from facts where valid_until is null and embedding is not null "
            "and (? is null or scope = ? or scope is null) "
            "order by vec_distance_cosine(embedding, ?) limit ?", (q, scope, scope, q, count))

    def insert_fact(self, fact, kind, scope, source_session_id, machine, embedding):
        fid, now = str(uuid.uuid4()), now_iso()
        self._insert("facts", {"id": fid, "fact": fact, "kind": kind, "scope": scope,
                               "source_session_id": source_session_id, "machine": machine,
                               "embedding": embedding, "valid_from": now, "created_at": now})
        return fid

    def fact_machines(self, fact_ids):
        ids = list(fact_ids)
        if not ids:
            return {}
        rows = self._q(f"select id, machine from facts where id in ({', '.join('?' * len(ids))})", ids)
        return {r["id"]: r["machine"] for r in rows}

    def invalidate_fact(self, fact_id, when, superseded_by=None):
        body = {"valid_until": when, **({"superseded_by": superseded_by} if superseded_by else {})}
        self._update("facts", body, "id = ?", (fact_id,))

    def supersede_fact(self, old_id, new_id, when):
        self.invalidate_fact(old_id, when, superseded_by=new_id)

    def find_fact_dupes(self, min_sim):
        """Pares (o mais novo em a) no mesmo scope com cosseno >= min_sim, mais parecidos primeiro.
        Em Python, por scope: roda só no nightly, sobre os fatos válidos."""
        rows = self._q("select id, fact, scope, created_at, embedding from facts "
                       "where valid_until is null and embedding is not null")
        groups = {}
        for r in rows:
            v = blob_vec(r["embedding"])
            n = sqrt(sum(x * x for x in v))
            if n:
                groups.setdefault(r["scope"] or "", []).append((r, [x / n for x in v]))
        out = []
        for members in groups.values():
            for i, (a, va) in enumerate(members):
                for b, vb in members[i + 1:]:
                    if a["created_at"] == b["created_at"]:
                        continue
                    newer, older = (a, b) if a["created_at"] > b["created_at"] else (b, a)
                    sim = sum(p * q for p, q in zip(va, vb))
                    if sim >= min_sim:
                        out.append({"a_id": newer["id"], "a_fact": newer["fact"], "b_id": older["id"],
                                    "b_fact": older["fact"], "scope": newer["scope"], "similarity": sim})
        return sorted(out, key=lambda d: -d["similarity"])

    def stale_fact_candidates(self, kinds, older_than, limit):
        kinds = list(kinds)
        return self._q("select id, fact, kind, scope, valid_from from facts where valid_until is null "
                       f"and kind in ({', '.join('?' * len(kinds))}) and valid_from < ? "
                       "order by valid_from asc limit ?", kinds + [ts(older_than), limit])

    def valid_facts(self, limit=500):
        return self._q("select fact, scope from facts where valid_until is null "
                       "order by scope nulls last limit ?", (limit,))

    def facts_created_since(self, since, limit=100):
        return self._q("select fact, scope, created_at from facts where created_at >= ? and valid_until is null "
                       "order by created_at desc limit ?", (ts(since), limit))

    def sessions_since(self, since, limit=500):
        return self._q("select project, summary, started_at, tool, machine from sessions where started_at >= ? "
                       "order by started_at desc limit ?", (ts(since), limit))

    # ---- perfil ----------------------------------------------------------------------------
    def profile_patterns(self, statuses, require_rule=False, limit=200):
        statuses = list(statuses)
        rule = " and proposed_rule is not null" if require_rule else ""
        return self._q("select id, pattern, category, evidence, confidence, status, proposed_rule "
                       f"from profile_patterns where status in ({', '.join('?' * len(statuses))}){rule} "
                       "order by confidence desc limit ?", statuses + [limit])

    def delete_proposed_patterns(self):
        self._conn().execute("delete from profile_patterns where status = 'proposed'")

    def insert_pattern(self, pattern, category, evidence, confidence, proposed_rule):
        self._insert("profile_patterns", {"id": str(uuid.uuid4()), "pattern": pattern, "category": category,
                                          "evidence": evidence, "confidence": confidence,
                                          "proposed_rule": proposed_rule, "created_at": now_iso()})

    def mark_facts_extracted(self, session_pk, when):
        self._update("sessions", {"facts_extracted_at": when}, "id = ?", (session_pk,))

    # ---- chunks ----------------------------------------------------------------------------
    def pending_chunk_sessions(self, limit):
        return self._q("select id, content, length(content) as content_len from sessions "
                       "where chunked_len is not length(content) order by started_at limit ?", (limit,))

    def replace_chunks(self, session_pk, chunks, content_len):
        """Troca os chunks e registra o tamanho coberto, numa transação só."""
        conn = self._conn()
        conn.execute("begin")
        try:
            conn.execute("delete from session_chunks where session_fk = ?", (session_pk,))
            conn.executemany("insert into session_chunks (session_fk, chunk_idx, char_start, char_end, embedding) "
                             "values (?, ?, ?, ?, ?)",
                             [(session_pk, i, a, b, vec_blob(e)) for i, a, b, e in chunks])
            conn.execute("update sessions set chunked_len = ? where id = ?", (content_len, session_pk))
            conn.execute("commit")
        except BaseException:
            conn.execute("rollback")
            raise

    # ---- busca -----------------------------------------------------------------------------
    def _fts_ids(self, query, project, limit):
        expr = fts_query(query)
        if not expr:
            return []
        flt = " and s.project = ?" if project else ""
        rows = self._conn().execute(
            "select s.id from sessions_fts f join sessions s on s.rowid = f.rowid "
            f"where sessions_fts match ?{flt} order by bm25(sessions_fts) limit ?",
            [expr] + ([project] if project else []) + [limit]).fetchall()
        return [r[0] for r in rows]

    def _vec_ids(self, embedding, project, limit):
        q = vec_blob(embedding)
        flt = "where s.project = ?" if project else ""
        rows = self._conn().execute(
            "select b.id from (select id, min(dist) as dist from ("
            "  select session_fk as id, vec_distance_cosine(embedding, ?) as dist from session_chunks"
            "  union all"
            "  select id, vec_distance_cosine(embedding, ?) from sessions where embedding is not null"
            ") group by id) b join sessions s on s.id = b.id "
            f"{flt} order by b.dist limit ?", [q, q] + ([project] if project else []) + [limit]).fetchall()
        return [r[0] for r in rows]

    def hybrid_search(self, query, embedding, count, project=None):
        """Fusão RRF de full-text e vetor por chunk, com o começo do content (sql/09 + sql/14)."""
        fts = {sid: i + 1 for i, sid in enumerate(self._fts_ids(query, project, POOL))}
        vec = {sid: i + 1 for i, sid in enumerate(self._vec_ids(embedding, project, POOL))} if embedding else {}
        ids = list(dict.fromkeys(list(fts) + list(vec)))
        if not ids:
            return []
        score = {i: (1.0 / (RRF_K + fts[i]) if i in fts else 0.0) + (1.0 / (RRF_K + vec[i]) if i in vec else 0.0)
                 for i in ids}
        top = sorted(ids, key=lambda i: -score[i])[:count]
        rows = {r["id"]: r for r in self._q(
            "select id, session_id, tool, machine, project, started_at, substr(content, 1, ?) as content "
            f"from sessions where id in ({', '.join('?' * len(top))})", [HEAD_CHARS] + top)}
        return [{**rows[i], "score": score[i], "fts_rank": fts.get(i), "vec_rank": vec.get(i)} for i in top]

    def best_chunks(self, embedding, session_pks, per_session=2):
        ids = list(session_pks)
        if not ids:
            return []
        q = vec_blob(embedding)
        return self._q(
            "select session_fk, chunk_idx, text, dist from ("
            "  select c.session_fk, c.chunk_idx,"
            "         substr(s.content, c.char_start + 1, c.char_end - c.char_start) as text,"
            "         vec_distance_cosine(c.embedding, ?) as dist,"
            "         row_number() over (partition by c.session_fk"
            "                            order by vec_distance_cosine(c.embedding, ?)) as rn"
            "  from session_chunks c join sessions s on s.id = c.session_fk"
            f"  where c.session_fk in ({', '.join('?' * len(ids))})"
            ") where rn <= ? order by session_fk, chunk_idx", [q, q] + ids + [per_session])

    # ---- sync de configuração --------------------------------------------------------------
    CONFIG_ITEM_FIELDS = "id, kind, name, hash, files, source_machine, updated_at, retired_at"

    def config_items(self, kinds=None, name_prefix=None):
        where, params = [], []
        if kinds:
            kinds = list(kinds)
            where.append(f"kind in ({', '.join('?' * len(kinds))})")
            params += kinds
        if name_prefix:
            where.append("substr(name, 1, ?) = ?")
            params += [len(name_prefix), name_prefix]
        flt = f" where {' and '.join(where)}" if where else ""
        return self._q(f"select {self.CONFIG_ITEM_FIELDS} from config_items{flt} order by kind, name", params)

    def config_item(self, item_id):
        return self._one(f"select {self.CONFIG_ITEM_FIELDS} from config_items where id = ?", (item_id,))

    def all_assignments(self):
        return self._q("select item_id, machine, status, installed_hash, decided_at, note from config_assignments")

    def set_assignments(self, item_ids, target, **fields):
        conn = self._conn()
        conn.execute("begin")
        try:
            for i in item_ids:
                self._insert("config_assignments", {"item_id": i, "machine": target, **fields},
                             conflict=("item_id", "machine"))
            conn.execute("commit")
        except BaseException:
            conn.execute("rollback")
            raise

    def assignments(self, target):
        rows = self._q("select item_id, status, installed_hash, decided_at from config_assignments "
                       "where machine = ?", (target,))
        return {r["item_id"]: r for r in rows}

    def create_config_item(self, fields):
        item_id = str(uuid.uuid4())
        self._insert("config_items", {"id": item_id, "updated_at": now_iso(), **fields})
        return item_id

    def update_config_item(self, item_id, fields):
        self._update("config_items", fields, "id = ?", (item_id,))

    def set_assignment(self, item_id, target, **fields):
        self._insert("config_assignments", {"item_id": item_id, "machine": target, **fields},
                     conflict=("item_id", "machine"))

    def reset_other_approvals(self, item_id, target, note):
        self._conn().execute("update config_assignments set status = 'pending', decided_at = null, note = ? "
                             "where item_id = ? and status = 'approved' and machine != ?",
                             (note, item_id, target))

    # ---- genéricas (backup, migrações, cópia entre backends) --------------------------------
    def table_rows(self, table, fields="*", page=1000):
        """Todas as linhas, na ordem da chave; vetores no formato texto do PostgREST ('[...]')."""
        if table not in COLUMNS:
            raise ValueError(f"tabela desconhecida: {table}")
        cols = COLUMNS[table] if fields == "*" else self._cols(table, [c.strip() for c in fields.split(",")])
        order = ", ".join(primary_key(table))
        rows = self._q(f"select {', '.join(cols)} from {table} order by {order}")
        for r in rows:
            for c in VECTOR_COLS & set(r):
                if r[c] is not None:
                    r[c] = json.dumps(blob_vec(r[c]))
        return rows

    def load_rows(self, table, rows):
        """Carga em lote (cópia de outro backend): substitui pela chave, numa transação. As chaves
        estrangeiras são conferidas no commit (um fato pode apontar para um que vem depois)."""
        conn = self._conn()
        conn.execute("begin")
        conn.execute("pragma defer_foreign_keys = on")
        try:
            for r in rows:
                self._insert(table, {k: v for k, v in r.items() if k in COLUMNS[table]},
                             conflict=primary_key(table))
            conn.execute("commit")
        except BaseException:
            conn.execute("rollback")
            raise

    def update_rows(self, table, ids, fields, batch=100):
        ids = list(ids)
        for i in range(0, len(ids), batch):
            part = ids[i:i + batch]
            self._update(table, fields, f"id in ({', '.join('?' * len(part))})", part)

    def sessions_without_summary(self):
        return self._q("select id, content from sessions where summary is null")

    def update_session(self, session_pk, fields):
        self._update("sessions", fields, "id = ?", (session_pk,))

    # ---- blobs -----------------------------------------------------------------------------
    def blob_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), "blobs")

    def put_blob(self, bucket, path, data, content_type="application/gzip", timeout=120):
        """Grava o objeto em <pasta do banco>/blobs/<bucket>/<path> (sem sair dessa pasta)."""
        root = os.path.realpath(os.path.join(self.blob_dir(), bucket))
        dest = os.path.realpath(os.path.join(root, path))
        if not dest.startswith(root + os.sep) or not re.match(r"^[A-Za-z0-9._-]+$", bucket):
            raise ValueError(f"caminho de blob inválido: {bucket}/{path}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)

    # ---- leituras de sessões e fatos -------------------------------------------------------
    def summaries(self, session_ids):
        ids = list(session_ids)
        if not ids:
            return {}
        rows = self._q(f"select session_id, summary from sessions where session_id in ({', '.join('?' * len(ids))})",
                       ids)
        return {r["session_id"]: r["summary"] for r in rows}

    def fts_sessions(self, query, project=None, limit=8):
        expr = fts_query(query)
        if not expr:
            return []
        flt = " and s.project = ?" if project else ""
        return self._q("select s.session_id, s.project, s.machine, s.summary, s.content "
                       "from sessions_fts f join sessions s on s.rowid = f.rowid "
                       f"where sessions_fts match ?{flt} limit ?",
                       [expr] + ([project] if project else []) + [limit])

    def recent_sessions(self, limit=10):
        return self._q("select session_id, started_at, machine, tool, project, summary from sessions "
                       "order by started_at desc limit ?", (limit,))

    def facts_for_scope(self, project=None, limit=60):
        flt = " and (scope = ? or scope is null)" if project else ""
        return self._q(f"select fact, kind, scope from facts where valid_until is null{flt} "
                       "order by scope nulls last, kind limit ?", ([project] if project else []) + [limit])

    def last_activity(self, project=None, exclude_tool=None):
        where, params = [], []
        if project:
            where.append("project = ?")
            params.append(project)
        if exclude_tool:
            where.append("tool != ?")
            params.append(exclude_tool)
        flt = f" where {' and '.join(where)}" if where else ""
        return self._one("select session_id, tool, machine, project, started_at, ended_at, summary, content, "
                         f"metadata from sessions{flt} order by ended_at desc nulls last limit 1", params)

    def find_session(self, id_prefix):
        return self._one("select session_id, project, tool, machine, started_at, content from sessions "
                         "where substr(session_id, 1, ?) = ? limit 1", (len(id_prefix), id_prefix))

    # ---- console ---------------------------------------------------------------------------
    def stats(self):
        by_tool = {(r["tool"] or "?"): r["n"] for r in self._q("select tool, count(*) as n from sessions group by tool")}
        return {"by_tool": by_tool, "facts": self.valid_fact_count()}

    def sessions_active_between(self, since, until=None, limit=300):
        flt, params = "ended_at >= ?", [ts(since)]
        if until:
            flt += " and ended_at < ?"
            params.append(ts(until))
        return self._q(f"select session_id, ended_at, project, tool, summary from sessions where {flt} "
                       "order by ended_at asc limit ?", params + [limit])

    def procedure_facts(self):
        return self._q("select id, fact, scope, source_session_id, valid_from from facts "
                       "where kind = 'procedure' and valid_until is null order by valid_from desc")

    def valid_fact_details(self):
        return self._q("select id, fact, kind, scope, confidence, valid_from, source_session_id from facts "
                       "where valid_until is null order by valid_from desc")

    def pattern_ids(self):
        return [r["id"] for r in self._q("select id from profile_patterns")]

    def update_pattern(self, pattern_id, fields):
        self._update("profile_patterns", fields, "id = ?", (pattern_id,))

    def session_ids_with_subagents(self):
        return {r["session_id"] for r in self._q(
            "select session_id from sessions where session_id is not null and instr(content, ?) > 0",
            (SUBAGENT_SEP.strip("\n"),))}

    def pending_fact_count(self):
        return self._scalar("select count(*) from sessions where facts_extracted_at is null")

    # ---- dashboard -------------------------------------------------------------------------
    def session_activity(self):
        return self._q("select machine, tool, started_at, facts_extracted_at from sessions")

    def ops_statuses(self):
        return self._q("select machine, updated_at, payload from ops_status order by machine")

    def valid_fact_count(self):
        return self._scalar("select count(*) from facts where valid_until is null")

    def pattern_count(self, status):
        return self._scalar("select count(*) from profile_patterns where status = ?", (status,))

    def list_sessions(self, query=None, project=None, machine=None, limit=50):
        where, params, join = [], [], ""
        if query:
            expr = fts_query(query)
            if not expr:
                return []
            join = " join sessions_fts f on f.rowid = s.rowid"
            where.append("sessions_fts match ?")
            params.append(expr)
        if project:
            where.append("s.project = ?")
            params.append(project)
        if machine:
            where.append("s.machine = ?")
            params.append(machine)
        flt = f" where {' and '.join(where)}" if where else ""
        return self._q("select s.session_id, s.project, s.machine, s.tool, s.started_at, s.summary, "
                       f"s.facts_extracted_at from sessions s{join}{flt} order by s.started_at desc limit ?",
                       params + [limit])

    def session_detail(self, session_id):
        return self._one("select session_id, project, machine, tool, started_at, ended_at, summary, content, "
                         "facts_extracted_at from sessions where session_id = ?", (session_id,))

    def facts_from_session(self, session_id):
        return self._q("select id, fact, kind, confidence, valid_until from facts where source_session_id = ?",
                       (session_id,))

    def all_patterns(self):
        return self._q("select id, pattern, category, evidence, confidence, status, proposed_rule, created_at, "
                       "reviewed_at from profile_patterns order by status, confidence desc")

    # ---- evals, custo, verificação, backfill de uso ----------------------------------------
    def summarized_sessions(self, project=None, limit=None):
        flt, params = ("and project = ? ", [project]) if project else ("", [])
        sql = f"select session_id, project, machine, summary from sessions where summary is not null {flt}"
        if limit:
            return self._q(sql + "order by started_at desc limit ?", params + [limit])
        return self._q(sql, params)

    def sessions_with_usage(self, since, project=None):
        flt, params = (" and project = ?", [project]) if project else ("", [])
        return self._q("select session_id, project, tool, started_at, summary, usage, "
                       "json_extract(metadata, '$.git.branch') as branch from sessions "
                       f"where started_at >= ?{flt}", [ts(since)] + params)

    def sessions_missing_usage(self, tools):
        tools = list(tools)
        rows = self._q(f"select session_id, tool from sessions where usage is null "
                       f"and tool in ({', '.join('?' * len(tools))})", tools)
        return {r["session_id"]: r["tool"] for r in rows}

    def sessions_without_git(self, tool):
        return self._q("select session_id, metadata from sessions where tool = ? "
                       "and json_type(metadata, '$.git') is null", (tool,))   # JSON null conta como presente, como no Postgres

    def update_session_by_id(self, session_id, fields):
        self._update("sessions", fields, "session_id = ?", (session_id,))

    def facts_for_code_check(self):
        return self._q("select id, fact, scope, code_check from facts where valid_until is null")

    def set_fact_code_check(self, fact_id, check):
        self._update("facts", {"code_check": check}, "id = ?", (fact_id,))
