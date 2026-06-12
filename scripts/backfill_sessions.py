#!/usr/bin/env python3
"""
agent-memory-hub — backfill local Claude Code sessions into Supabase.

Finds this machine's transcripts (~/.claude/projects/*/*.jsonl), skips the ones
already in Supabase, and captures the rest by reusing capture_session.py (so they
get the same parsing + summary). Idempotent (upsert by session_id).

Useful when adopting agent-memory-hub on a machine that already has Claude Code
history from before the hooks were installed.

Usage:
  python3 scripts/backfill_sessions.py --dry-run   # preview, no upload
  python3 scripts/backfill_sessions.py             # upload

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import glob
import json
import os
import subprocess
import sys
import urllib.request

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
CAPTURE = os.path.join(HERE, "..", "hooks", "capture_session.py")
PROJECTS = os.path.join(HOME, ".claude", "projects")


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


def cwd_of(path):
    try:
        with open(path) as fh:
            for line in fh:
                e = json.loads(line)
                if e.get("cwd"):
                    return e["cwd"]
    except Exception:
        pass
    return None


def main(argv):
    dry = "--dry-run" in argv
    env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    files = sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")))
    seen = existing_ids(url, key)   # sempre consulta, p/ preview e dedup corretos
    todo = [f for f in files if os.path.splitext(os.path.basename(f))[0] not in seen]
    print(f"{len(files)} transcripts locais; {len(files) - len(todo)} já no Supabase; "
          f"{len(todo)} a enviar")

    if dry:
        for f in todo[:25]:
            sid = os.path.splitext(os.path.basename(f))[0]
            print(f"  [dry] {sid[:8]}… cwd={cwd_of(f)}")
        if len(todo) > 25:
            print(f"  ... +{len(todo) - 25}")
        return 0

    done = 0
    for f in todo:
        sid = os.path.splitext(os.path.basename(f))[0]
        payload = json.dumps({"session_id": sid, "transcript_path": f,
                              "cwd": cwd_of(f) or os.getcwd(),
                              "hook_event_name": "SessionEnd", "reason": "backfill"})
        subprocess.run([sys.executable, CAPTURE], input=payload, text=True, capture_output=True)
        done += 1
        if done % 20 == 0:
            print(f"  {done}/{len(todo)}")
    print(f"enviadas {done} sessões")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
