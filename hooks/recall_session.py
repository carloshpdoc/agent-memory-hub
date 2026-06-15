#!/usr/bin/env python3
"""
agent-memory-hub — hook de recall (SessionStart).

Ao iniciar uma sessao, busca no Supabase as sessoes anteriores mais relevantes
(do mesmo projeto + mais recentes no geral) e injeta um resumo compacto no contexto,
para o agente "ja chegar sabendo". Detalhe completo fica sob demanda via MCP/REST.

- Pure stdlib (urllib).
- So injeta em source 'startup'/'clear' (pula 'resume'/'compact' p/ nao duplicar).
- Resumo truncado e limitado (nao despeja transcripts inteiros).
- Cada item vem com proveniencia (fatos: confianca + validade; sessoes: session_id),
  para o recall ser explicavel (de onde veio, quanto confiar).
- Proativo: se ha padroes de perfil detectados e ainda nao revisados, sugere revisa-los.
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


def get(url, key, query, table="sessions"):
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?{query}",
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

    # dedup por session_id E por tema (resumo normalizado), pulando sessoes sem conteudo util
    def topic_key(r):
        s = r.get("summary") or r.get("content") or ""
        return " ".join(s.split()).lower()[:60]

    seen_ids, seen_topics, rows = set(), set(), []
    for r in proj_rows + recent_rows:
        sid = r.get("session_id") or r.get("started_at")
        tk = topic_key(r)
        if not tk or sid in seen_ids or tk in seen_topics:
            continue
        seen_ids.add(sid)
        seen_topics.add(tk)
        rows.append(r)
        if len(rows) >= MAX_ENTRIES:
            break

    # fatos/preferencias validos (scope = projeto atual ou global)
    try:
        facts = get(url, key,
                    f"valid_until=is.null&or=(scope.eq.{project},scope.is.null)"
                    f"&order=created_at.desc&limit=12&select=fact,kind,scope,confidence,valid_from",
                    table="facts")
    except Exception:
        facts = []

    # sugestão proativa: padrões detectados (synthesize) ainda não revisados
    try:
        pending = get(url, key,
                      "status=eq.proposed&order=confidence.desc&limit=3"
                      "&select=pattern,confidence,evidence",
                      table="profile_patterns")
    except Exception:
        pending = []

    if not rows and not facts and not pending:
        return 0

    lines = []
    if facts:
        lines += ["## Fatos e preferências (memória durável)",
                  "_★ = projeto atual · conf = confiança (0-1) · desde = válido desde._", ""]
        for f in facts:
            tag = "★" if f.get("scope") == project else " "
            meta = f.get("kind", "fact")
            conf = f.get("confidence")
            if conf is not None:
                meta += f" · conf {conf:.2f}"
            vf = (f.get("valid_from") or "")[:10]
            if vf:
                meta += f" · desde {vf}"
            lines.append(f"- {tag} ({meta}) {' '.join((f.get('fact') or '').split())}")
        lines.append("")

    if rows:
        lines += [
            "## Memória de sessões anteriores",
            f"Sessões passadas salvas no Supabase (projeto atual: `{project}`). "
            f"Use isto para continuidade; para o transcript completo de qualquer uma, "
            f"consulte `public.sessions` via Supabase MCP (filtre por `session_id`).",
            "",
        ]
        for r in rows:
            tag = "★" if r.get("project") == project else " "
            sid = (r.get("session_id") or "")[:8]
            lines.append(
                f"- {tag} [{fmt_date(r.get('started_at'))} · {r.get('machine','?')} · "
                f"{r.get('project','?')} · {sid}] {preview(r.get('summary') or r.get('content'))}"
            )

    if pending:
        if rows:
            lines.append("")
        lines += [
            "## Padrões detectados aguardando sua revisão",
            "Estes se repetiram em vários projetos teus. Viram regra pro agente? "
            "Revise com `python3 scripts/memory.py profile`.",
            "",
        ]
        for p in pending:
            nproj = len(set((p.get("evidence") or {}).get("projects", [])))
            conf = p.get("confidence")
            meta = f"conf {conf:.2f} · {nproj} projetos" if conf is not None else f"{nproj} projetos"
            lines.append(f"- ({meta}) {' '.join((p.get('pattern') or '').split())}")

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
