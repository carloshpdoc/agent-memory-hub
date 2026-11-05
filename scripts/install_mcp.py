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

Also registers the Agent Console partner server (app/dist/partnerMcp.js, built by
`npm run build` in app/) as `agent-console` in Claude Code (user scope) and Codex, so a
terminal session can delegate to another agent that runs in the console. The call waits
for the result: Codex gets tool_timeout_sec above the console's wait; Claude Code's stdio
limits (30 min idle, ~28 h total, backgrounded after 2 min) already cover it.

Config: CODEX_CONFIG, CURSOR_MCP (override the paths above).
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "agent-memory-hub"
SERVER = os.path.join(HERE, "mcp_server.py")
CONSOLE = "agent-console"
CONSOLE_SCRIPT = os.path.join(os.path.dirname(HERE), "app", "dist", "partnerMcp.js")
CONSOLE_TIMEOUT_SEC = 1500   # acima da espera do console (20 min) + margem


def codex_block(python, server):
    return (f'\n[mcp_servers.{NAME}]\n'
            f'command = {json.dumps(python)}\n'
            f'args = [{json.dumps(server)}]\n')


def add_to_codex(text, python, server):
    """Returns (new_text, changed). Appends the table only if it isn't there yet."""
    if f"[mcp_servers.{NAME}]" in text:
        return text, False
    return text.rstrip("\n") + "\n" + codex_block(python, server), True


def add_console_to_codex(text, node, script):
    """Returns (new_text, changed). The partner server, with a tool timeout that covers the wait."""
    if f"[mcp_servers.{CONSOLE}]" in text:
        return text, False
    block = (f'\n[mcp_servers.{CONSOLE}]\n'
             f'command = {json.dumps(node)}\n'
             f'args = [{json.dumps(script)}, "--from", "codex"]\n'
             f'tool_timeout_sec = {CONSOLE_TIMEOUT_SEC}\n')
    return text.rstrip("\n") + "\n" + block, True


def console_claude_argv(node, script):
    """`claude mcp add-json` for the partner server, user scope (every project)."""
    entry = {"type": "stdio", "command": node, "args": [script, "--from", "claude"]}
    return ["claude", "mcp", "add-json", "--scope", "user", CONSOLE, json.dumps(entry)]


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

    done += install_console()

    for line in done or ["nem Codex nem Cursor instalados nesta máquina; nada a fazer"]:
        print(f"    {line}")
    if done:
        print("    reinicie o app para carregar as tools do hub")
    return 0


def install_console():
    """Partner server in Claude Code and Codex. Skipped with a reason when the app isn't built."""
    node = shutil.which("node")
    if not os.path.exists(CONSOLE_SCRIPT) or not node:
        why = "app não compilado (rode npm run build em app/)" if node else "node não está no PATH"
        return [f"agent-console não registrado: {why}"]
    out = []
    codex = os.environ.get("CODEX_CONFIG") or os.path.expanduser("~/.codex/config.toml")
    if os.path.isdir(os.path.dirname(codex)):
        text = open(codex).read() if os.path.exists(codex) else ""
        new, changed = add_console_to_codex(text, node, CONSOLE_SCRIPT)
        if changed:
            with open(codex, "w") as f:
                f.write(new)
        out.append(f"Codex  agent-console {'registrado' if changed else 'já registrado'}")
    if shutil.which("claude"):
        present = subprocess.run(["claude", "mcp", "get", CONSOLE], capture_output=True, text=True).returncode == 0
        if not present:
            r = subprocess.run(console_claude_argv(node, CONSOLE_SCRIPT), capture_output=True, text=True)
            if r.returncode != 0:
                out.append(f"Claude agent-console FALHOU: {(r.stderr or r.stdout).strip()[:200]}")
                return out
        out.append(f"Claude agent-console {'já registrado' if present else 'registrado'} (escopo user)")
    return out


if __name__ == "__main__":
    sys.exit(main())
