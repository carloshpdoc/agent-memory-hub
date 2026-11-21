#!/usr/bin/env python3
"""
agent-memory-hub — native capture hook for the Cursor CLI (`agent`), on `stop` (every turn).

Cursor hooks get JSON on stdin with `conversation_id` and `workspace_roots`; the chat itself
lives in ~/.cursor/chats/<md5(workspace)>/<conversation_id>/, which the Cursor CLI adapter
(scripts/adapters/cursor_cli.py) knows how to read. Upsert by session id: each turn
refreshes the same row, with the git state of the moment (what `amh handoff` and
`amh continuar` show), and the adapter's backfill (`amh import` / nightly) converges on it.

The IDE fires the same hook, but its chats are not in ~/.cursor/chats: no chat found means
nothing to do here (the IDE is covered by scripts/adapters/cursor.py).

Never fails the turn: problems go to hooks/capture.log, exit code is always 0, and the
upload runs detached (fork + setsid), like the Codex hook.
Wired by scripts/install_hooks.py into ~/.cursor/hooks.json.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts", "adapters"))

from capture_session import config, get_store, log  # noqa: E402
from codex_capture import detach  # noqa: E402
from handoff import git_snapshot  # noqa: E402
import cursor_cli  # noqa: E402


def main():
    if os.environ.get("AMH_NO_CAPTURE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin, strict=False)
    except Exception as e:
        log(f"cursor-cli: stdin invalido: {e}")
        return 0
    if not detach("AMH_CURSOR_FOREGROUND"):
        return 0
    store = get_store()
    if not store.configured:
        log("cursor-cli: SUPABASE_URL/SECRET_KEY ausentes no .env")
        return 0
    cwd = next(iter(payload.get("workspace_roots") or []), None) or payload.get("cwd")
    try:
        row = cursor_cli.capture_from_hook(store, payload, config.ENV, git_snapshot(cwd) if cwd else None)
        if row:
            log(f"OK cursor-cli sessao {row['session_id']} salva ({len(row['content'])} chars)")
    except Exception as e:
        log(f"cursor-cli: erro ao salvar: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
