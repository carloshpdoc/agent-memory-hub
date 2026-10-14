-- agent-memory-hub — the end of a session's main conversation, cut server-side
--
-- The SessionStart handoff (hooks/handoff.py) only needs the last user ask and the agent's
-- last reply of the MAIN thread. capture_session appends subagent transcripts after it
-- ("\n\n--- subagent <id> ---"), and long sessions reach megabytes, so recall used to
-- download the whole content to read its last few kilobytes.

create or replace function public.session_main_tail(p_session_id text, p_chars int default 20000)
returns text
language sql stable
as $$
  select right(split_part(s.content, E'\n\n--- subagent ', 1), p_chars)
  from public.sessions s
  where s.session_id = p_session_id;
$$;
