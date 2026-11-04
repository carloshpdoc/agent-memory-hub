#!/usr/bin/env python3
"""
agent-memory-hub — config sync: Claude Code plugins, approved per target (one Claude account
on one machine, as for skills and MCP servers).

Item (sql/11):
  plugin  "claude:<plugin>@<marketplace>"  files {tool, id, marketplace, source}

source is the marketplace's entry in that account's plugins/known_marketplaces.json (github
repo, git url or local directory, home paths as __HOME__), or {} for a marketplace Claude
Code knows by itself (e.g. anthropic-plugin-directory). Only user-scope installs count;
project and local scope plugins belong to their repo and never sync.

Writes go through the CLI, per CLAUDE_CONFIG_DIR: `claude plugin marketplace add` when the
target does not know the marketplace yet, then `claude plugin install <id> --scope user`.
Never passes -y: a plugin that runs a marketplace-declared command needs a person to accept
it, and the reason lands in config_assignments.note. Rejected: `claude plugin uninstall`.
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import settings_sync as ss  # noqa: E402  (portable/localize)

TOOL = "claude"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TIMEOUT = 300                      # install clona o marketplace/plugin: rede


def plugin_targets(skill_targets):
    """[(target, config_dir, env)] para cada conta do Claude Code desta máquina."""
    if not shutil.which("claude"):
        return []
    out = []
    for target, skills_dir, primary in skill_targets:
        config_dir = os.path.dirname(os.path.normpath(skills_dir))
        if os.path.isdir(config_dir):
            out.append((target, config_dir, None if primary else config_dir))
    return out


def _env(config_dir):
    """Ambiente do CLI para a conta. Conta padrão: sem CLAUDE_CONFIG_DIR, mesmo que o sync
    tenha sido disparado de dentro de uma sessão da outra conta."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def _run(args, config_dir):
    try:
        return subprocess.run([shutil.which("claude"), "plugin", *args], check=True, capture_output=True,
                              text=True, env=_env(config_dir), timeout=TIMEOUT)
    except subprocess.CalledProcessError as e:
        out = (e.stderr or e.stdout or "").strip().splitlines()
        raise RuntimeError(out[-1] if out else f"exit {e.returncode}") from None


def list_installed(config_dir):
    """{id: entrada} dos plugins instalados no escopo de usuário daquela conta."""
    data = json.loads(_run(["list", "--json"], config_dir).stdout)
    if not isinstance(data, list):
        raise ValueError("claude plugin list --json não devolveu uma lista")
    return {p["id"]: p for p in data if p.get("scope") == "user" and ID_RE.match(p.get("id") or "")}


def known_marketplaces(config_dir):
    path = os.path.join(config_dir, "plugins", "known_marketplaces.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def to_item(plugin_id, markets):
    """(item_name, files) portátil."""
    marketplace = plugin_id.rsplit("@", 1)[1]
    source = (markets.get(marketplace) or {}).get("source")
    source = json.loads(ss.portable(json.dumps(source))) if isinstance(source, dict) else {}
    return f"{TOOL}:{plugin_id}", {"tool": TOOL, "id": plugin_id, "marketplace": marketplace, "source": source}


def marketplace_arg(source):
    """Argumento de `claude plugin marketplace add` para a fonte, ou None se não souber."""
    src = json.loads(ss.localize(json.dumps(source or {})))
    kind = src.get("source")
    arg = {"github": src.get("repo"), "git": src.get("url"), "url": src.get("url"),
           "directory": src.get("path")}.get(kind)
    return arg if isinstance(arg, str) and arg and not arg.startswith("-") else None


def resolve_for_target(files, markets):
    """(arg do marketplace a adicionar ou None, motivo para não instalar ou None)."""
    if not ID_RE.match(files.get("id") or ""):
        return None, f"id de plugin inválido: {files.get('id')!r}"
    if files["marketplace"] in markets or not files.get("source"):
        return None, None                  # já conhecido aqui, ou embutido no Claude Code
    arg = marketplace_arg(files["source"])
    if arg is None:
        return None, f"fonte de marketplace desconhecida: {files['source'].get('source')}"
    if files["source"].get("source") == "directory" and not os.path.isdir(arg):
        return None, f"falta dependência: {arg.replace(ss.HOME, '~')}"
    return arg, None


def install(config_dir, files, add_arg):
    """Adiciona o marketplace (se preciso) e instala. Lança RuntimeError com a mensagem do CLI."""
    if add_arg:
        _run(["marketplace", "add", add_arg], config_dir)
    _run(["install", files["id"], "--scope", "user"], config_dir)


def uninstall(config_dir, plugin_id):
    if not ID_RE.match(plugin_id):
        raise ValueError(f"id de plugin inválido: {plugin_id!r}")
    _run(["uninstall", plugin_id, "--scope", "user"], config_dir)
