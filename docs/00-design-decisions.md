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
