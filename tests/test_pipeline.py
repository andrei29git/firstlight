"""A whole run against saved board responses and a fake `claude`."""

from __future__ import annotations

import pytest

from jobhunt import config as cfg
from jobhunt import pipeline
from jobhunt.companies import Company
from jobhunt.sources import greenhouse
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE, FakeClaude, claude_stream, fake_http, fixture_json

FLOW = Company(name="Flow Traders", board="greenhouse", slug="flowtraders")
URL = greenhouse.listing_url("flowtraders")


def listing(*titles_and_offices):
    """A Greenhouse listing built from the recorded fixture, with chosen titles and offices."""
    data = fixture_json("greenhouse_flowtraders.json")
    template = data["jobs"][1]
    jobs = []
    for i, (title, office) in enumerate(titles_and_offices):
        raw = dict(template, id=1000 + i, title=title, offices=[{"location": office}],
                   absolute_url=f"https://job-boards.greenhouse.io/flowtraders/jobs/{1000 + i}")
        jobs.append(raw)
    return {"jobs": jobs, "meta": {"total": len(jobs)}}


@pytest.fixture
def settings():
    """Scoring after the fetch, phase by phase (these tests check each phase; overlap: test_overlap.py)."""
    loaded = cfg.load_settings(EXAMPLE_CONFIG)
    return {**loaded, "scoring": {**loaded["scoring"], "score_while_fetching": False}}


def do_run(tmp_path, settings, body, claude=None, companies=(FLOW,), **options):
    http, _ = fake_http({URL: body})
    claude = claude or FakeClaude()
    summary = pipeline.run(EXAMPLE_CONFIG, settings, list(companies), State(tmp_path / "state"),
                           pipeline.Options(**options), make_http=lambda: http, runner=claude)
    return summary, State(tmp_path / "state"), claude


def test_run_prefilters_and_scores_only_survivors(tmp_path, settings):
    body = listing(("Graduate Software Engineer", "Amsterdam, Netherlands"),
                   ("Senior Software Engineer", "Amsterdam, Netherlands"),
                   ("Software Engineer", "Bengaluru, India"))
    summary, state, claude = do_run(tmp_path, settings, body)
    assert summary["jobs"] == {"fetched": 3, "passed": 1, "dropped title": 1, "dropped location": 1,
                               "new": 3, "new passed": 1}
    assert summary["scoring"]["scheduled"]["calls"] == 1 and len(claude.calls) == 1
    passed = [r for r in state.jobs.values() if r["prefilter"] == "pass"]
    assert len(passed) == 1 and state.scores[f'{passed[0]["key"]}@sonnet']["result"]["fit"] == 8
    # Only the survivor's text is kept.
    assert len(list((tmp_path / "state" / "postings").iterdir())) == 1


def test_each_role_is_scored_once(tmp_path, settings):
    body = listing(("Graduate Software Engineer", "Amsterdam, Netherlands"))
    do_run(tmp_path, settings, body)
    summary, _, claude = do_run(tmp_path, settings, body)
    assert summary["scoring"]["scheduled"]["calls"] == 0 and claude.calls == []


def test_new_posting_text_rescores(tmp_path, settings):
    body = listing(("Graduate Software Engineer", "Amsterdam, Netherlands"))
    do_run(tmp_path, settings, body)
    body["jobs"][0]["content"] += "&lt;p&gt;Now with Rust.&lt;/p&gt;"
    summary, _, _ = do_run(tmp_path, settings, body)
    assert summary["scoring"]["scheduled"]["calls"] == 1


def test_scoring_cap_takes_graduate_wording_first(tmp_path, settings):
    body = listing(("Software Engineer, Payments", "London, United Kingdom"),
                   ("Software Engineer Intern", "London, United Kingdom"))
    body["jobs"][1]["content"] += "&lt;p&gt;Different team.&lt;/p&gt;"
    summary, state, claude = do_run(tmp_path, settings, body, cap=1)
    assert summary["scoring"]["scheduled"]["deferred"] == 1
    assert "Software Engineer Intern" in claude.calls[0]["stdin"]


