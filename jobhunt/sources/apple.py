"""Apple: jobs.apple.com's search and job pages (2026-10-31), read without a browser. Both pages
embed their data as JSON (`window.__staticRouterHydrationData = JSON.parse("...")`): a search page
its 20 results with title, places (country names), team and posting time; a job page its full text.
Until then the site was rendered with Playwright, which left titles with " - Jobs - Careers at
Apple", gave no places, and could hang for minutes on a page.

`listing_url` is the search, with "{page}" for the page number; job pages are read only for the
roles the `details` hook wants (new ones that can be scored), at most `max_links` a run.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, unique
from .custom import DEFAULT_MAX_LINKS, text_of
from .http import Http

JOB_URL = "https://jobs.apple.com/en-us/details/{id}/{slug}?team={team}"
DATA = re.compile(r'window\.__staticRouterHydrationData = JSON\.parse\((".*?")\);', re.DOTALL)
PAGE = 20
MAX_PAGES = 30
SECTIONS = (("jobSummary", ""), ("description", "Description"), ("responsibilities", "Responsibilities"),
            ("minimumQualifications", "Minimum qualifications"), ("preferredQualifications", "Preferred qualifications"))


def embedded(markup: str) -> dict[str, Any]:
    """The page's own data (the router's `loaderData`)."""
    match = DATA.search(markup)
    if not match:
        raise SourceError("Apple jobs page without its data (the page layout changed?)")
    try:
        return json.loads(json.loads(match.group(1)))["loaderData"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"Apple jobs page data unreadable: {e}") from e


def places(locations: list[dict[str, Any]]) -> list[str]:
    """"Cork, Ireland", or the country alone ("Germany") for a role posted country-wide."""
    return unique(", ".join(p for p in (str(l.get("city") or ""), str(l.get("countryName") or l.get("name") or "")) if p)
                  for l in locations or [] if isinstance(l, dict))


def fetch(company: Company, http: Http, details: WantDetails = all_details,
          max_links: int = DEFAULT_MAX_LINKS) -> list[Job]:
    if not company.listing_url or "{page}" not in company.listing_url:
        raise SourceError(f"{company.name}: set listing_url (Apple's search, with {{page}}) for the Apple board")
    jobs: dict[str, Job] = {}
    for number in range(1, MAX_PAGES + 1):
        url = company.listing_url.replace("{page}", str(number))
        if not http.allowed_by_robots(url):
            raise SourceError(f"robots.txt disallows {url}")
        search = embedded(text_of(http.get(url))).get("search") or {}
        results = search.get("searchResults") or []
        for raw in results:
            job = parse(company.name, raw)
            jobs.setdefault(job.source_id, job)
        if not results or number * PAGE >= int(search.get("totalRecords") or 0):
            break
    budget = max_links
    for job in jobs.values():
        if budget > 0 and details(job) and http.allowed_by_robots(job.url):
            budget -= 1
            try:
                read_page(job, text_of(http.get(job.url)))
            except SourceError:
                pass  # the listing's data stays; the text is filled in on a later run
    return list(jobs.values())


def parse(company: str, raw: dict[str, Any]) -> Job:
    team = raw.get("team") or {}
    job_id, slug = str(raw["positionId"]), str(raw["transformedPostingTitle"])
    return Job(company=company, title=" ".join(str(raw["postingTitle"]).split()),
               url=JOB_URL.format(id=job_id, slug=slug, team=team.get("teamCode") or ""), source="apple",
               source_id=job_id, locations=places(raw.get("locations")),
               posted=str(raw.get("postDateInGMT") or "")[:10] or None, department=team.get("teamName") or None)


def read_page(job: Job, markup: str) -> None:
    """The job's full text from its page; its places too, should the search have given none."""
    data = (embedded(markup).get("jobDetails") or {}).get("jobsData") or {}
    parts = [(label, str(data.get(key) or "").strip()) for key, label in SECTIONS]
    job.description = "\n\n".join(f"{label}\n{text}" if label else text for label, text in parts if text)
    job.locations = job.locations or places(data.get("locations"))
