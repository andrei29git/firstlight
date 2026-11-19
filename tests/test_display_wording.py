"""Display wording: places as "City, Country", posting text reflowed, plain dates, the Companies
wording and coverage sheet, and the wishlist getting Sonnet."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt import config as cfg
from jobhunt import display, pipeline, scoring, wishlist
from jobhunt.state import State
from jobhunt.web.app import create_app

from .conftest import EXAMPLE_CONFIG
from .fakes import FakeClaude
from .test_jobs_web import job, score


@pytest.mark.parametrize("text, label", [
    ("Iasi, ROU", "Iași, Romania"), ("PL - Warsaw, Poland", "Warsaw, Poland"), ("München, BY, Germany", "Munich, Germany"),
    ("Amsterdam, Nederland", "Amsterdam, Netherlands"), ("Bucureşti, Bucharest, Romania", "Bucharest, Romania"),
    ("Cork, County Cork, Ireland", "Cork, Ireland"), ("NLD Amsterdam (Radarweg)", "Amsterdam, Netherlands"),
    ("San Francisco, CA, USA", "San Francisco, United States"), ("Netherlands", "Netherlands"),
    ("Remote, Netherlands", "Remote, Netherlands"), ("Remote", "Remote"),
])
def test_places_read_as_city_and_country_in_english(text, label):
    assert display.place_label(text) == label


def test_posting_text_broken_mid_sentence_is_joined_again():
    text = ("Do you want to have a significant impact on how work is done here? Our tool is used by\n"
            "every major team to track their work.\n\nResponsibilities\n- Build tools\n- Work with teams\n\nShort.\nLines.")
    assert display.reflow(text) == ("Do you want to have a significant impact on how work is done here? Our tool is used by "
                                    "every major team to track their work.\n\nResponsibilities\n- Build tools\n- Work with teams"
                                    "\n\nShort.\nLines.")


def test_the_angle_does_not_repeat_lead_with_and_dates_read_plainly():
    assert display.angle_text("Lead with the payments gateway work") == "The payments gateway work"
    assert display.angle_text("Your Go refunds service") == "Your Go refunds service"
    assert display.long_date("2026-10-07") == "7 Oct 2026" and display.long_date(None) == ""
    assert display.is_past("2000-01-01") and not display.is_past("2999-01-01")


def test_companies_say_plainly_how_often_they_are_checked_and_the_coverage_sheet_can_open(paths):
    wishlist.set_company(paths.state_dir, "Mistral", True)
    with TestClient(create_app(paths)) as client:
        html = client.get("/companies").text
    assert 'title="Wishlist: checked every run"' in html  # Mistral: on your list and starred
    assert "generated-list rules" not in html
    # The table's own hx-select and hx-swap must not reach the coverage buttons inside it, or the
    # dialog is replaced instead of filled (the coverage sheet never opened before 2026-11-18).
    assert 'hx-disinherit="hx-select hx-swap' in html


def test_a_starred_companys_haiku_only_roles_get_sonnet_once(tmp_path):
    settings = cfg.load_settings(EXAMPLE_CONFIG)
    state = State(tmp_path / "state")
    text = "A graduate role."
    digest = scoring.description_hash(text)
    key = "greenhouse:Acme:a"  # the key a run gives it (source:company:id)
    state.jobs[key] = {**job("a", "Graduate Engineer", company="Acme", description_hash=digest), "key": key}
    state.write_posting(digest, text)
    haiku = settings["scoring"]["backlog_model"]
    state.scores[f"{key}@{haiku}"] = {**score(key, haiku, 5), "description_hash": digest}
    state.save()
    claude = FakeClaude()
    assert "wishlist, Sonnet for earlier Haiku scores" not in pipeline.score_all(
        EXAMPLE_CONFIG, settings, state, pipeline.Options(), claude)  # not starred: left as it is
    wishlist.set_company(state.root, "Acme", True)
    phases = pipeline.score_all(EXAMPLE_CONFIG, settings, state, pipeline.Options(), claude)
    assert phases["wishlist, Sonnet for earlier Haiku scores"]["calls"] == 1
    assert pipeline.has_result(state, state.jobs[key], settings["scoring"]["model"])


def test_gaps_read_in_lowercase_except_names():
    from jobhunt.display import gap_text
    assert gap_text("No production cloud experience") == "no production cloud experience"
    assert gap_text("Production Kubernetes") == "production Kubernetes"
    assert gap_text("On-call") == "on-call"  # one word, but a hyphenated common one
    for name in ("Solr", "Kafka Streams experience", "MCP integrations", "German at B2", "TypeScript depth"):
        assert gap_text(name) == name


def test_one_coverage_for_a_wishlist_company_in_the_sidebar_companies_and_its_page(paths):
    """2026-11-19: Channable was "–" in the sidebar, "Recruitee, 2 open" on Companies and "No job
    board was found" on its own page. A starred company with no board of its own is covered by its
    companies.toml entry, everywhere."""
    wishlist.set_company(paths.state_dir, "Mistral", True)  # on companies.toml with Ashby; no board of its own
    wishlist.set_company(paths.state_dir, "Stripe", True)  # no board anywhere
    with TestClient(create_app(paths)) as client:
        today = client.get("/today").text
        mistral = client.get("/jobs?view=all&company=mistral").text
        stripe = client.get("/jobs?view=all&company=stripe").text
        companies = client.get("/companies?group=wishlist").text
    def item(company: str) -> str:
        return today.split(f'href="/jobs?view=all&amp;company={company}"')[1].split("</a>")[0]
    assert "badge muted not-covered" not in item("mistral") and 'class="badge' in item("mistral")
    assert '<span class="badge muted not-covered">no board</span>' in item("stripe")  # said, not "–"
    assert 'role="note">Mistral is not checked every run' not in mistral and "No job board was found" not in mistral
    assert 'class="banner warn" role="note">Stripe is not checked every run' in stripe  # amber, not red
    assert companies.index(">Mistral<") < companies.index(">Stripe<")  # by name, as in the sidebar


def test_banners_are_red_only_for_errors():
    css = (Path(__file__).parents[1] / "jobhunt" / "web" / "static" / "style.css").read_text()
    last = css[css.rindex("/* Banners (2026-11-19)"):]
    assert ".banner { background: var(--fill-2)" in last and '.banner[role="alert"] { background: var(--bad-fill)' in last
