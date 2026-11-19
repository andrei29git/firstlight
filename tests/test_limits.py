"""The limits registry: where each company's rule stands, and what applying would do."""

from __future__ import annotations

from datetime import date

from jobhunt import limits
from jobhunt.limits import Application, Limit

TODAY = date(2026, 10, 27)


def app(role="Engineer", applied="2026-10-01", stage="applied", history=(), cooldown_until=None):
    return Application(company="Acme", role=role, applied_on=date.fromisoformat(applied) if applied else None,
                       stage=stage, history=tuple((s, date.fromisoformat(d)) for s, d in history),
                       cooldown_until=date.fromisoformat(cooldown_until) if cooldown_until else None)


def test_a_rolling_window_counts_sent_applications_and_says_when_a_slot_opens():
    rule = Limit(company="Acme", scope="company", max=3, window_days=90, note="Max 3 per 90 days.")
    two = [app(applied="2026-09-01"), app(applied="2026-10-01"), app(applied=None, stage="to_apply")]
    state = limits.standing(rule, two, TODAY)
    assert (state.used, state.full, state.next_allowed) == (2, False, None)
    assert limits.warnings([rule], two, "Data Engineer", TODAY) == [
        "Uses the last slot left (2 of 3 in the last 90 days): Max 3 per 90 days."]
    three = two + [app(applied="2026-10-20")]
    full = limits.standing(rule, three, TODAY)
    assert full.full and full.next_allowed == date(2026, 11, 30)  # 1 Sep + 90 days
    assert limits.warnings([rule], three, "x", TODAY)[0].startswith("No slot left")


def test_a_recruiting_year_runs_september_to_august():
    rule = Limit(company="Acme", scope="graduate_programmes", max=1, window="academic_year", note="One a year.")
    assert limits.standing(rule, [app(applied="2026-08-25")], TODAY).full is False  # last year's
    state = limits.standing(rule, [app(applied="2026-10-07", stage="rejected")], TODAY)
    assert state.full and state.next_allowed == date(2027, 9, 1)


def test_cooldowns_after_applying_after_an_interview_and_dates_david_knows():
    rule = Limit(company="Acme", scope="global", cooldown_days=243, trigger="assessment_or_interview", note="8 months.")
    interviewed = app(applied="2026-08-01", stage="rejected", history=[("applied", "2026-08-01"), ("oa_done", "2026-09-01"),
                                                                        ("rejected", "2026-09-10")])
    state = limits.standing(rule, [interviewed], TODAY)
    assert state.cooling and state.next_allowed == date(2027, 5, 2)  # 1 Sep + 243 days
    assert limits.standing(rule, [app(applied="2026-08-01", stage="rejected")], TODAY).cooling is False  # no assessment
    known = app(applied="2026-08-01", stage="rejected", cooldown_until="2027-01-15")
    assert limits.standing(rule, [known], TODAY).next_allowed == date(2027, 1, 15)  # The user's date wins


def test_a_role_cooldown_only_warns_for_that_role():
    rule = Limit(company="Acme", scope="role", cooldown_days=180, note="180 days per role.")
    apps = [app(role="Backend Engineer", applied="2026-10-01")]
    assert limits.warnings([rule], apps, "Backend Engineer", TODAY) == [
        "You applied to this role: wait until 30 Mar 2027 (180 days per role.)"]
    assert limits.warnings([rule], apps, "Data Engineer", TODAY) == []


def test_a_rule_without_numbers_is_its_note():
    rule = Limit(company="Acme", note="Limits concurrent applications.")
    assert limits.warnings([rule], [], "x", TODAY) == ["Limits concurrent applications."]
