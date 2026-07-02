# Contributing

Thanks for wanting to help. `agent-memory-hub` is a small, dependency-light tool with a
clear stance — **memory you can explain, own, and gate** — so contributions are judged less
by size and more by whether they keep that stance intact.

## Principles (read these first)

- **Stdlib-first in the core.** The hooks, adapters, MCP server, console, and backup are pure
  Python stdlib on purpose — they must run on a fresh machine with no `pip install`. Anything
  that needs an external library must be **optional** and behind its own flag (like the facts
  layer's `FACTS_LLM`), never a hard dependency of capture/recall.
- **Human-gated.** Nothing that changes how the agent behaves (profile rules, fact merges) is
  applied automatically. If your change proposes something, it proposes it for review.
- **Explainable.** Recall carries provenance (confidence, age, `session_id`). Keep it that way.
- **Idempotent + safe.** Capture upserts by `session_id`. Adapters must be safe to re-run and
  must never crash a live session — errors are logged, not raised at the user.

## Dev setup

```bash
git clone https://github.com/carloshpdoc/agent-memory-hub.git
cd agent-memory-hub
cp .env.example .env          # fill SUPABASE_URL + SUPABASE_SECRET_KEY
./scripts/setup.sh            # migrations + hooks (idempotent)
```

## Tests

Fast, offline, no secrets. Run them before opening a PR:

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -q
```

CI runs the same suite on every push and PR (`.github/workflows/ci.yml`, Python 3.10 + 3.12).
If you touch capture/parsing/summary or an adapter, add or update a test. The suite exists
specifically to keep the three silent-capture bugs (control chars, NUL byte, `echo`→`printf`)
from ever coming back.

## Writing a capture adapter (the most wanted contribution)

Tools without lifecycle hooks are captured by an **adapter** that scans their local transcripts
and upserts them (idempotent), so recall/search/facts treat every tool uniformly. Two templates
ship:

- **`scripts/adapters/codex.py`** — for tools that store transcripts as JSONL files.
- **`scripts/adapters/cursor.py`** — for tools that store chat in a SQLite DB.

To add one, map the tool's transcripts to `(session_id, cwd, ordered user/assistant turns)` and
upsert with `tool=<name>`. Reuse `build_summary` from `hooks/capture_session.py` so summaries
match. Contract for each row:

```python
{
  "session_id": ...,           # stable, unique id from the tool
  "tool": "<name>",            # e.g. "gemini", "windsurf"
  "machine": socket.gethostname(),
  "project": os.path.basename(cwd) or "root",
  "started_at": ..., "ended_at": ...,   # ISO 8601
  "content": "[user]\n...\n\n[assistant]\n...",
  "summary": build_summary(user_texts, n_user, n_assistant),
  "metadata": {"cwd": ..., "source": "<name>"},
}
```

Guidelines: support `--dry-run`, skip sessions already present, open the source read-only, and
if the tool writes while you read (like an open editor's SQLite), open it in a way that can't
lock the app (see `cursor.py`'s `mode=ro&immutable=1`). Add a test with a temp fixture like
`tests/test_cursor_adapter.py`, and a bullet in both READMEs.

Good first adapters: **Gemini CLI**, **Windsurf**, **Zed**. See [ROADMAP.md](ROADMAP.md).

## Pull requests

- Keep them focused — one concern per PR.
- Update both `README.md` and `README.pt-br.md` when you change user-facing behavior.
- Make sure `python3 -m pytest tests/ -q` is green.
- Don't commit secrets. `.env` and `~/.pgpass` are gitignored; keep it that way.
