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

Config (env or ../.env): SUPABASE_URL, EMBED_KEY, SUPABASE_SECRET_KEY (chunking only).
"""
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")

CHUNK_CHARS = 1500                       # under the embed function's 2000-char slice
MAX_CHUNKS = 48                          # per session; longer ones are sampled evenly
EMBED_BATCH = 2                          # texts per Edge call: free-tier CPU 546s at 4 x 1500 chars
BOUNDARIES = ("\n[", "\n\n", "\n", " ")  # turn > paragraph > line > word
TRANSIENT = (429, 500, 502, 503, 504, 546)


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


def _post(url, body, headers, method="POST", timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                 headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read()


def embed_texts(fn, headers, texts, batch=EMBED_BATCH, max_fails=5):
    """Embeds texts via the function's `texts` mode. Halves the batch on the compute limit
    (546), backs off on transient errors, raises after max_fails in a row."""
    out, i, fails = [], 0, 0
    while i < len(texts):
        part = texts[i:i + batch]
        try:
            res = json.loads(_post(fn, {"texts": part}, headers))
        except urllib.error.HTTPError as e:
            if e.code == 546 and batch > 1:
                batch = max(1, batch // 2)
                continue
            if e.code not in TRANSIENT or fails >= max_fails:
                raise
            fails += 1
            time.sleep(min(2 ** fails, 30))
            continue
        except (urllib.error.URLError, socket.timeout, TimeoutError):
            if fails >= max_fails:
                raise
            fails += 1
            time.sleep(min(2 ** fails, 30))
            continue
        embs = res.get("embeddings")
        if not isinstance(embs, list) or len(embs) != len(part):
            raise RuntimeError(f"embed function returned {res!r:.200}")
        out += embs
        i += len(part)
        fails = 0
    return out


def chunk_session(url, fn, embed_headers, rest_headers, row):
    """Replaces a session's chunks with fresh ones and records the length it covered.
    Delete-then-insert is not atomic, but chunked_len is set last: any failure in between
    leaves the session pending, and the next run redoes it."""
    content, sid = row.get("content") or "", row["id"]
    picked = sample_spans(chunk_spans(content))
    embs = embed_texts(fn, embed_headers, [content[a:b] for _, (a, b) in picked])
    _post(f"{url}/rest/v1/session_chunks?session_fk=eq.{sid}", None, rest_headers,
          method="DELETE")
    if picked:
        _post(f"{url}/rest/v1/session_chunks", [
            {"session_fk": sid, "chunk_idx": i, "char_start": a, "char_end": b,
             "embedding": json.dumps(e)}
            for (i, (a, b)), e in zip(picked, embs)], rest_headers)
    _post(f"{url}/rest/v1/sessions?id=eq.{sid}", {"chunked_len": row["content_len"]},
          rest_headers, method="PATCH")
    return len(picked)


def chunk_pending(url, ek, key, max_seconds):
    fn = f"{url}/functions/v1/embed"
    embed_headers = {"x-embed-key": ek, "Content-Type": "application/json"}
    rest_headers = {"apikey": key, "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json", "Prefer": "return=minimal"}
    deadline = time.monotonic() + max_seconds
    done, chunks, failed = 0, 0, set()
    while time.monotonic() < deadline:
        rows = json.loads(_post(f"{url}/rest/v1/rpc/sessions_pending_chunks",
                                {"max_rows": len(failed) + 5},
                                {**rest_headers, "Prefer": "return=representation"}))
        rows = [r for r in rows if r["id"] not in failed]
        if not rows:
            break
        for row in rows:
            if time.monotonic() >= deadline:
                break
            try:
                chunks += chunk_session(url, fn, embed_headers, rest_headers, row)
                done += 1
            except Exception as e:
                failed.add(row["id"])
                print(f"falha ao chunkar {row['id']}: {e}", file=sys.stderr)
    return done, chunks, len(failed)


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
    key = os.environ.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SECRET_KEY")
    if not key:
        print("SUPABASE_SECRET_KEY ausente; pulando chunking", file=sys.stderr)
        print(f"embedded {total} session(s)")
        return 0
    max_seconds = int(os.environ.get("CHUNK_MAX_SECONDS") or env.get("CHUNK_MAX_SECONDS") or 600)
    try:
        done, chunks, failed = chunk_pending(url, ek, key, max_seconds)
    except urllib.error.HTTPError as e:
        print(f"chunking: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
        return 1
    print(f"embedded {total} session(s); chunked {done} session(s) into {chunks} chunk(s)"
          + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
