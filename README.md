# 기울기 (k_Tilt)

A free, fully automatic Korean news community. Twice a day it picks the 10 stories most outlets are covering and shows, in AI-written cards, what each political camp reports differently and whom each side quotes.

## Layout

| Path | What it is |
| --- | --- |
| `site/` | Static files served by Cloudflare Workers (placeholder until the Astro site lands in `web/`) |
| `wrangler.jsonc` | Cloudflare deploy config |
| `pipeline/outlets.yaml` | Outlet registry (no political labels; positions are measured) |
| `pipeline/discover.py` | Finds each outlet's RSS feed or Google News sitemap → `pipeline/data/feeds.lock.json` |
| `pipeline/ingest.py` | Hourly: headline, link and time for recent items → Supabase |
| `supabase/migrations/` | Database schema, applied by the `migrate` workflow |
| `.github/workflows/` | `ci` (tests, lint, secret scan), `ingest` (hourly), `migrate`, `discover-feeds` (weekly) |

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m pipeline.ingest --dry-run   # fetch only, no database
```

## Secrets (GitHub → Settings → Secrets and variables → Actions)

| Name | Value |
| --- | --- |
| `SUPABASE_DB_URL` | Supabase **Session pooler** connection string, with the database password filled in |

## Rules

- No API keys or passwords in this repo.
- robots.txt is honoured. Outlets that disallow feed access (e.g. MBC, 아시아경제, 미디어오늘) are left out.
- No article bodies are stored or shown; only headlines, links, timestamps, metrics and short attributed quotes.
- Every AI output is labeled as AI-written.
