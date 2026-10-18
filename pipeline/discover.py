"""Find working RSS feeds for every outlet and write pipeline/data/feeds.lock.json.

Order of candidates: registry hints, feeds advertised on the homepage or an RSS page,
Google News sitemaps declared in robots.txt, common CMS paths, and finally the outlet's own
listing pages (homepage plus politics and society sections). Up to two feeds are kept per
outlet (general + politics), or up to three listing pages.

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

from pipeline import feeds, http, relay

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
    if not http.allowed(url, http.is_relay(c)):
        return None
    try:
        r = http.get(c, url)
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


def robots_news_sitemaps(home: str, via_relay: bool = False) -> list[str]:
    """Sitemaps from robots.txt, recent-news ones first, yearly archives skipped."""
    urls = [u for u in http.sitemaps(home + "/", via_relay) if not SITEMAP_SKIP.search(u)]
    urls.sort(key=lambda u: 0 if SITEMAP_PREF.search(u) else 1)
    return urls[:6]


SECTION_WORDS = ("정치", "사회", "전국")


def listing_pages(home: str, c: httpx.Client) -> list[str]:
    """The homepage plus its politics and society section pages, in that order."""
    pages = [home + "/"]
    r = _get_html(home + "/", c)
    if r is None:
        return pages
    parser = feeds._LinkText()
    try:
        parser.feed(r.text)
    except Exception:
        return pages
    site = feeds.registered_domain(home)
    for word in SECTION_WORDS:
        for href, text in parser.links:
            if text.strip() == word and href:
                u = urljoin(str(r.url), href)
                if u.startswith("http") and feeds.registered_domain(u) == site:
                    pages.append(u)
                    break
    return list(dict.fromkeys(pages))[:3]


def resolve(outlet: dict, via_relay: bool = False) -> dict:
    home = outlet["home"].rstrip("/")
    tried: dict[str, str] = {}
    good: list[feeds.FeedCheck] = []
    with http.client(via_relay=via_relay) as c:

        def attempt(urls: list[str], kind_hint: str = "") -> None:
            for u in urls:
                if u in tried or len(good) >= 4:
                    continue
                try:
                    chk = feeds.check(u, home, c, kind_hint=kind_hint)
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
            attempt(robots_news_sitemaps(home, via_relay))
        if not good:
            attempt([home + p for p in COMMON_PATHS + SITEMAP_PATHS])
        elif not any(POLITICS.search(g.url + g.title) for g in good):
            # We have a general feed; look once for a politics section feed.
            attempt([home + "/rss/S1N1.xml"])
        if not good:
            attempt(listing_pages(home, c), kind_hint="page")

    chosen: list[feeds.FeedCheck] = []
    pages = [g for g in good if g.kind == "page"]
    if pages:
        good = []
        chosen = pages[:3]
    general = [g for g in good if not POLITICS.search(g.url + g.title)]
    politics = [g for g in good if POLITICS.search(g.url + g.title)]
    if general:
        chosen.append(max(general, key=lambda g: g.entries))
    if politics:
        chosen.append(politics[0])
    if not chosen and good:
        chosen.append(good[0])
    result = {
        "id": outlet["id"],
        "name": outlet["name"],
        "group": outlet["group"],
        "home": home,
        "feeds": [{"url": g.url, "kind": g.kind} for g in chosen],
        "tried": {} if chosen else tried,
    }
    if via_relay:
        result["via"] = "relay"
    return result


NETWORK_FAILURE = ("robots.txt unreachable", "robots.txt blocked", "http error", "error:", "relay ")


def only_network_failures(tried: dict[str, str]) -> bool:
    return bool(tried) and all(r.startswith(NETWORK_FAILURE) for r in tried.values())


def resolve_with_fallback(outlet: dict) -> dict:
    """Direct first; if the site was only unreachable (never a refusal), try from Seoul."""
    direct = resolve(outlet)
    if direct["feeds"] or not relay.available() or not only_network_failures(direct["tried"]):
        return direct
    via = resolve(outlet, via_relay=True)
    return via if via["feeds"] else direct


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
        results = list(pool.map(resolve_with_fallback, outlets))

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
            # Forgive network failures, never a refusal of the sources we actually use.
            via = old.get("via") == "relay" if old else False
            refused = old and any(
                http.refusal(src["url"], via) == "robots.txt disallows" for src in old["feeds"]
            )
            if old and not refused:
                lock["outlets"][r["id"]] = old
                failing.remove(r)
                kept.append(r["id"])
    for r in working:
        lock["outlets"][r["id"]] = {
            k: r[k] for k in ("name", "group", "home", "feeds", "via") if k in r
        }
    for r in failing:
        lock["failing"][r["id"]] = r["tried"]
    # Shown publicly as 수집거부, so only when robots.txt refuses the homepage itself.
    homes = {o["id"]: o["home"].rstrip("/") + "/" for o in outlets}
    refused = set(lock.get("refused", [])) - set(homes)
    refused |= {
        oid
        for oid in lock["failing"]
        if oid in homes and http.refusal(homes[oid]) == "robots.txt disallows"
    }
    lock["refused"] = sorted(refused)
    # 접속차단: the server turns our crawler away (HTTP 401/403 on robots.txt) both from
    # GitHub's runners and from the Seoul relay. Not an opt-out, but no route in either.
    lock["blocked"] = sorted(
        oid
        for oid in lock["failing"]
        if oid in homes
        and oid not in refused
        and http.refusal(homes[oid]) == "robots.txt blocked"
        and (not relay.available() or http.refusal(homes[oid], True).startswith("robots.txt"))
    )
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
