"""The generated company list in runs: slots, parking, the 7-day backlog rule, scoring stages,
and the weekly guard."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from jobhunt import config as cfg
from jobhunt import generated, pipeline, prefilter
from jobhunt.companies import Company
from jobhunt.sources import greenhouse
from jobhunt.state import State, write_jsonl

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE, FakeClaude, claude_stream, fake_http, fixture_text

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
ACME = Company(name="Acme", board="greenhouse", slug="acme", origin="generated", identity="greenhouse:acme")


@pytest.fixture
def settings():
    return cfg.load_settings(EXAMPLE_CONFIG)


def gh_job(job_id: int, title: str, location: str, published: str, content: bool = False) -> dict:
    job = {"id": job_id, "title": title, "location": {"name": location}, "first_published": f"{published}T09:00:00Z",
           "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}", "company_name": "Acme"}
    if content:
        job.update(content=f"&lt;p&gt;{title}: Python and Go services. Team {job_id}.&lt;/p&gt;", departments=[],
                   offices=[{"location": location}])
    return job


LISTING = [(1, "Graduate Engineer", "Amsterdam", "2026-10-24"),   # recent: scored
           (2, "Mobile Engineer", "Berlin", "2026-10-23"),         # recent: dropped by the title check
           (3, "Data Engineer", "London", "2026-10-24"),           # UK only: parked
           (4, "Backend Engineer", "Paris", "2026-09-01")]         # older than 7 days at first fetch


def routes(listing=LISTING) -> dict:
    out = {greenhouse.listing_url("acme", content=False): {"jobs": [gh_job(*j) for j in listing], "meta": {}}}
    for j in listing:
        out[greenhouse.JOB.format(slug="acme", id=j[0])] = gh_job(*j, content=True)
    return out


def answer(message: str, argv: list[str]) -> str:
    if "profile_quote" in argv[argv.index("--json-schema") + 1]:  # the title check
        ids = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. Mobile", message, re.M)]
        return claude_stream({"drop": [{"id": i, "reason": "Mobile role",
                                        "profile_quote": "Not interested in: mobile, game development"} for i in ids]})
    model = argv[argv.index("--model") + 1]
    return claude_stream({**GOOD_SCORE, "fit": 7 if model == "haiku" else 8})


def run(tmp_path, settings, claude=None, listing=LISTING, companies=(ACME,), now=NOW):
    http, session = fake_http(routes(listing))
    claude = claude or FakeClaude(respond_argv=answer)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, list(companies), State(tmp_path / "state"), pipeline.Options(),
                           make_http=lambda: http, runner=claude, now=now)
    return summary, State(tmp_path / "state"), claude, session


def by_title(state: State) -> dict[str, dict]:
    return {r["title"]: r for r in state.jobs.values()}


def test_load_skips_configured_and_excluded_boards(tmp_path, settings):
    write_jsonl(tmp_path / generated.FILE, [
        {"identity": "greenhouse:acme", "board": "greenhouse", "slug": "acme", "name": "Acme"},
        {"identity": "greenhouse:stripe", "board": "greenhouse", "slug": "stripe"},
        {"identity": "workday:https://x.wd1.myworkdayjobs.com/site", "board": "workday",
         "url": "https://x.wd1.myworkdayjobs.com/site"}])
    loaded = generated.load(tmp_path, settings, configured={"greenhouse:stripe"})
    assert [(c.name, c.origin, c.identity) for c in loaded] == [("Acme", "generated", "greenhouse:acme")]


def test_each_company_has_exactly_one_slot_a_day():
    companies = [Company(name=f"c{i}", identity=f"greenhouse:c{i}", origin="generated") for i in range(500)]
    slots = [generated.due(companies, s) for s in range(generated.SLOTS_PER_DAY)]
    assert sum(len(s) for s in slots) == 500 and all(len(s) > 20 for s in slots)
    assert generated.current_slot(datetime(2026, 10, 25, 23, 59)) == 11 and generated.current_slot(NOW) == 4


def test_generated_roles_go_through_title_check_haiku_then_sonnet(tmp_path, settings):
    summary, state, claude, session = run(tmp_path, settings)
    jobs = by_title(state)
    # Details fetched only for the two recent roles outside the UK and US.
    detail_urls = [u for u in session.urls() if re.search(r"/jobs/\d+$", u)]
    assert sorted(detail_urls) == [greenhouse.JOB.format(slug="acme", id=i) for i in (1, 2)]
    assert jobs["Data Engineer"]["parked"] and jobs["Backend Engineer"]["backfill_skip"]
    phases = summary["scoring"]
    assert phases["generated titlecheck"]["dropped"] == 1
    assert phases["generated"]["calls"] == 1 and phases["generated rescore"]["calls"] == 1
    assert set(state.scores_for(jobs["Graduate Engineer"]["key"])) == {"haiku", "sonnet"}
    assert jobs["Mobile Engineer"]["titlecheck"]["dropped"]
    drops = [json.loads(line) for line in (tmp_path / "state" / "drops.jsonl").read_text().splitlines()]
    assert [d["title"] for d in drops] == ["Mobile Engineer"] and drops[0]["quote"].startswith("Not interested")
    assert summary["generated"]["greenhouse"]["parked"] == 1


def test_a_later_run_scores_new_roles_of_any_age_and_nothing_twice(tmp_path, settings):
    run(tmp_path, settings)
    later = LISTING + [(5, "Platform Engineer", "Madrid", "2026-09-15")]  # old date, but new to the tool
    summary, state, claude, _ = run(tmp_path, settings, listing=later)
    assert not by_title(state)["Platform Engineer"]["backfill_skip"]
    assert summary["scoring"]["generated titlecheck"]["checked"] == 1  # only the new role
    assert summary["scoring"]["generated"]["calls"] == 1


def test_parked_roles_are_scored_when_switched_on(tmp_path, settings):
    settings = {**settings, "generated": {**settings["generated"], "score_parked": True}}
    _, state, _, _ = run(tmp_path, settings)
    assert set(state.scores_for(by_title(state)["Data Engineer"]["key"])) == {"haiku", "sonnet"}


def test_configured_companies_are_scored_by_sonnet_directly(tmp_path, settings):
    flow = Company(name="Acme", board="greenhouse", slug="acme")  # same board, but from companies.toml
    http, _ = fake_http({greenhouse.listing_url("acme"): {"jobs": [gh_job(1, "Graduate Engineer", "Amsterdam",
                                                                          "2026-10-24", content=True)]}})
    claude = FakeClaude(respond_argv=answer)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [flow], State(tmp_path / "s"), pipeline.Options(),
                           make_http=lambda: http, runner=claude, now=NOW)
    assert "generated titlecheck" not in summary["scoring"]
    assert [c["argv"][c["argv"].index("--model") + 1] for c in claude.calls] == ["sonnet"]


def write_runs(folder, used_per_run: list[float], when: str = "2026-10-24T09:00:00+00:00", filter_on=None,
               kind: str = "scheduled"):
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for used in used_per_run:
        run_summary = {"started_at": when, "kind": kind, "scoring": {"scheduled": {"window_share_spent": used}}}
        if filter_on is not None:
            run_summary["weekly_guard"] = {"filter_on": filter_on}
        lines.append(json.dumps(run_summary))
    (folder / "runs.jsonl").write_text("\n".join(lines) + "\n")


def test_weekly_guard_switches_the_filter_on_and_off_with_a_gap(tmp_path, settings):
    write_runs(tmp_path / "state", [3.8, 3.8])  # 7.6 of 12.5 windows: 61%
    summary, state, _, _ = run(tmp_path, settings)
    guard = summary["weekly_guard"]
    assert guard["filter_on"] and guard["changed"] and guard["share_of_week"] == pytest.approx(0.608, abs=0.01)
    # A company's first fetch is backlog: the guard's filter leaves it alone, so the role without
    # entry-level wording ("Mobile Engineer") is screened too.
    assert summary["scoring"]["generated titlecheck"]["pending"] == 2
    # Daily new roles later: only the one with entry-level wording goes on.
    later = LISTING + [(7, "Platform Engineer", "Madrid", "2026-10-25"), (8, "Graduate Platform Engineer", "Lyon", "2026-10-25")]
    summary, state, _, _ = run(tmp_path, settings, listing=later)
    assert summary["weekly_guard"]["filter_on"]
    assert summary["scoring"]["generated titlecheck"]["pending"] == 1
    assert not by_title(state)["Platform Engineer"].get("titlecheck")

    write_runs(tmp_path / "s2", [7.0], filter_on=True)  # 56%: between the thresholds, stays on
    assert pipeline.weekly_guard(State(tmp_path / "s2"), settings, NOW)["filter_on"]
    write_runs(tmp_path / "s3", [6.0], filter_on=True)  # 48%: off again
    off = pipeline.weekly_guard(State(tmp_path / "s3"), settings, NOW)
    assert not off["filter_on"] and off["changed"]
    write_runs(tmp_path / "s4", [9.0], when="2026-10-10T09:00:00+00:00")  # older than 7 days: not counted
    write_runs(tmp_path / "s5", [9.0], kind="backfill")  # 72% of the week, all backlog: the guard stays off
    backlog = pipeline.weekly_guard(State(tmp_path / "s5"), settings, NOW)
    assert not backlog["filter_on"] and backlog["share_of_week"] == pytest.approx(0.72) and backlog["scheduled_share"] == 0
    assert pipeline.weekly_guard(State(tmp_path / "s4"), settings, NOW)["used_windows"] == 0


def test_the_tools_share_covers_the_same_week_as_claudes_figure(tmp_path, settings):
    # Runs on 24 Oct (5 windows) fall in the rolling 7 days but before Claude's week began (resets
    # 31 Oct, so it began 24 Oct 12:00); only the 25 Oct run (1.25 windows) counts towards its figure.
    folder = tmp_path / "state"
    write_runs(folder, [5.0])
    lines = (folder / "runs.jsonl").read_text()
    write_runs(folder, [1.25], when="2026-10-25T09:00:00+00:00")
    (folder / "runs.jsonl").write_text(lines + (folder / "runs.jsonl").read_text())
    claude = {"utilization": 0.3, "limit": "weekly", "resets_at": "2026-10-31T12:00:00+00:00"}
    guard = pipeline.weekly_guard(State(folder), settings, NOW, claude=claude)
    assert guard["share_of_week"] == pytest.approx(0.5) and guard["tool_claude_week"] == pytest.approx(0.1)
    # Once Claude's week has reset, only runs since the reset count.
    after = pipeline.weekly_guard(State(folder), settings, NOW,
                                  claude={**claude, "resets_at": "2026-10-25T00:00:00+00:00"})
    assert after["tool_claude_week"] == pytest.approx(0.1)
    assert pipeline.weekly_guard(State(folder), settings, NOW)["tool_claude_week"] is None


def test_phase_usage_is_counted_once(tmp_path, settings):
    summary, _, _, _ = run(tmp_path, settings)
    spent = [p["window_share_spent"] for p in summary["scoring"].values()]
    # titlecheck 0.0025 + haiku 0.0012 + sonnet 0.004 (+ nothing for the empty configured phase)
    assert sum(spent) == pytest.approx(0.0025 + 0.0012 + 0.004)


def test_backlog_rule_at_a_first_fetch(tmp_path, settings):
    listing = [(1, "Graduate Engineer", "Amsterdam", "2026-10-07"),   # entry-level, 18 days: scored
               (2, "Junior Developer", "Berlin", "2026-10-01"),       # entry-level, 24 days: not
               (3, "Backend Engineer", "Paris", "2026-10-13"),        # other, 12 days: scored
               (4, "Platform Engineer", "Madrid", "2026-10-05")]      # other, 20 days: not
    _, state, _, session = run(tmp_path, settings, listing=listing)
    skipped = {r["title"]: r["backfill_skip"] for r in state.jobs.values()}
    assert skipped == {"Graduate Engineer": False, "Junior Developer": True, "Backend Engineer": False,
                       "Platform Engineer": True}
    details = sorted(u for u in session.urls() if re.search(r"/jobs/\d+$", u))
    assert details == [greenhouse.JOB.format(slug="acme", id=i) for i in (1, 3)]


@pytest.mark.parametrize("title, posted, eligible", [
    ("Graduate Engineer", None, True), ("Backend Engineer", None, False),
    ("Graduate Engineer", "2026-10-05", True), ("Graduate Engineer", "2026-10-04", False),
    ("Backend Engineer", "2026-10-11", True), ("Backend Engineer", "2026-10-10", False),
])
def test_backlog_eligible(settings, title, posted, eligible):
    from datetime import date
    rules = pipeline.GeneratedRules.from_settings(settings)
    assert pipeline.backlog_eligible(title, posted, date(2026, 10, 25), rules) is eligible


def test_backlog_plan_and_apply(tmp_path, settings):
    from datetime import date
    listing = [(1, "Graduate Engineer", "Amsterdam", "2026-09-01"), (2, "Backend Engineer", "Paris", "2026-09-01")]
    run(tmp_path, settings, listing=listing)  # first fetch: both too old, skipped
    state = State(tmp_path / "state")
    plan = pipeline.backlog_plan(state, settings, date(2026, 9, 10))  # as if run 9 days after posting
    assert plan["parts"]["entry-level"]["roles"] == 1 and plan["parts"]["other"]["roles"] == 1
    assert plan["parts"]["other"]["title_check_keeps"] == 1 and plan["parts"]["other"]["windows"] > 0
    pipeline.apply_backlog(state, plan["records"]["entry-level"], "now")
    assert {r["title"]: r["backfill_skip"] for r in state.jobs.values()} == {"Graduate Engineer": False,
                                                                          "Backend Engineer": True}

def test_generated_companies_store_only_passing_roles(tmp_path, settings):
    listing = LISTING + [(6, "Senior Engineer", "Amsterdam", "2026-10-24")]
    summary, state, _, _ = run(tmp_path, settings, listing=listing)
    assert "Senior Engineer" not in by_title(state)
    assert summary["generated"]["greenhouse"]["dropped title"] == 1 and summary["generated"]["greenhouse"]["fetched"] == 5


def test_budget_overrides_the_run_share(tmp_path, settings):
    http, _ = fake_http(routes())
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "state"),
                           pipeline.Options(budget=0.003), make_http=lambda: http,
                           runner=FakeClaude(respond_argv=answer), now=NOW)
    # The title check (0.0025) fits in 0.003; the first Haiku call (0.0012) would not.
    assert summary["scoring"]["generated"]["calls"] == 0 and "budget" in summary["scoring"]["generated"]["stopped"]


def test_weekly_guard_reads_older_run_records(tmp_path, settings):
    """Before phases existed, runs stored flat scoring stats; some runs have no scoring at all."""
    folder = tmp_path / "state"
    folder.mkdir()
    old = [{"started_at": "2026-10-24T15:22:07+00:00"},
           {"started_at": "2026-10-24T15:26:05+00:00", "scoring": {"calls": 8, "cost_usd": 0.5, "deferred": 991,
                                                                   "errors": {}, "rate_limit": {"allowed": 8}}},
           {"started_at": "2026-10-24T18:03:44+00:00", "scoring": {"backlog": {"calls": 606,
                                                                               "window_share_spent": 0.4}}},
           {"started_at": "2026-10-24T19:00:00+00:00", "scoring": None, "weekly_guard": None}]
    (folder / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))
    guard = pipeline.weekly_guard(State(folder), settings, NOW)
    # 8 flat-format (Haiku) calls estimated at 0.12% each, plus the recorded 0.4
    assert guard["used_windows"] == round(0.4 + 8 * 0.0012, 2) and not guard["filter_on"]
    assert pipeline.run_phases(old[1]) == [old[1]["scoring"]] and pipeline.run_phases(old[0]) == []
    summary, _, _, _ = run(tmp_path, settings)  # a whole run on top of the old records works
    assert summary["weekly_guard"]["used_windows"] == round(0.4 + 8 * 0.0012, 2)


def test_phase_usage_estimates_older_phases(settings):
    share = settings["usage"]["share_per_call"]
    assert pipeline.phase_usage({"model": "sonnet", "calls": 50}, share) == pytest.approx(0.2)
    assert pipeline.phase_usage({"model": "haiku", "calls": 605}, share) == pytest.approx(0.726)
    assert pipeline.phase_usage({"titlecheck": True, "calls": 4}, share) == pytest.approx(0.01)
    assert pipeline.phase_usage({"model": "sonnet", "calls": 50, "window_share_spent": 0.1}, share) == 0.1


def test_a_companys_fetch_slot_matches_its_jobs_file():
    from jobhunt.state import company_slot, shard
    acme = Company(name="Acme", board="greenhouse", origin="generated", identity="greenhouse:acme")
    slot = generated.slot(acme)
    assert slot == company_slot("greenhouse", "Acme")
    record = {"origin": "generated", "source": "greenhouse", "company": "Acme"}
    assert shard(record) == f"jobs/generated/{slot:02d}.jsonl"


def test_a_backlog_run_does_not_start_above_the_hard_stop(tmp_path, settings):
    write_runs(tmp_path / "state", [4.0, 3.6])  # 7.6 of 12.5 windows: 61%
    http, session = fake_http(routes())
    claude = FakeClaude(respond_argv=answer)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "state"), pipeline.Options(backlog=True),
                           make_http=lambda: http, runner=claude, now=NOW)
    assert "61%" in summary["backlog_stopped"] and session.calls == [] and claude.calls == []
    # A scheduled run still goes ahead.
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "state"), pipeline.Options(),
                           make_http=lambda: http, runner=claude, now=NOW)
    assert "backlog_stopped" not in summary and session.calls


def test_visa_blocks_become_checks(tmp_path, settings):
    from jobhunt import scoring
    on = {**settings, "scoring": {**settings["scoring"], "visa_never_blocks": True}}
    visa = {"type": "visa", "severity": "blocking", "detail": "No sponsorship", "min_years": None}

    def blocked_by_visa(message, argv):
        if "profile_quote" in argv[argv.index("--json-schema") + 1]:
            return claude_stream({"drop": []})
        return claude_stream({**GOOD_SCORE, "fit": 8, "blockers": [visa]})
    _, state, _, _ = run(tmp_path, on, claude=FakeClaude(respond_argv=blocked_by_visa))
    stored = state.scores_for(by_title(state)["Graduate Engineer"]["key"])
    assert all(s["status"] == "check" and s["result"]["blockers"][0]["severity"] == "check" for s in stored.values())
    # Scores stored before the setting are corrected at the start of the next run.
    off_folder = tmp_path / "off"
    run(off_folder, settings, claude=FakeClaude(respond_argv=blocked_by_visa))
    before = State(off_folder / "state")
    assert any(s["status"] == "blocked" for s in before.scores.values())
    assert pipeline.apply_score_policies(before, on) > 0
    assert all(s["status"] != "blocked" for s in before.scores.values() if s.get("result"))


def test_roles_left_unscored_never_have_their_text_fetched_again(tmp_path, settings):
    run(tmp_path, settings)  # first fetch: Backend Engineer (old) is skipped by the backlog rule
    _, state, _, session = run(tmp_path, settings)  # the next daily fetch
    assert not by_title(state)["Backend Engineer"].get("description_hash")
    assert not [u for u in session.urls() if re.search(r"/jobs/\d+$", u)]  # no detail page fetched at all


def test_scheduled_run_limits_on_posting_texts_and_fetch_time(tmp_path, settings):
    http, session = fake_http(routes())
    claude = FakeClaude(respond_argv=answer)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "a"), pipeline.Options(max_details=1),
                           make_http=lambda: http, runner=claude, now=NOW)
    assert summary["details_fetched"] == 1 and summary["details_deferred"] == 1
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "b"),
                           pipeline.Options(fetch_seconds=-1), make_http=lambda: http, runner=claude, now=NOW)
    assert summary["fetch_not_started"] == 1 and summary["companies"] == 1


def test_an_explicit_budget_is_the_only_limit_on_calls(settings):
    import argparse
    import sys

    from jobhunt import cli

    def args(**given):
        return argparse.Namespace(**{"cap": None, "budget": None, "backfill": False, "sonnet_only": False, **given})
    assert cli.run_cap(args(), settings) is None  # max_calls_per_run from settings
    assert cli.run_cap(args(budget=0.5), settings) == sys.maxsize
    assert cli.run_cap(args(budget=0.5, cap=20), settings) == 20
    assert cli.run_cap(args(backfill=True), settings) == settings["scoring"]["backfill_max_calls"]


def test_status_counts_what_waits_at_each_step(tmp_path, settings):
    def no_scores(message, argv):  # the title check answers; scoring calls fail on the output
        if "profile_quote" in argv[argv.index("--json-schema") + 1]:
            return answer(message, argv)
        return claude_stream({"fit": "not a number"})
    run(tmp_path, settings, claude=FakeClaude(respond_argv=no_scores))
    found = pipeline.status(State(tmp_path / "state"), settings, NOW)
    generated_part = found["parts"]["generated list"]
    # Graduate Engineer: checked, not scored yet. Mobile: dropped. Data (UK): parked. Backend: skipped.
    assert generated_part["waiting for a first score"] == 1 and generated_part["backlog total"] == 1
    assert "waiting for the title check" not in generated_part and found["weekly_guard"]["used_windows"] >= 0
    _, state, _, _ = run(tmp_path, settings)  # scored now: Haiku 7, so Sonnet too
    done = pipeline.status(state, settings, NOW)["parts"]["generated list"]
    assert done["backlog done"] == 1 and "waiting for a first score" not in done


def test_uk_roles_with_entry_level_wording_are_scored_and_the_us_stays_parked(settings):
    rules = pipeline.GeneratedRules.from_settings({**settings, "generated": {**settings["generated"],
                                                                            "unpark_entry_level": ["GB"]}})
    assert not rules.parks("Graduate Engineer", ["London, United Kingdom"])
    assert rules.parks("Data Engineer", ["London, United Kingdom"])  # no entry-level wording
    assert rules.parks("Graduate Engineer", ["New York, NY, United States"])
    assert rules.parks("Graduate Engineer", ["London, United Kingdom", "Boston, MA, United States"])


def test_generated_workday_sites_are_searched_a_few_pages_per_word(tmp_path, settings):
    from jobhunt.sources import workday
    write_jsonl(tmp_path / generated.FILE, [{"identity": "workday:https://x.wd1.myworkdayjobs.com/site",
                                             "board": "workday", "url": "https://x.wd1.myworkdayjobs.com/site"}])
    searched = {**settings, "generated": {**settings["generated"], "exclude_boards": []}}
    (company,) = generated.load(tmp_path, searched, configured=set())
    assert company.search == ("graduate", "intern", "junior") and company.search_pages == 2
    page = {"total": 100, "jobPostings": [{"title": f"Intern {i}", "externalPath": f"/job/x/Intern_{i}",
                                           "bulletFields": [f"R{i}"], "postedOn": "Posted 30+ Days Ago"} for i in range(20)]}
    http, session = fake_http({"https://x.wd1.myworkdayjobs.com/wday/cxs/x/site/jobs": page})
    jobs = workday.fetch(company, http, details=lambda job: False)
    assert len(session.calls) == 6  # 3 words, 2 pages each, although 100 match
    assert jobs[0].posted is None and jobs[0].posted_before  # "30+ days": undated, but known to be old
    assert not pipeline.backlog_eligible("Graduate Intern", jobs[0].posted or jobs[0].posted_before,
                                         datetime.now(UTC).date(), pipeline.GeneratedRules.from_settings(settings))


def test_a_searched_listing_closes_a_role_only_after_21_days_unseen(tmp_path, settings):
    from datetime import date, timedelta

    from jobhunt.sources.base import Job
    company = Company(name="Acme", board="workday", url="https://x.wd1.myworkdayjobs.com/site", search=("intern",))
    state = State(tmp_path / "s")
    today = date(2026, 10, 27)
    for key, seen in (("workday:Acme:1", today - timedelta(days=10)), ("workday:Acme:2", today - timedelta(days=30))):
        state.jobs[key] = {"key": key, "company": "Acme", "title": "Intern", "url": "u", "source": "workday",
                           "source_id": key[-1], "locations": [], "prefilter": "pass", "first_seen": "2026-09-01",
                           "closed": False, "last_seen": seen.isoformat(), "updated_at": "x", "account": company.account}
    fetched = pipeline.Fetched(company, [Job(company="Acme", title="Intern", url="u3", source="workday", source_id="3")])
    pipeline.update_jobs(state, fetched, set(), "2026-10-27T09:00:00+00:00")
    assert state.jobs["workday:Acme:2"]["closed"] and not state.jobs["workday:Acme:1"]["closed"]


def test_a_large_backlog_lets_a_scheduled_run_use_more_of_a_window(tmp_path, settings):
    """2026-10-28: above `scheduled_backlog_above` roles left, 20% of a window instead of 10%
    (since 2026-11-18 every waiting role counts, not only a first fetch's backlog)."""
    failing = FakeClaude(respond=lambda m: claude_stream(None))
    run(tmp_path, settings, claude=failing)  # Acme's first fetch: its roles are backlog, none scored yet
    left = pipeline.waiting_left(State(tmp_path / "state"), settings)
    assert left >= 1
    roomy = {**settings, "usage": {**settings["usage"], "scheduled_backlog_above": left - 1},
             "scoring": {**settings["scoring"], "parallel_backlog": 8, "max_calls_per_run_backlog": 400}}
    summary, _, _, _ = run(tmp_path, roomy, claude=failing)
    # 2026-10-31: also 8 calls at once and 400 a phase (the 14-minute limit, not the budget, ended runs).
    assert summary["backlog_boost"] == {"waiting_left": left, "share": 0.2, "parallel": 8, "cap": 400}
    assert {p["parallel"] for p in summary["scoring"].values() if "parallel" in p} == {8}
    quiet = {**settings, "usage": {**settings["usage"], "scheduled_backlog_above": left}}
    summary, _, _, _ = run(tmp_path, quiet, claude=failing)
    assert "backlog_boost" not in summary


def test_sonnet_takes_earlier_haiku_6_plus_before_new_haiku_scoring(tmp_path, settings):
    # 2026-10-31: the time limit stopped every run in the Haiku phase, so Haiku 6+ never reached Sonnet.
    def haiku_only(message, argv):
        return claude_stream(None) if argv[argv.index("--model") + 1] == "sonnet" else answer(message, argv)
    run(tmp_path, settings, claude=FakeClaude(respond_argv=haiku_only))  # Sonnet unavailable: Haiku 7 waits
    later = LISTING + [(5, "Graduate Data Engineer", "Lyon", "2026-10-25")]
    claude = FakeClaude(respond_argv=answer)
    summary, _, _, _ = run(tmp_path, settings, claude=claude, listing=later)
    order = [k for k in summary["scoring"] if k.startswith("generated") and summary["scoring"][k].get("calls")]
    assert order == ["generated titlecheck", "generated rescore first", "generated", "generated rescore"]
    assert summary["scoring"]["generated rescore first"]["calls"] == 1


def test_a_company_can_park_its_us_only_roles(tmp_path):
    from jobhunt.sources.base import Job
    apple = Company(name="Apple", board="custom", listing_url="https://jobs.example/search", link_pattern="/details/",
                    park_countries=("US",))
    state = State(tmp_path / "s")
    jobs = [Job(company="Apple", title="Java Engineer", url=f"u{i}", source="custom", source_id=str(i),
                locations=places, description="Text.")
            for i, places in enumerate([["Cupertino, California, United States"], ["Cork, County Cork, Ireland"],
                                        ["Austin, Texas, United States", "Netherlands"], []])]
    pipeline.update_jobs(state, pipeline.Fetched(apple, jobs), {"US", "IE", "NL"}, "2026-10-31T09:00:00+00:00")
    parked = {r["source_id"]: r["parked"] for r in state.jobs.values()}
    assert parked == {"0": True, "1": False, "2": False, "3": False}  # US only; a place unknown is scored
    assert [r["source_id"] for r in state.jobs.values() if pipeline.configured(r)] == ["1", "2", "3"]
    rules = pipeline.GeneratedRules()
    assert not pipeline.could_be_scored(state.jobs["custom:Apple:0"], rules)


def test_apples_us_only_roles_are_parked_without_fetching_their_pages(tmp_path, settings):
    search = "https://jobs.apple.com/en-us/search?location=germany-DEU&page={page}"
    pages = {"https://jobs.apple.com/robots.txt": "", search.format(page=1): fixture_text("apple_search.html"),
             "https://jobs.apple.com/en-us/details/200682946/mechanical-engineer?team=CORSV": fixture_text("apple_job.html")}
    http, session = fake_http(pages)
    apple = Company(name="Apple", board="apple", listing_url=search, park_countries=("US",))
    pipeline.run(EXAMPLE_CONFIG, settings, [apple], State(tmp_path / "state"), pipeline.Options(score=False),
                 make_http=lambda: http, runner=FakeClaude(), now=NOW)
    jobs = {r["source_id"]: r for r in State(tmp_path / "state").jobs.values()}
    assert jobs["114438158"]["parked"] and not jobs["200682946"]["parked"]
    assert not any("/details/114438158/" in u for u in session.urls())  # a parked role's page is not read
    assert jobs["200682946"]["description_hash"]


def test_the_backlog_counts_only_roles_the_filters_will_score(tmp_path, settings):
    run(tmp_path, settings, claude=FakeClaude(respond=lambda m: claude_stream(None)))  # nothing gets scored
    state = State(tmp_path / "state")
    graduate_only = {**settings, "generated": {**settings["generated"], "entry_level_only": True}}
    rules = pipeline.GeneratedRules.from_settings(graduate_only)
    worded = [r for r in state.jobs.values() if r.get("backlog") and pipeline.could_be_scored(r, rules)]
    assert worded and all(prefilter.entry_terms(r["title"]) for r in worded)
    assert pipeline.backlog_left(state, graduate_only) == len(worded) < pipeline.backlog_left(state, settings)
    assert pipeline.status(state, graduate_only, NOW)["parts"]["generated list"]["backlog total"] == len(worded)


MINE = Company(name="Acme", board="greenhouse", slug="acme", origin="wishlist", wishlist=True,
               identity="greenhouse:acme")  # a wishlist company: mine, not the generated list


def models_called(claude) -> list[str]:
    out = []
    for call in claude.calls:
        argv = call["argv"]
        schema = argv[argv.index("--json-schema") + 1]
        out.append("titlecheck" if "profile_quote" in schema else argv[argv.index("--model") + 1])
    return out


def test_my_large_companies_go_through_the_cascade(tmp_path, settings):
    """2026-10-28: more than `cascade_above_open` open roles: title check, Haiku, Sonnet for 6+."""
    large = {**settings, "scoring": {**settings["scoring"], "cascade_above_open": 2, "score_while_fetching": False}}
    summary, state, claude, _ = run(tmp_path, large, companies=(MINE,))
    called = models_called(claude)
    assert summary["cascade_companies"] == ["acme"] and called[0] == "titlecheck"
    assert "haiku" in called and called.count("sonnet") == called.count("haiku")  # each Haiku 7 goes on to Sonnet
    assert by_title(state)["Mobile Engineer"]["titlecheck"]["dropped"]  # the title check applies to it too
    small = {**settings, "scoring": {**settings["scoring"], "cascade_above_open": 50}}
    summary, _, claude, _ = run(tmp_path / "small", small, companies=(MINE,))
    assert "cascade_companies" not in summary and set(models_called(claude)) == {"sonnet"}
    exempt = {**large, "scoring": {**large["scoring"], "cascade_exempt": ["Acme"]}}
    summary, _, claude, _ = run(tmp_path / "exempt", exempt, companies=(MINE,))
    assert set(models_called(claude)) == {"sonnet"}


def detail_urls(session) -> set[str]:
    return {u for u in session.urls() if re.search(r"/jobs/\d+$", u.split("?")[0])}


def test_no_text_for_roles_the_graduate_filter_will_never_score(tmp_path, settings):
    graduate_only = {**settings, "generated": {**settings["generated"], "entry_level_only": True}}
    _, state, _, session = run(tmp_path, graduate_only)
    fetched = detail_urls(session)
    assert greenhouse.JOB.format(slug="acme", id=1) in fetched  # Graduate Engineer
    assert greenhouse.JOB.format(slug="acme", id=2) not in fetched  # Mobile Engineer: never scored


def test_stored_roles_are_title_checked_before_their_text_is_fetched(tmp_path, settings):
    http, session = fake_http(routes())
    pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "state"),
                 pipeline.Options(score=False, max_details=0), make_http=lambda: http, runner=FakeClaude(), now=NOW)
    stored = State(tmp_path / "state")
    assert not any(r.get("description_hash") for r in stored.jobs.values())  # no texts yet
    later = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    http, session = fake_http(routes())
    claude = FakeClaude(respond_argv=answer)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [ACME], State(tmp_path / "state"), pipeline.Options(),
                           make_http=lambda: http, runner=claude, now=later)
    assert "before fetch: titlecheck" in summary["scoring"]
    fetched = detail_urls(session)
    assert greenhouse.JOB.format(slug="acme", id=2) not in fetched  # Mobile Engineer: dropped, so no text
    assert greenhouse.JOB.format(slug="acme", id=1) in fetched  # Graduate Engineer: passed, text fetched
    assert by_title(State(tmp_path / "state"))["Mobile Engineer"]["titlecheck"]["dropped"]


