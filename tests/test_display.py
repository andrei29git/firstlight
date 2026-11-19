"""How names, places, dates and numbers are shown (2026-10-29)."""

from __future__ import annotations

from datetime import date

import pytest

from jobhunt import display

from .test_jobs_web import client, rows_in  # noqa: F401 (fixture)


@pytest.mark.parametrize("raw,shown", [
    ("Vienna, Vienna, Austria", "Vienna, Austria"), ("London, United Kingdom, GB", "London, UK"),
    ("Berlin, Germany, DE", "Berlin, Germany"), ("Belfast, Northern Ireland;", "Belfast, Northern Ireland"),
    ("Tallinn, EE", "Tallinn, EE"), ("Wilmington, DE", "Wilmington, DE"), ("Remote, United States", "Remote, US"),
])
def test_locations_are_cleaned(raw, shown):
    assert display.clean_location(raw) == shown


def test_long_location_lists_show_the_first_and_a_count():
    assert display.short_places(["Dublin, Dublin, Ireland", "Dublin, Ireland", "Seattle, WA", "New York, NY"]) == \
        "Dublin, Ireland +2"


def test_nameless_workday_sites_get_a_readable_name():
    assert display.company_label("https://adobe.wd5.myworkdayjobs.com/external_experienced") == "Adobe"
    assert display.company_label("https://bosch1.wd3.myworkdayjobs.com/x") == "Bosch"
    assert display.company_label("Stripe") == "Stripe"


def test_one_date_format_and_relative_recent_dates():
    today = date(2026, 10, 29)
    assert display.recent_date("2026-10-29", today) == "Today" and display.recent_date("2026-10-28", today) == "Yesterday"
    assert display.recent_date("2026-10-26", today) == "3 days ago" and display.recent_date("2026-10-12", today) == "12 Oct"
    assert display.nice_date("2025-06-03", today) == "Jun 2025" and display.number(1783) == "1,783"
    assert display.is_old("2026-03-01", today) and not display.is_old("2026-09-01", today)


def test_active_filters_show_as_removable_chips(client):
    html = client.get("/jobs?view=scored&odds=medium&status=clear").text
    assert ">Odds: Medium <" in html and ">Status: Clear <" in html
    assert 'href="/jobs?view=scored&amp;status=clear"' in html  # removing Odds keeps Status
    plain = client.get("/jobs?view=scored").text
    assert 'class="chip"' not in plain  # the view's own filters are not chips
    assert '<label class="filter"><span>Scope</span>' not in plain  # Scope repeats the Scored view


def test_roles_the_graduate_filter_never_scores_are_not_waiting(paths):
    from jobhunt import catalog as cat
    from jobhunt import config as cfg
    from jobhunt.state import State

    from .test_jobs_web import job
    state = State(paths.state_dir)
    state.jobs["g1"] = job("g1", "Graduate Engineer", company="Hooli", origin="generated")
    state.jobs["g2"] = job("g2", "Platform Engineer", company="Hooli", origin="generated")
    state.save()
    settings = cfg.load_settings(paths.config)
    graduate_only = {**settings, "generated": {**settings["generated"], "entry_level_only": True}}
    catalog = cat.Catalog(paths.state_dir, paths.config, graduate_only, paths.database.with_name("catalog.db"))
    catalog.refresh(wait=True)
    mine = cat.UserData(marks={}, applications={}, on_hold=set(), unlinked=[], wishlist=set(), blocked=set(), limit_apps={})
    waiting = [r.key for r in catalog.page(cat.Filters.from_query({"view": "waiting"}), mine).rows]
    skipped = [r.key for r in catalog.page(cat.Filters.from_query({"view": "skipped"}), mine).rows]
    assert "g1" in waiting and "g2" not in waiting and "g2" in skipped


def test_slug_names_from_boards_are_tidied_for_display_only():
    assert display.company_label("wiremind-1714146283", from_board=True) == "Wiremind"
    assert display.company_label("seb", from_board=True) == "SEB"
    assert display.company_label("ateme", from_board=True) == "Ateme"
    assert display.company_label("Outdooractive AG", from_board=True) == "Outdooractive AG"  # a proper name
    display.use_names(["bunq"])
    try:
        assert display.company_label("bunq") == "bunq"  # a name you set yourself stays as written
        assert display.company_label("BUNQ") == "bunq"  # and is the one name shown for it
    finally:
        display.use_names([])
    assert display.company_label("bunq") == "Bunq"


def test_one_display_name_everywhere():
    """The same stored name reads the same on every page, whichever way the role came in
    (2026-11-19: "almedia" on Applications, "Almedia" on Jobs)."""
    assert display.company_label("almedia") == display.company_label("almedia", from_board=True) == "Almedia"
    # A Workday site whose own words spell its tenant gives the name its spacing and capitals.
    assert display.company_label("https://kiongroup.wd3.myworkdayjobs.com/KIONGroup") == "KION Group"
    assert display.company_label("https://silvercross.wd5.myworkdayjobs.com/SilverCrossCareers") == "Silver Cross"
    # A site named for something else keeps the tenant.
    assert display.company_label("https://rochester.wd5.myworkdayjobs.com/UR_Nursing") == "Rochester"
    assert display.company_label("https://adobe.wd5.myworkdayjobs.com/external_experienced") == "Adobe"
    # Curated names for slugs and tenants that cannot be split by rule.
    assert display.company_label("https://fticonsulting.wd108.myworkdayjobs.com/fticonsultingcareersprivate") == "FTI Consulting"
    assert display.company_label("duravermeer") == "Dura Vermeer"


def test_posting_and_reply_text_as_safe_simple_html():
    html = str(display.rich_text("Intro line.\n\n\n\nYour work\n- Go services.\n- **Idempotent** code <b>!</b>\n\n"
                                 "What we offer:\n• Hybrid\n\n1. First\n2. Second"))
    assert html.count("<p>") == 1 and "<br>" not in html  # repeated blank lines collapse to one break
    assert '<h3 class="text-heading">Your work</h3><ul><li>Go services.</li>' in html
    assert "<strong>Idempotent</strong> code &lt;b&gt;!&lt;/b&gt;" in html  # escaped before anything else
    assert '<h3 class="text-heading">What we offer</h3><ul><li>Hybrid</li></ul>' in html
    assert "<ol><li>First</li><li>Second</li></ol>" in html
    # A short sentence is not a heading.
    assert "<h3" not in str(display.rich_text("Hybrid, three days a week.\nApply today."))


def test_deadlines_and_start_dates_in_one_format():
    today = date(2026, 11, 19)
    assert [display.due_label(d, today) for d in ("2026-11-19", "2026-11-20", "2026-11-24", "2026-11-18")] == \
        ["Due today", "Due tomorrow", "Due 24 Nov", "Deadline passed"]
    assert [display.due_tone(d, today) for d in ("2026-11-19", "2026-11-26", "2026-12-24", "2026-11-18")] == \
        ["soon", "soon", "", "past"]
    assert display.start_date_text("2027-10-01") == "1 Oct 2027" and display.start_date_text("2027-10") == "Oct 2027"
    assert display.start_date_text("September 2027") == "September 2027"
