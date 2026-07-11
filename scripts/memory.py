#!/usr/bin/env python3
"""
agent-memory-hub — memory console (Phase 8).

A single terminal entry point to browse, search and inspect your shared memory.
Pure stdlib, no server, no key in a browser. Run a subcommand, or run with no
arguments for an interactive prompt.

  python3 scripts/memory.py                     # interactive
  python3 scripts/memory.py stats
  python3 scripts/memory.py recent [N]
  python3 scripts/memory.py search [--project P] "<query>"
  python3 scripts/memory.py facts [project]
  python3 scripts/memory.py show <session-id-prefix>
  python3 scripts/memory.py profile [approve|reject|reopen <id-prefix> | rejected]
  python3 scripts/memory.py health              # cobertura local↔Supabase + saúde da captura
  python3 scripts/memory.py log [N]             # últimas N linhas do log de captura
  python3 scripts/memory.py standup [today|yesterday|week]  # o que você tocou, por projeto
  python3 scripts/memory.py export [dir]        # dump Markdown versionável (default memory-export/)
  python3 scripts/memory.py skills [dir] [--write]  # procedures → SKILL.md (default ~/.claude/skills)
  python3 scripts/memory.py extract [--embed]   # extrai facts das sessões novas (incremental)
  python3 scripts/memory.py reprocess [how-to|all] [--embed]  # reseta + re-extrai TUDO (pesado)

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY (for search).
"""
import glob
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from memory_client import ENV, URL, KEY, EK, rest, rpc, write, embed  # noqa: E402  núcleo compartilhado

_TTY = sys.stdout.isatty()
def c(s, code):
    return f"\033[{code}m{s}\033[0m" if _TTY else s
def bold(s): return c(s, "1")
def dim(s): return c(s, "2")
def cyan(s): return c(s, "36")
def green(s): return c(s, "32")
def yellow(s): return c(s, "33")


def one_line(s, n=90):
    return " ".join((s or "").split())[:n]


def fmt_date(iso):
    return (iso or "")[:16].replace("T", " ")


# ---- commands -------------------------------------------------------------
def cmd_stats(_args):
    s = rest("sessions?select=id")
    tools = rest("sessions?select=tool")
    facts = rest("facts?select=id&valid_until=is.null")
    by_tool = {}
    for r in tools:
        by_tool[r.get("tool", "?")] = by_tool.get(r.get("tool", "?"), 0) + 1
    print(bold("agent-memory-hub"))
    print(f"  sessões: {green(len(s))}  ({dim(', '.join(f'{k}:{v}' for k, v in by_tool.items()))})")
    print(f"  fatos:   {green(len(facts))}")


def cmd_recent(args):
    n = int(args[0]) if args and args[0].isdigit() else 10
    rows = rest(f"sessions?select=session_id,started_at,machine,tool,project,summary"
                f"&order=started_at.desc&limit={n}")
    for r in rows:
        print(f"{dim(fmt_date(r.get('started_at')))}  {cyan(r.get('tool', '?'))}  "
              f"{yellow(r.get('project', '?'))} {dim('· ' + (r.get('machine') or '?'))}")
        print(f"  {dim(r.get('session_id', '')[:8])} {one_line(r.get('summary') or '(sem resumo)')}")


def cmd_search(args):
    project = None
    if len(args) >= 2 and args[0] == "--project":
        project, args = args[1], args[2:]
    query = " ".join(args).strip()
    if not query:
        print("uso: search [--project P] <query>"); return
    if EK:
        rows = rpc("hybrid_search", {"query_text": query, "query_embedding": embed(query),
                                     "match_count": 8, "filter_project": project})
        for r in rows:
            score = f"{r['score']:.3f}"
            print(f"{green(score)}  {yellow(r.get('project', '?'))} "
                  f"{dim('· ' + (r.get('machine') or '?'))}")
            print(f"  {dim(r.get('session_id', '')[:8])} {one_line(r.get('content'))}")
    else:  # sem EMBED_KEY: full-text only
        q = urllib.parse.quote(query)
        flt = f"&project=eq.{project}" if project else ""
        rows = rest(f"sessions?select=session_id,project,machine,summary,content"
                    f"&content_tsv=fts(simple).{q}{flt}&limit=8")
        for r in rows:
            print(f"{yellow(r.get('project', '?'))} {dim('· ' + (r.get('machine') or '?'))}")
            print(f"  {dim(r.get('session_id', '')[:8])} {one_line(r.get('summary') or r.get('content'))}")


