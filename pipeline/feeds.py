"""Read outlet sources into uniform entries. Three kinds of source:

- rss:     RSS/Atom feeds
- sitemap: Google News sitemaps (and indexes pointing to them)
- page:    an outlet's own listing pages (homepage, section pages); only the headline links
           shown there are read, for outlets that publish no usable feed

Only headline, link and publication time are kept. Nothing from article bodies.
"""

from __future__ import annotations

import calendar
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from pipeline import http

MAX_BYTES = 6_000_000  # skip archive-sized sitemaps
_SECOND_LEVEL = {"co", "or", "go", "ne", "re", "pe", "ac", "kg", "es", "hs", "ms", "sc"}
_CHILD_PREF = re.compile(r"news|recent|latest|today|current|daily", re.IGNORECASE)
_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", re.DOTALL)
_DATE_IN_URL = re.compile(r"(20\d{2})[/_-]?(0[1-9]|1[0-2])[/_-]?(0[1-9]|[12]\d|3[01])")
_ARTICLE_HINT = re.compile(r"\d{5,}|article|view|html_dir|/news/", re.IGNORECASE)
_SKIP_LINK = re.compile(
    r"login|logout|member|search|javascript:|mailto:|/tag/|/author|/reporter|/journalist"
    r"|/photo/|/vod/|/video/|\.(jpg|jpeg|png|gif|pdf|mp4)(\?|$)",
    re.IGNORECASE,
)
_HANGUL = re.compile(r"[가-힣]")
_SPACES = re.compile(r"\s+")
_CHARSET = re.compile(rb"""charset\s*=\s*["']?([A-Za-z0-9_-]+)""", re.IGNORECASE)
_LEAD_MARKER = re.compile(r"\s(?:\[앵커\]|\[기자\]|\[리포트\]|앵커 멘트|<앵커>|\(서울=)")
MIN_PAGE_LINKS = 8


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


class _LinkText(HTMLParser):
    """Collects (href, visible text) for every <a>, including text inside nested tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text)))
            self._href = None


def decode_html(body: bytes) -> str:
    m = _CHARSET.search(body[:4096])
    enc = m.group(1).decode("ascii", "ignore").lower() if m else "utf-8"
    if enc in ("ks_c_5601-1987", "euc-kr", "ksc5601"):
        enc = "cp949"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def parse_listing(body: bytes, base_url: str, now: float, max_age_days: int = 3) -> list[Entry]:
    """Headline links from an outlet's listing page. Keeps same-site links that look like
    articles and carry a Korean headline; skips links whose URL date is too old."""
    parser = _LinkText()
    try:
        parser.feed(decode_html(body))
    except Exception:  # malformed HTML: use what was parsed
        pass
    site = registered_domain(base_url)
    cutoff = datetime.fromtimestamp(now, UTC).date() - timedelta(days=max_age_days)
    best: dict[str, str] = {}
    for href, text in parser.links:
        if not href:
            continue
        url = urljoin(base_url, href.strip())
        if not url.startswith("http") or _SKIP_LINK.search(url):
            continue
        if registered_domain(url) != site or not _ARTICLE_HINT.search(url):
            continue
        title = _SPACES.sub(" ", text).strip()
        if not (10 <= len(title) <= 150) or not _HANGUL.search(title):
            continue
        m = _DATE_IN_URL.search(url)
        if m:
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            except ValueError:
                d = None
            if d and d < cutoff:
                continue
        url = url.split("#", 1)[0]
        title = _LEAD_MARKER.split(title, 1)[0].strip()
        if len(title) < 10:
            continue
        # The same article is often linked twice: headline alone, and headline + lead.
        # The shorter text is the headline.
        if url not in best or len(title) < len(best[url]):
            best[url] = title
    return [Entry(u, t, None) for u, t in best.items()]


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
    why = http.refusal(url, http.is_relay(c))
    if why:
        return None, why
    body, reason = _get_once(url, c)
    if body is None and reason.startswith("http error"):  # one retry for network blips
        time.sleep(2)
        body, reason = _get_once(url, c)
    return body, reason


def _get_once(url: str, c: httpx.Client) -> tuple[bytes | None, str]:
    try:
        status, body = http.stream_bytes(c, url, MAX_BYTES)
    except (httpx.HTTPError, ValueError) as e:  # ValueError: malformed URL
        return None, f"http error: {type(e).__name__}"
    if status != 200:
        return None, f"status {status}"
    if body is None:
        return None, "too large"
    return body, ""


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


def fetch_page_entries(url: str, c: httpx.Client) -> tuple[list[Entry], str]:
    body, reason = _get(url, c)
    if body is None:
        return [], reason
    entries = parse_listing(body, url, time.time())
    if len(entries) < MIN_PAGE_LINKS:
        return entries, f"only {len(entries)} headline links"
    return entries, ""


def fetch_any(src: dict, c: httpx.Client) -> tuple[list[Entry], str, str]:
    """Fetch a lock-file source of any kind. Returns (entries, kind, reason)."""
    if src.get("kind") == "page":
        entries, reason = fetch_page_entries(src["url"], c)
        return entries, "page", reason
    return fetch_entries(src["url"], c)


def check(
    url: str, home: str, c: httpx.Client, max_age_h: float = 24 * 7, kind_hint: str = ""
) -> FeedCheck:
    if kind_hint == "page":
        items, reason = fetch_page_entries(url, c)
        if reason:
            return FeedCheck(url, False, reason)
        return FeedCheck(url, True, kind="page", entries=len(items))
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
