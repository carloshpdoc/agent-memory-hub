"""
agent-memory-hub — Supabase backend: PostgREST over HTTP, plus the embed Edge Function.

The only place that talks to Supabase. Modules are moved onto it one at a time (see
store/__init__.py); until then the low-level calls (rest/rest_all/count/rpc/write/embed)
are what memory_client re-exports. Pure stdlib.
"""
import json
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config


class SupabaseStore:
    name = "supabase"

    def __init__(self, url=None, key=None, embed_key=None):
        self.url = url if url is not None else config.get("SUPABASE_URL")
        self.key = key if key is not None else config.get("SUPABASE_SECRET_KEY")
        self.embed_key = embed_key if embed_key is not None else config.get("EMBED_KEY")
        self.headers = {"apikey": self.key or "", "Authorization": f"Bearer {self.key or ''}",
                        "Content-Type": "application/json"}

    @property
    def configured(self):
        return bool(self.url and self.key)

    # ---- PostgREST -----------------------------------------------------------------------
    def rest(self, path, timeout=20):
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", headers=self.headers)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def rest_all(self, path, page=1000):
        """rest() paginado: o PostgREST corta cada resposta em 1000 linhas (max-rows)."""
        rows, offset = [], 0
        sep = "&" if "?" in path else "?"
        while True:
            chunk = self.rest(f"{path}{sep}limit={page}&offset={offset}")
            rows += chunk
            if len(chunk) < page:
                return rows
            offset += page

    def count(self, path):
        """Total exato de linhas via Content-Range, sem baixar as linhas."""
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}{sep}limit=1",
                                     headers={**self.headers, "Prefer": "count=exact"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def rpc(self, name, body, timeout=30):
        req = urllib.request.Request(f"{self.url}/rest/v1/rpc/{name}",
                                     data=json.dumps(body).encode(), method="POST", headers=self.headers)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def parallel(self, calls, timeout=10):
        """Roda consultas independentes em paralelo (cada uma é um round-trip HTTPS de ~300 ms).
        calls: {nome: ("rest", path) | ("rpc", (função, corpo)) | None}. Devolve {nome: resultado};
        uma consulta que falha vira None, sem derrubar as outras."""
        def one(spec):
            kind, arg = spec
            return self.rpc(*arg, timeout=timeout) if kind == "rpc" else self.rest(arg, timeout=timeout)

        jobs = {k: v for k, v in calls.items() if v}
        out = {}
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futures = {k: pool.submit(one, v) for k, v in jobs.items()}
            for k, f in futures.items():
                try:
                    out[k] = f.result()
                except Exception:
                    out[k] = None
        return out

    def write(self, path, body, method="PATCH", prefer="return=minimal"):
        data = json.dumps(body).encode() if body is not None else None   # DELETE vai sem corpo
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", data=data,
                                     method=method, headers={**self.headers, "Prefer": prefer})
        urllib.request.urlopen(req, timeout=20).read()

    def upsert(self, table, row, on_conflict, timeout=15):
        """Insere ou mescla pela(s) coluna(s) única(s) de on_conflict; devolve o status HTTP."""
        req = urllib.request.Request(
            f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}", data=json.dumps(row).encode("utf-8"),
            method="POST", headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status

    # ---- embeddings (Edge Function gte-small, 384 dims) ------------------------------------
    EMBED_BATCH = 2                          # textos por chamada: o free tier estoura CPU (546) com 4 x 1500 chars
    TRANSIENT = (429, 500, 502, 503, 504, 546)

    def _send(self, url, body, headers, method="POST", timeout=60):
        """Uma requisição crua (os testes trocam só isto)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=timeout).read()

    @property
    def _embed_headers(self):
        return {"x-embed-key": self.embed_key or "", "Content-Type": "application/json"}

    def embed_texts(self, texts, batch=EMBED_BATCH, max_fails=5):
        """Vetores de vários textos (modo `texts` da função). Corta o lote pela metade no limite
        de CPU (546), espera nos erros transitórios, desiste após max_fails seguidos."""
        out, i, fails = [], 0, 0
        while i < len(texts):
            part = texts[i:i + batch]
            try:
                res = json.loads(self._send(f"{self.url}/functions/v1/embed", {"texts": part}, self._embed_headers))
            except urllib.error.HTTPError as e:
                if e.code == 546 and batch > 1:
                    batch = max(1, batch // 2)
                    continue
                if e.code not in self.TRANSIENT or fails >= max_fails:
                    raise
                fails += 1
                time.sleep(min(2 ** fails, 30))
                continue
            except (urllib.error.URLError, socket.timeout, TimeoutError):
                if fails >= max_fails:
                    raise
                fails += 1
                time.sleep(min(2 ** fails, 30))
                continue
            embs = res.get("embeddings")
            if not isinstance(embs, list) or len(embs) != len(part):
                raise RuntimeError(f"embed function returned {res!r:.200}")
            out += embs
            i += len(part)
            fails = 0
        return out

    def embed_pending_sessions(self, limit):
        """Gera o vetor de até `limit` sessões sem embedding (feito pela própria função, modo
        `limit`). Devolve {"embedded": n, "scanned": n}; HTTPError sobe para quem tenta de novo."""
        return json.loads(self._send(f"{self.url}/functions/v1/embed", {"limit": limit}, self._embed_headers))

    def embed(self, text):
        req = urllib.request.Request(f"{self.url}/functions/v1/embed",
                                     data=json.dumps({"text": text}).encode(), method="POST",
                                     headers={"x-embed-key": self.embed_key or "",
                                              "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())["embedding"]

    # ---- operações de domínio --------------------------------------------------------------
    RECALL_SESSION_FIELDS = "session_id,started_at,ended_at,machine,tool,project,summary"

    def recall_bundle(self, project):
        """O que o recall do SessionStart injeta, numa ida só (consultas em paralelo):
          proj     sessões recentes do projeto (6)         recent   recentes no geral (4)
          facts    fatos válidos do projeto + globais (12)  pending  padrões de perfil propostos (3)
          last     a última ATIVIDADE (ended_at) do projeto: decide se há passagem de contexto
        Só o resumo, nunca o content: sessões longas têm MBs. Chave que falhou vem None."""
        sel = f"select={self.RECALL_SESSION_FIELDS}"
        q = urllib.parse.quote(project) if project else None
        scope = f"or=(scope.eq.{q},scope.is.null)" if project else "scope=is.null"
        return self.parallel({
            "proj": ("rest", f"sessions?project=eq.{q}&order=started_at.desc&limit=6&{sel}") if project else None,
            "recent": ("rest", f"sessions?order=started_at.desc&limit=4&{sel}"),
            "facts": ("rest", f"facts?valid_until=is.null&{scope}&order=created_at.desc&limit=12"
                              "&select=fact,kind,scope,confidence,valid_from,code_check"),
            "pending": ("rest", "profile_patterns?status=eq.proposed&order=confidence.desc&limit=3"
                                "&select=pattern,confidence,evidence"),
            "last": ("rest", f"sessions?project=eq.{q}&order=ended_at.desc.nullslast&limit=1&{sel}")
                    if project else None,
        })

    def handoff_detail(self, session_id):
        """{"content": fim da conversa principal, "metadata": {...}} da sessão (handoff.py)."""
        res = self.parallel({
            "tail": ("rpc", ("session_main_tail", {"p_session_id": session_id})),
            "meta": ("rest", f"sessions?session_id=eq.{urllib.parse.quote(session_id)}&select=metadata"),
        })
        return {"content": res.get("tail") or "",
                "metadata": ((res.get("meta") or [{}])[0] or {}).get("metadata") or {}}

    def upsert_session(self, row, timeout=15):
        """Grava a sessão (mesclando pela session_id: a captura regrava a cada turno)."""
        return self.upsert("sessions", row, "session_id", timeout)

    def put_ops_status(self, row, timeout=10):
        """Estado operacional da máquina (uma linha por máquina, painel do dashboard)."""
        return self.upsert("ops_status", row, "machine", timeout)

    # ---- fatos (extract_facts) -------------------------------------------------------------
    RESET_BATCH = 100   # ids por PATCH: um UPDATE só, filtrando o content de centenas de sessões, estoura o statement timeout

    def _ids(self, flt):
        return [r["id"] for r in self.rest_all(f"sessions?select=id&{flt}&order=id")]

    def reset_facts_extraction(self, mode, terms=()):
        """Recoloca sessões na fila de extração. mode 'all': as já extraídas; 'how-to': as cujo
        content contém algum dos termos. Em duas etapas (ids, depois PATCH por lotes de id) pra
        cada instrução caber no statement timeout. Devolve quantas."""
        if mode == "all":
            ids = self._ids("facts_extracted_at=not.is.null")
        else:
            ors = ",".join(f"content.ilike.*{urllib.parse.quote(t)}*" for t in terms)   # "passo a passo" tem espaço
            ids = self._ids(f"or=({ors})")
        for i in range(0, len(ids), self.RESET_BATCH):
            self.write(f"sessions?id=in.({','.join(ids[i:i + self.RESET_BATCH])})", {"facts_extracted_at": None})
        return len(ids)

    def project_keys(self):
        """Toda chave de projeto já vista nas sessões."""
        return {r["project"] for r in self.rest_all("sessions?project=not.is.null&select=project") if r.get("project")}

    def pending_fact_sessions(self, limit, exclude_ids=()):
        """Sessões ainda não extraídas, mais recentes primeiro (id, session_id, project, machine, content)."""
        skip = f"&id=not.in.({','.join(sorted(exclude_ids))})" if exclude_ids else ""
        return self.rest(f"sessions?facts_extracted_at=is.null{skip}&order=started_at.desc&limit={limit}"
                         "&select=id,session_id,project,machine,content", timeout=60)

    def match_facts(self, embedding, count, scope=None):
        """Fatos válidos mais próximos (com similarity), do scope e globais."""
        return self.rpc("match_facts", {"query_embedding": embedding, "match_count": count, "filter_scope": scope})

    def insert_fact(self, fact, kind, scope, source_session_id, machine, embedding):
        """Grava um fato novo; devolve o id (gerado aqui, igual em qualquer backend)."""
        fid = str(uuid.uuid4())
        self.write("facts", {"id": fid, "fact": fact, "kind": kind, "scope": scope,
                             "source_session_id": source_session_id, "machine": machine,
                             "embedding": json.dumps(embedding)}, method="POST")
        return fid

    def fact_machines(self, fact_ids):
        """{id: máquina de origem} dos fatos (match_facts não traz a máquina)."""
        if not fact_ids:
            return {}
        ids = ",".join(f'"{i}"' for i in fact_ids)
        return {r["id"]: r.get("machine") for r in self.rest(f"facts?select=id,machine&id=in.({ids})")}

    def invalidate_fact(self, fact_id, when, superseded_by=None):
        """Fato deixa de valer a partir de `when` (não apaga); superseded_by aponta o substituto."""
        body = {"valid_until": when, **({"superseded_by": superseded_by} if superseded_by else {})}
        self.write(f"facts?id=eq.{fact_id}", body)

    def supersede_fact(self, old_id, new_id, when):
        self.invalidate_fact(old_id, when, superseded_by=new_id)

    def find_fact_dupes(self, min_sim):
        """Pares de fatos válidos parecidos no mesmo scope (a_id, b_id, a_fact, b_fact, scope, similarity)."""
        return self.rpc("find_fact_dupes", {"min_sim": min_sim}, timeout=120)

    def stale_fact_candidates(self, kinds, older_than, limit):
        """Fatos válidos dos tipos perecíveis com valid_from antes de older_than (ISO), mais velhos primeiro."""
        return self.rest(f"facts?valid_until=is.null&kind=in.({','.join(kinds)})"
                         f"&valid_from=lt.{urllib.parse.quote(older_than)}&order=valid_from.asc&limit={limit}"
                         "&select=id,fact,kind,scope,valid_from")

    def valid_facts(self, limit=500):
        """Fatos válidos (fact, scope), por scope com os globais por último."""
        return self.rest(f"facts?select=fact,scope&valid_until=is.null&order=scope.nullslast&limit={limit}")

    def facts_created_since(self, since, limit=100):
        return self.rest(f"facts?select=fact,scope,created_at&created_at=gte.{urllib.parse.quote(since)}"
                         f"&valid_until=is.null&order=created_at.desc&limit={limit}")

    def sessions_since(self, since, limit=500):
        return self.rest(f"sessions?select=project,summary,started_at,tool,machine"
                         f"&started_at=gte.{urllib.parse.quote(since)}&order=started_at.desc&limit={limit}")

    # ---- perfil (synthesize_profile, enforce_rules, apply_profile_rules) ---------------------
    def profile_patterns(self, statuses, require_rule=False, limit=200):
        """Padrões com status em `statuses`, mais confiantes primeiro (todas as colunas usadas)."""
        rule = "&proposed_rule=not.is.null" if require_rule else ""
        return self.rest(f"profile_patterns?select=pattern,category,evidence,confidence,status,proposed_rule"
                         f"&status=in.({','.join(statuses)}){rule}&order=confidence.desc&limit={limit}")

    def delete_proposed_patterns(self):
        """Apaga os padrões ainda não revisados (a síntese é idempotente: reinsere a lista nova)."""
        self.write("profile_patterns?status=eq.proposed", None, method="DELETE")

    def insert_pattern(self, pattern, category, evidence, confidence, proposed_rule):
        self.write("profile_patterns", {"pattern": pattern, "category": category, "evidence": evidence,
                                        "confidence": confidence, "proposed_rule": proposed_rule}, method="POST")

    def mark_facts_extracted(self, session_pk, when):
        self.write(f"sessions?id=eq.{session_pk}", {"facts_extracted_at": when})

    # ---- chunks (embed_pending) ------------------------------------------------------------
    @property
    def _write_headers(self):
        return {**self.headers, "Prefer": "return=minimal"}

    def pending_chunk_sessions(self, limit):
        """Sessões cujo content mudou desde o último chunking: [{id, content, content_len}]."""
        return json.loads(self._send(f"{self.url}/rest/v1/rpc/sessions_pending_chunks", {"max_rows": limit},
                                     {**self.headers, "Prefer": "return=representation"}))

    def replace_chunks(self, session_pk, chunks, content_len):
        """Troca os chunks da sessão e registra o tamanho coberto. chunks: [(idx, start, end, vetor)].
        Apagar-e-inserir não é atômico, mas chunked_len vai por último: uma falha no meio deixa a
        sessão pendente, e a próxima rodada refaz."""
        self._send(f"{self.url}/rest/v1/session_chunks?session_fk=eq.{session_pk}", None,
                   self._write_headers, method="DELETE")
        if chunks:
            self._send(f"{self.url}/rest/v1/session_chunks", [
                {"session_fk": session_pk, "chunk_idx": i, "char_start": a, "char_end": b,
                 "embedding": json.dumps(e)} for i, a, b, e in chunks], self._write_headers)
        self._send(f"{self.url}/rest/v1/sessions?id=eq.{session_pk}", {"chunked_len": content_len},
                   self._write_headers, method="PATCH")

    # ---- busca -----------------------------------------------------------------------------
    def hybrid_search(self, query, embedding, count, project=None):
        """Sessões por relevância (full-text + vetor por chunk, fusão RRF), com o começo do content
        (2000 chars) e score/fts_rank/vec_rank. Mesma ordem do hybrid_search de sql/09."""
        return self.rpc("hybrid_search_head", {"query_text": query, "query_embedding": embedding,
                                               "match_count": count, "filter_project": project})

    def best_chunks(self, embedding, session_pks, per_session=2):
        """Os trechos de cada sessão mais próximos do vetor: [{session_fk, chunk_idx, text, dist}]."""
        return self.rpc("best_chunks", {"query_embedding": embedding, "p_ids": list(session_pks),
                                        "per_session": per_session})
