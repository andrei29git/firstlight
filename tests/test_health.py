"""Run health (2026-11-18): far fewer companies than usual, or a company of mine failing several
runs in a row, shows on Today and sends one phone alert a day."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from jobhunt import health, today

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def run(hours_ago: float, companies: int = 750, sources: dict | None = None, **extra) -> dict:
    return {"kind": "scheduled", "ran_on": "github", "started_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "companies": companies, "sources": sources or {}, **extra}


def test_far_fewer_companies_is_flagged_and_stays_flagged():
    runs = [run(h) for h in range(40, 20, -2)]
    assert health.companies_drop(runs, run(0, companies=700)) is None
    first = run(18, companies=44)
    first["health"] = health.check(runs, first)
    assert first["health"]["companies_drop"] == {"fetched": 44, "usual": 750}
    runs.append(first)
    # A week of low runs later, flagged runs are still left out of "usual": the drop stays visible.
    for h in range(16, 0, -2):
        low = run(h, companies=44)
        low["health"] = health.check(runs, low)
        runs.append(low)
    assert health.check(runs, run(0, companies=44))["companies_drop"]["usual"] == 750


def test_a_paused_board_or_a_laptop_run_is_not_a_drop():
    runs = [run(h) for h in range(20, 0, -2)]
    assert health.companies_drop(runs, run(0, companies=44, weekly_guard={"paused_boards": ["workday"]})) is None
    assert health.companies_drop(runs, {**run(0, companies=3), "kind": "named: Apple"}) is None


def test_failing_in_a_row_skips_runs_that_did_not_fetch_the_company():
    err = {"LSEG": {"error": "HTTP 429"}}
    runs = [run(8, sources={"LSEG": {"jobs": 3}}), run(6, sources=err), run(4, sources={}), run(2, sources=err)]
    assert health.failing_in_a_row(runs, run(0, sources=err)) == {"LSEG": {"runs": 3, "error": "HTTP 429"}}
    assert health.failing_in_a_row(runs[:3], run(0, sources=err)) == {}  # only 2 in a row


def test_alerts_once_a_day_while_it_lasts():
    summary = run(0, companies=44, health={"companies_drop": {"fetched": 44, "usual": 750}})
    assert [d[0] for d in health.alerts_due([], summary, NOW)] == ["companies_drop"]
    told = run(2, companies=44, health={"companies_drop": {}, "alerted": ["companies_drop"]})
    assert health.alerts_due([told], summary, NOW) == []
    told["started_at"] = (NOW - timedelta(hours=25)).isoformat()
    assert len(health.alerts_due([told], summary, NOW)) == 1


def test_today_shows_the_drop_a_failing_company_and_a_refused_harvest(tmp_path):
    err = {"LSEG": {"error": "HTTP 429 from https://lseg.wd3.myworkdayjobs.com"}}
    runs = [run(4, sources=err), run(2, sources=err),
            run(0, companies=44, sources=err, health={"companies_drop": {"fetched": 44, "usual": 750},
                                                      "failing": {"LSEG": {"runs": 3, "error": "HTTP 429"}}})]
    (tmp_path / "harvest.jsonl").write_text(json.dumps({"harvested_at": "2026-11-02T12:42:29+00:00", "relevant": 17338,
                                                        "refused": "21 Common Crawl index pages failed"}) + "\n")
    titles = {i.title: i.tone for i in today.tool_items({}, tmp_path, runs, "run.yml", None, None, NOW)}
    assert titles["Runs fetch far fewer companies than usual"] == "red"
    assert titles["LSEG: blocked on GitHub"] == "red"
    assert titles["This month's company harvest was not saved"] == "red"
