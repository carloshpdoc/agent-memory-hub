#!/usr/bin/env python3
"""
agent-memory-hub — defrag/reflection job (roadmap item 5).

Passada periodica de manutencao da memoria, no espirito "sleep-time compute":
enquanto nada esta acontecendo, um LLM local arruma a casa. Duas etapas, ambas
NAO-DESTRUTIVAS (linhas sao marcadas com valid_until, nunca deletadas):

  1. dupes  — reusa consolidate_facts: pares quase-duplicados no mesmo scope sao
              julgados pelo LLM; duplicata/atualizacao superseda o fato antigo.
  2. stale  — fatos validos de tipos perecíveis (config/fact/procedure) mais velhos
              que STALE_AGE_DAYS sao julgados: estado efemero que quase certamente
              ja nao vale (task em andamento, pin temporario, status pontual) e
              invalidado. Na duvida, "keep" — o default e conservador.

Uso:
  python3 scripts/defrag_facts.py --dry-run       # preview, nao muda nada
  python3 scripts/defrag_facts.py                 # aplica
  python3 scripts/defrag_facts.py --stale-only    # so a etapa 2
  python3 scripts/defrag_facts.py --dupes-only    # so a etapa 1

Config (env ou ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, FACTS_LLM (+ vars do
  provider), STALE_AGE_DAYS (180), MIN_SIM (0.85 — etapa dupes).
Agende junto do weekly digest (cron/launchd) — precisa do LLM configurado.
"""
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, HERE)
from extract_facts import load_env, http, pick_caller  # noqa: E402
import consolidate_facts  # noqa: E402

STALE_KINDS = ("config", "fact", "procedure")  # preferencias/decisoes nao expiram sozinhas

STALE_PROMPT = """You judge whether a stored memory fact is still worth recalling.
It was recorded {age} days ago.
FACT (kind={kind}, scope={scope}): {fact}
Reply ONLY JSON: {{"status": "keep" | "stale"}}.
- "stale": ephemeral state almost surely obsolete after {age} days — an in-progress task,
  a temporary version pin, a one-off status, a dated TODO, something since replaced.
- "keep": setup facts, conventions, procedures and anything still plausibly true.
Default to "keep" when unsure.
"""


def parse_status(txt):
    """Extrai {"status": ...}; default 'keep' (seguro: na duvida nao invalida)."""
    t = (txt or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        d = json.loads(t.strip())
    except json.JSONDecodeError:
        return "keep"
    if isinstance(d, dict):
        return str(d.get("status", "keep")).lower()
    return "keep"


def sweep_stale(g, url, key, dry):
    caller_name, caller = pick_caller(g)
    if not caller:
        print("FACTS_LLM=off/invalido; etapa stale precisa de um LLM", file=sys.stderr)
        return 0
    age_days = int(g("STALE_AGE_DAYS", "180"))
    cutoff = datetime.now(timezone.utc).timestamp() - age_days * 86400
    # url-encode: o '+' do offset viraria espaco na query string do PostgREST
    cutoff_iso = urllib.parse.quote(datetime.fromtimestamp(cutoff, timezone.utc).isoformat())
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    kinds = ",".join(STALE_KINDS)
    rows = json.loads(http(
        f"{url}/rest/v1/facts?valid_until=is.null&kind=in.({kinds})"
        f"&valid_from=lt.{cutoff_iso}&order=valid_from.asc&limit=100"
        f"&select=id,fact,kind,scope,valid_from",
        {"apikey": key, "Authorization": f"Bearer {key}"}))
    print(f"[stale] {len(rows)} fato(s) perecivel(is) com mais de {age_days} dias; "
          f"julgando com {caller_name}...")

    n = 0
    for r in rows:
        try:
            ref = datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ref).days
        except Exception:
            age = age_days
        try:
            status = parse_status(caller(STALE_PROMPT.format(
                age=age, kind=r.get("kind"), scope=r.get("scope") or "global",
                fact=r.get("fact")), g))
        except Exception as e:
            print(f"  llm falhou: {type(e).__name__}", file=sys.stderr)
            continue
        if status == "stale":
            print(f"  [stale {age}d] ({r.get('scope') or 'global'}) {r['fact'][:80]}")
            if not dry:
                http(f"{url}/rest/v1/facts?id=eq.{r['id']}", {**H, "Prefer": "return=minimal"},
                     {"valid_until": datetime.now(timezone.utc).isoformat()}, "PATCH")
            n += 1
    print(f"[stale] {'(dry-run) ' if dry else ''}{n} fato(s) invalidado(s)")
    return n


def main(argv):
    dry = "--dry-run" in argv
    stale_only = "--stale-only" in argv
    dupes_only = "--dupes-only" in argv
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    if not stale_only:
        print("== etapa 1: dupes/supersessao (consolidate_facts) ==")
        consolidate_facts.main(["--dry-run"] if dry else [])
    if not dupes_only:
        print("== etapa 2: fatos stale ==")
        sweep_stale(g, url, key, dry)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
