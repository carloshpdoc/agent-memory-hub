#!/usr/bin/env python3
"""
agent-memory-hub — synthesize developer profile (Phase 9, OPTIONAL, bring-your-own-LLM).

Reads the durable facts across ALL projects and asks an LLM to distill higher-order
patterns about how the developer works -- ones that recur across 2+ projects
(preferences, recurring fixes, tooling habits, anti-patterns, workflow). High-confidence
patterns get a proposed CLAUDE.md rule. New patterns are stored as 'proposed' for you to
review (memory.py -> profile). A human-readable retrato is written to PROFILE.md.

Idempotent: each run REPLACES the un-reviewed 'proposed' rows and re-renders PROFILE.md;
'approved'/'rejected' rows are preserved and passed to the LLM so it doesn't repeat them.

This layer is OPTIONAL. Pick a provider via FACTS_LLM (off|ollama|gemini|openai), same as
extract_facts.py. The core product needs no LLM.

Usage:
  python3 scripts/synthesize_profile.py            # synthesize + render PROFILE.md

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, FACTS_LLM (+ provider vars),
  MAX_PATTERNS (default 15), PROFILE_PATH (default <repo>/PROFILE.md).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, HERE)
from extract_facts import load_env, http, PROVIDERS  # noqa: E402  (reuse providers)

CATEGORIES = ["preference", "recurring_fix", "tooling_habit", "anti_pattern", "workflow"]

PROMPT = """You are profiling ONE developer from durable facts your memory extracted across MANY of their coding projects. Find HIGHER-ORDER patterns about how THIS developer works that recur across 2 OR MORE different projects.

Return ONLY JSON: {{"patterns": [ ... ]}}. Each element:
{{"pattern": "<one self-contained sentence: a habit/preference/recurring-fix/anti-pattern/workflow that holds across 2+ projects>",
  "category": "preference" | "recurring_fix" | "tooling_habit" | "anti_pattern" | "workflow",
  "evidence": {{"projects": ["<project>", "<project>"]}},
  "confidence": <number 0.0-1.0>,
  "proposed_rule": "<if confidence >= 0.7: ONE imperative CLAUDE.md rule the agent should follow; else null>"}}

Rules:
- Only patterns supported by evidence in 2+ DIFFERENT projects. If it shows up in a single project, SKIP it.
- Prefer few, high-signal patterns (max {max}). NO generic/obvious advice ("write clean code", "test your code").
- proposed_rule must be specific and actionable, phrased as an instruction to the agent.
- Do NOT propose anything in DONE (already adopted as a rule).
- Items in SET ASIDE were previously rejected. Propose one again ONLY if the facts now support it across clearly MORE distinct projects than the count noted next to it. Otherwise skip it.
- Write each pattern in the same language as the facts (Portuguese or English, as they appear).

DONE (already adopted -- never repeat):
{done}

SET ASIDE (previously rejected -- re-propose ONLY if now clearly more widespread than the noted count):
{aside}

