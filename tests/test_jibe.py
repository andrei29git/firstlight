"""Jibe career sites (Booking.com), and custom sites that put the location in the job URL."""

from __future__ import annotations

import argparse

from jobhunt import cli, detect, pipeline, wishlist
from jobhunt import config as cfg
from jobhunt.companies import Company
from jobhunt.sources import custom, fetch, jibe

from .conftest import EXAMPLE_CONFIG
from .fakes import fake_http, fixture_json

CAREERS = "https://jobs.booking.com/booking/jobs"
BOOKING = Company(name="Booking.com", board="jibe", url=CAREERS)


def test_parse_a_recorded_response():
    jobs = jibe.parse("Booking.com", CAREERS, fixture_json("jibe_booking.json"))
    first, second = jobs
    assert (first.title, first.source, first.source_id) == ("Risk and Compliance Officer", "jibe", "30539")
    assert first.locations == ["Amsterdam, Netherlands"] and first.posted == "2026-10-26"
    assert first.url == f"{CAREERS}/30539?lang=en-us" and len(first.description) > 200
    assert "Manchester, United Kingdom" in second.locations and "London, United Kingdom" in second.locations


def test_fetch_pages_through_and_respects_robots():
    page = fixture_json("jibe_booking.json")
    one, two = ({**page, "jobs": page["jobs"][:1], "totalCount": 2}, {**page, "jobs": page["jobs"][1:], "totalCount": 2})
    http, session = fake_http({jibe.api_url(CAREERS, 1): one, jibe.api_url(CAREERS, 2): two,
                               "https://jobs.booking.com/robots.txt": "User-agent: *\nAllow: /\ncrawl-delay: 5\n"})
    assert len(fetch(BOOKING, http)) == 2 and max(http.sleeps) >= 4.9  # crawl-delay honoured between pages
    blocked, _ = fake_http({"https://jobs.booking.com/robots.txt": "User-agent: *\nDisallow: /api/\n"})
    try:
        fetch(BOOKING, blocked)
        raise AssertionError("expected a refusal")
    except Exception as e:  # noqa: BLE001
        assert "robots.txt disallows" in str(e)


def test_a_jibe_board_can_be_searched_by_location():
    # 2026-11-18: Keysight lists about 800 jobs worldwide; one search per place, each job once.
    page = fixture_json("jibe_booking.json")
    both = {**page, "totalCount": 2}
    http, session = fake_http({jibe.api_url(CAREERS, 1, "Romania"): both, jibe.api_url(CAREERS, 1, "Netherlands"): both,
                               "https://jobs.booking.com/robots.txt": ""})
    company = Company.from_dict({"name": "Keysight", "board": "jibe", "url": CAREERS, "locations": ["Romania", "Netherlands"]})
    assert len(fetch(company, http)) == 2  # the same two jobs from both searches
    assert sum("location=" in u for u in session.urls()) == 2 and jibe.api_url(CAREERS, 1, "Romania").endswith("location=Romania")


def test_detect_recognises_a_jibe_site():
    http, _ = fake_http({"https://jobs.booking.com/": "<script>window._jibe = {}</script>",
                         jibe.api_url("https://jobs.booking.com/", 1): fixture_json("jibe_booking.json")})
    findings, _ = detect.detect("https://jobs.booking.com/", http)
    found = next(f for f in findings if f.board == "jibe")
    assert found.jobs == 2 and found.config == {"board": "jibe", "url": "https://jobs.booking.com/"}


def test_location_from_the_job_url_filters_before_the_page_is_fetched():
    site = "https://careers.example.com"
    links = [f"{site}/en/job/{city}/software-engineer-graduate/3121/{i}" for i, city in
             enumerate(["amsterdam", "makati-city", "singapore", "bucharest"])]
    sitemap = "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in links) + "</urlset>"
    company = Company(name="Example Bank", board="custom", listing_url=site + "/sitemap.xml",
                      link_pattern=r"/en/job/", location_pattern=r"/en/job/(?P<location>[^/]+)/")
    http, session = fake_http({site + "/sitemap.xml": sitemap, **{u: "<html><title>Role</title></html>" for u in links}})
    allowed = set(cfg.load_settings(EXAMPLE_CONFIG)["locations"]["allowed_countries"])
    jobs = custom.fetch(company, http, details=lambda job: pipeline.prefilter.check(job.title, job.locations,
                                                                                    allowed).passed)
    assert [j.locations for j in jobs] == [["Amsterdam"], ["Makati City"], ["Singapore"], ["Bucharest"]]
    fetched = [u for u in session.urls() if "/en/job/" in u]
    assert fetched == [links[0], links[3]]  # Makati City and Singapore never fetched


def test_a_wishlist_company_can_be_fetched_from_the_laptop_only(tmp_path):
    state = tmp_path / "state"
    wishlist.set_company(state, "Booking.com", True, {"board": "jibe", "url": CAREERS, "fetch_from": "local"})
    paths = cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "db", backups=tmp_path / "b")
    settings = cfg.load_settings(EXAMPLE_CONFIG)

    def names(local_only):
        args = argparse.Namespace(no_fetch=False, sonnet_only=False, names=[], local_only=local_only, generated="none")
        return {c.name for c in cli.run_targets(args, paths, settings, state, say=lambda _: None)}
    assert "Booking.com" not in names(False) and "Booking.com" in names(True)


def test_status_points_out_sites_that_fail_only_on_github():
    runs = [{"ran_on": "laptop", "sources": {"ING": {"board": "custom", "fetched": 3}}},
            *[{"ran_on": "github", "sources": {"ING": {"board": "custom", "error": "HTTP 403 from ..."},
                                               "Mistral": {"board": "ashby", "fetched": 9}}} for _ in range(3)]]
    found = pipeline.failing_on_github(runs)
    assert found == {"ING": {"failed": 3, "runs": 3, "error": "HTTP 403 from ...", "current": False, "works_on_laptop": True,
                     "laptop": "ok"}}
