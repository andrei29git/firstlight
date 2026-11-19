"""Recruitee offers API, common with Dutch companies."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..companies import Company
from .base import Job, WantDetails, all_details, html_to_text, iso_date, unique, with_country
from .http import Http


def listing_url(slug: str) -> str:
    return f"https://{quote(slug, safe='')}.recruitee.com/api/offers/"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    return parse(company.name, http.get_json(listing_url(company.slug or "")))


def parse(company: str, data: dict[str, Any]) -> list[Job]:
    jobs = []
    for raw in data.get("offers", []):
        if raw.get("status", "published") != "published":
            continue
        places = [with_country(p.get("city") or p.get("name"), p.get("country")) for p in raw.get("locations") or []]
        locations = unique(places) or unique([raw.get("location")])
        if raw.get("remote") and not any("remote" in p.lower() for p in locations):
            locations.append("Remote")
        description = "\n\n".join(t for t in (html_to_text(raw.get("description")),
                                                html_to_text(raw.get("requirements"))) if t)
        jobs.append(Job(
            company=company,
            title=raw["title"].strip(),
            url=raw.get("careers_url") or raw.get("careers_apply_url"),
            source="recruitee",
            source_id=str(raw["id"]),
            locations=locations,
            description=description,
            posted=iso_date(raw.get("published_at")),
            deadline=iso_date(raw.get("close_at")),
            department=raw.get("department"),
            employment_type=raw.get("employment_type_code"),
        ))
    return jobs
