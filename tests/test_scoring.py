from __future__ import annotations

import copy
import json

import pytest

from jobhunt import config as cfg
from jobhunt import scoring
from jobhunt.sources.base import Job

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE, FakeClaude, claude_stream


@pytest.fixture
def settings():
    return cfg.load_settings(EXAMPLE_CONFIG)


def job(**kw) -> Job:
    values = dict(company="Example Bank", title="Backend Graduate", url="https://x.example/1", source="greenhouse",
                  source_id="1", locations=["Madrid, Spain"], description="Python and Go graduate programme.")
    values.update(kw)
    return Job(**values)


def test_rules_render_every_placeholder(settings):
    system = scoring.build_prompt(EXAMPLE_CONFIG, settings).system
    assert "{{" not in system and "note:" not in system
    assert "Spanish EU citizen" in system  # from candidate.toml
    assert "Calibration examples" in system and "Initech Mobile Engineer Intern" in system


def test_unknown_placeholder_fails():
    with pytest.raises(scoring.ScoringError, match="nope.missing"):
        scoring.render_rules("Hello {{nope.missing}}", {}, consider_start_date=False)


def test_timing_block_follows_the_setting():
    template = "Intro\n<!-- timing:start -->\n## Availability\nFrom {{a.b}}\n<!-- timing:end -->\nEnd"
    assert "Availability" not in scoring.render_rules(template, {}, consider_start_date=False)
    shown = scoring.render_rules(template, {"a": {"b": "June"}}, consider_start_date=True)
    assert "## Availability\nFrom June" in shown and "timing:" not in shown


def test_maintainer_notes_are_stripped():
    assert scoring.render_rules("<!-- note: private -->Text", {}, False) == "Text\n"


def test_calibration_uses_action_without_timing_and_drops_timing_blockers():
    example = {"role": "R", "fit": 7, "odds": "low", "action": "apply_state_availability",
               "action_without_timing": "apply_now", "fit_reason": "ok",
               "blockers": [{"type": "timing", "severity": "blocking", "detail": "Starts too early"}]}
    text = scoring.calibration_examples([example], consider_start_date=False)
    assert "`apply_now`" in text and "Starts too early" not in text
    text = scoring.calibration_examples([example], consider_start_date=True)
    assert "`apply_state_availability`" in text and "Starts too early" in text


def test_leave_one_out(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings, exclude_role="Initech Mobile Engineer Intern, Lisbon")
    assert "Initech" not in prompt.system and "Example Bank Backend" in prompt.system


def test_prompt_version_tracks_what_the_model_sees(settings):
    base = scoring.build_prompt(EXAMPLE_CONFIG, settings).version
    changed = copy.deepcopy(settings)
    changed["scoring"]["model"] = "opus"
    assert scoring.build_prompt(EXAMPLE_CONFIG, changed).version != base
    assert scoring.build_prompt(EXAMPLE_CONFIG, settings, model="haiku").version != base
    run_control = copy.deepcopy(settings)
    run_control["scoring"]["max_calls_per_run"] = 7
    run_control["scoring"]["parallel"] = 9
    run_control["scoring"]["backlog_rescore_min_fit"] = 3
    assert scoring.build_prompt(EXAMPLE_CONFIG, run_control).version == base


def test_call_is_isolated_and_tool_less(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    fake = FakeClaude()
    score = scoring.score_job(job(), prompt, runner=fake)
    assert score.result == GOOD_SCORE and score.error is None
    argv = fake.calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--no-session-persistence" in argv and "--bare" not in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == prompt.schema
    assert fake.calls[0]["cwd_files"] == []  # an empty working directory
    assert fake.calls[0]["env"]["MAX_THINKING_TOKENS"] == "0"


def test_posting_is_marked_untrusted_and_cut(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    message = scoring.user_message(job(description="word " * 5000), max_chars=100)
    assert "<posting>" in message and "[... cut to length]" in message
    assert len(message.split("<posting>")[1]) < 200
    assert "untrusted" in prompt.system


def test_records_usage_and_rate_limit(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    score = scoring.score_job(job(), prompt, runner=FakeClaude())
    assert score.cost_usd == 0.02
    assert score.tokens["cache_creation_input_tokens"] == 3000
    assert score.rate_limit["rateLimitType"] == "five_hour"


@pytest.mark.parametrize("stdout, error", [
    (claude_stream(None), "no structured_output"),
    (claude_stream(is_error=True), "claude error"),
    (claude_stream({**GOOD_SCORE, "fit": 11}), "invalid output"),
    (claude_stream({**GOOD_SCORE, "action": "apply_later"}), "invalid output"),
    (claude_stream({k: v for k, v in GOOD_SCORE.items() if k != "angle"}), "missing angle"),
    ("not json at all", "no result"),
])
def test_bad_output_is_an_error_not_a_score(settings, stdout, error):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    score = scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: stdout))
    assert score.result is None and error in score.error


def test_status_from_blockers():
    timing = {"type": "timing", "severity": "blocking", "detail": "x"}
    visa = {"type": "visa", "severity": "check", "detail": "x"}
    assert scoring.status([], False) == "clear"
    assert scoring.status([visa], False) == "check"
    assert scoring.status([timing, visa], True) == "blocked"
    assert scoring.status([timing, visa], False) == "check"  # timing ignored when start dates are off


def test_board_deadline_wins(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    answer = claude_stream({**GOOD_SCORE, "deadline": "2027-01-31"})
    assert scoring.score_job(job(deadline="2026-12-01"), prompt, runner=FakeClaude(lambda m: answer)).deadline == "2026-12-01"
    assert scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: answer)).deadline == "2027-01-31"


