"""Avature career sites: their public `SearchJobs` pages (2026-10-29), used by Macquarie, EA and
TenneT.

The search pages are rendered on the server: each job is an `<article>` with a link to its
`JobDetail` page, its title and (in one of a few shapes) its location. robots.txt allows
`/careers` on these sites and disallows only URLs with `qtvc=`, which are never requested. Pages
are read with `jobOffset`, stepping by the page size the site's own pager states, until the
stated total is reached or a page brings nothing new. A job's page (JobPosting JSON-LD where the
site has it, else its visible text) is read only for roles the pipeline wants.

Config: `url` is the search page ("https://jobs.ea.com/en_US/careers/SearchJobs").
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

from ..companies import Company
from . import custom
from .base import Job, SourceError, WantDetails, all_details
from .http import Http

MAX_PAGES = 100
ARTICLE = re.compile(r"<article\b.*?</article>", re.DOTALL | re.IGNORECASE)
LINK = re.compile(r'<a\b[^>]*href="([^"]*?/careers/JobDetail[^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
LOCATION = [
    re.compile(r'class="[^"]*\blist-item-location\b[^"]*"[^>]*>(.*?)</', re.DOTALL),  # EA
    re.compile(r'class="[^"]*\blist--location\b[^"]*"[^>]*>(.*?)</', re.DOTALL),  # TenneT
    re.compile(r'alt="[^"]*Location[^"]*"[^>]*>\s*<p[^>]*>(.*?)</p>', re.DOTALL | re.IGNORECASE),  # Macquarie
]
TOTAL = re.compile(r"([\d.,]+)\s+(?:results|jobs|Jobs|Results)\b")
STEP = re.compile(r"jobRecordsPerPage=(\d+)")


def text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def page_url(base: str, offset: int) -> str:
    return f"{base}{'&' if '?' in base else '?'}jobOffset={offset}"


def job_id(url: str) -> str:
    query = parse_qs(urlsplit(url).query)
    if query.get("jobId"):
        return query["jobId"][0]
    match = re.search(r"/JobDetail/(?:[^/]+/)?(\d+)", url)
    return match.group(1) if match else url


def parse_page(markup: str) -> list[tuple[str, str, list[str]]]:
    """(job URL, title, locations) for each job article on a search page."""
    found: dict[str, tuple[str, str, list[str]]] = {}
    for article in ARTICLE.findall(markup) or [markup]:
        for href, label in LINK.findall(article):
            url = html.unescape(href)
            if "/careers/JobDetail" not in urlsplit(url).path:
                continue  # share links carry a job's URL in their query
            title = text(label)
            if not title or url in found:
                continue
            places = []
            for pattern in LOCATION:
                places = [p for p in (text(m) for m in pattern.findall(article)) if p]
                if places:
                    break
            found[url] = (url, title, list(dict.fromkeys(places))[:5])  # some sites list them twice
            break  # one job per article
    return list(found.values())


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    base = (company.url or "").split("#")[0]
    if not base:
        raise SourceError(f"{company.name}: an Avature board needs url (its SearchJobs page)")
    jobs: dict[str, Job] = {}
    offset, total = 0, None
    for _ in range(MAX_PAGES):
        url = page_url(base, offset)
        if not http.allowed_by_robots(url):
            raise SourceError(f"robots.txt disallows {url}")
        markup = custom.text_of(http.get(url))
        if total is None:
            match = TOTAL.search(markup)
            total = int(re.sub(r"[.,]", "", match.group(1))) if match else None
            step_match = STEP.search(markup)
        rows = parse_page(markup)
        new = [r for r in rows if job_id(r[0]) not in jobs]
        for link, title, places in new:
            jobs[job_id(link)] = Job(company=company.name, title=title, url=link, source="avature",
                                     source_id=job_id(link), locations=places)
        step = int(step_match.group(1)) if step_match else len(rows)
        offset += max(step, 1)
        if not new or (total is not None and offset >= total):
            break
    for job in jobs.values():
        if details(job) and http.allowed_by_robots(job.url):
            title, places = job.title, list(job.locations)
            try:
                custom.fill_from_page(job, custom.text_of(http.get(job.url)))
            except SourceError:
                pass  # the listing's data stays; the text is filled in on a later run
            job.title = title  # the listing's title is clean; a page's <title> adds ids and the site name
            if places and not job.locations:
                job.locations = places
    if total and not jobs:
        raise SourceError(f"{company.name}: the search says {total} jobs but none could be read")
    return list(jobs.values())
