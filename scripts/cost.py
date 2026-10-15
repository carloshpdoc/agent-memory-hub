#!/usr/bin/env python3
"""
agent-memory-hub — `mem cost`: what each task cost, across Claude Code and Codex.

A task is the project plus its git branch when the work happened on a feature branch, so a
fix done partly in Claude Code and partly in Codex adds up into one line. On a default
branch (main, dev...) or outside git, each session is its own task. Cost is the
API-equivalent of the tokens stored in sessions.usage (hooks/usage.py: public prices); on a
subscription it is not what you pay, it is the unit to compare tasks, tools and models.
Cursor does not record tokens, so its sessions are counted as "sem dados".

  python3 scripts/cost.py [--days 30] [--by task|project|tool|model] [--project P] [--top 15]
"""
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
import memory_client as mc  # noqa: E402
from usage import cost_usd, tokens  # noqa: E402

DEFAULT_BRANCHES = {"main", "master", "dev", "develop", "trunk", "HEAD"}
GROUPINGS = {"task": "tarefa", "project": "projeto", "tool": "ferramenta", "model": "modelo"}
ID_LEN = 8


def task_key(row):
    """(chave, rótulo) da tarefa de uma sessão."""
    project = row.get("project") or "?"
    branch = row.get("branch")
    if branch and branch not in DEFAULT_BRANCHES:
        return f"{project}\0{branch}", f"{project} · {branch}"
    summary = mc._line(row.get("summary"), 60)
    return row["session_id"], f"{project} · [{row['session_id'][:ID_LEN]}] {summary}"


def aggregate(rows, by="task"):
    """Linhas agregadas (dicts), da mais cara para a mais barata, e a cobertura."""
    groups, cover = {}, {"with_usage": 0, "cursor": 0, "no_transcript": 0}
    unpriced = set()
    for r in rows:
        u = r.get("usage")
        if not u:
            cover["cursor" if r.get("tool") == "cursor" else "no_transcript"] += 1
            continue
        cover["with_usage"] += 1
        if by == "model":
            parts = [(m, m, {"models": {m: t}}) for m, t in u["models"].items()]
        else:
            key, label = (task_key(r) if by == "task" else
                          ((r.get("project") or "?"),) * 2 if by == "project" else (r.get("tool"),) * 2)
            parts = [(key, label, u)]
        for key, label, part in parts:
            cost, missing = cost_usd(part)
            unpriced.update(missing)
            g = groups.setdefault(key, {"label": label, "cost": 0.0, "tokens": 0, "sessions": set(),
                                        "tools": set(), "first": r.get("started_at"), "last": r.get("started_at")})
            g["cost"] += cost
            g["tokens"] += tokens(part)
            g["sessions"].add(r["session_id"])
            g["tools"].add(r.get("tool"))
            g["first"] = min(filter(None, (g["first"], r.get("started_at"))), default=None)
            g["last"] = max(filter(None, (g["last"], r.get("started_at"))), default=None)
    out = sorted(groups.values(), key=lambda g: -g["cost"])
    return out, cover, sorted(unpriced)


def fetch(days, project=None):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    flt = f"&project=eq.{urllib.parse.quote(project)}" if project else ""
    return mc.rest_all("sessions?select=session_id,project,tool,started_at,summary,usage,"
                       f"branch:metadata->git->>branch&started_at=gte.{urllib.parse.quote(since)}{flt}")


def fmt_tokens(n):
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def report(groups, cover, unpriced, days, by, top):
    total = sum(g["cost"] for g in groups)
    n = sum(cover.values())
    print(f"últimos {days} dias · US$ {total:,.2f} equivalente de API · {n} sessões")
    gaps = [f"{v} {k}" for k, v in (("do Cursor (não registra tokens)", cover["cursor"]),
                                     ("sem uso registrado (rode `mem import` na máquina de origem; transcripts "
                                      "já apagados não têm como)", cover["no_transcript"])) if v]
    print(f"  com uso registrado: {cover['with_usage']}" + (f"; fora da conta: {', '.join(gaps)}" if gaps else ""))
    if unpriced:
        print(f"  sem preço público conhecido (fora do total): {', '.join(unpriced)}")
    print()
    print(f"  {'US$':>9} {'tokens':>7} {'sessões':>7}  {'ferramentas':<18} {GROUPINGS[by]}")
    for g in groups[:top]:
        tools = ",".join(sorted(t for t in g["tools"] if t))
        print(f"  {g['cost']:9,.2f} {fmt_tokens(g['tokens']):>7} {len(g['sessions']):>7}  {tools:<18} {g['label']}")
    if len(groups) > top:
        rest = sum(g["cost"] for g in groups[top:])
        print(f"  {rest:9,.2f} {'':>7} {'':>7}  {'':<18} (+{len(groups) - top} outros)")


def parse_args(argv):
    opts, i = {"days": 30, "by": "task", "project": None, "top": 15}, 0
    while i < len(argv):
        a = argv[i]
        if a not in ("--days", "--by", "--project", "--top") or i + 1 >= len(argv):
            raise ValueError(f"argumento inválido: {a}")
        key, val = a.lstrip("-"), argv[i + 1]
        opts[key] = int(val) if key in ("days", "top") else val
        i += 2
    if opts["by"] not in GROUPINGS:
        raise ValueError(f"--by precisa ser um de: {', '.join(GROUPINGS)}")
    if opts["days"] < 1 or opts["top"] < 1:
        raise ValueError("--days e --top precisam ser >= 1")
    return opts


def main(argv):
    try:
        o = parse_args(argv)
    except ValueError as e:
        print(f"erro: {e}\nuso: mem cost [--days 30] [--by task|project|tool|model] [--project P] [--top 15]",
              file=sys.stderr)
        return 2
    rows = fetch(o["days"], o["project"])
    if not rows:
        print(f"nenhuma sessão nos últimos {o['days']} dias")
        return 1
    report(*aggregate(rows, o["by"]), o["days"], o["by"], o["top"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
