-- agent-memory-hub — token usage per session (cost per task)
--
-- Tokens per model, written at capture (hooks/usage.py): Claude Code and Codex record them,
-- Cursor's local store does not. Dollars are computed when reporting (mem cost), from the
-- public price table, so a price change never rewrites rows.
alter table public.sessions add column if not exists usage jsonb;
