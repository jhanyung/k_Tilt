-- Milestone 2, step 5: who is quoted. Article text is fetched, analysed in memory and
-- discarded; only the speaker, their role and the first sentence of each quote are kept.

alter table articles add column if not exists analysed_at    timestamptz;
alter table articles add column if not exists analyse_status text;
comment on column articles.analyse_status is
  'ok | empty | refused | unreachable | http NNN | too large | error';

create table if not exists quotes (
  id          bigint generated always as identity primary key,
  article_id  bigint not null references articles(id) on delete cascade,
  seq         int not null,                  -- order within the article
  speaker     text not null,                 -- as shown: "국민의힘 정점식 원내대표"
  name        text not null default '',
  title       text not null default '',
  org         text not null default '',
  role        text not null,                 -- 정부 정당 지자체 전문가 시민 단체 기업 사법·수사 언론 해외 기타 미상
  party       text not null default '',
  bloc        text not null default '',      -- ruling | opposition | ''
  quote       text not null check (char_length(quote) <= 200),
  method      text not null default 'rule',  -- rule | model
  flags       text[] not null default '{}',
  created_at  timestamptz not null default now(),
  unique (article_id, seq)
);
create index if not exists quotes_article_idx on quotes (article_id);

alter table quotes enable row level security;
