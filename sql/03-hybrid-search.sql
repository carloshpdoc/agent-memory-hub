-- agent-memory-hub — Phase 3: hybrid search (full-text + semantic via RRF)
-- Run after 01-schema.sql and 02-phase2-pgvector.sql.
--
-- Combines keyword (tsvector) and meaning (pgvector) ranking with Reciprocal
-- Rank Fusion, so exact terms that vector search misses still surface, and
-- semantic matches that keyword search misses still surface.

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
  vec as (
    select s.id,
           row_number() over (order by s.embedding <=> query_embedding) as rank
    from public.sessions s
    where s.embedding is not null
      and (filter_project is null or s.project = filter_project)
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
