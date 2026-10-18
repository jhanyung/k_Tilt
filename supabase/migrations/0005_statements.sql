-- Official statements used as reference points for each story: 청와대 and ministry
-- briefings, the government's rebuttals of news reports, party spokesperson statements.
-- Title, link and date only; the text is read in memory during analysis and dropped.

create table if not exists statements (
  id            bigint generated always as identity primary key,
  source        text not null,     -- cheongwadae | ministry | factcheck | minjoo | ppp
  side          text not null,     -- government | party
  party         text not null default '',
  url           text not null unique,
  title         text not null,
  published_on  date,
  first_seen_at timestamptz not null default now()
);
create index if not exists statements_published_idx on statements (published_on desc);

-- Which statements speak to which story, and how closely.
create table if not exists story_statements (
  story_id     bigint not null references stories(id) on delete cascade,
  statement_id bigint not null references statements(id) on delete cascade,
  similarity   real not null,
  primary key (story_id, statement_id)
);

alter table statements       enable row level security;
alter table story_statements enable row level security;
