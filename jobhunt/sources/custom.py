"""Custom career sites: a listing page (or an XML sitemap) plus a regex for job links.

Each job page is read for a schema.org JobPosting in <script type="application/ld+json">,
falling back to the page title and visible text. robots.txt is respected for every page.
Every matching link is returned, so the listing stays complete; the per-run cap applies only
to detail pages fetched. Sites that only render with JavaScript are marked manual for now
(Playwright is optional, later).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, iso_date, unique, with_country
from .http import Http

DEFAULT_MAX_LINKS = 50


class PageParser(HTMLParser):
    """Collects links (with their text), JSON-LD blocks and the page title in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.json_ld: list[str] = []
        self.title = ""
        self.base: str | None = None  # <base href>, which relative links resolve against
        self.scripts: list[str] = []  # src of every <script>, for board detection
        self._href: str | None = None
        self._text: list[str] = []
        self._capture: str | None = None  # "ld" or "title"
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._href, self._text = a["href"], []
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            if a.get("type", "").lower() == "application/ld+json":
                self._capture, self._buffer = "ld", []
        elif tag == "title" and not self.title:
            self._capture, self._buffer = "title", []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
        elif tag in ("script", "title") and self._capture:
            text = "".join(self._buffer)
            if self._capture == "ld":
                self.json_ld.append(text)
            else:
                self.title = " ".join(text.split())
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)
        elif self._href is not None:
            self._text.append(data)


def parse_page(markup: str) -> PageParser:
    parser = PageParser()
    parser.feed(markup)
    parser.close()
    return parser


GENERIC_LINK_TEXT = re.compile(r"\s*(?:see|view|read|learn|find out)\b.*\b(?:more|description|details|role|job|position)\s*$"
                               r"|\s*(?:apply(?: now)?|details|more|where we're hiring)\s*$", re.IGNORECASE)


