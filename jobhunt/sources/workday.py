"""Workday career sites (the cxs JSON API behind *.myworkdayjobs.com).

The listing gives only titles and coded locations, so details (description, real locations,
posting date) are fetched per job, and only for titles the caller wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, unique, with_country
from .http import Http

PAGE_SIZE = 20  # the API's maximum
MAX_PAGES = 150  # 3,000 jobs: a safety stop, well above any target board
LOCALE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


@dataclass(frozen=True)
class Site:
    host: str
    tenant: str
    site: str

    @classmethod
    def from_url(cls, url: str) -> "Site":
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s and not LOCALE.match(s)]
        if not parts.netloc.endswith(".myworkdayjobs.com") or not segments:
            raise SourceError(f"not a Workday careers URL: {url}")
        return cls(host=parts.netloc, tenant=parts.netloc.split(".")[0], site=segments[0])

    @property
    def api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def public(self) -> str:
        return f"https://{self.host}/{self.site}"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    site = Site.from_url(company.url or "")
    postings: dict[str, dict[str, Any]] = {}
    countries = [str(c) for c in company.extra.get("countries", [])]
    facets = country_facets(http, site, countries) if countries else None
    if countries and not facets:
        return []  # no location there: nothing to read (a run sees an empty board, which closes nothing it never had)
    for text in company.search or ("",):
        for raw in _list(http, site, text, company.search_pages or MAX_PAGES, facets):
            # Entries without a path or a title are not postings (seen at Blackstone, 2026-10-27, and a
            # healthcare site with no title, 2026-10-28): skip them rather than fail the whole site.
            if raw.get("externalPath") and str(raw.get("title") or "").strip():
                postings.setdefault(raw["externalPath"], raw)
    for text in company.local_search:  # local entry-level words (Werkstudent, stagiaire): one page each
        for raw in _list(http, site, text, 1):
            if raw.get("externalPath") and str(raw.get("title") or "").strip():
                postings.setdefault(raw["externalPath"], raw)
    jobs = []
    for raw in postings.values():
        job = parse_listing(company.name, site, raw)
        if details(job):
            try:
                add_details(job, http.get_json(site.api + raw["externalPath"]))
            except SourceError:
                pass  # keep the listing data; an empty description is filled on a later run
        jobs.append(job)
    return jobs


def country_facets(http: Http, site: Site, countries: list[str]) -> dict[str, list[str]]:
    """Workday's location facet values in the given countries (2026-10-29): "Romania", "Bucharest,
    Romania", "Remote Germany". A location facet may group child facets with their own parameters
    ("locationCountry" inside "locationMainGroup"); the country-level one is preferred. Read from
    the first listing page; empty when the site has none."""
    data = http.post_json(site.api + "/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    names = [c.lower() for c in countries]

    def matches(value: dict) -> bool:
        text = str(value.get("descriptor") or "").lower().rstrip(")")
        return any(text.endswith(n) or text == f"remote {n}" for n in names)
    found: dict[str, list[str]] = {}
    for facet in data.get("facets") or []:
        if not str(facet.get("facetParameter") or "").lower().startswith("location"):
            continue
        for value in facet.get("values") or []:
            if value.get("values"):  # a group of child facets
                ids = [v["id"] for v in value["values"] if v.get("id") and matches(v)]
                if ids:
                    found.setdefault(value.get("facetParameter") or facet["facetParameter"], []).extend(ids)
            elif value.get("id") and matches(value):
                found.setdefault(facet["facetParameter"], []).append(value["id"])
    for preferred in ("locationCountry", "locationMainGroup", "locations"):
        if preferred in found:
            return {preferred: found[preferred]}
    return dict(list(found.items())[:1])


def _list(http: Http, site: Site, search_text: str, max_pages: int = MAX_PAGES,
          facets: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    total = None
    for page in range(max_pages):
        data = http.post_json(site.api + "/jobs", {"appliedFacets": facets or {}, "limit": PAGE_SIZE,
                                                    "offset": page * PAGE_SIZE, "searchText": search_text})
        if total is None:
            total = int(data.get("total") or 0)  # only the first page reports the real total
        batch = data.get("jobPostings") or []
        found += batch
        if not batch or len(found) >= total:
            break
    return found


def posted_from_text(text: str | None, today: date | None = None) -> str | None:
    """'Posted Today', 'Posted Yesterday', 'Posted 3 Days Ago' to a date; '30+ Days Ago' is unknown
    (see posted_before_from_text)."""
    today = today or date.today()
    text = (text or "").lower()
    if "today" in text:
        return today.isoformat()
    if "yesterday" in text:
        return (today - timedelta(days=1)).isoformat()
    match = re.search(r"posted\s+(\d+)\s+days?\s+ago", text)
    return (today - timedelta(days=int(match.group(1)))).isoformat() if match else None


def posted_before_from_text(text: str | None, today: date | None = None) -> str | None:
    """'Posted 30+ Days Ago': posted on or before 30 days ago."""
    match = re.search(r"posted\s+(\d+)\+\s+days?\s+ago", (text or "").lower())
    return ((today or date.today()) - timedelta(days=int(match.group(1)))).isoformat() if match else None


def parse_listing(company: str, site: Site, raw: dict[str, Any]) -> Job:
    bullets = raw.get("bulletFields") or []
    source_id = bullets[0] if bullets else raw["externalPath"].rsplit("_", 1)[-1]
    locations_text = raw.get("locationsText") or ""
    return Job(
        company=company,
        title=raw["title"].strip(),
        url=site.public + raw["externalPath"],
        source="workday",
        source_id=source_id,
        # "2 Locations" is a count, not a place; the detail has the real ones.
        locations=[] if re.fullmatch(r"\d+ Locations", locations_text) else unique([locations_text]),
        posted=posted_from_text(raw.get("postedOn")),  # approximate; the detail's startDate replaces it
        posted_before=posted_before_from_text(raw.get("postedOn")),
    )


def add_details(job: Job, data: dict[str, Any]) -> None:
    info = data.get("jobPostingInfo") or {}
    requisition = info.get("jobRequisitionLocation") or {}
    country = ((requisition.get("country") or {}).get("descriptor")
               or (info.get("country") or {}).get("descriptor"))
    primary = with_country(info.get("location"), country)
    job.locations = unique([primary, *(info.get("additionalLocations") or [])]) or job.locations
    job.description = html_to_text(info.get("jobDescription"))
    # `startDate` is the date the posting went live (the listing only says "Posted 30+ Days Ago").
    job.posted = iso_date(info.get("startDate"))
    job.employment_type = info.get("timeType")
    job.url = info.get("externalUrl") or job.url
