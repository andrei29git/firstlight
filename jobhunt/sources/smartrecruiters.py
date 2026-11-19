"""SmartRecruiters: `GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N`;
each posting's description from its `ref` (the job ad's sections). Public API.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, remote_places
from .http import Http

PAGE_SIZE = 100
MAX_PAGES = 30


def listing_url(company_id: str, offset: int = 0) -> str:
    return (f"https://api.smartrecruiters.com/v1/companies/{quote(company_id, safe='')}/postings"
            f"?limit={PAGE_SIZE}&offset={offset}")


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    raws: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        data = http.get_json(listing_url(company.slug or "", page * PAGE_SIZE))
        raws += data.get("content") or []
        if not data.get("content") or len(raws) >= int(data.get("totalFound") or 0):
            break
    jobs = []
    for raw in raws:
        job = parse_listing(company.name, raw)
        if details(job) and raw.get("ref"):
            try:
                add_details(job, http.get_json(raw["ref"]))
            except SourceError:
                pass  # filled on a later run
        jobs.append(job)
    return jobs


def parse_listing(company: str, raw: dict[str, Any]) -> Job:
    location = raw.get("location") or {}
    places = [location.get("fullLocation") or ", ".join(p for p in (location.get("city"), location.get("country")) if p)]
    board = raw.get("company") or {}
    identifier = board.get("identifier") or ""
    return Job(company=company, title=str(raw.get("name") or "").strip(), source="smartrecruiters",
               source_id=str(raw["id"]), url=f"https://jobs.smartrecruiters.com/{identifier}/{raw['id']}",
               locations=remote_places(places, bool(location.get("remote"))), posted=iso_date(raw.get("releasedDate")),
               department=(raw.get("department") or {}).get("label"),
               employment_type=(raw.get("typeOfEmployment") or {}).get("label"), board_company=board.get("name"))


def add_details(job: Job, data: dict[str, Any]) -> None:
    sections = (data.get("jobAd") or {}).get("sections") or {}
    job.description = "\n\n".join(t for t in (html_to_text((sections.get(k) or {}).get("text"))
                                              for k in ("jobDescription", "qualifications", "additionalInformation")) if t)
    job.url = data.get("postingUrl") or job.url
