"""Database connection. SUPABASE_DB_URL must be the Supabase *session pooler* string,
because GitHub's runners have no IPv6 route to the direct database host."""

from __future__ import annotations

import os

import psycopg


def connect() -> psycopg.Connection:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("SUPABASE_DB_URL is not set (GitHub Secret).")
    # prepare_threshold=None keeps us compatible with Supabase's poolers.
    return psycopg.connect(url, prepare_threshold=None, connect_timeout=20)
