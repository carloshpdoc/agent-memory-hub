#!/usr/bin/env python3
"""
agent-memory-hub — embed pending sessions (Phase 2).

Calls the `embed` Edge Function in small batches until every row has an embedding.
Resilient to the Edge free-tier compute limit (HTTP 546): falls back to batch=1.
Meant to run periodically (e.g. EC2 cron) so new sessions become searchable.

Then chunks sessions (sql/09-session-chunks.sql): splits each session into
turn-aware chunks, embeds them, and stores one vector per chunk in session_chunks, so late
parts of long sessions are searchable too. Bounded by CHUNK_MAX_SECONDS (default 600); a
run that stops early resumes on the next one.

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY. Storage and the embed
calls go through the store (scripts/store/).
"""
import os
import sys
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import config, get_store  # noqa: E402

CHUNK_CHARS = 1500                       # under the embed function's 2000-char slice
MAX_CHUNKS = 48                          # per session; longer ones are sampled evenly
BOUNDARIES = ("\n[", "\n\n", "\n", " ")  # turn > paragraph > line > word


def chunk_spans(text, size=CHUNK_CHARS):
    """(start, end) offsets covering text in chunks of at most `size` chars, cut at the
    strongest boundary found in the chunk's second half. Whitespace-only spans dropped."""
    spans, pos, n = [], 0, len(text)
    while pos < n:
        end = min(pos + size, n)
        if end < n:
            for b in BOUNDARIES:
                cut = text.rfind(b, pos + size // 2, end)
                if cut != -1:
                    end = cut + (1 if b == "\n[" else len(b))   # next chunk opens at "[user]"
                    break
        if text[pos:end].strip():
            spans.append((pos, end))
        pos = end
    return spans


def sample_spans(spans, cap=MAX_CHUNKS):
    """[(chunk_idx, span)], keeping at most `cap` spans evenly spread (first and last kept).
    chunk_idx is the span's position in the full list, so it stays stable."""
    if cap < 2:
        raise ValueError("cap must be >= 2")
    if len(spans) <= cap:
        return list(enumerate(spans))
    idxs = sorted({round(i * (len(spans) - 1) / (cap - 1)) for i in range(cap)})
    return [(i, spans[i]) for i in idxs]


def chunk_session(store, row):
    """Troca os chunks da sessão por novos e registra o tamanho coberto (store.replace_chunks)."""
    content, sid = row.get("content") or "", row["id"]
    picked = sample_spans(chunk_spans(content))
    embs = store.embed_texts([content[a:b] for _, (a, b) in picked])
    store.replace_chunks(sid, [(i, a, b, e) for (i, (a, b)), e in zip(picked, embs)], row["content_len"])
    return len(picked)


def chunk_pending(store, max_seconds):
    deadline = time.monotonic() + max_seconds
    done, chunks, failed = 0, 0, set()
    while time.monotonic() < deadline:
        rows = [r for r in store.pending_chunk_sessions(len(failed) + 5) if r["id"] not in failed]
        if not rows:
            break
        for row in rows:
            if time.monotonic() >= deadline:
                break
            try:
                chunks += chunk_session(store, row)
                done += 1
            except Exception as e:
                failed.add(row["id"])
                print(f"falha ao chunkar {row['id']}: {e}", file=sys.stderr)
    return done, chunks, len(failed)


def main():
    store = get_store()
    if not store.configured or not config.get("EMBED_KEY"):
        print("ERRO: SUPABASE_URL/SUPABASE_SECRET_KEY/EMBED_KEY ausentes", file=sys.stderr)
        return 1

    total, fails = 0, 0
    for _ in range(5000):
        try:
            res = store.embed_pending_sessions(2)
        except urllib.error.HTTPError as e:
            if e.code == 546:               # compute limit -> one at a time
                try:
                    res = store.embed_pending_sessions(1)
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
    max_seconds = int(config.get("CHUNK_MAX_SECONDS") or 600)
    try:
        done, chunks, failed = chunk_pending(store, max_seconds)
    except urllib.error.HTTPError as e:
        print(f"chunking: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
        return 1
    print(f"embedded {total} session(s); chunked {done} session(s) into {chunks} chunk(s)"
          + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
