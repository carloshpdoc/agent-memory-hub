#!/usr/bin/env bash
# agent-memory-hub — backup via pg_dump (optional module).
#
# Full dump (schema + data) of the `public` schema -> rotating .sql.gz file.
# Connects through the Supabase Session Pooler (IPv4). Password is read from
# ~/.pgpass (never on the command line). Meant to run on an always-on host via cron.
#
# Config: reads ../.env (PG_POOLER_HOST, PG_POOLER_USER, ...). See .env.example.
# Restore: gunzip -c memory_XXXX.sql.gz | psql "<target-conn-string>"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

: "${PG_POOLER_HOST:?set PG_POOLER_HOST in .env}"
: "${PG_POOLER_USER:?set PG_POOLER_USER in .env}"
PG_POOLER_PORT="${PG_POOLER_PORT:-5432}"
PG_DATABASE="${PG_DATABASE:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/../backups}"
KEEP="${KEEP:-30}"
export PGSSLMODE=require

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%d_%H%M%S)"
out="$BACKUP_DIR/memory_${stamp}.sql.gz"

pg_dump -h "$PG_POOLER_HOST" -p "$PG_POOLER_PORT" -U "$PG_POOLER_USER" -d "$PG_DATABASE" \
  -n public --no-owner --no-privileges | gzip > "$out"

echo "$(date -u +%FT%TZ) OK $out ($(wc -c < "$out") bytes)"

# rotation: keep the newest $KEEP
ls -1t "$BACKUP_DIR"/memory_*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
