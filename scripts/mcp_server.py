#!/usr/bin/env python3
"""
agent-memory-hub — MCP server (stdio + JSON-RPC 2.0, pure stdlib).

Expõe a memória compartilhada como tools que qualquer agente MCP (Claude Code,
Cursor, Codex) chama on-demand, COM o contexto da tarefa em mãos — diferente do
recall passivo do SessionStart. Sem dependências: implementa o protocolo MCP
mínimo (initialize / tools/list / tools/call) sobre newline-delimited JSON-RPC.

Registrar (Claude Code):
  claude mcp add --scope user agent-memory-hub -- python3 <REPO>/scripts/mcp_server.py

Config (env ou ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_client as mc  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agent-memory-hub", "version": "1.0.0"}

TOOLS = [
    {
        "name": "recall_relevant",
        "description": "Busca semântica/híbrida na memória de sessões passadas, relevante "
                       "à tarefa atual. Use quando precisar de contexto de trabalho anterior "
                       "(decisões, bugs já resolvidos, como algo foi feito).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "O que você está tentando fazer/lembrar."},
                "project": {"type": "string", "description": "Filtrar por projeto (opcional)."},
                "limit": {"type": "integer", "description": "Máx. de sessões (default 8)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recent_sessions",
        "description": "Lista as sessões mais recentes (cross-projeto). Use para 'o que foi feito "
                       "ultimamente' ou para achar uma sessão por recência.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Quantas (default 10)."}},
        },
    },
    {
        "name": "get_facts",
        "description": "Fatos/preferências/decisões duráveis do desenvolvedor (globais ou do projeto). "
                       "Use para respeitar convenções e decisões já estabelecidas.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Escopo do projeto (opcional)."}},
        },
    },
    {
        "name": "get_session",
        "description": "Transcript completo de uma sessão por prefixo de session_id (ex: o id de 8 "
                       "chars mostrado pelos outros resultados).",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "session_id ou prefixo."}},
            "required": ["session_id"],
        },
    },
]


def _call(name, args):
    if name == "recall_relevant":
        rows = mc.recall(args.get("query", ""), args.get("project"), int(args.get("limit", 8)))
        if not rows:
            return "Nenhuma sessão relevante encontrada."
        out = []
        for r in rows:
            sc = f" ({r['score']})" if r.get("score") is not None else ""
            out.append(f"- [{r.get('project')} · {r.get('session_id', '')[:8]}]{sc} {r.get('text')}")
        return "\n".join(out)

    if name == "recent_sessions":
        rows = mc.recent(int(args.get("limit", 10)))
        return "\n".join(
            f"- [{r.get('project')} · {r.get('tool')} · {r.get('session_id', '')[:8]}] "
            f"{(r.get('started_at') or '')[:16]} {r.get('text')}" for r in rows
        ) or "Nenhuma sessão."

    if name == "get_facts":
        rows = mc.facts(args.get("project"))
        return "\n".join(f"- ({r['kind']} · {r['scope']}) {r['fact']}" for r in rows) or "Nenhum fato."

    if name == "get_session":
        s = mc.session(args.get("session_id", ""))
        if not s:
            return "Sessão não encontrada."
        head = f"{s.get('project')} · {s.get('tool')} · {s.get('machine')} · {(s.get('started_at') or '')[:16]}"
        return f"{head}\n{s.get('session_id')}\n\n{(s.get('content') or '')[:12000]}"

    raise ValueError(f"tool desconhecida: {name}")


def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _reply(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method")
        if method is None or rid is None:
            continue  # resposta ou notificação (ex: notifications/initialized) — sem reply
        try:
            if method == "initialize":
                params = req.get("params") or {}
                _reply(rid, {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                             "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
            elif method == "tools/list":
                _reply(rid, {"tools": TOOLS})
            elif method == "tools/call":
                params = req.get("params") or {}
                text = _call(params.get("name"), params.get("arguments") or {})
                _reply(rid, {"content": [{"type": "text", "text": text}]})
            elif method == "ping":
                _reply(rid, {})
            else:
                _error(rid, -32601, f"method not found: {method}")
        except Exception as e:  # nunca derruba o server; reporta como erro JSON-RPC
            _error(rid, -32603, f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
