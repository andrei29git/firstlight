"""JOIN (join.com): a company's page, `https://join.com/companies/{slug}?page=N`, carries its open
jobs as JSON in the page (`__NEXT_DATA__`), five a page; a job's own page has schema.org
JobPosting data with the description. join.com pages: robots.txt is checked.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from ..companies import Company
from . import custom
from .base import Job, SourceError, WantDetails, all_details, iso_date, remote_places, with_country
from .http import Http

MAX_PAGES = 40


def page_url(slug: str, page: int = 1) -> str:
    return f"https://join.com/companies/{quote(slug, safe='')}" + (f"?page={page}" if page > 1 else "")


def page_data(markup: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', markup, re.S)
    if not match:
        raise SourceError("no __NEXT_DATA__ on the JOIN page")
    page = (json.loads(match.group(1)).get("props") or {}).get("pageProps") or {}
    for holder in ("pageData", "initialState"):  # two page layouts seen (2026-10-27)
        if isinstance(page.get(holder), dict) and isinstance(page[holder].get("jobs"), dict):
            return page[holder]["jobs"]
    raise SourceError("no job list on the JOIN page (the company page may be gone)")


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    slug = company.slug or ""
    if not http.allowed_by_robots(page_url(slug)):
        raise SourceError(f"robots.txt disallows {page_url(slug)}")
    jobs: list[Job] = []
    for page in range(1, MAX_PAGES + 1):
        data = page_data(http.get(page_url(slug, page)).text)
        jobs += parse(company.name, slug, data.get("items") or [])
        if page >= int((data.get("pagination") or {}).get("pageCount") or 1):
            break
    for job in jobs:
        if details(job) and http.allowed_by_robots(job.url):
            try:
                read_page(job, custom.text_of(http.get(job.url)))
            except SourceError:
                pass  # filled on a later run
    return jobs


def parse(company: str, slug: str, items: list[dict[str, Any]]) -> list[Job]:
    jobs = []
    for raw in items:
        city = raw.get("city") or {}
        place = with_country(city.get("cityName"), city.get("countryName") or (raw.get("country") or {}).get("iso3166"))
        jobs.append(Job(
            company=company, title=str(raw.get("title") or "").strip(), source="join", source_id=str(raw["id"]),
            url=f"https://join.com/companies/{slug}/{raw.get('idParam') or raw['id']}", locations=remote_places([place], raw.get("workplaceType") == "REMOTE"),
            posted=iso_date(raw.get("createdAt")), department=(raw.get("category") or {}).get("name"),
            employment_type=(raw.get("employmentType") or {}).get("name")))
    return jobs


def read_page(job: Job, markup: str) -> None:
    """The description from the job's page (schema.org JobPosting). The listing's locations stay:
    they name the country in words, where the page often has only a lower-case code; so does its
    title (a page without JSON-LD gave "<role> | Jobs at <company>" until 2026-10-31)."""
    title, locations = job.title, job.locations
    custom.fill_from_page(job, markup)
    job.title = title or job.title
    job.locations = locations or job.locations
