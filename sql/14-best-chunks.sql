-- agent-memory-hub — the passages of each session closest to a query (for `mem ask`)
--
-- hybrid_search ranks whole sessions; to answer a question the LLM needs the parts that
-- matter, not megabytes of transcript. Returns up to per_session chunks per session, by
-- vector distance, with the text cut server-side from sessions.content (char_start is
-- 0-based, as written by scripts/embed_pending.py).

create or replace function public.best_chunks(
  query_embedding vector(384),
  p_ids uuid[],
  per_session int default 2
)
returns table (session_fk uuid, chunk_idx int, text text, dist float)
language sql stable
as $$
  select x.session_fk, x.chunk_idx, x.text, x.dist
  from (
    select c.session_fk, c.chunk_idx,
           substr(s.content, c.char_start + 1, c.char_end - c.char_start) as text,
           c.embedding <=> query_embedding as dist,
           row_number() over (partition by c.session_fk order by c.embedding <=> query_embedding) as rn
    from public.session_chunks c
    join public.sessions s on s.id = c.session_fk
    where c.session_fk = any(p_ids)
  ) x
  where x.rn <= per_session
  order by x.session_fk, x.chunk_idx;
$$;

-- hybrid_search returns each session's whole content (megabytes for 15 sessions) while
-- callers that rank (recall, mem ask) only read its start. Same ranking, content cut here.
create or replace function public.hybrid_search_head(
  query_text text,
  query_embedding vector(384),
  match_count int default 5,
  filter_project text default null,
  head_chars int default 2000
)
returns table (
  id uuid, session_id text, tool text, machine text, project text,
  started_at timestamptz, content text,
  score float, fts_rank int, vec_rank int
)
language sql stable
as $$
  select h.id, h.session_id, h.tool, h.machine, h.project, h.started_at, left(h.content, head_chars),
         h.score, h.fts_rank, h.vec_rank
  from public.hybrid_search(query_text, query_embedding, match_count, filter_project) h;
$$;
