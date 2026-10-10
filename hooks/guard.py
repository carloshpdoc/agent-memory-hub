#!/usr/bin/env python3
"""
agent-memory-hub — one guard policy, enforced in Claude Code, Codex, Cursor and Gemini CLI.

Each tool calls this before a shell command or a file write (PreToolUse / BeforeTool /
beforeShellExecution) with `--tool <name>`. The payload is normalised to
(commands, written files, written text) and checked against ONE local policy:

  deny_commands     regexes; a match blocks the command...
  allow_commands    ...unless the whole command matches one of these (trusted remotes)
  allow_rm_rf       rm -rf is allowed only when EVERY target matches one of these
  protected_paths   regexes on paths the agent may not write
  block_secrets     block writing text that the capture's secret patterns flag

The policy lives outside git (guard.json next to .env, or GUARD_POLICY): it names your own
hosts and folders. guard.example.json is a generic starting point. Without a policy file
the guard allows everything and says so on stderr.

Blocking is exit code 2 with the reason on stderr, which all four tools honour; Cursor
also needs a JSON verdict on stdout, so it gets one. A crash in the guard never blocks
(exit 0 with a warning) except where the tool itself is configured to fail closed.
"""
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_POLICY = os.path.join(REPO, "guard.json")
SHELL_CONTROL = {";", "&&", "||", "|", ">", ">>", "<", "<<"}
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
WRITE_TEXT_KEYS = ("content", "new_string", "new_str", "text")


def load_policy(path=None):
    path = path or os.environ.get("GUARD_POLICY") or DEFAULT_POLICY
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


# ---- normalização: o que a chamada quer fazer, em qualquer ferramenta --------------------
def normalize(payload):
    """(commands, paths, texts) de um payload de PreToolUse/BeforeTool/beforeShellExecution."""
    commands, paths, texts = [], [], []
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}
    if payload.get("hook_event_name") == "beforeShellExecution" and payload.get("command"):
        commands.append(payload["command"])                      # Cursor
    elif tool == "apply_patch":                                  # Codex: o patch inteiro
        patch = ti.get("command") or ti.get("input") or ""
        paths += PATCH_FILE_RE.findall(patch) + PATCH_MOVE_RE.findall(patch)
        texts.append("\n".join(ln[1:] for ln in patch.splitlines()
                               if ln.startswith("+") and not ln.startswith("+++")))
    else:
        if isinstance(ti.get("command"), str):
            commands.append(ti["command"])                       # Bash / run_shell_command
        for k in ("file_path", "path", "notebook_path"):
            if isinstance(ti.get(k), str):
                paths.append(ti[k])
        for k in WRITE_TEXT_KEYS:
            if isinstance(ti.get(k), str):
                texts.append(ti[k])
        for e in ti.get("edits") or []:                          # MultiEdit
            if isinstance(e, dict) and isinstance(e.get("new_string"), str):
                texts.append(e["new_string"])
    return commands, [p.strip() for p in paths if p.strip()], [t for t in texts if t]


# ---- regras ----------------------------------------------------------------------------
def _resolve(path, cwd):
    p = os.path.expanduser(os.path.expandvars(path))
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p))


def trusted_rm_rf(command, cwd, allow):
    """rm -rf permitido só se TODOS os alvos baterem em allow (sem encadeamento de shell)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv or os.path.basename(argv[0]) != "rm" or any(t in SHELL_CONTROL for t in argv):
        return False
    opts, targets, parsing = [], [], True
    for tok in argv[1:]:
        if parsing and tok == "--":
            parsing = False
        elif parsing and tok.startswith("-"):
            opts.append(tok)
        else:
            parsing = False
            targets.append(tok)
    short = "".join(o.lstrip("-") for o in opts if not o.startswith("--"))
    recursive = "r" in short or "R" in short or "--recursive" in opts
    force = "f" in short or "--force" in opts
    if not (recursive and force and targets):
        return False
    return all(any(re.search(a, _resolve(t, cwd), re.IGNORECASE) for a in allow) for t in targets)


PUBLIC_BY_DESIGN = ("sb_publishable_",)   # chaves feitas pra ir no cliente
NOISY_LABELS = {"jwt"}                     # anon keys e tokens de exemplo: falso positivo demais


def _secret_label(text):
    """Rótulo do 1o segredo de ALTA precisão no texto (chaves privadas, tokens com prefixo
    conhecido), ou None. Atribuições genéricas (TOKEN_KEY = '...'), JWTs e <private> ficam
    de fora: medido no histórico real, 4 de 5 bloqueios por eles eram falsos positivos."""
    from capture_session import SECRET_PATTERNS   # os mesmos padrões da captura
    for label, rx in SECRET_PATTERNS:
        if label in NOISY_LABELS:
            continue
        for m in rx.finditer(text):
            if not m.group(0).startswith(PUBLIC_BY_DESIGN):
                return label
    return None


def check(policy, payload):
    """None se permitido; senão o motivo do bloqueio."""
    if not policy:
        return None
    commands, paths, texts = normalize(payload)
    cwd = payload.get("cwd") or ""
    for cmd in commands:
        if any(re.match(a, cmd) for a in policy.get("allow_commands", [])):
            continue
        if trusted_rm_rf(cmd, cwd, policy.get("allow_rm_rf", [])):
            continue   # rm -rf só em alvos confiáveis (ex.: DerivedData)
        for rule in policy.get("deny_commands", []):
            pat = rule["pattern"] if isinstance(rule, dict) else rule
            if re.search(pat, cmd):
                why = rule.get("reason") if isinstance(rule, dict) else None
                return f"comando bloqueado pela política ({why or pat}): {cmd[:200]}"
    for p in paths:
        full = _resolve(p, cwd)
        for pat in policy.get("protected_paths", []):
            if re.search(pat, full, re.IGNORECASE):
                return f"arquivo protegido pela política ({pat}): {p}"
    if policy.get("block_secrets") and texts:
        for t in texts:
            label = _secret_label(t)
            if label:
                return f"escrita contém um segredo ({label}); use variável de ambiente ou arquivo ignorado"
    return None


def respond(tool, reason):
    """Veredito no formato da ferramenta; exit 2 bloqueia em todas."""
    if tool == "cursor":
        print(json.dumps({"permission": "deny" if reason else "allow",
                          **({"user_message": f"agent-memory-hub guard: {reason}",
                              "agent_message": f"Bloqueado: {reason}"} if reason else {})}))
    if reason:
        print(f"BLOQUEADO (agent-memory-hub guard): {reason}", file=sys.stderr)
        return 2
    return 0


def main(argv):
    tool = argv[argv.index("--tool") + 1] if "--tool" in argv else "claude-code"
    try:
        payload = json.load(sys.stdin)
        policy = load_policy()
        if policy is None:
            print("guard: sem guard.json; nada é verificado", file=sys.stderr)
        return respond(tool, check(policy, payload))
    except Exception as e:   # o guard nunca derruba a ferramenta por bug próprio
        print(f"guard: erro interno, permitindo: {type(e).__name__}: {e}", file=sys.stderr)
        return respond(tool, None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
