#!/usr/bin/env python3
"""
agent-memory-hub — backup logico da tabela `sessions`.

Estrategia: pagina a REST API do Supabase (service_role) e grava todas as linhas
como NDJSON comprimido. Sem pg_dump, sem driver pg, sem instalar nada — Python stdlib.
A prova de versao do Postgres. Restauravel em qualquer Postgres junto com sql/01-schema.sql.

Config (env vars ou ../.env):
  SUPABASE_URL, SUPABASE_SECRET_KEY
  BACKUP_DIR   (default: <repo>/backups)
  KEEP         (default: 30 — quantos backups manter)

Uso:
  python3 scripts/backup.py
"""
import gzip
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from store import config, get_store  # noqa: E402
PAGE = 50   # sessions têm content de MBs: páginas de 1000 estouravam o tempo de leitura


def rotate(backup_dir, keep):
    files = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("sessions_") and f.endswith(".ndjson.gz")
    )
    for old in files[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(backup_dir, old))
            print(f"  rotacionado (removido): {old}")
        except OSError:
            pass


def main():
    store = get_store()
    backup_dir = config.get("BACKUP_DIR") or os.path.join(REPO, "backups")
    keep = int(config.get("KEEP") or 30)

    if not store.configured:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes (env ou .env)", file=sys.stderr)
        return 1

    os.makedirs(backup_dir, exist_ok=True)
    try:
        rows = store.table_rows("sessions", page=PAGE)
    except urllib.error.HTTPError as e:
        print(f"ERRO HTTP {e.code}: {e.read()[:200]}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = os.path.join(backup_dir, f"sessions_{stamp}.ndjson.gz")
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    size = os.path.getsize(out)
    print(f"OK: {len(rows)} sessoes -> {out} ({size} bytes)")
    rotate(backup_dir, keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
