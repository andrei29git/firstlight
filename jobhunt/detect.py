"""`detect <careers-url>`: recognise the job board behind a careers page.

Looks at the final URL after redirects, links, script sources and inline text for known board
hosts, checks every candidate against the live API (a guessed slug is worthless until a board
answers with jobs), and prints a ready-to-paste companies.toml block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from . import geo
from .companies import Company
from .sources import SourceError, custom, fetch, no_details
from .sources.http import Http

# (board, regex). Named groups: slug, url, eu.
SUPPORTED: list[tuple[str, str]] = [
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/(?P<slug>[A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?(?:[^\"'\s]*&)?for=(?P<slug>[A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?P<slug>[A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?P<eu>eu\.)?lever\.co/(?P<slug>[A-Za-z0-9_.-]+)"),
    ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/(?P<slug>[A-Za-z0-9_.%-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9_.%-]+)"),
    ("workday", r"(?P<url>https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+)"),
    ("recruitee", r"(?P<slug>[a-z0-9-]+)\.recruitee\.com"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"api\.smartrecruiters\.com/v1/companies/(?P<slug>[A-Za-z0-9_-]+)"),
    ("personio", r"(?P<slug>[a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("workable", r"apply\.workable\.com/(?:api/v\d/accounts/)?(?P<slug>[a-z0-9_-]+)"),
    ("teamtailor", r"(?P<slug>[a-z0-9-]+)\.teamtailor\.com"),
    ("join", r"join\.com/companies/(?P<slug>[a-z0-9_-]+)"),
    ("homerun", r"feed\.homerun\.co/(?P<slug>[a-z0-9-]+)"),
    ("homerun", r"(?P<slug>[a-z0-9-]+)\.homerun\.co"),
]
# Recognised but without an adapter yet: reported so the decision to build one is informed.
UNSUPPORTED: list[tuple[str, str]] = [
    ("successfactors", r"successfactors\.(?:com|eu)|jobs\.sap\.com"),
    ("phenom", r"phenompeople\.com|cdn\.phenom"),
    ("avature", r"(?P<slug>[a-z0-9-]+)\.avature\.net"),
    ("eightfold", r"eightfold\.ai"),
    ("oracle", r"oraclecloud\.com/hcmUI/CandidateExperience"),
    ("icims", r"(?P<slug>[a-z0-9-]+)\.icims\.com"),
    ("taleo", r"taleo\.net"),
    ("yello", r"yello\.co\b"),
]
# Path segments that are part of a board's own site, never a company slug.
NOT_SLUGS = {"embed", "career", "support", "help", "blog", "status", "api-docs", "v1", "api", "www", "app", "static", "assets", "cdn", "careers", "jobs", "js", "css",
             "images", "favicon.ico", "robots.txt", "privacy", "terms"}
JOB_LINK = re.compile(r"/(?:jobs?|vacanc(?:y|ies)|positions?|openings?|roles?|vacatures?|stellen(?:angebote)?)"
                      r"/[^/?#]*[A-Za-z0-9]", re.IGNORECASE)


@dataclass
class Finding:
    board: str
    config: dict[str, Any]
    supported: bool
    jobs: int | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    sample: str | None = None  # one job URL, to confirm the board is the right company's
    samples: list[tuple[str, str]] = field(default_factory=list)  # (title, url) of a few jobs found

    @property
    def works(self) -> bool:
        return self.supported and self.jobs is not None


def candidates(text: str) -> list[Finding]:
    """Every board reference in the text, each once, supported boards first."""
    found: dict[tuple, Finding] = {}
    for supported, table in ((True, SUPPORTED), (False, UNSUPPORTED)):
        for board, pattern in table:
            for match in re.finditer(pattern, text):
                config = _config(board, match.groupdict())
                if config is None:
                    continue
                key = (board, tuple(sorted(config.items())))
                found.setdefault(key, Finding(board=board, config=config, supported=supported))
    return list(found.values())


def _config(board: str, groups: dict[str, str | None]) -> dict[str, Any] | None:
    config: dict[str, Any] = {"board": board}
    if groups.get("url"):
        config["url"] = groups["url"]
    if groups.get("slug") is not None:
        slug = unquote(groups["slug"]).rstrip(".")
        if not slug or slug.lower() in NOT_SLUGS:
            return None
        config["slug"] = slug
    if groups.get("eu"):
        config["region"] = "eu"
    return config


def suggest_custom(markup: str, url: str) -> Finding | None:
    """For a page with no known board: propose a custom config from links that look like jobs."""
    page = custom.parse_page(markup)
    links = [link for link, _ in custom.job_links(page, url, JOB_LINK)]
    has_postings = any(True for _ in custom.job_postings(page.json_ld))
    if not links and not has_postings:
        return None
    finding = Finding(board="custom", config={"board": "custom", "listing_url": url}, supported=True)
    if links:
        finding.config["link_pattern"] = _link_pattern(links)
        _add_location_pattern(finding, links)
        finding.notes.append(f"{len(links)} job-like links, e.g. {links[0]}")
    if has_postings:
        finding.notes.append("the page itself has schema.org JobPosting data")
    return finding


def _link_pattern(links: list[str]) -> str:
    """A regex for the shared host and leading path of the job links: a starting point to check."""
    parts = [urlsplit(link) for link in links]
    host = parts[0].netloc
    paths = [p.path for p in parts if p.netloc == host]
    prefix = paths[0]
    for path in paths[1:]:
        while not path.startswith(prefix):
            prefix = prefix[:-1]
    prefix = prefix[: prefix.rfind("/") + 1] or "/"
    return "^https?://" + re.escape(host) + re.escape(prefix)


def _add_location_pattern(finding: Finding, links: list[str]) -> None:
    """When most job URLs carry a place right after the shared prefix ("/en/job/amsterdam/..."),
    read it as the role's location, so roles in other countries are dropped before their page
    is fetched."""
    prefix = finding.config["link_pattern"]
    match = re.compile(prefix)
    places = []
    for link in links:
        found = match.search(link)
        rest = link[found.end():].split("/") if found else []
        if len(rest) >= 2:  # a place segment, then more of the path
            places.append(geo.parse(unquote(rest[0]).replace("-", " ")).recognised)
    if len(places) >= MIN_JOB_LINKS and sum(places) / len(places) >= 0.6:
        finding.config["location_pattern"] = prefix + r"(?P<location>[^/]+)/"
        finding.notes.append("locations are read from the job links")


def slug_guesses(name: str) -> list[str]:
    name = name.lower().replace("&", "and")
    return list(dict.fromkeys(s for s in (
        re.sub(r"[^a-z0-9]", "", name),  # grafanalabs
        re.sub(r"[^a-z0-9]+", "-", name).strip("-"),  # grafana-labs
        re.sub(r"[^a-z0-9]", "", name.split(".")[0]),  # booking.com -> booking
        name.strip() if "." in name and " " not in name.strip() else "",  # mistral.ai (Ashby allows dots)
    ) if s))


def probe(name: str, http: Http) -> list[Finding]:
    """Boards that answer, with jobs, for slugs guessed from the company name. A guessed
    slug is a lead, not a fact: the sample link shows whether it is the right company."""
    quick = http.without_retries()
    found = []
    for slug in slug_guesses(name):
        configs = [{"board": b, "slug": slug} for b in ("greenhouse", "lever", "ashby", "recruitee", "personio",
                                                          "teamtailor", "smartrecruiters", "workable", "join", "homerun")]
        configs.append({"board": "lever", "slug": slug, "region": "eu"})
        for config in configs:
            finding = Finding(board=config["board"], config=config, supported=True)
            check(finding, quick)
            if finding.jobs:
                finding.notes.append("found by guessing the slug from the name; check the sample link")
                found.append(finding)
    return found


def detect(url: str | None, http: Http, name: str | None = None) -> tuple[list[Finding], str | None]:
    """Findings for the careers page (and, if it shows no supported board, for slugs guessed
    from the company name), and the final URL after redirects."""
    if url is None:
        return (probe(name, http) if name else []), None
    if not http.allowed_by_robots(url):
        raise SourceError(f"robots.txt disallows {url}")
    response = http.get(url)
    final_url = response.url or url
    findings = candidates(final_url + "\n" + response.text)
    if re.search(r"\b_?jibe\b", response.text, re.IGNORECASE) and not any(f.board == "jibe" for f in findings):
        findings.append(Finding(board="jibe", config={"board": "jibe", "url": final_url}, supported=True,
                                notes=["a Jibe career site (its own /api/jobs)"]))
    for finding in findings:
        if finding.supported:
            check(finding, http)
    if not any(f.works for f in findings) and name:
        findings += probe(name, http)
    if not any(f.works for f in findings):
        findings.append(discover_custom(response.text, final_url, http))
    return findings, final_url


# Where a careers site keeps its jobs when the page itself has no job links.
LISTING_HINT = re.compile(r"(?:search|all|open|view|browse|find)[\s-]*(?:jobs|roles|positions|vacancies|openings)|"
                          r"/(?:jobs|vacancies|vacatures|positions|openings|search-jobs|job-search|careers/list)/?(?:$|\?)",
                          re.IGNORECASE)
MIN_JOB_LINKS = 3


def discover_custom(markup: str, url: str, http: Http) -> Finding:
    """Set a careers site up as a custom board: job links on the page itself, else the site's
    sitemaps (from robots.txt, then /sitemap.xml), else the site's own job-search pages. Each
    candidate is tried with the custom adapter (robots.txt respected); the first that finds jobs
    wins. When none does, the Finding says why (supported = False, error)."""
    reasons: list[str] = []
    candidates = []
    page = suggest_custom(markup, url)
    if page:
        candidates.append(page)
    candidates += sitemap_candidates(url, http, reasons)
    candidates += listing_page_candidates(markup, url, http, reasons)
    tried, working = set(), []
    for finding in candidates:
        key = (finding.config.get("listing_url"), finding.config.get("link_pattern"))
        if key in tried:
            continue
        tried.add(key)
        check(finding, http)
        if finding.jobs:
            working.append(finding)
        elif finding.error:
            reasons.append(f"{finding.config.get('listing_url')}: {finding.error}")
    if working:  # the most complete source: a sitemap lists every job, a search page often only page 1
        return max(working, key=lambda f: f.jobs or 0)
    if not candidates:
        reasons.insert(0, "no job links on the page, in the site's sitemaps or on its job-search pages")
    return Finding(board="custom", config={}, supported=False,
                   error="; ".join(dict.fromkeys(reasons)) or "the job links found led to no jobs")


def sitemap_candidates(url: str, http: Http, reasons: list[str]) -> list[Finding]:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    sitemaps: list[str] = []
    try:
        robots = http.get(origin + "/robots.txt").text
        sitemaps += [line.split(":", 1)[1].strip() for line in robots.splitlines()
                     if line.lower().startswith("sitemap:")]
    except SourceError:
        pass
    sitemaps.append(origin + "/sitemap.xml")
    found = []
    for sitemap in list(dict.fromkeys(sitemaps))[:4]:
        xml = _get_text(sitemap, http, reasons)
        if xml is None:
            continue
        if "<sitemapindex" in xml[:500]:  # an index: follow the parts that look like jobs (or all, if few)
            parts = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
            jobby = [p for p in parts if re.search(r"job|career|vacanc|position", p, re.IGNORECASE)]
            for part in (jobby or parts)[:3]:
                found += _sitemap_finding(part, _get_text(part, http, reasons))
            continue
        found += _sitemap_finding(sitemap, xml)
    return found


def _sitemap_finding(sitemap: str, xml: str | None) -> list[Finding]:
    if not xml:
        return []
    links = [u for u, _ in custom.sitemap_links(xml, JOB_LINK)]
    if len(links) < MIN_JOB_LINKS:
        return []
    finding = Finding(board="custom", config={"board": "custom", "listing_url": sitemap,
                                              "link_pattern": _link_pattern(links)}, supported=True)
    finding.notes.append(f"from the sitemap {sitemap}: {len(links)} job pages")
    _add_location_pattern(finding, links)
    return [finding]


def listing_page_candidates(markup: str, url: str, http: Http, reasons: list[str]) -> list[Finding]:
    """Links on the careers page that lead to its own job search ("Search jobs", /vacancies/...)."""
    page = custom.parse_page(markup)
    host = urlsplit(url).netloc
    base = urljoin(url, page.base) if page.base else url
    pages = []
    for link, text in page.links:
        target = urldefrag(urljoin(base, link)).url
        if urlsplit(target).netloc == host and target != url and (
                LISTING_HINT.search(text or "") or LISTING_HINT.search(urlsplit(target).path)):
            pages.append(target)
    found = []
    for target in list(dict.fromkeys(pages))[:3]:
        text = _get_text(target, http, reasons)
        suggestion = suggest_custom(text, target) if text else None
        if suggestion:
            suggestion.config["partial"] = True  # a search page may show only some jobs: none closed by absence
            suggestion.notes.append(f"from the job-search page {target} (it may show only some jobs)")
            found.append(suggestion)
    return found


def explain(error: Exception | str) -> str:
    """A fetch error in words, saying when a site refuses automated requests on purpose."""
    message = str(error)
    if re.search(r"HTTP (403|406|429)", message):
        message += " (the site refuses automated requests; the tool does not work around that)"
    return message


def _get_text(url: str, http: Http, reasons: list[str]) -> str | None:
    if not http.allowed_by_robots(url):
        reasons.append(f"robots.txt disallows {url}")
        return None
    try:
        return custom.text_of(http.get(url))
    except SourceError as e:
        reasons.append(f"{url}: {explain(e)}")
        return None


def check(finding: Finding, http: Http) -> None:
    company = Company.from_dict({"name": "detect", **finding.config})
    try:
        jobs = fetch(company, http, details=no_details)
        finding.jobs = len(jobs)
        finding.sample = jobs[0].url if jobs else None
        finding.samples = [(job.title, job.url) for job in jobs[:5]]
    except SourceError as e:
        finding.error = str(e)


def toml_block(name: str, finding: Finding, today: date) -> str:
    lines = ["[[company]]", f"name = {_toml(name)}"]
    lines += [f"{k} = {_toml(v)}" for k, v in finding.config.items()]
    if finding.works and finding.board != "custom":
        lines.append(f"verified = {today.isoformat()}")
    return "\n".join(lines)


def _toml(value: Any) -> str:
    if isinstance(value, str):
        if "\\" in value and "'" not in value:
            return f"'{value}'"  # literal string, so regex backslashes stay readable
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)
