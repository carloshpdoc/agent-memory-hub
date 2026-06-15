#!/usr/bin/env python3
"""
agent-memory-hub — apply approved profile rules (Phase 9).

Takes the profile patterns you APPROVED (memory.py -> profile) that carry a proposed_rule
and writes them to a SEPARATE file (default ~/.claude/profile-rules.md), overwriting it
whole each run. It never touches your hand-written CLAUDE.md. To load these rules every
session, add ONE line to ~/.claude/CLAUDE.md once, by hand:

    @profile-rules.md

Defensive by default: prints what WOULD be written (dry-run). Pass --write to actually write.

Usage:
  python3 scripts/apply_profile_rules.py            # dry-run (preview)
  python3 scripts/apply_profile_rules.py --write    # write the file

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY,
  PROFILE_RULES_PATH (default ~/.claude/profile-rules.md).
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, HERE)
from extract_facts import load_env, http  # noqa: E402

HEADER = (
    "<!-- agent-memory-hub: gerado por synthesize_profile.py + revisado por mim. "
    "NAO editar a mao; este arquivo e regenerado por apply_profile_rules.py. -->\n"
    "# Regras do meu perfil (derivadas do histórico)\n\n"
    "Padrões que se repetem nos meus projetos e que aprovei como regras pro agente seguir.\n\n"
)


def main(argv):
    write = "--write" in argv
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    path = os.path.expanduser(g("PROFILE_RULES_PATH", "~/.claude/profile-rules.md"))

    rows = json.loads(http(
        f"{url}/rest/v1/profile_patterns?select=pattern,category,proposed_rule,confidence"
        f"&status=eq.approved&proposed_rule=not.is.null&order=confidence.desc&limit=200",
        {"apikey": key, "Authorization": f"Bearer {key}"}))
    rules = [r for r in rows if (r.get("proposed_rule") or "").strip()]

    if not rules:
        print("nenhuma regra aprovada com proposed_rule; aprove padrões em: memory.py profile")
        return 0

    body = HEADER + "\n".join(f"- {r['proposed_rule'].strip()}" for r in rules) + "\n"

    if not write:
        print(f"(dry-run) escreveria {len(rules)} regra(s) em {path}:\n")
        print(body)
        print("para gravar de fato: python3 scripts/apply_profile_rules.py --write")
        return 0

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)
    print(f"gravadas {len(rules)} regra(s) em {path}")
    print("se ainda não fez, adicione UMA vez ao seu ~/.claude/CLAUDE.md:  @profile-rules.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
