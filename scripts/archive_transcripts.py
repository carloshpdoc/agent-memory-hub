#!/usr/bin/env python3
"""
agent-memory-hub — archive raw session transcripts to Supabase Storage (opt-in per machine).

The `sessions` table keeps the conversation text; the raw transcripts (tool calls, tool
output, subagents) live only on each machine, and Claude Code deletes them after
`cleanupPeriodDays`. This uploads them to a PRIVATE bucket, one object per transcript:

  transcripts/<tool>/<machine>/<session_id>[/<subagent>].jsonl.gz

Each file goes through the capture's secret/<private> redaction and has base64 blobs
(attached or generated images) replaced by a marker before gzip. Objects are upserted, so a
session that grew is re-sent; a local state file skips unchanged ones.

Opt-in and work-safe: runs only with ARCHIVE_RAW=1, and skips sessions whose project key or
cwd contains a term of ARCHIVE_EXCLUDE (default: COMMIT_DENYLIST) — raw transcripts carry
whole source files, which an employer may not allow outside its machines.

Usage:  python3 scripts/archive_transcripts.py [--dry-run]
Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, ARCHIVE_RAW, ARCHIVE_EXCLUDE,
        ARCHIVE_BUCKET (default transcripts).
"""
import glob
import gzip
import json
import os
import re
import socket
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "adapters"))
from memory_client import ENV, URL, KEY  # noqa: E402
from capture_session import sanitize_text  # noqa: E402
from project_key import project_key  # noqa: E402
import codex  # noqa: E402
import cursor  # noqa: E402

STATE_PATH = os.path.join(REPO, ".archive-state.json")   # gitignored (.archive*)
MAX_OBJECT = 45 * 1024 * 1024                            # bucket limit is 50 MB
BLOB_RE = re.compile(r'"(?:data:[\w/+.-]+;base64,)?[A-Za-z0-9+/=]{20000,}"')


def clean(text):
    """Texto do transcript pronto pra arquivar: sem blobs base64, com a mesma máscara da
    captura (segredos e <private>)."""
    return sanitize_text(BLOB_RE.sub('"[blob removido]"', text))


def excluded(cwd, terms, env):
    """True se o projeto (chave ou caminho) contém algum termo excluído."""
    if not terms:
        return False
    hay = f"{(cwd or '').lower()} {(project_key(cwd, env) or '').lower() if cwd else ''}"
    return any(t in hay for t in terms)


def object_path(tool, machine, sid, part=None):
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
    tail = f"/{safe(part)}" if part else ""
    return f"{tool}/{safe(machine)}/{safe(sid)}{tail}.jsonl.gz"


# ---- fontes: (tool, session_id, cwd, [(parte, caminho_ou_None, texto_ou_None, mtime)]) --------
def claude_sources():
    for main in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        sid = os.path.splitext(os.path.basename(main))[0]
        cwd = None
        try:
            for line in open(main, errors="replace"):
                cwd = json.loads(line).get("cwd")
                if cwd:
                    break
        except (OSError, ValueError):
            continue
        parts = [(None, main, None, os.path.getmtime(main))]
        for sub in sorted(glob.glob(os.path.join(os.path.splitext(main)[0], "subagents", "*.jsonl"))):
            parts.append((os.path.splitext(os.path.basename(sub))[0], sub, None, os.path.getmtime(sub)))
        yield "claude-code", sid, cwd, parts


def codex_sources():
    for p in glob.glob(os.path.join(codex.SESSIONS, "**", "rollout-*.jsonl"), recursive=True):
        parsed = codex.parse(p)
        if not parsed or not parsed[0] or codex.is_internal(parsed[3]):
            continue
        yield "codex", parsed[0], parsed[1], [(None, p, None, os.path.getmtime(p))]


def cursor_sources():
    db = cursor.cursor_db()
    if not db or not os.path.exists(db):
        return
    mtime = os.path.getmtime(db)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for (raw,) in con.execute("select value from cursorDiskKV where key like 'composerData:%'"):
            try:
                comp = json.loads(raw)
            except (TypeError, ValueError):
                continue
            cid = comp.get("composerId")
            headers = comp.get("fullConversationHeadersOnly") or []
            if not cid or not headers:
                continue
            _, _, nu, na, cwd, _, _ = cursor.reconstruct(con, cid, headers)
            if not (nu or na):
                continue
            lines = [json.dumps(comp)] + [v for (v,) in con.execute(
                "select value from cursorDiskKV where key like ?", (f"bubbleId:{cid}:%",)) if v]
            yield "cursor", cid, cwd, [(None, None, "\n".join(lines), mtime)]
    finally:
        con.close()


def upload(bucket, path, data):
    req = urllib.request.Request(
        f"{URL}/storage/v1/object/{bucket}/{urllib.parse.quote(path)}", data=data, method="POST",
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/gzip", "x-upsert": "true"})
    urllib.request.urlopen(req, timeout=120).read()


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except (OSError, ValueError):
        return {}


def main(argv):
    dry = "--dry-run" in argv
    env = {**ENV, **os.environ}
    if env.get("ARCHIVE_RAW") != "1" and not dry:
        print("ARCHIVE_RAW != 1 nesta máquina: arquivamento desligado")
        return 0
    if not URL or not KEY:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    bucket = env.get("ARCHIVE_BUCKET") or "transcripts"
    raw_terms = env.get("ARCHIVE_EXCLUDE") if "ARCHIVE_EXCLUDE" in env else env.get("COMMIT_DENYLIST")
    terms = [t.strip().lower() for t in (raw_terms or "").split(",") if t.strip()]
    machine, state = socket.gethostname(), load_state()
    sent = skipped_work = unchanged = failed = 0
    size = 0
    for source in (claude_sources, codex_sources, cursor_sources):
        for tool, sid, cwd, parts in source():
            if excluded(cwd, terms, env):
                skipped_work += 1
                continue
            for part, path, text, mtime in parts:
                obj = object_path(tool, machine, sid, part)
                if state.get(obj) == mtime:
                    unchanged += 1
                    continue
                try:
                    body = text if text is not None else open(path, errors="replace").read()
                    data = gzip.compress(clean(body).encode("utf-8"), 6)
                    if len(data) > MAX_OBJECT:
                        print(f"  grande demais, pulado: {obj} ({len(data) >> 20} MB)", file=sys.stderr)
                        continue
                    if not dry:
                        upload(bucket, obj, data)
                        state[obj] = mtime
                    sent += 1
                    size += len(data)
                except (OSError, urllib.error.URLError) as e:
                    failed += 1
                    print(f"  erro {obj}: {e}", file=sys.stderr)
            if not dry and sent % 20 == 0:
                json.dump(state, open(STATE_PATH, "w"))
    if not dry:
        json.dump(state, open(STATE_PATH, "w"))
    print(f"{'(dry-run) ' if dry else ''}{sent} arquivo(s) enviado(s) ({size / 1e6:.1f} MB gzip); "
          f"{unchanged} sem mudança; {skipped_work} sessão(ões) de trabalho excluída(s)"
          + (f"; {failed} falha(s)" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
