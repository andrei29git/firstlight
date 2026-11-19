"""The common Job object and the small helpers every adapter shares."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Callable

# Decides, from the listing alone (title, URL, key), whether a job is worth a detail request:
# the pipeline uses it to skip titles the pre-filter drops and jobs it already has. Boards whose
# listing carries the full posting (Greenhouse, Lever, Ashby, Recruitee) ignore it.
WantDetails = Callable[["Job"], bool]


def all_details(job: "Job") -> bool:
    return True


def no_details(job: "Job") -> bool:
    return False


class SourceError(Exception):
    """A fetch failed. A failed fetch never closes jobs, so callers must not treat it as 'no jobs'."""


@dataclass
class Job:
    company: str
    title: str
    url: str
    source: str
    source_id: str
    locations: list[str] = field(default_factory=list)
    description: str = ""  # plain text; empty when details were not fetched
    posted: str | None = None  # ISO date
    deadline: str | None = None  # ISO date
    department: str | None = None
    employment_type: str | None = None
    board_company: str | None = None  # the company name as the board states it, when it does
    # Posted on or before this date, when the board says only that (Workday's "Posted 30+ Days
    # Ago"): for recency decisions, never shown as the posting date.
    posted_before: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.source_id}"


BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table",
              "section", "article", "header", "footer", "blockquote", "pre", "hr", "dd", "dt"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self.skipping += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(markup: str | None) -> str:
    """Visible text of an HTML fragment, one block per line. Entities are decoded."""
    if not markup:
        return ""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def escaped_html_to_text(markup: str | None) -> str:
    """For boards that entity-escape their HTML (Greenhouse): unescape, strip tags, unescape again."""
    return html_to_text(html.unescape(markup or ""))


def iso_date(value: str | int | float | None) -> str | None:
    """ISO date from an ISO timestamp, a 'YYYY-MM-DD hh:mm:ss UTC' string or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
    match = re.match(r"\d{4}-\d{2}-\d{2}", str(value).strip())
    return match.group(0) if match else None


def with_country(place: str | None, country: str | None) -> str:
    """'Paris' + 'France' -> 'Paris, France', so the pre-filter can see the country."""
    place = (place or "").strip().rstrip(",")
    country = (country or "").strip()
    if not country or country.lower() in place.lower():
        return place or country
    return f"{place}, {country}" if place else country


MOJIBAKE = re.compile("[\u00c2-\u00c3\u00e2][\u0080-\u00bf\u20ac\u201a-\u203a\u0152\u0160\u0178\u017d]")


def repair_mojibake(text: str) -> str:
    """Text that was UTF-8 but read as Latin-1 or Windows-1252 ("MÃ¼nchen", "Intern â€” Data"),
    back to what it said. Left alone unless it decodes cleanly."""
    if not text or not MOJIBAKE.search(text):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        return fixed
    return text


def remote_places(places: list[str], remote: bool) -> list[str]:
    """A remote role keeps the place the board ties it to ("Remote, Colombia"), so the country
    rule still applies; only a remote role with no place named is plain "Remote"."""
    places = unique(places)
    if not remote:
        return places
    return [f"Remote, {p}" for p in places] or ["Remote"]


def unique(items) -> list[str]:
    return list(dict.fromkeys(i.strip() for i in items if i and i.strip()))
