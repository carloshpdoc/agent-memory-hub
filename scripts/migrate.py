#!/usr/bin/env python3
"""
agent-memory-hub — apply SQL migrations to Supabase (idempotent).

Runs every sql/*.sql file in order against DATABASE_POOLER_URL (IPv4 Session Pooler;
falls back to DATABASE_URL). All migrations use IF NOT EXISTS / CREATE OR REPLACE,
so re-running is safe. Needs pg8000 (setup.sh installs it in .venv).

Config (env or ../.env): DATABASE_POOLER_URL (or DATABASE_URL).
"""
import glob
import os
import socket
import ssl
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
SQL_DIR = os.path.join(REPO, "sql")


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def split_statements(sql):
    """Split on ';' respecting $$...$$ bodies and skipping '--' line comments
    (comments may contain ';')."""
    out, cur, i, n, in_dollar = [], [], 0, len(sql), False
    while i < n:
        if not in_dollar and sql[i:i + 2] == "--":
            j = sql.find("\n", i)            # pula o comentario ate o fim da linha
            i = n if j == -1 else j
            continue
        if sql[i:i + 2] == "$$":
            in_dollar = not in_dollar
            cur.append("$$"); i += 2; continue
        c = sql[i]
        if c == ";" and not in_dollar:
            out.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def main():
    env = load_env(ENV_PATH)
    conn_url = (os.environ.get("DATABASE_POOLER_URL") or env.get("DATABASE_POOLER_URL")
                or os.environ.get("DATABASE_URL") or env.get("DATABASE_URL"))
    if not conn_url:
        print("DATABASE_POOLER_URL ausente; pulando migrações (schema já aplicado em outro lugar?)")
        return 0

    try:
        import pg8000.dbapi as pg
    except ImportError:
        print("ERRO: pg8000 não instalado (rode via .venv)", file=sys.stderr)
        return 1

    u = urlparse(conn_url)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    socket.setdefaulttimeout(20)
    conn = pg.connect(user=u.username, password=u.password, host=u.hostname,
                      port=u.port or 5432, database=(u.path.lstrip("/") or "postgres"),
                      ssl_context=ctx)
    cur = conn.cursor()
    for path in sorted(glob.glob(os.path.join(SQL_DIR, "*.sql"))):
        name = os.path.basename(path)
        stmts = split_statements(open(path).read())
        for s in stmts:
            cur.execute(s)
        conn.commit()
        print(f"  ok {name} ({len(stmts)} stmt)")
    conn.close()
    print("migrações aplicadas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
