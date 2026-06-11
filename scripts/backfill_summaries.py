#!/usr/bin/env python3
"""
agent-memory-hub — backfill the `summary` column for existing rows.

Reuses the exact extractive logic from the capture hook. Parses each stored
`content` back into its user turns and writes a summary. Idempotent: only touches
rows where summary is null. Run once after adding the summary column.

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "hooks"))
from capture_session import build_summary  # noqa: E402  (reuse identical logic)

ENV_PATH = os.path.join(REPO, ".env")
BLOCK_SPLIT = re.compile(r"\n\n(?=\[(?:user|assistant)\]\n)")


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


def req(url, key, method="GET", body=None):
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=minimal"
        data = json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    resp = urllib.request.urlopen(r, timeout=30)
    return resp.read()


def main():
    env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    rows = json.loads(req(f"{url}/rest/v1/sessions?summary=is.null&select=id,content", key))
    done = 0
    for r in rows:
        blocks = BLOCK_SPLIT.split(r.get("content") or "")
        user_texts = [b[len("[user]\n"):] for b in blocks if b.startswith("[user]\n")]
        n_user = sum(1 for b in blocks if b.startswith("[user]\n"))
        n_assistant = sum(1 for b in blocks if b.startswith("[assistant]\n"))
        summary = build_summary(user_texts, n_user, n_assistant)
        if summary:
            req(f"{url}/rest/v1/sessions?id=eq.{r['id']}", key, "PATCH", {"summary": summary})
            done += 1
    print(f"backfilled {done}/{len(rows)} summaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
