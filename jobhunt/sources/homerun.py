"""Homerun career sites: `GET https://feed.homerun.co/{slug}` (2026-10-29).

Each company's public Atom feed, linked from its own career page ({slug}.homerun.co), with every
open job: title, link (the job page, sometimes on the company's own domain), id, the full HTML
description, department, location names (usually a city) and contract type. The only date is
`updated`, used as the posting date: an edited job looks newer than it is. The feed host has no
robots.txt (404, which allows everything); it is checked anyway. Common with Dutch companies.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, unique
from .http import Http

ATOM = "{http://www.w3.org/2005/Atom}"


def feed_url(slug: str) -> str:
    return f"https://feed.homerun.co/{quote(slug, safe='')}"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    url = feed_url(company.slug or "")
    if not http.allowed_by_robots(url):
        raise SourceError(f"robots.txt disallows {url}")
    return parse(company.name, http.get(url).content)  # bytes: the XML's own encoding applies


def _text(element: ET.Element | None, *path: str) -> str:
    for step in path:
        if element is None:
            return ""
        element = element.find(ATOM + step) if element.find(ATOM + step) is not None else element.find(step)
    return (element.text or "").strip() if element is not None else ""


def parse(company: str, xml: str | bytes) -> list[Job]:
    try:
        root = ET.fromstring(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    except ET.ParseError as e:
        raise SourceError(f"{company}: the Homerun feed is not XML ({e})") from e
    if root.tag not in (f"{ATOM}feed", "feed"):
        raise SourceError(f"{company}: not a Homerun feed (root {root.tag})")
    jobs = []
    for entry in [*root.iter(f"{ATOM}entry"), *root.iter("entry")]:
        link = next((x.get("href") for x in [*entry.findall(f"{ATOM}link"), *entry.findall("link")]
                     if x.get("rel", "alternate") == "alternate" and x.get("href")), "")
        link = re.sub(r"/apply/?$", "", link)
        places = unique([_text(loc, "name") for loc in [*entry.findall(f"{ATOM}location"), *entry.findall("location")]])
        places = [p for p in places if p.lower() not in ("other", "n/a")]
        updated = _text(entry, "updated")
        jobs.append(Job(
            company=company, title=_text(entry, "title"), url=link, source="homerun",
            source_id=_text(entry, "id") or link, locations=places,
            description=html_to_text(_text(entry, "description") or _text(entry, "content")),
            posted=updated[:10] if re.match(r"\d{4}-\d{2}-\d{2}", updated) else None,
            department=_text(entry, "department", "name") or None,
            employment_type=_text(entry, "type", "name") or None))
    return jobs
