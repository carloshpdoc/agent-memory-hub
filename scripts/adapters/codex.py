#!/usr/bin/env python3
"""
agent-memory-hub — capture adapter for Codex CLI.

Scans ~/.codex/sessions/**/rollout-*.jsonl, parses each into the shared session
shape, and upserts to Supabase (tool='codex'). Idempotent (skips sessions already
present). Reuses the same summary logic as the Claude Code hook, so recall/search
treat Codex sessions uniformly.

This is the template for adding more tools: locate the tool's local transcripts,
map them to (session_id, cwd, user/assistant turns), and upsert. Run on a cron.

Usage:
  python3 scripts/adapters/codex.py --dry-run
  python3 scripts/adapters/codex.py

Config (env or ../../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import glob
import json
import os
import socket
import sys
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))   # scripts/adapters -> repo root
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, os.path.join(REPO, "hooks"))
from capture_session import build_summary  # noqa: E402  (reuse summary logic)

SESSIONS = os.path.join(HOME, ".codex", "sessions")
TOOL = "codex"
MAX_CONTENT = 5_000_000


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


def parse(path):
    sid = cwd = first_ts = last_ts = None
    user_texts, lines, n_user, n_assistant = [], [], 0, 0
    try:
        for raw in open(path):
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
            if t == "session_meta":
                sid = sid or p.get("id")
                cwd = cwd or p.get("cwd")
            elif t == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = " ".join(
                    b.get("text", "") for b in (p.get("content") or [])
                    if b.get("type") in ("input_text", "output_text") and b.get("text")
                ).strip()
                if not text:
                    continue
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                if role == "user":
                    n_user += 1
                    user_texts.append(text)
                    lines.append(f"[user]\n{text}")
                else:
                    n_assistant += 1
                    lines.append(f"[assistant]\n{text}")
    except OSError:
        return None
    content = "\n\n".join(lines)
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n\n[...truncado...]"
    return sid, cwd, content, user_texts, n_user, n_assistant, first_ts, last_ts


def main(argv):
    dry = "--dry-run" in argv
    env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    files = sorted(glob.glob(os.path.join(SESSIONS, "**", "rollout-*.jsonl"), recursive=True))
    seen = existing_ids(url, key)
    sent = 0
    for f in files:
        parsed = parse(f)
        if not parsed:
            continue
        sid, cwd, content, uts, nu, na, fts, lts = parsed
        if not sid or not content or sid in seen:
            continue
        if dry:
            print(f"  [dry] {sid[:8]}… {os.path.basename((cwd or '').rstrip('/')) or 'root'} ({nu}u/{na}a)")
            sent += 1
            continue
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "session_id": sid, "tool": TOOL, "machine": socket.gethostname(),
            "project": os.path.basename((cwd or "").rstrip("/")) or "root",
            "started_at": fts or now, "ended_at": lts or now,
            "content": content, "summary": build_summary(uts, nu, na),
            "metadata": {"cwd": cwd, "source": "codex", "file": f},
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
            print(f"  erro {sid[:8]}: {type(e).__name__}", file=sys.stderr)

    print(f"{'(dry-run) ' if dry else ''}{sent} sessão(ões) do Codex")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
