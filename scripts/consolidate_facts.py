#!/usr/bin/env python3
"""
agent-memory-hub — consolidate facts (Phase 5): dedup + supersession via LLM.

Finds near-duplicate fact pairs in the same scope (find_fact_dupes RPC) and asks
the LLM to judge each: duplicate / update / distinct. For duplicate or update, the
OLDER fact is superseded (valid_until=now, superseded_by=newer). NON-DESTRUCTIVE:
rows are flagged, not deleted, so recall just stops showing them.

The LLM is required because same-project facts are vectorially close but usually
DISTINCT; only a judge can tell a real duplicate from a related-but-different fact.

Usage:
  python3 scripts/consolidate_facts.py --dry-run   # preview decisions, no change
  python3 scripts/consolidate_facts.py             # apply supersession

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, FACTS_LLM (+ provider vars),
  MIN_SIM (default 0.85).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, HERE)
from extract_facts import load_env, http, PROVIDERS  # noqa: E402  (reuse providers)

REL_PROMPT = """Compare two facts about the same project. Remove redundancy WITHOUT losing information.
A (newer): {a}
B (older): {b}
Reply ONLY JSON: {{"relation": "duplicate" | "update" | "distinct"}}.
- "duplicate": A and B state the SAME thing and B has NO extra specific detail.
- "update": A and B describe the SAME specific item (same PR/file/feature/id) and A is a newer state that makes B obsolete.
- "distinct": different items, OR B has a specific detail (an API, guideline, file, id, constraint) that A omits. KEEP BOTH.
Rules: default to "distinct". Never merge facts about different items. If B has useful specifics A lacks, answer "distinct".
"""


def parse_relation(txt):
    t = (txt or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        d = json.loads(t.strip())
    except json.JSONDecodeError:
        return "distinct"
    if isinstance(d, dict):
        return str(d.get("relation", "distinct")).lower()
    return "distinct"


def main(argv):
    dry = "--dry-run" in argv
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    provider = (g("FACTS_LLM", "off") or "off").lower()
    if provider not in PROVIDERS:
        print("FACTS_LLM precisa ser ollama/gemini/openai para consolidar", file=sys.stderr)
        return 1
    caller = PROVIDERS[provider]
    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    min_sim = float(g("MIN_SIM", "0.85"))
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    pairs = json.loads(http(f"{url}/rest/v1/rpc/find_fact_dupes", H, {"min_sim": min_sim}, "POST"))
    print(f"{len(pairs)} pares candidatos (sim >= {min_sim}); julgando com LLM...")

    superseded, n = set(), 0
    for p in pairs:
        a, b = p["a_id"], p["b_id"]
        if a in superseded or b in superseded:
            continue
        try:
            rel = parse_relation(caller(REL_PROMPT.format(a=p["a_fact"], b=p["b_fact"]), g))
        except Exception as e:
            print(f"  llm falhou: {type(e).__name__}", file=sys.stderr)
            continue
        if rel in ("duplicate", "update"):
            print(f"  [{rel}] {p['similarity']:.2f} ({p['scope']})")
            print(f"      mantem: {p['a_fact'][:70]}")
            print(f"    superseda: {p['b_fact'][:70]}")
            if not dry:
                http(f"{url}/rest/v1/facts?id=eq.{b}", {**H, "Prefer": "return=minimal"},
                     {"valid_until": datetime.now(timezone.utc).isoformat(), "superseded_by": a}, "PATCH")
            superseded.add(b)
            n += 1
    print(f"\n{'(dry-run) ' if dry else ''}{n} fato(s) supersedido(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
