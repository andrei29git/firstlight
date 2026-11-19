from __future__ import annotations

from datetime import date
import tomllib

import pytest

from jobhunt import detect
from jobhunt.sources import greenhouse, recruitee

from .fakes import fake_http, fixture_json


@pytest.mark.parametrize("text, board, config", [
    ('<a href="https://boards.greenhouse.io/acme/jobs/123">', "greenhouse", {"slug": "acme"}),
    ('<a href="https://job-boards.eu.greenhouse.io/acme">', "greenhouse", {"slug": "acme"}),
    ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>', "greenhouse",
     {"slug": "acme"}),
    ('<a href="https://jobs.lever.co/acme/abc">', "lever", {"slug": "acme"}),
    ('<a href="https://jobs.eu.lever.co/acme">', "lever", {"slug": "acme", "region": "eu"}),
    ('<a href="https://jobs.ashbyhq.com/mistral.ai/123">', "ashby", {"slug": "mistral.ai"}),
    ('<iframe src="https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers/job/x">', "workday",
     {"url": "https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers"}),
    ('<script src="https://acme.recruitee.com/widget.js">', "recruitee", {"slug": "acme"}),
])
def test_recognises_supported_boards(text, board, config):
    found = detect.candidates(text)
    assert found[0].board == board and found[0].supported
    assert found[0].config == {"board": board, **config}


def test_board_paths_are_not_slugs():
    assert detect.candidates('<script src="https://boards.greenhouse.io/embed/job_board/js"></script>') == []


def test_reports_unsupported_boards():
    found = detect.candidates('<a href="https://external-acme.icims.com/jobs">')
    assert [(f.board, f.supported) for f in found] == [("icims", False)]


def test_detect_checks_candidates_live():
    page = '<a href="https://boards.greenhouse.io/flowtraders">Jobs</a> <a href="https://jobs.lever.co/gone">'
    http, _ = fake_http({
        "https://acme.example/careers": page,
        greenhouse.listing_url("flowtraders"): fixture_json("greenhouse_flowtraders.json"),
    })
    findings, final_url = detect.detect("https://acme.example/careers", http)
    by_board = {f.board: f for f in findings}
    assert by_board["greenhouse"].jobs == 3
    assert by_board["lever"].error and not by_board["lever"].works  # the invented slug is dropped
    assert final_url == "https://acme.example/careers"


def test_detect_probes_the_name_when_the_page_shows_no_board():
    http, _ = fake_http({
        "https://bunq.example/careers": "<div id=app></div>",
        recruitee.listing_url("bunq"): fixture_json("recruitee_bunq.json"),
    })
    findings, _ = detect.detect("https://bunq.example/careers", http, name="bunq")
    assert [(f.board, f.config.get("slug"), f.jobs) for f in findings] == [("recruitee", "bunq", 2)]
    assert findings[0].sample.startswith("https://careers.bunq.com/")


def test_slug_guesses():
    assert detect.slug_guesses("Grafana Labs") == ["grafanalabs", "grafana-labs"]
    assert "booking" in detect.slug_guesses("Booking.com")
    assert "mistral.ai" in detect.slug_guesses("mistral.ai")


def test_suggests_custom_config_from_job_links():
    page = ('<a href="/en/vacancies/backend-engineer">Backend</a><a href="/en/vacancies/data-engineer">Data</a>'
            '<a href="/en/about">About</a>')
    finding = detect.suggest_custom(page, "https://acme.example/en/careers")
    assert finding.config == {"board": "custom", "listing_url": "https://acme.example/en/careers",
                              "link_pattern": r"^https?://acme\.example/en/vacancies/"}


def test_toml_block_parses_back():
    finding = detect.Finding(board="custom", supported=True, jobs=4,
                             config={"board": "custom", "listing_url": "https://a.example/jobs",
                                     "link_pattern": r"^https?://a\.example/job/\d+"})
    block = detect.toml_block('Acme "Labs"', finding, date(2026, 1, 2))
    parsed = tomllib.loads(block)["company"][0]
    assert parsed["name"] == 'Acme "Labs"'
    assert parsed["link_pattern"] == r"^https?://a\.example/job/\d+"
    assert "verified" not in parsed  # custom patterns are guesses until checked by hand
