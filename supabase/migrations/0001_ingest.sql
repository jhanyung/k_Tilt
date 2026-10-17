-- Milestone 1: outlets, article metadata and ingest run log.
-- No article body text is ever stored. Row-level security is on with no policies,
-- so the public API exposes nothing; only the pipeline's direct connection writes here.

create table if not exists outlets (
  id          text primary key,
  name        text not null,
  grp         text not null,
  home        text not null,
  active      boolean not null default true,
  ideology    real,              -- filled by the slant model (Milestone 2)
  power       real,
  confidence  real,
  updated_at  timestamptz not null default now()
);

create table if not exists articles (
  id            bigint generated always as identity primary key,
  outlet_id     text not null references outlets(id),
  url           text not null,
  url_hash      text not null unique,
  title         text not null,
  title_key     text not null,     -- normalised title, for duplicate detection
  published_at  timestamptz,
  first_seen_at timestamptz not null default now(),
  source_kind   text not null,     -- rss | sitemap
  metrics       jsonb not null default '{}'::jsonb
);

create index if not exists articles_published_idx on articles (published_at desc);
create index if not exists articles_outlet_title_idx on articles (outlet_id, title_key, first_seen_at desc);

create table if not exists ingest_runs (
  id              bigint generated always as identity primary key,
  started_at      timestamptz not null,
  finished_at     timestamptz not null default now(),
  outlets_ok      int not null,
  outlets_failed  int not null,
  new_articles    int not null,
  duplicates      int not null,
  errors          jsonb not null default '{}'::jsonb
);

alter table outlets     enable row level security;
alter table articles    enable row level security;
alter table ingest_runs enable row level security;
