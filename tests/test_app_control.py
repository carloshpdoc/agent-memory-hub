"""amh app: fala com a rota /control do app pelo endpoint.json e só aprova "uma vez"."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app_control as a

PERM = {"key": "s1", "requestId": "p1", "label": "Codex · d1", "cwd": "/r", "title": "npm test",
        "options": [{"optionId": "always", "name": "Sempre", "kind": "allow_always"},
                    {"optionId": "ok", "name": "Permitir", "kind": "allow_once"},
                    {"optionId": "no", "name": "Negar", "kind": "reject_once"}]}


@pytest.fixture
def app(tmp_path, monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get(a.TOKEN_HEADER), body))
            if body["cmd"] == "permissions":
                code, out = 200, {"ok": True, "result": [PERM]}
            elif body["cmd"] == "answer":
                code, out = 200, {"ok": True, "result": {"ok": True}}
            else:
                code, out = 400, {"ok": False, "error": "comando desconhecido"}
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = tmp_path / "endpoint.json"
    ep.write_text(json.dumps({"port": srv.server_address[1], "token": "T", "pid": os.getpid()}))
    monkeypatch.setattr(a, "ENDPOINT", str(ep))
    yield seen
    srv.shutdown()


def test_permitir_escolhe_uma_vez_nunca_sempre(app):
    assert "Permitir" in a.answer("s1", None, a.ALLOW)
    path, token, body = app[-1]
    assert (path, token, body) == ("/control", "T", {"cmd": "answer", "key": "s1", "requestId": "p1", "optionId": "ok"})
    a.answer("s1", "p1", a.REJECT)
    assert app[-1][2]["optionId"] == "no"


def test_pedido_errado_e_erro_do_app(app):
    with pytest.raises(a.AppError, match="não está pendente"):
        a.answer("s1", "p9", a.ALLOW)
    with pytest.raises(a.AppError, match="nenhum pedido pendente em s2"):
        a.answer("s2", None, a.ALLOW)
    with pytest.raises(a.AppError, match="comando desconhecido"):
        a.call("x")


def test_interativo_responde_a_opcao_digitada_e_pula_o_resto(app):
    answers = iter(["2", "", "9"])
    assert a.interactive([PERM, PERM, PERM], ask=lambda _: next(answers)) == 1
    assert [b["optionId"] for _, _, b in app if b["cmd"] == "answer"] == ["ok"]


def test_app_fechado(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "ENDPOINT", str(tmp_path / "nada.json"))
    with pytest.raises(a.AppError, match="não está aberto"):
        a.call("list")
    assert a.main(["ver"]) == 2
