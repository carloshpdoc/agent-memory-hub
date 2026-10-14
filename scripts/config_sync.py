#!/usr/bin/env python3
"""
agent-memory-hub — sync skills across machines, installing only what the dashboard approved.

Phase 1 of config sync (sql/11-config-sync.sql). Each run, on this machine:
  1. publish: every skill in ~/.claude/skills goes to config_items (new or changed). Having
     a skill locally counts as approval for this machine.
  2. register: items this machine has never seen get a 'pending' assignment, so they show
     up in the dashboard's Sync tab for approval.
  3. install: items 'approved' for this hostname are written to ~/.claude/skills/<name>
     (Claude Code; Cursor reads it too) and linked from ~/.agents/skills/<name> (Codex;
     one copy, no duplicate in Cursor). Anything overwritten is backed up first.
  4. reject: an item turned 'rejected' after the sync installed it is moved to backups.

Edited on two machines since the last sync: the newer one wins and the other version is
backed up, never lost. A skill that is a symlink (managed by its own git repo) is
published but never overwritten. Names and file paths from the database are validated
so nothing can be written outside the skills folder.

Usage:  python3 scripts/config_sync.py [--dry-run]     (also: mem sync, nightly, mem import)
Config (env or ../.env): SUPABASE_URL, SUPABASE_SECRET_KEY, SKILLS_DIR, AGENTS_SKILLS_DIR.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from memory_client import ENV, URL, KEY, rest_all  # noqa: E402

BACKUP_DIR = os.path.join(REPO, "backups", "skills")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKIP = {"synced"}                    # skills sincronizadas pelo claude.ai: geridas por ele
MAX_FILE = 256 * 1024


def _env(k, default):
    return os.path.expanduser(os.environ.get(k) or ENV.get(k) or default)


SKILLS_DIR = _env("SKILLS_DIR", "~/.claude/skills")
AGENTS_DIR = _env("AGENTS_SKILLS_DIR", "~/.agents/skills")


def extra_claude_dirs():
    """Pastas de skills de OUTRAS configs do Claude Code (CLAUDE_CONFIG_DIR, ex. ~/.claude-work
    de uma 2a conta). Cada config lê só a própria pasta, então a skill instalada também
    precisa de um link lá. CLAUDE_EXTRA_SKILL_DIRS (separadas por ':') sobrepõe a detecção."""
    raw = os.environ.get("CLAUDE_EXTRA_SKILL_DIRS")
    if raw is None:
        raw = ENV.get("CLAUDE_EXTRA_SKILL_DIRS")
    if raw is not None:                     # "" desliga: nenhuma pasta extra
        dirs = [os.path.expanduser(d) for d in raw.split(":") if d.strip()]
    else:
        home = os.path.expanduser("~")
        dirs = [os.path.join(home, d, "skills") for d in sorted(os.listdir(home))
                if d.startswith(".claude-") and os.path.isfile(os.path.join(home, d, "settings.json"))]
    primary = os.path.realpath(SKILLS_DIR)
    return [d for d in dirs if os.path.realpath(d) != primary]


def link_dirs():
    """Onde cada skill sincronizada ganha um link para a cópia em SKILLS_DIR."""
    return [AGENTS_DIR] + extra_claude_dirs()


# ---- arquivos de uma skill -------------------------------------------------------------
def read_skill(path):
    """{relpath: texto} da skill (só texto, sem .git), ou None se não for skill válida."""
    root = os.path.realpath(path)
    if not os.path.isfile(os.path.join(root, "SKILL.md")):
        return None
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            data = open(full, "rb").read()
            if b"\0" in data[:4096] or len(data) > MAX_FILE:
                continue                       # binário/grande: fase 1 só sincroniza texto
            files[os.path.relpath(full, root)] = data.decode("utf-8", errors="replace")
    return files


def files_hash(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_relpath(rel):
    """Caminho relativo seguro (sem absoluto, sem '..')."""
    norm = os.path.normpath(rel)
    return not (os.path.isabs(rel) or norm.startswith("..") or "/../" in f"/{norm}/")


def local_skills():
    """Skills desta máquina: SKILLS_DIR e, para nomes que só existem lá, as pastas das outras
    configs do Claude (ex. uma skill criada só na conta de trabalho). home=False marca
    essas: são publicadas, mas não espalhadas para as outras pastas desta máquina."""
    out = {}
    for base, home in [(SKILLS_DIR, True)] + [(d, False) for d in extra_claude_dirs()]:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if (name in out or name in SKIP or name.startswith(".") or not NAME_RE.match(name)
                    or not os.path.isdir(path)):
                continue
            files = read_skill(path)
            if files:
                out[name] = {"files": files, "hash": files_hash(files), "link": os.path.islink(path),
                             "home": home, "mtime": max(os.path.getmtime(
                                 os.path.join(os.path.realpath(path), f)) for f in files)}
    return out


# ---- instalação ------------------------------------------------------------------------
def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path, label):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}-{label}-{_stamp()}")
    shutil.move(path, dest)
    return dest


def write_skill(name, files, dry=False):
    """Instala em SKILLS_DIR/name (backup do que existir) e liga AGENTS_DIR/name -> ela."""
    if not NAME_RE.match(name) or not all(safe_relpath(r) for r in files):
        raise ValueError(f"nome ou caminho inseguro em {name!r}")
    target = os.path.join(SKILLS_DIR, name)
    if os.path.islink(target):
        return "link (gerida por repo; não sobrescrita)"
    if dry:
        return "instalaria"
    tmp = f"{target}.sync-tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    for rel, text in files.items():
        full = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    if os.path.exists(target):
        backup(target, "antes-do-sync")
    os.makedirs(SKILLS_DIR, exist_ok=True)
    os.rename(tmp, target)
    link_everywhere(name)
    return "instalada"


def link_into(base, name):
    """base/<name> -> SKILLS_DIR/<name>. Uma cópia solta antiga ali vai para backup e vira link
    (senão aquela ferramenta/conta usaria uma versão defasada). Links existentes ficam."""
    link = os.path.join(base, name)
    if os.path.islink(link):
        return None
    replaced = None
    if os.path.isdir(link):
        replaced = backup(link, "copia-antiga")
    elif os.path.lexists(link):
        return None
    os.makedirs(base, exist_ok=True)
    os.symlink(os.path.join(SKILLS_DIR, name), link)
    return replaced


def link_everywhere(name):
    """Liga a skill em ~/.agents/skills (Codex) e nas pastas das outras configs do Claude.
    Retorna as pastas onde uma cópia antiga foi trocada por link."""
    return [base for base in link_dirs() if link_into(base, name)]


# ---- Supabase (REST) -------------------------------------------------------------------
def _req(method, path, body=None, prefer="return=minimal"):
    req = urllib.request.Request(
        f"{URL}/rest/v1/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json", "Prefer": prefer})
    raw = urllib.request.urlopen(req, timeout=30).read()
    return json.loads(raw) if raw else None


def set_assignment(item_id, machine, **fields):
    _req("POST", "config_assignments?on_conflict=item_id,machine",
         {"item_id": item_id, "machine": machine, **fields},
         prefer="resolution=merge-duplicates,return=minimal")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---- decisão por skill (puro, testável) ------------------------------------------------
def decide(local, item, assignment):
    """O que fazer com uma skill. local/item/assignment podem ser None.
    Retorna 'publish' | 'update' | 'install' | 'conflict-local' | 'conflict-remote' |
    'uninstall' | 'noop'."""
    status = (assignment or {}).get("status")
    base = (assignment or {}).get("installed_hash")
    if status == "rejected":
        return "uninstall" if (base and local and not local.get("link")) else "noop"
    if item is None:
        return "publish" if local else "noop"
    if local is None:
        return "install" if status == "approved" else "noop"
    if local["hash"] == item["hash"]:
        return "noop"
    local_changed, remote_changed = local["hash"] != base, item["hash"] != base
    if local_changed and not remote_changed:
        return "update"
    if remote_changed and not local_changed:
        return "install" if status == "approved" else "noop"
    # as duas mudaram (ou nunca sincronizou): a mais recente vence
    remote_ts = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
    return "conflict-local" if local["mtime"] >= remote_ts else "conflict-remote"


def main(argv):
    dry = "--dry-run" in argv
    if not URL or not KEY:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    machine = socket.gethostname()
    local = local_skills()
    items = {i["name"]: i for i in rest_all("config_items?select=id,name,hash,files,updated_at,source_machine&kind=eq.skill")}
    mine = {a["item_id"]: a for a in rest_all(
        f"config_assignments?select=item_id,status,installed_hash&machine=eq.{urllib.parse.quote(machine)}")}
    report = {}

    def note(action, name):
        report.setdefault(action, []).append(name)

    for name in sorted(set(local) | set(items)):
        loc, item = local.get(name), items.get(name)
        a = mine.get(item["id"]) if item else None
        action = decide(loc, item, a)
        if action in ("publish", "update", "conflict-local"):
            if action == "conflict-local" and not dry:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                with open(os.path.join(BACKUP_DIR, f"{name}-remota-{_stamp()}.json"), "w") as f:
                    json.dump(item["files"], f, ensure_ascii=False, indent=1)
            if not dry:
                body = {"kind": "skill", "name": name, "files": loc["files"], "hash": loc["hash"],
                        "source_machine": machine, "updated_at": now_iso()}
                if item:
                    _req("PATCH", f"config_items?id=eq.{item['id']}", body)
                    item_id = item["id"]
                else:
                    item_id = _req("POST", "config_items", body, prefer="return=representation")[0]["id"]
                set_assignment(item_id, machine, status="approved", installed_hash=loc["hash"],
                               decided_at=now_iso(), installed_at=now_iso())
            note({"publish": "publicada", "update": "atualizada no hub",
                  "conflict-local": "conflito: local venceu (remota em backup)"}[action], name)
        elif action in ("install", "conflict-remote"):
            result = write_skill(name, item["files"], dry)
            if not dry and not result.startswith("link"):
                set_assignment(item["id"], machine, status="approved", installed_hash=item["hash"],
                               installed_at=now_iso())
            note("conflito: hub venceu (local em backup)" if action == "conflict-remote" else result, name)
        elif action == "uninstall":
            if not dry:
                target = os.path.join(SKILLS_DIR, name)
                if os.path.isdir(target) and not os.path.islink(target):
                    backup(target, "rejeitada")
                for base in link_dirs():
                    link = os.path.join(base, name)
                    if os.path.islink(link):
                        os.unlink(link)
                set_assignment(item["id"], machine, installed_hash=None)
            note("removida (rejeitada; em backup)", name)
        else:   # noop: só registra a máquina e garante o link do Codex
            if item and a is None:
                if not dry:
                    if loc:   # mesma versão já existe aqui: estar aqui é a aprovação
                        set_assignment(item["id"], machine, status="approved", installed_hash=loc["hash"],
                                       decided_at=now_iso(), installed_at=now_iso())
                    else:
                        set_assignment(item["id"], machine, status="pending")
                note("registrada (já existia aqui)" if loc else "pendente de aprovação", name)
            if loc and loc.get("home", True) and (not a or a.get("status") != "rejected") and not dry:
                if link_everywhere(name):
                    note("cópia antiga trocada por link (backup)", name)
    for action, names in report.items():
        print(f"  {action}: {', '.join(names)}")
    print(f"{'(dry-run) ' if dry else ''}skills: {len(local)} local, {len(items)} no hub ({machine})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
