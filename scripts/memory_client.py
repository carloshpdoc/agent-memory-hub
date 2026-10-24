#!/usr/bin/env python3
"""
agent-memory-hub — consultas de alto nível (recall, recent, facts, session).

Lê pelo store (scripts/store/) e reexporta os nomes de acesso de baixo nível que os
módulos ainda importam daqui. Consumido pelo console (scripts/memory.py), pelo MCP server
(scripts/mcp_server.py) e pelo dashboard. Pure stdlib.

Config (env ou ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY (p/ busca semântica).
"""
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "hooks"))
sys.path.insert(0, HERE)
from recall_session import topic_key  # noqa: E402  (same topic rule as SessionStart recall)
from handoff import build_handoff  # noqa: E402
from store import config as _config, get_store  # noqa: E402  (onde a memória mora: store/)

# Compatibilidade: estes nomes são a API que ~12 módulos (e seus testes) importam daqui.
# O acesso de verdade mora em store/; os módulos migram para operações do store aos poucos.
ENV_PATH, ENV, load_env = _config.ENV_PATH, _config.ENV, _config.load_env
_S = get_store()
URL, KEY, EK, H = _S.url, _S.key, _S.embed_key, _S.headers
rest, rest_all, count, rpc, write, embed = _S.rest, _S.rest_all, _S.count, _S.rpc, _S.write, _S.embed


def _line(s, n=300):
    return " ".join((s or "").split())[:n]


# ---- consultas de alto nível (usadas pelo MCP server) ----------------------
OVERFETCH = 3   # candidatos por vaga: sobra pra colapsar prompts repetidos sem perder limit


def collapse_repeats(rows, limit):
    """Mantém a 1a (melhor ranqueada) sessão de cada tema, até `limit`. Um job agendado com
    o mesmo prompt gera dezenas de sessões quase iguais que, sem isso, lotavam o resultado."""
    seen, out = set(), []
    for r in rows:
        tk = topic_key(r)
        if tk and tk in seen:
            continue
        seen.add(tk)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def ranked_sessions(query, project=None, limit=8, embedding=None):
    """Linhas do hybrid_search (com summary), repetições colapsadas, até `limit`."""
    rows = rpc("hybrid_search_head", {"query_text": query, "query_embedding": embedding or embed(query),
                                      "match_count": limit * OVERFETCH, "filter_project": project})
    ids = ",".join(f'"{r["session_id"]}"' for r in rows if r.get("session_id"))
    summ = ({s["session_id"]: s.get("summary") for s in
             rest(f"sessions?select=session_id,summary&session_id=in.({urllib.parse.quote(ids)})")}
            if ids else {})
    return collapse_repeats([{**r, "summary": summ.get(r.get("session_id"))} for r in rows], limit)


def recall(query, project=None, limit=8):
    """Busca híbrida (semântica+keyword se houver EMBED_KEY; senão full-text)."""
    if not query:
        return []
    if EK:
        rows = ranked_sessions(query, project, limit)
        return [{"session_id": r.get("session_id"), "project": r.get("project"),
                 "score": round(r["score"], 3) if r.get("score") is not None else None,
                 "text": _line(r.get("content"))} for r in rows]
    q = urllib.parse.quote(query)
    flt = f"&project=eq.{project}" if project else ""
    rows = rest(f"sessions?select=session_id,project,summary,content"
                f"&content_tsv=fts(simple).{q}{flt}&limit={limit * OVERFETCH}")
    return [{"session_id": r.get("session_id"), "project": r.get("project"),
             "text": _line(r.get("summary") or r.get("content"))} for r in collapse_repeats(rows, limit)]


def recent(limit=10):
    rows = rest(f"sessions?select=session_id,started_at,project,tool,summary"
                f"&order=started_at.desc&limit={limit}")
    return [{"session_id": r.get("session_id"), "project": r.get("project"),
             "tool": r.get("tool"), "started_at": r.get("started_at"),
             "text": _line(r.get("summary"), 200)} for r in rows]


def facts(project=None):
    flt = (f"&or=(scope.eq.{urllib.parse.quote(project)},scope.is.null)" if project else "")
    rows = rest(f"facts?select=fact,kind,scope&valid_until=is.null{flt}"
                f"&order=scope.nullslast,kind&limit=60")
    return [{"kind": r.get("kind"), "scope": r.get("scope") or "global",
             "fact": _line(r.get("fact"), 200)} for r in rows]


def handoff(project=None, exclude_tool=None):
    """Passagem de contexto: onde a última sessão (do projeto, se dado) parou. exclude_tool
    pula a ferramenta de quem pergunta, pra trazer o trabalho feito EM OUTRO lugar."""
    flt = f"&project=eq.{urllib.parse.quote(project)}" if project else ""
    flt += f"&tool=neq.{urllib.parse.quote(exclude_tool)}" if exclude_tool else ""
    rows = rest(f"sessions?select=session_id,tool,machine,project,started_at,ended_at,summary,"
                f"content,metadata{flt}&order=ended_at.desc.nullslast&limit=1")  # última atividade
    return build_handoff(rows[0]) if rows else None


def session(session_id):
    rows = rest(f"sessions?select=session_id,project,tool,machine,started_at,content"
                f"&session_id=like.{urllib.parse.quote(session_id)}*&limit=1")
    return rows[0] if rows else None
