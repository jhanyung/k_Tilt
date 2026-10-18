-- Partisan vocabulary learned from official statements (statements table): how often each
-- speaker group (정부, each party) uses a word, and the words that set each group apart.
-- Only word counts are kept, never statement text.

alter table statements add column if not exists counted_at timestamptz;

create table if not exists lexicon_counts (
  grp   text not null,           -- 정부 | party name
  term  text not null,
  n     int  not null default 0, -- occurrences
  docs  int  not null default 0, -- statements using it
  primary key (grp, term)
);

create table if not exists lexicon (
  grp        text not null,
  term       text not null,
  z          real not null,      -- log-odds z-score against the other groups
  docs       int  not null,
  updated_at timestamptz not null default now(),
  primary key (grp, term)
);

alter table lexicon_counts enable row level security;
alter table lexicon        enable row level security;
