#!/usr/bin/env python3
"""
agent-memory-hub — re-key existing sessions and facts with the canonical project key.

Before hooks/project_key.py, the project was the basename of the cwd: clones of one repo
were split into several projects and sessions opened in `/` or `~/Development` became
fake projects whose facts leaked into recall. This recomputes:

  sessions.project  <- project_key(metadata.cwd)            (None when not a project)
  facts.scope       <- fact_scope(...)                      ("_none" instead of a fake project)

A fact whose scope differed from its session's old project was scoped by the LLM; that
scope is kept when it names a known project (e.g. "swiftlang/swift" -> "swift").

Usage:
  python3 scripts/migrate_project_keys.py            # dry-run: prints the old -> new mapping
  python3 scripts/migrate_project_keys.py --apply    # backs up old values, then updates

The backup (<repo>/backups/project-keys-<timestamp>.json) holds every id with its old value,
so `--restore <file>` puts them back.
Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, WORKSPACE_ROOTS, PROJECT_ALIASES.
"""
import glob
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
sys.path.insert(0, os.path.join(REPO, "hooks"))
from project_key import fact_scope, normalise_key, parse_aliases, project_key  # noqa: E402

PAGE = 1000
CHUNK = 100


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
    return {**env, **os.environ}


def http(url, key, method="GET", body=None):
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Prefer": "return=minimal"}
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers,
                                                       method=method), timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def fetch_all(base, key, table, select):
    rows, offset = [], 0
    while True:
        page = http(f"{base}/rest/v1/{table}?select={select}&order=id&limit={PAGE}&offset={offset}", key)
        rows += page
        if len(page) < PAGE:
            return rows
        offset += PAGE


def old_name_map(sessions, resolved):
    """Old basename -> new key, when every resolvable session with that name agrees.

    Lets a session whose cwd no longer exists (e.g. an old copy in ~/Downloads) and an
    LLM scope that used the old name ("mysite") land on the new key ("my-site")."""
    targets = defaultdict(set)
    for s in sessions:
        if s.get("project") and resolved[s["id"]]:
            targets[normalise_key(s["project"])].add(resolved[s["id"]])
    return {old: next(iter(new)) for old, new in targets.items() if len(new) == 1}


def plan(env, sessions, facts):
    resolved = {s["id"]: project_key((s.get("metadata") or {}).get("cwd"), env) for s in sessions}
    renamed = old_name_map(sessions, resolved)
    new_project = {s["id"]: resolved[s["id"]] or renamed.get(normalise_key(s.get("project")))
                   for s in sessions}
    aliases = parse_aliases(env.get("PROJECT_ALIASES")) + [
        (glob.escape(old), new) for old, new in renamed.items() if old != new]
    known = {p for p in new_project.values() if p}
    by_sid = {s["session_id"]: s for s in sessions}

    session_changes = {s["id"]: (s.get("project"), new_project[s["id"]]) for s in sessions
                       if s.get("project") != new_project[s["id"]]}
    fact_changes = {}
    for f in facts:
        s = by_sid.get(f.get("source_session_id"))
        if not s:
            continue  # orphan fact: leave untouched
        old_scope = f.get("scope")
        llm_scope = old_scope if old_scope != s.get("project") else None
        new_scope = fact_scope(llm_scope, new_project[s["id"]], known, aliases)
        if new_scope != old_scope:
            fact_changes[f["id"]] = (old_scope, new_scope)
    return session_changes, fact_changes


def patch_grouped(base, key, table, column, changes):
    groups = defaultdict(list)
    for row_id, (_, new) in changes.items():
        groups[new].append(row_id)
    for new, ids in groups.items():
        for i in range(0, len(ids), CHUNK):
            chunk = ",".join(ids[i:i + CHUNK])
            http(f"{base}/rest/v1/{table}?id=in.({chunk})", key, "PATCH", {column: new})


def summarise(title, changes):
    print(f"\n{title}: {len(changes)} a alterar")
    for (old, new), n in Counter(changes.values()).most_common():
        print(f"  {n:4d}  {old!s:32} -> {new}")


def main(argv):
    env = load_env(ENV_PATH)
    base, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
    if not base or not key:
        print("ERRO: faltam SUPABASE_URL/SUPABASE_SECRET_KEY", file=sys.stderr)
        return 1

    if "--restore" in argv:
        i = argv.index("--restore")
        if i + 1 >= len(argv):
            print("uso: --restore <arquivo de backup>", file=sys.stderr)
            return 2
        with open(argv[i + 1]) as f:
            backup = json.load(f)
        patch_grouped(base, key, "sessions", "project",
                      {k: (None, v) for k, v in backup["sessions"].items()})
        patch_grouped(base, key, "facts", "scope",
                      {k: (None, v) for k, v in backup["facts"].items()})
        print(f"restaurado: {len(backup['sessions'])} sessões, {len(backup['facts'])} fatos")
        return 0

    sessions = fetch_all(base, key, "sessions", "id,session_id,project,metadata")
    facts = fetch_all(base, key, "facts", "id,scope,source_session_id")
    session_changes, fact_changes = plan(env, sessions, facts)
    summarise("sessões", session_changes)
    summarise("fatos", fact_changes)

    if "--apply" not in argv:
        print("\n(dry-run) para aplicar: python3 scripts/migrate_project_keys.py --apply")
        return 0

    os.makedirs(os.path.join(REPO, "backups"), exist_ok=True)
    path = os.path.join(REPO, "backups", f"project-keys-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(path, "w") as f:
        json.dump({"sessions": {k: old for k, (old, _) in session_changes.items()},
                   "facts": {k: old for k, (old, _) in fact_changes.items()}}, f)
    print(f"\nbackup dos valores antigos: {path}")

    patch_grouped(base, key, "sessions", "project", session_changes)
    patch_grouped(base, key, "facts", "scope", fact_changes)
    print(f"aplicado. para desfazer: python3 scripts/migrate_project_keys.py --restore {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
