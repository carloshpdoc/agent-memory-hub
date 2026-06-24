#!/usr/bin/env python3
"""
agent-memory-hub — install the Claude Code hooks idempotently.

Adds SessionStart (recall), Stop (capture checkpoint) and SessionEnd (capture)
hooks to the Claude Code settings.json, using the absolute path of this clone.
Safe to re-run: if a hook pointing at this repo already exists for an event, it is
left alone. Does not remove or touch other hooks.

Config: CLAUDE_SETTINGS (default ~/.claude/settings.json).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MARKER = "agent-memory-hub/hooks/"

SETTINGS = os.environ.get("CLAUDE_SETTINGS") or os.path.expanduser("~/.claude/settings.json")

HOOKS = {
    "SessionStart": {"type": "command",
                     "command": f"python3 {REPO}/hooks/recall_session.py", "timeout": 15},
    "Stop": {"type": "command",
             "command": f'payload=$(cat); printf \'%s\' "$payload" | python3 {REPO}/hooks/capture_session.py >/dev/null 2>&1 &'},
    "SessionEnd": {"type": "command",
                   "command": f"python3 {REPO}/hooks/capture_session.py", "timeout": 20},
}


def main():
    if os.path.exists(SETTINGS):
        with open(SETTINGS) as f:
            cfg = json.load(f)
    else:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        cfg = {}

    hooks = cfg.setdefault("hooks", {})
    changed = []
    for event, entry in HOOKS.items():
        groups = hooks.setdefault(event, [])
        already = any(MARKER in h.get("command", "")
                      for g in groups for h in g.get("hooks", []))
        if already:
            continue
        groups.append({"matcher": "", "hooks": [entry]})
        changed.append(event)

    if not changed:
        print(f"hooks já instalados em {SETTINGS}")
        return 0

    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"hooks instalados ({', '.join(changed)}) em {SETTINGS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
