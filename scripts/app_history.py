#!/usr/bin/env python3
"""
agent-memory-hub — histórico do hub para o Agent Console (aba Histórico).

O app não fala com o banco: chama este script e lê JSON da saída padrão. Assim a tela vê as
sessões das duas máquinas e das três ferramentas pelo mesmo store que o resto do hub usa
(Supabase ou SQLite), sem credencial nenhuma no processo do Electron.

  python3 scripts/app_history.py list [--limit N]
  python3 scripts/app_history.py show <session_id>
  python3 scripts/app_history.py pack <session_id> --to <claude|codex|cursor> --cwd <pasta>

Saída: um objeto JSON. Erro: {"error": "..."} e código 1 (nunca some calado).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))

LIST_DEFAULT = 200
LIST_MAX = 1000
TURN_MAX = 20000          # caracteres por turno mostrado (o resto fica no hub)
SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")   # também barra curingas do filtro por prefixo


class UsageError(Exception):
    pass


def _sid(value):
    if not value or not SID_RE.match(value):
        raise UsageError(f"session_id inválido: {value!r}")
    return value


def _flag(args, name):
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args):
        raise UsageError(f"{name} sem valor")
    return args[i + 1]


def _store():
    from memory_client import _S   # import tardio: erro de uso não abre conexão
    return _S


def _session(store, sid):
    row = store.find_session(sid)
    if not row or row.get("session_id") != sid:    # find_session casa por prefixo: exige o id inteiro
        raise LookupError(f"sessão {sid} não está no hub")
    return row


def cmd_list(args):
    raw = _flag(args, "--limit")
    limit = int(raw) if raw and raw.isdigit() else LIST_DEFAULT
    rows = _store().recent_sessions(min(max(limit, 1), LIST_MAX))
    keep = ("session_id", "started_at", "machine", "tool", "project", "summary")
    return {"sessions": [{k: r.get(k) for k in keep} for r in rows]}


def cmd_show(args):
    from continuar import turns
    sid = _sid(args[0] if args else None)
    row = _session(_store(), sid)
    convo = turns(row.get("content"))
    out = []
    for role, text in convo:
        cut = len(text) > TURN_MAX
        out.append({"role": "user" if role == "Usuário" else "agent",
                    "text": text[:TURN_MAX] + (f"… [+{len(text) - TURN_MAX} caracteres]" if cut else "")})
    meta = {k: row.get(k) for k in ("session_id", "tool", "machine", "project", "started_at")}
    return {**meta, "turns": out}


def cmd_pack(args):
    from continuar import AGENTS, build_pack
    from handoff import git_snapshot
    sid = _sid(args[0] if args else None)
    to, cwd = _flag(args, "--to"), _flag(args, "--cwd")
    if to not in AGENTS:
        raise UsageError(f"--to deve ser um de {', '.join(AGENTS)}")
    if not cwd or not os.path.isdir(cwd):
        raise UsageError(f"--cwd não é uma pasta: {cwd!r}")
    row = _session(_store(), sid)
    row = {**row, "metadata": {}, "ended_at": None}   # find_session não traz os dois; o pacote vive sem
    return {"pack": build_pack(row, cwd, AGENTS[to][1], git_snapshot(cwd))}


COMMANDS = {"list": cmd_list, "show": cmd_show, "pack": cmd_pack}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(json.dumps({"error": f"uso: app_history.py {'|'.join(COMMANDS)} ..."}))
        return 2
    try:
        out = COMMANDS[argv[0]](argv[1:])
    except (UsageError, LookupError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    except Exception as e:   # banco fora do ar etc.: a tela mostra o motivo
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
