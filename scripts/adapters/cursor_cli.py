#!/usr/bin/env python3
"""
agent-memory-hub — capture adapter for the Cursor CLI (`agent`, also `cursor-agent`).

The CLI keeps each chat apart from the IDE's state.vscdb (that one is scripts/adapters/cursor.py):

  ~/.cursor/chats/<md5(workspace path)>/<chat id>/
    meta.json   {"cwd", "title", "createdAtMs", "updatedAtMs", ...}
    store.db    SQLite: meta(key, value) + blobs(id, data), content-addressed by sha256
      meta['0']               hex of a JSON with agentId and latestRootBlobId
      blobs[latestRootBlobId] protobuf; field 1 repeats the 32-byte ids of the messages, in order
      blobs[<message id>]     JSON in the AI SDK shape: {"role", "content": str | [parts]}

User text comes wrapped in <user_query>; the first user message (<user_info>: OS, shell,
workspace) is context the CLI injects, not something the person asked. Assistant turns keep
only "text" parts: reasoning and tool calls stay out, as in the other adapters.

The CLI also writes a plain JSONL transcript, ~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl
({"role", "message": {"content": [parts]}} per line), and runs the Claude Code hooks of
~/.claude/settings.json with it as transcript_path. So hooks/capture_session.py sees every CLI
turn and hands it here (capture_from_hook); the JSONL is the fallback when the chat store
is not found.

Rows go in with tool='cursor' (it is the same Cursor, so handoff and `amh continuar` treat
both alike) and metadata.source='cursor-cli'. Three entry points:
  main()             backfill of every chat (nightly, `amh import`); skips ids already saved
  capture_chat()     one chat, refreshed in place
  capture_from_hook() a hook payload (capture_session.py and cursor_capture.py, every turn)
  refresh_cwd()      the latest chat of a folder (`amh continuar`, before it looks for one)

Usage:
  python3 scripts/adapters/cursor_cli.py --dry-run
  python3 scripts/adapters/cursor_cli.py

Config (env or ../../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
Override the chats dir with CURSOR_CHATS=/path (tests, another machine's copy).
"""
import glob
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))   # scripts/adapters -> repo root
sys.path.insert(0, os.path.join(REPO, "hooks"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, HERE)
from store import config, get_store  # noqa: E402
from capture_session import build_summary, sanitize_text, strip_nul  # noqa: E402
from project_key import project_key  # noqa: E402
from codex import is_internal  # noqa: E402  (mesmo marcador nos prompts que o hub manda ao Cursor)

TOOL = "cursor"
SOURCE = "cursor-cli"
MAX_CONTENT = 5_000_000
ROOT_IDS_FIELD = 1          # no blob raiz, o campo que lista as mensagens em ordem
ID_BYTES = 32               # sha256
QUERY_OPEN, QUERY_CLOSE = "<user_query>", "</user_query>"


TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


def _under(path, root):
    root = root.rstrip("/")
    return bool(root) and (path == root or path.startswith(root + "/"))


def skipped_cwd(cwd, env):
    """Motivo para não guardar um chat pela pasta dele, ou None. Pasta temporária é chamada
    automática (scripts, testes); CAPTURE_SKIP_PATHS (no .env, separado por ':') lista as
    pastas desta máquina onde só rodam chamadas automáticas, como um benchmark."""
    if not cwd:
        return None
    path = os.path.realpath(os.path.expanduser(cwd))
    if any(_under(path, os.path.realpath(r)) or _under(cwd, r) for r in (*TEMP_ROOTS, tempfile.gettempdir())):
        return "pasta temporária"
    raw = os.environ.get("CAPTURE_SKIP_PATHS") or (env or {}).get("CAPTURE_SKIP_PATHS") or ""
    for r in filter(None, (x.strip() for x in raw.split(":"))):
        if _under(path, os.path.realpath(os.path.expanduser(r))):
            return "pasta ignorada (CAPTURE_SKIP_PATHS)"
    return None


def chats_root():
    return os.environ.get("CURSOR_CHATS") or os.path.join(HOME, ".cursor", "chats")


def workspace_dir(cwd):
    """Pasta dos chats de um workspace: o CLI usa o md5 do caminho absoluto."""
    return os.path.join(chats_root(), hashlib.md5(cwd.encode()).hexdigest())


def chat_dirs(cwd=None):
    """Pastas de chat (com store.db): todas, ou só as do workspace `cwd`."""
    base = workspace_dir(cwd) if cwd else os.path.join(chats_root(), "*")
    return sorted(d for d in glob.glob(os.path.join(base, "*"))
                  if os.path.isfile(os.path.join(d, "store.db")))


def find_chat(chat_id):
    """Pasta de um chat pelo id (o conversation_id que o hook recebe), ou None."""
    if not chat_id or "/" in chat_id or chat_id.startswith("."):
        return None
    hits = [d for d in glob.glob(os.path.join(chats_root(), "*", chat_id))
            if os.path.isfile(os.path.join(d, "store.db"))]
    return hits[0] if hits else None


def read_meta_json(chat_dir):
    try:
        with open(os.path.join(chat_dir, "meta.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _varint(b, i):
    r = s = 0
    while True:
        if i >= len(b):
            raise ValueError("varint truncado")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def proto_fields(b):
    """(campo, valor) de primeiro nível de uma mensagem protobuf. Só os tipos que o
    CLI usa no blob raiz: varint (0) e length-delimited (2); outro tipo encerra a leitura."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
            yield field, v
        elif wire == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                return
            yield field, b[i:i + n]
            i += n
        else:
            return


def _open_copy(chat_dir, tmp):
    """Abre uma cópia do store.db (com -wal e -shm). O CLI pode estar escrevendo: abrir o
    original read-only com WAL pede o -shm, e immutable=1 ignoraria o que ainda está no WAL."""
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(chat_dir, "store.db" + suffix)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp, "store.db" + suffix))
    return sqlite3.connect(os.path.join(tmp, "store.db"))


def load_messages(chat_dir):
    """(meta do store, [mensagens JSON em ordem], uri do workspace) ou None se ilegível."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            con = _open_copy(chat_dir, tmp)
            row = con.execute("select value from meta where key = '0'").fetchone()
            if not row:
                return None
            meta = json.loads(bytes.fromhex(row[0]).decode())
            root_id = meta.get("latestRootBlobId")
            got = con.execute("select data from blobs where id = ?", (root_id,)).fetchone()
            if not got:
                return None
            ids, workspace = [], None
            for field, value in proto_fields(got[0]):
                if field == ROOT_IDS_FIELD and isinstance(value, bytes) and len(value) == ID_BYTES:
                    ids.append(value.hex())
                elif field == 9 and isinstance(value, bytes) and value.startswith(b"file://"):
                    workspace = workspace or value.decode(errors="replace")
            blobs = dict(con.execute(
                f"select id, data from blobs where id in ({','.join('?' * len(ids))})", ids
            ).fetchall()) if ids else {}
            con.close()
        except (sqlite3.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"  ilegível {os.path.basename(chat_dir)[:8]}: {type(e).__name__}", file=sys.stderr)
            return None
    messages = []
    for i in ids:
        try:
            m = json.loads(blobs[i])
        except (KeyError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
            continue   # blob que não é mensagem (estado interno) ou ausente
        if isinstance(m, dict) and m.get("role"):
            messages.append(m)
    return meta, messages, workspace


def _parts_text(content, kinds=("text",)):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in kinds and p.get("text"))
    return ""


def user_text(content):
    """O pedido de fato: o miolo de <user_query>; contexto injetado (<user_info>...) fica de fora."""
    text = _parts_text(content).strip()
    if QUERY_OPEN in text:
        start = text.index(QUERY_OPEN) + len(QUERY_OPEN)
        end = text.find(QUERY_CLOSE, start)
        return text[start:end if end != -1 else None].strip()
    return "" if text.startswith("<") else text


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def parse(chat_dir):
    """Mesma forma do codex.parse: (sid, cwd, content, user_texts, n_user, n_assistant,
    started_at, ended_at), ou None se o chat não puder ser lido."""
    loaded = load_messages(chat_dir)
    if not loaded:
        return None
    meta, messages, workspace = loaded
    mj = read_meta_json(chat_dir)
    sid = meta.get("agentId") or os.path.basename(chat_dir)
    cwd = mj.get("cwd") or (urllib.parse.unquote(workspace[len("file://"):]) if workspace else None)
    lines, user_texts, n_user, n_assistant = [], [], 0, 0
    for m in messages:
        role = m.get("role")
        if role == "user":
            text = user_text(m.get("content"))
        elif role == "assistant":
            text = _parts_text(m.get("content")).strip()
        else:
            continue   # system e tool: o prompt do CLI e saídas de ferramenta
        if not text:
            continue
        text = sanitize_text(text)   # mesma máscara de segredos/<private> da captura
        if role == "user":
            n_user += 1
            user_texts.append(text)
            lines.append(f"[user]\n{text}")
        else:
            n_assistant += 1
            lines.append(f"[assistant]\n{text}")
    content = "\n\n".join(lines)
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n\n[...truncado...]"
    started = ms_to_iso(mj.get("createdAtMs") or meta.get("createdAt"))
    ended = ms_to_iso(mj.get("updatedAtMs")) or started
    return sid, cwd, content, user_texts, n_user, n_assistant, started, ended


def build_row(parsed, chat_dir, env, meta=None):
    """Linha de `sessions` (a mesma forma do Codex e do Cursor IDE)."""
    sid, cwd, content, uts, nu, na, started, ended = parsed
    now = datetime.now(timezone.utc).isoformat()
    title = read_meta_json(chat_dir).get("title")
    return {
        "session_id": sid, "tool": TOOL, "machine": socket.gethostname(),
        "project": project_key(cwd, {**env, **os.environ}) if cwd else None,
        "started_at": started or now, "ended_at": ended or now,
        "content": content, "summary": build_summary(uts, nu, na),
        "metadata": {"cwd": cwd, "source": SOURCE, "chat": chat_dir,
                     **({"title": title} if title else {}), **(meta or {})},
    }


def capture_chat(store, chat_dir, env, meta=None):
    """Lê e grava um chat (upsert: atualiza a mesma linha a cada turno). Devolve a linha
    gravada, ou None se não havia o que gravar (vazio, ilegível, sessão interna do hub ou
    pasta ignorada: skipped_cwd)."""
    parsed = parse(chat_dir)
    if not parsed:
        return None
    sid, cwd, content, uts, *_ = parsed
    if not sid or not content or is_internal(uts) or skipped_cwd(cwd, env):
        return None
    row = strip_nul(build_row(parsed, chat_dir, env, meta))
    store.upsert_session(row, timeout=15)
    return row


def is_cli_transcript(path):
    """Transcript JSONL do Cursor CLI (o que ele passa como transcript_path aos hooks)."""
    p = (path or "").replace(os.sep, "/")
    return "/.cursor/projects/" in p and "/agent-transcripts/" in p and p.endswith(".jsonl")


def parse_jsonl(path, sid, cwd):
    """Mesma forma do parse(), a partir do transcript JSONL. Sem horários no arquivo:
    usa o mtime como fim da sessão."""
    lines, user_texts, n_user, n_assistant = [], [], 0, 0
    try:
        with open(path) as f:
            raw_lines = f.readlines()
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    for raw in raw_lines:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        role = d.get("role")
        content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
        if role == "user":
            text = user_text(content)
        elif role == "assistant":
            text = _parts_text(content).strip()
        else:
            continue
        if not text:
            continue
        text = sanitize_text(text)
        if role == "user":
            n_user += 1
            user_texts.append(text)
            lines.append(f"[user]\n{text}")
        else:
            n_assistant += 1
            lines.append(f"[assistant]\n{text}")
    content = "\n\n".join(lines)
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n\n[...truncado...]"
    ended = ms_to_iso(mtime * 1000)
    return sid, cwd, content, user_texts, n_user, n_assistant, None, ended


def _git(cwd):
    if not cwd:
        return None
    from handoff import git_snapshot   # import tardio: o backfill não precisa
    try:
        return git_snapshot(cwd)
    except Exception:
        return None


def capture_from_hook(store, payload, env, git=None):
    """Grava o turno que um hook recebeu do Cursor CLI. Prefere o chat completo do store.db
    (tem horários e título); sem ele, lê o transcript JSONL. Devolve a linha ou None."""
    chat_id = payload.get("conversation_id") or payload.get("session_id")
    chat = find_chat(chat_id)
    if git is None:   # o cwd do hook nem sempre é o repo: vale o do chat
        cwd = (read_meta_json(chat).get("cwd") if chat else None) or payload.get("cwd")
        git = _git(cwd)
    meta = {"hook_reason": payload.get("hook_event_name") or "stop", "git": git}
    if chat:
        return capture_chat(store, chat, env, meta)
    path = payload.get("transcript_path")
    if not chat_id or not is_cli_transcript(path):
        return None
    cwd = payload.get("cwd") or next(iter(payload.get("workspace_roots") or []), None)
    parsed = parse_jsonl(path, chat_id, cwd)
    if not parsed or not parsed[2] or is_internal(parsed[3]) or skipped_cwd(cwd, env):
        return None
    row = build_row(parsed, os.path.dirname(path), env, {**meta, "transcript": path})
    row = strip_nul(row)
    store.upsert_session(row, timeout=15)
    return row


def latest_chat(cwd):
    """Chat mais recente do workspace `cwd` (pelo updatedAtMs do meta.json), ou None."""
    dirs = chat_dirs(cwd)
    if not dirs:
        return None
    return max(dirs, key=lambda d: read_meta_json(d).get("updatedAtMs") or 0)


def refresh_cwd(store, cwd, env=None):
    """Regrava o chat mais recente do CLI nesta pasta, com o git de agora. Para quem vai
    retomar o trabalho (`amh continuar`) não depender do hook nem do nightly."""
    from handoff import git_snapshot   # import tardio: o backfill não precisa
    d = latest_chat(cwd)
    if not d:
        return None
    return capture_chat(store, d, env if env is not None else config.ENV,
                        {"git": git_snapshot(cwd)})


def main(argv):
    dry = "--dry-run" in argv
    env = config.ENV
    store = get_store()
    if not store.configured:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    dirs = chat_dirs()
    if not dirs:
        print("Cursor CLI sem chats (~/.cursor/chats vazio). Nada a fazer.")
        return 0
    seen = store.session_ids()
    # cada chat cai em exatamente um motivo: a soma fecha com o total enumerado
    count = {"ilegível": 0, "vazio": 0, "já no banco": 0, "interno do hub": 0,
             "pasta temporária": 0, "pasta ignorada (CAPTURE_SKIP_PATHS)": 0}
    todo = []
    for d in dirs:
        parsed = parse(d)
        if not parsed:
            count["ilegível"] += 1
            continue
        sid, cwd, content, uts, nu, na, *_ = parsed
        if not sid or not content:
            count["vazio"] += 1
        elif sid in seen:
            count["já no banco"] += 1
        elif is_internal(uts):
            count["interno do hub"] += 1
        elif skipped_cwd(cwd, env):
            count[skipped_cwd(cwd, env)] += 1   # o motivo é a própria chave da contagem
        else:
            todo.append((d, parsed))
    print(f"{len(dirs)} chats do Cursor CLI; " + "; ".join(f"{n} {k}" for k, n in count.items())
          + f"; {len(todo)} a enviar")
    if sum(count.values()) + len(todo) != len(dirs):
        print("ERRO: contagem não fecha", file=sys.stderr)
        return 1
    if dry:
        for d, (sid, cwd, _c, _u, nu, na, *_r) in todo:
            print(f"  [dry] {sid[:8]}… {project_key(cwd, {**env, **os.environ}) if cwd else '-'} ({nu}u/{na}a)")
        return 0
    for d, parsed in todo:
        try:
            store.upsert_session(strip_nul(build_row(parsed, d, env)), timeout=20)
        except Exception as e:
            print(f"  erro {parsed[0][:8]}: {type(e).__name__}: {e}", file=sys.stderr)
    # prova pelo banco, não pelo retorno do upsert
    now = store.session_ids() if todo else seen
    failed = [p[0] for _d, p in todo if p[0] not in now]
    for sid in failed:
        print(f"  FALHOU {sid[:8]}… (não está no banco)", file=sys.stderr)
    print(f"enviadas {len(todo) - len(failed)} sessões do Cursor CLI; {len(failed)} falharam")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