def test_vanished_jobs_close_only_after_a_good_fetch(tmp_path, settings):
    do_run(tmp_path, settings, listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France")))
    summary, state, _ = do_run(tmp_path, settings, 500, score=False)  # a failed fetch closes nothing
    assert summary["errors"] == 1 and not any(r["closed"] for r in state.jobs.values())
    summary, state, _ = do_run(tmp_path, settings, listing(("Graduate Engineer", "Paris, France")), score=False)
    assert summary["jobs"]["closed"] == 1
    assert [r["title"] for r in state.jobs.values() if r["closed"]] == ["Data Intern"]


def test_partial_listings_never_close(tmp_path, settings):
    partial = Company(name="Flow Traders", board="greenhouse", slug="flowtraders", partial=True)
    do_run(tmp_path, settings, listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France")),
           companies=(partial,), score=False)
    summary, _, _ = do_run(tmp_path, settings, listing(("Graduate Engineer", "Paris, France")),
                           companies=(partial,), score=False)
    assert "closed" not in summary["jobs"]


def test_same_role_in_two_cities_is_one_job(tmp_path, settings):
    body = listing(("Graduate Engineer (Amsterdam)", "Amsterdam, Netherlands"),
                   ("Graduate Engineer (London)", "London, United Kingdom"))
    summary, state, claude = do_run(tmp_path, settings, body)
    assert summary["duplicates"] == 1 and len(claude.calls) == 1
    head = next(r for r in state.jobs.values() if r["duplicate_of"] is None)
    assert head["merged_locations"] == ["Amsterdam, Netherlands", "London, United Kingdom"]
    assert "London" in claude.calls[0]["stdin"] and "Amsterdam" in claude.calls[0]["stdin"]


def test_failed_scores_are_retried_a_few_times(tmp_path, settings):
    body = listing(("Graduate Engineer", "Paris, France"))
    broken = FakeClaude(lambda m: "garbage")
    for _ in range(pipeline.MAX_SCORE_ATTEMPTS + 2):
        do_run(tmp_path, settings, body, claude=broken)
    assert len(broken.calls) == pipeline.MAX_SCORE_ATTEMPTS


def test_run_summary_is_recorded(tmp_path, settings):
    summary, state, _ = do_run(tmp_path, settings, listing(("Graduate Engineer", "Paris, France")))
    runs = state.runs()
    assert len(runs) == 1 and runs[0]["scoring"]["scheduled"]["cost_usd"] == 0.02
    assert summary["sources"]["Flow Traders"]["fetched"] == 1


def by_model(fits: dict[str, dict[str, int]]):
    """A fake claude answering with a fit chosen by model and by a word in the posting title."""
    def respond(message, argv):
        model = argv[argv.index("--model") + 1]
        fit = next(f for word, f in fits[model].items() if word in message)
        return claude_stream({**GOOD_SCORE, "fit": fit})
    return FakeClaude(respond_argv=respond)


def models_called(claude):
    return [c["argv"][c["argv"].index("--model") + 1] for c in claude.calls]


def test_backlog_uses_haiku_then_sonnet_for_promising_roles(tmp_path, settings):
    body = listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France"))
    body["jobs"][1]["content"] += "&lt;p&gt;Different team.&lt;/p&gt;"
    claude = by_model({"haiku": {"Graduate Engineer": 7, "Data Intern": 4},
                       "sonnet": {"Graduate Engineer": 8, "Data Intern": 5}})
    summary, state, _ = do_run(tmp_path, settings, body, claude=claude, backlog=True)
    assert summary["scoring"]["backlog"]["calls"] == 2 and summary["scoring"]["rescore"]["calls"] == 1
    assert sorted(models_called(claude)) == ["haiku", "haiku", "sonnet"]
    grad = next(r for r in state.jobs.values() if r["title"] == "Graduate Engineer")
    intern = next(r for r in state.jobs.values() if r["title"] == "Data Intern")
    assert set(state.scores_for(grad["key"])) == {"haiku", "sonnet"}
    assert pipeline.preferred_score(state, grad, settings)["result"]["fit"] == 8  # Sonnet shown
    assert pipeline.preferred_score(state, intern, settings)["model"] == "haiku"
    # A scheduled run afterwards scores nothing: every role has a score.
    summary, _, claude = do_run(tmp_path, settings, body)
    assert summary["scoring"]["scheduled"]["calls"] == 0


def test_scheduled_runs_use_the_scheduled_model(tmp_path, settings):
    _, _, claude = do_run(tmp_path, settings, listing(("Graduate Engineer", "Paris, France")))
    assert models_called(claude) == ["sonnet"]


def test_scores_written_before_ids_still_load(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "scores.jsonl").write_text('{"key": "greenhouse:A:1", "model": "haiku", "result": null}\n')
    assert "greenhouse:A:1@haiku" in State(folder).scores


def test_backlog_budget_is_shared_by_both_phases(tmp_path, settings):
    settings = {**settings, "usage": {"share_per_call": {"haiku": 0.1, "sonnet": 0.3}, "backlog_window_share": 0.25}}
    body = listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France"))
    body["jobs"][1]["content"] += "&lt;p&gt;Different team.&lt;/p&gt;"
    claude = by_model({"haiku": {"Graduate Engineer": 7, "Data Intern": 7}, "sonnet": {"Graduate Engineer": 8, "Data Intern": 8}})
    summary, _, _ = do_run(tmp_path, settings, body, claude=claude, backlog=True)
    # Haiku spends 0.2 of the 0.25 budget; the first Sonnet call (0.3) would pass it, so none starts.
    assert summary["scoring"]["backlog"]["calls"] == 2 and summary["scoring"]["rescore"]["calls"] == 0
    assert "run budget reached" in summary["scoring"]["rescore"]["stopped"]


def test_shortlist_lists_strong_sonnet_roles_first(tmp_path, settings):
    from jobhunt import shortlist
    body = listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France"),
                   ("Platform Intern", "Paris, France"))
    for i, raw in enumerate(body["jobs"]):
        raw["content"] += f"&lt;p&gt;Team {i}.&lt;/p&gt;"
    answers = {"Graduate Engineer": {"fit": 8}, "Data Intern": {"fit": 9, "action": "skip"},
               "Platform Intern": {"fit": 7}}
    claude = FakeClaude(lambda m: claude_stream({**GOOD_SCORE, **next(v for k, v in answers.items() if k in m)}))
    _, state, _ = do_run(tmp_path, settings, body, claude=claude)
    version = shortlist.current_version(EXAMPLE_CONFIG, settings)
    found = shortlist.rows(state, settings, version)
    assert [r.job["title"] for r in found] == ["Graduate Engineer", "Platform Intern"]  # skip excluded, fit order
    assert not any(r.stale for r in found)
    shortlist.write_csv(found, tmp_path / "out.csv")
    lines = (tmp_path / "out.csv").read_text().splitlines()
    assert lines[0].startswith("Snapshot only") and len(lines) == 4


