"""Lever postings API. Some companies are on the EU instance (region = "eu")."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, unique, with_country
from .http import Http

HOSTS = {None: "api.lever.co", "eu": "api.eu.lever.co"}


def listing_url(slug: str, region: str | None = None) -> str:
    return f"https://{HOSTS[region]}/v0/postings/{quote(slug, safe='')}?mode=json"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    data = http.get_json(listing_url(company.slug or "", company.region))
    if not isinstance(data, list):
        raise SourceError(f"unexpected Lever response for {company.name}")
    return parse(company.name, data)


def parse(company: str, data: list[dict[str, Any]]) -> list[Job]:
    jobs = []
    for raw in data:
        categories = raw.get("categories") or {}
        locations = unique(categories.get("allLocations") or [categories.get("location")])
        if len(locations) == 1:
            # `country` describes the primary location, so it is only safe to attach to a single one.
            locations = [with_country(locations[0], raw.get("country"))]
        jobs.append(Job(
            company=company,
            title=raw["text"].strip(),
            url=raw["hostedUrl"],
            source="lever",
            source_id=raw["id"],
            locations=locations,
            description=_description(raw),
            posted=iso_date(raw.get("createdAt")),
            department=categories.get("department") or categories.get("team"),
            employment_type=categories.get("commitment"),
        ))
    return jobs


def _description(raw: dict[str, Any]) -> str:
    parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description"))]
    for section in raw.get("lists") or []:
        parts.append(f"{section.get('text', '').strip()}\n{html_to_text(section.get('content'))}")
    parts.append(raw.get("additionalPlain") or html_to_text(raw.get("additional")))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
