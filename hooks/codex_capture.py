#!/usr/bin/env python3
"""
agent-memory-hub — native capture hook for Codex CLI (Stop, every turn).

Codex hooks get the same stdin shape as Claude Code's (session_id, transcript_path, cwd,
...), but transcript_path points at a Codex rollout, so this reuses the Codex adapter's
parser and row builder instead of capture_session's Claude parser. Upsert by session_id:
each turn refreshes the same row, and the adapter (`mem import` / nightly) converges on it.

Never fails the turn: any problem is logged to hooks/capture.log and exit code is 0.
Wired by scripts/install_hooks.py into ~/.codex/hooks.json; Codex skips it until you
trust it in `/hooks`.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts", "adapters"))

from capture_session import ENV_PATH, load_env, log  # noqa: E402
import codex  # noqa: E402  (parse, is_internal, build_row, upsert)


def main():
    if os.environ.get("AMH_NO_CAPTURE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin, strict=False)
    except Exception as e:
        log(f"codex: stdin invalido: {e}")
        return 0
    path = payload.get("transcript_path")
    if not path or not os.path.isfile(path):
        log(f"codex: transcript ausente: {path!r}")
        return 0

    parsed = codex.parse(path)
    if not parsed:
        return 0
    sid, _cwd, content, uts, nu, na, _fts, _lts = parsed
    if not sid or not content or codex.is_internal(uts):
        return 0

    env = load_env(ENV_PATH)
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        log("codex: SUPABASE_URL/SECRET_KEY ausentes no .env")
        return 0
    meta = {"hook_reason": payload.get("hook_event_name") or "Stop"}
    try:
        codex.upsert(url, key, codex.build_row(parsed, path, env, meta), timeout=15)
        log(f"OK codex sessao {sid} salva ({nu}u/{na}a, {len(content)} chars)")
    except Exception as e:
        log(f"codex: erro ao salvar {sid}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
