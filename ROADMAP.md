# Roadmap

Where `agent-memory-hub` is and where it's going. This is a living document — open an issue
or PR to propose changes. Items marked **good first issue** are self-contained and a nice way
to start.

## Done

- **Core** — auto-capture (`Stop` checkpoint + `SessionEnd` final save) and recall
  (`SessionStart`), idempotent upsert, per-turn checkpoint that survives a crash.
- **Robust capture** — the three silent-failure bugs fixed and pinned by tests: control
  characters (`strict=False`), NUL byte (`strip_nul`), `echo`→`printf`.
- **Search** — hybrid keyword + semantic (`pgvector`), optional LLM `--rerank`.
- **Facts layer** — durable preferences/decisions/configs, temporal validity, meaning dedup
  (bring-your-own-LLM, free options).
- **Developer profile** — cross-project pattern synthesis → proposed rules, human-gated,
  per-project rule files, proactive surfacing at recall.
- **Explainable recall** — provenance in the injected digest; age-based confidence decay.
- **MCP server** — stdlib stdio/JSON-RPC (`recall_relevant`, `recent_sessions`, `get_facts`,
  `get_session`).
- **Console** — `stats`, `recent`, `search`, `facts`, `show`, `profile`, `standup`, `health`, `log`.
- **Observability** — coverage reconciliation + capture error-rate watch (`health`).
- **Weekly digest** — 7-day cross-project summary (LLM-free).
- **Backups** — daily `pg_dump` to portable `.sql`.
- **Adapters** — Codex CLI (JSONL) and Cursor (SQLite).
- **Tests + CI** — offline pytest suite, GitHub Actions on push/PR.
- **Packaging** — `pip install -e .` / `pipx` exposes a global `mem` command.
- **Recall eval harness** — `scripts/eval_recall.py`: measures whether recall surfaces the
  right past context (hit@k, MRR), auto (retrieval regression) and gold (curated) modes.

## Near-term

- **More capture adapters** — **good first issue.** Using `codex.py` (JSONL) or `cursor.py`
  (SQLite) as templates:
  - Gemini CLI
  - Windsurf
  - Zed AI
- **Publish to PyPI** — so `pipx install agent-memory-hub` works without cloning first (the
  editable install from a clone already gives a `mem` command today).
- **Recall eval — gold sets & tuning** — grow curated gold cases and use the harness to tune
  recall weights (recency vs. semantic), building on `scripts/eval_recall.py`.

## Later / ideas

- A read-only local web viewer for browsing sessions and facts.
- Adapter for JetBrains AI / Copilot Chat.
- Recall relevance tuning (per-kind weights, recency vs. semantic balance) informed by the eval
  harness.
- Optional multi-user / team mode with row-level scoping.

## Non-goals

- A hosted SaaS. The point is self-owned Postgres you control.
- Auto-applying anything that changes agent behavior without human review.
- Heavy dependencies in the capture/recall core (stays stdlib).
