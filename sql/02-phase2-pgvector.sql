-- agent-memory-hub — Phase 2: semantic search (pgvector + gte-small)
-- Run in the Supabase SQL Editor (or via psql) after 01-schema.sql.

create extension if not exists vector;

alter table public.sessions add column if not exists embedding vector(384);  -- gte-small = 384 dims

create index if not exists sessions_embedding_idx
  on public.sessions using hnsw (embedding vector_cosine_ops);

-- Similarity search RPC. Called with the secret key (bypasses RLS).
create or replace function public.match_sessions(
  query_embedding vector(384),
  match_count int default 5,
  filter_project text default null
)
returns table (
  id uuid, session_id text, tool text, machine text, project text,
  started_at timestamptz, content text, similarity float
)
language sql stable
as $$
  select s.id, s.session_id, s.tool, s.machine, s.project, s.started_at, s.content,
         1 - (s.embedding <=> query_embedding) as similarity
  from public.sessions s
  where s.embedding is not null
    and (filter_project is null or s.project = filter_project)
  order by s.embedding <=> query_embedding
  limit match_count;
$$;
