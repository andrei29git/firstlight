"""Ashby posting API. Slugs can contain dots (mistral.ai)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..companies import Company
from .base import Job, WantDetails, all_details, html_to_text, iso_date, unique, with_country
from .http import Http

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def listing_url(slug: str) -> str:
    return API.format(slug=quote(slug, safe="."))


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    return parse(company.name, http.get_json(listing_url(company.slug or "")))


def _country(entry: dict[str, Any]) -> str | None:
    return (((entry.get("address") or {}).get("postalAddress")) or {}).get("addressCountry")


def parse(company: str, data: dict[str, Any]) -> list[Job]:
    jobs = []
    for raw in data.get("jobs", []):
        if raw.get("isListed") is False:
            continue
        locations = [with_country(raw.get("location"), _country(raw))]
        locations += [with_country(s.get("location"), _country(s)) for s in raw.get("secondaryLocations") or []]
        jobs.append(Job(
            company=company,
            title=raw["title"].strip(),
            url=raw["jobUrl"],
            source="ashby",
            source_id=raw["id"],
            locations=unique(locations),
            description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
            posted=iso_date(raw.get("publishedAt")),
            department=raw.get("department") or raw.get("team"),
            employment_type=raw.get("employmentType"),
        ))
    return jobs
