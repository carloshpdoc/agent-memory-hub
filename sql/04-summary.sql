-- agent-memory-hub — summary layer (extractive, generated in the capture hook)
-- Run anytime; then run scripts/backfill_summaries.py to populate existing rows.
--
-- The capture hook fills `summary` with a cheap, deterministic digest (first
-- substantive user ask + the last one + turn counts), so recall injects a clean
-- one-liner per session instead of a truncated raw transcript. No LLM involved.

alter table public.sessions add column if not exists summary text;
