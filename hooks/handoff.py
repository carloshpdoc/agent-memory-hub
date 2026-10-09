"""
agent-memory-hub — cross-tool handoff: "where did the last session leave off?"

A handoff is built at read time from the latest captured session of a project (any tool,
any machine): its goal (first ask), the latest ask, the agent's last reply (where "did X,
next Y" usually lives) and the git state saved at capture. Capture hooks store that git
state in metadata.git via git_snapshot(); nothing else is persisted.

Used by recall_session.py (auto-injected when the last work on the project was in another
tool or on another machine), memory_client.handoff (MCP `get_handoff`) and `mem handoff`.
Pure stdlib; git calls are short and fail-safe.
"""
import re
import subprocess
from datetime import datetime, timezone

BLOCK_RE = re.compile(r"(?:^|\n\n)\[(user|assistant)\]\n")
SUBAGENT_MARK = "\n\n--- subagent "   # capture_session anexa subagentes depois da conversa
SYSTEM_BLOCK_RE = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout|local-command-caveat)>.*?</\1>",
    re.DOTALL)
MIN_ASK_CHARS = 15
COUNTER_RE = re.compile(r"\s*\(\d+q/\d+r\)\s*$")
MAX_DIRTY = 15          # arquivos modificados guardados por sessão
REPLY_CHARS = 900       # última resposta do agente no handoff
ASK_CHARS = 300
HANDOFF_MAX_HOURS = 24  # injeção automática só para trabalho recente


def git_snapshot(cwd, timeout=3):
    """{'branch', 'head', 'dirty': [...], 'dirty_count'} do repo em cwd, ou None."""
    def git(*args):
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return r.stdout
    try:
        head = git("rev-parse", "--short", "HEAD").strip()
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        dirty = [ln[3:] for ln in git("status", "--porcelain").splitlines() if len(ln) > 3]
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    return {"branch": branch, "head": head, "dirty": dirty[:MAX_DIRTY], "dirty_count": len(dirty)}


def _real_ask(text):
    """Pedido do usuário sem blocos de sistema/ruído, ou '' se não sobrar pedido de verdade."""
    from capture_session import INJECTED_PREFIXES, clean_user_text   # import tardio: evita ciclo
    t = clean_user_text(SYSTEM_BLOCK_RE.sub(" ", text))
    return "" if len(t) < MIN_ASK_CHARS or t.lower().startswith(INJECTED_PREFIXES) else t


def last_turns(content):
    """(último pedido real do usuário, última resposta do agente), só da conversa principal."""
    main = (content or "").split(SUBAGENT_MARK)[0]
    parts = BLOCK_RE.split(main)
    # split -> ["", role, text, role, text, ...]
    ask = reply = ""
    for i in range(1, len(parts) - 1, 2):
        role, text = parts[i], parts[i + 1].strip()
        if role == "user":
            ask = _real_ask(text) or ask
        elif text:
            reply = text
    return ask, reply


def _clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _hours_since(iso, now=None):
    try:
        ts = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - ts).total_seconds() / 3600


def build_handoff(row, now=None):
    """Markdown da passagem a partir de uma linha de `sessions` (com content e metadata)."""
    meta = row.get("metadata") or {}
    goal = COUNTER_RE.sub("", row.get("summary") or "").split(" [...] ")[0]
    ask, reply = last_turns(row.get("content"))
    when = row.get("ended_at") or row.get("started_at") or ""
    age = _hours_since(when, now)
    ago = f"há {age:.0f}h" if age is not None and age >= 1 else "há menos de 1h"
    sid = row.get("session_id") or ""
    lines = [
        f"Última sessão em `{row.get('project') or '-'}`: **{row.get('tool', '?')}** em "
        f"{row.get('machine', '?')}, {ago} (`{sid[:8]}`).",
        f"- **Objetivo:** {_clip(goal, ASK_CHARS) or '-'}",
    ]
    if ask and _clip(ask, ASK_CHARS) != _clip(goal, ASK_CHARS):
        lines.append(f"- **Último pedido:** {_clip(ask, ASK_CHARS)}")
    if reply:
        lines.append(f"- **Onde parou (última resposta do agente):** {_clip(reply, REPLY_CHARS)}")
    g = meta.get("git")
    if isinstance(g, dict) and g.get("head"):
        state = f"`{g.get('branch')}` @ `{g.get('head')}`"
        n = g.get("dirty_count") or 0
        if n:
            files = ", ".join(f"`{f}`" for f in (g.get("dirty") or []))
            more = f" (+{n - len(g.get('dirty') or [])})" if n > len(g.get("dirty") or []) else ""
            state += f"; {n} arquivo(s) não commitado(s): {files}{more}"
        else:
            state += "; working tree limpa"
        lines.append(f"- **Git ao salvar:** {state}")
    lines.append(f"- Transcript completo: tool MCP `get_session` com `{sid[:8]}`.")
    return "\n".join(lines)


def should_inject(row, tool, machine, now=None, max_hours=HANDOFF_MAX_HOURS):
    """Injeta no início da sessão só se o último trabalho no projeto foi em OUTRA ferramenta
    ou máquina, e recente. Mesma ferramenta e máquina: o recall normal já cobre."""
    if not row:
        return False
    if row.get("tool") == tool and row.get("machine") == machine:
        return False
    age = _hours_since(row.get("ended_at") or row.get("started_at"), now)
    return age is not None and age <= max_hours


def tool_from_payload(payload):
    """Qual ferramenta disparou o hook: o transcript do Codex fica em ~/.codex/sessions."""
    path = (payload or {}).get("transcript_path") or ""
    return "codex" if "/.codex/" in path else "claude-code"
