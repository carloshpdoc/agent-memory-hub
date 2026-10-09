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
- Decaimento: a confianca de cada fato cai com a idade (meia-vida por tipo); fatos muito
  velhos somem do recall (RECALL_CONF_FLOOR), mas permanecem no banco. Read-time, nao-destrutivo.
- Orcamento de tokens (RECALL_MAX_TOKENS, default 1500): se o contexto montado estoura,
  encolhe previews para modo indice (divulgacao progressiva — detalhe completo via MCP)
  e, se ainda estourar, derruba itens de menor prioridade. Nada de despejo ilimitado.
- Transparencia: cada injecao e registrada em hooks/recall.log (JSON por linha: o que
  entrou, quanto custou, o que foi cortado) e o proprio contexto informa o custo.
- Nunca derruba a sessao: erro -> sai sem contexto.

Saida (stdout, formato SessionStart):
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request

from project_key import project_key
from handoff import build_handoff, should_inject, tool_from_payload
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")
LOG_PATH = os.path.join(HERE, "recall.log")
MAX_ENTRIES = 8
PREVIEW_CHARS = 280
INDEX_PREVIEW_CHARS = 90   # modo indice (divulgacao progressiva)
DEFAULT_MAX_TOKENS = 1500

# decaimento de confiança por idade (read-time, não-destrutivo): meia-vida em dias por tipo.
# duráveis (preference/decision) decaem devagar; config/fact, mais rápido.
HALF_LIFE_DAYS = {"preference": 240, "decision": 240, "config": 75, "fact": 90,
                  "procedure": 180}
DEFAULT_HALF_LIFE = 120


STALE_REF_PENALTY = 0.5


def stale_refs(fact):
    """Arquivos citados pelo fato que a última verificação contra o código não achou."""
    check = fact.get("code_check")
    return (check.get("missing") or []) if isinstance(check, dict) else []


def decayed_conf(base, kind, valid_from):
    """Confiança base * 0.5^(idade/meia-vida). Sem data válida -> retorna a base."""
    if base is None:
        return None
    try:
        ref = datetime.fromisoformat((valid_from or "").replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ref).days
    except Exception:
        return base
    if age <= 0:
        return base
    return base * (0.5 ** (age / HALF_LIFE_DAYS.get(kind, DEFAULT_HALF_LIFE)))


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


def preview(text, chars=PREVIEW_CHARS):
    t = " ".join((text or "").split())
    return t[:chars] + ("…" if len(t) > chars else "")


def est_tokens(text):
    """Heuristica de custo: ~4 chars/token. Serve pra orcamento, nao pra billing."""
    return (len(text or "") + 3) // 4


def assemble_context(project, facts_scored, rows, pending, max_tokens, handoff=""):
    """Monta o contexto dentro do orcamento de tokens.

    Divulgacao progressiva em 3 degraus: (1) previews completos; (2) se estourar,
    modo indice (previews de sessao curtos — o transcript completo fica a um
    `get_session`/`recall_relevant` de distancia via MCP); (3) se ainda estourar,
    derruba sessoes do fim da lista (as globais mais antigas) e depois fatos de
    menor confianca. Retorna (texto, stats) — stats alimenta o recall.log."""

    def build(fs, rs, chars):
        lines = []
        if handoff:   # vem primeiro: é a continuação direta do trabalho; nunca é cortado
            lines += ["## Passagem de contexto (continue de onde parou)",
                      "O último trabalho neste projeto foi em outra ferramenta ou máquina:", "",
                      handoff, ""]
        if fs:
            lines += ["## Fatos e preferências (memória durável)",
                      "_★ = projeto atual · conf = confiança com decaimento por idade · desde = válido desde._", ""]
            for eff, f in fs:
                tag = "★" if project and f.get("scope") == project else " "
                meta = f.get("kind", "fact")
                if f.get("confidence") is not None:
                    meta += f" · conf {eff:.2f}"
                vf = (f.get("valid_from") or "")[:10]
                if vf:
                    meta += f" · desde {vf}"
                warn = ""
                if stale_refs(f):
                    warn = " ⚠ cita arquivo que não existe mais: " + ", ".join(f"`{p}`" for p in stale_refs(f))
                lines.append(f"- {tag} ({meta}) {' '.join((f.get('fact') or '').split())}{warn}")
            lines.append("")
        if rs:
            lines += [
                "## Memória de sessões anteriores",
                f"Sessões passadas salvas no Supabase (projeto atual: `{project or 'nenhum'}`). "
                f"Use isto para continuidade; para o transcript completo de qualquer uma, "
                f"use a tool MCP `get_session` (servidor `agent-memory-hub`) com o session_id.",
                "",
            ]
            for r in rs:
                tag = "★" if project and r.get("project") == project else " "
                sid = (r.get("session_id") or "")[:8]
                lines.append(
                    f"- {tag} [{fmt_date(r.get('started_at'))} · {r.get('machine','?')} · "
                    f"{r.get('project') or '-'} · {sid}] {preview(r.get('summary') or r.get('content'), chars)}"
                )
        if pending:
            if rs:
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
        # lembrete de descoberta: as ferramentas de browse/search existem mas sao esquecidas.
        lines += [
            "",
            "## memory-hub — consulte proativamente (não espere o usuário pedir)",
            "Ao INICIAR uma tarefa substantiva, busque contexto passado ANTES de agir: chame a "
            "tool MCP `recall_relevant` (servidor `agent-memory-hub`) com uma query do que o "
            "usuário quer — ela traz sessões semanticamente relevantes (decisões, bugs já "
            "resolvidos, como algo foi feito). Sem o MCP, use `mem search <termo>` no terminal.",
            "Outras ferramentas (lembre o usuário quando útil): `mem standup` (o que ele fez "
            "hoje/semana), `mem recent`, `mem health` (cobertura/saúde da captura), `DIGEST.md` "
            "(resumo). O usuário tende a esquecer que existem — sugira em vez de só usar SQL.",
        ]
        return "\n".join(lines)

    fs, rs = list(facts_scored), list(rows)
    style = "full"
    text = build(fs, rs, PREVIEW_CHARS)
    if est_tokens(text) > max_tokens:
        style = "index"
        text = build(fs, rs, INDEX_PREVIEW_CHARS)
    while est_tokens(text) > max_tokens and (fs or rs):
        if rs:
            rs.pop()      # sessoes do fim (globais/mais antigas) caem primeiro
        else:
            fs.pop()      # depois fatos de menor confianca (lista ja ordenada)
        text = build(fs, rs, INDEX_PREVIEW_CHARS)
    stats = {
        "style": style,
        "est_tokens": est_tokens(text),
        "facts": [" ".join((f.get("fact") or "").split())[:60] for _, f in fs],
        "sessions": [(r.get("session_id") or "")[:8] for r in rs],
        "pending": len(pending),
        "dropped_facts": len(facts_scored) - len(fs),
        "dropped_sessions": len(rows) - len(rs),
        "handoff": bool(handoff),
    }
    return text, stats