def test_parallel_scoring_stops_after_a_usage_limit(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    fake = FakeClaude(lambda m: claude_stream(None, limit_status="rejected"))
    scores = scoring.score_many([job(source_id=str(i)) for i in range(10)], prompt, parallel=1, runner=fake)
    assert len(scores) == 1 and "usage limit" in scores[0].error


def test_timing_actions_are_not_offered_when_timing_is_ignored(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    assert prompt.schema["properties"]["action"]["enum"] == ["apply_now", "watch_for_reopening", "skip"]
    answer = claude_stream({**GOOD_SCORE, "action": "apply_state_availability"})
    score = scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: answer))
    assert score.result is None and "invalid output" in score.error
    with_timing = copy.deepcopy(settings)
    with_timing["scoring"]["consider_start_date"] = True
    assert "apply_state_availability" in scoring.build_prompt(EXAMPLE_CONFIG, with_timing).schema["properties"]["action"]["enum"]


def test_no_timing_block_follows_the_setting():
    template = "<!-- no-timing:start -->\nTiming is ignored.\n<!-- no-timing:end -->\nEnd"
    assert "Timing is ignored." in scoring.render_rules(template, {}, consider_start_date=False)
    assert "Timing is ignored." not in scoring.render_rules(template, {}, consider_start_date=True)


def test_example_rules_render_both_ways(settings):
    system = scoring.build_prompt(EXAMPLE_CONFIG, settings).system
    assert "## Timing is ignored" in system and "## Availability" not in system
    assert "apply_state_availability" not in system
    with_timing = copy.deepcopy(settings)
    with_timing["scoring"]["consider_start_date"] = True
    system = scoring.build_prompt(EXAMPLE_CONFIG, with_timing).system
    assert "## Availability" in system and "## Timing is ignored" not in system


def test_timing_blockers_are_dropped_when_timing_is_ignored(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    blockers = [{"type": "timing", "severity": "blocking", "detail": "Starts now", "min_years": None},
                {"type": "visa", "severity": "check", "detail": "UK sponsorship", "min_years": None}]
    answer = claude_stream({**GOOD_SCORE, "blockers": blockers})
    score = scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: answer))
    assert [b["type"] for b in score.result["blockers"]] == ["visa"]
    assert score.timing_blockers_dropped == 1 and score.status == "check"
    with_timing = copy.deepcopy(settings)
    with_timing["scoring"]["consider_start_date"] = True
    kept = scoring.score_job(job(), scoring.build_prompt(EXAMPLE_CONFIG, with_timing), runner=FakeClaude(lambda m: answer))
    assert len(kept.result["blockers"]) == 2 and kept.timing_blockers_dropped == 0


def test_experience_blockers_need_stated_years(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    blockers = [{"type": "experience", "severity": "blocking", "detail": "Trading systems experience", "min_years": None},
                {"type": "experience", "severity": "blocking", "detail": "3+ years of Go", "min_years": 3}]
    answer = claude_stream({**GOOD_SCORE, "blockers": blockers, "gaps": ["Rust"]})
    score = scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: answer))
    assert [b["detail"] for b in score.result["blockers"]] == ["3+ years of Go"]
    assert score.result["gaps"] == ["Rust", "Trading systems experience"]
    assert score.experience_to_gaps == 1 and score.status == "blocked"


def test_blockers_must_carry_min_years(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    answer = claude_stream({**GOOD_SCORE, "blockers": [{"type": "visa", "severity": "check", "detail": "x"}]})
    assert "invalid output" in scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: answer)).error


@pytest.mark.parametrize("blocking_types, action", [
    (["eligibility"], "watch_for_reopening"),
    (["eligibility", "grade"], "skip"),
    (["enrolment"], "skip"),
    (["language"], "skip"),
])
def test_watch_for_reopening_only_for_eligibility_blocks(settings, blocking_types, action):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    blockers = [{"type": t, "severity": "blocking", "detail": t, "min_years": None} for t in blocking_types]
    blockers.append({"type": "visa", "severity": "check", "detail": "visa", "min_years": None})
    answer = claude_stream({**GOOD_SCORE, "action": "watch_for_reopening", "blockers": blockers})
    score = scoring.score_job(job(), prompt, runner=FakeClaude(lambda m: answer))
    assert score.result["action"] == action
    assert score.watch_to_skip == (action == "skip")


def test_usage_guard_budget(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)  # sonnet
    guard = scoring.UsageGuard(max_share=0.01, share_per_call={"sonnet": 0.004, "haiku": 0.0012})
    scores = scoring.score_many([job(source_id=str(i)) for i in range(10)], prompt, runner=FakeClaude(), guard=guard)
    assert len(scores) == 2 and "run budget reached" in guard.stopped


def test_usage_guard_stops_at_the_first_warning(settings):
    prompt = scoring.build_prompt(EXAMPLE_CONFIG, settings)
    guard = scoring.UsageGuard(stop_on_warning=True)
    fake = FakeClaude(lambda m: claude_stream(limit_status="allowed_warning"))
    assert len(scoring.score_many([job(source_id=str(i)) for i in range(5)], prompt, runner=fake, guard=guard)) == 1
    assert "nearly used" in guard.stopped or "used" in guard.stopped


def test_usage_guard_from_settings(settings):
    guard = scoring.UsageGuard.from_settings(settings, "backlog")
    assert guard.max_share == 0.4 and guard.share_per_call["haiku"] == 0.0012 and guard.stop_on_warning
