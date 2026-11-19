""""Fetch and score now" (2026-10-29): each step is a named run in its own process; the app polls."""

from __future__ import annotations

import sys
import time

from jobhunt.web import fetchnow


def wait_for(task, stages, seconds=10.0):
    end = time.monotonic() + seconds
    while task.stage not in stages and time.monotonic() < end:
        time.sleep(0.05)
    return task.stage


def fake(script: str):
    return lambda args: [sys.executable, "-c", script, *args]


def test_a_fetch_reports_progress_then_counts_and_a_confirmed_scoring_follows(tmp_path):
    seen = []
    runner = fetchnow.Runner(tmp_path, command=fake("import sys; print('[10:00:00] fetching · 1/1 (100%)'); seen = sys.argv[1:]; print(' '.join(seen))"))
    task = runner.start_fetch("Acme", "acme", after=lambda t: t.counts.update(found=3, waiting=2, windows=0.008))
    assert wait_for(task, {"fetched", "error"}) == "fetched" and task.counts["waiting"] == 2
    assert task.line == "fetching · 1/1 (100%)"
    runner.start_scoring(task, 0.01, after=lambda t: t.results.update(scored=2, seven_plus=1, six=0))
    assert wait_for(task, {"done", "error"}) == "done" and task.results["seven_plus"] == 1
    assert runner.get("acme") is task


def test_a_failed_run_is_an_error_with_its_last_words(tmp_path):
    runner = fetchnow.Runner(tmp_path, command=fake("print('The data is busy: a run since 10:00'); raise SystemExit(1)"))
    task = runner.start_fetch("Acme", "acme", after=lambda t: None)
    assert wait_for(task, {"fetched", "error"}) == "error" and "busy" in task.error


def test_the_add_sheet_offers_fetch_and_score_now(client):
    html = client.post("/wishlist/find", data={"name": "Mistral"}).text  # in the example companies.toml
    assert 'name="fetch_now"' in html and "Fetch and score now" in html
