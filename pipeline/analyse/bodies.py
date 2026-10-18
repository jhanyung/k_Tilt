"""Fetch article pages and extract their text in memory.

The text is handed to the analysers and then dropped: nothing here writes it anywhere.
robots.txt is checked for every article URL, through the Seoul relay when the outlet
is only reachable that way. Requests to one host are made one at a time with a pause.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import lxml.html
import trafilatura

from pipeline import http
from pipeline.feeds import decode_html

MAX_BYTES = 4_000_000
HOST_PAUSE = 1.0  # seconds between two requests to the same host
WORKERS = 8
MIN_CHARS = 200  # shorter extractions are menus, paywalls or photo captions


@dataclass
class Page:
    url: str
    status: str  # ok | refused | unreachable | http NNN | too large | empty | error: ...
    text: str = ""


_LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
_BODY_KEYS = re.compile(
    r'"(articleBody|articleContent|article_body|newsContent)"\s*:\s*("(?:[^"\\]|\\.)*")'
)
_FUSION = re.compile(r"Fusion\.globalContent\s*=\s*(\{.*?\});\s*Fusion\.", re.S)
# Arc-style content elements, also streamed inside Next.js payloads (news1).
_TEXT_ELEMENT = re.compile(r'"type":"text","content":"((?:[^"\\]|\\.)*)"')
_TAG = re.compile(r"<br\s*/?>|</p>|</div>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    text = _ANY_TAG.sub("", _TAG.sub("\n", fragment))
    lines = (re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in htmllib.unescape(text).splitlines())
    return "\n".join(ln for ln in lines if ln)


def _walk(node, key: str):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, str):
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


_DROP = ("script", "style", "figure", "figcaption", "noscript", "iframe", "button", "table")


def _unescape_js(raw: str) -> str:
    text = raw.replace("\\'", "'")
    for _ in range(2):
        try:
            text = json.loads(f'"{text}"', strict=False)
        except ValueError:
            break
        if '\\"' not in text:
            break
    return text


def element_text(html: str) -> str:
    """Paragraphs stored as {"type":"text","content":...} objects."""
    return "\n".join(_unescape_js(m) for m in _TEXT_ELEMENT.findall(html))


# Malformed comments (<!------ x ------>) and an unclosed <iframe> inside <noscript>
# (MBN) make libxml2 swallow the rest of the page as text.
_NOISE = re.compile(r"<!--.*?-->|<noscript\b.*?</noscript>|</?iframe\b[^>]*>", re.S | re.I)


def clean_html(html: str) -> str:
    return _NOISE.sub("", html)


def microdata_text(html: str) -> str:
    """Text of the element marked itemprop="articleBody"."""
    try:
        doc = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    lower = "translate(@itemprop,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
    nodes = doc.xpath(f"//*[{lower}='articlebody']")
    if not nodes:
        return ""
    node = max(nodes, key=lambda n: len(n.text_content()))
    for bad in node.xpath(" | ".join(f".//{t}" for t in _DROP)):
        bad.drop_tree()
    for br in node.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
    for block in node.xpath(".//p | .//div | .//h2 | .//h3 | .//li"):
        block.tail = "\n" + (block.tail or "")
    return _strip_tags(lxml.html.tostring(node, encoding="unicode"))


def embedded_text(html: str) -> str:
    """Article text that script-rendered pages carry as data: schema.org articleBody,
    an articleContent field, or an Arc Fusion content object."""
    found: list[str] = []
    for block in _LD_JSON.findall(html):
        try:
            found += list(_walk(json.loads(block.strip(), strict=False), "articleBody"))
        except ValueError:
            continue
    for _, literal in _BODY_KEYS.findall(html):
        try:
            found.append(json.loads(literal, strict=False))
        except ValueError:
            continue
    m = _FUSION.search(html)
    if m:
        try:
            content = json.loads(m.group(1), strict=False)
            parts = [
                el.get("content", "")
                for el in content.get("content_elements", [])
                if isinstance(el, dict) and el.get("type") == "text"
            ]
            found.append("\n".join(parts))
        except ValueError:
            pass
    found.append(element_text(html))
    texts = [_strip_tags(t) for t in found if t]
    texts.append(microdata_text(html))
    return max(texts, key=len, default="")


def extract(html: str, url: str = "") -> str:
    html = clean_html(html)
    text = trafilatura.extract(
        html,
        url=url or None,
        favor_precision=True,
        include_comments=False,
        include_tables=False,
        include_images=False,
        deduplicate=True,
    )
    if text and len(text) >= MIN_CHARS:
        return text
    return embedded_text(html) or text or ""


def fetch(c: httpx.Client, url: str, via_relay: bool = False) -> Page:
    why = http.refusal(url, via_relay)
    if why:
        return Page(url, "unreachable" if "unreachable" in why else "refused")
    try:
        code, body = http.stream_bytes(c, url, MAX_BYTES)
    except (httpx.HTTPError, ValueError) as e:
        return Page(url, f"error: {type(e).__name__}")
    if code != 200:
        return Page(url, f"http {code}")
    if body is None:
        return Page(url, "too large")
    text = extract(decode_html(body), url)
    if len(text) < MIN_CHARS:
        return Page(url, "empty")
    return Page(url, "ok", text)


def fetch_many(
    items: Iterable[tuple[str, bool]],
    on_page: Callable[[Page], None],
) -> dict[str, int]:
    """Fetch (url, via_relay) pairs and call on_page for each result. Returns status counts.

    on_page runs on worker threads; it must analyse the text and let it go.
    """
    by_host: dict[tuple[str, bool], list[str]] = defaultdict(list)
    for url, via in items:
        by_host[(urlsplit(url).netloc, via)].append(url)
    counts: dict[str, int] = defaultdict(int)

    def run(key: tuple[str, bool]) -> None:
        _, via = key
        with http.client(via_relay=via) as c:
            for n, url in enumerate(by_host[key]):
                if n:
                    time.sleep(HOST_PAUSE)
                page = fetch(c, url, via)
                counts[page.status.split(":")[0]] += 1
                on_page(page)

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(run, list(by_host)))
    return dict(counts)
