"""Personio, Teamtailor, JOIN, Workable and SmartRecruiters, against responses recorded live
on 2026-10-27 (trimmed to a few jobs)."""

from __future__ import annotations

import json
import re

import pytest

from jobhunt import detect, geo, harvest, pipeline
from jobhunt.companies import Company
from jobhunt.sources import SourceError, fetch, join, no_details, personio, smartrecruiters, teamtailor, workable

from .fakes import fake_http, fixture_json, fixture_text

ALLOWED = {"DE", "GB", "NL", "FR"}


def allowed(job) -> bool:
    return any(geo.allowed(loc, ALLOWED) is not False for loc in job.locations)


# --- Personio --------------------------------------------------------------------------------

PERSONIO = Company(name="Outdooractive", board="personio", slug="outdooractive")


def test_personio_feed():
    jobs = personio.parse("Outdooractive", "outdooractive", "de", fixture_text("personio_outdooractive.xml"))
    accountant, android, _ = jobs
    assert (accountant.title, accountant.source, accountant.source_id) == ("Accountant (f/m/d)", "personio", "2717447")
    assert accountant.url == "https://outdooractive.jobs.personio.de/job/2717447?language=en"
    assert accountant.locations == ["Immenstadt (Deutschland)"] and accountant.posted == "2026-07-17"
    assert accountant.department == "Finance" and accountant.board_company == "Outdooractive AG"
    assert len(accountant.description) > 1000
    assert android.locations == ["Immenstadt (Deutschland)", "Großbritannien"]
    assert all(allowed(j) for j in jobs)  # German country names are read


def test_personio_falls_back_to_com_and_refuses_what_is_not_a_feed():
    http, session = fake_http({personio.feed_url("outdooractive", "com"): fixture_text("personio_outdooractive.xml")})
    jobs = fetch(PERSONIO, http, details=no_details)
    assert len(jobs) == 3 and jobs[0].url.startswith("https://outdooractive.jobs.personio.com/")
    page, _ = fake_http({personio.feed_url("outdooractive", "de"): "<html><body>Not here</body></html>"})
    with pytest.raises(SourceError):
        fetch(PERSONIO, page, details=no_details)


def test_personio_reads_the_page_for_a_position_without_text():
    feed = fixture_text("personio_outdooractive.xml")
    start, end = feed.index("<jobDescriptions>"), feed.index("</jobDescriptions>") + len("</jobDescriptions>")
    feed = feed[:start] + feed[end:]  # the first position without its text
    url = "https://outdooractive.jobs.personio.de/job/2717447?language=en"
    page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Accountant (f/m/d)", '
            '"description": "<p>Bookkeeping and reporting.</p>"}</script>')
    http, session = fake_http({personio.feed_url("outdooractive"): feed, url: page})
    first = fetch(PERSONIO, http, details=lambda job: job.source_id == "2717447")[0]
    assert first.description == "Bookkeeping and reporting." and first.locations == ["Immenstadt (Deutschland)"]


# --- Teamtailor ------------------------------------------------------------------------------

TEAMTAILOR = Company(name="McCormack", board="teamtailor", slug="mccormack")


def test_teamtailor_feed():
    jobs = teamtailor.parse("McCormack", fixture_text("teamtailor_mccormack.rss"))
    first = jobs[0]
    assert (first.title, first.source_id, first.posted) == ("PRECONSTRUCTION ASSOCIATE", "8482619", "2026-10-01")
    assert first.url == "https://mccormack.teamtailor.com/jobs/8482619-preconstruction-associate"
    assert first.locations == ["Melbourne, Australia"] and first.department == "Preconstruction"
    assert len(first.description) > 1000 and not any(allowed(j) for j in jobs)


def test_teamtailor_respects_robots_and_ties_remote_to_the_place():
    rss = fixture_text("teamtailor_mccormack.rss").replace("<remoteStatus>none</remoteStatus>",
                                                           "<remoteStatus>fully</remoteStatus>", 1)
    http, _ = fake_http({teamtailor.feed_url("mccormack"): rss})
    assert fetch(TEAMTAILOR, http)[0].locations == ["Remote, Melbourne, Australia"]
    blocked, _ = fake_http({"https://mccormack.teamtailor.com/robots.txt": "User-agent: *\nDisallow: /jobs\n"})
    with pytest.raises(SourceError, match="robots.txt"):
        fetch(TEAMTAILOR, blocked)


