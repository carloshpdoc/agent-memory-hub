#!/usr/bin/env python3
"""
agent-memory-hub — nightly maintenance job (the loop that used to depend on you).

Capture is automatic, but turning sessions into memory was not: extraction, embeddings,
defrag and the profile were manual commands, so when nobody ran them the loop silently
stopped. This runs them on a schedule (launchd, see install_nightly.sh) and records the
outcome in nightly-status.json, which `mem health` reports.

Every night:
  1. codex     import new Codex CLI sessions (scripts/adapters/codex.py)
  2. cursor    import new Cursor sessions (scripts/adapters/cursor.py)
  3. extract   facts from new sessions (capped by EXTRACT_MAX_SESSIONS, default 30)
  4. embed     pending session embeddings
Once a week (NIGHTLY_WEEKLY_DAY, 0=Mon .. 6=Sun, default 6):
  5. defrag    dedupe and expire stale facts (non-destructive)
  6. profile   re-propose cross-project patterns for you to review (mem profile)
  7. digest    write DIGEST.md

Run it on ONE machine only: the database is shared, two runners would double the LLM cost.

Usage:
  python3 scripts/nightly.py             # what the schedule runs
  python3 scripts/nightly.py --weekly    # force the weekly steps today
  python3 scripts/nightly.py --dry-run   # print the plan, run nothing
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATUS_PATH = os.path.join(REPO, "nightly-status.json")
LOCK_PATH = os.path.join(REPO, ".nightly.lock")
# minutos por passo: um passo travado nao pode segurar os outros a noite toda
STEP_MINUTES = {"codex": 10, "cursor": 10, "extract": 120, "verify": 10, "embed": 15, "archive": 20, "defrag": 60, "profile": 30, "digest": 10}


def steps(weekly):
    py = sys.executable
    adapters = os.path.join(HERE, "adapters")
    plan = [
        ("codex", [py, os.path.join(adapters, "codex.py")], {}),
        ("cursor", [py, os.path.join(adapters, "cursor.py")], {}),
        ("extract", [py, os.path.join(HERE, "extract_facts.py"), "--loop"],
         {"EXTRACT_MAX_SESSIONS": os.environ.get("EXTRACT_MAX_SESSIONS", "30")}),
        ("verify", [py, os.path.join(HERE, "verify_facts.py")], {}),
        ("embed", [py, os.path.join(HERE, "embed_pending.py")], {}),
        ("archive", [py, os.path.join(HERE, "archive_transcripts.py")], {}),   # só com ARCHIVE_RAW=1
    ]
    if weekly:
        plan += [
            ("defrag", [py, os.path.join(HERE, "defrag_facts.py")], {}),
            ("profile", [py, os.path.join(HERE, "synthesize_profile.py")], {}),
            ("digest", [py, os.path.join(HERE, "weekly_digest.py")], {}),
        ]
    return plan


def run_step(name, cmd, extra_env):
    env = {**os.environ, **extra_env, "AMH_NO_CAPTURE": "1"}
    timeout = int(os.environ.get(f"NIGHTLY_{name.upper()}_MINUTES", STEP_MINUTES[name])) * 60
    started = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        ok, code = r.returncode == 0, r.returncode
        tail = (r.stdout.strip().splitlines() or [""])[-1][:300]
        err = (r.stderr.strip().splitlines() or [""])[-1][:300] if not ok else ""
    except subprocess.TimeoutExpired:
        ok, code, tail, err = False, None, "", f"timeout após {timeout // 60} min"
    seconds = round(time.monotonic() - started)
    print(f"[{name}] {'ok' if ok else 'FALHOU'} em {seconds}s  {tail}")
    if err:
        print(f"[{name}] erro: {err}", file=sys.stderr)
    return {"step": name, "ok": ok, "exit": code, "seconds": seconds, "last_line": tail, "error": err}


def push_ops_snapshot():
    """O dashboard le o status do nightly pelo ops_status: empurra na hora, sem throttle."""
    sys.path.insert(0, os.path.join(REPO, "hooks"))
    from capture_session import load_env, ENV_PATH
    import ops_snapshot
    ops_snapshot.push({**load_env(ENV_PATH), **os.environ}, force=True)


def main(argv):
    today = datetime.now()
    weekly_day = int(os.environ.get("NIGHTLY_WEEKLY_DAY", "6"))
    weekly = "--weekly" in argv or today.weekday() == weekly_day
    plan = steps(weekly)

    if "--dry-run" in argv:
        for name, cmd, extra in plan:
            print(f"{name:8} {' '.join(os.path.basename(c) for c in cmd[1:])} {extra or ''}")
        return 0

    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("outra execução do nightly está rodando; saindo", file=sys.stderr)
            return 1

        started_at = datetime.now(timezone.utc).isoformat()
        results = [run_step(name, cmd, extra) for name, cmd, extra in plan]
        status = {"started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
                  "weekly": weekly, "ok": all(r["ok"] for r in results), "steps": results}
        with open(STATUS_PATH, "w") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)
        push_ops_snapshot()
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
