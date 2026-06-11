#!/usr/bin/env python3
"""
agent-memory-hub — hook de recall (SessionStart).

Ao iniciar uma sessao, busca no Supabase as sessoes anteriores mais relevantes
(do mesmo projeto + mais recentes no geral) e injeta um resumo compacto no contexto,
para o agente "ja chegar sabendo". Detalhe completo fica sob demanda via MCP/REST.

- Pure stdlib (urllib).
- So injeta em source 'startup'/'clear' (pula 'resume'/'compact' p/ nao duplicar).
- Resumo truncado e limitado (nao despeja transcripts inteiros).
- Nunca derruba a sessao: erro -> sai sem contexto.

Saida (stdout, formato SessionStart):
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")
MAX_ENTRIES = 8
PREVIEW_CHARS = 280


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


def get(url, key, query):
    req = urllib.request.Request(
        f"{url}/rest/v1/sessions?{query}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def fmt_date(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (iso or "")[:16]


def preview(text):
    t = " ".join((text or "").split())
    return t[:PREVIEW_CHARS] + ("…" if len(t) > PREVIEW_CHARS else "")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    source = payload.get("source", "startup")
    if source not in ("startup", "clear"):
        return 0  # resume/compact: contexto ja presente

    cwd = payload.get("cwd") or os.getcwd()
    project = os.path.basename(cwd.rstrip("/")) or cwd

    env = load_env(ENV_PATH)
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        return 0

    sel = "select=session_id,started_at,machine,tool,project,summary,content"
    try:
        # mesmas do projeto atual + mais recentes no geral
        proj_rows = get(url, key, f"project=eq.{project}&order=started_at.desc&limit=6&{sel}")
        recent_rows = get(url, key, f"order=started_at.desc&limit=4&{sel}")
    except Exception:
        return 0

    seen, rows = set(), []
    for r in proj_rows + recent_rows:
        sid = r.get("session_id") or r.get("started_at")
        if sid in seen:
            continue
        seen.add(sid)
        rows.append(r)
        if len(rows) >= MAX_ENTRIES:
            break

    if not rows:
        return 0

    lines = [
        f"## Memória de sessões anteriores",
        f"Sessões passadas salvas no Supabase (projeto atual: `{project}`). "
        f"Use isto para continuidade; para o transcript completo de qualquer uma, "
        f"consulte `public.sessions` via Supabase MCP (filtre por `session_id`).",
        "",
    ]
    for r in rows:
        tag = "★" if r.get("project") == project else " "
        lines.append(
            f"- {tag} [{fmt_date(r.get('started_at'))} · {r.get('machine','?')} · "
            f"{r.get('project','?')}] {preview(r.get('summary') or r.get('content'))}"
        )

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