_COUNTER_RE = re.compile(r"\s*\(\d+q/\d+r\)\s*$")


def topic_key(r):
    """Tema normalizado de uma sessao: o 1o pedido inteiro (o resumo sem contador e sem o
    arco '[...]'). So o MESMO pedido colapsa, ex. um job agendado rodando todo dia; um
    prefixo curto juntava pedidos distintos (slide-3 vs slide-5). Usado aqui e no recall
    por busca (memory_client)."""
    s = r.get("summary") or r.get("content") or ""
    s = _COUNTER_RE.sub("", s).split(" [...] ")[0]
    return " ".join(s.split()).lower()[:240]


def log_injection(project, source, stats):
    """Uma linha JSON por injecao — a resposta pra 'o que entrou no meu contexto?'."""
    try:
        entry = {"ts": datetime.now(timezone.utc).isoformat(),
                 "project": project, "source": source, **stats}
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    # mesmo guard do capture: as chamadas `claude -p` do extract/defrag/profile nao
    # devem receber ~1.4k tokens de recall em cada prompt
    if os.environ.get("AMH_NO_CAPTURE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    source = payload.get("source", "startup")
    if source not in ("startup", "clear"):
        return 0  # resume/compact: contexto ja presente

    cwd = payload.get("cwd") or os.getcwd()
    env = load_env(ENV_PATH)
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        return 0
    # None fora de um projeto (/, ~, ~/Development): nada e injetado como "projeto atual"
    project = project_key(cwd, {**env, **os.environ})

    sel = "select=session_id,started_at,ended_at,machine,tool,project,summary,content,metadata"
    try:
        # mesmas do projeto atual + mais recentes no geral
        proj_rows = (get(url, key, f"project=eq.{urllib.parse.quote(project)}"
                         f"&order=started_at.desc&limit=6&{sel}") if project else [])
        recent_rows = get(url, key, f"order=started_at.desc&limit=4&{sel}")
    except Exception:
        return 0

    # dedup por session_id E por tema (resumo normalizado), pulando sessoes sem conteudo util
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
    scope_filter = (f"or=(scope.eq.{urllib.parse.quote(project)},scope.is.null)" if project
                    else "scope=is.null")
    try:
        facts = get(url, key,
                    f"valid_until=is.null&{scope_filter}"
                    f"&order=created_at.desc&limit=12&select=fact,kind,scope,confidence,valid_from,code_check",
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

    # decaimento por idade: ordena por confiança decaída e descarta o que caiu sob o piso
    floor = float(env.get("RECALL_CONF_FLOOR", "0.2") or "0.2")
    facts_scored = []
    for f in facts:
        eff = decayed_conf(f.get("confidence"), f.get("kind", "fact"), f.get("valid_from"))
        if eff is not None and stale_refs(f):
            eff *= STALE_REF_PENALTY   # cita arquivo que sumiu (verify_facts.py): cai na fila
        if eff is not None and eff < floor:
            continue  # esquecimento suave: some do recall, permanece no banco
        facts_scored.append((eff if eff is not None else 0.0, f))
    facts_scored.sort(key=lambda t: -t[0])

    max_tokens = int(env.get("RECALL_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)) or DEFAULT_MAX_TOKENS)
    handoff = ""
    if project:
        try:   # última ATIVIDADE (ended_at): uma sessão longa começa cedo mas termina agora
            last = get(url, key, f"project=eq.{urllib.parse.quote(project)}"
                                 f"&order=ended_at.desc.nullslast&limit=1&{sel}")
            if last and should_inject(last[0], tool_from_payload(payload), socket.gethostname()):
                handoff = build_handoff(last[0])
        except Exception:
            handoff = ""
    text, stats = assemble_context(project, facts_scored, rows, pending, max_tokens, handoff)
    # transparencia no proprio contexto: quanto custou e onde esta o log
    cut = stats["dropped_facts"] + stats["dropped_sessions"]
    footer = (f"\n\n_recall: {len(stats['facts'])} fatos + {len(stats['sessions'])} sessões "
              f"(~{stats['est_tokens']} tokens, modo {stats['style']}"
              + (f", {cut} itens cortados pelo orçamento" if cut else "")
              + "). Log: hooks/recall.log_")
    log_injection(project, source, stats)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text + footer,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
