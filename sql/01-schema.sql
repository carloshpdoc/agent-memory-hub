-- agent-memory-hub — schema Fase 1 (tabela sessions + full-text + RLS)
-- Rodar no Supabase: painel → SQL Editor → cole e execute.

create table if not exists public.sessions (
  id           uuid primary key default gen_random_uuid(),
  tool         text not null,            -- 'claude-code', 'cursor', 'codex'...
  machine      text not null,            -- hostname da máquina
  project      text,                     -- repo/dir de trabalho, se houver
  started_at   timestamptz not null default now(),
  ended_at     timestamptz,
  content      text not null,            -- transcript / resumo da sessão
  metadata     jsonb not null default '{}'::jsonb
);

-- busca full-text (Fase 1, sem embeddings)
alter table public.sessions
  add column if not exists content_tsv tsvector
  generated always as (to_tsvector('simple', coalesce(content, ''))) stored;

create index if not exists sessions_content_tsv_idx on public.sessions using gin (content_tsv);
create index if not exists sessions_started_at_idx  on public.sessions (started_at desc);
create index if not exists sessions_tool_idx        on public.sessions (tool);
create index if not exists sessions_project_idx     on public.sessions (project);

-- RLS: protege os dados.
-- A service_role (secret key, usada pelos hooks) ignora RLS → escreve/lê normalmente.
-- A publishable key (anon) NÃO lê nada sem política → seguro por padrão.
alter table public.sessions enable row level security;
