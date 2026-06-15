-- agent-memory-hub — Phase 9: developer profile / self-improving rules (OPTIONAL, bring-your-own-LLM)
-- Run after the earlier migrations. The core product does not need this.
--
-- scripts/synthesize_profile.py reads the durable facts across ALL projects and asks an
-- LLM to distill higher-order patterns about how the developer works (recurring across 2+
-- projects). High-confidence patterns get a proposed CLAUDE.md rule. You review (approve/
-- reject) via memory.py, and apply_profile_rules.py writes the approved rules to a separate
-- file imported by CLAUDE.md. Set FACTS_LLM in .env to enable; default is off.

create table if not exists public.profile_patterns (
  id            uuid primary key default gen_random_uuid(),
  pattern       text not null,                    -- one self-contained statement about the dev
  category      text not null default 'preference', -- preference | recurring_fix | tooling_habit | anti_pattern | workflow
  evidence      jsonb not null default '{}',      -- { "projects": [...] }
  confidence    real not null default 0.5,
  status        text not null default 'proposed', -- proposed | approved | rejected
  proposed_rule text,                             -- candidate CLAUDE.md rule (null if low-confidence)
  created_at    timestamptz not null default now(),
  reviewed_at   timestamptz
);

create index if not exists profile_patterns_status_idx on public.profile_patterns (status);

alter table public.profile_patterns enable row level security;
