"""
agent-memory-hub — `amh app`: controla o Agent Console pelo terminal (tmux, ssh, celular).

Fala com o app aberto pela rota /control da ponte local (o mesmo endpoint.json e token que
os hooks usam; nunca por proxy). Lista as sessões, mostra o que cada uma fez e responde os
pedidos de permissão. Quem decide é você, digitando: "permitir" escolhe só a opção de uma
vez (allow_once), nunca "sempre".
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
from console_bridge import ENDPOINT, _pid_alive  # noqa: E402

TIMEOUT = 10
TOKEN_HEADER = "X-Agent-Console-Token"
ALLOW, REJECT = "allow_once", "reject_once"

USAGE = """uso: amh app [comando]
  (nada)                         sessões abertas no app, com status e pedidos pendentes
  ver <sessão> [N]               últimas N entradas da sessão (padrão 15)
  permissoes                     pedidos de permissão pendentes; no terminal, pergunta um a um
  permitir <sessão> [pedido]     aprova uma vez (sem pedido: o único pendente da sessão)
  negar <sessão> [pedido]        nega o pedido"""


class AppError(Exception):
    pass


def call(cmd, **args):
    """Uma chamada à rota /control do app aberto; AppError com a mensagem do app se recusar."""
    try:
        with open(ENDPOINT) as f:
            ep = json.load(f)
        port, token = int(ep["port"]), str(ep["token"])
    except (OSError, ValueError, KeyError, TypeError):
        raise AppError("o Agent Console não está aberto (sem endpoint.json)")
    if not _pid_alive(ep.get("pid")):
        raise AppError("o Agent Console não está aberto (o endpoint é de um app que já fechou)")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/control", data=json.dumps({"cmd": cmd, **args}).encode(), method="POST",
        headers={"Content-Type": "application/json", TOKEN_HEADER: token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())["result"]
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        if e.code == 404:
            msg = "este app não tem o controle pelo terminal: reabra o Agent Console na versão nova"
        raise AppError(msg or f"o app recusou (HTTP {e.code})")
    except (urllib.error.URLError, OSError) as e:
        raise AppError(f"o app não respondeu: {e}")


def fmt_sessions(rows):
    if not rows:
        return "nenhuma sessão aberta no app."
    out = []
    for s in rows:
        flag = f"  [{s['pending']} permissão(ões) pendente(s)]" if s["pending"] else ""
        out.append(f"{s['key']:<4} {s['label']:<24} {s['status']:<16} {s['branch'] or s['cwd']}{flag}")
    return "\n".join(out)


def fmt_permission(p):
    opts = "\n".join(f"    {i}. {o['name']} ({o['kind']})" for i, o in enumerate(p["options"], 1))
    return f"{p['key']} {p['requestId']}  {p['label']} · {p['cwd']}\n  {p['title']}\n{opts}"


def pick_request(key, request_id):
    """O pedido a responder: o dado, ou o único pendente da sessão."""
    pending = [p for p in call("permissions") if p["key"] == key]
    if request_id:
        match = [p for p in pending if p["requestId"] == request_id]
        if not match:
            raise AppError(f"pedido {request_id} não está pendente em {key}")
        return match[0]
    if not pending:
        raise AppError(f"nenhum pedido pendente em {key}")
    if len(pending) > 1:
        ids = ", ".join(p["requestId"] for p in pending)
        raise AppError(f"{key} tem {len(pending)} pedidos pendentes ({ids}): diga qual")
    return pending[0]


def option_of(p, kind):
    for o in p["options"]:
        if o["kind"] == kind:
            return o
    return None


def answer(key, request_id, kind):
    p = pick_request(key, request_id)
    opt = option_of(p, kind)
    if opt is None and kind == ALLOW:
        names = ", ".join(o["name"] for o in p["options"])
        raise AppError(f"o pedido não oferece permitir uma vez (opções: {names}); responda pela tela")
    # negar sem opção de recusa = cancelar o pedido (o protocolo aceita)
    call("answer", key=key, requestId=p["requestId"], optionId=opt["optionId"] if opt else None)
    return f"{key} {p['requestId']}: {opt['name'] if opt else 'cancelado'} — {p['title']}"


def interactive(perms, ask=input):
    """Um a um: número da opção, Enter pula. Devolve quantos foram respondidos."""
    done = 0
    for p in perms:
        print(fmt_permission(p))
        choice = ask("  opção (Enter pula): ").strip()
        if not choice:
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(p["options"]):
            print("  opção inválida, pulei.")
            continue
        opt = p["options"][int(choice) - 1]
        try:
            call("answer", key=p["key"], requestId=p["requestId"], optionId=opt["optionId"])
            print(f"  -> {opt['name']}")
            done += 1
        except AppError as e:
            print(f"  não respondeu: {e}")
    return done


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("lista", [])
    try:
        if cmd in ("lista", "ls"):
            print(fmt_sessions(call("list")))
        elif cmd == "ver" and rest:
            n = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
            r = call("show", key=rest[0], **({"n": n} if n else {}))
            print(f"{rest[0]} {r['label']} · {r['status']} · {len(r['entries'])} de {r['total']} entradas\n")
            print("\n\n".join(r["entries"]))
        elif cmd == "permissoes":
            perms = call("permissions")
            if not perms:
                print("nenhum pedido de permissão pendente.")
            elif sys.stdin.isatty():
                interactive(perms)
            else:
                print("\n\n".join(fmt_permission(p) for p in perms))
        elif cmd in ("permitir", "negar") and rest:
            print(answer(rest[0], rest[1] if len(rest) > 1 else None, ALLOW if cmd == "permitir" else REJECT))
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except AppError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
