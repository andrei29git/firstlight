"""A nearly full week, and custom sites' titles and non-job pages."""

from __future__ import annotations

from datetime import UTC, datetime

from jobhunt import pipeline, prefilter
from jobhunt.companies import Company
from jobhunt.sources import custom
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .fakes import FakeClaude, fake_http
from .test_generated import ACME, NOW, answer, by_title, routes, settings  # noqa: F401 (fixture)
from .test_wishlist import WISHED_ACME

WEEKLY_WARNING = {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.86,
                  "resetsAt": datetime(2026, 10, 30, tzinfo=UTC).timestamp()}


def stored_score(state: State, key: str, scored_at: str, rate_limit: dict) -> None:
    state.scores[f"{key}@haiku"] = {"id": f"{key}@haiku", "key": key, "model": "haiku", "scored_at": scored_at,
                                    "rate_limit": rate_limit, "result": None, "description_hash": None}


def test_the_week_is_nearly_full_until_claude_reports_the_five_hour_window_again(tmp_path):
    state = State(tmp_path / "s")
    stored_score(state, "a", "2026-10-25T10:00:00+00:00", {"status": "allowed", "rateLimitType": "five_hour"})
    stored_score(state, "b", "2026-10-25T11:00:00+00:00", WEEKLY_WARNING)
    found = pipeline.week_nearly_full(state, NOW)
    assert found["message"] == "Claude warned the weekly limit is 86% used" and found["resets_at"].startswith("2026-10-30")
    stored_score(state, "c", "2026-10-25T12:00:00+00:00", {"status": "allowed", "rateLimitType": "five_hour"})
    assert pipeline.week_nearly_full(state, NOW) is None  # a later call is about the five-hour window again
    past = State(tmp_path / "t")
    stored_score(past, "b", "2026-10-25T11:00:00+00:00", WEEKLY_WARNING)
    assert pipeline.week_nearly_full(past, datetime(2026, 10, 31, tzinfo=UTC)) is None  # the week reset


def run(tmp_path, settings, companies, **options):  # noqa: F811
    http, _ = fake_http(routes())
    claude = FakeClaude(respond_argv=answer)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, companies, State(tmp_path / "state"), pipeline.Options(**options),
                           make_http=lambda: http, runner=claude, now=NOW)
    return summary, claude


def test_a_nearly_full_week_stops_backfill_runs_and_pauses_the_generated_list(tmp_path, settings):  # noqa: F811
    state = State(tmp_path / "state")
    stored_score(state, "old", "2026-10-25T08:00:00+00:00", WEEKLY_WARNING)
    state.save()
    summary, claude = run(tmp_path, settings, [ACME], backlog=True)
    assert "backlog runs wait until the week clears" in summary["backlog_stopped"] and claude.calls == []
    summary, claude = run(tmp_path, settings, [ACME])
    assert claude.calls == [] and "generated list waits" in summary["week_nearly_full"]  # ACME is generated
    wished = State(tmp_path / "w" / "state")
    stored_score(wished, "old", "2026-10-25T08:00:00+00:00", WEEKLY_WARNING)
    wished.save()
    summary, claude = run(tmp_path / "w", settings, [WISHED_ACME])  # a wishlist company is still scored
    assert claude.calls and summary["week_nearly_full"]
    summary, claude = run(tmp_path, settings, [ACME], ignore_weekly_warning=True)
    assert claude.calls  # --ignore-weekly-warning runs anyway


def test_pages_that_are_not_job_openings_are_dropped_by_their_title():
    for title in ("Meet A Trainee", "ING's Early Career Event", "Open Day Amsterdam", "Join our Talent Community",
                  "Open application - Engineering"):
        assert not prefilter.check_title(title).passed, title
    for title in ("Events And Issues Management Officer", "Graduate Engineer", "Open Source Engineer"):
        assert prefilter.check_title(title).passed, title


SITE = "https://careers.example.com"
LINKS = [f"{SITE}/en/job/amsterdam/graduate-engineer-w-m-d/3121/1", f"{SITE}/en/job/amsterdam/life-at-example/3121/2",
         f"{SITE}/en/job/amsterdam/data-analyst/3121/3"]


def page(title: str, posting: bool = True) -> str:
    ld = (f'<script type="application/ld+json">{{"@type": "JobPosting", "title": "{title}", '
          f'"description": "<p>{title}: Python services.</p>", "datePosted": "2026-10-20", '
          f'"jobLocation": {{"@type": "Place", "address": {{"addressLocality": "Amsterdam", "addressCountry": "NL"}}}}}}'
          f'</script>') if posting else ""
    return f"<html><head><title>{title} at Example</title>{ld}</head><body>{title}</body></html>"


def test_a_custom_site_keeps_the_title_read_from_the_page_and_drops_non_postings(tmp_path, settings):  # noqa: F811
    company = Company(name="Example Bank", board="custom", listing_url=SITE + "/sitemap.xml", link_pattern=r"/en/job/",
                      location_pattern=r"/en/job/(?P<location>[^/]+)/", postings_only=True)
    sitemap = "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in LINKS) + "</urlset>"
    http, session = fake_http({SITE + "/sitemap.xml": sitemap, LINKS[0]: page("Graduate Engineer (w/m/d)"),
                               LINKS[1]: page("Life at Example", posting=False), LINKS[2]: page("Data Analyst")})

    def fetch_once():
        return pipeline.run(EXAMPLE_CONFIG, settings, [company], State(tmp_path / "state"), pipeline.Options(score=False),
                            make_http=lambda: http, runner=FakeClaude(), now=NOW)
    fetch_once()
    titles = sorted(r["title"] for r in State(tmp_path / "state").jobs.values())
    assert titles == ["Data Analyst", "Graduate Engineer (w/m/d)"]  # the page without JobPosting data is not a job
    fetch_once()  # the next fetch reads only the sitemap: the titles from the pages stay
    after = by_title(State(tmp_path / "state"))
    assert set(after) == {"Data Analyst", "Graduate Engineer (w/m/d)"}
    assert after["Graduate Engineer (w/m/d)"]["locations"] == ["Amsterdam, NL"]


def test_a_title_made_from_the_url_gets_its_page_read_once_more(tmp_path, settings):  # noqa: F811
    company = Company(name="Example Bank", board="custom", listing_url=SITE + "/sitemap.xml", link_pattern=r"/en/job/")
    state = State(tmp_path / "state")
    key = f"custom:Example Bank:{custom.source_id(LINKS[0])}"
    state.jobs[key] = {"key": key, "company": "Example Bank", "title": custom.title_from_url(LINKS[0]),
                       "url": LINKS[0], "source": "custom", "source_id": custom.source_id(LINKS[0]), "locations": [],
                       "prefilter": "pass", "first_seen": "2026-10-20T00:00:00+00:00", "closed": False,
                       "description_hash": "h", "updated_at": "2026-10-20T00:00:00+00:00", "origin": "config"}
    state.save()
    sitemap = f"<urlset><url><loc>{LINKS[0]}</loc></url></urlset>"
    http, session = fake_http({SITE + "/sitemap.xml": sitemap, LINKS[0]: page("Graduate Engineer (w/m/d)")})
    pipeline.run(EXAMPLE_CONFIG, settings, [company], State(tmp_path / "state"), pipeline.Options(score=False),
                 make_http=lambda: http, runner=FakeClaude(), now=NOW)
    assert State(tmp_path / "state").jobs[key]["title"] == "Graduate Engineer (w/m/d)"
    assert LINKS[0] in session.urls()
