#!/usr/bin/env python3
"""
agent-memory-hub — facts/regras → enforcement (roadmap item 12).

"Regra em markdown é wish list; hook é contrato." Regras em prosa são ignoradas
pelos agentes com frequência documentada; um hook PreToolUse que bloqueia o comando
violador, não. Este script pega as regras de perfil APROVADAS (as mesmas que
apply_profile_rules.py escreve em prosa) e pergunta ao LLM quais são mecanizáveis
como guard de comando shell — a maioria NÃO é, e tudo bem: só as com regex de
quase-zero falso positivo viram guard.

Human-gated, como todo o resto do projeto:
  - default é dry-run: imprime o guard que SERIA gerado e as decisões por regra;
  - --write grava o guard em ~/.claude/amh-guard.py e IMPRIME o snippet de
    settings.json pra VOCÊ colar — nunca edita settings.json sozinho.

Uso:
  python3 scripts/enforce_rules.py            # dry-run
  python3 scripts/enforce_rules.py --write    # grava o guard

Config (env ou ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, FACTS_LLM (+ provider),
  ENFORCE_GUARD_PATH (default ~/.claude/amh-guard.py).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, HERE)
from extract_facts import load_env, http, pick_caller  # noqa: E402

ENFORCE_PROMPT = """You turn a human code-workflow rule into a mechanical guard for shell commands, when possible.
RULE: {rule}
Reply ONLY JSON:
{{"enforceable": true|false,
  "pattern": "<python regex matched against a single shell command that VIOLATES the rule>",
  "message": "<short reminder of the rule, same language as the rule>"}}
- enforceable=true ONLY if a shell-command regex catches violations with near-zero false
  positives (e.g. a rule forbidding a specific command/flag).
- Style, architecture and judgment rules are NOT mechanizable: enforceable=false.
- Default to false when unsure.
"""

GUARD_TEMPLATE = '''#!/usr/bin/env python3
# agent-memory-hub — guard PreToolUse gerado por enforce_rules.py. NAO editar a mao:
# regenere com `python3 scripts/enforce_rules.py --write` apos aprovar novas regras.
# Protocolo: stdin = JSON do tool call; exit 2 + stderr = bloqueia com mensagem.
import json
import re
import sys

RULES = {rules_json}

try:
    p = json.load(sys.stdin, strict=False)
except Exception:
    sys.exit(0)
cmd = (p.get("tool_input") or {{}}).get("command") or ""
if not cmd:
    sys.exit(0)
for r in RULES:
    try:
        if re.search(r["pattern"], cmd):
            print("[agent-memory-hub] regra do teu perfil: " + r["message"], file=sys.stderr)
            sys.exit(2)
    except re.error:
        continue
sys.exit(0)
'''

SETTINGS_SNIPPET = '''  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash",
       "hooks": [{"type": "command", "command": "python3 %s"}]}
    ]
  }'''


def parse_enforce(txt):
    """Extrai a decisão do LLM; default NÃO-mecanizável (seguro)."""
    t = (txt or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        d = json.loads(t.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not d.get("enforceable"):
        return None
    pattern = (d.get("pattern") or "").strip()
    message = (d.get("message") or "").strip()
    if not pattern or not message:
        return None
    try:
        re.compile(pattern)
    except re.error:
        return None
    return {"pattern": pattern, "message": message}


def build_guard(rules):
    """Gera o texto do guard a partir das regras mecanizáveis. Determinístico."""
    rules_json = json.dumps(rules, ensure_ascii=False, indent=2)
    return GUARD_TEMPLATE.format(rules_json=rules_json)


def main(argv):
    write = "--write" in argv
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    name, caller = pick_caller(g)
    if not caller:
        print("ERRO: FACTS_LLM=off/invalido; preciso de um LLM pra classificar", file=sys.stderr)
        return 1

    rows = json.loads(http(
        f"{url}/rest/v1/profile_patterns?select=pattern,proposed_rule"
        f"&status=eq.approved&proposed_rule=not.is.null&order=confidence.desc&limit=100",
        {"apikey": key, "Authorization": f"Bearer {key}"}))
    rules_texts = [r["proposed_rule"].strip() for r in rows if (r.get("proposed_rule") or "").strip()]
    if not rules_texts:
        print("nenhuma regra aprovada; aprove padrões em `mem profile` primeiro")
        return 0

    print(f"{len(rules_texts)} regra(s) aprovada(s); classificando com {name}...\n")
    enforceable = []
    for rule in rules_texts:
        try:
            verdict = parse_enforce(caller(ENFORCE_PROMPT.format(rule=rule), g))
        except Exception as e:
            print(f"  llm falhou p/ regra: {type(e).__name__}", file=sys.stderr)
            verdict = None
        if verdict:
            print(f"  [guard]  {rule[:90]}")
            print(f"           regex: {verdict['pattern']}")
            enforceable.append(verdict)
        else:
            print(f"  [prosa]  {rule[:90]}")

    if not enforceable:
        print("\nnenhuma regra mecanizável — todas continuam valendo como prosa no profile-rules.md")
        return 0

    guard_path = os.path.expanduser(g("ENFORCE_GUARD_PATH", "~/.claude/amh-guard.py"))
    guard = build_guard(enforceable)
    if not write:
        print(f"\n(dry-run) {len(enforceable)} guard(s); geraria {guard_path}:\n")
        print(guard)
        print("para gravar: python3 scripts/enforce_rules.py --write")
        return 0

    os.makedirs(os.path.dirname(guard_path), exist_ok=True)
    with open(guard_path, "w") as f:
        f.write(guard)
    os.chmod(guard_path, 0o755)
    print(f"\ngravado {guard_path} com {len(enforceable)} guard(s)")
    print("adicione (uma vez) ao seu settings.json — o script NUNCA edita ele por você:\n")
    print(SETTINGS_SNIPPET % guard_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
