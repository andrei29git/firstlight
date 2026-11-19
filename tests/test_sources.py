"""Adapters against saved live responses (tests/fixtures), run through the fake session."""

from __future__ import annotations

import pytest

from jobhunt.companies import Company
from jobhunt.sources import SourceError, fetch, no_details
from jobhunt.sources import ashby, custom, greenhouse, lever, recruitee, workday
from jobhunt.sources.base import html_to_text, iso_date, with_country

from .fakes import fake_http, fixture_json, fixture_text


def company(**kw) -> Company:
    return Company.from_dict({"name": "Acme", **kw})


def test_greenhouse():
    http, _ = fake_http({greenhouse.listing_url("flowtraders"): fixture_json("greenhouse_flowtraders.json")})
    jobs = fetch(company(board="greenhouse", slug="flowtraders"), http)
    assert [j.title for j in jobs] == ["AI Engineering Manager", "Core Data Engineer", "Corporate Receptionist"]
    job = jobs[1]
    assert job.key == f"greenhouse:Acme:{job.source_id}"
    assert job.url.startswith("https://job-boards.greenhouse.io/flowtraders/jobs/")
    # Office location (with country) wins over the bare city in location.name.
    assert job.locations == ["Amsterdam, Noord-Holland, Netherlands"]
    assert job.posted == "2026-10-23"  # first_published, not updated_at
    assert job.department == "Data"
    # Entity-escaped HTML becomes plain text: no tags, no entities.
    assert job.description and "<" not in job.description and "&lt;" not in job.description
    assert "&amp;" not in job.description and "&#39;" not in job.description


def test_greenhouse_deadline_comes_from_the_board():
    data = fixture_json("greenhouse_flowtraders.json")
    data["jobs"][0]["application_deadline"] = "2026-12-01T23:59:00Z"
    assert greenhouse.parse("Acme", data)[0].deadline == "2026-12-01"


def test_lever():
    http, _ = fake_http({lever.listing_url("spotify"): fixture_json("lever_spotify.json")})
    single, multi = fetch(company(board="lever", slug="spotify"), http)
    assert single.locations == ["New York, NY, US"]  # country attached to a single location
    assert multi.locations == ["New York, NY", "San Francisco, CA"]  # not guessed for several
    assert single.url.startswith("https://jobs.lever.co/spotify/")
    assert single.posted == iso_date(fixture_json("lever_spotify.json")[0]["createdAt"])
    assert single.employment_type == "Permanent"
    # The plain description plus each list section ("What You'll Do") and the closing text.
    raw = fixture_json("lever_spotify.json")[0]
    assert raw["lists"][0]["text"] in single.description
    assert raw["additionalPlain"].strip()[:40] in single.description


def test_lever_eu_region():
    assert lever.listing_url("acme", "eu").startswith("https://api.eu.lever.co/v0/postings/acme")


def test_ashby():
    http, _ = fake_http({ashby.listing_url("mistral.ai"): fixture_json("ashby_mistral.json")})
    jobs = fetch(company(board="ashby", slug="mistral.ai"), http)
    assert jobs[0].locations == ["Palo Alto, United States", "New York, United States", "San Francisco, United States"]
    assert jobs[0].posted == "2026-07-10"
    assert jobs[0].employment_type == "FullTime"
    assert jobs[0].url.startswith("https://jobs.ashbyhq.com/")
    assert ashby.listing_url("mistral.ai").endswith("/job-board/mistral.ai")  # dots kept


def test_ashby_skips_unlisted_jobs():
    data = fixture_json("ashby_mistral.json")
    data["jobs"][0]["isListed"] = False
    assert len(ashby.parse("Acme", data)) == len(data["jobs"]) - 1


def test_recruitee():
    http, _ = fake_http({recruitee.listing_url("bunq"): fixture_json("recruitee_bunq.json")})
    first, second = fetch(company(board="recruitee", slug="bunq"), http)
    assert first.title == "Head of Product"
    assert first.locations == ["Amsterdam, Netherlands", "Sofia, Bulgaria"]
    assert first.url.startswith("https://careers.bunq.com/o/")
    assert first.posted == "2026-10-15"
    assert first.description


