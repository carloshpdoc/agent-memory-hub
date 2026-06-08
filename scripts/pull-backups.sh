#!/usr/bin/env bash
# agent-memory-hub — pull backups from the always-on host to this machine (rsync).
#
# Config: reads ../.env (REMOTE_SSH, SSH_KEY, REMOTE_BACKUP_DIR). See .env.example.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

: "${REMOTE_SSH:?set REMOTE_SSH (user@host) in .env}"
: "${SSH_KEY:?set SSH_KEY (path to key) in .env}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-agent-memory-hub/backups/}"
LOCAL="${LOCAL:-$SCRIPT_DIR/../backups/}"

mkdir -p "$LOCAL"
rsync -av -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  "$REMOTE_SSH:$REMOTE_BACKUP_DIR" "$LOCAL"
echo "Backups in: $LOCAL"
ls -1t "$LOCAL"memory_*.sql.gz 2>/dev/null | head -5