def job_links(page: PageParser, base_url: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Absolute job URLs matching the pattern, in page order, each once, with the best link text."""
    found: dict[str, str] = {}
    base_url = urljoin(base_url, page.base) if page.base else base_url
    for href, text in page.links:
        url = urldefrag(urljoin(base_url, href)).url
        if pattern.search(url):
            if GENERIC_LINK_TEXT.match(text):
                text = ""  # "See full role description" names no role
            if len(text) > len(found.get(url, "")):
                found[url] = text
            else:
                found.setdefault(url, text)
    return list(found.items())


# An XML sitemap: <urlset> or <sitemapindex>, after any XML declaration, stylesheet instructions
# and comments (Drupal's simple_sitemap writes both; seen at Societe Generale, 2026-10-29).
SITEMAP_START = re.compile(r"\s*(?:<\?[^>]*\?>\s*|<!--.*?-->\s*)*<(?:urlset|sitemapindex)\b", re.DOTALL)


def is_sitemap(body: str) -> bool:
    return bool(SITEMAP_START.match(body))


MAX_CHILD_SITEMAPS = 20


def index_links(xml: str, pattern: re.Pattern[str], http: Http) -> list[tuple[str, str]]:
    """Job links from the sitemaps a sitemap index lists (Allianz splits its jobs over four, 2026-10-29),
    each fetched where robots.txt allows it."""
    links: list[tuple[str, str]] = []
    children = [html.unescape(u.strip()) for u in re.findall(r"<loc>(.*?)</loc>", xml, re.DOTALL)][:MAX_CHILD_SITEMAPS]
    for child in children:
        if not http.allowed_by_robots(child):
            continue
        body = text_of(http.get(child))
        if is_sitemap(body):
            links += sitemap_links(body, pattern)
    return list(dict.fromkeys(links))


def sitemap_links(xml: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Job URLs from an XML sitemap, titled from their last path segment until the page is read."""
    urls = (html.unescape(u.strip()) for u in re.findall(r"<loc>(.*?)</loc>", xml, re.DOTALL))
    return [(u, title_from_url(u)) for u in dict.fromkeys(urls) if pattern.search(u)]


def text_of(response) -> str:
    """The body as text. A page or sitemap served without a charset is read as UTF-8 (with or
    without a byte-order mark), not as Latin-1, which mangles names like "București"."""
    content_type = response.headers.get("Content-Type", "")
    if "charset" in content_type.lower():
        return response.text
    try:
        return response.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return response.text


def location_from_url(url: str, pattern: re.Pattern[str]) -> str | None:
    """"/en/job/makati-city/..." with pattern r"/job/(?P<location>[^/]+)/" gives "Makati City"."""
    match = pattern.search(url)
    if not match or not match.groupdict().get("location"):
        return None
    words = [w for w in re.split(r"[-_+]+", unquote(match.group("location"))) if w]
    return " ".join(w.capitalize() if w.islower() else w for w in words) or None


def title_from_url(url: str) -> str:
    """"/en/job/amsterdam/risk-model-expert/3121/372390" gives "Risk Model Expert": the last path
    segment with letters in it (numeric ids are skipped), its words capitalised."""
    segments = [s for s in urlsplit(url).path.split("/") if re.search(r"[A-Za-z]", s)]
    segment = segments[-1] if segments else ""
    words = [w for w in re.split(r"[-_+]+", segment) if w and not w.isdigit()]  # drop numeric ids
    return " ".join(w.capitalize() if w.islower() else w for w in words)


def source_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def fetch(company: Company, http: Http, details: WantDetails = all_details,
          max_links: int = DEFAULT_MAX_LINKS) -> list[Job]:
    if not company.listing_url or not company.link_pattern:
        raise SourceError(f"{company.name}: custom board needs listing_url and link_pattern")
    if company.render:
        return fetch_rendered(company, http, details, max_links)
    if not http.allowed_by_robots(company.listing_url):
        raise SourceError(f"robots.txt disallows {company.listing_url}")
    body = text_of(http.get(company.listing_url))
    pattern = re.compile(company.link_pattern)
    if is_sitemap(body):
        links = sitemap_links(body, pattern)
        if not links and re.search(r"<sitemapindex\b", body[:2000], re.IGNORECASE):
            links = index_links(body, pattern, http)
    else:
        links = job_links(parse_page(body), company.listing_url, pattern)
    return jobs_from_links(company, links, details, max_links,
                           lambda url: http.allowed_by_robots(url), lambda url: text_of(http.get(url)))


def fetch_rendered(company: Company, http: Http, details: WantDetails, max_links: int) -> list[Job]:
    """A JavaScript-only site (`render = true`): its listing pages and the job pages wanted are
    rendered in a headless browser (render.py), with the same robots.txt check and host delay."""
    from .render import Renderer

    pattern = re.compile(company.link_pattern or "")
    pages = range(1, (company.render_pages or 1) + 1) if "{page}" in (company.listing_url or "") else [None]
    with Renderer(http, company.render_wait) as renderer:
        links: list[tuple[str, str]] = []
        for number in pages:
            url = company.listing_url.replace("{page}", str(number)) if number else company.listing_url
            final, markup = renderer.html(url)
            found = job_links(parse_page(markup), final, pattern)
            if not found and number and number > 1:
                break  # past the last page
            links += found
        return jobs_from_links(company, links, details, max_links, lambda url: True, lambda url: renderer.html(url)[1])


def jobs_from_links(company: Company, links: list[tuple[str, str]], details: WantDetails, max_links: int,
                    allowed, read) -> list[Job]:
    """Jobs from the listing's links; the pages of those the `details` hook wants are read (at most
    `max_links` a run)."""
    by_url: dict[str, str] = {}
    for url, text in links:  # one job per URL, titled by the first link text that is not empty
        if not by_url.get(url):
            by_url[url] = text
    links = list(by_url.items())
    jobs = []
    budget = max_links
    where = re.compile(company.location_pattern) if company.location_pattern else None
    for url, text in links:
        # The place in the URL ("/job/amsterdam/...") lets the pre-filter drop roles outside the
        # allowed countries before their page is fetched; the page's own location replaces it.
        place = location_from_url(url, where) if where else None
        job = Job(company=company.name, title=text or title_from_url(url) or url, url=url, source="custom",
                  source_id=source_id(url), locations=[place] if place else [])
        if budget > 0 and details(job) and allowed(url):
            budget -= 1
            try:
                if not fill_from_page(job, read(url)) and company.postings_only:
                    continue  # not a job posting (the site lists other pages too)
            except SourceError:
                pass  # keep the link; the page is retried on a later run
        jobs.append(job)
    return jobs


# A careers site's name after the role in a page <title>: "Quality Engineer - Jobs - Careers at Apple",
# "Data Engineer | Careers", "Analyst – Jobs at Acme", "Account Strategist — Google Careers".
SITE_SUFFIX = re.compile(r"\s+[-|–—]\s+(?:[\w&.'’]+\s){0,3}(?:jobs?|careers?|vacatures?|stellenangebote?)\b[^-|–—]*(?:\s+[-|–—]\s.*)?$",
                         re.IGNORECASE)


def page_title(title: str) -> str:
    """A page <title> without the careers site's name after the role."""
    return SITE_SUFFIX.sub("", title).strip() or title


def fill_from_page(job: Job, markup: str) -> bool:
    """Fill the job from its page. False when the page has no schema.org JobPosting data: then its
    title is the page <title> without the site's name (2026-10-31)."""
    page = parse_page(markup)
    posting = next(job_postings(page.json_ld), None)
    if posting is None:
        job.title = page_title(page.title) or job.title
        job.description = html_to_text(markup)
        return False
    job.title = " ".join(str(posting.get("title") or job.title).split())
    # Some sites (Optiver) put only title, date and place in JSON-LD: use the page text then.
    job.description = html_to_text(str(posting.get("description") or "")) or html_to_text(markup)
    job.locations = posting_locations(posting) or job.locations
    job.posted = iso_date(posting.get("datePosted"))
    job.deadline = iso_date(posting.get("validThrough"))
    employment = posting.get("employmentType")
    job.employment_type = ", ".join(employment) if isinstance(employment, list) else employment
    job.department = _text(posting.get("industry")) or None
    return True


def job_postings(blocks: list[str]) -> Iterator[dict[str, Any]]:
    """Every schema.org JobPosting in the JSON-LD blocks, looking inside @graph and lists."""
    def walk(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                yield node
            yield from walk(node.get("@graph"))
    for block in blocks:
        try:
            yield from walk(json.loads(block, strict=False))
        except ValueError:
            continue


def posting_locations(posting: dict[str, Any]) -> list[str]:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    found = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            city = _text(address.get("addressLocality")) or _text(address.get("addressRegion"))
            found.append(with_country(city, _text(address.get("addressCountry"))))
        elif isinstance(address, str):
            found.append(address)
        elif isinstance(place, dict) and place.get("name"):
            found.append(str(place["name"]))  # a Place with only a name ("Shanghai")
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        found.append("Remote")
    return unique(found)


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value)
    return str(value or "")
