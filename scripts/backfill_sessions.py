#!/usr/bin/env python3
"""
agent-memory-hub — backfill local Claude Code sessions into Supabase.

Finds this machine's transcripts across every config dir (~/.claude/projects,
~/.claude-work/projects, ~/.claude-personal/projects, ...), skips the ones already
in Supabase, and captures the rest by reusing capture_session.py (so they get the
same parsing + summary). Idempotent (upsert by session_id).

Only top-level session transcripts are picked up (projects/<dir>/<session>.jsonl);
subagent transcripts (projects/<dir>/<session>/subagents/*.jsonl) are part of the
main session and intentionally skipped.

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

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from store import get_store  # noqa: E402
CAPTURE = os.path.join(HERE, "..", "hooks", "capture_session.py")


def project_dirs():
    """Todos os ~/.claude*/projects (claude pessoal + claude-work + claude-personal)."""
    return sorted(d for d in glob.glob(os.path.join(HOME, ".claude*", "projects"))
                  if os.path.isdir(d))


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
    store = get_store()
    if not store.configured:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    dirs = project_dirs()
    files = sorted(f for d in dirs for f in glob.glob(os.path.join(d, "*", "*.jsonl")))
    print("config dirs:", ", ".join(os.path.dirname(d).split("/")[-1] for d in dirs))
    seen = store.session_ids()   # sempre consulta, p/ preview e dedup corretos
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
