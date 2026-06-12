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

Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY (for search).
"""
import json
import os
import sys
import urllib.parse
import urllib.request

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


COMMANDS = {"stats": cmd_stats, "recent": cmd_recent, "search": cmd_search,
            "facts": cmd_facts, "show": cmd_show}


def repl():
    print(bold("memory console") + dim("  (stats | recent [N] | search <q> | facts [proj] | show <id> | quit)"))
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
