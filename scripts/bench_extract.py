#!/usr/bin/env python3
"""
A/B the fact-extraction model on a fixed sample of real sessions (READ-ONLY).

Runs the exact production path of extract_facts.py — same PROMPT, same provider call,
same parse_facts — against several llama.cpp models, and reports per model:
schema-valid outputs, facts, procedures, facts the production dedup would drop, and time.
Nothing is written to the store: no insert_fact, no mark_facts_extracted.

  python3 scripts/bench_extract.py --models qwen3.6-27b,qwen3.6-35b-a3b --n 12 \
      --out bench/extract-2026-11.jsonl

The sample is deterministic (spread over the most recent already-extracted sessions with
enough content), so the same --n/--pool picks the same sessions for every model.
"""
import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract_facts as ef  # noqa: E402
from store import config, get_store  # noqa: E402

MIN_CONTENT = 2000
DUP_SIM = 0.90  # same threshold as DEDUP_SIM in extract_facts


def sample_sessions(store, n, pool):
    """n sessions spread evenly over the `pool` most recent already-extracted ones.

    Two light queries instead of one heavy one: pulling `content` for the whole pool at once
    made Supabase answer 500. First the ids (ordered), then content only for spread candidates."""
    ids = [r["id"] for r in store.rest("sessions?facts_extracted_at=not.is.null&order=started_at.desc"
                                       f"&limit={pool}&select=id", timeout=60)]
    picked = []
    for per in (2, 4, 8):                      # oversample; widen if too many are short
        k = min(len(ids), n * per)
        cand = [ids[int(i * len(ids) / k)] for i in range(k)] if k else []
        rows = []
        for i in range(0, len(cand), 10):      # small batches keep each response light
            batch = ",".join(str(c) for c in cand[i:i + 10])
            rows += store.rest(f"sessions?id=in.({batch})&select=id,session_id,project,content", timeout=60)
        by_id = {r["id"]: r for r in rows}
        long_enough = [by_id[c] for c in cand if c in by_id and len(by_id[c].get("content") or "") >= MIN_CONTENT]
        if len(long_enough) >= n:
            step = len(long_enough) / n
            picked = [long_enough[int(i * step)] for i in range(n)]
            break
    if len(picked) < n:
        raise SystemExit(f"could not find {n} sessions with >= {MIN_CONTENT} chars in the last {pool}; lower --n")
    return picked


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def near_dupes(store, facts):
    """How many facts are >= DUP_SIM to an earlier fact of the same run, i.e. what the
    production dedup (same embedder, same threshold) would drop. Short, similarly-worded
    facts can land here while being distinct, so read it next to the JSONL, not alone."""
    vecs, dupes = [], 0
    for f in facts:
        v = store.embed(f)
        if any(cosine(v, u) >= DUP_SIM for u in vecs):
            dupes += 1
        vecs.append(v)
    return dupes


def run_model(model, sessions, known, g_base, out):
    g = lambda k, d=None: model if k == "LLAMACPP_MODEL" else g_base(k, d)  # noqa: E731
    stats = {"model": model, "sessions": len(sessions), "ok": 0, "errors": 0, "empty": 0,
             "facts": 0, "procedures": 0, "seconds": 0.0, "all_facts": []}
    for s in sessions:
        prompt = ef.PROMPT.format(max_facts=ef.MAX_FACTS, project=s.get("project") or "none",
                                  known=", ".join(sorted(known)) or "none",
                                  content=(s.get("content") or "")[:ef.MAX_CONTENT])
        t0 = time.time()
        try:
            raw = ef.call_llamacpp(prompt, g)
            err = None
        except Exception as e:  # counted, never swallowed
            raw, err = "", f"{type(e).__name__}: {e}"
        dt = time.time() - t0
        stats["seconds"] += dt
        facts = [] if err else ef.parse_facts(raw)[:ef.MAX_FACTS]
        facts = [f for f in facts if isinstance(f, dict) and len((f.get("fact") or "").strip()) >= 8]
        if err:
            stats["errors"] += 1
        else:
            stats["ok"] += 1
            stats["empty"] += int(not facts)
        stats["facts"] += len(facts)
        stats["procedures"] += sum(1 for f in facts if f.get("kind") == "procedure")
        stats["all_facts"] += [f["fact"].strip() for f in facts]
        out.write(json.dumps({"model": model, "session_id": s["session_id"], "project": s.get("project"),
                              "seconds": round(dt, 1), "error": err, "facts": facts},
                             ensure_ascii=False) + "\n")
        out.flush()
        mark = "ERR" if err else f"{len(facts)} facts"
        print(f"  [{model}] {s['session_id'][:8]} {mark} in {dt:.0f}s", flush=True)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", required=True, help="comma-separated llama.cpp preset names")
    ap.add_argument("--n", type=int, default=12, help="sessions in the sample")
    ap.add_argument("--pool", type=int, default=300, help="recent extracted sessions to sample from")
    ap.add_argument("--out", required=True, help="JSONL with every model output, for manual review")
    ap.add_argument("--sessions-file", help="pin the sample: one session_id per line (ignores --n/--pool)")
    args = ap.parse_args(argv)

    g_base = config.get  # process env, then .env (same lookup as the other scripts)
    store = get_store()
    if args.sessions_file:
        # pinned sample: the same sessions every run, even after new sessions arrive in the store
        wanted = [l.strip() for l in open(args.sessions_file) if l.strip()]
        rows = []
        for i in range(0, len(wanted), 10):
            q = ",".join(f'"{x}"' for x in wanted[i:i + 10])
            rows += store.rest(f"sessions?session_id=in.({q})&select=id,session_id,project,content", timeout=60)
        by = {r["session_id"]: r for r in rows}
        missing = [w for w in wanted if w not in by]
        if missing:
            raise SystemExit(f"pinned sessions not found in the store: {missing}")
        sessions = [by[w] for w in wanted]
    else:
        sessions = sample_sessions(store, args.n, args.pool)
    known = store.project_keys()
    print(f"sample: {len(sessions)} sessions -> {[s['session_id'][:8] for s in sessions]}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    results = []
    with open(args.out, "w") as out:
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            print(f"== {model}", flush=True)
            results.append(run_model(model, sessions, known, g_base, out))

    print("\nmodel | ok/err | empty | facts | procedures | dedup>=0.90 | s/session")
    for r in results:
        dupes = near_dupes(store, r["all_facts"])
        per = r["seconds"] / max(r["sessions"], 1)
        print(f"{r['model']} | {r['ok']}/{r['errors']} | {r['empty']} | {r['facts']} | "
              f"{r['procedures']} | {dupes} | {per:.0f}")
        # coverage gate: every sampled session is accounted for exactly once
        assert r["ok"] + r["errors"] == r["sessions"], r
    print(f"\nall outputs: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
