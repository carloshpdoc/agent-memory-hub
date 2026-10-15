#!/usr/bin/env python3
"""
agent-memory-hub — fill sessions.usage for sessions captured before it existed.

Capture writes the usage of every new session (hooks/usage.py). This reads the transcripts
still on THIS machine (Claude Code in every ~/.claude* account, Codex rollouts) and patches
the sessions already in the hub whose usage is empty. Idempotent: a session with usage is
never touched again. Run on each machine (`mem import` runs it).

Also fills metadata.git (branch) of Codex sessions saved before the adapter recorded it.

  python3 scripts/backfill_usage.py [--dry-run]
"""
import glob
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
sys.path.insert(0, os.path.join(HERE, "adapters"))
import memory_client as mc  # noqa: E402
from usage import claude_usage, codex_usage, cost_usd  # noqa: E402
from codex import git_from_meta, session_meta  # noqa: E402

HOME = os.path.expanduser("~")


def claude_files(home=HOME):
    """{session_id: transcript} de todas as contas (~/.claude, ~/.claude-*)."""
    out = {}
    for path in glob.glob(os.path.join(home, ".claude*", "projects", "*", "*.jsonl")):
        out.setdefault(os.path.splitext(os.path.basename(path))[0], path)
    return out


def codex_files(home=HOME):
    out = {}
    for path in glob.glob(os.path.join(home, ".codex", "sessions", "*", "*", "*", "*.jsonl")):
        sid = session_meta(path).get("id")
        if sid:
            out.setdefault(sid, path)
    return out


def main(argv):
    dry = "--dry-run" in argv
    if not (mc.URL and mc.KEY):
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes no .env", file=sys.stderr)
        return 1
    missing = {r["session_id"]: r["tool"] for r in
               mc.rest_all("sessions?select=session_id,tool&usage=is.null&tool=in.(claude-code,codex)")}
    sources = {"claude-code": (claude_files(), claude_usage), "codex": (codex_files(), codex_usage)}
    done = cost = 0
    for sid, tool in missing.items():
        files, fn = sources[tool]
        path = files.get(sid)
        usage = fn(path) if path else None
        if not usage:
            continue
        if not dry:
            mc.write(f"sessions?session_id=eq.{urllib.parse.quote(sid)}", {"usage": usage})
        done += 1
        cost += cost_usd(usage)[0]
    print(f"uso preenchido em {done} de {len(missing)} sessões sem uso "
          f"(as outras não têm transcript nesta máquina){' (dry-run)' if dry else ''}; "
          f"equivalente de API: US$ {cost:,.2f}")

    # branch das sessões do Codex gravadas antes do adaptador salvar metadata.git: é o que
    # junta Claude e Codex na mesma tarefa no mem cost
    codex = sources["codex"][0]
    branched = 0
    for r in mc.rest_all("sessions?select=session_id,metadata&tool=eq.codex&metadata->git=is.null"):
        path = codex.get(r["session_id"])
        git = git_from_meta(session_meta(path)) if path else None
        if not git:
            continue
        if not dry:
            mc.write(f"sessions?session_id=eq.{urllib.parse.quote(r['session_id'])}",
                     {"metadata": {**(r.get("metadata") or {}), "git": git}})
        branched += 1
    print(f"branch do git preenchido em {branched} sessões do Codex{' (dry-run)' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
