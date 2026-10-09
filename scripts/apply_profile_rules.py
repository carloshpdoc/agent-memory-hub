#!/usr/bin/env python3
"""
agent-memory-hub — apply approved profile rules (Phase 9).

Takes the profile patterns you APPROVED (memory.py -> profile) that carry a proposed_rule
and writes them to a file your CLAUDE.md imports. It never touches hand-written content.

Two modes:
  (default)      one global file (~/.claude/profile-rules.md), imported once from CLAUDE.md,
                 plus a marked block in Codex's global ~/.codex/AGENTS.md (if Codex is
                 installed); text outside the block is never touched.
  --per-project  one file per project (~/.claude/profile-rules/<project>.md), each with the
                 rules whose evidence includes that project. Import the relevant file from each
                 repo's CLAUDE.md, so project-specific rules don't load in unrelated sessions.

  --cursor       copy the rules to the clipboard for Cursor's Settings -> Rules. Cursor keeps
                 user rules only in its settings UI (no file); repo rules (.cursor/rules)
                 would leak personal rules into work repos, so they are not used.

Defensive by default: prints what WOULD be written (dry-run). Pass --write to actually write.

Usage:
  python3 scripts/apply_profile_rules.py                       # dry-run, global
  python3 scripts/apply_profile_rules.py --write               # write the global file
  python3 scripts/apply_profile_rules.py --per-project         # dry-run, per-project
  python3 scripts/apply_profile_rules.py --per-project --write # write per-project files

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY,
  PROFILE_RULES_PATH (default ~/.claude/profile-rules.md),
  PROFILE_RULES_DIR  (default ~/.claude/profile-rules),
  CODEX_AGENTS_PATH  (default ~/.codex/AGENTS.md).
"""
import json
import os
import subprocess
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


def proj_header(proj):
    return (
        f"<!-- agent-memory-hub: regras de perfil do projeto '{proj}'. "
        "Gerado por apply_profile_rules.py --per-project; nao editar a mao. -->\n"
        f"# Regras do meu perfil — projeto {proj}\n\n"
    )


def sanitize(proj):
    return proj.replace("/", "-").replace(" ", "-")


def body_for(rules, header):
    return header + "\n".join(f"- {r['proposed_rule'].strip()}" for r in rules) + "\n"


BLOCK_START = "<!-- agent-memory-hub:rules:start (regenerado por apply_profile_rules.py) -->"
BLOCK_END = "<!-- agent-memory-hub:rules:end -->"


def upsert_block(text, body):
    """Replaces the marked block in text, or appends it. Text outside the markers is kept."""
    block = f"{BLOCK_START}\n{body.rstrip()}\n{BLOCK_END}\n"
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if start != -1 and end > start:
        return text[:start] + block + text[end + len(BLOCK_END):].lstrip("\n")
    if start != -1 or end != -1:
        raise ValueError("marcadores do agent-memory-hub incompletos; corrija o arquivo à mão")
    return (text.rstrip() + "\n\n" if text.strip() else "") + block


def main(argv):
    write = "--write" in argv
    per_project = "--per-project" in argv
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    rows = json.loads(http(
        f"{url}/rest/v1/profile_patterns?select=pattern,proposed_rule,confidence,evidence"
        f"&status=eq.approved&proposed_rule=not.is.null&order=confidence.desc&limit=200",
        {"apikey": key, "Authorization": f"Bearer {key}"}))
    rules = [r for r in rows if (r.get("proposed_rule") or "").strip()]
    if not rules:
        print("nenhuma regra aprovada com proposed_rule; aprove padrões em: memory.py profile")
        return 0

    if "--cursor" in argv:
        text = "\n".join(f"- {r['proposed_rule'].strip()}" for r in rules)
        copied = subprocess.run(["pbcopy"], input=text, text=True).returncode == 0 \
            if os.path.exists("/usr/bin/pbcopy") else False
        print(text)
        print(f"\n{len(rules)} regra(s)" + (" copiadas para a área de transferência." if copied else "."))
        print("Cole em Cursor -> Settings -> Rules (User Rules), substituindo a versão anterior.")
        return 0

    if per_project:
        dir_disp = g("PROFILE_RULES_DIR", "~/.claude/profile-rules")
        base_dir = os.path.expanduser(dir_disp)
        by_proj = {}
        for r in rules:
            for p in (r.get("evidence") or {}).get("projects", []):
                by_proj.setdefault(p, []).append(r)
        if not by_proj:
            print("as regras aprovadas não têm evidence.projects; nada por-projeto a escrever")
            return 0
        if not write:
            print(f"(dry-run) escreveria {len(by_proj)} arquivo(s) por projeto em {dir_disp}/:\n")
            for proj, rs in sorted(by_proj.items()):
                print(f"  {sanitize(proj)}.md  ({len(rs)} regra(s))")
            print("\npara gravar: python3 scripts/apply_profile_rules.py --per-project --write")
            return 0
        os.makedirs(base_dir, exist_ok=True)
        for proj, rs in sorted(by_proj.items()):
            with open(os.path.join(base_dir, sanitize(proj) + ".md"), "w") as f:
                f.write(body_for(rs, proj_header(proj)))
        print(f"gravados {len(by_proj)} arquivo(s) em {base_dir}/")
        print("no CLAUDE.md do repo de cada projeto, importe o arquivo dele, ex:")
        for proj in sorted(by_proj):
            print(f"  {proj}: @{dir_disp}/{sanitize(proj)}.md")
        return 0

    # global (default)
    path = os.path.expanduser(g("PROFILE_RULES_PATH", "~/.claude/profile-rules.md"))
    body = body_for(rules, HEADER)
    if not write:
        print(f"(dry-run) escreveria {len(rules)} regra(s) em {path}:\n")
        print(body)
        if os.path.isdir(os.path.expanduser(os.path.dirname(g("CODEX_AGENTS_PATH", "~/.codex/AGENTS.md")))):
            print("(o --write também atualiza o bloco de regras no AGENTS.md do Codex)")
        print("para gravar de fato: python3 scripts/apply_profile_rules.py --write")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)
    print(f"gravadas {len(rules)} regra(s) em {path}")
    print("se ainda não fez, adicione UMA vez ao seu ~/.claude/CLAUDE.md:  @profile-rules.md")

    codex = os.path.expanduser(g("CODEX_AGENTS_PATH", "~/.codex/AGENTS.md"))
    if os.path.isdir(os.path.dirname(codex)):
        old = open(codex).read() if os.path.exists(codex) else ""
        with open(codex, "w") as f:
            f.write(upsert_block(old, body))
        print(f"bloco de regras atualizado em {codex} (Codex)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