LSEG = "https://lseg.wd3.myworkdayjobs.com/Careers"
LSEG_API = "https://lseg.wd3.myworkdayjobs.com/wday/cxs/lseg/Careers"


def workday_routes() -> dict:
    page = fixture_json("workday_lseg_jobs.json")
    detail_path = page["jobPostings"][1]["externalPath"]
    return {LSEG_API + "/jobs": page, LSEG_API + detail_path: fixture_json("workday_lseg_detail.json")}


@pytest.mark.parametrize("url, site", [
    ("https://lseg.wd3.myworkdayjobs.com/Careers", "Careers"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/Acme_Careers", "Acme_Careers"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/Acme_Careers/", "Acme_Careers"),
])
def test_workday_site_from_url(url, site):
    parsed = workday.Site.from_url(url)
    assert parsed.site == site and parsed.tenant == url.split("//")[1].split(".")[0]


def test_workday_rejects_other_urls():
    with pytest.raises(SourceError):
        workday.Site.from_url("https://example.com/careers")


def test_workday_fetches_details_only_for_wanted_titles():
    http, session = fake_http(workday_routes())
    jobs = fetch(company(board="workday", url=LSEG), http, details=lambda job: "Content" in job.title)
    senior, wanted = jobs
    assert senior.description == "" and senior.posted is None  # listing only
    assert senior.locations == ["New York City, United States"]
    assert wanted.source_id == "R0123958"  # bulletFields[0]
    assert wanted.posted == "2026-10-16"  # detail startDate, not "Posted 8 Days Ago"
    assert wanted.locations == ["GBR-Edinburgh-5th Floor, Exchange Tower, United Kingdom"]
    assert wanted.employment_type == "Full time"
    assert wanted.description
    assert len(session.urls("GET")) == 1


def test_workday_pages_until_total():
    pages = ({"total": 25, "jobPostings": [{"title": f"Job {i}", "externalPath": f"/job/x/Job_{i}",
                                             "bulletFields": [f"R{i}"]} for i in range(20)]},
             {"total": 0, "jobPostings": [{"title": f"Job {i}", "externalPath": f"/job/x/Job_{i}",
                                           "bulletFields": [f"R{i}"]} for i in range(20, 25)]})
    http, session = fake_http({LSEG_API + "/jobs": pages})
    jobs = fetch(company(board="workday", url=LSEG), http, details=no_details)
    assert len(jobs) == 25
    assert [body["offset"] for _, _, body in session.calls] == [0, 20]


def test_workday_skips_entries_that_are_not_postings():
    page = {"total": 2, "jobPostings": [{"title": "Analyst", "externalPath": "/job/x/Analyst_1", "bulletFields": ["R1"]},
                                        {"title": "Talent community"},  # no externalPath (Blackstone)
                                        {"externalPath": "/job/x/Untitled_2", "bulletFields": ["R2"]}]}  # no title
    http, _ = fake_http({LSEG_API + "/jobs": page})
    assert [j.title for j in fetch(company(board="workday", url=LSEG), http, details=no_details)] == ["Analyst"]


def test_workday_detail_failure_keeps_the_job():
    routes = workday_routes()
    routes = {LSEG_API + "/jobs": routes[LSEG_API + "/jobs"]}  # every detail 404s
    http, _ = fake_http(routes)
    assert len(fetch(company(board="workday", url=LSEG), http)) == 2


def test_workday_search_texts_are_merged():
    page = fixture_json("workday_lseg_jobs.json")
    http, session = fake_http({LSEG_API + "/jobs": page})
    jobs = fetch(company(board="workday", url=LSEG, search=["graduate", "intern"]), http, details=no_details)
    assert len(jobs) == 2  # same postings from both searches, kept once
    assert [body["searchText"] for _, _, body in session.calls] == ["graduate", "intern"]


OPTIVER_SITEMAP = "https://www.optiver.com/sitemap.xml"
OPTIVER_PATTERN = r"^https://www\.optiver\.com/join-us/jobs/[^/]+/[^/]+/[^/]+/$"


