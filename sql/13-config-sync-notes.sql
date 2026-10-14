-- agent-memory-hub — config sync phase 2 (hooks, permissions)
--
-- note: why an approved item is not active on that target yet, written by the machine's
--       sync (e.g. "falta dependência: ~/.claude/scripts/x.sh", "Codex: aprovar em /hooks").
-- retired_at: the source no longer has this hook/permission. Targets keep it until the item
--       is rejected for them (then the sync removes what it installed).

alter table public.config_assignments add column if not exists note text;
alter table public.config_items add column if not exists retired_at timestamptz;