def test_shortlist_leaves_out_roles_with_an_application(tmp_path, settings):
    from jobhunt import shortlist, store
    _, state, _ = do_run(tmp_path, settings, listing(("Graduate Engineer (2027)", "Paris, France")))
    version = shortlist.current_version(EXAMPLE_CONFIG, settings)
    conn = store.connect(tmp_path / "db.sqlite")
    store.add_application(conn, company="Flow Traders B.V.", role="Graduate Engineer", stage="rejected")
    conn.close()
    hidden: list = []
    applied = shortlist.load_applied(tmp_path / "db.sqlite")
    assert shortlist.rows(state, settings, version, applied=applied, hidden=hidden) == []
    assert hidden[0][1].stage == "rejected"
    assert len(shortlist.rows(state, settings, version)) == 1
    assert shortlist.load_applied(tmp_path / "missing.db") == []


def four_roles():
    body = listing(*[(t, "Paris, France") for t in ("Graduate Engineer", "Data Intern", "Platform Intern",
                                                     "Backend Graduate")])
    for i, raw in enumerate(body["jobs"]):
        raw["content"] += f"&lt;p&gt;Team {i}.&lt;/p&gt;"
    return body


def test_claude_being_unavailable_does_not_count_against_roles(tmp_path, settings):
    tries = []

    def logged_out(argv, stdin, cwd, timeout, env):
        tries.append(1)
        return 1, "", "Invalid API key. Please run /login"
    for _ in range(pipeline.MAX_SCORE_ATTEMPTS + 1):
        summary, state, _ = do_run(tmp_path, settings, four_roles(), claude=logged_out, parallel=1)
    phase = summary["scoring"]["scheduled"]
    # Each call is tried 3 times; the run stops after 3 calls in a row fail even so.
    assert phase["calls"] == 3 and "3 failed calls in a row" in phase["stopped"]
    assert len(tries) == 9 * (pipeline.MAX_SCORE_ATTEMPTS + 1)
    assert "Invalid API key" in phase["failures"][0] and phase["retries"] == 6
    assert state.scores == {}  # nothing recorded, so nothing is ever given up
    summary, state, _ = do_run(tmp_path, settings, four_roles())  # logged in again: all are scored
    assert summary["scoring"]["scheduled"]["calls"] == 4