def test_custom_sitemap_listing_and_json_ld():
    sitemap = fixture_text("custom_optiver_sitemap.xml")
    job_urls = [u for u in custom.sitemap_links(sitemap, custom.re.compile(OPTIVER_PATTERN))]
    assert len(job_urls) == 3  # non-job pages in the sitemap are ignored
    first_url = job_urls[0][0]
    http, _ = fake_http({OPTIVER_SITEMAP: sitemap, first_url: fixture_text("custom_optiver_job.html")})
    jobs = fetch(company(board="custom", listing_url=OPTIVER_SITEMAP, link_pattern=OPTIVER_PATTERN), http,
                 details=lambda job: job.url == first_url)
    assert len(jobs) == 3
    detailed = jobs[0]
    assert detailed.title == "AI Engineer"
    assert detailed.locations == ["Amsterdam"]  # a Place with only a name
    assert detailed.posted == "2026-02-13"
    assert detailed.description == "Page body trimmed."  # no description in JSON-LD: page text
    assert jobs[1].title == custom.title_from_url(jobs[1].url) and jobs[1].description == ""


def test_custom_listing_page_with_base_href():
    listing = """<html><head><base href="https://jobs.example.com/careers/"></head><body>
      <a href="jobs/results/123-software-engineer?x=1"></a>
      <a href="jobs/results/456-data-engineer">Data Engineer</a>
      <a href="jobs/results/456-data-engineer">Apply</a>
      <a href="/about">About</a></body></html>"""
    http, _ = fake_http({"https://jobs.example.com/careers/search": listing})
    jobs = fetch(company(board="custom", listing_url="https://jobs.example.com/careers/search",
                         link_pattern=r"/jobs/results/\d+"), http, details=no_details)
    assert [(j.title, j.url) for j in jobs] == [
        ("Software Engineer", "https://jobs.example.com/careers/jobs/results/123-software-engineer?x=1"),
        ("Data Engineer", "https://jobs.example.com/careers/jobs/results/456-data-engineer"),
    ]


def test_custom_cap_limits_detail_pages_not_links():
    listing = "".join(f'<a href="/job/{i}">Engineer {i}</a>' for i in range(5))
    routes = {"https://x.example/jobs": listing}
    routes.update({f"https://x.example/job/{i}": "<title>Job</title><p>Text</p>" for i in range(5)})
    http, session = fake_http(routes)
    jobs = fetch(company(board="custom", listing_url="https://x.example/jobs", link_pattern=r"/job/\d"),
                 http, custom_max_links=2)
    assert len(jobs) == 5
    assert sum(1 for u in session.urls() if "/job/" in u) == 2


def test_custom_respects_robots():
    robots = "User-agent: *\nDisallow: /job/\n"
    listing = '<a href="/job/1">Engineer</a>'
    http, session = fake_http({"https://x.example/robots.txt": robots, "https://x.example/jobs": listing})
    jobs = fetch(company(board="custom", listing_url="https://x.example/jobs", link_pattern=r"/job/"), http)
    assert len(jobs) == 1 and jobs[0].description == ""
    assert "https://x.example/job/1" not in session.urls()


def test_custom_json_ld_in_graph_with_address():
    page = """<script type="application/ld+json">{"@graph": [{"@type": "WebPage"},
      {"@type": "JobPosting", "title": "Graduate  Engineer", "description": "<p>Build &amp; ship</p>",
       "datePosted": "2026-10-01T09:00:00Z", "validThrough": "2026-11-30",
       "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Berlin", "addressCountry": "DE"}}],
       "employmentType": ["FULL_TIME", "INTERN"]}]}</script>"""
    job = custom.Job(company="Acme", title="x", url="u", source="custom", source_id="1")
    custom.fill_from_page(job, page)
    assert (job.title, job.description, job.posted, job.deadline) == ("Graduate Engineer", "Build & ship",
                                                                      "2026-10-01", "2026-11-30")
    assert job.locations == ["Berlin, DE"]
    assert job.employment_type == "FULL_TIME, INTERN"


@pytest.mark.parametrize("title, expected", [
    ("Quality Engineer, Radar Engineering - Jobs - Careers at Apple", "Quality Engineer, Radar Engineering"),
    ("Account Strategist — Google Careers", "Account Strategist"), ("Werkstudent Finance (m/w/d) | Jobs at Acme", "Werkstudent Finance (m/w/d)"),
    ("Data Engineer | Careers", "Data Engineer"), ("Analyst – Jobs at Acme", "Analyst"),
    ("Stagiair Data - Vacatures - Acme", "Stagiair Data"), ("Graduate Engineer", "Graduate Engineer"),
    ("Careers", "Careers"),
])
def test_page_title_drops_the_careers_site_name(title, expected):
    assert custom.page_title(title) == expected


