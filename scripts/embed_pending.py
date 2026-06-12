#!/usr/bin/env python3
"""
agent-memory-hub — embed pending sessions (Phase 2).

Calls the `embed` Edge Function in small batches until every row has an embedding.
Resilient to the Edge free-tier compute limit (HTTP 546): falls back to batch=1.
Meant to run periodically (e.g. EC2 cron) so new sessions become searchable.

Config (env or ../.env): SUPABASE_URL, EMBED_KEY (and SUPABASE_SECRET_KEY unused here).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

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


def main():
    env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    ek = os.environ.get("EMBED_KEY") or env.get("EMBED_KEY")
    if not url or not ek:
        print("ERRO: SUPABASE_URL/EMBED_KEY ausentes", file=sys.stderr)
        return 1
    fn = f"{url}/functions/v1/embed"
    headers = {"x-embed-key": ek, "Content-Type": "application/json"}

    def call(limit):
        req = urllib.request.Request(fn, data=json.dumps({"limit": limit}).encode(),
                                     method="POST", headers=headers)
        return json.loads(urllib.request.urlopen(req, timeout=60).read())

    total, fails = 0, 0
    for _ in range(5000):
        try:
            res = call(2)
        except urllib.error.HTTPError as e:
            if e.code == 546:               # compute limit -> one at a time
                try:
                    res = call(1)
                except Exception:
                    fails += 1
                    time.sleep(1.0)
                    if fails > 25:
                        break
                    continue
            elif e.code in (429, 500, 502, 503, 504):   # transiente -> backoff e retry
                fails += 1
                if fails > 25:
                    print(f"desistindo apos varios {e.code}", file=sys.stderr)
                    break
                time.sleep(min(2 ** min(fails, 5), 30))
                continue
            else:
                print(f"HTTP {e.code}", file=sys.stderr)
                return 1
        except Exception:
            fails += 1
            if fails > 25:
                break
            time.sleep(2)
            continue
        fails = 0
        total += res.get("embedded", 0)
        if res.get("scanned", 0) == 0:
            break
        time.sleep(0.3)
    print(f"embedded {total} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
