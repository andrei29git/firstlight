"""Scoring while the fetch runs: the same scores as scoring after it, the same priority order when
the budget is short, and duplicates merged before anything is scored."""

from __future__ import annotations

import threading
import time

import pytest

from jobhunt import config as cfg
from jobhunt import pipeline, scoring
from jobhunt.companies import Company
from jobhunt.sources import greenhouse
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE, FakeClaude, claude_stream, fake_http
from .test_pipeline import FLOW, URL, listing

BETA = Company(name="Beta", board="greenhouse", slug="beta")
SLOW = 0.4  # seconds Beta's board takes to answer


@pytest.fixture
def settings():
    return cfg.load_settings(EXAMPLE_CONFIG)


def board(*roles, slug="flowtraders"):
    body = listing(*roles)
    for i, raw in enumerate(body["jobs"]):
        raw["content"] += f"&lt;p&gt;{slug} team {i}.&lt;/p&gt;"
        raw["absolute_url"] = raw["absolute_url"].replace("flowtraders", slug)
    return body


def run(tmp_path, settings, routes, companies, claude, slow=("beta",), **options):
    http, session = fake_http(routes)
    real = session.request
    fetched_at: dict[str, float] = {}

    def slow_request(method, url, **kw):
        if any(f"/boards/{s}/" in url for s in slow):
            time.sleep(SLOW)
        response = real(method, url, **kw)
        fetched_at[url] = time.monotonic()
        return response
    session.request = slow_request
    summary = pipeline.run(EXAMPLE_CONFIG, settings, list(companies), State(tmp_path / "state"),
                           pipeline.Options(**options), make_http=lambda: http, runner=claude)
    return summary, State(tmp_path / "state"), fetched_at


class TimedClaude(FakeClaude):
    def __init__(self, respond=None):
        super().__init__(respond_argv=respond or (lambda m, a: claude_stream(GOOD_SCORE)))
        self.started: list[float] = []
        self.lock = threading.Lock()

    def __call__(self, argv, stdin, cwd, timeout, env):
        with self.lock:
            self.started.append(time.monotonic())
        return super().__call__(argv, stdin, cwd, timeout, env)


def scored(state):
    return sorted((state.jobs[s["key"]]["title"], s["model"]) for s in state.scores.values() if s.get("result"))


def test_scoring_starts_during_the_fetch_and_gives_the_same_scores(tmp_path, settings):
    routes = {URL: board(("Graduate Engineer", "Paris, France"), ("Data Engineer", "Paris, France")),
              greenhouse.listing_url("beta"): board(("Platform Intern", "Berlin, Germany"), slug="beta")}
    sequential = {**settings, "scoring": {**settings["scoring"], "score_while_fetching": False}}
    _, before, _ = run(tmp_path / "a", sequential, routes, (FLOW, BETA), TimedClaude())
    claude = TimedClaude()
    summary, after, fetched_at = run(tmp_path / "b", settings, routes, (FLOW, BETA), claude, slow=("beta",))
    assert scored(after) == scored(before) and len(scored(after)) == 3
    assert "during fetch: your companies and wishlist" in summary["scoring"]
    overlap = summary["overlap"]
    assert overlap["seconds_without_overlap"] >= summary["seconds"]
    assert overlap["scored_while_fetching"] == 3


def test_my_companies_keep_their_order_when_the_budget_is_short(tmp_path, settings):
    """Flow Traders arrives first with a role without graduate wording; Beta, slower, has a graduate
    role. With room for one call, the graduate role still goes first."""
    settings = {**settings, "usage": {**settings["usage"], "share_per_call": {"sonnet": 0.3, "haiku": 0.1},
                                      "scheduled_window_share": 0.35}}
    routes = {URL: board(("Data Engineer", "Paris, France")),
              greenhouse.listing_url("beta"): board(("Graduate Engineer", "Berlin, Germany"), slug="beta")}
    summary, state, _ = run(tmp_path, settings, routes, (FLOW, BETA), TimedClaude())
    assert scored(state) == [("Graduate Engineer", "sonnet")]


def test_duplicates_across_boards_are_merged_before_scoring(tmp_path, settings):
    """The same role on two boards of one company is scored once, even though the first board's
    copy is in long before the second board answers."""
    acme_a = Company(name="Acme", board="greenhouse", slug="acme-a")
    acme_b = Company(name="Acme", board="greenhouse", slug="acme-b")
    first = board(("Graduate Engineer", "Paris, France"), slug="acme")
    second = board(("Graduate Engineer", "Paris, France"), slug="acme")
    for raw in second["jobs"]:
        raw["id"] += 500  # another board: another id, same role and text
    routes = {greenhouse.listing_url("acme-a"): first, greenhouse.listing_url("acme-b"): second}
    claude = TimedClaude()
    summary, state, _ = run(tmp_path, settings, routes, (acme_a, acme_b), claude, slow=("acme-b",))
    assert len(claude.calls) == 1 and summary["duplicates"] == 1
    assert sum(1 for r in state.jobs.values() if r["duplicate_of"]) == 1


def test_other_generated_roles_are_scored_during_the_fetch_after_entry_level_ones(tmp_path, settings):
    """2026-10-31: kept generated roles without graduate wording waited for the minutes after the
    fetch; now they go during it, after the entry-level ones."""
    state = State(tmp_path / "state")
    for i, title in enumerate(("Backend Engineer", "Graduate Engineer")):
        text = f"{title} at Gamma."
        digest = scoring.description_hash(text)
        state.write_posting(digest, text)
        state.jobs[f"greenhouse:Gamma:{i}"] = {
            "key": f"greenhouse:Gamma:{i}", "company": "Gamma", "title": title, "url": f"u{i}", "source": "greenhouse",
            "source_id": str(i), "locations": ["Paris, France"], "prefilter": "pass", "description_hash": digest,
            "origin": "generated", "closed": False, "first_seen": "2026-10-30T09:00:00+00:00", "backlog": False,
            "parked": False, "titlecheck": {"dropped": False, "checked_at": "x", "version": "v"}}
    state.save()
    routes = {URL: board(("Data Engineer", "Paris, France"))}
    claude = TimedClaude(lambda m, a: claude_stream(GOOD_SCORE))
    summary, after, _ = run(tmp_path, settings, routes, (FLOW,), claude, slow=("flowtraders",))
    phases = summary["scoring"]
    assert phases["during fetch: generated (entry-level wording)"]["calls"] == 1
    assert phases["during fetch: generated (other roles)"]["calls"] == 1
    assert not phases.get("generated", {}).get("calls")  # nothing left for after the fetch
    assert {s["key"] for s in after.scores.values() if s["model"] == "haiku"} >= {"greenhouse:Gamma:0", "greenhouse:Gamma:1"}
    gamma = [c for c in claude.calls if "at Gamma." in c["stdin"] and c["argv"][c["argv"].index("--model") + 1] == "haiku"]
    assert [("Graduate" in c["stdin"]) for c in gamma] == [True, False]  # entry-level first
