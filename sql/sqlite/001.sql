-- agent-memory-hub — local SQLite schema (MEM_BACKEND=sqlite), mirror of sql/01..15.
-- Ids are TEXT (uuid4 made in Python), JSON columns are TEXT read with json_extract,
-- vectors are float32 BLOBs (the sqlite-vec format), timestamps are ISO-8601 UTC strings
-- normalised by the store so text order is time order. Applied by store/sqlite.py when
-- PRAGMA user_version is below 1.

create table if not exists sessions (
  id                 text primary key,
  session_id         text unique,
  tool               text not null,
  machine            text not null,
  project            text,
  started_at         text not null,
  ended_at           text,
  content            text not null,
  metadata           text not null default '{}',
  summary            text,
  embedding          blob,
  facts_extracted_at text,
  chunked_len        integer,
  usage              text
);
create index if not exists sessions_started_at_idx on sessions (started_at desc);
create index if not exists sessions_ended_at_idx   on sessions (ended_at desc);
create index if not exists sessions_project_idx    on sessions (project);
create index if not exists sessions_tool_idx       on sessions (tool);

-- full-text (o content_tsv do Postgres): tabela FTS5 de conteúdo externo, mantida por triggers
create virtual table if not exists sessions_fts using fts5(
  content, content='sessions', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
create trigger if not exists sessions_fts_ai after insert on sessions begin
  insert into sessions_fts (rowid, content) values (new.rowid, new.content);
end;
create trigger if not exists sessions_fts_ad after delete on sessions begin
  insert into sessions_fts (sessions_fts, rowid, content) values ('delete', old.rowid, old.content);
end;
create trigger if not exists sessions_fts_au after update of content on sessions begin
  insert into sessions_fts (sessions_fts, rowid, content) values ('delete', old.rowid, old.content);
  insert into sessions_fts (rowid, content) values (new.rowid, new.content);
end;

create table if not exists session_chunks (
  session_fk  text not null references sessions (id) on delete cascade,
  chunk_idx   integer not null,
  char_start  integer not null,
  char_end    integer not null,
  embedding   blob not null,
  primary key (session_fk, chunk_idx)
);

create table if not exists facts (
  id                text primary key,
  fact              text not null,
  kind              text not null default 'fact',
  scope             text,
  source_session_id text,
  machine           text,
  embedding         blob,
  valid_from        text not null,
  valid_until       text,
  superseded_by     text references facts (id) on delete set null,
  confidence        real not null default 0.7,
  created_at        text not null,
  code_check        text
);
create index if not exists facts_scope_valid_idx on facts (scope) where valid_until is null;
create index if not exists facts_session_idx     on facts (source_session_id);

create table if not exists profile_patterns (
  id            text primary key,
  pattern       text not null,
  category      text not null default 'preference',
  evidence      text not null default '{}',
  confidence    real not null default 0.5,
  status        text not null default 'proposed',
  proposed_rule text,
  created_at    text not null,
  reviewed_at   text
);
create index if not exists profile_patterns_status_idx on profile_patterns (status);

create table if not exists ops_status (
  machine     text primary key,
  updated_at  text not null,
  payload     text not null default '{}'
);

create table if not exists config_items (
  id              text primary key,
  kind            text not null,
  name            text not null,
  files           text not null,
  hash            text not null,
  source_machine  text not null,
  updated_at      text not null,
  retired_at      text,
  unique (kind, name)
);

create table if not exists config_assignments (
  item_id         text not null references config_items (id) on delete cascade,
  machine         text not null,
  status          text not null default 'pending'
                  check (status in ('pending', 'approved', 'rejected')),
  installed_hash  text,
  decided_at      text,
  installed_at    text,
  note            text,
  primary key (item_id, machine)
);

pragma user_version = 1;