def cmd_facts(args):
    scope = args[0] if args else None
    flt = (f"&or=(scope.eq.{urllib.parse.quote(scope)},scope.is.null)" if scope else "")
    rows = rest(f"facts?select=fact,kind,scope&valid_until=is.null{flt}"
                f"&order=scope.nullslast,kind&limit=60")
    for r in rows:
        tag = green("★") if scope and r.get("scope") == scope else " "
        kind = "(" + str(r.get("kind", "fact")) + ")"
        print(f"{tag} {dim(kind)} {yellow(r.get('scope') or 'global')}: "
              f"{one_line(r.get('fact'), 100)}")


def cmd_show(args):
    if not args:
        print("uso: show <session-id-prefix>"); return
    pref = args[0]
    rows = rest(f"sessions?select=session_id,tool,project,machine,started_at,content"
                f"&session_id=like.{pref}*&limit=1")
    if not rows:
        print("não encontrada"); return
    r = rows[0]
    print(bold(f"{r.get('project')} · {r.get('tool')} · {r.get('machine')} · {fmt_date(r.get('started_at'))}"))
    print(dim(r.get("session_id"))); print()
    print(r.get("content", "")[:8000])


def cmd_profile(args):
    """List / approve / reject synthesized developer-profile patterns (Phase 9)."""
    action = args[0] if args else "list"
    if action in ("approve", "reject"):
        if len(args) < 2:
            print(f"uso: profile {action} <id-prefix>"); return
        pref = args[1]
        # uuid não aceita LIKE no PostgREST; resolve o prefixo no cliente e usa eq
        ids = [r["id"] for r in rest("profile_patterns?select=id") if r["id"].startswith(pref)]
        if len(ids) != 1:
            print(yellow(f"prefixo '{pref}' casou {len(ids)} padrão(ões); seja mais específico")); return
        status = "approved" if action == "approve" else "rejected"
        write(f"profile_patterns?id=eq.{ids[0]}",
              {"status": status, "reviewed_at": datetime.now(timezone.utc).isoformat()})
        print(f"{dim(ids[0][:8])} → {green(status) if status == 'approved' else yellow(status)}")
        return
    if action == "reopen":                       # tira da "geladeira": rejeitado -> proposto
        if len(args) < 2:
            print("uso: profile reopen <id-prefix>"); return
        ids = [r["id"] for r in rest("profile_patterns?select=id") if r["id"].startswith(args[1])]
        if len(ids) != 1:
            print(yellow(f"prefixo '{args[1]}' casou {len(ids)} padrão(ões)")); return
        write(f"profile_patterns?id=eq.{ids[0]}", {"status": "proposed", "reviewed_at": None})
        print(f"{dim(ids[0][:8])} → {cyan('proposed (reaberto)')}")
        return
    flt = "status=eq.rejected" if action == "rejected" else "status=in.(proposed,approved)"
    rows = rest("profile_patterns?select=id,pattern,category,confidence,status,evidence,proposed_rule"
                f"&{flt}&order=status.asc,confidence.desc&limit=100")
    if not rows:
        print(dim("nenhum padrão ainda — rode: python3 scripts/synthesize_profile.py")); return
    for r in rows:
        st = r.get("status")
        mark = green("★") if st == "approved" else (dim("✗") if st == "rejected" else yellow("?"))
        projs = ", ".join((r.get("evidence") or {}).get("projects", [])) or "?"
        conf = r.get("confidence") or 0
        print(f"{mark} {dim(r.get('id', '')[:8])} {dim('(' + str(r.get('category')) + ')')} "
              f"{green(f'{conf:.2f}')}  {one_line(r.get('pattern'), 100)}")
        print(f"     {dim('· ' + projs)}")
        if r.get("proposed_rule"):
            print(f"     {cyan('→ ' + one_line(r['proposed_rule'], 100))}")
    print(dim("\naprovar/rejeitar: profile approve <id> | profile reject <id>"
              "  ·  geladeira: profile rejected | profile reopen <id>"))


def _hm_local(iso):
    """HH:MM no fuso local a partir de um timestamp ISO (armazenado em UTC)."""
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except Exception:
        return (iso or "")[11:16]


