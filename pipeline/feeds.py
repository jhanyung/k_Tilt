"""Read outlet sources (RSS/Atom feeds and Google News sitemaps) into uniform entries.

Only headline, link and publication time are kept. Nothing from article bodies.
"""

from __future__ import annotations

import calendar
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import feedparser
import httpx

from pipeline import http

MAX_BYTES = 6_000_000  # skip archive-sized sitemaps
_SECOND_LEVEL = {"co", "or", "go", "ne", "re", "pe", "ac", "kg", "es", "hs", "ms", "sc"}
_CHILD_PREF = re.compile(r"news|recent|latest|today|current|daily", re.IGNORECASE)
_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", re.DOTALL)


def registered_domain(url_or_host: str) -> str:
    host = urlsplit(url_or_host).netloc or url_or_host
    host = host.split(":")[0].lower().rstrip(".")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass
class Entry:
    link: str
    title: str
    published: float | None  # unix seconds, UTC


@dataclass
class FeedCheck:
    url: str
    ok: bool
    reason: str = ""
    kind: str = ""
    title: str = ""
    entries: int = 0
    newest_age_h: float | None = None


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def _clean_title(s: str | None) -> str:
    s = (s or "").strip()
    m = _CDATA.match(s)
    return (m.group(1) if m else s).strip()


def fix_kst_mislabel(ts: float | None, now: float) -> float | None:
    """Many Korean feeds write Korean time but label it UTC, putting items up to 9h in the
    future. Shift such timestamps back by 9h; leave everything else alone."""
    if ts is None:
        return None
    if now + 3600 < ts <= now + 9 * 3600 + 600:
        return ts - 9 * 3600
    return ts


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(content: bytes) -> tuple[str, list]:
    """Return ("urlset", [Entry]) or ("index", [child urls]) or ("", [])."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return "", []
    kind = _local(root.tag)
    if kind == "sitemapindex":
        locs = [_clean_title(el.text) for el in root.iter() if _local(el.tag) == "loc"]
        return "index", [u for u in locs if u]
    if kind != "urlset":
        return "", []
    out: list[Entry] = []
    for url in root:
        if _local(url.tag) != "url":
            continue
        link = title = pub = lastmod = None
        for el in url.iter():
            name = _local(el.tag)
            if name == "loc" and link is None:
                link = _clean_title(el.text)
            elif name == "title":
                title = _clean_title(el.text)
            elif name == "publication_date":
                pub = el.text
            elif name == "lastmod":
                lastmod = el.text
        if link and title:
            out.append(Entry(link, title, _iso(pub) or _iso(lastmod)))
    return "urlset", out


def _get(url: str, c: httpx.Client) -> tuple[bytes | None, str]:
    if not http.allowed(url):
        return None, "robots.txt disallows"
    body, reason = _get_once(url, c)
    if body is None and reason.startswith("http error"):  # one retry for network blips
        time.sleep(2)
        body, reason = _get_once(url, c)
    return body, reason


def _get_once(url: str, c: httpx.Client) -> tuple[bytes | None, str]:
    try:
        with c.stream("GET", url) as r:
            if r.status_code != 200:
                return None, f"status {r.status_code}"
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    return None, "too large"
            return bytes(buf), ""
    except (httpx.HTTPError, ValueError) as e:  # ValueError: malformed URL
        return None, f"http error: {type(e).__name__}"


def fetch_entries(url: str, c: httpx.Client, _depth: int = 0) -> tuple[list[Entry], str, str]:
    """Returns (entries, kind, reason). kind is "rss" or "sitemap"; reason set on failure."""
    body, reason = _get(url, c)
    if body is None:
        return [], "", reason
    kind, items = parse_sitemap(body)
    if kind == "urlset":
        return items, "sitemap", "" if items else "sitemap without news titles"
    if kind == "index":
        if _depth >= 1:
            return [], "", "nested sitemap index"
        children = sorted(items, key=lambda u: 0 if _CHILD_PREF.search(u) else 1)[:3]
        merged: list[Entry] = []
        for child in children:
            got, _, _ = fetch_entries(child, c, _depth + 1)
            merged += got
        return merged, "sitemap", "" if merged else "sitemap index without news titles"
    parsed = feedparser.parse(body)
    out = []
    for e in parsed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        out.append(
            Entry(
                e.get("link", ""),
                (e.get("title") or "").strip(),
                float(calendar.timegm(t)) if t else None,
            )
        )
    return out, "rss", "" if out else "no entries"


def check(url: str, home: str, c: httpx.Client, max_age_h: float = 24 * 7) -> FeedCheck:
    items, kind, reason = fetch_entries(url, c)
    if reason:
        return FeedCheck(url, False, reason)
    entries = [e for e in items if e.link.startswith("http") and e.title]
    if len(entries) < 3:
        return FeedCheck(url, False, f"only {len(entries)} usable entries")
    site = registered_domain(home)
    own = sum(1 for e in entries if registered_domain(e.link) == site)
    if own / len(entries) < 0.5:
        return FeedCheck(url, False, "links mostly point to other sites")
    times = [e.published for e in entries if e.published]
    age = (time.time() - max(times)) / 3600 if times else None
    if age is not None and age > max_age_h:
        return FeedCheck(url, False, f"stale: newest item {age:.0f}h old", entries=len(entries))
    return FeedCheck(url, True, kind=kind, entries=len(entries), newest_age_h=age)
