#!/usr/bin/env python3
"""
agent-memory-hub — wire hooks/guard.py into every installed agent tool, idempotently.

  Claude Code  ~/.claude/settings.json   PreToolUse  (Bash|Edit|MultiEdit|Write|NotebookEdit)
  Codex        ~/.codex/hooks.json       PreToolUse  (Bash|apply_patch)   -> trust it in /hooks
  Cursor       ~/.cursor/hooks.json      beforeShellExecution + preToolUse (Write)
  Gemini CLI   ~/.gemini/settings.json   BeforeTool  (run_shell_command|write_file|replace)

Only tools whose config dir exists are touched; other hooks are never changed. With
`--replace SUBSTRING`, an existing hook whose command contains SUBSTRING (a previous
personal guard) is switched to this guard in place instead of running both.

The policy itself is guard.json (gitignored; start from guard.example.json).
Usage: python3 scripts/install_guard.py [--replace file-protection.py] [--dry-run]
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GUARD = os.path.join(REPO, "hooks", "guard.py")
MARKER = "agent-memory-hub/hooks/guard.py"


def command(tool):
    python = shutil.which("python3") or "python3"
    return f"{python} {GUARD} --tool {tool}"


def merge_nested(cfg, event, matcher, cmd, replace=None, extra=None):
    """Claude/Codex/Gemini shape: hooks[event] = [{matcher, hooks: [{type, command}]}].
    Returns a change label or None. Rewrites a legacy (replace) or outdated guard hook in
    place; otherwise appends a group."""
    groups = cfg.setdefault("hooks", {}).setdefault(event, [])
    hooks = [h for g in groups for h in g.get("hooks", [])]
    mine = [h for h in hooks if MARKER in h.get("command", "")]
    if mine:
        if mine[0].get("command") == cmd:
            return None
        mine[0]["command"] = cmd
        return f"{event} (atualizado)"
    legacy = [h for h in hooks if replace and replace in h.get("command", "")]
    if legacy:
        legacy[0]["command"] = cmd
        return f"{event} (substituiu {replace})"
    groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd, **(extra or {})}]})
    return f"{event} (novo)"


def merge_cursor(cfg, cmd):
    """Cursor shape: {version: 1, hooks: {event: [{command, matcher?}]}}."""
    cfg.setdefault("version", 1)
    hooks, changes = cfg.setdefault("hooks", {}), []
    for event, matcher in (("beforeShellExecution", None), ("preToolUse", "Write")):
        entries = hooks.setdefault(event, [])
        mine = [e for e in entries if MARKER in e.get("command", "")]
        if mine:
            if mine[0]["command"] != cmd:
                mine[0]["command"] = cmd
                changes.append(f"{event} (atualizado)")
            continue
        entries.append({"command": cmd, **({"matcher": matcher} if matcher else {})})
        changes.append(f"{event} (novo)")
    return changes


TARGETS = (
    # (tool, config path, kind, event, matcher)
    ("claude-code", "~/.claude/settings.json", "nested", "PreToolUse", "Bash|Edit|MultiEdit|Write|NotebookEdit"),
    ("codex", "~/.codex/hooks.json", "nested", "PreToolUse", "Bash|apply_patch"),
    ("cursor", "~/.cursor/hooks.json", "cursor", None, None),
    ("gemini", "~/.gemini/settings.json", "nested", "BeforeTool", "run_shell_command|write_file|replace"),
)


def main(argv):
    dry = "--dry-run" in argv
    replace = argv[argv.index("--replace") + 1] if "--replace" in argv else None
    if not os.path.exists(os.path.join(REPO, "guard.json")) and not os.environ.get("GUARD_POLICY"):
        print("aviso: sem guard.json — o guard vai permitir tudo. Copie guard.example.json para guard.json.")
    for tool, path, kind, event, matcher in TARGETS:
        path = os.path.expanduser(path)
        if not os.path.isdir(os.path.dirname(path)):
            continue
        cfg = json.load(open(path)) if os.path.exists(path) else {}
        cmd = command(tool)
        if kind == "cursor":
            changes = merge_cursor(cfg, cmd)
        else:
            change = merge_nested(cfg, event, matcher, cmd, replace,
                                  {"timeout": 10} if tool != "gemini" else {"timeout": 10000})
            changes = [change] if change else []
        if not changes:
            print(f"    {tool:<12} já instalado")
            continue
        if not dry:
            with open(path, "w") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
        print(f"    {tool:<12} {', '.join(changes)}{' (dry-run)' if dry else ''}  ({path})")
        if tool == "codex" and not dry:
            print("                 aprove no Codex: codex -> /hooks (hook novo ou alterado fica parado até lá)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