def test_one_failed_call_is_retried_and_the_run_carries_on(tmp_path, settings):
    calls = []

    def flaky(argv, stdin, cwd, timeout, env):  # "claude exited 1" with nothing printed, once
        calls.append(1)
        if len(calls) == 2:
            return 1, "", ""
        return 0, claude_stream(GOOD_SCORE), ""
    summary, state, _ = do_run(tmp_path, settings, four_roles(), claude=flaky, parallel=1)
    phase = summary["scoring"]["scheduled"]
    assert (phase["calls"], phase["ok"], phase["retries"], phase["stopped"]) == (4, 4, 1, None)
    assert "stderr: (empty)" in phase["failures"][0]


def test_failures_far_apart_never_stop_the_run(tmp_path, settings):
    calls = []

    def every_other_fails(argv, stdin, cwd, timeout, env):  # each failure persists through its retries
        calls.append(1)
        return (1, "", "API Error: 529 overloaded") if stdin.count("Team 1") or stdin.count("Team 3") \
            else (0, claude_stream(GOOD_SCORE), "")
    summary, _, _ = do_run(tmp_path, settings, four_roles(), claude=every_other_fails, parallel=1)
    phase = summary["scoring"]["scheduled"]
    assert phase["calls"] == 4 and phase["ok"] == 2 and phase["stopped"] is None  # never 3 in a row


def test_entry_level_wording_is_scored_first_then_the_newest():
    def job(title, posted):
        return {"title": title, "posted": posted, "first_seen": posted + "T00:00:00+00:00"}
    jobs = [job("Platform Engineer", "2026-10-24"), job("Associate Software Engineer", "2026-10-01"),
            job("Backend Engineer", "2026-10-20"), job("Graduate Engineer", "2026-10-10")]
    assert [j["title"] for j in sorted(jobs, key=pipeline.priority)] == [
        "Graduate Engineer", "Associate Software Engineer", "Platform Engineer", "Backend Engineer"]


def two_promising_roles():
    body = listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France"))
    body["jobs"][1]["content"] += "&lt;p&gt;Different team.&lt;/p&gt;"
    return body


def test_a_backlog_run_gives_sonnet_the_earlier_fit_6_roles_first(tmp_path, settings):
    settings = {**settings, "usage": {"share_per_call": {"haiku": 0.1, "sonnet": 0.3}, "backlog_window_share": 0.25}}
    fits = {"haiku": {"Graduate Engineer": 7, "Data Intern": 8, "Platform Intern": 6},
            "sonnet": {"Graduate Engineer": 8, "Data Intern": 9, "Platform Intern": 7}}
    do_run(tmp_path, settings, two_promising_roles(), claude=by_model(fits), backlog=True)  # Haiku only: budget
    body = two_promising_roles()
    body["jobs"].append({**body["jobs"][0], "id": 99, "title": "Platform Intern",
                         "content": "&lt;p&gt;Platform Intern: Go services.&lt;/p&gt;"})
    claude = by_model(fits)
    summary, state, _ = do_run(tmp_path, settings, body, claude=claude, backlog=True, budget=0.35)
    phases = list(summary["scoring"])
    assert phases[0] == "fit 6+ from earlier runs"  # before any new Haiku scoring
    assert summary["scoring"]["fit 6+ from earlier runs"]["calls"] == 1 and models_called(claude) == ["sonnet"]
    assert "Data Intern" in claude.calls[0]["stdin"]  # Haiku 8 before Haiku 7
    assert "run budget reached" in summary["scoring"]["fit 6+ from earlier runs"]["stopped"]
    assert "backlog" not in summary["scoring"]  # --budget 0.35 left 0.05: no new Haiku scoring started
    intern = next(r for r in state.jobs.values() if r["title"] == "Data Intern")
    assert pipeline.preferred_score(state, intern, settings)["model"] == "sonnet"


