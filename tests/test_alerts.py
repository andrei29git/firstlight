from __future__ import annotations

import pytest
import requests

from jobhunt import alerts, preferences, scoring
from jobhunt import config as cfg
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE

SINCE = "2026-10-25T10:00:00+00:00"


def add(state: State, key: str, fit: int, scored_at: str = "2026-10-25T10:05:00+00:00", model: str = "sonnet",
        blockers=(), **job):
    state.jobs[key] = {"key": key, "company": f"Co {key}", "title": "Graduate Engineer", "url": f"https://x/{key}",
                       "source": "greenhouse", "source_id": key, "locations": ["Paris, France"], "prefilter": "pass",
                       "closed": False, "duplicate_of": None, "description_hash": f"h{key}",
                       "first_seen": SINCE, "updated_at": SINCE, **job}
    state.scores[scoring.score_id(key, model)] = {
        "id": scoring.score_id(key, model), "key": key, "model": model, "description_hash": f"h{key}",
        "scored_at": scored_at, "status": scoring.status(list(blockers), False), "deadline": None,
        "result": {**GOOD_SCORE, "fit": fit, "blockers": list(blockers)}}


@pytest.fixture
def settings():
    return cfg.load_settings(EXAMPLE_CONFIG)


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state")
    preferences.set_value(tmp_path / "state", "apply_min_fit", 8)  # the Apply now threshold (Settings)
    add(s, "a", 9)
    add(s, "b", 8, blockers=[{"type": "visa", "severity": "check", "detail": "UK", "min_years": None}])
    add(s, "c", 7)                                                     # below min_fit
    add(s, "d", 9, blockers=[{"type": "language", "severity": "blocking", "detail": "Dutch", "min_years": None}])
    add(s, "e", 9, scored_at="2026-10-24T09:00:00+00:00")                # scored before this run (backfill)
    add(s, "f", 9, model="haiku")                                      # only the backlog model
    add(s, "g", 9, alerted_at="2026-10-24T09:00:00+00:00")             # alerted before
    return s


def test_only_strong_new_unblocked_roles_alert_once(state, settings):
    posts = []
    result = alerts.send(state, settings, since=SINCE, now="now", post=lambda url, p: posts.append(p), topic="t")
    assert [p["title"].split(" · ")[1] for p in posts] == ["Co a", "Co b"] and result == {"sent": 2, "roles": 2}
    assert posts[0]["click"] == "https://x/a" and posts[0]["topic"] == "t"
    assert posts[0]["title"] == "Fit 9 · Co a · Graduate Engineer · Paris, France"
    assert "Check: visa." in posts[1]["message"]
    assert state.jobs["a"]["alerted_at"] == "now"
    again = []
    alerts.send(state, settings, since=SINCE, now="later", post=lambda url, p: again.append(p), topic="t")
    assert again == []


def test_above_the_cap_one_summary(state, settings):
    for i in range(8):
        add(state, f"x{i}", 8)
    posts = []
    result = alerts.send(state, settings, since=SINCE, now="now", post=lambda url, p: posts.append(p), topic="t")
    assert len(posts) == 6 and posts[-1]["title"] == "and 5 more strong roles" and result["roles"] == 10
    assert all(r.get("alerted_at") for k, r in state.jobs.items() if k.startswith("x") or k in "ab")


def test_without_a_topic_nothing_is_sent_or_marked(state, settings):
    result = alerts.send(state, settings, since=SINCE, now="now", post=lambda url, p: 1 / 0, topic="")
    assert result["sent"] == 0 and result["waiting"] == 2 and not state.jobs["a"].get("alerted_at")


def test_a_failed_post_leaves_the_role_for_next_time(state, settings):
    def fail(url, payload):
        raise requests.ConnectionError("offline")
    result = alerts.send(state, settings, since=SINCE, now="now", post=fail, topic="t")
    assert result["sent"] == 0 and len(result["errors"]) == 2 and not state.jobs["a"].get("alerted_at")


def test_alerts_are_exactly_the_new_apply_now_roles(tmp_path, settings):
    """2026-11-19: one threshold for Apply now and the phone; a role alerts only if Apply now shows it."""
    s = State(tmp_path / "state")
    add(s, "now", 7)
    add(s, "skip", 9, **{})
    s.scores[scoring.score_id("skip", "sonnet")]["result"]["action"] = "skip"
    add(s, "us", 9, locations=["Austin, TX, United States"])
    found = [r["key"] for r, _ in alerts.candidates(s, settings, SINCE)]
    assert found == ["now"]  # the default threshold, 7; not a skip, not US-only
    preferences.set_value(tmp_path / "state", "apply_min_fit", 8)
    assert alerts.candidates(s, settings, SINCE) == []
