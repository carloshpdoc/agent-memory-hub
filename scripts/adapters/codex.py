#!/usr/bin/env python3
"""
agent-memory-hub — capture adapter for Codex CLI.

Scans ~/.codex/sessions/**/rollout-*.jsonl, parses each into the shared session
shape, and upserts to Supabase (tool='codex'). Idempotent (skips sessions already
present). Reuses the same summary logic as the Claude Code hook, so recall/search
treat Codex sessions uniformly.

This is the template for adding more tools: locate the tool's local transcripts,
map them to (session_id, cwd, user/assistant turns), and upsert. Run on a cron.

Usage:
  python3 scripts/adapters/codex.py --dry-run
  python3 scripts/adapters/codex.py

Config (env or ../../.env): SUPABASE_URL, SUPABASE_SECRET_KEY.
"""
import glob
import json
import os
import socket
import sys
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))   # scripts/adapters -> repo root
sys.path.insert(0, os.path.join(REPO, "hooks"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
from store import config, get_store  # noqa: E402
from capture_session import build_summary, sanitize_text, strip_nul, INTERNAL_PROMPT_MARKER  # noqa: E402
from project_key import project_key  # noqa: E402
from usage import codex_usage  # noqa: E402

SESSIONS = os.path.join(HOME, ".codex", "sessions")
TOOL = "codex"
MAX_CONTENT = 5_000_000


def session_meta(path):
    """payload do session_meta (nas primeiras linhas do rollout), sem ler o arquivo inteiro; {} se não houver."""
    try:
        with open(path) as f:
            for _, raw in zip(range(5), f):
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "session_meta":
                    return d.get("payload") or {}
    except OSError:
        pass
    return {}


def git_from_meta(meta):
    """metadata.git no formato da captura do Claude (handoff, mem cost). O Codex não registra os
    arquivos alterados: dirty_count None, e o handoff não afirma working tree limpa."""
    g = meta.get("git") or {}
    if not g.get("branch"):
        return None
    return {"branch": g["branch"], "head": (g.get("commit_hash") or "")[:7], "dirty": [], "dirty_count": None}


def parse(path):
    sid = cwd = first_ts = last_ts = None
    user_texts, lines, n_user, n_assistant = [], [], 0, 0
    try:
        for raw in open(path):
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
            if t == "session_meta":
                sid = sid or p.get("id")
                cwd = cwd or p.get("cwd")
            elif t == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = " ".join(
                    b.get("text", "") for b in (p.get("content") or [])
                    if b.get("type") in ("input_text", "output_text") and b.get("text")
                ).strip()
                if not text:
                    continue
                text = sanitize_text(text)   # mesma mascara de segredos/<private> da captura
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                if role == "user":
                    n_user += 1
                    user_texts.append(text)
                    lines.append(f"[user]\n{text}")
                else:
                    n_assistant += 1
                    lines.append(f"[assistant]\n{text}")
    except OSError:
        return None
    content = "\n\n".join(lines)
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n\n[...truncado...]"
    return sid, cwd, content, user_texts, n_user, n_assistant, first_ts, last_ts


# Rollouts gravados antes do INTERNAL_PROMPT_MARKER existir: reconhece pelo inicio dos
# prompts do hub (extract, supersede, consolidate, defrag, profile, enforce, rerank).
# Sem isto, apagar o lixo do banco faria o adapter reimportar tudo de ~/.codex/sessions.
LEGACY_INTERNAL_PREFIXES = (
    "You extract durable, reusable memory from a coding-assistant session transcript.",
    "Two facts about the same project scope. Does the NEW fact make the OLD one obsolete?",
    "Compare two facts about the same project.",
    "You judge whether a stored memory fact is still worth recalling.",
    "You are profiling ONE developer from durable facts",
    "You turn a human code-workflow rule into a mechanical guard",
    "Rank the snippets by relevance to the QUERY",
)


def is_internal(user_texts):
    """Sessao que o proprio hub abriu (codex exec do extract/defrag/profile)."""
    return any(t.lstrip().startswith((INTERNAL_PROMPT_MARKER, *LEGACY_INTERNAL_PREFIXES))
               for t in user_texts)


def build_row(parsed, path, env, meta=None):
    """Linha de `sessions` a partir de um rollout parseado (compartilhado com o hook)."""
    sid, cwd, content, uts, nu, na, fts, lts = parsed
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "session_id": sid, "tool": TOOL, "machine": socket.gethostname(),
        "project": project_key(cwd, {**env, **os.environ}),
        "started_at": fts or now, "ended_at": lts or now,
        "content": content, "summary": build_summary(uts, nu, na),
        "metadata": {"cwd": cwd, "source": "codex", "file": path, **(meta or {})},
    }
    git = git_from_meta(session_meta(path))
    if git:
        row["metadata"]["git"] = git
    usage = codex_usage(path)   # tokens por modelo (custo por tarefa: mem cost)
    if usage:
        row["usage"] = usage
    return row


def main(argv):
    dry = "--dry-run" in argv
    env = config.ENV
    store = get_store()
    if not store.configured:
        print("ERRO: SUPABASE_URL/SECRET_KEY ausentes", file=sys.stderr)
        return 1

    files = sorted(glob.glob(os.path.join(SESSIONS, "**", "rollout-*.jsonl"), recursive=True))
    seen = store.session_ids()
    sent = 0
    for f in files:
        parsed = parse(f)
        if not parsed:
            continue
        sid, cwd, content, uts, nu, na, fts, lts = parsed
        if not sid or not content or sid in seen:
            continue
        if is_internal(uts):
            continue
        if dry:
            print(f"  [dry] {sid[:8]}… {project_key(cwd, {**env, **os.environ}) or '-'} ({nu}u/{na}a)")
            sent += 1
            continue
        try:
            store.upsert_session(strip_nul(build_row(parsed, f, env)), timeout=20)
            sent += 1
        except Exception as e:
            print(f"  erro {sid[:8]}: {type(e).__name__}", file=sys.stderr)

    print(f"{'(dry-run) ' if dry else ''}{sent} sessão(ões) do Codex")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