def test_sonnet_only_rescores_and_scores_nothing_new(tmp_path, settings):
    fits = {"haiku": {"Graduate Engineer": 7, "Data Intern": 5}, "sonnet": {"Graduate Engineer": 8, "Data Intern": 6}}
    settings_cap = {**settings, "usage": {"share_per_call": {"haiku": 0.1, "sonnet": 0.3}, "backlog_window_share": 0.25}}
    do_run(tmp_path, settings_cap, two_promising_roles(), claude=by_model(fits), backlog=True)
    claude = by_model(fits)
    summary, _, _ = do_run(tmp_path, settings, two_promising_roles(), claude=claude, companies=(),
                           sonnet_only=True, budget=1.0)
    assert list(summary["scoring"]) == ["fit 6+ from earlier runs"]
    assert models_called(claude) == ["sonnet"] and "Graduate Engineer" in claude.calls[0]["stdin"]  # not Haiku 5


def test_sonnet_only_fetches_nothing():
    import argparse

    from jobhunt import cli
    args = argparse.Namespace(no_fetch=False, sonnet_only=True, names=[], local_only=False, generated="all")
    assert cli.run_targets(args, None, {}, None) == []


def test_one_role_posted_per_city_with_the_city_in_the_title_is_scored_once(tmp_path, settings):
    body = listing(*[(f"Graduate Engineer - {city}", "Remote") for city in
                     ("Chicago, IL, USA", "Paris, France", "Berlin")],
                   ("Graduate Engineer - Payments", "Remote"))  # not a place: a different role
    for raw in body["jobs"]:
        raw["content"] = "&lt;p&gt;Graduate Engineer: build Python services for our reading app.&lt;/p&gt;"
    body["jobs"][3]["content"] = "&lt;p&gt;Payments team: card processing and ledgers in Go.&lt;/p&gt;"
    summary, state, claude = do_run(tmp_path, settings, body)
    assert summary["duplicates"] == 2 and len(claude.calls) == 2
    heads = sorted(r["title"] for r in state.jobs.values() if not r["duplicate_of"])
    assert heads == ["Graduate Engineer - Chicago, IL, USA", "Graduate Engineer - Payments"]


def test_two_boards_of_one_company_never_close_each_others_roles(tmp_path, settings):
    first, second = (Company(name="Acme", board="greenhouse", slug=s) for s in ("acme-a", "acme-b"))
    a = listing(("Graduate Engineer", "Paris, France"))
    b = listing(("Data Intern", "Berlin, Germany"))
    b["jobs"][0]["id"] = 2000
    http, _ = fake_http({greenhouse.listing_url("acme-a"): a, greenhouse.listing_url("acme-b"): b})
    for _ in range(2):
        pipeline.run(EXAMPLE_CONFIG, settings, [first, second], State(tmp_path / "state"), pipeline.Options(score=False),
                     make_http=lambda: http, runner=FakeClaude())
    state = State(tmp_path / "state")
    assert sorted((r["title"], r["closed"], r["account"]) for r in state.jobs.values()) == [
        ("Data Intern", False, "greenhouse::acme-b"), ("Graduate Engineer", False, "greenhouse::acme-a")]


