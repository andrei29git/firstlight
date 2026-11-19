"""The Runs page: every run with its numbers, Actions minutes, the week's usage and what waits."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobhunt import actions_usage, runs_view
from jobhunt import config as cfg
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .test_jobs_web import client  # noqa: F401 (fixture)

GITHUB_RUN = {
    "started_at": "2026-10-27T12:56:29+00:00", "ran_on": "github", "kind": "scheduled", "seconds": 122.1,
    "sources": {"Mistral": {"board": "ashby", "fetched": 40, "new": 2, "closed": 1, "passed": 12},
                "Blackstone": {"board": "workday", "error": "unexpected workday response"}},
    "generated": {"greenhouse": {"companies": 300, "fetched": 9000, "new": 50, "closed": 30, "passed": 4000}},
    "scoring": {"during fetch: my companies": {"model": "sonnet", "calls": 4, "deferred": 0, "window_share_spent": 0.016},
                "scheduled": {"model": "sonnet", "calls": 0, "deferred": 7, "window_share_spent": 0.0,
                              "stopped": "Claude warned the window is 95% used"}},
    "alerts": {"sent": 1},
}
OLD_FLAT_RUN = {"started_at": "2026-10-24T15:22:07+00:00", "seconds": 300,
                "scoring": {"calls": 8, "pending": 10, "deferred": 2}}


def test_rows_add_up_boards_scoring_and_problems():
    settings = cfg.load_settings(EXAMPLE_CONFIG)
    rows = runs_view.rows([OLD_FLAT_RUN, GITHUB_RUN], settings)
    newest, oldest = rows
    assert (newest.ran_on, newest.kind, newest.calls, newest.deferred, newest.alerts) == ("github", "scheduled", 4, 7, 1)
    assert newest.boards["greenhouse"]["new"] == 50 and newest.boards["workday"]["errors"] == 1
    assert newest.totals["fetched"] == 9040 and round(newest.usage, 3) == 0.016
    assert newest.errors == ["Blackstone: unexpected workday response"]  # named once
    assert newest.stops == ["Claude warned the window is 95% used"]
    assert (oldest.ran_on, oldest.calls) == ("laptop", 8) and oldest.usage > 0  # estimated from its calls


def test_actions_minutes_round_each_job_up_and_cache_finished_runs(tmp_path):
    asked = []

    def api(path):
        asked.append(path)
        if path.endswith("/jobs"):
            return {"jobs": [{"started_at": "2026-10-27T12:55:30Z", "completed_at": "2026-10-27T12:58:41Z"}]}
        return {"workflow_runs": [{"id": 1, "name": "run", "status": "completed"},
                                  {"id": 2, "name": "ci", "status": "completed"}]}
    now = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    found = actions_usage.month_usage("owner/repo", tmp_path / "cache.json", api, now)
    assert found["minutes"] == 8 and found["by_workflow"] == {"ci": 4, "run": 4}  # 3m11s rounds up to 4
    assert "created=>=2026-10-01" in asked[0]
    asked.clear()
    actions_usage.month_usage("owner/repo", tmp_path / "cache.json", api, now)
    assert not [p for p in asked if p.endswith("/jobs")]  # finished runs are not asked about again


def test_the_runs_page_and_a_run_sheet(client, paths):  # noqa: F811
    state = State(paths.state_dir)
    state.append_run(OLD_FLAT_RUN)
    state.append_run(GITHUB_RUN)
    html = client.get("/runs").text
    assert "last on GitHub 27 Oct" in html
    assert "GitHub" in html and "Laptop" in html and "1 error" in html and "95% used" in html
    assert "of the weekly limit" in html and "Sync now" in html
    assert "Weekly guard off: at 60% of the week by scheduled runs (the tool&#39;s estimate) it pauses Homerun, Personio, SmartRecruiters, Teamtailor, Workable for the generated list" in html
    assert 'href="/runs"' in html and 'aria-current="page"' in html
    index = len(state.runs()) - 1  # the GitHub run is the newest
    sheet = client.get(f"/runs/{index}/sheet").text
    assert "Fetched, per board" in sheet and "Greenhouse" in sheet and "Blackstone" in sheet
    assert client.get("/runs/99/sheet").status_code == 404


def test_health_turns_orange_after_3_hours_and_red_after_6_and_expects_the_next_run():
    workflow = 'on:\n  schedule:\n    - cron: "41 * * * *"\n'
    runs = [{**GITHUB_RUN, "started_at": "2026-10-27T10:00:00+00:00", "seconds": 120}]
    at = lambda h, m: datetime(2026, 10, 27, h, m, tzinfo=UTC)  # noqa: E731
    fresh = runs_view.health(runs, workflow, at(11, 0))
    # Finished 10:02: the 11:41 run comes 99 minutes after it and skips, so 12:41 is next.
    assert fresh["level"] == "ok" and fresh["expected"] == "2026-10-27T12:41:00+00:00"
    assert runs_view.health(runs, workflow, at(13, 30))["level"] == "orange"
    late = runs_view.health(runs, workflow, at(16, 30))
    assert late["level"] == "red" and late["expected"] == "2026-10-27T16:41:00+00:00"
    assert runs_view.health([], workflow, at(9, 0))["level"] == "none"


def test_the_next_run_follows_the_external_trigger_with_github_as_the_backup():
    workflow = 'on:\n  schedule:\n    - cron: "41 * * * *"\n'
    at = lambda h, m: datetime(2026, 10, 27, h, m, tzinfo=UTC)  # noqa: E731
    run = lambda h, m, secs: [{**GITHUB_RUN, "started_at": at(h, m).isoformat(), "seconds": secs}]  # noqa: E731
    # Finished 10:16: 12:11 is 115 minutes later, so cron-job.org's run goes ahead.
    usual = runs_view.health(run(10, 11, 300), workflow, at(11, 0), external="11 */2 * * *")
    assert usual["expected"] == "2026-10-27T12:11:00+00:00" and usual["expected_by"] == "cron-job.org"
    # Finished 10:45 (a backup run): 12:11 is 86 minutes later and ends at once; the 12:41 backup runs.
    backup = runs_view.health(run(10, 41, 240), workflow, at(11, 0), external="11 */2 * * *")
    assert backup["expected"] == "2026-10-27T12:41:00+00:00" and backup["expected_by"] == "GitHub schedule"
    # Overdue: the next tick from now.
    late = runs_view.health(run(4, 11, 300), workflow, at(13, 0), external="11 */2 * * *")
    assert late["expected"] == "2026-10-27T13:41:00+00:00" and late["level"] == "red"
    assert runs_view.health([], workflow, at(12, 30), external="11 */2 * * *")["expected"] == "2026-10-27T12:41:00+00:00"
    assert [t.hour for t in runs_view.cron_ticks("11 */2 * * *", at(23, 0), hours=5)] == [0, 2, 4]
    assert list(runs_view.cron_ticks("*/5 * * * *", at(9, 0))) == []  # not a form this tool reads


def test_the_latest_seven_day_reading_wins_wherever_it_was_recorded():
    runs = [{"started_at": "x", "seven_day": {"utilization": 0.81, "at": "2026-10-27T14:00:00+00:00"}}]
    from_scores = {"utilization": 0.88, "at": "2026-10-27T12:06:57+00:00"}
    assert runs_view.latest_seven_day(runs, from_scores)["utilization"] == 0.81
    assert runs_view.latest_seven_day([], from_scores)["utilization"] == 0.88


def test_the_guard_keeps_the_latest_seven_day_reading():
    from jobhunt import scoring
    guard = scoring.UsageGuard(stop_on_warning=True, ignore_weekly_warning=True)
    guard.observe({"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.84})
    assert guard.seven_day["utilization"] == 0.84 and guard.stopped is None


def test_filters_and_the_counting_minutes_tile(client, paths, monkeypatch):  # noqa: F811
    state = State(paths.state_dir)
    state.append_run(OLD_FLAT_RUN)
    state.append_run(GITHUB_RUN)
    only_github = client.get("/runs?where=github").text
    assert only_github.count('hx-get="/runs/') == 1 and "of" in only_github
    assert client.get("/runs?kind=scheduled").text.count('hx-get="/runs/') == 1
    import jobhunt.web.app as webapp  # noqa: F401
    tile = client.get("/runs/minutes").text
    assert "Actions minutes" in tile


def test_a_count_in_progress_shows_its_figure_so_far(tmp_path):
    seen = []

    def api(path):
        if path.endswith("/jobs"):
            return {"jobs": [{"started_at": "2026-10-27T12:00:00Z", "completed_at": "2026-10-27T12:00:30Z"}]}
        return {"total_count": 2, "workflow_runs": [{"id": 1, "name": "ci", "status": "completed"},
                                                    {"id": 2, "name": "run", "status": "completed"}]}
    actions_usage.month_usage("o/r", tmp_path / "c.json", api, datetime(2026, 10, 27, tzinfo=UTC), progress=seen.append)
    assert seen == [{"minutes": 1, "runs_counted": 1, "runs_total": 2}, {"minutes": 2, "runs_counted": 2, "runs_total": 2}]


def test_the_scheduled_workflow_skips_when_a_run_pushed_less_than_100_minutes_ago():
    from pathlib import Path
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "run.yml"
    if not workflow.exists():
        pytest.skip("the scheduled workflow is private: left out of the public copy")
    text = workflow.read_text()
    assert 'cron: "41 * * * *"' in text and "gap=6000" in text and '-lt "$gap"' in text
    assert 'select(.commit.author.name == "jobhunt (GitHub Actions)")' in text
    assert "actions/checkout@v7" in text and "ubuntu-24.04" in text and "@v4" not in text
    # The external trigger dispatches mode "scheduled": the same 100-minute rule applies.
    assert "options: [run, scheduled, selftest]" in text
    assert '[ "${{ github.event_name }}" = "schedule" ] || [ "${{ inputs.mode }}" = "scheduled" ]' in text


def test_only_github_runs_count_towards_the_100_minute_skip():
    """The skip looks at the data branch's latest commit made by a GitHub run; a laptop run's push
    (made under your own git name, or sync's fallback "jobhunt") never counts (checked 2026-11-19:
    on 19 Nov the laptop pushed at 15:14 and the 16:11 trigger still saw the GitHub push of 14:26,
    105 minutes earlier; it ended because the Actions minutes guard had raised the gap to 220)."""
    import json as json_
    import re
    import shutil
    import subprocess
    from pathlib import Path

    from jobhunt import sync
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "run.yml"
    if not workflow.exists():
        pytest.skip("the scheduled workflow is private: left out of the public copy")
    if not shutil.which("jq"):
        pytest.skip("jq is not installed")
    text = workflow.read_text()
    jq_filter = re.search(r"commits\?sha=data&per_page=30\" \\\n\s*--jq '([^']+)'", text).group(1)
    identity = re.search(r'git -C \.data config user\.name "([^"]+)"', text).group(1)
    commits = [  # newest first, as GitHub's API lists them
        {"commit": {"author": {"name": "Andrei Example", "date": "2026-11-19T14:14:04Z"}}},  # the laptop
        {"commit": {"author": {"name": sync.FALLBACK_IDENTITY["GIT_AUTHOR_NAME"], "date": "2026-11-19T14:00:00Z"}}},
        {"commit": {"author": {"name": identity, "date": "2026-11-19T13:26:15Z"}}},  # GitHub's run
        {"commit": {"author": {"name": identity, "date": "2026-11-19T11:26:14Z"}}},
    ]
    found = subprocess.run(["jq", "-r", jq_filter], input=json_.dumps(commits), capture_output=True, text=True, check=True)
    assert found.stdout.strip() == "2026-11-19T13:26:15Z"
    assert identity == "jobhunt (GitHub Actions)" != sync.FALLBACK_IDENTITY["GIT_AUTHOR_NAME"]
    # With no GitHub push among the latest commits, nothing is skipped.
    none = subprocess.run(["jq", "-r", jq_filter], input=json_.dumps(commits[:2]), capture_output=True, text=True, check=True)
    assert none.stdout.strip() == ""


def test_the_external_trigger_is_read_in_its_own_time_zone():
    at = lambda h, m: datetime(2026, 10, 28, h, m, tzinfo=UTC)  # noqa: E731
    run = [{**GITHUB_RUN, "started_at": at(8, 11).isoformat(), "seconds": 300}]  # finished 08:16 UTC
    workflow = 'on:\n  schedule:\n    - cron: "41 * * * *"\n'
    # 11 past every even hour in Amsterdam: 10:11 UTC is 11:11 there (not a tick); 12:11 there is 11:11 UTC.
    winter = runs_view.health(run, "", at(9, 0), external="11 */2 * * *", external_timezone="Europe/Amsterdam")
    assert winter["expected"] == "2026-10-28T11:11:00+00:00" and winter["expected_by"] == "cron-job.org"
    backup = runs_view.health(run, workflow, at(9, 0), external="11 */2 * * *", external_timezone="Europe/Amsterdam")
    assert backup["expected"] == "2026-10-28T10:41:00+00:00"  # GitHub's hourly backup could come first
    summer = list(runs_view.cron_ticks("11 */2 * * *", datetime(2026, 7, 1, 10, 30, tzinfo=UTC), hours=4,
                                       timezone="Europe/Amsterdam"))
    assert [t.hour for t in summer] == [12, 14]  # 14:11 and 16:11 summer time


def test_recent_triggers_say_what_came_of_each():
    minutes = {"latest_runs": [
        {"at": "2026-10-29T09:11:03Z", "conclusion": "success", "event": "workflow_dispatch", "seconds": 9},
        {"at": "2026-10-29T07:40:12Z", "conclusion": "success", "event": "schedule", "seconds": 300},
        {"at": "2026-10-28T19:11:04Z", "conclusion": "failure", "event": "workflow_dispatch", "seconds": 70}]}
    found = runs_view.triggers(minutes)
    assert [(t["by"], t["outcome"].split(" (")[0]) for t in found] == [
        ("cron-job.org", "ended at once"), ("GitHub schedule", "ran"), ("cron-job.org", "failed")]
