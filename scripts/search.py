#!/usr/bin/env python3
"""
agent-memory-hub — semantic search over sessions (Phase 2).

Embeds the query via the `embed` Edge Function (gte-small) and ranks sessions by
cosine similarity through the `match_sessions` RPC.

Usage:
  python3 scripts/search.py "how did we set up the backup"
  python3 scripts/search.py --project agent-memory-hub "pgvector decision"

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY.
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")


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


def post(u, body, headers):
    req = urllib.request.Request(u, data=json.dumps(body).encode(), method="POST", headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


RERANK_PROMPT = """Rank the snippets by relevance to the QUERY (most relevant first).
QUERY: {query}
SNIPPETS:
{snippets}
Reply ONLY JSON: {{"order": [snippet indices from most to least relevant]}}.
"""


def llm_rerank(query, rows, env):
    """Second-pass listwise rerank via the configured LLM (FACTS_LLM). Optional."""
    if not rows:
        return rows
    sys.path.insert(0, HERE)
    try:
        from extract_facts import PROVIDERS
    except Exception:
        return rows

    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    provider = (g("FACTS_LLM", "off") or "off").lower()
    if provider not in PROVIDERS:
        print("(rerank: FACTS_LLM=off; mantendo ordem hibrida)", file=sys.stderr)
        return rows
    snippets = "\n".join(
        f"[{i}] {' '.join((r.get('content') or '').split())[:200]}" for i, r in enumerate(rows))
    try:
        raw = PROVIDERS[provider](RERANK_PROMPT.format(query=query, snippets=snippets), g)
        txt = raw.strip().strip("`")
        if txt[:4].lower() == "json":
            txt = txt[4:]
        order = json.loads(txt.strip()).get("order", [])
    except Exception as e:
        print(f"(rerank falhou: {type(e).__name__}; ordem hibrida)", file=sys.stderr)
        return rows
    seen, ranked = set(), []
    for i in order:
        if isinstance(i, int) and 0 <= i < len(rows) and i not in seen:
            ranked.append(rows[i])
            seen.add(i)
    ranked += [r for j, r in enumerate(rows) if j not in seen]
    return ranked


def main(argv):
    project, rerank, words = None, False, []
    i = 0
    while i < len(argv):
        if argv[i] == "--project" and i + 1 < len(argv):
            project = argv[i + 1]
            i += 2
        elif argv[i] == "--rerank":
            rerank = True
            i += 1
        else:
            words.append(argv[i])
            i += 1
    query = " ".join(words).strip()
    if not query:
        print("usage: search.py [--project P] [--rerank] \"<query>\"", file=sys.stderr)
        return 2

    env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SECRET_KEY")
    ek = os.environ.get("EMBED_KEY") or env.get("EMBED_KEY")
    if not (url and key and ek):
        print("ERRO: SUPABASE_URL/SECRET_KEY/EMBED_KEY ausentes", file=sys.stderr)
        return 1

    emb = post(f"{url}/functions/v1/embed", {"text": query},
               {"x-embed-key": ek, "Content-Type": "application/json"})["embedding"]
    # hybrid search: full-text + semantic, fused with Reciprocal Rank Fusion
    rows = post(f"{url}/rest/v1/rpc/hybrid_search",
                {"query_text": query, "query_embedding": emb,
                 "match_count": 15 if rerank else 5, "filter_project": project},
                {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    if rerank:
        rows = llm_rerank(query, rows, env)
    rows = rows[:5]
    for r in rows:
        snippet = " ".join((r.get("content") or "").split())[:80]
        src = []
        if r.get("fts_rank"):
            src.append(f"kw#{r['fts_rank']}")
        if r.get("vec_rank"):
            src.append(f"sem#{r['vec_rank']}")
        print(f"{r['score']:.4f} [{'+'.join(src)}]  [{r.get('project')}/{r.get('machine')}]  {snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
