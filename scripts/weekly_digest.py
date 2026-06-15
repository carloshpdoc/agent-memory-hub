#!/usr/bin/env python3
"""
agent-memory-hub — weekly digest (Phase 10).

Summarizes the last 7 days of sessions across ALL projects into a short digest:
what you worked on (grouped by project), what entered memory (new facts), and a
content hook if the week was notable. Pure stdlib, NO LLM (reuses the extractive
summaries already stored). Run on a cron (e.g. Monday) or on demand.

  python3 scripts/weekly_digest.py            # render DIGEST.md + print a one-liner
  python3 scripts/weekly_digest.py 14         # last 14 days

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY,
  DIGEST_DAYS (default 7), DIGEST_PATH (default <repo>/DIGEST.md),
  DIGEST_CONTENT_MIN (default 5 sessions -> suggest /content).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, HERE)
from extract_facts import load_env, http  # noqa: E402  (reuse env + http helpers)


def one_line(s, n=120):
    return " ".join((s or "").split())[:n]


def main(argv):
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    url, key = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY")
    if not url or not key:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    days = int(argv[0]) if argv and argv[0].isdigit() else int(g("DIGEST_DAYS", "7"))
    content_min = int(g("DIGEST_CONTENT_MIN", "5"))
    path = g("DIGEST_PATH", os.path.join(REPO, "DIGEST.md"))
    auth = {"apikey": key, "Authorization": f"Bearer {key}"}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")  # 'Z' evita o '+' que quebra a query

    sessions = json.loads(http(
        f"{url}/rest/v1/sessions?select=project,summary,started_at,tool,machine"
        f"&started_at=gte.{since_iso}&order=started_at.desc&limit=500", auth))
    facts = json.loads(http(
        f"{url}/rest/v1/facts?select=fact,scope,created_at"
        f"&created_at=gte.{since_iso}&valid_until=is.null&order=created_at.desc&limit=100", auth))

    by_proj = {}
    for s in sessions:
        by_proj.setdefault(s.get("project") or "?", []).append(s)

    today = datetime.now(timezone.utc).date()
    lines = [
        f"# Digest — últimos {days} dias ({since.date()} a {today})",
        "",
        f"{len(sessions)} sessão(ões) em {len(by_proj)} projeto(s); "
        f"{len(facts)} fato(s) novo(s) na memória.",
        "",
        "## Por projeto",
        "",
    ]
    for proj, ss in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {proj}  ({len(ss)})")
        for s in ss[:8]:
            lines.append(f"- {(s.get('started_at') or '')[:10]} · {one_line(s.get('summary') or '(sem resumo)')}")
        if len(ss) > 8:
            lines.append(f"- … e mais {len(ss) - 8} sessão(ões)")
        lines.append("")
    if facts:
        lines += ["## Entrou na memória (fatos novos)", ""]
        for f in facts[:20]:
            lines.append(f"- ({f.get('scope') or 'global'}) {one_line(f.get('fact'))}")
        lines.append("")
    if len(sessions) >= content_min:
        lines += [
            "## Gancho de conteúdo",
            "",
            f"Semana ativa ({len(sessions)} sessões). Vale rodar `/content` sobre o destaque.",
            "",
        ]

    with open(path, "w") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    print(f"digest: {path}  ({len(sessions)} sessões, {len(by_proj)} projetos, {len(facts)} fatos novos)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
