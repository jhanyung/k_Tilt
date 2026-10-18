"""Client side of the Seoul-region relay (supabase/functions/relay).

The relay URL and key are derived from SUPABASE_DB_URL, which every workflow already has:
- project ref: the pooler user name is "postgres.<project-ref>"
- key: SHA-256 of a fixed label plus the full connection string (contains the DB password)
The deploy workflow sets the same key as the function's RELAY_KEY secret, so no extra
secret has to be created or copied by hand.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

REGION = "ap-northeast-2"  # Seoul


def _db_url() -> str:
    return os.environ.get("SUPABASE_DB_URL", "")


def project_ref(db_url: str | None = None) -> str:
    user = urlsplit(db_url if db_url is not None else _db_url()).username or ""
    return user.split(".", 1)[1] if user.startswith("postgres.") else ""


def relay_key(db_url: str | None = None) -> str:
    raw = db_url if db_url is not None else _db_url()
    return hashlib.sha256(f"giulgi-relay:{raw}".encode()).hexdigest() if raw else ""


def endpoint(db_url: str | None = None) -> str:
    ref = project_ref(db_url)
    return f"https://{ref}.supabase.co/functions/v1/relay" if ref else ""


def available() -> bool:
    return bool(endpoint() and relay_key())


def allowlist_domains() -> list[str]:
    import yaml

    from pipeline.feeds import registered_domain

    registry = Path(__file__).resolve().parent / "outlets.yaml"
    outlets = yaml.safe_load(registry.read_text(encoding="utf-8"))["outlets"]
    domains = {registered_domain(o["home"]) for o in outlets}
    for o in outlets:
        domains |= {registered_domain(u) for u in o.get("feeds", [])}
    return sorted(domains)


def main(argv: list[str] | None = None) -> int:
    """Deploy helpers: `allowlist` writes the function's allowed.json, `ref` prints the
    project ref, `key` prints the relay key (the workflow masks it before use)."""
    import json
    import sys

    cmd = (argv or sys.argv[1:] or [""])[0]
    if cmd == "allowlist":
        out = Path(__file__).resolve().parent.parent / "supabase/functions/relay/allowed.json"
        out.write_text(json.dumps({"domains": allowlist_domains()}, indent=1) + "\n")
        print(f"wrote {out.name} with {len(allowlist_domains())} domains")
    elif cmd == "ref":
        print(project_ref())
    elif cmd == "key":
        print(relay_key())
    else:
        print("usage: python -m pipeline.relay allowlist|ref|key")
        return 2
    return 0


class RelayTransport(httpx.BaseTransport):
    """Sends every request through the relay and returns the upstream response."""

    def __init__(self, timeout: httpx.Timeout) -> None:
        self._inner = httpx.HTTPTransport(retries=1)
        self._timeout = timeout
        self._endpoint = endpoint()
        self._key = relay_key()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise httpx.UnsupportedProtocol("relay supports GET only")
        relayed = httpx.Request(
            "GET",
            self._endpoint,
            params={"url": str(request.url)},
            headers={"x-relay-key": self._key, "x-region": REGION},
            extensions={"timeout": self._timeout.as_dict()},
        )
        resp = self._inner.handle_request(relayed)
        resp.read()
        if "x-upstream-status" not in resp.headers:
            # Relay not deployed, wrong key, host not allowed, or upstream unreachable.
            raise httpx.ConnectError(f"relay {resp.status_code}: {resp.text[:80]}", request=request)
        return httpx.Response(
            status_code=resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "")},
            content=resp.content,
            request=request,
        )

    def close(self) -> None:
        self._inner.close()


if __name__ == "__main__":
    raise SystemExit(main())
