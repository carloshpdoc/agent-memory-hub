-- agent-memory-hub — config sync across machines, approved per machine in the dashboard
--
-- config_items: one row per synced item (phase 1: kind='skill'; later hooks, permissions,
-- MCP servers, guard policy). Text files only, as {"relative/path": "content"}.
-- config_assignments: for every item x machine, whether it may be installed there.
-- The machine an item comes from approves it implicitly; every other machine starts
-- 'pending' until approved in the dashboard (scripts/config_sync.py never installs
-- anything that is not 'approved' for its own hostname).

create table if not exists public.config_items (
  id              uuid primary key default gen_random_uuid(),
  kind            text not null,
  name            text not null,
  files           jsonb not null,
  hash            text not null,                 -- sha256 of the normalised files
  source_machine  text not null,
  updated_at      timestamptz not null default now(),
  unique (kind, name)
);

create table if not exists public.config_assignments (
  item_id         uuid not null references public.config_items(id) on delete cascade,
  machine         text not null,
  status          text not null default 'pending'
                  check (status in ('pending', 'approved', 'rejected')),
  installed_hash  text,                          -- what this machine has, reported by sync
  decided_at      timestamptz,
  installed_at    timestamptz,
  primary key (item_id, machine)
);

alter table public.config_items enable row level security;
alter table public.config_assignments enable row level security;