FACTS (one per line, "fact  [scope]"):
{facts}
"""


def parse_patterns(txt):
    """Accept {"patterns":[...]}, a bare array, or markdown-fenced JSON."""
    t = (txt or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("patterns", [])
    return data if isinstance(data, list) else []


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def render_profile(rows, path):
    """Write a human-readable retrato grouped by category."""
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "preference"), []).append(r)
    titles = {
        "preference": "Preferências",
        "recurring_fix": "Fixes recorrentes",
        "tooling_habit": "Hábitos de tooling",
        "anti_pattern": "Anti-padrões",
        "workflow": "Workflow",
    }
    lines = [
        "# Perfil do desenvolvedor",
        "",
        "_Gerado por `synthesize_profile.py` a partir do histórico de sessões (cross-projeto)._",
        "_`★` = aprovado por mim · `?` = proposto, aguardando revisão (`memory.py profile`)._",
        "",
    ]
    for cat in CATEGORIES:
        items = by_cat.get(cat)
        if not items:
            continue
        lines.append(f"## {titles.get(cat, cat)}")
        lines.append("")
        for r in sorted(items, key=lambda x: -(x.get("confidence") or 0)):
            mark = "★" if r.get("status") == "approved" else "?"
            projs = ", ".join((r.get("evidence") or {}).get("projects", [])) or "?"
            conf = r.get("confidence") or 0
            lines.append(f"- {mark} {r.get('pattern')}  ")
            lines.append(f"  _conf {conf:.2f} · {projs}_")
            if r.get("proposed_rule"):
                lines.append(f"  → regra: `{r['proposed_rule']}`")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main():
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    provider = (g("FACTS_LLM", "off") or "off").lower()
    if provider not in PROVIDERS:
        print("FACTS_LLM precisa ser ollama/gemini/openai para sintetizar o perfil", file=sys.stderr)
        return 1
    caller = PROVIDERS[provider]
    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    max_patterns = int(g("MAX_PATTERNS", "15"))
    profile_path = g("PROFILE_PATH", os.path.join(REPO, "PROFILE.md"))
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    facts = json.loads(http(
        f"{url}/rest/v1/facts?select=fact,scope&valid_until=is.null&order=scope.nullslast&limit=500",
        {"apikey": key, "Authorization": f"Bearer {key}"}))
    if len(facts) < 4:
        print(f"poucos fatos ({len(facts)}) para sintetizar um perfil; rode extract_facts primeiro")
        return 0

    auth = {"apikey": key, "Authorization": f"Bearer {key}"}
    approved = json.loads(http(f"{url}/rest/v1/profile_patterns?select=pattern&status=eq.approved", auth))
    rejected = json.loads(http(f"{url}/rest/v1/profile_patterns?select=pattern,evidence&status=eq.rejected", auth))
    done = "\n".join(f"- {r['pattern']}" for r in approved) or "(nenhum ainda)"
    aside = "\n".join(
        f"- (tinha {len(set((r.get('evidence') or {}).get('projects', [])))} projeto(s)) {r['pattern']}"
        for r in rejected) or "(nenhum ainda)"
    facts_txt = "\n".join(f"{f['fact']}  [{f.get('scope') or 'global'}]" for f in facts)

    prompt = PROMPT.format(max=max_patterns, done=done, aside=aside, facts=facts_txt)
    try:
        patterns = parse_patterns(caller(prompt, g))
    except Exception as e:
        print(f"{provider} falhou: {type(e).__name__} {e}", file=sys.stderr)
        return 1

    # idempotente: limpa os 'proposed' antigos (nao revisados) e reinsere
    http(f"{url}/rest/v1/profile_patterns?status=eq.proposed", {**H, "Prefer": "return=minimal"},
         method="DELETE")

    stored = 0
    for p in patterns[:max_patterns]:
        pattern = (p.get("pattern") or "").strip()
        if len(pattern) < 8:
            continue
        cat = p.get("category") if p.get("category") in CATEGORIES else "preference"
        try:
            conf = max(0.0, min(1.0, float(p.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        ev = p.get("evidence") if isinstance(p.get("evidence"), dict) else {}
        projs = ev.get("projects") if isinstance(ev.get("projects"), list) else []
        if len(set(projs)) < 2:        # exige evidência cross-projeto (2+); não confia no LLM obedecer
            continue
        rule = (p.get("proposed_rule") or None)
        if isinstance(rule, str) and rule.strip().lower() in ("", "null", "none"):
            rule = None
        http(f"{url}/rest/v1/profile_patterns", {**H, "Prefer": "return=minimal"}, {
            "pattern": pattern, "category": cat, "evidence": ev,
            "confidence": conf, "proposed_rule": rule if conf >= 0.7 else None,
        }, "POST")
        stored += 1

    allrows = json.loads(http(
        f"{url}/rest/v1/profile_patterns?select=pattern,category,evidence,confidence,status,proposed_rule"
        f"&status=in.(proposed,approved)&order=confidence.desc&limit=200",
        {"apikey": key, "Authorization": f"Bearer {key}"}))
    render_profile(allrows, profile_path)

    print(f"[{provider}] {stored} padrão(ões) proposto(s) a partir de {len(facts)} fatos")
    print(f"retrato: {profile_path}")
    print("revise com: python3 scripts/memory.py profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