def test_apple_reads_the_data_its_pages_embed():
    from jobhunt.sources import apple

    search = "https://jobs.apple.com/en-us/search?location=germany-DEU&page={page}"
    cork = "https://jobs.apple.com/en-us/details/200682946/mechanical-engineer?team=CORSV"
    routes = {"https://jobs.apple.com/robots.txt": "", search.format(page=1): fixture_text("apple_search.html"),
              cork: fixture_text("apple_job.html")}
    http, session = fake_http(routes)
    jobs = fetch(company(board="apple", listing_url=search), http, details=lambda j: j.url == cork)
    by_id = {j.source_id: j for j in jobs}
    assert len(jobs) == 3 and all(j.source == "apple" for j in jobs)
    assert search.format(page=2) not in session.urls()  # 3 results in all: one page
    engineer = by_id["200682946"]
    assert (engineer.title, engineer.locations, engineer.posted, engineer.department, engineer.url) == (
        "Mechanical Engineer", ["Ireland"], "2026-10-29", "Corporate Functions", cork)
    assert engineer.description.startswith("In Places") and "\n\nResponsibilities\n" in engineer.description
    assert by_id["114438158"].locations == ["United States of America"] and by_id["114438158"].description == ""
    assert by_id["114438043"].title == "DE - Specialist: Seasonal, Full-time and Part-time"
    with pytest.raises(SourceError):
        apple.embedded("<html>no data</html>")


def test_google_reads_the_jobs_its_search_page_embeds():
    from jobhunt.sources import google

    base = "https://www.google.com/about/careers/applications/jobs/results?target_level=EARLY&sort_by=date"
    page = fixture_text("google_search.html")
    routes = {"https://www.google.com/robots.txt": "User-agent: *\nDisallow: /about/careers/applications/jobs/results?*&page=\n",
              base + "&location=Netherlands": page, base + "&location=India": page}
    http, session = fake_http(routes)
    jobs = fetch(company(board="google", listing_url=base, locations=["Netherlands", "India"]), http)
    by_id = {j.source_id: j for j in jobs}
    assert len(jobs) == 3  # each job once, though both searches list it
    eemshaven = by_id["101635256011891398"]
    assert eemshaven.locations == ["Eemshaven, Netherlands"] and eemshaven.source == "google"
    assert eemshaven.title.startswith("Mechanical Technician II") and "Google Careers" not in eemshaven.title
    assert eemshaven.url == google.JOB_URL.format(id="101635256011891398") and eemshaven.posted == "2026-10-30"
    assert "Responsibilities" in eemshaven.description
    assert by_id["140848925899662022"].locations == ["Atlanta, GA, USA"]
    assert all("/jobs/results/" not in u for u in session.urls())  # no detail pages
    with pytest.raises(SourceError):
        google.listed("<html>no data</html>")


def test_unexpected_response_shape_is_a_source_error():
    http, _ = fake_http({greenhouse.listing_url("acme"): {"jobs": [{"title": "No URL or id"}]}})
    with pytest.raises(SourceError):
        fetch(company(board="greenhouse", slug="acme"), http)


def test_unknown_board_is_a_source_error():
    http, _ = fake_http()
    with pytest.raises(SourceError):
        fetch(company(board="nope"), http)


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><ul><li>One</li><li>Two</li></ul>"
                        "<script>bad()</script>") == "Hello world\n\n• One\n\n• Two"


@pytest.mark.parametrize("value, expected", [
    ("2026-10-16T09:02:17-04:00", "2026-10-16"), ("2026-10-15 09:31:36 UTC", "2026-10-15"),
    (1792767640768, "2026-10-23"), (None, None), ("Posted 30+ Days Ago", None),
])
def test_iso_date(value, expected):
    assert iso_date(value) == expected