def cmd_standup(args):
    """O que você tocou hoje/ontem/semana, agrupado por projeto (cross-tool)."""
    period = args[0].lower() if args else "today"
    day0 = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    until = None
    if period in ("today", "hoje"):
        since, label = day0, "hoje"
    elif period in ("yesterday", "ontem"):
        since, until, label = day0 - timedelta(days=1), day0, "ontem"
    elif period in ("week", "semana", "7d"):
        since, label = day0 - timedelta(days=6), "últimos 7 dias"
    else:
        print("uso: standup [today|yesterday|week]"); return

    # filtra por ended_at (última atividade) — captura sessões longas que cruzam dias
    q = lambda dt: urllib.parse.quote(dt.astimezone(timezone.utc).isoformat())
    flt = f"ended_at=gte.{q(since)}"
    if until:
        flt += f"&ended_at=lt.{q(until)}"
    rows = rest(f"sessions?select=session_id,ended_at,project,tool,summary"
                f"&{flt}&order=ended_at.asc&limit=300")
    if not rows:
        print(dim(f"nada registrado ({label})")); return

    by_proj = {}
    for r in rows:
        by_proj.setdefault(r.get("project") or "?", []).append(r)
    ns, np = len(rows), len(by_proj)
    print(bold(f"standup · {label}")
          + dim(f"  ({ns} sess{'ão' if ns == 1 else 'ões'} · "
                f"{np} projeto{'' if np == 1 else 's'})") + "\n")
    for proj in sorted(by_proj, key=lambda p: -len(by_proj[p])):
        rs = by_proj[proj]
        print(f"{yellow(proj)} {dim('· ' + str(len(rs)) + (' sessão' if len(rs) == 1 else ' sessões'))}")
        for r in rs:
            print(f"  {dim(_hm_local(r.get('ended_at')))} "
                  f"{dim(r.get('session_id', '')[:8])} "
                  f"{one_line(r.get('summary') or '(sem resumo)', 96)}")
        print()


def cmd_log(args):
    """Ultimas N linhas do log de captura (default 20), colorizadas por status."""
    n = int(args[0]) if args and args[0].isdigit() else 20
    log = os.path.join(REPO, "hooks", "capture.log")
    try:
        with open(log) as f:
            tail = f.readlines()[-n:]
    except FileNotFoundError:
        print(dim("sem capture.log ainda")); return
    for ln in tail:
        ln = ln.rstrip()
        if "OK sessao" in ln:
            print(green(ln))
        elif "stdin invalido" in ln or "HTTPError" in ln or "erro ao salvar" in ln:
            print(yellow(ln))
        else:
            print(dim(ln))


def _local_main_sessions():
    """session_id -> path de toda sessao principal em ~/.claude*/projects (todos os config dirs)."""
    home = os.path.expanduser("~")
    out = {}
    for d in glob.glob(os.path.join(home, ".claude*", "projects")):
        for f in glob.glob(os.path.join(d, "*", "*.jsonl")):
            out[os.path.splitext(os.path.basename(f))[0]] = f
    return out


def _local_sessions_with_subagents():
    """session_ids que possuem pasta subagents/ localmente."""
    home, out = os.path.expanduser("~"), set()
    for sd in glob.glob(os.path.join(home, ".claude*", "projects", "*", "*", "subagents")):
        main = os.path.dirname(sd) + ".jsonl"
        if os.path.isfile(main):
            out.add(os.path.splitext(os.path.basename(main))[0])
    return out


def _bar(frac, width=22):
    n = max(0, min(width, int(round(frac * width))))
    return "█" * n + "░" * (width - n)


