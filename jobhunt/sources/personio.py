"""Personio: `GET https://{slug}.jobs.personio.de/xml?language=en` (some accounts are on .com,
tried when .de fails).

The feed lists every open position with its offices and creation date; the description is in the
feed when the account fills it in, else on the position's page (schema.org JobPosting).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..companies import Company
from . import custom
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, unique
from .http import Http

DOMAINS = ("de", "com")


def feed_url(slug: str, domain: str = "de") -> str:
    return f"https://{quote(slug, safe='')}.jobs.personio.{domain}/xml?language=en"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    slug = company.slug or ""
    last: SourceError | None = None
    for domain in DOMAINS:
        try:
            # The bytes, not .text: the feed declares no charset in its headers, so requests read
            # it as Latin-1 ("MÃ¼nchen"); the XML itself says UTF-8 (fixed 2026-10-29).
            jobs = parse(company.name, slug, domain, http.get(feed_url(slug, domain)).content)
        except SourceError as e:
            last = e
            continue
        for job in jobs:
            if not job.description and details(job) and http.allowed_by_robots(job.url):
                try:
                    read_page(job, custom.text_of(http.get(job.url)))
                except SourceError:
                    pass  # filled on a later run
        return jobs
    raise last or SourceError(f"{company.name}: no Personio feed")


def parse(company: str, slug: str, domain: str, xml: str | bytes) -> list[Job]:
    try:
        root = ET.fromstring(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    except ET.ParseError as e:
        raise SourceError(f"{company}: the Personio feed is not XML ({e})") from e
    if root.tag != "workzag-jobs":
        raise SourceError(f"{company}: not a Personio feed (<{root.tag}>)")
    jobs = []
    for position in root.iter("position"):
        text = lambda tag: (position.findtext(tag) or "").strip()  # noqa: E731
        offices = [text("office")] + [o.text or "" for o in position.findall("additionalOffices/office")]
        parts = [f"{d.findtext('name') or ''}\n{html_to_text(d.findtext('value'))}".strip()
                 for d in position.findall("jobDescriptions/jobDescription")]
        jobs.append(Job(
            company=company, title=text("name"), source="personio", source_id=text("id"),
            url=f"https://{slug}.jobs.personio.{domain}/job/{text('id')}?language=en",
            locations=unique(offices), description="\n\n".join(p for p in parts if p),
            posted=iso_date(text("createdAt")), department=text("department") or None,
            employment_type=text("employmentType") or None, board_company=text("subcompany") or None))
    return jobs


def read_page(job: Job, markup: str) -> None:
    """The description from the job's page (schema.org JobPosting). The listing's locations stay:
    they name the country in words, where the page often has only a lower-case code; so does its
    title (a page without JSON-LD gave "<role> | Jobs at <company>" until 2026-10-31)."""
    title, locations = job.title, job.locations
    custom.fill_from_page(job, markup)
    job.title = title or job.title
    job.locations = locations or job.locations
