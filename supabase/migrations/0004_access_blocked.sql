-- 접속차단: the outlet's server turns the crawler away (HTTP 401/403) from every route we
-- have. Kept apart from 수집거부, which means robots.txt opts out.
comment on column outlets.access is
  'ok | relay | refused (robots.txt opts out; 수집거부) | blocked (HTTP 403; 접속차단) | unavailable';
