-- agent-memory-hub — promote procedure facts to Claude Code skills, approved in the dashboard
--
-- A fact of kind='procedure' is a candidate skill. The dashboard's Procedures tab records the
-- human decision here; `amh skills --approved --write` then writes a SKILL.md only for the
-- approved ones, on whatever machine runs it. Propagation to other machines stays with
-- config_items/config_assignments (the Sync tab), once the file exists. One row per fact;
-- the default (no row) is 'pending'.

create table if not exists public.skill_promotions (
  fact_id     uuid primary key references public.facts(id) on delete cascade,
  status      text not null default 'pending'
              check (status in ('pending', 'approved', 'rejected')),
  decided_at  timestamptz
);

alter table public.skill_promotions enable row level security;