def test_a_busy_company_of_mine_goes_through_the_cascade_too(tmp_path, settings):
    state = State(tmp_path / "s")
    for i in range(80):  # 80 new roles in the last week: over 10 a day
        state.jobs[f"k{i}"] = {"key": f"k{i}", "company": "Busy", "source": "custom", "origin": "config",
                               "prefilter": "pass", "closed": i < 40, "first_seen": "2026-10-24T08:00:00+00:00"}
    rules = {**settings, "scoring": {**settings["scoring"], "cascade_above_open": 50, "cascade_above_new_per_day": 10}}
    assert pipeline.cascade_companies(state, rules, NOW) == {"busy"}  # 40 open, but 80 new
    calm = {**rules, "scoring": {**rules["scoring"], "cascade_above_new_per_day": 20}}
    assert pipeline.cascade_companies(state, calm, NOW) == frozenset()


def haiku_five(message: str, argv: list[str]) -> str:
    if "profile_quote" in argv[argv.index("--json-schema") + 1]:
        return claude_stream({"drop": []})
    return claude_stream({**GOOD_SCORE, "fit": 5 if argv[argv.index("--model") + 1] == "haiku" else 8})


def test_core_targets_go_on_to_sonnet_from_haiku_5(tmp_path, settings):
    base = {**settings["scoring"], "cascade_above_open": 2, "score_while_fetching": False, "cascade_core_min_fit": 5}
    core = {**settings, "scoring": {**base, "cascade_core": ["Acme"]}}
    _, _, claude, _ = run(tmp_path, core, claude=FakeClaude(respond_argv=haiku_five), companies=(MINE,))
    assert "sonnet" in models_called(claude)  # Haiku 5 is enough for a core target
    other = {**settings, "scoring": {**base, "cascade_core": []}}
    _, _, claude, _ = run(tmp_path / "other", other, claude=FakeClaude(respond_argv=haiku_five), companies=(MINE,))
    assert "sonnet" not in models_called(claude)  # elsewhere it takes 6


