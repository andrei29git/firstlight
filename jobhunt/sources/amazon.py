"""Amazon: `GET https://www.amazon.jobs/en/search.json` (2026-10-29), the search behind
amazon.jobs, which robots.txt allows (it disallows only the internal-jobs pages).

Filtered by country (`countries` in companies.toml, ISO alpha-3 as Amazon writes them), newest
first, 100 a page. Each job carries its title, description and qualifications, location
("Milan, Lombardy, ITA"), posting date and path; no detail page is needed.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text
from .http import Http

BASE = "https://www.amazon.jobs"
PAGE = 100
MAX_PAGES = 30


def search_url(countries: list[str], offset: int) -> str:
    query = [("normalized_country_code[]", c) for c in countries] + [
        ("result_limit", PAGE), ("offset", offset), ("sort", "recent")]
    return f"{BASE}/en/search.json?{urlencode(query)}"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    countries = [str(c).upper() for c in company.extra.get("countries", [])]
    if not countries:
        raise SourceError(f"{company.name}: set countries (ISO alpha-3) for the Amazon search")
    jobs: dict[str, Job] = {}
    total = None
    for page in range(MAX_PAGES):
        url = search_url(countries, page * PAGE)
        if not http.allowed_by_robots(url):
            raise SourceError(f"robots.txt disallows {url}")
        data = http.get_json(url)
        if data.get("error"):
            raise SourceError(f"{company.name}: Amazon search error {data['error']}")
        total = int(data.get("hits") or 0)
        batch = data.get("jobs") or []
        for raw in batch:
            job = parse(company.name, raw)
            jobs.setdefault(job.source_id, job)
        if not batch or (page + 1) * PAGE >= total:
            break
    return list(jobs.values())


def parse(company: str, raw: dict) -> Job:
    try:
        posted = datetime.strptime(raw.get("posted_date") or "", "%B %d, %Y").date().isoformat()
    except ValueError:
        posted = None
    text = "\n\n".join(part for part in (
        html_to_text(raw.get("description")),
        "Basic qualifications\n" + html_to_text(raw["basic_qualifications"]) if raw.get("basic_qualifications") else "",
        "Preferred qualifications\n" + html_to_text(raw["preferred_qualifications"]) if raw.get("preferred_qualifications") else "",
    ) if part)
    return Job(company=company, title=(raw.get("title") or "").strip(), url=BASE + (raw.get("job_path") or ""),
               source="amazon", source_id=str(raw.get("id_icims") or raw.get("id")),
               locations=[raw["normalized_location"]] if raw.get("normalized_location") else [],
               description=text, posted=posted, department=raw.get("job_category") or None,
               employment_type="Internship" if raw.get("is_intern") else (raw.get("job_schedule_type") or None))
