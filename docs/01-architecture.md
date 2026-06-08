# Architecture

## Overview

```
   any machine / any AI tool (Claude Code, Cursor, Codex, ...)
                       │
        hooks (capture / recall)        REST API / Supabase MCP
                       │                          │
                       └────────────┬─────────────┘
                                    ▼
                  Supabase — managed Postgres
                    table public.sessions  (always-on)
                                    │
              optional ────────────▼──────────── pg_dump (cron) → .sql.gz → pull locally
```

## Components

- **Supabase (managed Postgres)** — single source of truth. Always-on, reachable anywhere.
- **`public.sessions`** — one row per session (see `sql/01-schema.sql`).
- **REST API (PostgREST)** — auto-generated; any tool reads/writes over HTTP.
- **Supabase MCP (official)** — interactive querying from MCP-capable agents.
- **Capture hooks** (`Stop`, `SessionEnd`) — read the session transcript and upsert it.
- **Recall hook** (`SessionStart`) — injects a digest of relevant past sessions.
- **RLS on** — the secret (service_role) key used by hooks bypasses RLS; the public key
  reads nothing without a policy.

## Table `sessions`

```sql
id           uuid primary key default gen_random_uuid()
session_id   text unique            -- upsert key (idempotent capture)
tool         text                   -- 'claude-code', 'cursor', ...
machine      text                   -- hostname
project      text                   -- working dir basename
started_at   timestamptz
ended_at     timestamptz
content      text                   -- transcript / summary
metadata     jsonb
content_tsv  tsvector generated     -- full-text (config 'simple', good for mixed languages)
```

Full-text query: `content_tsv=fts(simple).<term>` (REST) — the `simple` config matches the
generated column (no stemming, language-agnostic).

## Capture flow

`Stop` (every turn, background) and `SessionEnd` (final) both call `capture_session.py`,
which reads the transcript JSONL, extracts user/assistant text, and upserts the row keyed by
`session_id` (`Prefer: resolution=merge-duplicates`). Same id → one row, always current.

## Recall flow

`SessionStart` calls `recall_session.py` (only on `startup`/`clear`), which queries recent
sessions for the current project plus a few most-recent overall, and returns a compact digest
via `hookSpecificOutput.additionalContext`. Previews are truncated; full transcripts stay on
demand via MCP/REST.

## Backup

- **`scripts/backup.sh`** — `pg_dump -n public --no-owner --no-privileges | gzip`, rotating,
  via the Supabase **Session Pooler** (IPv4; the direct host is IPv6-only). Password in
  `~/.pgpass`. Run on an always-on host via cron.
- **`scripts/pull-backups.sh`** — rsync backups to a local machine.
- **`scripts/backup.py`** — portable fallback: logical NDJSON dump via REST (pure stdlib,
  no Postgres client / version constraints).
- **Restore:** `gunzip -c memory_*.sql.gz | psql "<target>"`.

> Note: `pg_dump` must be **>= the server's major version**. Supabase uses recent Postgres,
> so install a matching client.

## Phase 2 — semantic search (planned)

Incremental, same table:
```sql
create extension if not exists vector;
alter table public.sessions add column embedding vector(384);  -- e.g. gte-small
create index on public.sessions using hnsw (embedding vector_cosine_ops);
```
Embed `content` via a Supabase Edge Function (`gte-small`), a local model, or an API. Combine
with the existing full-text index for hybrid search.
