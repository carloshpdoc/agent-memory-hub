#!/usr/bin/env python3
"""
agent-memory-hub — register the hub's MCP server in Codex and Cursor, idempotently.

Claude Code gets memory through its hooks (and `claude mcp add`); Codex and Cursor only
*send* sessions to the hub unless they can query it. This adds the stdio server
(scripts/mcp_server.py) to:
  Codex   ~/.codex/config.toml   [mcp_servers.agent-memory-hub]
  Cursor  ~/.cursor/mcp.json     mcpServers["agent-memory-hub"]
Only for tools installed on this machine (config dir exists). Never edits or removes an
existing entry with the same name, and leaves every other server untouched. Restart the
app afterwards.

Config: CODEX_CONFIG, CURSOR_MCP (override the paths above).
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "agent-memory-hub"
SERVER = os.path.join(HERE, "mcp_server.py")


def codex_block(python, server):
    return (f'\n[mcp_servers.{NAME}]\n'
            f'command = {json.dumps(python)}\n'
            f'args = [{json.dumps(server)}]\n')


def add_to_codex(text, python, server):
    """Returns (new_text, changed). Appends the table only if it isn't there yet."""
    if f"[mcp_servers.{NAME}]" in text:
        return text, False
    return text.rstrip("\n") + "\n" + codex_block(python, server), True


def add_to_cursor(cfg, python, server):
    """Returns (new_cfg, changed). Adds the server only if the name is free."""
    if not isinstance(cfg, dict):
        raise ValueError("mcp.json não é um objeto JSON")
    servers = cfg.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers não é um objeto JSON")
    if NAME in servers:
        return cfg, False
    servers[NAME] = {"command": python, "args": [server]}
    return cfg, True


def main():
    # the PATH entry (e.g. /opt/homebrew/bin/python3) survives Python upgrades; the
    # resolved sys.executable (.../python@3.14/...) would break on the next one
    python = shutil.which("python3") or sys.executable
    done = []

    codex = os.environ.get("CODEX_CONFIG") or os.path.expanduser("~/.codex/config.toml")
    if os.path.isdir(os.path.dirname(codex)):
        text = open(codex).read() if os.path.exists(codex) else ""
        new, changed = add_to_codex(text, python, SERVER)
        if changed:
            with open(codex, "w") as f:
                f.write(new)
        done.append(f"Codex  {'registrado' if changed else 'já registrado'}  ({codex})")

    cursor = os.environ.get("CURSOR_MCP") or os.path.expanduser("~/.cursor/mcp.json")
    if os.path.isdir(os.path.dirname(cursor)):
        cfg = json.load(open(cursor)) if os.path.exists(cursor) else {}
        new, changed = add_to_cursor(cfg, python, SERVER)
        if changed:
            with open(cursor, "w") as f:
                json.dump(new, f, indent=4)
                f.write("\n")
        done.append(f"Cursor {'registrado' if changed else 'já registrado'}  ({cursor})")

    for line in done or ["nem Codex nem Cursor instalados nesta máquina; nada a fazer"]:
        print(f"    {line}")
    if done:
        print("    reinicie o app para carregar as tools do hub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
