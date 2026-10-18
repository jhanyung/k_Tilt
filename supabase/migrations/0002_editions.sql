-- Milestone 2: editions (morning/evening), candidate stories and their articles,
-- outlet access status, and 30-day retention for article metadata.

alter table outlets add column if not exists access text not null default 'ok';
comment on column outlets.access is
  'ok | relay | refused (robots.txt opts out; shown as 수집거부) | unavailable';

create index if not exists articles_first_seen_idx on articles (first_seen_at);

create table if not exists editions (
  id           bigint generated always as identity primary key,
  edition_date date not null,
  slot         text not null check (slot in ('am', 'pm')),
  window_end   timestamptz not null,
  created_at   timestamptz not null default now(),
  params       jsonb not null default '{}'::jsonb,
  unique (edition_date, slot)
);

create table if not exists stories (
  id           bigint generated always as identity primary key,
  edition_id   bigint not null references editions(id) on delete cascade,
  rank         int not null,            -- candidate rank from the rule-based pass
  score        real not null,
  outlet_count int not null,
  headline     text not null,           -- most central headline, used until a card exists
  keywords     text[] not null default '{}',
  status       text not null default 'candidate'
               check (status in ('candidate', 'published', 'merged', 'dropped')),
  merged_into  bigint references stories(id),
  card         jsonb,                    -- AI-written card (Milestone 2, step 8)
  unique (edition_id, rank)
);

create table if not exists story_articles (
  story_id   bigint not null references stories(id) on delete cascade,
  article_id bigint not null references articles(id),
  sub_event  int not null,               -- 0 = largest angle
  primary key (story_id, article_id)
);
create index if not exists story_articles_article_idx on story_articles (article_id);

alter table editions       enable row level security;
alter table stories        enable row level security;
alter table story_articles enable row level security;

-- Keep 30 days of headline metadata, except articles that belong to a story.
create or replace function prune_articles(keep_days int default 30) returns int
language sql as $$
  with gone as (
    delete from articles a
    where a.first_seen_at < now() - make_interval(days => keep_days)
      and not exists (select 1 from story_articles s where s.article_id = a.id)
    returning 1
  )
  select count(*)::int from gone;
$$;
