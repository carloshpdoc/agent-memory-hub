#!/usr/bin/env python3
"""
agent-memory-hub — recall eval harness.

The tool's own thesis, applied to itself: don't *trust* that recall surfaces the right
past context — *measure* it. Runs the real recall path (`memory_client.recall`, hybrid if
EMBED_KEY is set) and scores it with hit@k and MRR.

Two modes:

  --auto N   (default) Retrieval regression check. Samples N recent sessions, turns each
             session's own summary into a query, and checks whether that same session comes
             back near the top. It won't tell you recall is "smart", but it *will* scream when
             recall is broken (embeddings down, FTS misconfigured, RPC changed) — the silent
             failure this project exists to catch.

  --gold F   Curated check. Reads a JSON file of cases and scores them:
               [{"query": "how did we set up backups",
                 "expect": {"project": "agent-memory-hub", "contains": "pg_dump"}}]
             A case hits if a returned session matches `project` and/or has `contains` in its
             text (either key optional). See tests/eval/recall_gold.example.json.

Usage:
  python3 scripts/eval_recall.py --auto 30
  python3 scripts/eval_recall.py --auto 30 --project mysite --k 5
  python3 scripts/eval_recall.py --gold tests/eval/recall_gold.example.json

Config (env or .env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY (for hybrid recall).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from memory_client import recall, rest, EK  # noqa: E402

DEFAULT_N = 25
DEFAULT_KS = (1, 3, 5)

_COUNT_RE = re.compile(r"\s*\(\d+q/\d+r\)\s*$")


def query_from_summary(summary):
    """Turn a stored summary into a search query: drop the (Nq/Nr) counter and the
    '[...] <arc>' tail, keep the leading theme. Returns '' if nothing usable."""
    if not summary:
        return ""
    s = _COUNT_RE.sub("", summary).strip()
    s = s.split(" [...] ")[0].strip()
    return " ".join(s.split())[:120]


def rank_of(target_id, results):
    """1-based rank of target_id in results (list of dicts with session_id), or None."""
    for i, r in enumerate(results):
        if r.get("session_id") == target_id:
            return i + 1
    return None


def metrics(ranks, ks=DEFAULT_KS):
    """From a list of ranks (int or None), compute hit@k for each k and MRR."""
    n = len(ranks) or 1
    out = {f"hit@{k}": sum(1 for r in ranks if r is not None and r <= k) / n for k in ks}
    out["mrr"] = sum((1.0 / r) for r in ranks if r) / n
    return out


def sample_sessions(n, project=None):
    flt = f"&project=eq.{project}" if project else ""
    rows = rest(f"sessions?select=session_id,project,summary"
                f"&summary=not.is.null&order=started_at.desc&limit={n}{flt}")
    return [r for r in rows if query_from_summary(r.get("summary"))]


def run_auto(n, project, k, verbose):
    rows = sample_sessions(n, project)
    if not rows:
        print("nenhuma sessão com summary para avaliar.", file=sys.stderr)
        return 1
    ranks = []
    for r in rows:
        q = query_from_summary(r["summary"])
        res = recall(q, project=project, limit=k)
        rank = rank_of(r["session_id"], res)
        ranks.append(rank)
        if verbose:
            tag = f"#{rank}" if rank else "miss"
            print(f"  {tag:>5}  {r['session_id'][:8]}…  {q[:64]}")
    report("auto", len(ranks), metrics(ranks, ks=tuple(sorted({1, 3, k}))), project, k)
    return 0


def run_gold(path, project, k, verbose):
    with open(path) as f:
        cases = json.load(f)
    ranks = []
    for case in cases:
        q = case.get("query", "")
        exp = case.get("expect") or {}
        res = recall(q, project=exp.get("project") or project, limit=k)
        rank = None
        for i, row in enumerate(res):
            ok_proj = ("project" not in exp) or row.get("project") == exp["project"]
            ok_has = ("contains" not in exp) or (exp["contains"].lower() in (row.get("text") or "").lower())
            if ok_proj and ok_has:
                rank = i + 1
                break
        ranks.append(rank)
        if verbose:
            tag = f"#{rank}" if rank else "miss"
            print(f"  {tag:>5}  {q[:64]}")
    report("gold", len(ranks), metrics(ranks, ks=tuple(sorted({1, 3, k}))), project, k)
    return 0


def report(mode, n, m, project, k):
    scope = f" · project={project}" if project else ""
    path = "hybrid (semantic+keyword)" if EK else "keyword (no EMBED_KEY)"
    print(f"\nagent-memory-hub · recall eval [{mode}]  n={n} · k={k} · {path}{scope}")
    for key in sorted(m):
        if key.startswith("hit@"):
            print(f"  {key:<8} {m[key]*100:5.1f}%")
    print(f"  {'mrr':<8} {m['mrr']:.3f}")


def main(argv):
    mode, n, project, k, gold, verbose = "auto", DEFAULT_N, None, 5, None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--auto":
            mode = "auto"; n = int(argv[i + 1]); i += 2
        elif a == "--gold":
            mode = "gold"; gold = argv[i + 1]; i += 2
        elif a == "--project":
            project = argv[i + 1]; i += 2
        elif a == "--k":
            k = int(argv[i + 1]); i += 2
        elif a in ("-v", "--verbose"):
            verbose = True; i += 1
        else:
            print(f"arg desconhecido: {a}", file=sys.stderr); return 2
    if mode == "gold":
        return run_gold(gold, project, k, verbose)
    return run_auto(n, project, k, verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
