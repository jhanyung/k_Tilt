"""Collect official statements: 청와대 and ministry briefings, the government's rebuttals of
news reports, and each party's spokesperson statements.

Only title, link, date and source are stored. The text is read in memory when a story
is analysed (pipeline.analyse) and dropped.

Usage:
  python -m pipeline.statements                 # newest page of every source
  python -m pipeline.statements --pages 20      # backfill
  python -m pipeline.statements --dry-run
"""

from __future__ import annotations

import argparse
import html as htmllib
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import yaml

from pipeline import http, relay
from pipeline.feeds import decode_html

SOURCES = Path(__file__).resolve().parent / "data" / "statements.yaml"
DATE = re.compile(r"(?<![\w\"'=.-])(20\d\d)[.-](\d{1,2})[.-](\d{1,2})(?![\d\"'])")
TAG = re.compile(r"<[^>]+>")
STRONG = re.compile(r"<strong[^>]*>(.*?)</strong>", re.S)
KEEP_PARAMS = {"newsId", "brd", "post"}
PAGE_PAUSE = 1.5


@dataclass
class Statement:
    source: str
    url: str
    title: str
    published_on: date | None


def sources() -> list[dict]:
    return yaml.safe_load(SOURCES.read_text(encoding="utf-8"))["sources"]


def canonical(url: str) -> str:
    """Drop list-navigation parameters so the same statement always has one URL."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k in KEEP_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _clean(fragment: str) -> str:
    m = STRONG.search(fragment)
    text = TAG.sub(" ", m.group(1) if m else fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def parse_list(page: str, base: str, link: str, source: str) -> list[Statement]:
    """Statements on a list page: each link matching `link`, its text, and the first date
    printed after it (dates inside attributes are ignored)."""
    pattern = re.compile(
        r"<a\b[^>]*href=[\"']([^\"']*(?:" + link + r")[^\"']*)[\"'][^>]*>(.*?)</a>", re.S
    )
    hits = list(pattern.finditer(page))
    out: dict[str, Statement] = {}
    for n, m in enumerate(hits):
        url = canonical(urljoin(base, htmllib.unescape(m.group(1))))
        title = _clean(m.group(2))
        nxt = hits[n + 1].start() if n + 1 < len(hits) else len(page)
        # The date sits inside the link on some lists, after it on others.
        d = DATE.search(page, m.start(2), min(nxt, m.end() + 3000))
        day = None
        if d:
            try:
                day = date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
            except ValueError:
                day = None
        if len(title) < 6:  # icon links next to the real one
            if url in out and out[url].published_on is None:
                out[url].published_on = day
            continue
        if url not in out or len(title) > len(out[url].title):
            prev = out[url].published_on if url in out else None
            out[url] = Statement(source, url, title, day or prev)
    return list(out.values())


def page_url(src: dict, n: int) -> str:
    if n == 1:
        return src["list"]
    sep = "&" if "?" in src["list"] else "?"
    value = (n - 1) * src["page_step"] if src.get("page_step") else n
    return f"{src['list']}{sep}{src['page_param']}={value}"


def collect(src: dict, pages: int = 1) -> tuple[list[Statement], str]:
    """Statements from the first `pages` list pages. One client per source keeps the
    cookies some sites set on the first visit (as any browser would)."""
    found: list[Statement] = []
    last = ""
    for via in (False, True):
        try:
            with http.client(via_relay=via) as c:
                for n in range(1, pages + 1):
                    url = page_url(src, n)
                    why = http.refusal(url, via)
                    if why:
                        raise PermissionError(why)
                    try:
                        r = http.get(c, url)
                    except httpx.TransportError:
                        time.sleep(5)
                        r = http.get(c, url)
                    if r.status_code != 200:
                        raise httpx.HTTPError(f"status {r.status_code}")
                    batch = parse_list(decode_html(r.content), url, src["link"], src["id"])
                    if not batch:
                        break
                    found += batch
                    time.sleep(PAGE_PAUSE)
            return found, "relay" if via else "ok"
        except PermissionError as e:
            if "disallows" in str(e):
                return [], str(e)
            last = str(e)
        except (httpx.HTTPError, ValueError) as e:
            last = f"error: {type(e).__name__} {e}"[:120]
        if not relay.available():
            break
    return found, last


def store(rows: list[Statement]) -> int:
    from pipeline.db import connect

    side = {s["id"]: (s["side"], s.get("party", "")) for s in sources()}
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            "insert into statements (source, side, party, url, title, published_on) "
            "values (%s, %s, %s, %s, %s, %s) "
            "on conflict (url) do update set title = excluded.title, "
            "published_on = coalesce(statements.published_on, excluded.published_on)",
            [(r.source, *side[r.source], r.url, r.title[:300], r.published_on) for r in rows],
        )
        conn.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    rows: list[Statement] = []
    for src in sources():
        if args.only and src["id"] not in args.only.split(","):
            continue
        got, how = collect(src, args.pages)
        rows += got
        newest = max((r.published_on for r in got if r.published_on), default=None)
        print(f"{src['id']:<12} {len(got):>4} statements  newest {newest}  ({how})")
        for r in got[:3]:
            print(f"    {r.published_on}  {r.title[:70]}")
    if not args.dry_run and rows:
        print(f"stored {store(rows)}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
