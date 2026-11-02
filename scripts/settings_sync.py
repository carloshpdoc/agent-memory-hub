#!/usr/bin/env python3
"""
agent-memory-hub — config sync phase 2: hooks and permissions, approved per target.

Called by config_sync.py after the skills. Items (sql/11 + sql/13):
  hook        "<tool>:<event>:<id>"   files {tool, event, matcher, command}
  permission  "<tool>:<list>:<rule>"  files {tool, list, rule}
The id of a hook hashes event + matcher + command, so ANY edit is a new item that starts
'pending' on the other targets; the old one is marked retired (source no longer has it)
and is removed from a target only when rejected there. Permissions are one item per rule.

Targets: Claude Code settings.json per config dir (accounts that symlink the same file
share one target), Codex ~/.codex/hooks.json and Cursor ~/.cursor/hooks.json (host),
Cursor CLI ~/.cursor/cli-config.json permissions (host). Hooks the hub installs itself and
hooks of apps/plugins are never synced. Home paths travel as __HOME__ and are expanded on
install; a hook whose referenced files are missing on the target is not installed, and
the reason goes to config_assignments.note (shown in the dashboard). Every write is
merge-only, with a backup of the file first.

Scripts a hook calls from a tool's own hooks folder (~/.claude*/hooks, ~/.codex/hooks,
~/.cursor/hooks) travel inside the item as files["scripts"] and are written on install, so
approving the hook on another machine also brings what it runs. Never bundled: symlinks
(managed elsewhere), binaries, files over MAX_SCRIPT and files that look like they carry a
secret; those stay a missing dependency on the other machine.
"""
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
HOME_TOKEN = "__HOME__"
NEVER_SYNC_RE = re.compile(r"agent-memory-hub/hooks/|Application Support/|/\.tmux/plugins/|/plugins/")
REF_RE = re.compile(r"(?:~|__HOME__|/Users/[^/\s\"']+|/opt/[^/\s\"']+)/[^\s\"'|;&)<>]+")
PERM_LISTS = {"claude": ("allow", "deny", "ask"), "cursor-cli": ("allow", "deny")}
# só scripts dentro da pasta hooks/ de uma config de ferramenta viajam com o hook
SCRIPT_PATH_RE = re.compile(r"^__HOME__/\.(?:claude[A-Za-z0-9_-]*|codex|cursor)/hooks/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
MAX_SCRIPT = 64 * 1024


# ---- portabilidade e identidade ------------------------------------------------------------
def portable(text):
    return text.replace(HOME, HOME_TOKEN)


def localize(text):
    return text.replace(HOME_TOKEN, HOME)


def hook_item(tool, event, matcher, command):
    pc = portable(command)
    hid = hashlib.sha256(f"{event}\0{matcher}\0{pc}".encode()).hexdigest()[:10]
    return f"{tool}:{event}:{hid}", {"tool": tool, "event": event, "matcher": matcher or "", "command": pc}


def perm_item(tool, lst, rule):
    return f"{tool}:{lst}:{rule}", {"tool": tool, "list": lst, "rule": rule}


def item_hash(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def syncable_hook(command):
    return bool(command) and not NEVER_SYNC_RE.search(command)


def _refs(command):
    """Caminhos (formato portável) que o comando lê, sem os destinos de redirecionamento."""
    text = portable(command)
    for m in REF_RE.finditer(text):
        if text[:m.start()].rstrip().endswith(">"):
            continue                 # destino de redirecionamento (>> log.txt): saída, não dependência
        ref = m.group(0).rstrip(".,")
        yield ref.replace("~", HOME_TOKEN, 1) if ref.startswith("~/") else ref


def missing_deps(command, provided=()):
    """Arquivos que o hook referencia (na home ou /opt) e que não existem nesta máquina.
    `provided`: scripts que vêm junto com o item e serão gravados antes de instalar."""
    out = []
    for ref in _refs(command):
        if ref in provided:
            continue
        path = localize(ref) if ref.startswith(HOME_TOKEN) else ref
        if not os.path.exists(path):
            out.append(ref.replace(HOME_TOKEN, "~"))
    return out


def _script_path(ref):
    """Caminho local de um script portável, ou None se ele sair da pasta hooks/ permitida."""
    if not SCRIPT_PATH_RE.match(ref) or ".." in ref.split("/"):
        return None
    path = localize(ref)
    hooks_dir = os.path.join(HOME, ref[len(HOME_TOKEN) + 1:].split("/")[0], "hooks")
    real = os.path.realpath(path)
    return path if real.startswith(os.path.realpath(hooks_dir) + os.sep) else None


def hook_scripts(command):
    """({ref: {content, exec}}, [(ref, motivo)]) dos scripts que viajam com o hook."""
    hooks_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    from capture_session import SECRET_PATTERNS
    scripts, skipped = {}, []
    for ref in _refs(command):
        path = _script_path(ref)
        if path is None or ref in scripts:
            continue
        if not os.path.isfile(path):
            continue                 # some: missing_deps acusa no destino
        if os.path.islink(path):
            skipped.append((ref, "link"))
        elif os.path.getsize(path) > MAX_SCRIPT:
            skipped.append((ref, "grande demais"))
        else:
            try:
                content = open(path, "rb").read().decode("utf-8")
            except UnicodeDecodeError:
                skipped.append((ref, "binário"))
                continue
            if any(rx.search(content) for _, rx in SECRET_PATTERNS):
                skipped.append((ref, "parece ter segredo"))
                continue
            scripts[ref] = {"content": content, "exec": os.access(path, os.X_OK)}
    return scripts, skipped


def write_scripts(scripts, backup_dir, dry=False):
    """Grava os scripts do item (backup do que for sobrescrito). Devolve os refs que mudaram;
    ref fora da pasta hooks/ permitida levanta ValueError (nada é gravado)."""
    plan = []
    for ref, spec in sorted((scripts or {}).items()):
        path = _script_path(ref)
        if path is None or not isinstance(spec, dict) or not isinstance(spec.get("content"), str):
            raise ValueError(f"script inválido: {ref}")
        mode = 0o755 if spec.get("exec") else 0o644
        try:
            same = open(path, encoding="utf-8").read() == spec["content"]
        except (OSError, UnicodeDecodeError):
            same = False
        if not same:
            plan.append((ref, path, spec["content"], mode))
    if dry:
        return [ref for ref, *_ in plan]
    for ref, path, content, mode in plan:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        backup_and_write_text(path, content, backup_dir)
        os.chmod(path, mode)
    return [ref for ref, *_ in plan]


# ---- leitura/escrita dos formatos de cada ferramenta (puro: dict -> dict) -------------------
def read_hooks(cfg, shape):
    """[(event, matcher, command)]. shape 'nested' (Claude/Codex) ou 'flat' (Cursor)."""
    out = []
    for event, groups in ((cfg or {}).get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            if shape == "flat":
                if g.get("command"):
                    out.append((event, g.get("matcher") or "", g["command"]))
                continue
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("type", "command") == "command" and h.get("command"):
                    out.append((event, g.get("matcher") or "", h["command"]))
    return out


def add_hook(cfg, shape, event, matcher, command):
    """Acrescenta se ainda não houver o mesmo comando no evento. True se mudou."""
    if any(e == event and c == command for e, _, c in read_hooks(cfg, shape)):
        return False
    groups = cfg.setdefault("hooks", {}).setdefault(event, [])
    if shape == "flat":
        groups.append({"command": command, **({"matcher": matcher} if matcher else {})})
        return True
    for g in groups:
        if isinstance(g, dict) and (g.get("matcher") or "") == (matcher or ""):
            g.setdefault("hooks", []).append({"type": "command", "command": command})
            return True
    groups.append({"matcher": matcher or "", "hooks": [{"type": "command", "command": command}]})
    return True


def remove_hook(cfg, shape, event, command):
    """Tira o comando do evento (e grupos que ficarem vazios). True se mudou."""
    groups = ((cfg or {}).get("hooks") or {}).get(event)
    if not isinstance(groups, list):
        return False
    changed = False
    if shape == "flat":
        keep = [g for g in groups if not (isinstance(g, dict) and g.get("command") == command)]
        changed = len(keep) != len(groups)
        cfg["hooks"][event] = keep
    else:
        for g in groups:
            hooks = g.get("hooks") or []
            keep = [h for h in hooks if h.get("command") != command]
            if len(keep) != len(hooks):
                g["hooks"], changed = keep, True
        cfg["hooks"][event] = [g for g in groups if g.get("hooks")]
    if not cfg["hooks"][event]:
        del cfg["hooks"][event]
    return changed


def read_perms(cfg, lists):
    perms = (cfg or {}).get("permissions") or {}
    return [(lst, rule) for lst in lists for rule in perms.get(lst) or [] if isinstance(rule, str)]


def add_perm(cfg, lst, rule):
    rules = cfg.setdefault("permissions", {}).setdefault(lst, [])
    if rule in rules:
        return False
    rules.append(rule)
    return True


def remove_perm(cfg, lst, rule):
    rules = ((cfg or {}).get("permissions") or {}).get(lst) or []
    if rule not in rules:
        return False
    rules.remove(rule)
    return True


# ---- alvos ---------------------------------------------------------------------------------
def settings_targets(machine, skill_targets):
    """[(target, tool, path, shape)] desta máquina. Configs do Claude que apontam para o mesmo
    settings.json (link) viram um alvo só: o primeiro (a conta padrão)."""
    out, seen = [], set()
    for target, skills_dir, _primary in skill_targets:
        settings = os.path.join(os.path.dirname(os.path.normpath(skills_dir)), "settings.json")
        real = os.path.realpath(settings)
        if os.path.isfile(settings) and real not in seen:
            seen.add(real)
            out.append((target, "claude", settings, "nested"))
    for tool, path, shape in (("codex", "~/.codex/hooks.json", "nested"),
                              ("cursor", "~/.cursor/hooks.json", "flat"),
                              ("cursor-cli", "~/.cursor/cli-config.json", None)):
        path = os.path.expanduser(path)
        if os.path.isdir(os.path.dirname(path)):
            out.append((machine, tool, path, shape))
    return out


def local_items(cfg, tool, shape):
    """{name: files} dos hooks pessoais e permissões presentes neste arquivo."""
    out = {}
    if shape:
        for event, matcher, command in read_hooks(cfg, shape):
            if syncable_hook(command):
                name, files = hook_item(tool, event, matcher, command)
                scripts, _ = hook_scripts(command)
                if scripts:          # sem script, o item fica igual ao de antes (mesmo hash)
                    files["scripts"] = scripts
                out[name] = ("hook", files)
    for lst, rule in read_perms(cfg, PERM_LISTS.get(tool, ())):
        name, files = perm_item(tool, lst, rule)
        out[name] = ("permission", files)
    return out


def apply_item(cfg, shape, kind, files, install=True):
    """Instala/remove um item no dict do arquivo. True se mudou."""
    if kind == "hook":
        cmd = localize(files["command"])
        return (add_hook(cfg, shape, files["event"], files["matcher"], cmd) if install
                else remove_hook(cfg, shape, files["event"], cmd))
    return add_perm(cfg, files["list"], files["rule"]) if install else remove_perm(cfg, files["list"], files["rule"])


def backup_and_write(path, cfg, backup_dir):
    backup_and_write_text(path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", backup_dir)


def backup_and_write_text(path, text, backup_dir):
    path = os.path.realpath(path)    # escreve no arquivo real: um link (outra conta) continua link
    if os.path.exists(path):
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, os.path.join(backup_dir, f"{os.path.basename(os.path.dirname(path))}-"
                                                    f"{os.path.basename(path)}-{stamp}"))
    tmp = f"{path}.sync-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)   # mantém a permissão original (ex.: 600)
    os.replace(tmp, path)
