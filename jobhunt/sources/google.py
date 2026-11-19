"""Google: its careers search page (2026-10-31), read without a browser. The page embeds the jobs it
lists as JSON (`AF_initDataCallback({key: 'ds:1', ... data: [...]})`): per job its id, title, places
(with country codes), posting time and full text, so no detail page is needed.

robots.txt disallows paging, so each search gives only its newest 20 jobs; it allows filters, so
one search runs per place in `locations` (companies.toml, as Google's own filter writes them:
"Netherlands", "Germany"), with the rest of `listing_url`'s query (early-career levels, newest first).
The listing is partial: a job it has not shown for 21 days counts as gone.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, unique
from .custom import text_of
from .http import Http

JOB_URL = "https://www.google.com/about/careers/applications/jobs/results/{id}"
DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel: \{\}\}\);</script>",
                  re.DOTALL)


def search_url(listing_url: str, location: str | None) -> str:
    if not location:
        return listing_url
    return listing_url + ("&" if "?" in listing_url else "?") + urlencode({"location": location})


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    if not company.listing_url:
        raise SourceError(f"{company.name}: set listing_url (Google's careers search) for the Google board")
    jobs: dict[str, Job] = {}
    for location in company.extra.get("locations") or [None]:
        url = search_url(company.listing_url, location)
        if not http.allowed_by_robots(url):
            raise SourceError(f"robots.txt disallows {url}")
        for raw in listed(text_of(http.get(url))):
            job = parse(company.name, raw)
            jobs.setdefault(job.source_id, job)
    return list(jobs.values())


def listed(markup: str) -> list[list[Any]]:
    """The jobs a search page embeds."""
    match = DATA.search(markup)
    if not match:
        raise SourceError("Google careers page without its job data (the page layout changed?)")
    try:
        data = json.loads(match.group(1))
        return [raw for raw in data[0] or [] if isinstance(raw, list) and len(raw) > 12]
    except (ValueError, IndexError, TypeError) as e:
        raise SourceError(f"Google careers job data unreadable: {e}") from e


def _html(raw: list[Any], index: int) -> str:
    part = raw[index] if len(raw) > index else None
    return html_to_text(part[1]) if isinstance(part, list) and len(part) > 1 and part[1] else ""


def parse(company: str, raw: list[Any]) -> Job:
    """One job: [0] id, [1] title, [3] responsibilities, [4] qualifications, [9] places
    ([display name, lines, city, _, region, country code]), [10] about the job, [12] posted (epoch s)."""
    try:
        job_id, title = str(raw[0]), " ".join(str(raw[1]).split())
    except (IndexError, TypeError) as e:
        raise SourceError(f"Google job without an id or title: {e}") from e
    places = unique(str(p[0]) for p in raw[9] or [] if isinstance(p, list) and p and p[0])
    stamp = raw[12][0] if isinstance(raw[12], list) and raw[12] and isinstance(raw[12][0], (int, float)) else None
    text = "\n\n".join(t for t in (_html(raw, 10), "Responsibilities\n" + _html(raw, 3) if _html(raw, 3) else "",
                                   _html(raw, 4)) if t)
    return Job(company=company, title=title, url=JOB_URL.format(id=job_id), source="google", source_id=job_id,
               locations=places, description=text,
               posted=datetime.fromtimestamp(stamp, tz=UTC).date().isoformat() if stamp else None)
