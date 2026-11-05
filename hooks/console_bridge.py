#!/usr/bin/env python3
"""
agent-memory-hub — ponte de eventos das sessões abertas FORA do Agent Console (app/).

O console já vê pelo ACP as sessões que ele mesmo abre. Este hook cobre as outras (Claude
ou Codex no terminal comum) para que apareçam no app. Regras, tiradas do hook do Orca:

- nunca trava nem muda o agente: não decide nada, não imprime nada, sempre sai 0,
  e a entrega ao app tem timeout curto;
- sessão aberta pelo console (AGENT_CONSOLE_SESSION) é ignorada: o app já a vê;
- app fechado ou fora do ar: o evento vai para um spool local (dir 700, arquivo 600),
  com teto de tamanho e uma rotação, que o app lê ao abrir.

Uso no hook: python3 <repo>/hooks/console_bridge.py --tool claude|codex
Config: AGENT_CONSOLE_HOME (default ~/.agent-console).
"""
import json
import os
import sys
import time
import urllib.request

HOME_DIR = os.environ.get("AGENT_CONSOLE_HOME") or os.path.expanduser("~/.agent-console")
ENDPOINT = os.path.join(HOME_DIR, "endpoint.json")   # o app grava {port, token, pid} ao abrir
SPOOL = os.path.join(HOME_DIR, "spool.jsonl")
SPOOL_MAX = 5 * 1024 * 1024
POST_TIMEOUT = 0.5
MAX_PAYLOAD = 256 * 1024
DETAIL_MAX = 160
TOOLS = ("claude", "codex")


def _short(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= DETAIL_MAX else s[:DETAIL_MAX - 1] + "…"


def build_event(tool, payload):
    """Só o necessário para mostrar a sessão no app; o texto do prompt não sai daqui."""
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    detail = ti.get("command") or ti.get("file_path") or ti.get("path") or ti.get("pattern")
    ev = {
        "v": 1,
        "tool": tool,
        "event": payload.get("hook_event_name") or "?",
        "session_id": payload.get("session_id") or "",
        "cwd": payload.get("cwd") or "",
        "ts": int(time.time() * 1000),
    }
    if payload.get("tool_name"):
        ev["tool_name"] = str(payload["tool_name"])
    if detail:
        ev["detail"] = _short(detail)
    return ev


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def post(ev):
    """True se o app aceitou o evento."""
    try:
        with open(ENDPOINT) as f:
            ep = json.load(f)
        port, token = int(ep["port"]), str(ep["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not _pid_alive(ep.get("pid")):
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook", data=json.dumps(ev).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Agent-Console-Token": token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # nunca por proxy
    try:
        with opener.open(req, timeout=POST_TIMEOUT) as r:
            return r.status == 204
    except Exception:
        return False


def spool(ev):
    os.makedirs(HOME_DIR, mode=0o700, exist_ok=True)
    try:
        if os.path.getsize(SPOOL) >= SPOOL_MAX:
            os.replace(SPOOL, SPOOL + ".1")          # uma rotação: o anterior é descartado
    except OSError:
        pass
    fd = os.open(SPOOL, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def main(argv, stdin):
    tool = argv[argv.index("--tool") + 1] if "--tool" in argv[:-1] else ""
    raw = stdin.read(MAX_PAYLOAD + 1)
    if tool not in TOOLS or os.environ.get("AGENT_CONSOLE_SESSION") or len(raw) > MAX_PAYLOAD:
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    ev = build_event(tool, payload)
    if not post(ev):
        spool(ev)
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv[1:], sys.stdin)
    except Exception:
        pass        # o agente nunca paga por falha da ponte
    sys.exit(0)
