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
sys.path.insert(0, os.path.join(REPO, "hooks"))
from store import get_store  # noqa: E402
from capture_session import is_internal, parse_transcript  # noqa: E402
CAPTURE = os.path.join(HERE, "..", "hooks", "capture_session.py")


def project_dirs():
    """Todos os ~/.claude*/projects (claude pessoal + claude-work + claude-personal)."""
    return sorted(d for d in glob.glob(os.path.join(HOME, ".claude*", "projects"))
                  if os.path.isdir(d))


def sid_of(path):
    return os.path.splitext(os.path.basename(path))[0]


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
    pending = [f for f in files if sid_of(f) not in seen]
    todo, empty, internal = [], 0, 0
    for f in pending:   # o capture pularia estas; contá-las à parte fecha a conta
        content, *_, user_texts = parse_transcript(f)
        if not content:
            empty += 1
        elif is_internal(user_texts):
            internal += 1
        else:
            todo.append(f)
    print(f"{len(files)} transcripts locais; {len(files) - len(pending)} já no banco; "
          f"{empty} vazias; {internal} internas do hub; {len(todo)} a enviar")

    if dry:
        for f in todo[:25]:
            print(f"  [dry] {sid_of(f)[:8]}… cwd={cwd_of(f)}")
        if len(todo) > 25:
            print(f"  ... +{len(todo) - 25}")
        return 0

    for i, f in enumerate(todo, 1):
        payload = json.dumps({"session_id": sid_of(f), "transcript_path": f,
                              "cwd": cwd_of(f) or os.getcwd(),
                              "hook_event_name": "SessionEnd", "reason": "backfill"})
        subprocess.run([sys.executable, CAPTURE], input=payload, text=True, capture_output=True)
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}")
    # o capture sempre sai 0 (é hook); a prova de que salvou é a sessão estar no banco
    now = store.session_ids() if todo else seen
    failed = [sid_of(f) for f in todo if sid_of(f) not in now]
    for sid in failed:
        print(f"  FALHOU {sid[:8]}… (ver hooks/capture.log)", file=sys.stderr)
    print(f"enviadas {len(todo) - len(failed)} sessões; {len(failed)} falharam")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