# --- JOIN ------------------------------------------------------------------------------------

JOIN = Company(name="Hallo Welt", board="join", slug="hallowelt")


def join_page(items: list, page: int, pages: int) -> str:
    data = join.page_data(fixture_text("join_hallowelt.html"))
    data = {**data, "items": items, "pagination": {**data["pagination"], "page": page, "pageCount": pages}}
    body = json.dumps({"props": {"pageProps": {"pageData": {"jobs": data}}}})
    return f'<script id="__NEXT_DATA__" type="application/json">{body}</script>'


def test_join_pages_through_and_reads_each_wanted_job_page():
    items = join.page_data(fixture_text("join_hallowelt.html"))["items"]
    first_url = "https://join.com/companies/hallowelt/" + items[0]["idParam"]
    http, session = fake_http({join.page_url("hallowelt"): join_page(items[:2], 1, 2),
                               join.page_url("hallowelt", 2): join_page(items[2:], 2, 2),
                               first_url: fixture_text("join_hallowelt_job.html")})
    jobs = fetch(JOIN, http, details=lambda job: job.url == first_url)
    assert [j.source_id for j in jobs] == [str(i["id"]) for i in items]
    first = jobs[0]
    assert first.title == "Account Executive - B2B Software/SaaS (m/w/d)" and first.posted == "2026-07-07"
    assert first.locations == ["Regensburg, Germany"] and len(first.description) > 1000  # the listing's place stays
    assert jobs[1].description == "" and "https://join.com/companies/hallowelt?page=2" in session.urls()


def test_join_reads_the_older_page_layout_too():
    body = json.dumps({"props": {"pageProps": {"initialState": {"jobs": {"items": [], "pagination": {"total": 0}}}}}})
    http, _ = fake_http({join.page_url("quiet"): f'<script id="__NEXT_DATA__">{body}</script>'})
    assert fetch(Company(name="Quiet", board="join", slug="quiet"), http) == []


def test_join_says_when_a_company_page_has_no_jobs_list():
    http, _ = fake_http({join.page_url("gone"): '<script id="__NEXT_DATA__">{"props": {"pageProps": {}}}</script>'})
    with pytest.raises(SourceError, match="no job list"):
        fetch(Company(name="Gone", board="join", slug="gone"), http)


# --- Workable --------------------------------------------------------------------------------

WORKABLE = Company(name="ThinkWell", board="workable", slug="thinkwell")


def test_workable_lists_a_board_in_one_request_and_fetches_details_for_wanted_jobs():
    detail = workable.detail_url("thinkwell", "1BBCE224A3")
    http, session = fake_http({workable.listing_url("thinkwell"): fixture_json("workable_thinkwell_widget.json"),
                               detail: fixture_json("workable_thinkwell_detail.json")})
    jobs = fetch(WORKABLE, http, details=lambda job: job.source_id == "1BBCE224A3")
    assert [j.source_id for j in jobs] == ["1BBCE224A3", "C9F258D606", "433DA8164D"]
    assert session.urls() == [workable.listing_url("thinkwell"), detail]
    remote = jobs[0]
    assert remote.url == "https://apply.workable.com/thinkwell/j/1BBCE224A3/" and remote.posted == "2026-10-09"
    assert remote.locations == ["Remote, Colombia"] and not allowed(remote)  # remote in Colombia is not Europe
    assert "consultant" in remote.description and remote.board_company == "ThinkWell"
    assert jobs[1].locations == ["Kampala, Uganda"] and jobs[1].description == ""
    gone, _ = fake_http({})
    with pytest.raises(SourceError, match="404"):
        fetch(Company(name="Gone", board="workable", slug="gone"), gone)


# --- SmartRecruiters -------------------------------------------------------------------------

SMART = Company(name="Oasis Africa", board="smartrecruiters", slug="OasisAfricaConsultingLimited")


