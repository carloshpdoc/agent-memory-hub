#!/usr/bin/env python3
"""
agent-memory-hub — verify facts against the current code (nightly, LLM-free).

A fact that cites a repo file ("the filter screen is Filters/.../FilterSelection.swift")
is only true while the file exists. For every valid fact whose project has local clones
(under WORKSPACE_ROOTS), this checks each cited file — tracked by git or present on disk —
and stores the outcome in facts.code_check. Recall then marks the fact with a warning
and lowers its priority. Nothing is deleted or invalidated; a file that reappears clears it.

Every local clone of the project counts: a file is missing only if no clone has it.
Only file references with a code extension and at least one '/' are checked: bare names,
directories, branch names and owner/repo slugs proved too noisy. Facts of projects without
a local clone are skipped (unverifiable here). A clone that is behind its remote can flag
a file that only exists upstream: keep clones pulled.

Usage:  python3 scripts/verify_facts.py [--dry-run]
Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, WORKSPACE_ROOTS.
"""
import glob
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from memory_client import ENV, rest_all, write  # noqa: E402
from project_key import project_key  # noqa: E402  (memory_client já pôs hooks/ no path)

CODE_EXT = ("py|ts|tsx|js|jsx|mjs|swift|kt|kts|java|m|mm|c|cpp|go|rb|rs|md|json|ya?ml|toml|sh|"
            "sql|html|css|scss|plist|xcconfig|gradle|scad|gd|tscn|cs|php|vue|svelte|ipynb")
FILE_REF_RE = re.compile(r"(?<![\w/.-])((?:[\w@.\[\]-]+/)+[\w@.\[\]-]+\.(?:" + CODE_EXT + r"))(?![\w/-])")


def file_refs(text):
    """Caminhos de arquivo relativos ao repo citados no texto (com '/' e extensão de código)."""
    out = []
    for r in FILE_REF_RE.findall(text or ""):
        if r.startswith(("./", "../")) or r in out:
            continue
        out.append(r)
    return out


def local_clones(env):
    """{project_key: [dirs]} dos repos git direto sob cada WORKSPACE_ROOT. Todos os clones
    do projeto contam: o arquivo existe se estiver em QUALQUER um (clones podem estar em
    branches ou pontos diferentes do histórico)."""
    found = {}
    for root in (env.get("WORKSPACE_ROOTS") or "~/Development").split(":"):
        for d in sorted(glob.glob(os.path.join(os.path.expanduser(root), "*"))):
            if os.path.isdir(os.path.join(d, ".git")):
                key = project_key(d, env)
                if key:
                    found.setdefault(key, []).append(d)
    return found


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=30)


def _present(root, ref, tracked):
    """Existe no disco, ou (como sufixo) entre os versionados, ou é ignorado pelo git
    (não verificável -> não acusa)."""
    return (os.path.exists(os.path.join(root, ref))
            or any(p == ref or p.endswith("/" + ref) for p in tracked)
            or _git(root, "check-ignore", "-q", ref).returncode == 0)


def missing_refs(clones, refs):
    """Refs ausentes de TODOS os clones. clones: [(root, tracked_files)]."""
    return [ref for ref in refs if not any(_present(root, ref, tr) for root, tr in clones)]


def check_value(head, refs, missing, now):
    return {"checked_at": now, "head": head, "refs": len(refs), "missing": missing}


def changed(old, new):
    """Só grava quando muda o que importa (evita um PATCH por fato toda noite)."""
    old = old or {}
    return ((old.get("missing") or []) != new["missing"] or old.get("refs") != new["refs"]
            or old.get("head") != new["head"])


def main(argv):
    dry = "--dry-run" in argv
    env = {**ENV, **os.environ}
    clones = local_clones(env)
    facts = rest_all("facts?select=id,fact,scope,code_check&valid_until=is.null")
    now = datetime.now(timezone.utc).isoformat()
    cache, checked, flagged, writes = {}, 0, 0, 0
    for f in facts:
        refs, roots = file_refs(f.get("fact")), clones.get(f.get("scope"))
        if not refs or not roots:
            continue
        for root in roots:
            if root not in cache:
                cache[root] = (_git(root, "rev-parse", "--short", "HEAD").stdout.strip(),
                               _git(root, "ls-files").stdout.splitlines())
        miss = missing_refs([(root, cache[root][1]) for root in roots], refs)
        checked += 1
        flagged += bool(miss)
        new = check_value([cache[root][0] for root in roots], refs, miss, now)
        if miss and dry:
            print(f"  ⚠ [{f['scope']}] {', '.join(miss)}  — {' '.join(f['fact'].split())[:80]}")
        if not dry and changed(f.get("code_check"), new):
            write(f"facts?id=eq.{f['id']}", {"code_check": new})
            writes += 1
    print(f"{'(dry-run) ' if dry else ''}{checked} fato(s) verificado(s) em {len(cache)} repo(s); "
          f"{flagged} citam arquivo inexistente; {writes} atualizado(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
