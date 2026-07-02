# Design decisions

Why the project is built the way it is.

## Goal

Save **entire agent sessions** to a database and access them from **anywhere** and from
**any AI tool**, with the user owning the data.

## Why Supabase (and not the alternatives)

| Option | Saves session | Always-on / anywhere | Any tool | Self-owned | Verdict |
|---|---|---|---|---|---|
| Build from scratch | — | — | — | — | reinvents solved infra |
| claude-mem | yes (auto) | no (local SQLite) | partial | yes | no cross-machine |
| mem0 (self-hosted) | yes (LLM extract) | depends on host | yes (MCP) | yes | heavier than needed for a simple store |
| Custom MCP server | yes | depends on host | yes | yes | unnecessary — Supabase ships REST + MCP |
| Local + tunnel (ngrok) | yes | no (laptop isn't always-on) | yes | yes | not truly always-on |
| **Supabase** | yes | **yes (managed)** | **REST + official MCP** | **yes** | **chosen** |

Reasons:
1. **Always-on** managed Postgres, reachable from any machine — no dependency on a laptop or
   a self-managed server being up.
2. **No wheel reinvention** — auto-generated REST API (PostgREST) + official Supabase MCP.
   Any AI tool connects via MCP, or via REST.
3. **pgvector native** — semantic search (Phase 2) is an incremental upgrade on the *same*
   table, no platform migration.
4. **No lock-in** — it's plain Postgres; `pg_dump` to a `.sql` anytime, restore anywhere.

## Why hooks (not just the MCP)

Saving the session must be **automatic and independent** of any MCP being connected/authed.
A Claude Code `Stop`/`SessionEnd` hook is a shell command that runs every session with no
manual step. The MCP is only a convenience for *reading* memories interactively.

## Why a `Stop` checkpoint (not only `SessionEnd`)

`SessionEnd` only fires on a clean exit. An abrupt kill (crash, killed terminal, dropped SSH)
would lose the session. Capturing on `Stop` too — upserting by `session_id` — keeps the row
current every turn, so abrupt termination still leaves the session saved up to its last turn.

## Why a logical/`pg_dump` backup despite Supabase's own backups

Owning a portable copy is the whole anti-lock-in point. `pg_dump` (or the REST/NDJSON
fallback) gives a `.sql`/`.ndjson` you control, restorable into any Postgres.

## Why recall has no recency term, and stays 1:1 RRF (measured, not assumed)

The intuitive "tune": weight recent sessions higher, and bias the fusion toward semantic.
We didn't guess — we measured with the recall eval harness (`scripts/eval_recall.py`),
sweeping variants of `hybrid_search` on a fixed 60-session set (spread across the corpus by
`session_id`, not recency-skewed). hit@1 / hit@5 / MRR:

| Variant | hit@1 | hit@5 | MRR |
|---|---|---|---|
| **baseline (RRF 1:1, k=50, no recency)** | **61.7%** | **71.7%** | **0.671** |
| fts-heavy (2:1) | 63.3% | 71.7% | 0.681 |
| vec-heavy (1:2) | 61.7% | 71.7% | 0.661 |
| rrf_k = 20 / 100 | 61.7% | 71.7% | 0.671 |
| + recency (w=0.02) | 35.0% | 46.7% | 0.422 |
| + recency (w=0.05) | 16.7% | 31.7% | 0.241 |

Findings:
- **Recency is actively harmful** for retrieving the right session: −27 to −45 points at
  hit@1. It promotes recent-but-wrong sessions over the correct keyword/semantic match. So
  recall deliberately has **no recency term**.
- **Weight tuning is noise here**: the one variant above baseline (fts-heavy) is +1.7pp =
  one session out of 60. Shipping it would be overfitting.
- **`rrf_k` has no effect** with equal weights (it rescales both sides identically).

Conclusion: keep the baseline. This is the project's own thesis — *verify, don't trust* —
applied to itself; measuring killed a plausible change that would have degraded recall.
(Recency could still serve a *different* goal — "what was I just doing" — but that's a
separate, opt-in feature, not a change to precision-oriented recall.)
