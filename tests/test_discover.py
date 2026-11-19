"""A wishlist company added by name alone: board slugs first, then a web search (WebSearch the only
tool) for its careers site, which the tool's own fetching then reads."""

from __future__ import annotations

from jobhunt import detect as det
from jobhunt import discover, scoring, wishlist

from .fakes import FakeClaude, claude_stream
from .test_jobs_web import client  # noqa: F401 (fixture)


def answer(**out):
    return FakeClaude(respond=lambda message: claude_stream({"careers_url": None, "job_board_url": None,
                                                             "note": "", **out}))


def test_the_search_may_use_web_search_and_nothing_else():
    claude = answer(careers_url="https://jobs.picnic.app/en/", job_board_url="https://jobs.picnic.app/en/",
                    note="Dutch grocery delivery company.")
    found = discover.search_careers_site("Picnic", {}, claude)
    assert found.urls == ["https://jobs.picnic.app/en/"] and found.note == "Dutch grocery delivery company."
    argv = claude.calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch" and argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert argv[argv.index("--setting-sources") + 1] == "" and claude.calls[0]["cwd_files"] == []
    assert claude.calls[0]["stdin"] == "Company: Picnic"
    scoring_argv = scoring.claude_argv(discover.prompt({}).__class__(system="s", version="v", model="haiku",
                                                                       max_description_chars=1, consider_start_date=False))
    assert scoring_argv[scoring_argv.index("--tools") + 1] == "" and "--allowedTools" not in scoring_argv


def test_only_web_addresses_are_taken_from_the_answer():
    found = discover.search_careers_site("Acme", {}, answer(careers_url="javascript:alert(1)",
                                                            job_board_url="jobs.acme.example"))
    assert found.urls == []
    failed = discover.search_careers_site("Acme", {}, FakeClaude(respond=lambda m: claude_stream(None)))
    assert failed.error == "claude returned no structured_output"


def test_a_name_alone_falls_back_to_a_web_search_and_shows_what_it_found(client, paths, monkeypatch):  # noqa: F811
    site = "https://careers.globex.example/jobs"
    custom = det.Finding(board="custom", config={"board": "custom", "listing_url": site, "link_pattern": "^https://careers"},
                         supported=True, jobs=12, samples=[("Graduate Engineer", site + "/1")])
    calls = []

    def detect(url, http, name=None):
        calls.append((url, name))
        return ([custom] if url == site else []), url
    monkeypatch.setattr(det, "detect", detect)
    monkeypatch.setattr(discover, "find_careers_site",
                        lambda name, settings, runner=None, timeout=0: discover.Found(careers_url=site, note="Globex, Berlin."))
    html = client.post("/wishlist/find", data={"name": "Globex"}).text
    assert calls == [(None, "Globex"), (site, None)]  # slugs from the name first, then the site the search found
    assert "Found with a web search" in html and "its own careers site" in html and "12 jobs" in html
    assert "Graduate Engineer" in html and "No job board answered to names made from the company" in html
    assert wishlist.load(paths.state_dir) == {}  # shown, not saved: the user chooses


def test_a_careers_page_given_by_hand_is_never_searched_for(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(det, "detect", lambda url, http, name=None: ([], url))
    searched = []
    monkeypatch.setattr(discover, "find_careers_site", lambda *a, **k: searched.append(a) or discover.Found())
    html = client.post("/wishlist/find", data={"name": "Initech", "url": "https://initech.example/careers"}).text
    assert searched == [] and "Saved with the star only" in html
