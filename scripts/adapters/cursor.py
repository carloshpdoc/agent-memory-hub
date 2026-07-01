#!/usr/bin/env python3
"""
agent-memory-hub — capture adapter for Cursor.

Cursor stores its agent/chat history in a SQLite key-value DB
(`.../Cursor/User/globalStorage/state.vscdb`, table `cursorDiskKV`):
  - `composerData:<composerId>`  → one conversation: metadata + the ordered list
    `fullConversationHeadersOnly` = [{bubbleId, type}, ...]  (type 1=user, 2=assistant)
  - `bubbleId:<composerId>:<bubbleId>` → one message: `text`, `type`, `createdAt`,
    `workspaceUris` (the real project path).

This adapter reconstructs each conversation into the shared session shape and upserts
to Supabase (tool='cursor'). Idempotent (skips composers already present). Reuses the
same summary logic as the Claude Code hook, so recall/search treat Cursor uniformly.

A settle guard skips conversations whose last message is very recent, so an in-flight
chat isn't captured half-finished and then skipped forever.

Usage:
  python3 scripts/adapters/cursor.py --dry-run
  python3 scripts/adapters/cursor.py

Config (env or ../../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
Override the DB path with CURSOR_DB=/path/to/state.vscdb (e.g. on another OS).
"""
import json
import os
import socket
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))   # scripts/adapters -> repo root
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, os.path.join(REPO, "hooks"))
from capture_session import build_summary  # noqa: E402  (reuse summary logic)

TOOL = "cursor"
MAX_CONTENT = 5_000_000
SETTLE_SECONDS = 5 * 60   # skip conversations whose last message is newer than this


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def cursor_db():
    """Locate Cursor's global state.vscdb across platforms (or CURSOR_DB override)."""
    override = os.environ.get("CURSOR_DB")
    if override:
        return override if os.path.exists(override) else None
    if sys.platform == "darwin":
        base = os.path.join(HOME, "Library", "Application Support", "Cursor")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", ""), "Cursor")
    else:  # linux and friends
        base = os.path.join(HOME, ".config", "Cursor")
    path = os.path.join(base, "User", "globalStorage", "state.vscdb")
    return path if os.path.exists(path) else None


def connect_ro(db):
    """Open read-only + immutable so a running Cursor can't cause a lock error."""
    return sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)


def age_seconds(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, AttributeError):
        return None


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def existing_ids(url, key):
    ids, offset = set(), 0
    while True:
        req = urllib.request.Request(
            f"{url}/rest/v1/sessions?select=session_id&limit=1000&offset={offset}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"})
        page = json.loads(urllib.request.urlopen(req, timeout=30).read())
        ids.update(r["session_id"] for r in page if r.get("session_id"))
        if len(page) < 1000:
            return ids
        offset += 1000


def reconstruct(con, cid, headers):
    """Rebuild one conversation from its bubbles, in the header order."""
    rows = con.execute(
        "select key, value from cursorDiskKV where key like ?",
        (f"bubbleId:{cid}:%",)).fetchall()
    by_id = {}
    for k, v in rows:
        parts = k.split(":", 2)
        if len(parts) == 3:
            try:
                by_id[parts[2]] = json.loads(v)
            except json.JSONDecodeError:
                pass

    lines, user_texts = [], []
    n_user = n_assistant = 0
    cwd = first_ts = last_ts = None
    for h in headers:
        b = by_id.get(h.get("bubbleId"))
        if not b:
            continue
        if cwd is None:
            uris = b.get("workspaceUris") or []
            if uris:
                cwd = urllib.parse.unquote(uris[0].replace("file://", ""))
            elif b.get("workspaceProjectDir"):
                cwd = b.get("workspaceProjectDir")
        ts = b.get("createdAt")
        if isinstance(ts, str) and ts:
            first_ts = first_ts or ts
            last_ts = ts
        text = (b.get("text") or "").strip()
        if not text:
            continue
        role = b.get("type") or h.get("type")
        if role == 1:
            n_user += 1
            user_texts.append(text)
            lines.append(f"[user]\n{text}")
        elif role == 2:
            n_assistant += 1
            lines.append(f"[assistant]\n{text}")
    content = "\n\n".join(lines)
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n\n[...truncado...]"
    return content, user_texts, n_user, n_assistant, cwd, first_ts, last_ts


def main(argv):
    dry = "--dry-run" in argv
    env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    db = cursor_db()
    if not db:
        print("Cursor não encontrado (state.vscdb ausente). Nada a fazer.")
        return 0

    try:
        con = connect_ro(db)
    except sqlite3.Error as e:
        print(f"ERRO ao abrir {db}: {type(e).__name__}", file=sys.stderr)
        return 1

    composers = con.execute(
        "select value from cursorDiskKV where key like 'composerData:%'").fetchall()
    seen = existing_ids(url, key)
    sent = skipped_live = 0
    for (raw,) in composers:
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        cid = d.get("composerId")
        headers = d.get("fullConversationHeadersOnly") or []
        if not cid or not headers or cid in seen:
            continue

        content, uts, nu, na, cwd, fts, lts = reconstruct(con, cid, headers)
        if not content:
            continue

        # settle guard: don't capture a conversation that's still in flight
        if lts is not None:
            age = age_seconds(lts)
            if age is not None and age < SETTLE_SECONDS:
                skipped_live += 1
                continue

        project = os.path.basename((cwd or "").rstrip("/")) or "root"
        if dry:
            print(f"  [dry] {cid[:8]}… {project} ({nu}u/{na}a)")
            sent += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        row = {
            "session_id": cid, "tool": TOOL, "machine": socket.gethostname(),
            "project": project,
            "started_at": fts or ms_to_iso(d.get("createdAt")) or now,
            "ended_at": lts or now,
            "content": content, "summary": build_summary(uts, nu, na),
            "metadata": {"cwd": cwd, "source": "cursor", "composerId": cid},
        }
        req = urllib.request.Request(
            f"{url}/rest/v1/sessions?on_conflict=session_id",
            data=json.dumps(row).encode(), method="POST",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"})
        try:
            urllib.request.urlopen(req, timeout=20)
            sent += 1
        except Exception as e:
            print(f"  erro {cid[:8]}: {type(e).__name__}", file=sys.stderr)

    tail = f" ({skipped_live} em andamento, puladas)" if skipped_live else ""
    print(f"{'(dry-run) ' if dry else ''}{sent} sessão(ões) do Cursor{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
