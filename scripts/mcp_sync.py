#!/usr/bin/env python3
"""
agent-memory-hub — config sync phase 3: MCP servers and the guard policy, approved per target.

Items (sql/11):
  mcp    "<tool>:<server>"  files {tool, name, config}     tool: claude | codex | cursor
  guard  "guard:<host>"     files {"guard.json": text}     the hub's guard policy (guard.json)

MCP servers are identified by name; when the source edits one, every other target that had
approved it goes back to 'pending' (keeps what it installed until re-approved). Never synced:
the hub's own server (installers manage it) and servers bundled in apps. Credentials never
leave the machine: an env/header value that looks like a secret travels as __LOCAL__ and the
target installs the server only if it already has that value (otherwise it reports which
one is missing). Home paths travel as __HOME__. A server whose command is not installed on
the target is not added, with the reason in config_assignments.note.

Writes go through each tool's own surface: `claude mcp add-json/remove --scope user`
(per CLAUDE_CONFIG_DIR, so the live ~/.claude.json is never edited by hand), a validated
append to ~/.codex/config.toml (+ `codex mcp remove`), and a merge into ~/.cursor/mcp.json.
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
from capture_session import SECRET_PATTERNS  # noqa: E402
import settings_sync as ss  # noqa: E402  (portable/localize, backup_and_write)

LOCAL = "__LOCAL__"
NEVER_SYNC_RE = re.compile(r"agent-memory-hub|/Applications/|\.app/|Application Support/")
SECRET_KEY_RE = re.compile(r"(?i)(key|token|secret|password|passwd|auth|bearer|cookie|credential)")


# ---- item ---------------------------------------------------------------------------------
def syncable(name, cfg):
    """Fora: o MCP do próprio hub e os que rodam DE DENTRO de um app (o comando aponta para
    um bundle). Um caminho de app só no env (ex. GODOT_PATH) é dependência, não exclusão."""
    launch = f"{cfg.get('command') or ''} {cfg.get('url') or ''}"
    return name != "agent-memory-hub" and not NEVER_SYNC_RE.search(launch)


def _secretish(key, value):
    if not isinstance(value, str):
        return False
    if any(rx.search(value) for _, rx in SECRET_PATTERNS):
        return True
    path = os.path.expanduser(value)
    # chave "de credencial" com valor que é caminho de arquivo (ex. GOOGLE_APPLICATION_CREDENTIALS):
    # o caminho viaja; o arquivo vira dependência na máquina de destino
    return bool(SECRET_KEY_RE.search(key)) and not (path.startswith("/") and os.path.exists(path))


def to_item(tool, name, cfg):
    """(item_name, files): config portátil, com segredos trocados por __LOCAL__."""
    clean = json.loads(ss.portable(json.dumps(cfg)))
    for field in ("env", "headers", "http_headers"):
        vals = clean.get(field)
        if isinstance(vals, dict):
            for k, v in vals.items():
                if _secretish(k, ss.localize(v) if isinstance(v, str) else v):
                    vals[k] = LOCAL
    return f"{tool}:{name}", {"tool": tool, "name": name, "config": clean}


def resolve_for_target(files, current):
    """Config pronta para instalar aqui, ou (None, motivo). current: a config que a máquina
    já tem para esse servidor (de onde vêm os valores __LOCAL__)."""
    cfg = json.loads(ss.localize(json.dumps(files["config"])))
    missing = []
    for field in ("env", "headers", "http_headers"):
        vals = cfg.get(field)
        if isinstance(vals, dict):
            for k, v in vals.items():
                if v == LOCAL:
                    have = ((current or {}).get(field) or {}).get(k)
                    if have and have != LOCAL:
                        vals[k] = have
                    else:
                        missing.append(k)
    if missing:
        return None, f"precisa da credencial local: {', '.join(missing)}"
    cmd = cfg.get("command")
    if cmd and not (shutil.which(cmd) if "/" not in cmd else os.path.exists(cmd)):
        return None, f"comando não encontrado nesta máquina: {cmd}"
    paths = [a for a in cfg.get("args") or [] if isinstance(a, str) and a.startswith(ss.HOME)]
    paths += [v for v in (cfg.get("env") or {}).values() if isinstance(v, str) and v.startswith("/")]
    deps = [p for p in paths if not os.path.exists(p)]
    if deps:
        return None, f"falta dependência: {', '.join(d.replace(ss.HOME, '~') for d in deps)}"
    return cfg, None


# ---- leitura por ferramenta ---------------------------------------------------------------
def toml_lib():
    """tomllib (3.11+) ou tomli; None se nenhum existir (ex.: /usr/bin/python3 3.9 do macOS):
    aí o Codex fica de fora do sync de MCP nesta máquina, sem derrubar o resto."""
    try:
        import tomllib
        return tomllib
    except ImportError:
        try:
            import tomli
            return tomli
        except ImportError:
            return None


def read_servers(tool, path):
    """{nome: config} dos servidores MCP de usuário daquela ferramenta/arquivo."""
    if not os.path.exists(path):
        return {}
    if tool == "codex":
        return (toml_lib().load(open(path, "rb")).get("mcp_servers") or {})
    return (json.load(open(path)).get("mcpServers") or {})


def mcp_targets(machine, skill_targets):
    """[(target, tool, path, env)]: Claude por conta (~/.claude.json da conta padrão,
    <CLAUDE_CONFIG_DIR>/.claude.json das outras), Codex e Cursor pela máquina."""
    out = []
    for target, skills_dir, primary in skill_targets:
        config_dir = os.path.dirname(os.path.normpath(skills_dir))
        path = os.path.expanduser("~/.claude.json") if primary else os.path.join(config_dir, ".claude.json")
        if os.path.exists(path) and shutil.which("claude"):
            out.append((target, "claude", path, {} if primary else {"CLAUDE_CONFIG_DIR": config_dir}))
    codex = os.path.expanduser("~/.codex/config.toml")
    if os.path.exists(codex) and shutil.which("codex") and toml_lib():
        out.append((machine, "codex", codex, {}))
    cursor = os.path.expanduser("~/.cursor/mcp.json")
    if os.path.isdir(os.path.dirname(cursor)):
        out.append((machine, "cursor", cursor, {}))
    return out


# ---- escrita por ferramenta ---------------------------------------------------------------
def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)       # string TOML básica == string JSON


def codex_table(name, cfg):
    """Texto TOML de [mcp_servers.<name>] (+ subtabelas para dicts como env)."""
    key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
    lines, subs = [f"[mcp_servers.{key}]"], []
    for k, v in cfg.items():
        if isinstance(v, dict):
            subs.append(f"\n[mcp_servers.{key}.{k}]\n" + "\n".join(f"{json.dumps(sk)} = {_toml_value(sv)}" for sk, sv in v.items()))
        else:
            lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "".join(subs) + "\n"


def install(tool, path, env, name, cfg, backup_dir):
    """Acrescenta o servidor. Lança erro se a ferramenta recusar."""
    if tool == "claude":
        subprocess.run([shutil.which("claude"), "mcp", "add-json", name, json.dumps(cfg), "--scope", "user"],
                       check=True, capture_output=True, text=True, env={**os.environ, **env}, timeout=60)
    elif tool == "codex":
        text = open(path).read()
        new = text.rstrip("\n") + "\n\n" + codex_table(name, cfg)
        toml_lib().loads(new)                                # nunca grava TOML inválido
        ss.backup_and_write_text(path, new, backup_dir)
    else:
        data = json.load(open(path)) if os.path.exists(path) else {}
        data.setdefault("mcpServers", {})[name] = cfg
        ss.backup_and_write(path, data, backup_dir)


def uninstall(tool, path, env, name, backup_dir):
    if tool == "claude":
        subprocess.run([shutil.which("claude"), "mcp", "remove", name, "--scope", "user"],
                       check=True, capture_output=True, text=True, env={**os.environ, **env}, timeout=60)
    elif tool == "codex":
        subprocess.run([shutil.which("codex"), "mcp", "remove", name],
                       check=True, capture_output=True, text=True, timeout=60)
    else:
        data = json.load(open(path))
        if (data.get("mcpServers") or {}).pop(name, None) is not None:
            ss.backup_and_write(path, data, backup_dir)
