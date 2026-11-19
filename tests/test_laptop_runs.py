"""The laptop's scheduled runs (2026-11-18): the launchd agent, GitHub's latest failures fetched on
the laptop too, the Runs tile, and Today's warning when no laptop run has happened for 36 hours."""

from __future__ import annotations

import argparse
import json
import plistlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from jobhunt import cli, launchd, pipeline, today
from jobhunt import config as cfg
from jobhunt.web.app import create_app

from .conftest import EXAMPLE_CONFIG

NOW = datetime(2026, 11, 18, 21, 0, tzinfo=UTC)


def laptop_run(hours_ago: float) -> dict:
    return {"started_at": (NOW - timedelta(hours=hours_ago)).isoformat(), "ran_on": "laptop", "seconds": 40,
            "companies": 4, "errors": 0, "kind": "run, generated list: due (local sites)", "sources": {}}


def test_the_agent_runs_the_local_sites_twice_a_day_kept_awake_and_logged(tmp_path):
    agent = launchd.plist(tmp_path, "/opt/uv", ["/opt", "/opt/claude/bin"])
    assert agent["Label"] == launchd.LABEL
    assert agent["StartCalendarInterval"] == [{"Hour": 8, "Minute": 0}, {"Hour": 20, "Minute": 0}]
    line = agent["ProgramArguments"][-1]
    assert "/usr/bin/caffeinate -i" in line and "jobhunt run --local-only" in line and f'cd "{tmp_path}"' in line
    assert agent["StandardOutPath"] == agent["StandardErrorPath"] == str(tmp_path / "logs" / "local-run.log")
    assert agent["EnvironmentVariables"]["PATH"].startswith("/opt:/opt/claude/bin")


def test_install_writes_the_agent_and_loads_it(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd.shutil, "which", lambda tool: f"/opt/bin/{tool}")
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    path = launchd.install(tmp_path / "repo", run=run, home=tmp_path / "home")
    assert plistlib.loads(path.read_bytes())["Label"] == launchd.LABEL and (tmp_path / "repo" / "logs").is_dir()
    assert [c[1] for c in calls] == ["bootout", "bootstrap"]
    assert launchd.uninstall(run=run, home=tmp_path / "home") and not path.exists()


def test_github_failures_are_fetched_on_the_laptop_too(tmp_path):
    runs = [{"ran_on": "github", "sources": {"Mistral": {"error": "HTTP 429"}, "Winton": {"jobs": 3}}},
            {"ran_on": "github", "sources": {"Winton": {"error": "HTTP 429"}}},
            {"ran_on": "github", "sources": {"Winton": {"jobs": 2}}}]
    assert pipeline.github_failed_latest(runs) == {"Mistral"}  # Winton worked again since
    state = tmp_path / "state"
    state.mkdir()
    (state / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    paths = cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "db", backups=tmp_path / "b")
    args = argparse.Namespace(no_fetch=False, sonnet_only=False, names=[], local_only=True, generated="none")
    said = []
    names = {c.name for c in cli.run_targets(args, paths, cfg.load_settings(EXAMPLE_CONFIG), state, say=said.append)}
    assert "Mistral" in names and "Winton" not in names
    assert any("latest GitHub fetch failed: Mistral" in s for s in said)


def test_today_warns_when_the_laptop_has_not_run_for_36_hours(tmp_path):
    def items(runs):
        return [i for i in today.tool_items({}, tmp_path, runs, "", None, None, NOW, local_companies=["LSEG", "BlackRock"])
                if "laptop" in i.title]
    assert [i.title for i in items([])] == ["No laptop run yet"]
    assert items([laptop_run(10)]) == []
    late = items([laptop_run(40)])
    assert late[0].title == "No laptop run for 40 hours" and late[0].tone == "orange" and "LSEG" in late[0].detail
    # No company fetched from the laptop: no warning.
    assert all("laptop" not in i.title for i in today.tool_items({}, tmp_path, [], "", None, None, NOW))


def test_the_runs_page_shows_the_last_laptop_run(paths):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "runs.jsonl").write_text(json.dumps(laptop_run(3)) + "\n")
    with TestClient(create_app(paths)) as client:
        html = client.get("/runs").text
    assert "Last laptop run" in html and "4 companies" in html
