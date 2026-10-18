"""Developer diagnostics for outlets without a working source. Prints what a crawler sees:
robots.txt rules for '*', declared sitemaps, homepage status and feed-like links.

Usage: python -m pipeline.diagnose id1,id2,...
"""

from __future__ import annotations

import re
import sys
from urllib.parse import urljoin

import yaml

from pipeline import http
from pipeline.discover import REGISTRY, _AltLinks, _Anchors

FEEDLIKE = re.compile(r"rss|\.xml|/feed|sitemap", re.IGNORECASE)


def show(outlet: dict) -> None:
    home = outlet["home"].rstrip("/")
    print(f"\n===== {outlet['id']} {outlet['name']} {home}")
    with http.client() as c:
        try:
            r = http.get(c, home + "/robots.txt")
            print(f"robots.txt: {r.status_code}")
            star = False
            for line in r.text.splitlines():
                s = line.strip()
                low = s.lower()
                if low.startswith("user-agent:"):
                    star = s.split(":", 1)[1].strip() == "*"
                if low.startswith("sitemap") or (star and low.startswith(("disallow", "allow"))):
                    print("   ", s[:160])
        except Exception as e:
            print(f"robots.txt: {type(e).__name__}: {str(e)[:160]}")
        try:
            r = http.get(c, home + "/")
            print(f"homepage: {r.status_code} {r.headers.get('content-type', '')} -> {r.url}")
            alt, anchors = _AltLinks(), _Anchors()
            alt.feed(r.text)
            anchors.feed(r.text)
            links = [urljoin(str(r.url), h) for h in alt.found + anchors.found]
            feedish = list(dict.fromkeys(u for u in links if FEEDLIKE.search(u)))[:12]
            for u in feedish:
                print("    link:", u)
        except Exception as e:
            print(f"homepage: {type(e).__name__}: {str(e)[:160]}")


def main() -> int:
    wanted = set(sys.argv[1].split(",")) if len(sys.argv) > 1 else set()
    outlets = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["outlets"]
    for o in outlets:
        if not wanted or o["id"] in wanted:
            show(o)
    return 0


if __name__ == "__main__":
    sys.exit(main())
