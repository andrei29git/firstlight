"""robots.txt read the way the standard says (RFC 9309, 2026-10-29), not first-match as Python's
`urllib.robotparser` does.

The group for our own product token is used when there is one, else the `*` group. Within it, the
rule with the longest matching path wins, Allow on a tie; `*` matches any characters and a final
`$` anchors the end. So "Disallow: /" with "Allow: /api/pcsx" (Microsoft's careers site) allows
/api/pcsx/search and nothing else. Crawl-delay is read from the same group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, path pattern)
    crawl_delay: float | None = None


def parse(text: str) -> list[Group]:
    groups: list[Group] = []
    current: Group | None = None
    in_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not in_agents:
                current = Group()
                groups.append(current)
            current.agents.append(value.lower())
            in_agents = True
            continue
        in_agents = False
        if current is None:
            continue
        if key in ("allow", "disallow") and value:
            current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                pass
    return groups


def _pattern(path: str) -> re.Pattern[str]:
    anchored = path.endswith("$")
    body = re.escape(path[:-1] if anchored else path).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


@dataclass
class Robots:
    groups: list[Group]

    @classmethod
    def from_text(cls, text: str) -> "Robots":
        return cls(parse(text))

    def group(self, user_agent: str) -> Group | None:
        token = re.split(r"[/\s]", user_agent.strip().lower(), maxsplit=1)[0]
        mine = [g for g in self.groups if any(a != "*" and a and a in token for a in g.agents)]
        if mine:
            return Group(rules=[r for g in mine for r in g.rules], crawl_delay=mine[0].crawl_delay)
        anyone = [g for g in self.groups if "*" in g.agents]
        if anyone:
            return Group(rules=[r for g in anyone for r in g.rules], crawl_delay=anyone[0].crawl_delay)
        return None

    def allowed(self, user_agent: str, url: str) -> bool:
        group = self.group(user_agent)
        if group is None:
            return True
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
        best: tuple[int, bool] | None = None  # (length of the matching rule, allow)
        for allow, rule in group.rules:
            if _pattern(rule).match(path):
                length = len(rule)
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self.group(user_agent)
        return group.crawl_delay if group else None
