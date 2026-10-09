#!/usr/bin/env python3
"""
agent-memory-hub — block commits that mention names that must never reach the repo.

The hub runs next to work for employers and clients, and their names leak easily into
examples, tests and docs. The terms live ONLY in .env (gitignored), so the list itself is
never committed:

  COMMIT_DENYLIST="acme,globex"      # comma-separated, case-insensitive substrings

Modes (wired by .githooks/, enabled per clone by scripts/setup.sh):
  --staged          pre-commit: added lines and paths of the staged diff
  --message FILE    commit-msg: the commit message
  --all             every tracked file (one-off audit)

Exit 1 on any hit. With no COMMIT_DENYLIST it warns and lets the commit through, so a
clone without the list still works (and says so loudly).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")


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


def parse_terms(raw):
    """'Acme, globex,,' -> ['acme', 'globex'] (lowercased, blanks dropped, deduped)."""
    out = []
    for t in (raw or "").split(","):
        t = t.strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def find_hits(items, terms):
    """items: [(where, text)] -> [(where, term)] for every term found in text."""
    hits = []
    for where, text in items:
        low = text.lower()
        for t in terms:
            if t in low:
                hits.append((where, t))
    return hits


def staged_items(diff_text, paths):
    """Added lines of a `git diff --cached -U0` as (path:line, text), plus the paths."""
    items = [(f"{p} (path)", p) for p in paths]
    path, line_no = None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            # @@ -a,b +c,d @@  -> next added line is number c
            plus = raw.split("+", 1)[1].split(" ", 1)[0]
            line_no = int(plus.split(",")[0])
        elif raw.startswith("+") and path:
            items.append((f"{path}:{line_no}", raw[1:]))
            line_no += 1
    return items


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def main(argv):
    raw = os.environ.get("COMMIT_DENYLIST") or load_env(ENV_PATH).get("COMMIT_DENYLIST")
    terms = parse_terms(raw)
    if not terms:
        print("check_denylist: COMMIT_DENYLIST vazio no .env; commit NÃO verificado", file=sys.stderr)
        return 0

    if argv[:1] == ["--staged"]:
        diff = _git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff")
        paths = [p for p in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p]
        items = staged_items(diff, paths)
    elif argv[:1] == ["--message"] and len(argv) == 2:
        with open(argv[1]) as f:
            items = [("mensagem do commit", line) for line in f if not line.startswith("#")]
    elif argv[:1] == ["--all"]:
        items = [(f"{p} (path)", p) for p in _git("ls-files").splitlines()]
        for p in _git("ls-files").splitlines():
            try:
                with open(os.path.join(REPO, p), encoding="utf-8") as f:
                    items += [(f"{p}:{i}", line) for i, line in enumerate(f, 1)]
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
    else:
        print(__doc__, file=sys.stderr)
        return 2

    hits = find_hits(items, terms)
    if not hits:
        return 0
    print("BLOQUEADO: termos da COMMIT_DENYLIST encontrados:", file=sys.stderr)
    for where, term in hits:
        print(f"  {where}  ->  '{term}'", file=sys.stderr)
    print("Troque por um nome genérico (ex.: acme, shop-app). Não use --no-verify para isso.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
