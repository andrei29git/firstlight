"""Claude accounts and the Actions minutes guard (2026-11-18): GitHub's runs and the laptop's launchd
job use the coding account's token; other laptop runs use whoever is logged in. Weekly figures and
guards stay per account, and scheduled runs slow down when the month's minutes run short."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from jobhunt import accounts, actions_usage, launchd, pipeline, runs_view, today
from jobhunt.state import State
from jobhunt.web.app import create_app

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)
SETTINGS = {"accounts": {"token": "coding", "orgs": {"org-personal-1234": "personal"}}}
STATUS = {"loggedIn": True, "authMethod": "claude.ai", "email": "someone@example.com", "orgId": "org-personal-1234",
          "subscriptionType": "pro"}


def reading(utilization: float, at: datetime, account: str | None) -> dict:
    record = {"scored_at": at.isoformat(), "rate_limit": {"unifiedWindows": {"seven_day": {
        "utilization": utilization, "resetsAt": int((NOW + timedelta(days=3)).timestamp())}}}}
    return {**record, "account": account} if account else record


def test_a_token_is_the_coding_account_and_a_login_is_named_by_its_organisation():
    token = accounts.detect(SETTINGS, env={accounts.TOKEN_ENV: "secret"}, status=lambda: STATUS)
    assert token["key"] == "coding" and token["how"] == "token" and "secret" not in json.dumps(token)
    login = accounts.detect(SETTINGS, env={}, status=lambda: STATUS)
    assert (login["key"], login["plan"]) == ("personal", "pro") and "email" not in login  # never the email
    other = accounts.detect(SETTINGS, env={}, status=lambda: {**STATUS, "orgId": "abcdef12-0000"})
    assert other["key"] == "account abcdef12"
    assert accounts.detect(SETTINGS, env={}, status=lambda: None)["key"] == "unknown"


def test_weekly_figures_and_the_guard_keep_accounts_apart(tmp_path):
    state = State(tmp_path / "state")
    state.scores.update({"a": reading(0.30, NOW - timedelta(hours=5), None),  # before accounts: coding
                         "b": reading(0.80, NOW - timedelta(hours=1), "personal")})
    assert pipeline.claude_week(state, NOW, "coding", SETTINGS)["utilization"] == 0.30
    assert pipeline.claude_week(state, NOW, "personal", SETTINGS)["utilization"] == 0.80
    weeks = pipeline.claude_week_by_account(state, NOW, SETTINGS)
    assert list(weeks) == ["coding", "personal"]  # the token's account first
    settings = {**SETTINGS, "usage": {"weekly_guard_share": 0.75, "weekly_guard_off_share": 0.65}}
    assert not pipeline.weekly_guard(state, settings, NOW)["filter_on"]  # the personal 80% does not count
    assert pipeline.weekly_guard(state, settings, NOW, account="personal")["filter_on"]


def test_runs_and_scores_record_the_account(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "ACCOUNT", {"key": "personal"})
    from jobhunt import scoring
    state = State(tmp_path / "state")
    score = scoring.Score(key="k", description_hash="h", prompt_version="v", model="m", result={"fit": 5},
                          status="clear", deadline=None, scored_at=NOW.isoformat(), seconds=1.0, cost_usd=0.0,
                          tokens={}, rate_limit=None)
    pipeline.store_score(state, score)
    assert state.scores[score.id]["account"] == "personal"
    rows = runs_view.rows([{"started_at": NOW.isoformat(), "ran_on": "laptop", "account": {"key": "personal"}},
                           {"started_at": NOW.isoformat(), "ran_on": "github"}], SETTINGS)
    assert [r.account for r in rows] == ["coding", "personal"]


def test_the_launchd_job_uses_the_token_in_the_keychain():
    line = launchd.script(Path("/repo"), "/opt/uv")
    assert f"find-generic-password -s {launchd.KEYCHAIN_SERVICE} -w" in line
    assert 'export CLAUDE_CODE_OAUTH_TOKEN="$token"' in line and "export JOBHUNT_LAUNCHD=1" in line
    assert line.index("unset token") < line.index("exec ")  # the token is not kept in the shell's variables


def test_today_warns_when_the_launchd_run_used_another_account(tmp_path):
    def items(account):
        run = {"started_at": (NOW - timedelta(hours=2)).isoformat(), "ran_on": "laptop", "launchd": True,
               "kind": "run, generated list: due (local sites)", "account": {"key": account}}
        return [i for i in today.tool_items(SETTINGS, tmp_path, [run], "", None, None, NOW) if "scheduled run used" in i.title]
    assert [i.title for i in items("personal")] == ["The laptop's scheduled run used personal, not coding"]
    assert items("coding") == []


def test_the_minutes_projection_follows_the_last_three_days():
    now = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    runs = [{"minutes": 1000, "started": "2026-11-05T10:00:00Z"},
            {"minutes": 300, "started": (now - timedelta(days=1)).isoformat()}]  # 100 a day lately
    assert actions_usage.projection(runs, now) == 1300 + 1100  # 11 days left at 100 a day


def test_the_guard_reads_its_thresholds_from_the_workflow_and_spaces_runs_out():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "run.yml"
    text = workflow.read_text() if workflow.exists() else "ACTIONS_SLOW_ABOVE: 2800\nACTIONS_SLOW_GAP_MINUTES: 220\n"
    assert actions_usage.guard_settings(text) == {"included": 3000, "slow_above": 2800, "slow_gap_minutes": 220}
    run = {"ran_on": "github", "started_at": (NOW - timedelta(hours=4)).isoformat(), "seconds": 60}
    normal = runs_view.health([run], text, NOW)
    slowed = runs_view.health([{**run, "actions_minutes": {"projected": 2900, "slow": True}}], text, NOW)
    assert normal["level"] == "orange" and slowed["level"] == "ok" and slowed["slow"]


def test_the_runs_page_shows_each_run_s_account_and_the_projection(paths):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "runs.jsonl").write_text(json.dumps(
        {"started_at": NOW.isoformat(), "ran_on": "laptop", "seconds": 30, "account": {"key": "personal"}}) + "\n")
    with TestClient(create_app(paths)) as client:
        html = client.get("/runs").text
    assert 'class="secondary small run-account"' in html and ">personal<" in html
