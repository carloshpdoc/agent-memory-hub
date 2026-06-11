-- agent-memory-hub — Phase 4: facts/preferences layer (OPTIONAL, bring-your-own-LLM)
-- Run after the earlier migrations. The core product does not need this.
--
-- An optional cron (scripts/extract_facts.py) asks an LLM to distill each session
-- into durable, atomic facts (preferences, decisions, configs) with temporal validity,
-- which recall then injects. Set FACTS_LLM in .env to enable; default is off.

create table if not exists public.facts (
  id                uuid primary key default gen_random_uuid(),
  fact              text not null,                 -- atomic statement
  kind              text not null default 'fact',  -- preference | decision | config | fact
  scope             text,                          -- project, or null = global
  source_session_id text,
  machine           text,
  embedding         vector(384),
  valid_from        timestamptz not null default now(),
  valid_until       timestamptz,                   -- null = currently valid (temporal model)
  superseded_by     uuid references public.facts(id) on delete set null,
  confidence        real not null default 0.7,
  created_at        timestamptz not null default now()
);

create index if not exists facts_embedding_idx on public.facts using hnsw (embedding vector_cosine_ops);
create index if not exists facts_scope_valid_idx on public.facts (scope) where valid_until is null;
create index if not exists facts_session_idx on public.facts (source_session_id);

alter table public.facts enable row level security;

-- avoid re-extracting the same session
alter table public.sessions add column if not exists facts_extracted_at timestamptz;

-- semantic search over currently-valid facts (on demand)
create or replace function public.match_facts(
  query_embedding vector(384),
  match_count int default 8,
  filter_scope text default null
)
returns table (id uuid, fact text, kind text, scope text, confidence real, similarity float)
language sql stable
as $$
  select f.id, f.fact, f.kind, f.scope, f.confidence,
         1 - (f.embedding <=> query_embedding) as similarity
  from public.facts f
  where f.valid_until is null and f.embedding is not null
    and (filter_scope is null or f.scope = filter_scope or f.scope is null)
  order by f.embedding <=> query_embedding
  limit match_count;
$$;
