"""
Ponte de eventos para o Agent Console: entrega ao app quando ele está no ar, spool quando
não, e nunca atrapalha o agente. Offline: o "app" é um servidor HTTP local de teste.
"""
import io
import json
import os
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import console_bridge as cb

PAYLOAD = {"hook_event_name": "PostToolUse", "session_id": "S1", "cwd": "/r",
           "tool_name": "Bash", "tool_input": {"command": "ls -la", "description": "x"},
           "prompt": "segredo do prompt"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "console"
    monkeypatch.setattr(cb, "HOME_DIR", str(d))
    monkeypatch.setattr(cb, "ENDPOINT", str(d / "endpoint.json"))
    monkeypatch.setattr(cb, "SPOOL", str(d / "spool.jsonl"))
    monkeypatch.delenv("AGENT_CONSOLE_SESSION", raising=False)
    return d


def run(payload, tool="claude"):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return cb.main(["--tool", tool], io.StringIO(raw))


def spooled(home):
    p = home / "spool.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_without_app_event_goes_to_private_spool_without_prompt_text(home):
    assert run(PAYLOAD) == 0
    [ev] = spooled(home)
    assert ev["tool"] == "claude" and ev["event"] == "PostToolUse" and ev["session_id"] == "S1"
    assert ev["tool_name"] == "Bash" and ev["detail"] == "ls -la"
    assert "segredo" not in json.dumps(ev)
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "spool.jsonl").st_mode) == 0o600


def test_session_opened_by_the_console_is_ignored(home, monkeypatch):
    monkeypatch.setenv("AGENT_CONSOLE_SESSION", "s1")
    assert run(PAYLOAD) == 0
    assert spooled(home) == []


@pytest.mark.parametrize("raw", ["", "não é json", "[1,2]", "x" * (cb.MAX_PAYLOAD + 10)])
def test_bad_input_is_dropped_quietly(home, raw):
    assert run(raw) == 0
    assert spooled(home) == []


def test_unknown_tool_is_dropped(home):
    assert run(PAYLOAD, tool="vim") == 0
    assert spooled(home) == []


def test_spool_rotates_once_at_the_cap(home, monkeypatch):
    monkeypatch.setattr(cb, "SPOOL_MAX", 200)
    for _ in range(6):
        run(PAYLOAD)
    assert (home / "spool.jsonl.1").exists()
    assert os.path.getsize(home / "spool.jsonl") < 400


class _App:
    """Servidor no papel do console: aceita POST /hook com o token certo."""

    def __init__(self, status=204):
        got, code = [], status

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append((self.path, self.headers.get("X-Agent-Console-Token"), json.loads(body)))
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.got = got
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def endpoint(self, home, pid=None):
        home.mkdir(exist_ok=True)
        (home / "endpoint.json").write_text(json.dumps(
            {"port": self.srv.server_port, "token": "tok", "pid": pid or os.getpid()}))

    def close(self):
        self.srv.shutdown()


def test_with_app_running_event_is_delivered_with_token_and_not_spooled(home):
    app = _App()
    try:
        app.endpoint(home)
        assert run(PAYLOAD, tool="codex") == 0
        [(path, token, ev)] = app.got
        assert path == "/hook" and token == "tok" and ev["tool"] == "codex"
        assert spooled(home) == []
    finally:
        app.close()


def test_app_refusing_the_event_falls_back_to_spool(home):
    app = _App(status=403)
    try:
        app.endpoint(home)
        run(PAYLOAD)
        assert len(app.got) == 1 and len(spooled(home)) == 1
    finally:
        app.close()


def test_stale_endpoint_of_a_dead_app_is_not_contacted(home):
    app = _App()
    try:
        app.endpoint(home, pid=2 ** 22 + 12345)       # pid que não existe
        run(PAYLOAD)
        assert app.got == [] and len(spooled(home)) == 1
    finally:
        app.close()


def test_dead_port_does_not_hold_the_agent(home):
    home.mkdir()
    (home / "endpoint.json").write_text(json.dumps({"port": 9, "token": "t", "pid": os.getpid()}))
    t = time.time()
    run(PAYLOAD)
    assert time.time() - t < 2 * cb.POST_TIMEOUT + 0.5
    assert len(spooled(home)) == 1
