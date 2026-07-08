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

## In progress — market-research backlog (Jul 2026)

Twelve ideas distilled from a competitive sweep (GitHub OSS landscape, HN/Reddit pain
points, Product Hunt / vendor-native memory features). Context: by 2026 every major
vendor ships machine-local automatic memory (Claude Auto Memory, Cursor Memories,
Copilot Memory, Codex Memories) — all siloed per tool and per machine. The gaps this
project targets: self-hosted + cross-agent + cross-machine + measured quality +
human-gated curation. Full research notes in `docs/00-design-decisions.md`.

Shipped (Jul 2026):

1. ~~**Secrets redaction in capture**~~ — done: always-on masking of private-key blocks,
   cloud/API tokens and `NAME=value` credentials in `capture_session.py`.
2. ~~**Temporal fact supersession at extraction**~~ — done: an LLM judge invalidates the
   contradicted near-match (`valid_until` + `superseded_by`, non-destructive).
3. ~~**Token budget on recall + injection log**~~ — done: `RECALL_MAX_TOKENS` hard cap +
   per-injection JSON log in `hooks/recall.log` + cost footer in the context itself.
4. ~~**Publish eval numbers**~~ — done: `--spread` sampling mode + measured hit@k/MRR in
   both READMEs.
5. ~~**Defrag/reflection job**~~ — done: `scripts/defrag_facts.py` (dupes + stale sweeps,
   non-destructive, conservative defaults).
6. ~~**Progressive disclosure recall**~~ — done: over-budget context degrades to a compact
   index; full detail via MCP `get_session`.
7. ~~**`<private>` tag**~~ — done: `<private>...</private>` content is never persisted
   (fail-closed on unclosed tags).
8. ~~**Procedural memory**~~ — done: `procedure` fact kind with its own recall half-life.
9. ~~**Markdown export**~~ — done: `mem export` (facts / sessions / approved rules).
12. ~~**Facts → enforcement**~~ — done: `scripts/enforce_rules.py` generates a human-reviewed
   PreToolUse guard from mechanizable approved rules.

Saved for later (decide after the batch above):

10. **Team scope with RLS** — personal fact → team fact with review; Supabase RLS makes
    this cheap. Only worth it with a team actually sharing the hub. (Egregore, ByteRover.)
11. **Web viewer** — read-only observability UI for sessions/facts. Lesson from
    claude-mem's unauthenticated-local-API incident: ship with auth from day one.

## Near-term

- **More capture adapters** — **good first issue.** Using `codex.py` (JSONL) or `cursor.py`
  (SQLite) as templates:
  - Gemini CLI
  - Windsurf
  - Zed AI
- **Publish to PyPI** — so `pipx install agent-memory-hub` works without cloning first (the
  editable install from a clone already gives a `mem` command today).
- **Recall eval — gold sets** — grow curated gold cases, building on `scripts/eval_recall.py`
  (recency-weight tuning was measured and rejected; see `docs/00-design-decisions.md`).

## Later / ideas

- Adapter for JetBrains AI / Copilot Chat.
- Per-kind recall weights informed by the eval harness.

## Non-goals

- A hosted SaaS. The point is self-owned Postgres you control.
- Auto-applying anything that changes agent behavior without human review.
- Heavy dependencies in the capture/recall core (stays stdlib).
