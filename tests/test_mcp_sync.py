"""Unit tests for mcp_sync (config sync phase 3) — redaction, target resolution, TOML."""
import json

import pytest

import mcp_sync as ms

tomllib = ms.toml_lib()
needs_toml = pytest.mark.skipif(tomllib is None, reason="sem tomllib/tomli neste Python")


def test_syncable_skips_hub_and_app_bundled_but_not_app_paths_in_env():
    assert not ms.syncable("agent-memory-hub", {"command": "python3"})
    assert not ms.syncable("node_repl", {"command": "/Applications/ChatGPT.app/Contents/x/node_repl"})
    assert ms.syncable("godot", {"command": "npx", "env": {"GODOT_PATH": "/Applications/Godot.app/x"}})


def test_to_item_keeps_secrets_local_and_home_portable(monkeypatch):
    monkeypatch.setattr(ms.ss, "HOME", "/Users/me")
    cfg = {"command": "/Users/me/venv/bin/python", "args": ["-m", "x"],
           "env": {"API_KEY": "abc123def456", "LEVEL": "debug"},
           "http_headers": {"Authorization": "Bearer xyz"}}
    name, files = ms.to_item("codex", "srv", cfg)
    assert name == "codex:srv"
    c = files["config"]
    assert c["command"] == "__HOME__/venv/bin/python"
    assert c["env"] == {"API_KEY": ms.LOCAL, "LEVEL": "debug"}
    assert c["http_headers"]["Authorization"] == ms.LOCAL


def test_resolve_uses_local_secret_or_reports_missing(monkeypatch):
    monkeypatch.setattr(ms.shutil, "which", lambda c: "/usr/bin/" + c)
    files = {"config": {"command": "npx", "env": {"API_KEY": ms.LOCAL}}}
    cfg, why = ms.resolve_for_target(files, None)
    assert cfg is None and "API_KEY" in why
    cfg, why = ms.resolve_for_target(files, {"env": {"API_KEY": "s3cret"}})
    assert why is None and cfg["env"]["API_KEY"] == "s3cret"


def test_resolve_reports_missing_command_and_app_dependency(monkeypatch):
    monkeypatch.setattr(ms.shutil, "which", lambda c: None)
    assert "comando não encontrado" in ms.resolve_for_target({"config": {"command": "uv"}}, None)[1]
    monkeypatch.setattr(ms.shutil, "which", lambda c: "/x/" + c)
    why = ms.resolve_for_target({"config": {"command": "npx", "env": {"GODOT": "/nonexistent/Godot.app"}}}, None)[1]
    assert "falta dependência" in why


@needs_toml
def test_codex_table_is_valid_toml_with_env_subtable():
    text = ms.codex_table("my-srv", {"command": "npx", "args": ["-y", "pkg@1"], "startup_timeout_sec": 30,
                                     "enabled": True, "env": {"A": "1", "B C": "x\"y"}})
    data = tomllib.loads(text)["mcp_servers"]["my-srv"]
    assert data["args"] == ["-y", "pkg@1"] and data["startup_timeout_sec"] == 30 and data["enabled"] is True
    assert data["env"] == {"A": "1", "B C": 'x"y'}


def test_install_and_uninstall_cursor_json(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    ms.install("cursor", str(path), {}, "srv", {"command": "npx"}, str(tmp_path / "bk"))
    assert set(json.loads(path.read_text())["mcpServers"]) == {"other", "srv"}
    ms.uninstall("cursor", str(path), {}, "srv", str(tmp_path / "bk"))
    assert set(json.loads(path.read_text())["mcpServers"]) == {"other"}


@needs_toml
def test_install_codex_appends_validated_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model = "x"\n\n[mcp_servers.a]\ncommand = "npx"\n')
    ms.install("codex", str(path), {}, "b", {"url": "https://h/mcp"}, str(tmp_path / "bk"))
    servers = tomllib.loads(path.read_text())["mcp_servers"]
    assert set(servers) == {"a", "b"} and servers["b"]["url"] == "https://h/mcp"
