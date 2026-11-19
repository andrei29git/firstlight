"""Target companies from companies.toml.

The file is hand-edited and versioned, so the only thing the tool writes back is the `verified`
date, edited in place so comments and layout survive.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

FILE = "companies.toml"


@dataclass(frozen=True)
class Company:
    name: str
    board: str | None = None
    slug: str | None = None  # greenhouse, lever, ashby, recruitee
    url: str | None = None  # workday careers URL
    region: str | None = None  # lever: "eu" for api.eu.lever.co
    search: tuple[str, ...] = ()  # workday: search texts; empty fetches the whole board
    search_pages: int | None = None  # workday: result pages read per search text (None: all)
    local_search: tuple[str, ...] = ()  # workday: local words for the site's countries, one page each
    listing_url: str | None = None  # custom
    link_pattern: str | None = None  # custom
    location_pattern: str | None = None  # custom: regex with a (?P<location>...) group, read from job URLs
    postings_only: bool = False  # custom: keep only pages with schema.org JobPosting data
    partial: bool = False  # the listing shows only some jobs, so a missing job is not closed
    render: bool = False  # custom: a JavaScript-only site, rendered with Playwright (laptop only)
    render_pages: int | None = None  # custom with render: listing pages 1..N, "{page}" in listing_url
    render_wait: str | None = None  # custom with render: a CSS selector that shows the jobs have loaded
    # Roles whose every location is in these countries are parked (stored, not scored, kept out of
    # the app), as the generated list's UK and US roles are: for a site whose search cannot leave them out.
    park_countries: tuple[str, ...] = ()
    manual: bool = False
    fetch_from: str = "anywhere"  # or "local": the site blocks cloud IPs
    wishlist: bool = False
    verified: date | None = None
    todo: str | None = None
    note: str | None = None
    origin: str = "config"  # "config" (companies.toml) or "generated" (the harvest's list)
    identity: str | None = None  # generated companies: their harvest identity, e.g. greenhouse:acme
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Company":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        values = {k: v for k, v in raw.items() if k in known}
        for key in ("search", "local_search", "park_countries"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values, extra={k: v for k, v in raw.items() if k not in known})

    @property
    def account(self) -> str:
        """Which board account this is: two boards of one company (two Greenhouse slugs, say) are
        told apart by it, so fetching one never closes the other's roles."""
        return f"{self.board}:{self.region or ''}:{self.slug or self.url or self.listing_url or self.name}"

    @property
    def fetchable(self) -> bool:
        """Has a board configured and is not checked by hand or waiting for `detect`."""
        return bool(self.board) and not self.manual and not self.todo


def load_companies(config: Path) -> list[Company]:
    """companies.toml. An entry with `rules = "generated"` (2026-10-29: large employers added for the
    main countries) is yours to configure but follows the generated list's rules: fetched once a
    day in its slot, graduate wording needed, title check, Haiku then Sonnet. Starring it in the app
    makes it a wishlist company like any other of yours (see cli.run_targets)."""
    with (config / FILE).open("rb") as f:
        return [Company.from_dict({**c, "origin": "generated"} if c.get("rules") == "generated" else c)
                for c in tomllib.load(f).get("company", [])]


def set_verified(config: Path, name: str, day: date) -> bool:
    """Set `verified = <day>` in the named company's block. Returns False if the company is missing."""
    path = config / FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = _block_start(lines, name)
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    entry = f"verified = {day.isoformat()}\n"
    for i in range(start + 1, end):
        if re.match(r"\s*verified\s*=", lines[i]):
            lines[i] = entry
            break
    else:
        # Insert after the block's last key line, so trailing blank lines and comments stay put.
        last = max((i for i in range(start, end) if re.match(r"\s*[A-Za-z_]+\s*=", lines[i])), default=start)
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, entry)
    path.write_text("".join(lines), encoding="utf-8")
    return True


def _block_start(lines: list[str], name: str) -> int | None:
    header = None
    for i, line in enumerate(lines):
        if line.strip() == "[[company]]":
            header = i
        elif header is not None and re.match(r"\s*name\s*=", line):
            if tomllib.loads(line.strip()).get("name") == name:
                return header
            header = None
    return None
