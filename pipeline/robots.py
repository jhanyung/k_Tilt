"""robots.txt parsing per RFC 9309.

Differences from Python's urllib.robotparser that matter for Korean news sites:
- every group naming the same agent is merged (urllib keeps only the first '*' group,
  so a site ending with 'User-agent: * / Disallow: /' would be misread as allowing us);
- the longest matching rule wins, and Allow wins a tie;
- '*' and '$' wildcards are supported;
- full-URL rules ('Disallow: https://site/path', seen in the wild) are honoured as paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

_KEY = re.compile(r"^\s*([A-Za-z-]+)\s*:\s*(.*?)\s*$")


@dataclass
class Rule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return _compile(self.pattern).match(path) is not None


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""))


@dataclass
class Robots:
    groups: list[tuple[list[str], list[Rule]]] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    disallow_all: bool = False

    @classmethod
    def deny_all(cls) -> Robots:
        return cls(disallow_all=True)

    @classmethod
    def parse(cls, text: str) -> Robots:
        robots = cls()
        agents: list[str] = []
        rules: list[Rule] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0]
            m = _KEY.match(line)
            if not m:
                continue
            key, value = m.group(1).lower(), m.group(2)
            if key == "user-agent":
                if not last_was_agent and agents:
                    robots.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if not agents:
                    continue  # rules before any user-agent line are ignored
                path = _as_path(value)
                if path is None:
                    continue  # empty Disallow means "allow everything": no rule needed
                rules.append(Rule(key == "allow", path))
            elif key == "sitemap":
                if value:
                    robots.sitemaps.append(value)
            else:
                last_was_agent = False
        if agents:
            robots.groups.append((agents, rules))
        return robots

    def _rules_for(self, token: str) -> list[Rule] | None:
        token = token.lower()
        specific: list[Rule] = []
        star: list[Rule] = []
        found_specific = found_star = False
        for agents, rules in self.groups:
            names = {a.split("/", 1)[0].strip() for a in agents}
            if token in names:
                found_specific = True
                specific += rules
            if "*" in names:
                found_star = True
                star += rules
        if found_specific:
            return specific
        if found_star:
            return star
        return None

    def can_fetch(self, token: str, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        rules = self._rules_for(token)
        if not rules:
            return True
        best: Rule | None = None
        for r in rules:
            if not r.matches(path):
                continue
            if (
                best is None
                or r.specificity > best.specificity
                or (r.specificity == best.specificity and r.allow and not best.allow)
            ):
                best = r
        return True if best is None else best.allow


def _as_path(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        p = urlsplit(value)
        return (p.path or "/") + (f"?{p.query}" if p.query else "")
    return value if value.startswith(("/", "*")) else "/" + value