def _slugify(text, max_words=6):
    """Nome de diretório de skill a partir do texto do procedimento (ASCII, kebab)."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    words = re.findall(r"[a-zA-Z0-9]+", t.lower())
    # pula preâmbulos genéricos ("para", "to", "how") pro slug começar no verbo/tema
    while words and words[0] in ("para", "pra", "to", "how", "como", "the", "o", "a"):
        words.pop(0)
    return "-".join(words[:max_words]) or "procedure"


def _skill_md(row):
    """Renderiza um fato 'procedure' como SKILL.md (frontmatter + corpo + proveniência)."""
    fact = " ".join((row.get("fact") or "").split())
    # aspas no YAML: description costuma ter ':' (quebraria o frontmatter sem elas)
    desc = json.dumps(fact[:150] + ("…" if len(fact) > 150 else ""), ensure_ascii=False)
    scope = row.get("scope")
    sid = (row.get("source_session_id") or "")[:8]
    vf = (row.get("valid_from") or "")[:10]
    title = fact.split(":")[0][:80] if ":" in fact[:100] else fact[:80]
    prov = " · ".join(x for x in (
        f"projeto {scope}" if scope else "escopo global",
        f"sessão {sid}" if sid else "", f"desde {vf}" if vf else "") if x)
    return (
        "---\n"
        f"name: {_slugify(fact)}\n"
        f"description: {desc}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{fact}\n\n"
        "---\n"
        f"_Fonte: agent-memory-hub ({prov}). Gerado por `mem skills --write` a partir de um\n"
        "procedimento que funcionou numa sessão real. Este arquivo é seu: edite à vontade —\n"
        "`mem skills` nunca sobrescreve uma skill existente._\n"
    )


def cmd_extract(args):
    """Extrai facts das sessões NOVAS (incremental — só as ainda não processadas).

    O comando do dia a dia: rode quando quiser e ele processa só o que acumulou
    (facts_extracted_at IS NULL), sem resetar nada. Precisa do LLM em FACTS_LLM
    (ex: Ollama local). Com --embed, roda embed_pending no fim."""
    also_embed = "--embed" in args
    print(bold("extract") +
          dim("  — processa só as sessões novas (as ainda não extraídas). Precisa de FACTS_LLM.\n"))
    rc = subprocess.call([sys.executable, os.path.join(HERE, "extract_facts.py"), "--loop"])
    if rc != 0:
        print(yellow(f"extract_facts saiu com código {rc} — confira FACTS_LLM/Ollama"), file=sys.stderr)
        return
    if also_embed:
        print(bold("\nembeddings pendentes das sessões..."))
        subprocess.call([sys.executable, os.path.join(HERE, "embed_pending.py")])
    print(dim("\npronto."))


def cmd_reprocess(args):
    """Reseta sessões e re-extrai facts em um comando (colapsa reset + loop manual).

    Precisa do LLM configurado em FACTS_LLM (ex: Ollama local com o T7 conectado).
    Modos: 'how-to' (só candidatas a procedimento, default) | 'all' (tudo).
    Com --embed, roda embed_pending no fim (sessões novas ficam semânticas)."""
    mode = "all" if "all" in args else "how-to"
    also_embed = "--embed" in args
    print(bold(f"reprocess ({mode})") +
          dim("  — precisa do LLM em FACTS_LLM (ex: Ollama/T7). O dedup descarta o que já existe.\n"))
    rc = subprocess.call([sys.executable, os.path.join(HERE, "extract_facts.py"),
                          "--reprocess", mode, "--loop"])
    if rc != 0:
        print(yellow(f"extract_facts saiu com código {rc} — confira FACTS_LLM/Ollama"), file=sys.stderr)
        return
    if also_embed:
        print(bold("\nembeddings pendentes das sessões..."))
        subprocess.call([sys.executable, os.path.join(HERE, "embed_pending.py")])
    print(dim("\npronto. veja o que virou skill: mem skills"))


def cmd_skills(args):
    """Promove fatos 'procedure' a skills do Claude Code (roadmap item 8, fecho do ciclo).

    sessão → fato procedural (extract_facts) → SKILL.md carregável sob demanda.
    Human-gated: dry-run por default; --write grava só skills NOVAS (nunca sobrescreve
    — depois de criada, a skill é do humano).

    Filtros (curadoria — evita despejar centenas de skills e inchar o contexto):
      --scope <nome>   só procedures daquele projeto (ou 'global')
      --only <ids>     só os facts com esses ids (csv) — casado com a UI de seleção
      --top <N>        no máximo os N mais recentes (após os outros filtros)"""
    write_flag = "--write" in args
    args = [a for a in args if a != "--write"]

    def take(flag):
        if flag in args:
            i = args.index(flag)
            val = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
            return val
        return None
    scope = take("--scope")
    only = take("--only")
    top = take("--top")
    only_ids = set(x.strip() for x in only.split(",") if x.strip()) if only else None
    out_disp = args[0] if args else (os.environ.get("SKILLS_DIR") or ENV.get("SKILLS_DIR")
                                     or "~/.claude/skills")
    out_dir = os.path.expanduser(out_disp)

    rows = rest("facts?select=id,fact,scope,source_session_id,valid_from"
                "&kind=eq.procedure&valid_until=is.null&order=valid_from.desc")
    total_all = len(rows)
    if scope is not None:
        want = None if scope == "global" else scope
        rows = [r for r in rows if (r.get("scope") or None) == want]
    if only_ids is not None:
        rows = [r for r in rows if r.get("id") in only_ids]
    if top:
        rows = rows[:int(top)]
    if not rows:
        if total_all == 0:
            print("nenhum fato 'procedure' válido ainda — eles nascem da extração de facts")
            print(dim("(sessões novas que mostram um how-to funcionando geram kind=procedure)"))
        else:
            print(f"{total_all} procedures no banco, mas 0 casaram os filtros (scope/only/top)")
        return
    if scope or only_ids or top:
        print(dim(f"filtro: {len(rows)} de {total_all} procedures\n"))

    plan, seen = [], set()
    for r in rows:
        slug = _slugify(r.get("fact") or "")
        while slug in seen:
            slug += "-2"
        seen.add(slug)
        path = os.path.join(out_dir, slug, "SKILL.md")
        plan.append((slug, path, os.path.exists(path), r))

    novas = [p for p in plan if not p[2]]
    print(bold(f"{len(rows)} procedimento(s) válido(s) → {out_disp}/"))
    for slug, _path, exists, r in plan:
        mark = dim("(já existe, não toco)") if exists else green("(nova)")
        print(f"  {slug:<40} {mark}  {one_line(r.get('fact'), 70)}")

    if not write_flag:
        if novas:
            print(f"\n(dry-run) para gravar {len(novas)} skill(s): mem skills --write")
        else:
            print("\nnada novo a gravar")
        return
    n = 0
    for slug, path, exists, r in plan:
        if exists:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(_skill_md(r))
        n += 1
    print(green(f"\n{n} skill(s) gravada(s) em {out_dir}/"))
    if n:
        print(dim("o Claude Code carrega skills pessoais de ~/.claude/skills automaticamente"))


def cmd_export(args):
    """Dump da memória em Markdown legível e versionável (roadmap item 9).

    A memória continua morando no Postgres; o export é a cópia auditável — dá pra
    ler, commitar no git e diffar o que o agente sabe. Ordenação estável para
    diffs limpos entre exports."""
    out_dir = args[0] if args else "memory-export"
    os.makedirs(out_dir, exist_ok=True)

    # facts.md — fatos válidos agrupados por scope
    facts = rest("facts?select=fact,kind,scope,confidence,valid_from,source_session_id"
                 "&valid_until=is.null&order=kind,valid_from,fact")
    by_scope = {}
    for f in facts:
        by_scope.setdefault(f.get("scope") or "global", []).append(f)
    lines = ["# Fatos e preferências (memória durável)", "",
             "_Gerado por `mem export`. Fonte de verdade: tabela `facts` no Supabase;",
             "isto é a cópia legível/versionável._", ""]
    for scope in sorted(by_scope):
        lines += [f"## {scope}", ""]
        for f in by_scope[scope]:
            meta = f.get("kind", "fact")
            if f.get("confidence") is not None:
                meta += f" · conf {f['confidence']:.2f}"
            vf = (f.get("valid_from") or "")[:10]
            if vf:
                meta += f" · desde {vf}"
            sid = (f.get("source_session_id") or "")[:8]
            if sid:
                meta += f" · sessão {sid}"
            lines.append(f"- ({meta}) {one_line(f.get('fact'), 500)}")
        lines.append("")
    with open(os.path.join(out_dir, "facts.md"), "w") as fh:
        fh.write("\n".join(lines))

    # sessions.md — uma linha por sessão, agrupado por projeto
    sessions = rest("sessions?select=session_id,project,tool,started_at,summary"
                    "&order=started_at.desc&limit=2000")
    by_proj = {}
    for s in sessions:
        by_proj.setdefault(s.get("project") or "?", []).append(s)
    lines = ["# Sessões capturadas", "",
             "_Uma linha por sessão (resumo extrativo). Transcript completo: `mem show <id>`._", ""]
    for proj in sorted(by_proj):
        lines += [f"## {proj}", ""]
        for s in by_proj[proj]:
            sid = (s.get("session_id") or "")[:8]
            lines.append(f"- [{fmt_date(s.get('started_at'))} · {s.get('tool','?')} · {sid}] "
                         f"{one_line(s.get('summary'), 300)}")
        lines.append("")
    with open(os.path.join(out_dir, "sessions.md"), "w") as fh:
        fh.write("\n".join(lines))

    # profile-rules.md — padrões aprovados que viraram regra
    rules = rest("profile_patterns?select=pattern,proposed_rule,confidence,status"
                 "&status=eq.approved&order=confidence.desc")
    lines = ["# Regras de perfil aprovadas", "",
             "_Padrões cross-projeto aprovados por revisão humana (`mem profile`)._", ""]
    for r in rules:
        rule = (r.get("proposed_rule") or r.get("pattern") or "").strip()
        if rule:
            conf = r.get("confidence")
            meta = f" (conf {conf:.2f})" if conf is not None else ""
            lines.append(f"- {one_line(rule, 500)}{meta}")
    with open(os.path.join(out_dir, "profile-rules.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    print(green(f"exportado para {out_dir}/"))
    print(f"  facts.md          {len(facts)} fato(s) válidos, {len(by_scope)} scope(s)")
    print(f"  sessions.md       {len(sessions)} sessão(ões), {len(by_proj)} projeto(s)")
    print(f"  profile-rules.md  {len(rules)} regra(s) aprovadas")
    print(dim("legível, diffável, commitável — a memória continua no Postgres"))


def cmd_health(_args):
    """Reconcilia transcripts locais vs Supabase e vigia a saude da captura."""
    print(bold("agent-memory-hub · health") + "\n")

    sys.path.insert(0, os.path.join(REPO, "hooks"))
    from capture_session import parse_transcript  # reusa o mesmo parsing do hook

    local = _local_main_sessions()
    saved = {r["session_id"] for r in rest("sessions?select=session_id&limit=100000")
             if r.get("session_id")}
    # so conta como "faltando" o que tem conteudo de verdade; sessoes vazias sao ignoradas
    missing, empty = [], 0
    for sid, path in local.items():
        if sid in saved:
            continue
        if parse_transcript(path)[0]:
            missing.append(sid)
        else:
            empty += 1
    total = len(local) - empty
    frac = (total - len(missing)) / total if total else 1.0
    mark = green("✓") if not missing else yellow("⚠")
    print(f"{mark} cobertura   {_bar(frac)} {total - len(missing)}/{total} sessões locais salvas"
          + (dim(f"  ({empty} vazias ignoradas)") if empty else ""))
    if missing:
        print(dim(f"    {len(missing)} faltando → python3 scripts/backfill_sessions.py"))

    log = os.path.join(REPO, "hooks", "capture.log")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        with open(log) as f:
            recent = [ln for ln in f if ln[:32] >= cutoff]  # ISO ordena lexicograficamente
        ok = sum("OK sessao" in ln for ln in recent)
        err = sum(("stdin invalido" in ln or "HTTPError" in ln or "erro ao salvar" in ln)
                  for ln in recent)
        mark = green("✓") if err == 0 else yellow("⚠")
        print(f"{mark} captura     últimas 24h: {green(ok)} ok, {yellow(err) if err else err} erros")
        if err:
            print(dim("    veja: mem log"))
    except FileNotFoundError:
        print(dim("· captura     sem capture.log ainda"))

    subs = _local_sessions_with_subagents()
    if subs:
        with_block = {r["session_id"] for r in rest(
            "sessions?select=session_id&content=like.*" + urllib.parse.quote("--- subagent ")
            + "*&limit=100000") if r.get("session_id")}
        sub_missing = [s for s in subs if s not in with_block]
        mark = green("✓") if not sub_missing else yellow("⚠")
        print(f"{mark} subagentes  {len(subs) - len(sub_missing)}/{len(subs)} "
              f"sessões com subagentes anexados")
        if sub_missing:
            print(dim(f"    {len(sub_missing)} sem o bloco → re-capture via backfill"))

    print(dim("\natalhos: search <termo> · recent · stats · profile · `DIGEST.md` (resumo)"))


# (comando, args, descrição) — fonte única do help; agrupado por intenção de uso
HELP_SECTIONS = (
    ("consulta", (
        ("stats", "", "visão geral: sessões, fatos, por ferramenta"),
        ("recent", "[N]", "últimas N sessões, cross-projeto (default 10)"),
        ("search", "[--project P] [--rerank] <query>", "busca híbrida (keyword + semântica) em todo o histórico"),
        ("show", "<id-prefixo>", "transcript completo de uma sessão (o id de 8 chars serve)"),
        ("facts", "[projeto]", "fatos duráveis válidos (globais + do projeto)"),
        ("standup", "[today|yesterday|week]", "o que você tocou, por projeto (default today)"),
    )),
    ("curadoria — portões humanos (dry-run por default)", (
        ("profile", "[approve|reject|reopen <id> | rejected]", "revisa padrões detectados → regras pro CLAUDE.md (apply_profile_rules.py --write grava)"),
        ("skills", "[dir] [--scope S|--only ids|--top N] [--write]", "procedures → SKILL.md; filtre pra curar (evita inchar contexto)"),
        ("export", "[dir]", "dump Markdown versionável: fatos, sessões, regras (default memory-export/)"),
    )),
    ("operação", (
        ("health", "", "reconcilia transcripts locais ↔ Supabase e vigia erro de captura"),
        ("log", "[N]", "últimas N linhas do log de captura (default 15)"),
        ("extract", "[--embed]", "extrai facts só das sessões novas (incremental) — o comando do dia a dia"),
        ("reprocess", "[how-to|all] [--embed]", "reseta e re-extrai TUDO (pesado; só pra troca de modelo)"),
    )),
)

# jobs que não são subcomandos (rodam direto, feitos pra cron) — listados no help
HELP_SCRIPTS = (
    ("extract_facts.py", "extrai fatos duráveis das sessões novas (FACTS_LLM: ollama/CLI/API)"),
    ("defrag_facts.py [--dry-run]", "manutenção: superseda duplicatas, invalida fatos stale (não-destrutivo)"),
    ("embed_pending.py", "gera embeddings das sessões que faltam (cron)"),
    ("enforce_rules.py [--write]", "regras aprovadas mecanizáveis → guard PreToolUse que bloqueia violação"),
    ("apply_profile_rules.py [--write]", "regras aprovadas → ~/.claude/profile-rules.md (importado pelo CLAUDE.md)"),
    ("eval_recall.py --auto 30", "mede a qualidade do recall (hit@k / MRR); --spread = amostra representativa"),
    ("weekly_digest.py", "resumo de 7 dias cross-projeto (sem LLM)"),
    ("backup.py", "pg_dump diário pra .sql portável"),
)


def cmd_help(_args):
    print(bold("agent-memory-hub · mem") + " — memória compartilhada dos teus agentes\n")
    print("uso: mem <comando> [args]        (sem argumentos: modo interativo)\n")
    for section, cmds in HELP_SECTIONS:
        print(bold(section))
        for name, args, desc in cmds:
            left = f"{name} {args}".strip()
            print(f"  {cyan(f'{left:<44}')} {desc}")
        print()
    print(bold("jobs (python3 scripts/<nome>) — semi-automáticos, feitos pra cron"))
    for name, desc in HELP_SCRIPTS:
        print(f"  {dim(f'{name:<44}')} {desc}")
    print()
    print(dim("automático via hooks (você nunca chama): captura com redação de secrets a cada"))
    print(dim("Stop/SessionEnd; recall com orçamento de tokens a cada SessionStart."))
    print(dim("detalhes: README.md · o que é automático vs. manual"))


COMMANDS = {"stats": cmd_stats, "recent": cmd_recent, "search": cmd_search,
            "facts": cmd_facts, "show": cmd_show, "profile": cmd_profile,
            "health": cmd_health, "log": cmd_log, "standup": cmd_standup,
            "export": cmd_export, "skills": cmd_skills,
            "extract": cmd_extract, "reprocess": cmd_reprocess,
            "help": cmd_help}


def repl():
    print(bold("memory console") + dim("  (stats | recent [N] | search <q> | facts [proj] | show <id> | profile | health | log [N] | standup [period] | quit)"))
    while True:
        try:
            line = input(cyan("memory> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return
        parts = line.split()
        fn = COMMANDS.get(parts[0])
        if not fn:
            print(dim("comandos: " + ", ".join(COMMANDS) + "  (help explica cada um)")); continue
        try:
            fn(parts[1:])
        except Exception as e:
            print(f"erro: {type(e).__name__}: {e}", file=sys.stderr)


def main(argv):
    if not (URL and KEY):
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes no .env", file=sys.stderr)
        return 1
    if not argv:
        repl(); return 0
    if argv[0] in ("--help", "-h"):
        cmd_help([]); return 0
    fn = COMMANDS.get(argv[0])
    if not fn:
        print(f"comando desconhecido: {argv[0]}\n", file=sys.stderr)
        cmd_help([]); return 2
    fn(argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