def test_smartrecruiters_pages_by_offset_and_reads_details():
    listing = fixture_json("smartrecruiters_oasisafricaconsultinglimited_postings.json")
    raws = listing["content"]
    smartrecruiters.PAGE_SIZE, size = 2, smartrecruiters.PAGE_SIZE
    try:
        routes = {smartrecruiters.listing_url(SMART.slug, 0): {**listing, "content": raws[:2], "totalFound": 3},
                  smartrecruiters.listing_url(SMART.slug, 2): {**listing, "content": raws[2:], "totalFound": 3},
                  raws[0]["ref"]: fixture_json("smartrecruiters_oasisafricaconsultinglimited_detail.json")}
        http, _ = fake_http(routes)
        jobs = fetch(SMART, http, details=lambda job: job.source_id == raws[0]["id"])
    finally:
        smartrecruiters.PAGE_SIZE = size
    assert len(jobs) == 3 and jobs[0].board_company == "Oasis Africa Consulting Limited"
    assert jobs[0].title == "Commercial Manager - Mining mineral trading (Jos)" and jobs[0].posted == "2026-10-15"
    assert jobs[0].url.startswith("https://jobs.smartrecruiters.com/OasisAfricaConsultingLimited/744000155819679")
    assert len(jobs[0].description) > 1000 and jobs[1].description == ""
    assert jobs[1].url == "https://jobs.smartrecruiters.com/OasisAfricaConsultingLimited/744000155500664"


# --- Recognising the boards ------------------------------------------------------------------

@pytest.mark.parametrize("text,board,slug", [
    ("https://acme.jobs.personio.de/job/1", "personio", "acme"),
    ("https://acme.jobs.personio.com/", "personio", "acme"),
    ("https://acme.teamtailor.com/jobs/12-x", "teamtailor", "acme"),
    ("https://join.com/companies/acme/123-dev", "join", "acme"),
    ("https://apply.workable.com/acme/j/ABC/", "workable", "acme"),
    ("https://jobs.smartrecruiters.com/Acme1/7440001", "smartrecruiters", "Acme1"),
])
def test_detect_and_harvest_recognise_the_new_boards(text, board, slug):
    found = [f for f in detect.candidates(text) if f.board == board]
    assert found and found[0].supported and found[0].config["slug"] == slug
    assert harvest.BOARDS_BY_NAME[board].supported
    assert harvest.BOARDS_BY_NAME[board].identify(text)["slug"] == slug.lower()


def test_each_new_board_is_fetched_by_one_worker():
    hosts = {pipeline.host_of(Company(name=n, board=b, slug=n)) for b in ("personio", "teamtailor") for n in ("a", "b")}
    assert hosts == {"jobs.personio.de", "teamtailor.com"}


def test_feeds_are_read_as_their_xml_says_not_as_latin_1():
    """Personio sends text/xml without a charset; read as text, "München" became "MÃ¼nchen"."""
    from .fakes import response
    feed = fixture_text("personio_outdooractive.xml").replace("Immenstadt (Deutschland)", "München", 1)
    url = personio.feed_url("outdooractive")
    http, _ = fake_http({url: response(url, 200, feed, {"Content-Type": "text/xml"})})
    jobs = fetch(PERSONIO, http, details=no_details)
    assert jobs[0].locations == ["München"]


def test_text_read_as_latin_1_is_repaired_and_real_accents_left_alone():
    from jobhunt.sources.base import repair_mojibake
    assert repair_mojibake("MÃ¼nchen") == "München"
    assert repair_mojibake("Intern â€” Data Analytics") == "Intern — Data Analytics"
    for fine in ("São Paulo", "Zürich", "JOÃO PESSOA", "Café"):
        assert repair_mojibake(fine) == fine


# --- Homerun (recorded live 2026-10-29) --------------------------------------------------------

from jobhunt.sources import homerun  # noqa: E402

HOMERUN = Company(name="Assist Digital", board="homerun", slug="assist-digital")