def test_marked_roles_are_scored_with_the_chosen_model_and_only_by_runs_for_them(tmp_path, settings):
    """Booking-style roles go to Sonnet directly; ING-style roles go Haiku first (Sonnet for 6+),
    and only in backlog runs; a run naming companies scores only theirs."""
    beta = Company(name="Beta", board="greenhouse", slug="beta")
    flow_body = listing(("Graduate Engineer", "Paris, France"), ("Data Intern", "Paris, France"))
    flow_body["jobs"][1]["content"] += "&lt;p&gt;Different team.&lt;/p&gt;"
    beta_body = listing(("Zeta Engineer", "Berlin, Germany"), ("Omega Intern", "Berlin, Germany"))
    for i, raw in enumerate(beta_body["jobs"]):
        raw["id"] = 3000 + i
        raw["content"] += f"&lt;p&gt;Beta team {i}.&lt;/p&gt;"
    http, _ = fake_http({URL: flow_body, greenhouse.listing_url("beta"): beta_body})

    def run(claude, **options):
        return pipeline.run(EXAMPLE_CONFIG, settings, [FLOW, beta], State(tmp_path / "state"),
                            pipeline.Options(**options), make_http=lambda: http, runner=claude)
    run(FakeClaude(), score=False)
    state = State(tmp_path / "state")
    now = "2026-10-27T12:00:00+00:00"
    assert pipeline.mark_company(state, "Flow Traders", "sonnet", now, ["sonnet", "haiku"])["eligible already"] == 2
    pipeline.mark_company(state, "Beta", "cascade", now, ["sonnet", "haiku"])
    state.save()

    scheduled = by_model({"sonnet": {"Graduate": 8, "Data": 6, "Zeta": 7, "Omega": 5},
                          "haiku": {"Graduate": 7, "Data": 5, "Zeta": 7, "Omega": 4}})
    run(scheduled)
    called = sorted(c["stdin"].split("Title: ")[1].split("\n")[0] for c in scheduled.calls)
    assert called == ["Data Intern", "Graduate Engineer"]  # Beta's cascade roles wait for a backlog run

    backlog = by_model({"sonnet": {"Zeta": 7, "Omega": 5}, "haiku": {"Zeta": 7, "Omega": 4}})
    summary = run(backlog, backlog=True, only=frozenset({"beta"}))
    assert sorted(models_called(backlog)) == ["haiku", "haiku", "sonnet"]  # Haiku on both, Sonnet on the 7
    assert summary["scoring"]["backlog"]["calls"] == 2 and summary["scoring"]["rescore"]["calls"] == 1


def test_the_weekly_warning_can_be_ignored_for_one_run_only(tmp_path, settings):
    from jobhunt import scoring
    weekly = {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.82}
    five_hour = {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.9}

    default = scoring.UsageGuard(stop_on_warning=True)
    default.observe(weekly)
    assert default.stopped == "Claude warned the weekly limit is 82% used"  # the default: stop
    ignoring = scoring.UsageGuard(stop_on_warning=True, ignore_weekly_warning=True)
    ignoring.observe(weekly)
    assert ignoring.stopped is None and ignoring.weekly_warning_ignored == 0.82
    ignoring.observe(five_hour)
    assert ignoring.stopped == "Claude warned the window is 90% used"  # a five-hour warning still stops
    refused = scoring.UsageGuard(stop_on_warning=True, ignore_weekly_warning=True)
    refused.observe({"status": "rejected", "rateLimitType": "seven_day"})
    assert refused.stopped.startswith("Claude refused")
    budget = scoring.UsageGuard(max_share=0.1, share_per_call={"sonnet": 0.06}, stop_on_warning=True,
                                ignore_weekly_warning=True)
    assert budget.allow("sonnet") and not budget.allow("sonnet") and "budget" in budget.stopped

    def warned(message, argv):
        return claude_stream(GOOD_SCORE, limit_status="allowed_warning", limit_type="seven_day", utilization=0.82)
    summary, _, claude = do_run(tmp_path / "a", settings, four_roles(), claude=FakeClaude(respond_argv=warned),
                                parallel=1)
    assert len(claude.calls) == 1 and "weekly limit is 82%" in summary["scoring"]["scheduled"]["stopped"]
    summary, _, claude = do_run(tmp_path / "b", settings, four_roles(), claude=FakeClaude(respond_argv=warned),
                                parallel=1, ignore_weekly_warning=True)
    assert len(claude.calls) == 4 and summary["scoring"]["scheduled"]["stopped"] is None
    assert "82% used; scoring went on" in summary["weekly_warning_ignored"]


def test_a_workday_role_without_a_country_gets_its_detail_page_before_the_title_check():
    rules = pipeline.GeneratedRules()
    base = {"key": "workday:x:1", "company": "https://x.wd1.myworkdayjobs.com/Ext", "title": "Graduate Engineer",
            "source": "workday", "origin": "generated", "prefilter": "pass", "closed": False}
    assert pipeline.place_unknown({**base, "locations": ["Main Campus"]})
    assert pipeline.place_unknown({**base, "locations": []})
    assert not pipeline.place_unknown({**base, "locations": ["Berlin"]})
    assert not pipeline.place_unknown({**base, "source": "greenhouse", "locations": ["Main Campus"]})
    # Without its text the title check waits (the detail names the country, and US-only roles are
    # parked); with the text, a role whose page names no country either is checked as usual.
    assert not pipeline.needs_title_check({**base, "locations": ["Main Campus"]}, rules)
    assert pipeline.needs_title_check({**base, "locations": ["Main Campus"], "description_hash": "h"}, rules)
    assert pipeline.needs_title_check({**base, "locations": ["Berlin"]}, rules)


