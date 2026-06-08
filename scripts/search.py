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


def main(argv):
    project = None
    if len(argv) >= 2 and argv[0] == "--project":
        project, argv = argv[1], argv[2:]
    query = " ".join(argv).strip()
    if not query:
        print("usage: search.py [--project P] \"<query>\"", file=sys.stderr)
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
    rows = post(f"{url}/rest/v1/rpc/match_sessions",
                {"query_embedding": emb, "match_count": 5, "filter_project": project},
                {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for r in rows:
        snippet = " ".join((r.get("content") or "").split())[:80]
        print(f"{r['similarity']:.3f}  [{r.get('project')}/{r.get('machine')}]  {snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