def test_with_country():
    assert with_country("Paris", "France") == "Paris, France"
    assert with_country("Paris, France", "France") == "Paris, France"
    assert with_country(None, "France") == "France"
    assert with_country("Paris", None) == "Paris"


def test_greenhouse_light_listing_has_no_content():
    http, session = fake_http({greenhouse.listing_url("flowtraders", content=False): fixture_json("greenhouse_flowtraders.json")})
    jobs = fetch(company(board="greenhouse", slug="flowtraders"), http, details=no_details, light=True)
    assert session.urls() == ["https://boards-api.greenhouse.io/v1/boards/flowtraders/jobs"]
    assert jobs[0].board_company == "Flow Traders"


def test_greenhouse_light_listing_fetches_details_for_survivors_only():
    full = fixture_json("greenhouse_flowtraders.json")
    light = {"jobs": [{k: v for k, v in j.items() if k not in ("content", "offices", "departments")} for j in full["jobs"]],
             "meta": full["meta"]}
    wanted = full["jobs"][1]
    routes = {greenhouse.listing_url("flowtraders", content=False): light,
              greenhouse.JOB.format(slug="flowtraders", id=wanted["id"]): wanted}
    http, session = fake_http(routes)
    jobs = fetch(company(board="greenhouse", slug="flowtraders"), http,
                 details=lambda job: job.source_id == str(wanted["id"]), light=True)
    assert len(session.urls()) == 2
    detailed = next(j for j in jobs if j.source_id == str(wanted["id"]))
    assert detailed.description and detailed.locations == ["Amsterdam, Noord-Holland, Netherlands"]
    assert all(not j.description for j in jobs if j is not detailed)


@pytest.mark.parametrize("text, expected", [
    ("Posted Today", "2026-10-24"), ("Posted Yesterday", "2026-10-23"), ("Posted 3 Days Ago", "2026-10-21"),
    ("Posted 30+ Days Ago", None), (None, None),
])
def test_workday_posted_text(text, expected):
    from datetime import date
    assert workday.posted_from_text(text, today=date(2026, 10, 24)) == expected


def test_a_sitemap_with_a_stylesheet_and_a_comment_is_still_a_sitemap():
    from jobhunt.sources import custom

    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<?xml-stylesheet type="text/xsl" href="/s.xsl"?>\n'
            '<!--Generated by the Simple XML Sitemap Drupal module.-->\n<urlset xmlns="x"><url><loc>https://a.example/en/job-offers/x-1</loc></url></urlset>')
    assert custom.is_sitemap(body) and not custom.is_sitemap("<html><body>urlset</body></html>")


def test_workday_reads_only_the_configured_countries_through_its_location_facet():
    from jobhunt.sources import workday
    site = workday.Site.from_url("https://x.wd3.myworkdayjobs.com/External")
    first = {"total": 3, "jobPostings": [], "facets": [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
            {"descriptor": "Romania", "id": "ro", "count": 2}, {"descriptor": "India", "id": "in", "count": 9}]},
        {"facetParameter": "locations", "descriptor": "Location", "values": [{"descriptor": "Bucharest, Romania", "id": "buc", "count": 2}]}]}]}

    class Http:
        def __init__(self): self.payloads = []
        def post_json(self, url, payload):
            self.payloads.append(payload)
            return first
    http = Http()
    assert workday.country_facets(http, site, ["Romania"]) == {"locationCountry": ["ro"]}  # the country level wins
    assert workday.country_facets(http, site, ["France"]) == {}


def test_a_sitemap_index_is_followed_to_its_job_sitemaps():
    from jobhunt.sources import custom
    index = '<?xml version="1.0"?><sitemapindex><sitemap><loc>https://c.example/s1.xml</loc></sitemap></sitemapindex>'
    child = '<?xml version="1.0"?><urlset><url><loc>https://c.example/en/job/1/x</loc></url><url><loc>https://c.example/about</loc></url></urlset>'
    http, _ = fake_http({"https://c.example/index.xml": index, "https://c.example/s1.xml": child})
    company = Company(name="C", board="custom", listing_url="https://c.example/index.xml", link_pattern=r"/en/job/\d+/")
    assert [j.url for j in custom.fetch(company, http, details=no_details)] == ["https://c.example/en/job/1/x"]
