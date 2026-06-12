-- agent-memory-hub — Phase 5 (optional): find near-duplicate fact pairs.
-- Used by scripts/consolidate_facts.py as a MANUAL, reviewed tool. Not automated:
-- pruning memory reliably is hard, and an LLM judge can over-merge useful specifics,
-- so a human should approve supersessions (which are non-destructive anyway).

create or replace function public.find_fact_dupes(min_sim float default 0.80)
returns table (
  a_id uuid, a_fact text, b_id uuid, b_fact text, scope text, similarity float
)
language sql stable
as $$
  select a.id, a.fact, b.id, b.fact, a.scope,
         1 - (a.embedding <=> b.embedding) as similarity
  from public.facts a
  join public.facts b
    on a.id <> b.id
   and coalesce(a.scope, '') = coalesce(b.scope, '')
   and a.valid_until is null and b.valid_until is null
   and a.embedding is not null and b.embedding is not null
   and a.created_at > b.created_at
  where 1 - (a.embedding <=> b.embedding) >= min_sim
  order by similarity desc;
$$;
