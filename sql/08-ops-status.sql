-- agent-memory-hub — per-machine operational snapshot (feeds the dashboard)
-- Run anytime. Nothing in the core product reads this table.
--
-- Capture, recall and the nightly job leave their state in local files on each machine
-- (hooks/capture.log, hooks/recall.log, nightly-status.json, ~/.claude/skills). The
-- capture hook condenses them into one row per machine (throttled, see
-- hooks/ops_snapshot.py), so the dashboard sees every machine without reading their disks.

create table if not exists public.ops_status (
  machine     text primary key,
  updated_at  timestamptz not null default now(),
  payload     jsonb not null default '{}'   -- { nightly, capture, recall, skills }
);

alter table public.ops_status enable row level security;
