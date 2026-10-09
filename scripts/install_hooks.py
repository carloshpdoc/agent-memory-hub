#!/usr/bin/env python3
"""
agent-memory-hub — install the Claude Code hooks idempotently.

Adds SessionStart (recall), Stop (capture checkpoint) and SessionEnd (capture)
hooks to the Claude Code settings.json, using the absolute path of this clone.
Safe to re-run: if a hook pointing at this repo already exists for an event, it is
left alone. Does not remove or touch other hooks.

Also wires Codex (~/.codex/hooks.json, when Codex is installed): SessionStart recall and a
Stop capture that parses Codex rollouts (hooks/codex_capture.py). A hub hook pointing at
the wrong script is corrected in place; other hooks are never touched. Codex runs a new or
changed hook only after you trust it in `/hooks`.

Config: CLAUDE_SETTINGS (default ~/.claude/settings.json),
        CODEX_HOOKS (default ~/.codex/hooks.json).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MARKER = "agent-memory-hub/hooks/"

SETTINGS = os.environ.get("CLAUDE_SETTINGS") or os.path.expanduser("~/.claude/settings.json")
CODEX_HOOKS_PATH = os.environ.get("CODEX_HOOKS") or os.path.expanduser("~/.codex/hooks.json")

HOOKS = {
    "SessionStart": {"type": "command",
                     "command": f"python3 {REPO}/hooks/recall_session.py", "timeout": 15},
    "Stop": {"type": "command",
             "command": f'payload=$(cat); printf \'%s\' "$payload" | python3 {REPO}/hooks/capture_session.py >/dev/null 2>&1 &'},
    "SessionEnd": {"type": "command",
                   "command": f"python3 {REPO}/hooks/capture_session.py", "timeout": 20},
}


CODEX_HOOKS = {
    "SessionStart": {"type": "command",
                     "command": f"python3 {REPO}/hooks/recall_session.py", "timeout": 15},
    # sem `&`: o script se desvincula sozinho (o Codex mata filhos em background do hook)
    "Stop": {"type": "command",
             "command": f"python3 {REPO}/hooks/codex_capture.py", "timeout": 15},
}


def merge_codex(cfg, wanted):
    """Returns (cfg, changes). Per event: a hub hook (MARKER in command) with a different
    command is rewritten in place (keeps its slot); missing ones get a new group."""
    hooks = cfg.setdefault("hooks", {})
    changes = []
    for event, entry in wanted.items():
        groups = hooks.setdefault(event, [])
        mine = [h for g in groups for h in g.get("hooks", []) if MARKER in h.get("command", "")]
        if not mine:
            groups.append({"matcher": "", "hooks": [dict(entry)]})
            changes.append(f"{event} (novo)")
        elif mine[0].get("command") != entry["command"]:
            mine[0].clear()
            mine[0].update(entry)
            changes.append(f"{event} (corrigido)")
    return cfg, changes


def install_codex():
    if not os.path.isdir(os.path.dirname(CODEX_HOOKS_PATH)):
        return
    cfg = json.load(open(CODEX_HOOKS_PATH)) if os.path.exists(CODEX_HOOKS_PATH) else {}
    cfg, changes = merge_codex(cfg, CODEX_HOOKS)
    if not changes:
        print(f"hooks do Codex já instalados em {CODEX_HOOKS_PATH}")
        return
    with open(CODEX_HOOKS_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"hooks do Codex: {', '.join(changes)} em {CODEX_HOOKS_PATH}")
    print("  abra o Codex e aprove em /hooks: hook novo ou alterado fica parado até ser confiado")


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
    else:
        with open(SETTINGS, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        print(f"hooks instalados ({', '.join(changed)}) em {SETTINGS}")
    install_codex()
    return 0


if __name__ == "__main__":
    sys.exit(main())
