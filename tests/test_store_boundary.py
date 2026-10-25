"""Only the Supabase backend talks to Supabase, and only store/config reads the .env.
A module that builds its own /rest/v1 URL or .env reader would silently bypass the store
(and the local SQLite backend that replaces Supabase)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPABASE_URL_RE = re.compile(r"/(rest|storage|functions)/v1/")
SUPABASE_BACKEND = os.path.join("scripts", "store", "supabase.py")
# .env lido fora do store/config: o hook de commit (sem rede, roda sem o resto do repo) e o
# aplicador de migrações SQL do Postgres (específico do backend Supabase)
OWN_ENV_READER = {os.path.join("scripts", "store", "config.py"), os.path.join("scripts", "check_denylist.py"),
                  os.path.join("scripts", "migrate.py")}


def _sources():
    for d in ("hooks", "scripts", "dashboard"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, d)):
            if "__pycache__" in dirpath or ".egg-info" in dirpath:
                continue
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    yield os.path.relpath(path, ROOT), open(path).read()


def test_supabase_urls_only_in_supabase_backend():
    bad = [p for p, src in _sources() if SUPABASE_URL_RE.search(src) and p != SUPABASE_BACKEND]
    assert not bad, f"URL do Supabase fora de {SUPABASE_BACKEND}: {bad}"


def test_env_read_only_by_store_config():
    bad = [p for p, src in _sources() if re.search(r"^def load_env\(", src, re.M) and p not in OWN_ENV_READER]
    assert not bad, f"leitor de .env próprio (use store.config): {bad}"
