"""Usage from Claude's own readings (2026-10-30): calls counted at their actual cost, a time limit
for scheduled runs, Claude's two seven-day limits told apart, and weekly warnings stopping a run
only from the guard's threshold."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from jobhunt import pipeline, scoring
from jobhunt.state import State

WEEK_RESET = datetime(2026, 11, 4, 1, tzinfo=UTC).timestamp()
SECOND_RESET = datetime(2026, 11, 5, 18, tzinfo=UTC).timestamp()
NOW = datetime(2026, 10, 30, 13, tzinfo=UTC)


class Call:
    def __init__(self, cost):
        self.cost_usd, self.infrastructure, self.error, self.permanent = cost, False, None, False


def test_a_call_counts_at_its_actual_cost_once_it_returns():
    guard = scoring.UsageGuard(max_share=0.2, share_per_call={"haiku": 0.00015}, share_per_usd=0.10)
    assert guard.allow("haiku") and abs(guard.spent - 0.00015) < 1e-9  # reserved before the call
    guard.record(Call(0.0013), "haiku")
    assert abs(guard.spent - 0.00013) < 1e-9  # its real cost: $0.0013 at 0.10 of a window per dollar
    old = scoring.UsageGuard(share_per_call={"haiku": 0.0012})  # without a rate: the reservation stays
    old.allow("haiku")
    old.record(Call(0.0013), "haiku")
    assert old.spent == 0.0012


def test_a_scheduled_run_starts_no_call_after_its_time_limit():
    guard = scoring.UsageGuard(deadline=time.monotonic() - 1)
    assert not guard.allow("haiku") and guard.stopped.startswith("run time limit reached")


def test_past_runs_count_at_their_recorded_cost():
    phase = {"model": "haiku", "calls": 155, "cost_usd": 0.211, "window_share_spent": 0.2}
    assert pipeline.phase_usage(phase, {"haiku": 0.0012}) == 0.2  # as recorded
    assert abs(pipeline.phase_usage(phase, {"haiku": 0.0012}, 0.10) - 0.0211) < 1e-9  # from its cost


def score(state, key, at, rate_limit):
    state.scores[key] = {"id": key, "key": key, "scored_at": at, "rate_limit": rate_limit}


def test_claudes_two_weekly_limits_are_told_apart_by_their_reset(tmp_path):
    state = State(tmp_path / "s")
    score(state, "a@haiku", "2026-10-30T11:20:00+00:00", {
        "rateLimitType": "five_hour", "status": "allowed",
        "unifiedWindows": {"five_hour": {"utilization": 0.08, "resetsAt": WEEK_RESET - 3e5},
                           "seven_day": {"utilization": 0.33, "resetsAt": WEEK_RESET, "status": "allowed"}}})
    score(state, "b@sonnet", "2026-10-30T12:30:00+00:00", {
        "rateLimitType": "seven_day", "status": "allowed_warning", "utilization": 0.26, "resetsAt": SECOND_RESET})
    weeks = pipeline.claude_weeks(state, NOW)
    assert weeks["weekly"]["utilization"] == 0.33 and weeks["second"]["utilization"] == 0.26
    binding = pipeline.claude_week(state, NOW)
    assert binding["limit"] == "weekly" and binding["utilization"] == 0.33  # the higher one binds
    assert pipeline.limits_text(binding) == ("33% of the weekly limit (resets 4 Nov 01:00 UTC) and "
                                             "26% of the second weekly limit (resets 5 Nov 18:00 UTC)")
    # A seven-day warning with the weekly limit's reset (as on 27 October) is the weekly limit.
    score(state, "c@haiku", "2026-10-30T12:40:00+00:00", {
        "rateLimitType": "seven_day", "status": "allowed_warning", "utilization": 0.8, "resetsAt": WEEK_RESET})
    assert pipeline.claude_weeks(state, NOW)["weekly"]["utilization"] == 0.8


def test_a_weekly_warning_stops_a_run_only_from_the_guards_threshold(tmp_path):
    guard = scoring.UsageGuard.from_settings({"usage": {"weekly_guard_share": 0.75, "stop_on_warning": True}}, "backlog")
    guard.observe({"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.26})
    assert guard.stopped is None and guard.weekly_warnings_below == 0.26
    guard.observe({"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.75})
    assert guard.stopped == "Claude warned the weekly limit is 75% used"
    state = State(tmp_path / "s")
    score(state, "b@sonnet", "2026-10-30T12:30:00+00:00", {
        "rateLimitType": "seven_day", "status": "allowed_warning", "utilization": 0.26, "resetsAt": SECOND_RESET})
    assert pipeline.week_nearly_full(state, NOW) is None  # a backfill may start
    score(state, "c@sonnet", "2026-10-30T12:31:00+00:00", {
        "rateLimitType": "seven_day", "status": "allowed_warning", "utilization": 0.8, "resetsAt": SECOND_RESET})
    assert pipeline.week_nearly_full(state, NOW)["utilization"] == 0.8


# --- Every role in the main countries (2026-10-30) ----------------------------------------------

def role(key, title, place, posted=None, first_seen="2026-10-29T10:00:00+00:00", **extra):
    return {"key": key, "company": "Globex", "title": title, "locations": [place], "origin": "generated",
            "source": "greenhouse", "prefilter": "pass", "closed": False, "posted": posted, "first_seen": first_seen,
            "description_hash": None, **extra}


def main_rules(on):
    return pipeline.GeneratedRules.from_settings({"generated": {
        "entry_level_only": True, "main_countries": ["NL", "RO", "IT", "FR", "DE"], "all_roles_in_main": on}})


def test_every_role_in_the_main_countries_is_scored_when_switched_on():
    amsterdam, dublin = role("a", "Backend Engineer", "Amsterdam, Netherlands"), role("b", "Backend Engineer", "Dublin, Ireland")
    graduate = role("c", "Graduate Backend Engineer", "Dublin, Ireland")
    off, on = main_rules(False), main_rules(True)
    assert not pipeline.could_be_scored(amsterdam, off) and pipeline.could_be_scored(amsterdam, on)
    assert not pipeline.could_be_scored(dublin, on) and pipeline.could_be_scored(graduate, on)  # filter stays elsewhere
    from jobhunt import catalog
    assert catalog.view_of(amsterdam, None, True, lambda p: not on.needs_entry_terms(p)) == "waiting"
    assert catalog.view_of(dublin, None, True, lambda p: not on.needs_entry_terms(p)) == "skipped"


def test_the_backlog_takes_main_country_roles_posted_in_the_last_14_days(tmp_path):
    state = State(tmp_path / "s")
    for r in [role("new", "Data Engineer", "Bucharest, Romania", posted="2026-10-25"),
              role("old", "Data Engineer", "Paris, France", posted="2026-10-01"),
              role("undated", "Data Engineer", "Milan, Italy", first_seen="2026-10-28T10:00:00+00:00"),
              role("abroad", "Data Engineer", "Dublin, Ireland", posted="2026-10-25")]:
        state.jobs[r["key"]] = r
    settings = {"generated": {"entry_level_only": True, "main_countries": ["NL", "RO", "IT", "FR", "DE"],
                              "all_roles_in_main": True, "backfill_days_other": 14}}
    counts = pipeline.apply_rule_changes(state, settings, datetime(2026, 10, 30).date(), "2026-10-30T14:00:00+00:00")
    assert counts["main countries, no graduate wording, recent: scored"] == 2
    assert counts["main countries, no graduate wording, older: left out"] == 1
    assert state.jobs["new"]["backlog"] and state.jobs["undated"]["backlog"] and state.jobs["old"]["backfill_skip"]
    assert "backlog" not in state.jobs["abroad"] and "backfill_skip" not in state.jobs["abroad"]
