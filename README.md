# agent-memory-hub

> Leia em [Português](README.pt-br.md).

Persistent, **shared memory for AI coding agents** (Claude Code, and any MCP/REST-capable
tool), backed by **your own Supabase**. Every session is auto-saved to a Postgres table and
recalled at the start of the next one, across **sessions, tool instances, and machines**.

Self-hosted on your Supabase project. No SaaS in the middle. Your data, your backups.

## Why

Claude Code starts every session from zero. Tools like claude-mem or mem0 solve this, but
either store locally (no cross-machine) or run through a hosted service. `agent-memory-hub`
is the simplest self-owned take: a single Supabase table, three hooks, and an optional backup.

- **Cross-session:** open tomorrow, recall today.
- **Cross-instance:** multiple Claude Code configs share the same memory.
- **Cross-machine:** any machine pointing at the same Supabase shares everything.
- **Yours:** it's plain Postgres. `pg_dump` anytime, no lock-in.

## How it works

```
  SessionStart   -> recall_session.py    -> injects a digest of relevant past sessions
  ...session...
  Stop (each turn) -> capture_session.py  -> continuous checkpoint (background, upsert)
  SessionEnd     -> capture_session.py    -> final save
                       |
                       v
            Supabase (your project): table public.sessions
                       |
   optional backup ....v.... pg_dump (cron on an always-on host) -> .sql.gz -> pull locally
```

- Capture is **idempotent** (upsert by `session_id`). The `Stop` checkpoint means even an
  abrupt kill keeps the session up to its last turn.
- Recall injects only a **compact digest** (truncated previews). Full transcripts stay
  queryable on demand via the Supabase MCP or REST.

## Requirements and supported tools

The memory itself is just Postgres, so what's tool-specific is only the automatic
capture/recall, which ships as Claude Code hooks.

- **Auto-capture + recall (the hooks):** [Claude Code](https://claude.com/claude-code). The
  hooks use its `SessionStart`, `Stop`, and `SessionEnd` events.
- **Read and query the shared memory:** any MCP or REST capable AI tool, for example Cursor,
  Codex CLI, Gemini CLI, or ChatGPT, through the Supabase MCP or the REST API.
- **Capture from another tool:** point that tool's session lifecycle (or a manual step) at
  the same REST endpoint. `capture_session.py` is small; adapting it to another tool's
  transcript format is straightforward, and contributions are welcome.

Also needed:

- A free [Supabase](https://supabase.com) project.
- `python3` (hooks and backup are pure stdlib, no pip needed).

## Getting started (also: how to set it up on a machine)

### 1. Clone
```bash
git clone https://github.com/carloshpdoc/agent-memory-hub.git
cd agent-memory-hub
```

### 2. Create a Supabase project
At [supabase.com](https://supabase.com): new project. Enable **Data API** and **RLS**.
Grab from **Settings > API**: Project URL, publishable key, secret key.

### 3. Apply the schema
Open **SQL Editor** in Supabase and run [`sql/01-schema.sql`](sql/01-schema.sql).
It creates the `sessions` table, the full-text index, and RLS.

### 4. Configure `.env`
```bash
cp .env.example .env
# edit .env with your SUPABASE_URL and SUPABASE_SECRET_KEY (and backup vars if used)
```
`.env` is gitignored. The hooks read it directly.

### 5. Wire the hooks into Claude Code
Add to your Claude Code `settings.json` (`~/.claude/settings.json` for user scope), using the
**absolute path** to your clone:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "python3 /ABS/PATH/agent-memory-hub/hooks/recall_session.py", "timeout": 15 }
      ]}
    ],
    "Stop": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "payload=$(cat); echo \"$payload\" | python3 /ABS/PATH/agent-memory-hub/hooks/capture_session.py >/dev/null 2>&1 &" }
      ]}
    ],
    "SessionEnd": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "python3 /ABS/PATH/agent-memory-hub/hooks/capture_session.py", "timeout": 20 }
      ]}
    ]
  }
}
```
> If you already have hooks for these events, **add** these entries to the existing arrays.

### 6. (optional) Add the Supabase MCP
Lets the agent query memories interactively:
```bash
claude mcp add --scope user --transport http supabase \
  "https://mcp.supabase.com/mcp?project_ref=<YOUR_PROJECT_REF>"