def test_homerun_feed():
    jobs = homerun.parse("Assist Digital", fixture_text("homerun_assist-digital.xml"))
    first = jobs[0]
    assert (first.title, first.source_id, first.posted) == ("Internships - Design & Strategy", "job_e3QRjK7uOg6y1RkbdBH9",
                                                            "2026-05-04")
    assert first.url == "https://careers-nl.assistdigital.com/internships-design-and-strategy"  # not the /apply page
    assert first.locations == ["Amsterdam"] and first.department == "Operations" and first.employment_type == "Internship"
    assert len(first.description) > 1000 and all(allowed(j) for j in jobs)


def test_homerun_fetches_the_feed_and_respects_robots():
    http, _ = fake_http({homerun.feed_url("assist-digital"): fixture_text("homerun_assist-digital.xml")})
    assert len(fetch(HOMERUN, http)) == 5
    blocked, _ = fake_http({"https://feed.homerun.co/robots.txt": "User-agent: *\nDisallow: /\n"})
    with pytest.raises(SourceError, match="robots.txt"):
        fetch(HOMERUN, blocked)


def test_homerun_is_recognised_and_harvested():
    found = detect.candidates("https://careers-nl.assistdigital.com/ <a href='https://feed.homerun.co/assist-digital'>")
    assert any(f.board == "homerun" and f.supported and f.config["slug"] == "assist-digital" for f in found)
    board = next(b for b in harvest.BOARDS if b.name == "homerun")
    assert board.supported and re.match(board.pattern, "https://varo.homerun.co/terminal-operator-4")
    assert not re.match(board.pattern, "https://help.homerun.co/en/")


# --- Amazon, Oracle Recruiting Cloud, Bending Spoons and rendered sites (2026-10-29) ----------

from jobhunt.sources import amazon, bendingspoons, custom, oracle  # noqa: E402


def test_amazon_search_by_country():
    company = Company(name="Amazon", board="amazon", extra={"countries": ["DEU", "ITA"]})
    http, _ = fake_http({amazon.search_url(["DEU", "ITA"], 0): fixture_text("amazon_search.json")})
    jobs = fetch(company, http)
    first = jobs[0]
    assert (first.title, first.source_id, first.posted) == ("Area Manager", "10592278", "2026-10-29")
    assert first.url == "https://www.amazon.jobs/en/jobs/10592278/area-manager"
    assert first.locations == ["Gernsheim, Hesse, DEU"] and allowed(first) and "Basic qualifications" in first.description
    with pytest.raises(SourceError, match="countries"):
        fetch(Company(name="Amazon", board="amazon"), http)


def test_oracle_recruiting_cloud_keeps_the_configured_countries_and_reads_wanted_details():
    host, site = "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1"
    listing = json.loads(fixture_text("oracle_nokia_jobs.json"))
    listing["items"][0]["TotalJobsCount"] = len(listing["items"][0]["requisitionList"])
    first_id = str(listing["items"][0]["requisitionList"][0]["Id"])
    http, _ = fake_http({oracle.list_url(host, site, 0): json.dumps(listing),
                         oracle.detail_url(host, site, first_id): fixture_text("oracle_nokia_detail.json")})
    company = Company(name="Nokia", board="oracle", url=host, slug=site,
                      extra={"job_url": "https://jobs.nokia.com/en/sites/CX_1", "countries": ["RO"]})
    jobs = fetch(company, http, details=lambda job: job.source_id == first_id)
    assert jobs and all("RO" in "".join(j.locations) or "Romania" in "".join(j.locations) for j in jobs)
    first = next(j for j in jobs if j.source_id == first_id)
    assert first.url == f"https://jobs.nokia.com/en/sites/CX_1/job/{first_id}" and len(first.description) > 1000
    assert first.locations == ["Romania, RO"] and geo.parse(first.locations[0]).codes == {"RO"}


def test_bending_spoons_reads_the_roles_from_the_page_data():
    jobs = bendingspoons.parse("Bending Spoons", fixture_text("bendingspoons_jobs.html"))
    assert [j.title for j in jobs] == ["Product manager", "Bookkeeper", "Experiences manager"]
    assert jobs[0].locations[:2] == ["Milan, Italy", "London, UK"] and jobs[0].url.endswith("/positions/63c548b3521886004f3daec0")
    assert len(jobs[0].description) > 1000


