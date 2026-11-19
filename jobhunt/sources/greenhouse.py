"""Greenhouse job board API. The listing carries the full posting with ?content=true."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, escaped_html_to_text, iso_date, unique
from .http import Http

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
JOB = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}"


def listing_url(slug: str, content: bool = True) -> str:
    """With content=False the listing has no descriptions, offices or departments (about 7 times
    smaller): enough to check a board and pre-filter titles and locations."""
    return API.format(slug=quote(slug, safe="")) + ("?content=true" if content else "")


def fetch(company: Company, http: Http, details: WantDetails = all_details, light: bool = False) -> list[Job]:
    """With `light`, the listing has no descriptions; each job the `details` hook wants is then
    fetched on its own (two-phase fetching: pre-filter on the light listing, details for survivors)."""
    slug = company.slug or ""
    jobs = parse(company.name, http.get_json(listing_url(slug, content=not light)))
    if not light:
        return jobs
    for i, job in enumerate(jobs):
        if details(job):
            try:
                raw = http.get_json(JOB.format(slug=quote(slug, safe=""), id=quote(job.source_id, safe="")))
                jobs[i] = parse(company.name, {"jobs": [raw]})[0]
            except (SourceError, KeyError, TypeError, ValueError):
                pass  # keep the listing data; the description is filled on a later run
    return jobs


def parse(company: str, data: dict[str, Any]) -> list[Job]:
    jobs = []
    for raw in data.get("jobs", []):
        # Offices carry the full place ("Amsterdam, Noord-Holland, Netherlands"); the location
        # name is often just the city. Fall back to it when no office has a location.
        offices = [o.get("location") for o in raw.get("offices") or []]
        locations = unique(offices) or unique([(raw.get("location") or {}).get("name")])
        departments = [d.get("name") for d in raw.get("departments") or []]
        jobs.append(Job(
            company=company,
            title=raw["title"].strip(),
            url=raw["absolute_url"],
            source="greenhouse",
            source_id=str(raw["id"]),
            locations=locations,
            description=escaped_html_to_text(raw.get("content")),
            posted=iso_date(raw.get("first_published")),
            deadline=iso_date(raw.get("application_deadline")),
            department=next(iter(unique(departments)), None),
            board_company=raw.get("company_name"),
        ))
    return jobs
