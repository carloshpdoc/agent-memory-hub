"""Unit tests for install_mcp — config editing only, no filesystem."""
import json

import pytest

import install_mcp as im

PY, SRV = "/usr/bin/python3", "/hub/scripts/mcp_server.py"


def test_codex_appends_table_and_keeps_existing():
    text = '[mcp_servers.other]\ncommand = "npx"\n'
    new, changed = im.add_to_codex(text, PY, SRV)
    assert changed
    assert new.startswith(text)
    assert f'[mcp_servers.{im.NAME}]\ncommand = "{PY}"\nargs = ["{SRV}"]\n' in new


def test_codex_is_idempotent():
    once, _ = im.add_to_codex("", PY, SRV)
    twice, changed = im.add_to_codex(once, PY, SRV)
    assert not changed and twice == once


def test_codex_escapes_paths():
    new, _ = im.add_to_codex("", 'C:\\py "x"', SRV)
    assert 'command = "C:\\\\py \\"x\\""' in new


def test_cursor_adds_server_and_keeps_others():
    cfg = {"mcpServers": {"video-review": {"command": "x"}}}
    new, changed = im.add_to_cursor(cfg, PY, SRV)
    assert changed
    assert new["mcpServers"]["video-review"] == {"command": "x"}
    assert new["mcpServers"][im.NAME] == {"command": PY, "args": [SRV]}


def test_cursor_creates_section_and_never_overwrites():
    new, changed = im.add_to_cursor({}, PY, SRV)
    assert changed and im.NAME in new["mcpServers"]
    custom = {"mcpServers": {im.NAME: {"command": "mine"}}}
    same, changed = im.add_to_cursor(json.loads(json.dumps(custom)), PY, SRV)
    assert not changed and same == custom


def test_cursor_rejects_malformed_config():
    with pytest.raises(ValueError):
        im.add_to_cursor([], PY, SRV)
    with pytest.raises(ValueError):
        im.add_to_cursor({"mcpServers": []}, PY, SRV)


def test_console_codex_block_has_timeout_and_origin():
    new, changed = im.add_console_to_codex('[mcp_servers.other]\ncommand = "x"\n', "/bin/node", "/hub/app/dist/partnerMcp.js")
    assert changed and new.startswith('[mcp_servers.other]\ncommand = "x"\n')
    assert ('[mcp_servers.agent-console]\ncommand = "/bin/node"\n'
            'args = ["/hub/app/dist/partnerMcp.js", "--from", "codex"]\n'
            f"tool_timeout_sec = {im.CONSOLE_TIMEOUT_SEC}\n") in new
    again, changed2 = im.add_console_to_codex(new, "/bin/node", "/hub/app/dist/partnerMcp.js")
    assert not changed2 and again == new
    assert im.CONSOLE_TIMEOUT_SEC * 1000 > 20 * 60_000          # cobre a espera do console


def test_console_claude_is_user_scope_stdio():
    argv = im.console_claude_argv("/bin/node", "/p.js")
    assert argv[:6] == ["claude", "mcp", "add-json", "--scope", "user", "agent-console"]
    assert json.loads(argv[6]) == {"type": "stdio", "command": "/bin/node", "args": ["/p.js", "--from", "claude"]}


def test_console_codex_rewrites_stale_entry_in_place_and_keeps_neighbors():
    old = ('[mcp_servers.agent-console]\ncommand = "/mise/installs/node/24/bin/node"\nargs = ["/p.js", "--from", "codex"]\n'
           'tool_timeout_sec = 1500\n\n[mcp_servers.zeta]\ncommand = "z"\n')
    new, changed = im.add_console_to_codex('[a]\nx = 1\n\n' + old, "/shims/node", "/p.js")
    assert changed
    assert new == ('[a]\nx = 1\n\n[mcp_servers.agent-console]\ncommand = "/shims/node"\nargs = ["/p.js", "--from", "codex"]\n'
                   'tool_timeout_sec = 1500\n\n[mcp_servers.zeta]\ncommand = "z"\n')
    last, _ = im.add_console_to_codex('[a]\nx = 1\n\n[mcp_servers.agent-console]\ncommand = "/old"\n', "/shims/node", "/p.js")
    assert last.endswith('command = "/shims/node"\nargs = ["/p.js", "--from", "codex"]\ntool_timeout_sec = 1500\n')


def test_stable_node_prefers_mise_shim(tmp_path):
    shim = tmp_path / "node"
    shim.write_text("")
    assert im.stable_node("/u/.local/share/mise/installs/node/24.18.0/bin/node", str(shim)) == str(shim)
    assert im.stable_node("/opt/homebrew/bin/node", str(shim)) == "/opt/homebrew/bin/node"
    assert im.stable_node("/u/mise/installs/node/24/bin/node", str(tmp_path / "nao")) == "/u/mise/installs/node/24/bin/node"
    assert im.stable_node(None, str(shim)) is None
