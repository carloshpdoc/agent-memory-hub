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

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY (for search).
"""
import glob
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")

_TTY = sys.stdout.isatty()
def c(s, code):
    return f"\033[{code}m{s}\033[0m" if _TTY else s
def bold(s): return c(s, "1")
def dim(s): return c(s, "2")
def cyan(s): return c(s, "36")
def green(s): return c(s, "32")
def yellow(s): return c(s, "33")


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


ENV = load_env(ENV_PATH)
URL = os.environ.get("SUPABASE_URL") or ENV.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SECRET_KEY") or ENV.get("SUPABASE_SECRET_KEY")
EK = os.environ.get("EMBED_KEY") or ENV.get("EMBED_KEY")
H = {"apikey": KEY or "", "Authorization": f"Bearer {KEY or ''}", "Content-Type": "application/json"}


def rest(path):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", headers=H)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def rpc(name, body):
    req = urllib.request.Request(f"{URL}/rest/v1/rpc/{name}",
                                 data=json.dumps(body).encode(), method="POST", headers=H)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def write(path, body, method="PATCH"):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}", data=json.dumps(body).encode(),
                                 method=method, headers={**H, "Prefer": "return=minimal"})
    urllib.request.urlopen(req, timeout=20).read()


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
        emb = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{URL}/functions/v1/embed", data=json.dumps({"text": query}).encode(),
            method="POST", headers={"x-embed-key": EK, "Content-Type": "application/json"}),
            timeout=20).read())["embedding"]
        rows = rpc("hybrid_search", {"query_text": query, "query_embedding": emb,
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


COMMANDS = {"stats": cmd_stats, "recent": cmd_recent, "search": cmd_search,
            "facts": cmd_facts, "show": cmd_show, "profile": cmd_profile,
            "health": cmd_health, "log": cmd_log}


def repl():
    print(bold("memory console") + dim("  (stats | recent [N] | search <q> | facts [proj] | show <id> | profile | health | log [N] | quit)"))
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
            print(dim("comandos: " + ", ".join(COMMANDS))); continue
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
    fn = COMMANDS.get(argv[0])
    if not fn:
        print(__doc__); return 2
    fn(argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
