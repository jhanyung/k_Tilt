"""Polite HTTP access: an honest user agent, robots.txt checks, timeouts, and a fallback
for servers that still use legacy TLS settings."""

from __future__ import annotations

import ssl
import time
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import httpx

from pipeline import relay
from pipeline.robots import Robots

PRODUCT_TOKEN = "giulgibot"
USER_AGENT = "GiulgiBot/0.1 (+https://github.com/jhanyung/k_Tilt; news community, links out only)"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_LEGACY_MARKERS = (
    "dh key too small",
    "dh_key_too_small",
    "unsafe legacy renegotiation",
    "handshake failure",
    "unsupported protocol",
    "no ciphers available",
)


def _legacy_context() -> ssl.SSLContext:
    """Certificates are still verified; only the minimum key-strength level is relaxed."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
    return ctx


class RelayClient(httpx.Client):
    """A client whose requests are fetched from Seoul by the Supabase relay."""


def client(legacy: bool = False, via_relay: bool = False) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"}
    if via_relay:
        # The relay follows redirects itself.
        return RelayClient(
            headers=headers, timeout=TIMEOUT, transport=relay.RelayTransport(TIMEOUT)
        )
    return httpx.Client(
        headers=headers,
        timeout=TIMEOUT,
        follow_redirects=True,
        verify=_legacy_context() if legacy else True,
    )


def is_relay(c: httpx.Client) -> bool:
    return isinstance(c, RelayClient)


def _is_legacy_tls_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(m in text for m in _LEGACY_MARKERS)


def get(c: httpx.Client, url: str) -> httpx.Response:
    """GET with a one-time retry on a legacy-TLS client when the handshake demands it."""
    try:
        return c.get(url)
    except httpx.ConnectError as e:
        if is_relay(c) or not _is_legacy_tls_error(e):
            raise
    with client(legacy=True) as lc:
        return lc.get(url)


def stream_bytes(c: httpx.Client, url: str, limit: int) -> tuple[int, bytes | None]:
    """Status and body (None if larger than limit), with the same legacy-TLS fallback."""

    def run(cl: httpx.Client) -> tuple[int, bytes | None]:
        with cl.stream("GET", url) as r:
            if r.status_code != 200:
                return r.status_code, b""
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > limit:
                    return r.status_code, None
            return r.status_code, bytes(buf)

    try:
        return run(c)
    except httpx.ConnectError as e:
        if is_relay(c) or not _is_legacy_tls_error(e):
            raise
    with client(legacy=True) as lc:
        return run(lc)


@lru_cache(maxsize=512)
def _robots_for(origin: str, via_relay: bool = False) -> Robots | None:
    """robots.txt for an origin, fetched once per run. None means "no robots.txt" (allow).

    401/403 and server errors are treated as "disallow everything", which is stricter
    than RFC 9309 requires for 4xx, so that an unreadable file never becomes a yes.
    """
    r = None
    for attempt in range(2):
        try:
            with client(via_relay=via_relay) as c:
                r = get(c, f"{origin}/robots.txt")
            break
        except (httpx.HTTPError, ValueError):
            if attempt == 0:
                time.sleep(3)
    if r is None or r.status_code >= 500:
        return Robots.deny_all(unreachable=True)
    if r.status_code in (401, 403):
        return Robots.deny_all()
    if r.status_code >= 400:
        return None
    return Robots.parse(r.text)


def sitemaps(url: str, via_relay: bool = False) -> list[str]:
    """Sitemap URLs the site declares in robots.txt."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    rp = _robots_for(origin, via_relay)
    # Some sites declare relative sitemap paths; resolve them against the origin.
    return [urljoin(origin + "/", u) for u in rp.sitemaps] if rp else []


def refusal(url: str, via_relay: bool = False) -> str:
    """Empty if we may fetch url; otherwise why not."""
    parts = urlsplit(url)
    rp = _robots_for(f"{parts.scheme}://{parts.netloc}", via_relay)
    if rp is None or rp.can_fetch(PRODUCT_TOKEN, url):
        return ""
    return "robots.txt unreachable" if rp.unreachable else "robots.txt disallows"


def allowed(url: str, via_relay: bool = False) -> bool:
    return not refusal(url, via_relay)
