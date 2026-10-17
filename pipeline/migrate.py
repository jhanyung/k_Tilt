"""Apply supabase/migrations/*.sql in order, each once, each in its own transaction."""

from __future__ import annotations

import sys
from pathlib import Path

from pipeline.db import connect

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


def main() -> int:
    files = sorted(MIGRATIONS.glob("*.sql"))
    with connect() as conn:
        conn.execute(
            "create table if not exists schema_migrations ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
        conn.execute("alter table schema_migrations enable row level security")
        conn.commit()
        done = {r[0] for r in conn.execute("select name from schema_migrations")}
        for f in files:
            if f.name in done:
                continue
            print(f"applying {f.name}")
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (name) values (%s)", (f.name,))
    print(f"migrations up to date ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
