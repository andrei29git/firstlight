"""Eightfold career sites: the search API behind them (2026-10-29), used by Microsoft.

robots.txt on apply.careers.microsoft.com disallows everything except named paths, among them
`/careers` and this API (`/api/pcsx`). Listing: `GET {url}/api/pcsx/search?domain={slug}&query=
&location={country}&start=N&sort_by=timestamp`, ten positions a page with the total in `count`:
name, locations ("Netherlands, Noord-Holland, Amsterdam"), posting time, department and the
position's page. Detail (for roles the pipeline wants): `GET {url}/api/pcsx/position_details?
position_id={id}&domain={slug}&hl=en`, with the description as HTML.

Config: `url` (the career site's origin), `slug` (the Eightfold domain, "microsoft.com") and
`countries` (names as the site writes them, one search each).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text
from .http import Http

PAGE = 10
MAX_PAGES = 50


def search_url(origin: str, domain: str, country: str, start: int) -> str:
    query = {"domain": domain, "query": "", "location": country, "start": start, "sort_by": "timestamp"}
    return f"{origin}/api/pcsx/search?{urlencode(query)}"


def detail_url(origin: str, domain: str, position_id: str) -> str:
    return f"{origin}/api/pcsx/position_details?{urlencode({'position_id': position_id, 'domain': domain, 'hl': 'en'})}"


def _data(http: Http, url: str, company: str) -> dict[str, Any]:
    if not http.allowed_by_robots(url):
        raise SourceError(f"robots.txt disallows {url}")
    answer = http.get_json(url)
    if not isinstance(answer, dict) or not isinstance(answer.get("data"), dict):
        raise SourceError(f"{company}: unexpected Eightfold response")
    return answer["data"]


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    origin, domain = (company.url or "").rstrip("/"), company.slug or ""
    countries = [str(c) for c in company.extra.get("countries", [])]
    if not origin or not domain or not countries:
        raise SourceError(f"{company.name}: an Eightfold board needs url, slug (its domain) and countries")
    jobs: dict[str, Job] = {}
    for country in countries:
        for page in range(MAX_PAGES):
            data = _data(http, search_url(origin, domain, country, page * PAGE), company.name)
            positions = data.get("positions") or []
            for raw in positions:
                job = parse(company.name, raw, origin)
                jobs.setdefault(job.source_id, job)
            if not positions or (page + 1) * PAGE >= int(data.get("count") or 0):
                break
    for job in jobs.values():
        if details(job):
            try:
                data = _data(http, detail_url(origin, domain, job.source_id), company.name)
                job.description = html_to_text(data.get("jobDescription")) or job.description
            except SourceError:
                pass  # the listing's data stays; the text is filled in on a later run
    return list(jobs.values())


def parse(company: str, raw: dict[str, Any], origin: str) -> Job:
    posted = None
    if isinstance(raw.get("postedTs"), (int, float)):
        posted = datetime.fromtimestamp(raw["postedTs"], UTC).date().isoformat()
    # "Netherlands, Noord-Holland, Amsterdam" names the country (the standardized form ends in a
    # code that can read as another country: "Berlin, BE, DE").
    return Job(company=company, title=(raw.get("name") or "").strip(), url=origin + (raw.get("positionUrl") or ""),
               source="eightfold", source_id=str(raw.get("id")), locations=list(raw.get("locations") or []),
               posted=posted, department=raw.get("department") or None)
