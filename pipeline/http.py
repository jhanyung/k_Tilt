"""Polite HTTP access: an honest user agent, robots.txt checks and timeouts."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "GiulgiBot/0.1 (+https://github.com/jhanyung/k_Tilt; news community, links out only)"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


@lru_cache(maxsize=512)
def _robots_for(origin: str) -> RobotFileParser | None:
    """Fetch and parse robots.txt once per origin. None means 'no usable robots.txt'."""
    rp = RobotFileParser()
    try:
        with client() as c:
            r = c.get(f"{origin}/robots.txt")
    except (httpx.HTTPError, ValueError):
        return None
    if r.status_code in (401, 403):
        # Access to robots.txt itself is denied: treat the whole site as disallowed.
        rp.parse(["User-agent: *", "Disallow: /"])
        return rp
    if r.status_code >= 400:
        return None
    rp.parse(r.text.splitlines())
    return rp


def sitemaps(url: str) -> list[str]:
    """Sitemap URLs the site declares in robots.txt."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    rp = _robots_for(origin)
    # Some sites declare relative sitemap paths; resolve them against the origin.
    return [urljoin(origin + "/", u) for u in (rp.site_maps() or [])] if rp else []


def allowed(url: str) -> bool:
    parts = urlsplit(url)
    rp = _robots_for(f"{parts.scheme}://{parts.netloc}")
    return True if rp is None else rp.can_fetch(USER_AGENT, url)
