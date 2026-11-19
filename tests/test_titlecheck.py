from __future__ import annotations

import json

from jobhunt import config as cfg
from jobhunt import titlecheck

from .conftest import EXAMPLE_CONFIG
from .fakes import FakeClaude, claude_stream

PROFILE = (EXAMPLE_CONFIG / "profile.md").read_text()


def items(n: int) -> list[titlecheck.Item]:
    return [titlecheck.Item(key=f"k{i}", company="Acme", title=f"Role {i}", department="Eng") for i in range(n)]


def test_grounded_quotes_must_be_in_the_profile():
    assert titlecheck.grounded("Not interested in: mobile, game development", PROFILE)
    assert titlecheck.grounded("not interested in:  MOBILE, game development.", PROFILE)  # case and spacing
    assert not titlecheck.grounded("not interested in trading roles", PROFILE)  # invented
    assert not titlecheck.grounded("Go", PROFILE)  # too short to show anything


def test_drops_need_a_real_quote_and_batches_are_split():
    def respond(message):
        return claude_stream({"drop": [
            {"id": 0, "reason": "Mobile role", "profile_quote": "Not interested in: mobile, game development"},
            {"id": 1, "reason": "Trading", "profile_quote": "Not interested in trading roles"},
            {"id": 0, "reason": "duplicate", "profile_quote": "Not interested in: mobile, game development"}]})
    fake = FakeClaude(respond)
    settings = {**cfg.load_settings(EXAMPLE_CONFIG), "titlecheck": {"model": "haiku", "batch_size": 3}}
    out = titlecheck.check(items(5), EXAMPLE_CONFIG, settings, runner=fake)
    assert out.calls == 2 and len(fake.calls) == 2
    assert sorted(out.dropped_keys) == ["k0", "k3"]  # id 0 of each batch
    assert [d.key for d in out.rejected] == ["k1", "k4"]  # ungrounded: kept
    first = next(c["stdin"] for c in fake.calls if "Role 0" in c["stdin"])  # batches run in parallel
    assert "0. Role 0 | Acme | department: Eng" in first and "Role 3" not in first


def test_failed_batches_keep_their_roles():
    out = titlecheck.check(items(2), EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG),
                           runner=FakeClaude(lambda m: "garbage"))
    assert out.dropped_keys == set() and sorted(out.unchecked) == ["k0", "k1"] and out.failed_calls == 1


def test_out_of_range_ids_are_invalid():
    out = titlecheck.check(items(1), EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG),
                           runner=FakeClaude(lambda m: claude_stream({"drop": [{"id": 7, "reason": "x", "profile_quote": "y"}]})))
    assert out.unchecked == ["k0"]


def test_prompt_states_the_rules():
    prompt, _ = titlecheck.build_prompt(EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG))
    assert "profile_quote" in prompt.system and "When in doubt, keep" in prompt.system and prompt.model == "haiku"
    assert json.dumps(prompt.schema) == json.dumps(titlecheck.SCHEMA)
