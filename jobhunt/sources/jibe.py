"""Jibe career sites (Google's career-site layer, often over iCIMS): jobs.booking.com and others.

The site's own pages read `GET {origin}/api/jobs?page=N&limit=100`; it returns full postings with
locations, dates and the requisition id. Config: `url` is the careers page that lists the jobs,
e.g. "https://jobs.booking.com/booking/jobs"; a job's page is that URL plus its slug. Not a
public ATS API, so robots.txt is checked (and its Crawl-delay honoured by Http).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlsplit

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, unique, with_country
from .http import Http

PAGE_SIZE = 100
MAX_PAGES = 50


def api_url(careers_url: str, page: int, location: str | None = None) -> str:
    parts = urlsplit(careers_url)
    query = {"page": page, "limit": PAGE_SIZE, **({"location": location} if location else {})}
    return f"{parts.scheme}://{parts.netloc}/api/jobs?" + urlencode(query)


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    """Every job, or one search per place in `locations` (companies.toml, 2026-11-18: Keysight
    lists about 800 jobs worldwide, about 120 for "Romania"; the match is loose, the pre-filter
    keeps the allowed countries)."""
    if not company.url:
        raise SourceError(f"{company.name}: a Jibe board needs url (its careers page)")
    first = api_url(company.url, 1)
    if not http.allowed_by_robots(first):
        raise SourceError(f"robots.txt disallows {first}")
    jobs: dict[str, Job] = {}
    for location in company.extra.get("locations") or [None]:
        found, total = 0, None
        for page in range(1, MAX_PAGES + 1):
            data = http.get_json(api_url(company.url, page, location))
            total = int(data.get("totalCount") or 0) if total is None else total
            batch = parse(company.name, company.url, data)
            for job in batch:
                jobs.setdefault(job.source_id, job)
            found += len(batch)
            if not batch or found >= total:
                break
    return list(jobs.values())


def location(place: dict[str, Any]) -> str:
    return with_country(place.get("city") or place.get("location_name"), place.get("country"))


def parse(company: str, careers_url: str, data: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        raw = item["data"]
        if raw.get("searchable") is False:
            continue  # hidden from the site's own search (the external/internal flags are false on all)
        locations = unique([location(raw)] + [location(p) for p in raw.get("additional_locations") or []])
        if raw.get("location_type") == "ANYWHERE" or "remote" in str(raw.get("full_location", "")).lower():
            locations.append("Remote")
        slug = str(raw.get("slug") or raw["req_id"])
        jobs.append(Job(
            company=company,
            title=raw["title"].strip(),
            url=f"{careers_url.rstrip('/')}/{slug}?lang={raw.get('language') or 'en-us'}",
            source="jibe",
            source_id=str(raw["req_id"]),
            locations=unique(locations),
            description=html_to_text(raw.get("description")),
            posted=iso_date(raw.get("posted_date") or raw.get("create_date")),
            deadline=None,  # posting_expiry_date is the listing's expiry, not an application deadline
            department=raw.get("department") or ", ".join(c.get("name", "") for c in raw.get("categories") or []) or None,
            employment_type=raw.get("employment_type"),
        ))
    return jobs
