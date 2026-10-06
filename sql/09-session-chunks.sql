-- agent-memory-hub — chunked session embeddings
-- Run after 03-hybrid-search.sql (this file redefines hybrid_search).
--
-- sessions.embedding covers only the first ~2000 chars of a session (gte-small's window),
-- so anything decided late in a long session was invisible to semantic search. Each session
-- is now split into turn-aware chunks (scripts/embed_pending.py), one vector per chunk; a
-- session's semantic rank is its best chunk. Chunk text is not duplicated: char_start /
-- char_end point into sessions.content.

create table if not exists public.session_chunks (
  session_fk  uuid not null references public.sessions(id) on delete cascade,
  chunk_idx   int  not null,
  char_start  int  not null,
  char_end    int  not null,
  embedding   vector(384) not null,
  primary key (session_fk, chunk_idx)
);

alter table public.session_chunks enable row level security;

-- length(content) at the time the session was chunked. Capture upserts the same row every
-- turn, so a session whose content grew since is chunked again.
alter table public.sessions add column if not exists chunked_len int;

create or replace function public.sessions_pending_chunks(max_rows int default 5)
returns table (id uuid, content text, content_len int)
language sql stable
as $$
  select s.id, s.content, length(s.content)
  from public.sessions s
  where s.chunked_len is distinct from length(s.content)
  order by s.started_at
  limit max_rows;
$$;

-- Same signature as 03; only the `vec` side changed: a session ranks by its closest vector
-- among its chunks and its legacy whole-session embedding (sessions not chunked yet still
-- rank as before). Exact scan on purpose: an ANN pre-limit would drop project-filtered hits.
-- The project filter is applied after aggregating: joined before, the generic plan (SQL
-- functions are planned with parameters) misestimated it and nested-looped every chunk.
create or replace function public.hybrid_search(
  query_text text,
  query_embedding vector(384),
  match_count int default 5,
  filter_project text default null,
  rrf_k int default 50,
  pool int default 30
)
returns table (
  id uuid, session_id text, tool text, machine text, project text,
  started_at timestamptz, content text,
  score float, fts_rank int, vec_rank int
)
language sql stable
as $$
  with fts as (
    select s.id,
           row_number() over (
             order by ts_rank(s.content_tsv, websearch_to_tsquery('simple', query_text)) desc
           ) as rank
    from public.sessions s
    where query_text is not null and query_text <> ''
      and s.content_tsv @@ websearch_to_tsquery('simple', query_text)
      and (filter_project is null or s.project = filter_project)
    limit pool
  ),
  vec_candidates as (
    select c.session_fk as id, c.embedding <=> query_embedding as dist
    from public.session_chunks c
    union all
    select s.id, s.embedding <=> query_embedding
    from public.sessions s
    where s.embedding is not null
  ),
  vec_best as (
    select v.id, min(v.dist) as dist
    from vec_candidates v
    group by v.id
  ),
  vec as (
    select b.id, row_number() over (order by b.dist) as rank
    from vec_best b
    join public.sessions s on s.id = b.id
    where filter_project is null or s.project = filter_project
    order by b.dist
    limit pool
  )
  select s.id, s.session_id, s.tool, s.machine, s.project, s.started_at, s.content,
         coalesce(1.0 / (rrf_k + fts.rank), 0.0)
       + coalesce(1.0 / (rrf_k + vec.rank), 0.0) as score,
         fts.rank::int as fts_rank,
         vec.rank::int as vec_rank
  from public.sessions s
  left join fts on fts.id = s.id
  left join vec on vec.id = s.id
  where fts.id is not null or vec.id is not null
  order by score desc
  limit match_count;
$$;
