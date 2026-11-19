"""The weekly guard's pause (2026-10-27): with the graduate-wording filter permanent, the guard at
60% of the week (scheduled runs only) pauses the generated list's companies on the heavy boards instead."""

from __future__ import annotations

from jobhunt import pipeline, runs_view
from jobhunt.state import State

from .test_generated import ACME, NOW, run, settings, write_runs  # noqa: F401 (fixture)
from .test_wishlist import WISHED_ACME


def pausing(settings, boards=("greenhouse",)):  # noqa: F811
    generated = {**settings["generated"], "entry_level_only": True, "guard_pause_boards": list(boards)}
    return {**settings, "generated": generated}


def test_the_guard_pauses_its_boards_for_the_generated_list_only(tmp_path, settings):  # noqa: F811
    rules = pausing(settings)
    write_runs(tmp_path / "state", [3.8, 3.8])  # 61% of the week
    summary, state, claude, session = run(tmp_path, rules)
    guard = summary["weekly_guard"]
    assert guard["filter_on"] and guard["paused_boards"] == ["greenhouse"] and guard["companies_not_fetched"] == 1
    assert session.calls == [] and claude.calls == [] and not state.jobs  # Acme (generated) not fetched

    write_runs(tmp_path / "w" / "state", [3.8, 3.8])
    summary, state, claude, _ = run(tmp_path / "w", rules, companies=(WISHED_ACME,))
    assert state.jobs and claude.calls  # a wishlist company on the same board is fetched and scored
    assert summary["weekly_guard"]["paused_boards"] == ["greenhouse"]


def test_pending_roles_of_a_paused_board_wait_and_resume_below_50_percent(tmp_path, settings):  # noqa: F811
    rules = pausing(settings)
    summary, state, claude, _ = run(tmp_path, rules)  # guard off: fetched, scored
    assert not summary["weekly_guard"]["paused_boards"] and claude.calls
    record = next(iter(state.jobs.values()))
    paused = pipeline.GeneratedRules.from_settings(rules, paused_boards=frozenset({"greenhouse"}))
    assert not pipeline.generated_ok(record, paused)  # a pending role of a paused board is held

    write_runs(tmp_path / "s2", [7.0], filter_on=True)  # 56%: between the thresholds, stays on
    assert pipeline.weekly_guard(State(tmp_path / "s2"), rules, NOW)["paused_boards"] == ["greenhouse"]
    write_runs(tmp_path / "s3", [6.0], filter_on=True)  # 48%: off, the boards resume
    assert pipeline.weekly_guard(State(tmp_path / "s3"), rules, NOW)["paused_boards"] == []


def test_excluded_boards_are_never_listed_as_paused(tmp_path, settings):  # noqa: F811
    rules = pausing(settings, boards=("workday", "smartrecruiters"))
    rules["generated"]["exclude_boards"] = ["smartrecruiters"]
    write_runs(tmp_path / "s", [8.0])
    assert pipeline.weekly_guard(State(tmp_path / "s"), rules, NOW)["paused_boards"] == ["workday"]


def test_the_guard_is_described_in_words():
    base = {"on_at": 0.6, "off_at": 0.5, "entry_level_only": True, "pause_boards": ["personio", "workday"],
            "changed": False}
    off = pipeline.guard_words({**base, "filter_on": False, "paused_boards": []})
    assert off == ("Graduate-wording filter for the generated list: always on. "
                   "Weekly guard off: at 60% of the week by scheduled runs (the tool's estimate) it pauses Personio, "
                   "Workday for the generated list.")
    on = pipeline.guard_words({**base, "filter_on": True, "paused_boards": ["personio", "workday"], "changed": True})
    assert "Weekly guard ON: Personio, Workday paused for the generated list (your companies and wishlist are " \
           "still scored) until less than 50% of the week by scheduled runs (the tool's estimate) is used." in on
    assert on.endswith("(Switched on in this run.)")
    claude = pipeline.guard_words({**base, "on_at": 0.75, "off_at": 0.65, "filter_on": False, "paused_boards": [],
                                   "claude": {"utilization": 0.21}})
    assert "at 75% of the week (Claude's figure, your own use included) it pauses Personio, Workday" in claude
    assert "Claude reports 21% of the weekly limit." in claude
    row = runs_view.row(0, {"started_at": "2026-10-27T10:00:00+00:00", "weekly_guard": {
        "paused_boards": ["workday"], "companies_not_fetched": 40}}, {})
    assert "Weekly guard on: Workday paused for the generated list (40 companies not fetched)" in row.notes


def test_the_guard_follows_claudes_own_figure_and_counts_a_reset_week_as_empty(tmp_path):
    from datetime import UTC, datetime

    from jobhunt.state import State
    state = State(tmp_path)
    now = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
    resets = datetime(2026, 11, 4, 1, 0, tzinfo=UTC).timestamp()
    for at, used in (("2026-10-29T15:00:00+00:00", 0.78), ("2026-10-29T12:00:00+00:00", 0.10)):
        state.scores[at] = {"key": at, "model": "haiku", "scored_at": at, "rate_limit": {
            "rateLimitType": "five_hour", "status": "allowed",
            "unifiedWindows": {"seven_day": {"utilization": used, "resetsAt": resets, "status": "allowed"}}}}
    settings = {"usage": {"weekly_guard_share": 0.75, "weekly_guard_off_share": 0.65},
                "generated": {"guard_pause_boards": ["workday"]}}
    guard = pipeline.weekly_guard(state, settings, now)
    assert guard["measure"] == "claude" and guard["claude"]["utilization"] == 0.78 and guard["filter_on"]
    assert guard["paused_boards"] == ["workday"]
    after_reset = pipeline.weekly_guard(state, settings, datetime(2026, 11, 4, 2, 0, tzinfo=UTC))
    assert after_reset["claude"]["reset"] and not after_reset["filter_on"]