def test_a_rendered_site_reads_its_listing_pages_and_wanted_jobs_through_the_browser(monkeypatch):
    from jobhunt.sources import render

    pages = {"https://x.example/search?page=1": '<a href="/details/1/intern-a">Intern A</a><a href="/details/1/intern-a">See full role description</a>',
             "https://x.example/search?page=2": '<a href="/details/2/engineer-b">Engineer B</a>',
             "https://x.example/search?page=3": "<p>No results</p>",
             "https://x.example/details/1/intern-a": "<html><head><title>Intern A</title></head><body>An internship in Berlin.</body></html>"}
    rendered = []

    class FakeRenderer:
        def __init__(self, http, wait_for=None): pass
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def html(self, url):
            rendered.append(url)
            return url, pages[url]
    monkeypatch.setattr(render, "Renderer", FakeRenderer)
    company = Company(name="X", board="custom", render=True, render_pages=5, listing_url="https://x.example/search?page={page}",
                      link_pattern=r"/details/\d+/")
    http, _ = fake_http({})
    jobs = custom.fetch(company, http, details=lambda job: "Intern" in job.title)
    assert [j.title for j in jobs] == ["Intern A", "Engineer B"]  # the generic link text never titles a role
    assert "An internship in Berlin." in jobs[0].description and not jobs[1].description
    assert "https://x.example/search?page=4" not in rendered  # stopped after the first empty page


# --- Avature (recorded live 2026-10-29) ----------------------------------------------------------

from jobhunt.sources import avature  # noqa: E402


def test_avature_search_pages_give_title_link_and_place():
    tennet = avature.parse_page(fixture_text("avature_tennet_search.html"))
    assert len(tennet) == 3 and all("/careers/JobDetail" in url for url, _, _ in tennet)
    url, title, places = tennet[0]
    assert title == "Project Coordinator" and places == ["Weert"]  # listed twice on the page, kept once
    assert avature.job_id(url) == "119020"
    ea = avature.parse_page(fixture_text("avature_ea_search.html"))
    assert len(ea) == 2 and ea[0][1] == "Game Designer II" and ea[0][2] == ["Hyderabad, India"]  # not the share links
    assert avature.job_id(ea[0][0]) == "215680"


def test_avature_pages_by_offset_until_the_stated_total():
    base = "https://careers.tennet.eu/en_US/careers/SearchJobs"
    page = fixture_text("avature_tennet_search.html").replace("120 results", "5 results")
    second = page.replace("jobId=", "jobId=9")  # three other jobs on the next page
    http, session = fake_http({avature.page_url(base, 0): page, avature.page_url(base, 3): second})
    jobs = fetch(Company(name="TenneT", board="avature", url=base), http, details=no_details)
    assert len(jobs) == 6 and avature.page_url(base, 6) not in [c[1] for c in session.calls]


# --- Eightfold (recorded live 2026-10-29) -----------------------------------------------------------

from jobhunt.sources import eightfold  # noqa: E402


def test_eightfold_searches_each_country_and_reads_wanted_details():
    origin, domain = "https://apply.careers.microsoft.com", "microsoft.com"
    search = json.loads(fixture_text("eightfold_microsoft_search.json"))
    first = str(search["data"]["positions"][0]["id"])
    http, _ = fake_http({eightfold.search_url(origin, domain, "Romania", 0): json.dumps(search),
                         eightfold.detail_url(origin, domain, first): fixture_text("eightfold_microsoft_detail.json"),
                         origin + "/robots.txt": "User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /api/pcsx\n"})
    company = Company(name="Microsoft", board="eightfold", url=origin, slug=domain, extra={"countries": ["Romania"]})
    jobs = fetch(company, http, details=lambda job: job.source_id == first)
    assert len(jobs) == 3 and all(j.url.startswith(origin + "/careers/job/") for j in jobs)
    read = next(j for j in jobs if j.source_id == first)
    assert len(read.description) > 500 and read.posted and any("Romania" in p for p in read.locations)
