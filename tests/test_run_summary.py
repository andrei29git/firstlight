"""The summary `jobhunt run` prints at the end works for every kind of run (2026-10-30: a
--no-fetch backfill run stopped by the weekly hard stop crashed on a missing fetch time)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobhunt import cli
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG


def use_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_ROOT", str(tmp_path))
    monkeypatch.setenv("JOBHUNT_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("JOBHUNT_DB", str(tmp_path / "test.db"))
    state = tmp_path / "state"
    State(state).save()
    return state


def test_a_no_fetch_backfill_run_stopped_by_the_weekly_hard_stop_says_why(monkeypatch, tmp_path, capsys):
    state = use_paths(monkeypatch, tmp_path)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    State(state).append_run({"started_at": yesterday, "scoring": {"backlog": {"calls": 900, "window_share_spent": 9.0}}})
    code = cli.main(["run", "--no-fetch", "--backfill", "--budget", "0.9", "--state", str(state), "--no-sync"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("Backlog run not started: the tool used 72% of the weekly limit")
    assert "Fetched" not in out and "Run took" in out
    assert State(state).runs()[-1]["backlog_stopped"]


def test_a_no_fetch_backfill_run_that_goes_ahead_prints_its_summary(monkeypatch, tmp_path, capsys):
    state = use_paths(monkeypatch, tmp_path)
    code = cli.main(["run", "--no-fetch", "--backfill", "--budget", "0.9", "--state", str(state), "--no-sync"])
    out = capsys.readouterr().out
    assert code == 0 and "not started" not in out
    assert "No fetching in this run" in out and "Weekly guard:" in out and "Run took" in out


def test_the_summary_copes_with_a_bare_record(capsys):
    cli.print_summary({"started_at": "2026-10-30T12:00:00+00:00"}, ".data")
    assert "Run took 0s" in capsys.readouterr().out


def claude_reports(state, utilization):
    """A stored score carrying Claude's seven-day figure, as every call reports it."""
    s = State(state)
    resets = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    s.scores["x@sonnet"] = {"id": "x@sonnet", "scored_at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "rate_limit": {"unifiedWindows": {"seven_day": {"utilization": utilization,
                                                                            "resetsAt": resets}}}}
    s.save()


def test_the_hard_stop_reads_claudes_own_figure_over_the_tools_estimate(monkeypatch, tmp_path, capsys):
    state = use_paths(monkeypatch, tmp_path)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    State(state).append_run({"started_at": yesterday, "scoring": {"backlog": {"calls": 900, "window_share_spent": 9.0}}})
    claude_reports(state, 0.33)  # the estimate says 72%, Claude 33%: the run goes ahead
    cli.main(["run", "--no-fetch", "--backfill", "--budget", "0.9", "--state", str(state), "--no-sync"])
    assert "not started" not in capsys.readouterr().out
    claude_reports(state, 0.65)
    cli.main(["run", "--no-fetch", "--backfill", "--state", str(state), "--no-sync"])
    assert capsys.readouterr().out.startswith("Backlog run not started: Claude reports 65% of the weekly limit")
    # (with its reset), "(backlog runs stop at 60%)."