def test_workday_sites_in_the_main_countries_get_local_search_words(tmp_path):
    state = State(tmp_path / "s")
    for key, company, locations in (("workday:a:1", "https://a.wd3.myworkdayjobs.com/x", ["Paris, France"]),
                                    ("workday:a:2", "https://a.wd3.myworkdayjobs.com/x", ["Munich, Germany"]),
                                    ("workday:b:1", "https://b.wd3.myworkdayjobs.com/y", ["Austin, TX"])):
        state.jobs[key] = {"key": key, "company": company, "source": "workday", "origin": "generated",
                           "closed": False, "locations": locations}
    settings = {"generated": {"workday_search_local": {"FR": ["stage", "stagiaire"], "DE": ["werkstudent", "stage"]}}}
    words = pipeline.workday_local_search(state, settings)
    assert words == {"https://a.wd3.myworkdayjobs.com/x": ("werkstudent", "stage", "stagiaire")}
    assert Company.from_dict({"name": "a", "board": "workday", "local_search": ["stage"]}).local_search == ("stage",)


def test_your_companies_with_the_generated_rules_join_the_daily_slots(tmp_path):
    from jobhunt.companies import load_companies
    config = tmp_path / "config"
    config.mkdir()
    (config / "companies.toml").write_text('[[company]]\nname = "Mine"\nboard = "greenhouse"\nslug = "mine"\n\n'
                                           '[[company]]\nname = "Daily"\nboard = "greenhouse"\nslug = "daily"\nrules = "generated"\n')
    by_name = {c.name: c for c in load_companies(config)}
    assert by_name["Mine"].origin == "config" and by_name["Daily"].origin == "generated"


