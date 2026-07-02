#!/usr/bin/env python3
"""
agent-memory-hub — núcleo de acesso + consultas (fonte única).

Centraliza o acesso ao Supabase (REST / RPC / embed) e as consultas de leitura de
alto nível (recall, recent, facts, session). Consumido pelo console
(scripts/memory.py) e pelo MCP server (scripts/mcp_server.py). Pure stdlib.

Config (env ou ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY (p/ busca semântica).
"""
import json
import os
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _env_path():
    """Resolve the .env: explicit override → XDG config → repo root (clone default).

    Lets an installed `mem` find its config without living inside the repo, while a
    plain clone keeps working with no setup."""
    override = os.environ.get("AGENT_MEMORY_HUB_ENV")
    if override:
        return override
    xdg = os.path.join(os.path.expanduser("~"), ".config", "agent-memory-hub", ".env")
    if os.path.exists(xdg):
        return xdg
    return os.path.join(REPO, ".env")


ENV_PATH = _env_path()


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


ENV = load_env(ENV_PATH)
URL = os.environ.get("SUPABASE_URL") or ENV.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SECRET_KEY") or ENV.get("SUPABASE_SECRET_KEY")
EK = os.environ.get("EMBED_KEY") or ENV.get("EMBED_KEY")
H = {"apikey": KEY or "", "Authorization": f"Bearer {KEY or ''}", "Content-Type": "application/json"}


# ---- acesso de baixo nível -------------------------------------------------
def rest(path):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", headers=H)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def rpc(name, body):
    req = urllib.request.Request(f"{URL}/rest/v1/rpc/{name}",
                                 data=json.dumps(body).encode(), method="POST", headers=H)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def write(path, body, method="PATCH"):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", data=json.dumps(body).encode(),
                                 method=method, headers={**H, "Prefer": "return=minimal"})
    urllib.request.urlopen(req, timeout=20).read()


def embed(text):
    req = urllib.request.Request(f"{URL}/functions/v1/embed",
                                 data=json.dumps({"text": text}).encode(), method="POST",
                                 headers={"x-embed-key": EK or "", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["embedding"]


def _line(s, n=300):
    return " ".join((s or "").split())[:n]


# ---- consultas de alto nível (usadas pelo MCP server) ----------------------
def recall(query, project=None, limit=8):
    """Busca híbrida (semântica+keyword se houver EMBED_KEY; senão full-text)."""
    if not query:
        return []
    if EK:
        rows = rpc("hybrid_search", {"query_text": query, "query_embedding": embed(query),
                                     "match_count": limit, "filter_project": project})
        return [{"session_id": r.get("session_id"), "project": r.get("project"),
                 "score": round(r["score"], 3) if r.get("score") is not None else None,
                 "text": _line(r.get("content"))} for r in rows]
    q = urllib.parse.quote(query)
    flt = f"&project=eq.{project}" if project else ""
    rows = rest(f"sessions?select=session_id,project,summary,content"
                f"&content_tsv=fts(simple).{q}{flt}&limit={limit}")
    return [{"session_id": r.get("session_id"), "project": r.get("project"),
             "text": _line(r.get("summary") or r.get("content"))} for r in rows]


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


def session(session_id):
    rows = rest(f"sessions?select=session_id,project,tool,machine,started_at,content"
                f"&session_id=like.{urllib.parse.quote(session_id)}*&limit=1")
    return rows[0] if rows else None
