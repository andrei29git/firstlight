from __future__ import annotations

from jobhunt import config as cfg
from jobhunt import evaluate

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE, FakeClaude, claude_stream


def answer(message: str) -> str:
    if "Initech" in message:
        return claude_stream({**GOOD_SCORE, "fit": 3, "odds": "high", "action": "skip"})
    return claude_stream({**GOOD_SCORE, "fit": 9})


def test_eval_scores_roles_with_postings_three_times():
    fake = FakeClaude(answer)
    found, missing = evaluate.run_eval(EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG), runs=3, runner=fake)
    assert [c.role for c in found] == ["Example Bank Backend Graduate Programme, Madrid",
                                       "Initech Mobile Engineer Intern, Lisbon"]
    assert missing == ["Umbrella Data Engineer, Paris"]
    assert len(fake.calls) == 6
    report = evaluate.agreement(found)
    assert report["fit within 1"] == "6/6 (100%)"
    assert report["action"] == "6/6 (100%)"
    assert report["mean fit spread"] == 0


def test_eval_leaves_the_scored_role_out_of_the_prompt():
    fake = FakeClaude(answer)
    evaluate.run_eval(EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG), runs=1, runner=fake)
    for call in fake.calls:
        system = call["argv"][call["argv"].index("--system-prompt") + 1]
        role = "Initech" if "Initech" in call["stdin"] else "Example Bank Backend"
        assert role not in system


def test_eval_reports_disagreement():
    fake = FakeClaude(lambda m: claude_stream({**GOOD_SCORE, "fit": 5}))
    found, _ = evaluate.run_eval(EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG), runs=1, runner=fake)
    assert evaluate.agreement(found)["fit within 1"] == "0/2 (0%)"


def test_eval_flags_timing_that_changes_the_action():
    timing_skip = {**GOOD_SCORE, "fit": 9, "action": "skip",
                   "explanation": "Strong fit, but it needs an immediate start, so skip.",
                   "blockers": [{"type": "timing", "severity": "blocking", "detail": "Immediate start", "min_years": None}]}
    fake = FakeClaude(lambda m: claude_stream(timing_skip))
    found, _ = evaluate.run_eval(EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG), runs=1, runner=fake)
    report = evaluate.agreement(found, consider_start_date=False)
    assert report["timing changed the action (want 0)"] == "2/2 (100%)"
    assert report["timing blockers reported (want 0)"] == "2/2 (100%)"
    assert "timing changed the action (want 0)" not in evaluate.agreement(found, consider_start_date=True)


def test_a_skip_for_a_hard_requirement_is_not_a_timing_skip():
    language_skip = {**GOOD_SCORE, "action": "skip", "explanation": "Fluent Dutch is required; it starts in March.",
                     "blockers": [{"type": "language", "severity": "blocking", "detail": "Dutch"}]}
    assert not evaluate.timing_changed_action(language_skip)
    assert evaluate.timing_changed_action({**GOOD_SCORE, "action": "contact_recruiter_about_later_intake"})


def test_a_graduation_window_skip_is_not_a_timing_skip():
    window_skip = {**GOOD_SCORE, "action": "skip", "blockers": [],
                   "explanation": "The window opens after he graduates in June 2028, so skip."}
    assert not evaluate.timing_changed_action(window_skip)
