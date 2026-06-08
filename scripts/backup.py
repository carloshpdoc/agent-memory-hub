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
ENV_PATH = os.path.join(REPO, ".env")
PAGE = 1000


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def fetch_all(url, key):
    """Pagina /rest/v1/sessions e devolve todas as linhas."""
    rows = []
    offset = 0
    while True:
        req = urllib.request.Request(
            f"{url}/rest/v1/sessions?select=*&order=id&limit={PAGE}&offset={offset}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.loads(resp.read())
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return rows


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
    file_env = load_env(ENV_PATH)
    url = os.environ.get("SUPABASE_URL") or file_env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or file_env.get("SUPABASE_SECRET_KEY")
    backup_dir = os.environ.get("BACKUP_DIR") or file_env.get("BACKUP_DIR") \
        or os.path.join(REPO, "backups")
    keep = int(os.environ.get("KEEP") or file_env.get("KEEP") or 30)

    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes (env ou .env)", file=sys.stderr)
        return 1

    os.makedirs(backup_dir, exist_ok=True)
    try:
        rows = fetch_all(url, key)
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
