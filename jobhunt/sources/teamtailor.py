"""Teamtailor career sites: `GET https://{slug}.teamtailor.com/jobs.rss`.

The feed carries each job's full description, publication date, department and locations
(city and country); it is the career site's own page, so robots.txt is checked.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, remote_places, with_country
from .http import Http

TT = "{https://teamtailor.com/locations}"


def feed_url(slug: str) -> str:
    return f"https://{quote(slug, safe='')}.teamtailor.com/jobs.rss"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    url = feed_url(company.slug or "")
    if not http.allowed_by_robots(url):
        raise SourceError(f"robots.txt disallows {url}")
    return parse(company.name, http.get(url).content)  # bytes: the XML's own encoding applies


def parse(company: str, xml: str | bytes) -> list[Job]:
    try:
        root = ET.fromstring(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    except ET.ParseError as e:
        raise SourceError(f"{company}: the Teamtailor feed is not XML ({e})") from e
    jobs = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        match = re.search(r"/jobs/(\d+)", link)
        places = [with_country(loc.findtext(f"{TT}city"), loc.findtext(f"{TT}country"))
                  for loc in item.findall(f"{TT}locations/{TT}location")]
        remote = (item.findtext("remoteStatus") or "").strip() in ("fully", "remote")
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            posted = None
        jobs.append(Job(
            company=company, title=(item.findtext("title") or "").strip(), url=link, source="teamtailor",
            source_id=match.group(1) if match else (item.findtext("guid") or link),
            locations=remote_places(places, remote), description=html_to_text(item.findtext("description")), posted=posted,
            department=(item.findtext(f"{TT}department") or "").strip() or None))
    return jobs