def test_daily_companies_are_fetched_in_their_slot_not_every_run(tmp_path, monkeypatch):
    import argparse

    from jobhunt import cli
    from jobhunt import config as cfg
    from jobhunt import generated as gen
    config = tmp_path / "config"
    config.mkdir()
    (config / "companies.toml").write_text('[[company]]\nname = "Mine"\nboard = "greenhouse"\nslug = "mine"\n\n'
                                           '[[company]]\nname = "Daily"\nboard = "greenhouse"\nslug = "daily"\nrules = "generated"\n')
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    paths = argparse.Namespace(config=config)
    args = argparse.Namespace(no_fetch=False, sonnet_only=False, names=[], local_only=False, generated="due")
    monkeypatch.setattr(gen, "due", lambda companies, *a, **k: [c for c in companies if c.name == "Daily"])
    names = [c.name for c in cli.run_targets(args, paths, {}, state_dir, say=lambda *_: None)]
    assert names.count("Mine") == 1 and names.count("Daily") == 1  # Daily comes from the slot, not every run
    monkeypatch.setattr(gen, "due", lambda companies, *a, **k: [])
    assert [c.name for c in cli.run_targets(args, paths, {}, state_dir, say=lambda *_: None)] == ["Mine"]


def test_workday_companies_of_mine_take_turns_across_runs():
    mine = [Company(name=f"W{i}", board="workday", url=f"https://w{i}.wd3.myworkdayjobs.com/x") for i in range(40)]
    others = [Company(name="Acme", board="greenhouse", slug="acme")]
    turns = [generated.spread(mine + others, slot, {"workday": 2}) for slot in range(4)]
    assert all(others[0] in t for t in turns)  # boards not listed: every run
    first, second = ({c.name for c in t if c.board == "workday"} for t in turns[:2])
    assert first and second and not first & second and len(first | second) == 40  # each tenant every 2nd run
    assert turns[0] == turns[2]


def test_new_roles_waiting_count_towards_the_larger_budget_too(tmp_path, settings):
    """2026-11-18: the generated list's restore brought thousands of new roles, none marked backlog."""
    state = State(tmp_path / "state")
    for i in range(3):
        key = f"greenhouse:acme:{i}"
        state.jobs[key] = {"key": key, "company": "Acme", "title": "Graduate Software Engineer", "url": "u",
                           "source": "greenhouse", "source_id": str(i), "locations": ["Paris, France"],
                           "prefilter": "pass", "origin": "generated", "first_seen": "2026-11-18", "closed": False,
                           "last_seen": "2026-11-18", "updated_at": "x"}
    assert pipeline.backlog_left(state, settings) == 0
    assert pipeline.waiting_left(state, settings) == 3
