"""Find working RSS feeds for every outlet and write pipeline/data/feeds.lock.json.

Order of candidates: registry hints, feeds advertised on the homepage or an RSS page,
Google News sitemaps declared in robots.txt, then common CMS paths. Up to two sources are
kept per outlet: a general one and, if found, a politics feed.

Usage: python -m pipeline.discover [--only id1,id2] [--keep-existing]

--keep-existing: an outlet already in the lock keeps its sources when this run cannot
reach it (e.g. a site that refuses foreign IPs during a GitHub-hosted run).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml

from pipeline import feeds, http

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "outlets.yaml"
LOCK = ROOT / "data" / "feeds.lock.json"

COMMON_PATHS = [
    "/rss/allArticle.xml",  # ndsoft CMS, used by many Korean outlets
    "/rss/S1N1.xml",  # ndsoft: politics section
    "/rss",
    "/rss.xml",
    "/rss/rss.xml",
    "/feed",
    "/feed/",
    "/rss/all.xml",
    "/rss/total.xml",
]
SITEMAP_PATHS = ["/sitemap-news.xml", "/news-sitemap.xml", "/sitemap_news.xml", "/sitemap/news.xml"]
SITEMAP_PREF = re.compile(r"news|recent|latest|today|google", re.IGNORECASE)
SITEMAP_SKIP = re.compile(r"/(19|20)\d\d\b|archive|image|video|section|category|tag", re.IGNORECASE)
POLITICS = re.compile(r"politic|정치|S1N1|/pol", re.IGNORECASE)


class _AltLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = dict(attrs)
        typ = (a.get("type") or "").lower()
        if "alternate" in (a.get("rel") or "").lower() and ("rss" in typ or "atom" in typ):
            if a.get("href"):
                self.found.append(a["href"])


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.found.append(href)


RSS_PAGES = ["", "/rss", "/rss.html", "/rss/index.html", "/etc/rss", "/help/rss", "/service/rss"]
FEEDLIKE = re.compile(r"rss|\.xml|/feed", re.IGNORECASE)
PREFERRED = re.compile(r"all|total|sokbo|newsflash|recent|latest|전체|politic|정치", re.IGNORECASE)


def _get_html(url: str, c: httpx.Client) -> httpx.Response | None:
    if not http.allowed(url):
        return None
    try:
        r = c.get(url)
    except (httpx.HTTPError, ValueError):
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    return r


def homepage_feeds(home: str, c: httpx.Client) -> list[str]:
    """Feed URLs advertised by the homepage or listed on an RSS directory page."""
    site = feeds.registered_domain(home)
    found: list[str] = []
    for path in RSS_PAGES:
        r = _get_html(home + (path or "/"), c)
        if r is None:
            continue
        alt, anchors = _AltLinks(), _Anchors()
        try:
            alt.feed(r.text)
            anchors.feed(r.text)
        except Exception:  # malformed HTML should not stop discovery
            continue
        base = str(r.url)
        found += [urljoin(base, h) for h in alt.found]
        for h in anchors.found:
            u = urljoin(base, h)
            if u.startswith("http") and FEEDLIKE.search(u) and feeds.registered_domain(u) == site:
                found.append(u)
    unique = list(dict.fromkeys(found))
    # Try general and politics feeds first, and cap the number of requests per outlet.
    unique.sort(key=lambda u: 0 if PREFERRED.search(u) else 1)
    return unique[:15]


def robots_news_sitemaps(home: str) -> list[str]:
    """Sitemaps from robots.txt, recent-news ones first, yearly archives skipped."""
    urls = [u for u in http.sitemaps(home + "/") if not SITEMAP_SKIP.search(u)]
    urls.sort(key=lambda u: 0 if SITEMAP_PREF.search(u) else 1)
    return urls[:6]


def resolve(outlet: dict) -> dict:
    home = outlet["home"].rstrip("/")
    tried: dict[str, str] = {}
    good: list[feeds.FeedCheck] = []
    with http.client() as c:

        def attempt(urls: list[str]) -> None:
            for u in urls:
                if u in tried or len(good) >= 4:
                    continue
                try:
                    chk = feeds.check(u, home, c)
                except Exception as e:  # one bad URL must never stop the run
                    tried[u] = f"error: {type(e).__name__}"
                    continue
                tried[u] = "ok" if chk.ok else chk.reason
                if chk.ok:
                    good.append(chk)

        attempt(outlet.get("feeds", []))
        if not good:
            attempt(homepage_feeds(home, c))
        if not good:
            attempt(robots_news_sitemaps(home))
        if not good:
            attempt([home + p for p in COMMON_PATHS + SITEMAP_PATHS])
        elif not any(POLITICS.search(g.url + g.title) for g in good):
            # We have a general feed; look once for a politics section feed.
            attempt([home + "/rss/S1N1.xml"])

    chosen: list[feeds.FeedCheck] = []
    general = [g for g in good if not POLITICS.search(g.url + g.title)]
    politics = [g for g in good if POLITICS.search(g.url + g.title)]
    if general:
        chosen.append(max(general, key=lambda g: g.entries))
    if politics:
        chosen.append(politics[0])
    if not chosen and good:
        chosen.append(good[0])
    return {
        "id": outlet["id"],
        "name": outlet["name"],
        "group": outlet["group"],
        "home": home,
        "feeds": [{"url": g.url, "kind": g.kind} for g in chosen],
        "tried": {} if chosen else tried,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-existing", action="store_true")
    args = ap.parse_args(argv)
    outlets = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["outlets"]
    if args.only:
        keep = set(args.only.split(","))
        outlets = [o for o in outlets if o["id"] in keep]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(resolve, outlets))

    working = [r for r in results if r["feeds"]]
    failing = [r for r in results if not r["feeds"]]
    lock = {"outlets": {}, "failing": {}}
    previous = json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.exists() else lock
    if args.only:  # partial run: merge into the existing lock
        lock = previous
    kept = []
    for r in results:
        lock["outlets"].pop(r["id"], None)
        lock["failing"].pop(r["id"], None)
    if args.keep_existing:
        for r in list(failing):
            old = previous["outlets"].get(r["id"])
            if old:
                lock["outlets"][r["id"]] = old
                failing.remove(r)
                kept.append(r["id"])
    for r in working:
        lock["outlets"][r["id"]] = {k: r[k] for k in ("name", "group", "home", "feeds")}
    for r in failing:
        lock["failing"][r["id"]] = r["tried"]
    lock["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    lock["outlets"] = dict(sorted(lock["outlets"].items()))
    lock["failing"] = dict(sorted(lock["failing"].items()))
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"working outlets this run: {len(working)}/{len(results)}")
    print(f"working outlets in lock: {len(lock['outlets'])}")
    if kept:
        print("unreachable this run, previous sources kept: " + ", ".join(kept))
    for r in failing:
        print(f"  no feed: {r['id']} ({r['name']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
