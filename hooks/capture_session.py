#!/usr/bin/env python3
"""
agent-memory-hub — hook de captura de sessao.

Disparado pelo Claude Code no evento SessionEnd. Le o transcript .jsonl da sessao,
extrai a conversa (user/assistant) e faz UPSERT na tabela `sessions` do Supabase.

- Pure stdlib (urllib) — nao precisa do .venv.
- Idempotente: upsert por session_id (re-execucoes atualizam a mesma linha).
- Nunca derruba a sessao: qualquer erro vira log + exit 0.

Entrada (stdin, JSON do Claude Code):
  { session_id, transcript_path, cwd, hook_event_name, reason }
"""
import json
import os
import socket
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", ".env")
LOG_PATH = os.path.join(HERE, "capture.log")
TOOL = "claude-code"
MAX_CONTENT_CHARS = 5_000_000  # guarda contra transcripts patologicos


def log(msg):
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def extract_text(content):
    """content pode ser string ou lista de blocks; retorna so o texto."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return ""


NOISE_PREFIXES = (
    "<local-command-caveat>", "<command-name>", "<command-message>",
    "<command-args>", "<system-reminder>", "caveat:", "<bash-",
)


def clean_user_text(t):
    """Remove ruido (caveats, command/system tags) e colapsa espacos."""
    out = []
    for ln in (t or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.lower().startswith(NOISE_PREFIXES) or (s.startswith("<") and s.endswith(">")):
            continue
        out.append(s)
    return " ".join(" ".join(out).split())


INJECTED_PREFIXES = (
    "# agents.md", "<permissions", "# codex", "<system-reminder", "<command-name",
)


def build_summary(user_texts, n_user, n_assistant):
    """Resumo extrativo: 1a pergunta substantiva (tema) + arco + contadores."""
    cleaned = [c for c in (clean_user_text(t) for t in user_texts) if len(c) > 15]
    # pula contexto injetado (AGENTS.md, permissions, reminders) ao escolher o tema
    real = [c for c in cleaned if not c.lower().startswith(INJECTED_PREFIXES)]
    cleaned = real or cleaned
    if not cleaned:
        return None
    parts = [cleaned[0][:240]]
    if len(cleaned) > 1 and cleaned[-1] != cleaned[0]:
        parts.append("[...] " + cleaned[-1][:120])
    return f"{' '.join(parts)}  ({n_user}q/{n_assistant}r)"


def parse_transcript(path):
    """Le o JSONL e devolve (texto, n_user, n_assistant, first_ts, last_ts, user_texts)."""
    lines_out = []
    user_texts = []
    n_user = n_assistant = 0
    first_ts = last_ts = None
    try:
        with open(path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type")
                if etype not in ("user", "assistant"):
                    continue
                ts = entry.get("timestamp")
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                msg = entry.get("message") or {}
                text = extract_text(msg.get("content"))
                if not text:
                    continue  # pula tool_result/tool_use sem texto
                if etype == "user":
                    n_user += 1
                    user_texts.append(text)
                    lines_out.append(f"[user]\n{text}")
                else:
                    n_assistant += 1
                    lines_out.append(f"[assistant]\n{text}")
    except FileNotFoundError:
        log(f"transcript nao encontrado: {path}")
    content = "\n\n".join(lines_out)
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n\n[...truncado...]"
    return content, n_user, n_assistant, first_ts, last_ts, user_texts


def main():
    try:
        payload = json.load(sys.stdin, strict=False)
    except Exception as e:
        log(f"stdin invalido: {e}")
        return 0

    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd") or os.getcwd()
    reason = payload.get("reason")

    if not session_id or not transcript_path:
        log(f"payload incompleto: {payload}")
        return 0

    env = load_env(ENV_PATH)
    url = env.get("SUPABASE_URL")
    key = env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        log("SUPABASE_URL/SECRET_KEY ausentes no .env")
        return 0

    content, n_user, n_assistant, first_ts, last_ts, user_texts = parse_transcript(transcript_path)
    if not content:
        log(f"sessao {session_id} sem conteudo textual; nada a salvar")
        return 0
    summary = build_summary(user_texts, n_user, n_assistant)

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "session_id": session_id,
        "tool": TOOL,
        "machine": socket.gethostname(),
        "project": os.path.basename(cwd.rstrip("/")) or "root",
        "started_at": first_ts or now,
        "ended_at": last_ts or now,
        "content": content,
        "summary": summary,
        "metadata": {
            "cwd": cwd,
            "transcript_path": transcript_path,
            "n_user": n_user,
            "n_assistant": n_assistant,
            "hook_reason": reason,
        },
    }

    body = json.dumps(row).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/rest/v1/sessions?on_conflict=session_id",
        data=body,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"OK sessao {session_id} salva ({n_user}u/{n_assistant}a, "
                f"{len(content)} chars) HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        log(f"HTTPError {e.code} ao salvar {session_id}: {e.read()[:300]}")
    except Exception as e:
        log(f"erro ao salvar {session_id}: {type(e).__name__} {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
