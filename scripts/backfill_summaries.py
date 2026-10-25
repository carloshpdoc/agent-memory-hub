#!/usr/bin/env python3
"""
agent-memory-hub — backfill the `summary` column for existing rows.

Reuses the exact extractive logic from the capture hook. Parses each stored
`content` back into its user turns and writes a summary. Idempotent: only touches
rows where summary is null. Run once after adding the summary column.

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "hooks"))
from capture_session import build_summary  # noqa: E402  (reuse identical logic)

sys.path.insert(0, HERE)
from store import get_store  # noqa: E402
BLOCK_SPLIT = re.compile(r"\n\n(?=\[(?:user|assistant)\]\n)")


def main():
    store = get_store()
    if not store.configured:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    rows = store.sessions_without_summary()
    done = 0
    for r in rows:
        blocks = BLOCK_SPLIT.split(r.get("content") or "")
        user_texts = [b[len("[user]\n"):] for b in blocks if b.startswith("[user]\n")]
        n_user = sum(1 for b in blocks if b.startswith("[user]\n"))
        n_assistant = sum(1 for b in blocks if b.startswith("[assistant]\n"))
        summary = build_summary(user_texts, n_user, n_assistant)
        if summary:
            store.update_session(r["id"], {"summary": summary})
            done += 1
    print(f"backfilled {done}/{len(rows)} summaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
