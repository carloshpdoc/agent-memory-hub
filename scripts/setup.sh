#!/usr/bin/env bash
# agent-memory-hub — one-shot setup / update for a machine. Idempotent.
#
# Does: git pull, ensure .env, apply Supabase migrations (if DB creds present),
# install the Claude Code hooks. Heavy/optional bits (Ollama fact extraction) are
# NOT enabled here, so it is safe to run on a weaker machine that only captures
# and reads memory.
#
# Usage: ./scripts/setup.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "==> git pull"
git pull --ff-only 2>/dev/null || echo "    (sem pull: sem remote/branch limpo)"

echo "==> .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    Criei .env a partir do exemplo. PREENCHA SUPABASE_URL e SUPABASE_SECRET_KEY e rode de novo."
  exit 1
fi
set -a; . ./.env; set +a
: "${SUPABASE_URL:?defina SUPABASE_URL no .env}"
: "${SUPABASE_SECRET_KEY:?defina SUPABASE_SECRET_KEY no .env}"
case "$SUPABASE_SECRET_KEY" in *xxx*|"") echo "    ERRO: SUPABASE_SECRET_KEY é placeholder"; exit 1 ;; esac
echo "    .env ok"

if [ -n "${DATABASE_POOLER_URL:-}" ] && ! printf '%s' "${DATABASE_POOLER_URL}" | grep -q '<'; then
  echo "==> migrações Supabase (idempotente)"
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install --quiet pg8000 >/dev/null 2>&1 || true
  .venv/bin/python scripts/migrate.py
else
  echo "==> migrações: puladas (sem DATABASE_POOLER_URL; o schema é compartilhado e já está aplicado)"
fi

echo "==> hooks do Claude Code"
python3 scripts/install_hooks.py

echo
echo "==> Pronto nesta máquina:"
echo "    captura + recall + resumo + hybrid search ativos (núcleo, sem LLM)."
echo "    Camada de fatos fica DESLIGADA aqui (FACTS_LLM=off). A extração roda só na máquina forte."
echo "    Reinicie o Claude Code para os hooks entrarem em vigor."
