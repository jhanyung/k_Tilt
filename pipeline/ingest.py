"""Hourly ingest: read every outlet's sources, keep recent headline metadata, store new items.

Usage:
  python -m pipeline.ingest            # write to Supabase (needs SUPABASE_DB_URL)
  python -m pipeline.ingest --dry-run  # fetch and report only, no database
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pipeline import feeds, http

LOCK = Path(__file__).resolve().parent / "data" / "feeds.lock.json"
REGISTRY = Path(__file__).resolve().parent / "outlets.yaml"
KEEP_DAYS = 30
MAX_AGE_H = 72  # ignore items older than this
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|from$|rss|sc_|cmpid)", re.IGNORECASE)
_BRACKETS = re.compile(r"^\s*(\[[^\]]{1,12}\]|【[^】]{1,12}】|<[^>]{1,12}>)\s*")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    query = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)
    ]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), ""))


def title_key(title: str) -> str:
    """Normalised title: drops leading tags like [속보] or [단독], punctuation and spacing."""
    t = unicodedata.normalize("NFKC", title)
    while True:
        stripped = _BRACKETS.sub("", t, count=1)
        if stripped == t:
            break
        t = stripped
    return _NON_WORD.sub("", t).lower()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


@dataclass
class Item:
    outlet_id: str
    url: str
    title: str
    published: float | None
    kind: str


@dataclass
class OutletResult:
    outlet_id: str
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def collect(outlet_id: str, info: dict, now: float) -> OutletResult:
    res = OutletResult(outlet_id)
    site = feeds.registered_domain(info["home"])
    seen: set[str] = set()
    with http.client(via_relay=info.get("via") == "relay") as c:
        for src in info["feeds"]:
            try:
                entries, kind, reason = feeds.fetch_any(src, c)
            except Exception as e:  # one outlet must never stop the run
                entries, kind, reason = [], "", f"error: {type(e).__name__}"
            if reason:
                res.errors.append(f"{src['url']}: {reason}")
                continue
            for e in entries:
                e.published = feeds.fix_kst_mislabel(e.published, now)
                if not e.link.startswith("http") or not e.title:
                    continue
                if feeds.registered_domain(e.link) != site:
                    continue
                if e.published and now - e.published > MAX_AGE_H * 3600:
                    continue
                if e.published and e.published - now > 3600:  # clock nonsense in the future
                    continue
                url = canonical_url(e.link)
                if url in seen:
                    continue
                seen.add(url)
                res.items.append(Item(outlet_id, url, e.title.strip()[:300], e.published, kind))
    return res


def collect_all(lock: dict, workers: int = 12) -> list[OutletResult]:
    now = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(collect, oid, info, now) for oid, info in lock["outlets"].items()]
        return [f.result() for f in futures]


def plan_rows(
    results: list[OutletResult], known_hashes: set[str], recent_keys: set[tuple[str, str]]
) -> tuple[list[tuple], int]:
    """Rows to insert after dropping URLs we already have and titles an outlet already
    published in the last 48 hours. Pure function, so it is unit-tested."""
    rows: list[tuple] = []
    dup = 0
    seen_keys = set(recent_keys)
    seen_hashes = set(known_hashes)
    for res in results:
        for item in res.items:
            h = sha1(item.url)
            key = title_key(item.title)
            if h in seen_hashes or (item.outlet_id, key) in seen_keys:
                dup += 1
                continue
            seen_hashes.add(h)
            seen_keys.add((item.outlet_id, key))
            published = datetime.fromtimestamp(item.published, UTC) if item.published else None
            rows.append((item.outlet_id, item.url, h, item.title, key, published, item.kind))
    return rows, dup


def outlet_rows(lock: dict) -> list[tuple]:
    """Every registry outlet with its access status, so refusals can be shown (수집거부)."""
    import yaml

    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["outlets"]
    rows = []
    for o in registry:
        oid = o["id"]
        if oid in lock["outlets"]:
            access = "relay" if lock["outlets"][oid].get("via") == "relay" else "ok"
            home = lock["outlets"][oid]["home"]
        else:
            # 수집거부 only when discovery found robots.txt refusing the homepage itself.
            access = "refused" if oid in lock.get("refused", []) else "unavailable"
            home = o["home"]
        rows.append((oid, o["name"], o["group"], home, access))
    return rows


def store(results: list[OutletResult], lock: dict, started: datetime) -> dict:
    """A handful of round trips regardless of volume: the database is in Seoul and the
    runner usually is not, so per-row statements would take tens of minutes."""
    from pipeline.db import connect

    hashes = [sha1(i.url) for r in results for i in r.items]
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            "insert into outlets (id, name, grp, home, access) values (%s, %s, %s, %s, %s) "
            "on conflict (id) do update set name = excluded.name, grp = excluded.grp, "
            "home = excluded.home, access = excluded.access, updated_at = now()",
            outlet_rows(lock),
        )
        cur.execute("select url_hash from articles where url_hash = any(%s)", (hashes,))
        known = {r[0] for r in cur.fetchall()}
        cur.execute(
            "select outlet_id, title_key from articles "
            "where first_seen_at > now() - interval '48 hours'"
        )
        recent = {(r[0], r[1]) for r in cur.fetchall()}
        rows, dup = plan_rows(results, known, recent)

        cur.execute(
            "create temp table incoming (outlet_id text, url text, url_hash text, title text, "
            "title_key text, published_at timestamptz, source_kind text) on commit drop"
        )
        with cur.copy(
            "copy incoming (outlet_id, url, url_hash, title, title_key, published_at, "
            "source_kind) from stdin"
        ) as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(
            "insert into articles (outlet_id, url, url_hash, title, title_key, published_at, "
            "source_kind) select outlet_id, url, url_hash, title, title_key, published_at, "
            "source_kind from incoming on conflict (url_hash) do nothing"
        )
        new = cur.rowcount
        dup += len(rows) - new

        ok = sum(1 for r in results if r.items)
        errors = {r.outlet_id: r.errors for r in results if r.errors}
        cur.execute(
            "insert into ingest_runs (started_at, outlets_ok, outlets_failed, new_articles, "
            "duplicates, errors) values (%s, %s, %s, %s, %s, %s)",
            (started, ok, len(results) - ok, new, dup, json.dumps(errors, ensure_ascii=False)),
        )
        cur.execute("select prune_articles(%s)", (KEEP_DAYS,))
        pruned = cur.fetchone()[0]
        conn.commit()
    return {"new": new, "duplicates": dup, "pruned": pruned}


def summary(results: list[OutletResult], stored: dict | None) -> str:
    ok = [r for r in results if r.items]
    empty = [r for r in results if not r.items]
    lines = [
        f"outlets with items: {len(ok)}/{len(results)}",
        f"items collected (last {MAX_AGE_H}h): {sum(len(r.items) for r in results)}",
    ]
    if stored:
        lines.append(
            f"new articles stored: {stored['new']}, duplicates skipped: "
            f"{stored['duplicates']}, pruned (>{KEEP_DAYS} days): {stored['pruned']}"
        )
    if empty:
        lines.append("no items: " + ", ".join(r.outlet_id for r in empty))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", default="", help="write collected items to this JSON file")
    args = ap.parse_args(argv)

    started = datetime.now(UTC)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    results = collect_all(lock)
    if args.dump:
        Path(args.dump).write_text(
            json.dumps(
                [i.__dict__ for r in results for i in r.items], ensure_ascii=False, indent=0
            ),
            encoding="utf-8",
        )
    stored = None if args.dry_run else store(results, lock, started)

    text = summary(results, stored)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Ingest\n\n```\n" + text + "\n```\n")

    # Fail the run (and trigger GitHub's failure email) if most sources broke at once.
    ok = sum(1 for r in results if r.items)
    return 0 if ok >= 0.5 * len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
