#!/usr/bin/env python3
"""
agent-memory-hub — sync skills across machines, installing only what the dashboard approved.

Phase 1 of config sync (sql/11-config-sync.sql). A target is one Claude Code account on one
machine: the default config (~/.claude) is "<hostname>"; every other config dir
(CLAUDE_CONFIG_DIR, e.g. ~/.claude-work) is "<hostname>:<profile>", approved separately.
Each run, for every target on this machine:
  1. publish: every skill in ~/.claude/skills goes to config_items (new or changed). Having
     a skill locally counts as approval for this machine.
  2. register: items this machine has never seen get a 'pending' assignment, so they show
     up in the dashboard's Sync tab for approval.
  3. install: items 'approved' for the target are written to its skills folder; for the
     default account also linked from ~/.agents/skills/<name> (Codex; Cursor reads both
     ~/.claude/skills and ~/.agents/skills). Anything overwritten is backed up first.
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


def targets(machine):
    """[(id, pasta, padrão?)]: a conta padrão (~/.claude) usa o hostname; cada outra config do
    Claude Code (ex. ~/.claude-work) é o alvo '<host>:<perfil>', aprovado à parte no dashboard."""
    out = [(machine, SKILLS_DIR, True)]
    for d in extra_claude_dirs():
        profile = os.path.basename(os.path.dirname(os.path.normpath(d))).lstrip(".")
        profile = profile[len("claude-"):] if profile.startswith("claude-") else profile
        out.append((f"{machine}:{profile}", d, False))
    return out


def managed_link(base, name):
    """Link deixado pelo sync anterior (aponta para dentro de SKILLS_DIR) na pasta de outra conta."""
    p = os.path.join(base, name)
    return os.path.islink(p) and os.path.realpath(p).startswith(os.path.realpath(SKILLS_DIR) + os.sep)


def local_skills(base=None):
    """Skills em base (padrão: SKILLS_DIR). Na pasta de outra conta, um link do sync anterior
    não conta como presença: a aprovação é por conta, não herdada da conta padrão."""
    base = base or SKILLS_DIR
    out = {}
    if not os.path.isdir(base):
        return out
    primary = os.path.realpath(base) == os.path.realpath(SKILLS_DIR)
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if name in SKIP or name.startswith(".") or not NAME_RE.match(name) or not os.path.isdir(path):
            continue
        if not primary and managed_link(base, name):
            continue
        files = read_skill(path)
        if files:
            out[name] = {"files": files, "hash": files_hash(files), "link": os.path.islink(path),
                         "mtime": max(os.path.getmtime(os.path.join(os.path.realpath(path), f)) for f in files)}
    return out


# ---- instalação ------------------------------------------------------------------------
def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path, label):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}-{label}-{_stamp()}")
    shutil.move(path, dest)
    return dest


def write_skill(name, files, dry=False, base=None):
    """Instala em base/name (padrão SKILLS_DIR; backup do que existir). Na conta padrão, liga
    também ~/.agents/skills/name (Codex)."""
    base = base or SKILLS_DIR
    if not NAME_RE.match(name) or not all(safe_relpath(r) for r in files):
        raise ValueError(f"nome ou caminho inseguro em {name!r}")
    target = os.path.join(base, name)
    primary = os.path.realpath(base) == os.path.realpath(SKILLS_DIR)
    if os.path.islink(target) and (primary or not managed_link(base, name)):
        return "link (gerida por repo; não sobrescrita)"
    if dry:
        return "instalaria"
    if os.path.islink(target):
        os.unlink(target)                  # link do sync anterior: vira cópia própria da conta
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
    os.makedirs(base, exist_ok=True)
    os.rename(tmp, target)
    if primary:
        link_into(AGENTS_DIR, name)
    return "instalada"


def link_into(base, name):
    """base/<name> -> SKILLS_DIR/<name>. Uma cópia solta antiga ali vai para backup e vira link
    (senão o Codex usaria uma versão defasada). Links existentes ficam."""
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


def sync_target(target, base, primary, dry, note):
    """Um alvo (conta): publica o que existe ali e instala o que está aprovado PARA ELE."""
    items = {i["name"]: i for i in rest_all(
        "config_items?select=id,name,hash,files,updated_at,source_machine&kind=eq.skill")}
    mine = {a["item_id"]: a for a in rest_all(
        f"config_assignments?select=item_id,status,installed_hash&machine=eq.{urllib.parse.quote(target)}")}
    if not primary and not dry and os.path.isdir(base):
        for name in sorted(os.listdir(base)):     # links do sync anterior sem aprovação desta conta
            item = items.get(name)
            approved = item and (mine.get(item["id"]) or {}).get("status") == "approved"
            if managed_link(base, name) and not approved:
                os.unlink(os.path.join(base, name))
                note("link removido (aprove por conta)", name)
    local = local_skills(base)
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
                        "source_machine": target, "updated_at": now_iso()}
                if item:
                    _req("PATCH", f"config_items?id=eq.{item['id']}", body)
                    item_id = item["id"]
                else:
                    item_id = _req("POST", "config_items", body, prefer="return=representation")[0]["id"]
                set_assignment(item_id, target, status="approved", installed_hash=loc["hash"],
                               decided_at=now_iso(), installed_at=now_iso())
            note({"publish": "publicada", "update": "atualizada no hub",
                  "conflict-local": "conflito: local venceu (remota em backup)"}[action], name)
        elif action in ("install", "conflict-remote"):
            result = write_skill(name, item["files"], dry, base)
            if not dry and not result.startswith("link"):
                set_assignment(item["id"], target, status="approved", installed_hash=item["hash"],
                               installed_at=now_iso())
            note("conflito: hub venceu (local em backup)" if action == "conflict-remote" else result, name)
        elif action == "uninstall":
            if not dry:
                path = os.path.join(base, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    backup(path, "rejeitada")
                link = os.path.join(AGENTS_DIR, name)
                if primary and os.path.islink(link):
                    os.unlink(link)
                set_assignment(item["id"], target, installed_hash=None)
            note("removida (rejeitada; em backup)", name)
        else:   # noop: registra o alvo e, na conta padrão, garante o link do Codex
            if item and a is None:
                if not dry:
                    if loc:   # mesma versão já existe aqui: estar aqui é a aprovação
                        set_assignment(item["id"], target, status="approved", installed_hash=loc["hash"],
                                       decided_at=now_iso(), installed_at=now_iso())
                    else:
                        set_assignment(item["id"], target, status="pending")
                note("registrada (já existia aqui)" if loc else "pendente de aprovação", name)
            if primary and loc and (not a or a.get("status") != "rejected") and not dry:
                if link_into(AGENTS_DIR, name):
                    note("cópia antiga no ~/.agents trocada por link (backup)", name)
    return len(local), len(items)


def main(argv):
    dry = "--dry-run" in argv
    if not URL or not KEY:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1
    for target, base, primary in targets(socket.gethostname()):
        report = {}
        n_local, n_hub = sync_target(target, base, primary, dry,
                                     lambda action, name: report.setdefault(action, []).append(name))
        for action, names in report.items():
            print(f"  {action}: {', '.join(names)}")
        print(f"{'(dry-run) ' if dry else ''}skills [{target}]: {n_local} local, {n_hub} no hub")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