# then authenticate: /mcp > supabase
```

### 7. (optional) Backup module
On an always-on host with `pg_dump` at or above your Postgres major version, and the repo cloned:
- Put your pooler creds in `~/.pgpass` (chmod 600):
  `HOST:5432:postgres:postgres.<PROJECT_REF>:PASSWORD`
- Fill the `PG_POOLER_*` vars in `.env`.
- Cron: `30 3 * * * /ABS/PATH/agent-memory-hub/scripts/backup.sh >> .../backup.log 2>&1`
- Pull copies locally with `scripts/pull-backups.sh` (set `REMOTE_SSH` and `SSH_KEY` in `.env`).

## Adding another machine

This is the whole point, and it's trivial:

1. Clone the repo on the new machine.
2. Copy the **same `.env`** (same Supabase credentials).
3. Apply the hooks block to that machine's `settings.json` (same as step 5).

Done. That machine now writes to and reads from the **same shared memory**. No need to
re-run the schema, because the table already exists in your Supabase.

## Configuration reference

| Var | Used by | Meaning |
|-----|---------|---------|
| `SUPABASE_URL` | hooks, backup.py | `https://<ref>.supabase.co` |
| `SUPABASE_SECRET_KEY` | hooks | service_role key (writes, bypasses RLS) |
| `PG_POOLER_HOST`, `PG_POOLER_USER` | backup.sh | Session Pooler host, `postgres.<ref>` |
| `BACKUP_DIR`, `KEEP` | backup.sh, backup.py | output dir, how many to keep |
| `REMOTE_SSH`, `SSH_KEY` | pull-backups.sh | always-on host, SSH key |
| `EMBED_KEY` | embed_pending.py, search.py | guard for the embedding function (Phase 2) |

## Querying your memory

- **MCP:** ask the agent. It runs SQL via the Supabase MCP.
- **REST full-text:** `GET /rest/v1/sessions?content_tsv=fts(simple).<term>` with the secret key.
- **Filters:** by `project`, `machine`, `started_at`, `session_id`.

## Semantic search (Phase 2)

Optional. Adds meaning-based recall on top of full-text search, using `pgvector` and the
`gte-small` model running inside a Supabase Edge Function (free, no external API).

1. Run [`sql/02-phase2-pgvector.sql`](sql/02-phase2-pgvector.sql). It adds the `embedding`
   column, the HNSW index, and the `match_sessions` RPC.
2. Set a guard secret and deploy the function:
   ```bash
   supabase secrets set EMBED_KEY=$(openssl rand -hex 24)
   supabase functions deploy embed --no-verify-jwt   # new API keys are not JWTs
   ```
   Put the same `EMBED_KEY` in your `.env`.
3. Embed existing rows: `python3 scripts/embed_pending.py`. Run it on a cron to keep new
   sessions embedded (for example `*/15 * * * *` on your always-on host).
4. Search: `python3 scripts/search.py "how did we set up backups"`.

The Edge Function returns only vectors and counts, never session content.

## Security

- Secrets live only in `.env` and `~/.pgpass` (gitignored, chmod 600). Never commit them.
- RLS is on. The public (anon) key can't read without a policy. Hooks use the secret key.
- The secret key is powerful. Treat it like a password.

## Files

```
hooks/capture_session.py    capture (Stop + SessionEnd)
hooks/recall_session.py     recall  (SessionStart)
sql/01-schema.sql           table + full-text + RLS
sql/02-phase2-pgvector.sql  pgvector + match_sessions RPC (Phase 2)
supabase/functions/embed/   gte-small embedding Edge Function (Phase 2)
scripts/backup.sh           pg_dump backup (cron on an always-on host)
scripts/pull-backups.sh     rsync backups to this machine
scripts/backup.py           portable logical backup (REST/NDJSON, no pg client)
scripts/embed_pending.py    embed sessions missing a vector (Phase 2)
scripts/search.py           semantic search CLI (Phase 2)
docs/                       architecture and design decisions
```

## License

[MIT](LICENSE)

## Star, share, contribute

If this saved you from re-explaining your project to your agent for the tenth time today,
give the repo a star. It genuinely helps other people find it.

Ideas, rough edges, or a Phase 3 you want? Open an issue or a pull request. If you build
something on top of agent-memory-hub, I would love to see it.

## Built by

Made by **[buildcomcarlos.com](https://buildcomcarlos.com)**: articles and open tools on AI
agents, iOS, and shipping software solo. If this project was useful, the site is where the
deep dives and the next experiments live. Come say hi.
