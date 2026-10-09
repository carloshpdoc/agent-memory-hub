-- agent-memory-hub — facts verified against the code (scripts/verify_facts.py, nightly)
-- Facts that cite repo files ("the filter screen lives in Filters/.../FilterSelection.swift")
-- go stale when code moves. The nightly checks each cited file in the project's local clone
-- and stores the result here; recall flags and deprioritises facts whose files are gone.
-- Non-destructive: nothing is invalidated, and a file that comes back clears the flag.
--   {"checked_at": iso, "head": "<sha>", "refs": n, "missing": ["path", ...]}
alter table public.facts add column if not exists code_check jsonb;