def test_queued_rescores_pick_matching_and_high_fit_roles_and_clear_once_current(tmp_path):
    import re
    from types import SimpleNamespace

    state = State(tmp_path)
    settings = {"scoring": {"model": "sonnet", "backlog_model": "haiku"}}
    for key, fit, text in (("a", 7, "Solid match."), ("b", 4, "Graduation in July clashes."), ("c", 2, "Graduation in July."),
                           ("d", 3, "Wrong stack.")):
        state.jobs[key] = {"key": key, "company": "Acme", "title": "Engineer", "closed": False, "prefilter": "pass",
                           "description_hash": "h", "origin": "config", "first_seen": "2026-10-01T00:00:00+00:00"}
        state.scores[f"{key}@sonnet"] = {"key": key, "model": "sonnet", "prompt_version": "old", "description_hash": "h",
                                         "result": {"fit": fit, "explanation": text, "blockers": [], "gaps": []}}
    counts = pipeline.queue_rescores(state, settings, "now", re.compile("graduation", re.I), 4, 6)
    assert counts == {"sonnet": 2} and {k for k, r in state.jobs.items() if r.get("rescore_requested")} == {"a", "b"}
    prompt = SimpleNamespace(model="sonnet", version="new")
    assert {k for k, r in state.jobs.items() if pipeline.requested_rescore(state, prompt)(r)} == {"a", "b"}
    state.scores["a@sonnet"]["prompt_version"] = "new"  # rescored
    assert pipeline.clear_done_rescores(state, [prompt], "later") == 1
    assert "rescore_requested" not in state.jobs["a"] and state.jobs["b"]["rescore_requested"]["model"] == "sonnet"


def test_a_scheduled_run_rescores_queued_roles(tmp_path, settings):
    # The real Prompt, not a stand-in: on 2026-10-30 every scheduled run with a queued rescore crashed
    # before pushing (a Prompt holds its schema dict, so it cannot be a dict key).
    do_run(tmp_path, settings, four_roles())
    state = State(tmp_path / "state")
    key = next(iter(state.jobs))
    state.scores[f"{key}@sonnet"]["prompt_version"] = "old"
    state.jobs[key]["rescore_requested"] = {"model": "sonnet", "at": "now"}
    state.save()
    summary, state, claude = do_run(tmp_path, settings, four_roles())
    assert summary["scoring"]["requested rescore sonnet"]["calls"] == 1 and len(claude.calls) == 1
    assert "rescore_requested" not in state.jobs[key]


def test_main_countries_widen_the_backlog_rule_for_graduate_roles():
    from datetime import date

    rules = pipeline.GeneratedRules(main_countries=frozenset({"NL", "DE"}), backfill_days_main=60)
    today = date(2026, 10, 29)
    # 40 days old: in a main country yes, elsewhere no (20 days); without graduate wording, never.
    assert pipeline.backlog_eligible("Graduate Engineer", "2026-09-19", today, rules, ["Amsterdam, Netherlands"])
    assert not pipeline.backlog_eligible("Graduate Engineer", "2026-09-19", today, rules, ["Madrid, Spain"])
    assert not pipeline.backlog_eligible("Software Engineer", "2026-10-01", today, rules, ["Berlin, Germany"])
    # Workday's "Posted 30+ Days Ago" counts in a main country, not elsewhere (also when stored without a date).
    assert pipeline.backlog_eligible("Werkstudent Software", "2026-09-29", today, rules, ["Berlin"], posted_before=True)
    assert not pipeline.backlog_eligible("Graduate Engineer", None, today, rules, ["Madrid, Spain"], posted_before=True)
    assert pipeline.backlog_eligible("Graduate Engineer", None, today, rules, ["Madrid, Spain"])  # truly undated


@pytest.mark.parametrize("title", ["Afstudeerstage Data Engineering", "Werkstudent:in Software (m/w/d)",
                                   "Alternance – Développeur Full Stack H/F", "Tirocinio Software Developer",
                                   "Starter Software Engineer", "Einsteiger IT-Consulting"])
def test_local_entry_level_words_count_as_graduate_wording(title):
    from jobhunt import prefilter

    assert prefilter.entry_terms(title)
