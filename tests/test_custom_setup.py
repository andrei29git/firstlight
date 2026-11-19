"""Setting a careers site up as a custom board: from the page, its sitemaps or its job-search
pages, with robots.txt and crawl-delay respected, and a clear reason when it cannot be done."""

from __future__ import annotations

import json

import requests

from jobhunt import detect, wishlist
from jobhunt.sources import custom

from .fakes import fake_http
from .test_jobs_web import client  # noqa: F401 (fixture)

SITE = "https://careers.example.com"


def raw(url: str, body: bytes, content_type: str = "text/xml") -> requests.Response:
    """A response as the server sends it: bytes, with or without a charset."""
    r = requests.Response()
    r.status_code, r.url, r._content = 200, url, body
    r.headers["Content-Type"] = content_type
    r.encoding = "ISO-8859-1"  # what requests assumes for text/* without a charset
    return r


def sitemap(*paths: str, bom: bool = True) -> bytes:
    urls = "".join(f"<url><loc>{SITE}{p}</loc></url>" for p in paths)
    xml = f'<?xml version="1.0" encoding="utf-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return (b"\xef\xbb\xbf" if bom else b"") + xml.encode("utf-8")


JOBS = ["/en/job/amsterdam/graduate-software-engineer/3121/101", "/en/job/bucuresti/data-engineer/3121/102",
        "/en/job/warsaw/backend-developer/3121/103", "/en/job/madrid/it-risk-officer/3121/104"]


def test_titles_from_urls_skip_numeric_ids():
    assert custom.title_from_url(SITE + JOBS[0]) == "Graduate Software Engineer"


def test_pages_without_a_charset_are_read_as_utf8():
    response = raw(SITE + "/x", "﻿<p>București</p>".encode("utf-8"), "text/html")
    assert custom.text_of(response) == "<p>București</p>"


def test_a_sitemap_is_found_and_preferred_to_a_partial_search_page():
    page = f'<html><a href="/en/search-jobs">Search jobs</a><a href="/en/about">About us</a></html>'
    search = "<html>" + "".join(f'<a href="{p}">job</a>' for p in JOBS[:3]) + "</html>"
    http, session = fake_http({
        SITE + "/en": page,
        SITE + "/robots.txt": f"User-agent: *\nDisallow: /search-jobs/\nSitemap: {SITE}/jobs-sitemap.xml\n",
        SITE + "/jobs-sitemap.xml": raw(SITE + "/jobs-sitemap.xml", sitemap(*JOBS, "/en/category/it")),
        SITE + "/en/search-jobs": search,
    })
    findings, _ = detect.detect(SITE + "/en", http)
    found = findings[-1]
    assert found.config == {"board": "custom", "listing_url": SITE + "/jobs-sitemap.xml",
                            "link_pattern": r"^https?://careers\.example\.com/en/job/",
                            # the city after /en/job/ is read as the location
                            "location_pattern": r"^https?://careers\.example\.com/en/job/(?P<location>[^/]+)/"}
    assert found.jobs == 4 and found.samples[0] == ("Graduate Software Engineer", SITE + JOBS[0])


def test_a_job_search_page_is_used_when_there_is_no_sitemap_and_marked_partial():
    page = '<html><a href="/vacancies/">View all vacancies</a></html>'
    search = "<html>" + "".join(f'<a href="/vacancies/role-{i}">Role {i}</a>' for i in range(4)) + "</html>"
    http, _ = fake_http({SITE + "/": page, SITE + "/vacancies/": search})
    found = detect.detect(SITE + "/", http)[0][-1]
    assert found.jobs == 4 and found.config["listing_url"] == SITE + "/vacancies/" and found.config["partial"]


def test_when_nothing_works_the_reason_is_given():
    http, _ = fake_http({SITE + "/": "<html><p>Join us!</p></html>", SITE + "/sitemap.xml": 406})
    found = detect.detect(SITE + "/", http)[0][-1]
    assert not found.supported and "refuses automated requests" in found.error


def test_robots_txt_is_respected_and_its_crawl_delay_honoured():
    http, session = fake_http({
        SITE + "/robots.txt": "User-agent: *\nDisallow: /jobs-sitemap.xml\nCrawl-delay: 5\n",
        SITE + "/": "<html><p>Careers</p></html>",
        SITE + "/jobs-sitemap.xml": raw(SITE + "/jobs-sitemap.xml", sitemap(*JOBS)),
    })
    reasons: list[str] = []
    assert detect._get_text(SITE + "/jobs-sitemap.xml", http, reasons) is None
    assert "robots.txt disallows" in reasons[0] and SITE + "/jobs-sitemap.xml" not in session.urls()
    http.get(SITE + "/")
    http.get(SITE + "/")
    assert http.sleeps and max(http.sleeps) >= 4.9  # 5 seconds between requests, as robots.txt asks


def test_the_add_sheet_offers_the_careers_site_with_sample_jobs(client, paths, monkeypatch):  # noqa: F811
    found = detect.Finding(board="custom", supported=True, jobs=4, samples=[("Graduate Software Engineer", SITE + JOBS[0])],
                           config={"board": "custom", "listing_url": SITE + "/sitemap.xml",
                                   "link_pattern": r"^https?://careers\.example\.com/en/job/"})
    monkeypatch.setattr(detect, "detect", lambda url, http, name=None: ([found], url))
    html = client.post("/wishlist/find", data={"name": "Example Bank", "url": SITE + "/en"}).text
    assert "its own careers site, read from its sitemap: 4 jobs" in html and "Graduate Software Engineer" in html
    import html as htmllib
    choice = htmllib.unescape(html.split('name="choice" value=\'')[1].split("'")[0])
    client.post("/wishlist/set", data={"on": "1", "choice": choice})
    entry = wishlist.load(paths.state_dir)["example bank"]
    assert entry["identity"] == f"custom:{SITE}/sitemap.xml"
    company = wishlist.companies(paths.state_dir, set())[0]
    assert (company.board, company.listing_url, company.origin) == ("custom", SITE + "/sitemap.xml", "wishlist")


def test_the_add_sheet_says_why_a_site_could_not_be_set_up(client, monkeypatch):  # noqa: F811
    nothing = detect.Finding(board="custom", config={}, supported=False,
                             error="https://x/sitemap.xml: HTTP 406 (the site refuses automated requests; "
                                   "the tool does not work around that)")
    monkeypatch.setattr(detect, "detect", lambda url, http, name=None: ([nothing], url))
    html = client.post("/wishlist/find", data={"name": "Fortress", "url": "https://x/"}).text
    assert "could not be set up" in html and "refuses automated requests" in json.dumps(html)
