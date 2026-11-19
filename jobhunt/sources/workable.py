"""Workable: `GET https://apply.workable.com/api/v1/widget/accounts/{slug}` lists every published
job in one request, with the company's name; each job's description comes from
`GET https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}`. The career-site widget's
API, best effort, at the normal per-host delay (robots.txt allows everything). Unknown accounts
answer 404. (The v3 jobs API used before pages ten jobs at a time.)
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, remote_places, with_country
from .http import Http

BASE = "https://apply.workable.com/api"


def listing_url(slug: str) -> str:
    return f"{BASE}/v1/widget/accounts/{quote(slug, safe='')}"


def detail_url(slug: str, shortcode: str) -> str:
    return f"{BASE}/v2/accounts/{quote(slug, safe='')}/jobs/{quote(shortcode, safe='')}"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    slug = company.slug or ""
    data = http.get_json(listing_url(slug))
    jobs = [parse_listing(company.name, slug, raw, data.get("name")) for raw in data["jobs"]]
    for job in jobs:
        if details(job):
            try:
                add_details(job, http.get_json(detail_url(slug, job.source_id)))
            except SourceError:
                pass  # filled on a later run
    return jobs


def parse_listing(company: str, slug: str, raw: dict[str, Any], board_company: str | None = None) -> Job:
    places = [with_country(p.get("city"), p.get("country")) for p in raw.get("locations") or [] if not p.get("hidden")]
    if not places:
        places = [with_country(raw.get("city"), raw.get("country"))]
    return Job(company=company, title=str(raw.get("title") or "").strip(), source="workable",
               source_id=str(raw["shortcode"]), url=f"https://apply.workable.com/{slug}/j/{raw['shortcode']}/",
               locations=remote_places(places, bool(raw.get("telecommuting"))),
               posted=iso_date(raw.get("published_on") or raw.get("created_at")),
               department=raw.get("department") or None, employment_type=raw.get("employment_type") or None,
               board_company=board_company)


def add_details(job: Job, detail: dict[str, Any]) -> None:
    job.description = "\n\n".join(t for t in (html_to_text(detail.get(k)) for k in
                                            ("description", "requirements", "benefits")) if t)
