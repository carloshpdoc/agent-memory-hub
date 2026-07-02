# tests

Fast, offline unit tests for the capture pipeline and the Cursor adapter. No network,
no Supabase, no secrets — they exercise the pure logic (parsing, sanitizing, summarizing,
conversation reconstruction). CI runs them on every push and PR (`.github/workflows/ci.yml`).

## Run

```bash
python3 -m pip install pytest      # the only test dependency
python3 -m pytest tests/ -q
```

## What's covered

- **`test_capture_session.py`** — pins the three silent-capture bugs so they can't return:
  control characters in the payload (json `strict=False`), NUL-byte stripping (recursive),
  and backslash/ANSI survival. Plus `extract_text`, `clean_user_text`, `build_summary`
  (theme + arc + `(Nq/Nr)` counts, skipping injected context), and `parse_transcript`.
- **`test_cursor_adapter.py`** — reconstructing a conversation from a cursorDiskKV-shaped
  SQLite DB (ordering, roles, project from `workspaceUris`, URL-decoding, the no-workspace
  fallback), and the `ms_to_iso` / `age_seconds` / `cursor_db` helpers.
