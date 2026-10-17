# 기울기 (k_Tilt)

A free, fully automatic Korean news community. Twice a day it picks the 10 stories most outlets are covering and shows, in AI-written cards, what each political camp reports differently and whom each side quotes.

## Layout

- `site/` — static files served by Cloudflare Workers (placeholder until the Astro site lands in `web/`)
- `wrangler.jsonc` — Cloudflare deploy config
- `pipeline/` — news ingest and analysis (coming in Milestone 1)
- `supabase/` — database schema and policies (coming in Milestone 4)

## Rules

- No API keys or passwords in this repo. Secrets live in GitHub Secrets and the Supabase dashboard.
- No article bodies are stored or shown; only headlines, links, metrics and short attributed quotes.
- Every AI output is labeled as AI-written.
