"""Run health (2026-11-18): failures that would otherwise go unnoticed show on Today and send a
phone alert. On 2026-11-02 a harvest emptied the generated list and every run still succeeded,
fetching 44 companies instead of about 750, for 16 days.

Checked after each scheduled run:
- far fewer companies fetched than usual (under half the median of recent runs that were not
  themselves flagged, so a lasting drop does not become the new normal), unless the weekly guard
  paused boards on purpose;
- a company of mine (companies.toml or the wishlist) whose fetch failed in each of its last
  `IN_A_ROW` scheduled runs (runs that did not fetch it do not count either way).
Each alerts once when it starts and again once a day while it lasts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

DROP_SHARE = 0.5
BASELINE_RUNS = 100  # about 8 days of scheduled runs
MIN_BASELINE = 3
IN_A_ROW = 3
REMIND_AFTER = timedelta(hours=24)


def scheduled(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in runs if r.get("kind") == "scheduled"]


def flagged(run: dict[str, Any]) -> bool:
    return bool((run.get("health") or {}).get("companies_drop"))


def companies_drop(runs: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any] | None:
    """This run fetched far fewer companies than usual: {"fetched", "usual"}, or None."""
    if summary.get("kind") != "scheduled" or (summary.get("weekly_guard") or {}).get("paused_boards"):
        return None
    usual_runs = [r["companies"] for r in scheduled(runs)[-BASELINE_RUNS:]
                  if r.get("companies") and not flagged(r) and not (r.get("weekly_guard") or {}).get("paused_boards")]
    if len(usual_runs) < MIN_BASELINE:
        return None
    usual = median(usual_runs)
    fetched = int(summary.get("companies") or 0)
    return {"fetched": fetched, "usual": round(usual)} if fetched < DROP_SHARE * usual else None


def failing_in_a_row(runs: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """My companies whose fetch failed in this run and in the runs before it that fetched them,
    `IN_A_ROW` times or more: {name: {"runs": n, "error": latest error}}."""
    if summary.get("kind") != "scheduled":
        return {}
    history = scheduled(runs) + [summary]
    failing: dict[str, dict[str, Any]] = {}
    for name, entry in (summary.get("sources") or {}).items():
        if not entry.get("error"):
            continue
        count = 0
        for run in reversed(history):
            seen = (run.get("sources") or {}).get(name)
            if seen is None:
                continue  # not fetched in that run (spread across runs, or not yet configured)
            if not seen.get("error"):
                break
            count += 1
        if count >= IN_A_ROW:
            failing[name] = {"runs": count, "error": str(entry["error"])}
    return failing


def check(runs: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    """The run's health record (empty when all is well)."""
    found: dict[str, Any] = {}
    drop = companies_drop(runs, summary)
    if drop:
        found["companies_drop"] = drop
    failing = failing_in_a_row(runs, summary)
    if failing:
        found["failing"] = failing
    return found


def last_alert(runs: list[dict[str, Any]], what: str) -> datetime | None:
    """When the latest alert about `what` ("companies_drop", or a company's name) was sent."""
    for run in reversed(runs):
        if what in ((run.get("health") or {}).get("alerted") or []):
            return datetime.fromisoformat(run["started_at"])
    return None


def alerts_due(runs: list[dict[str, Any]], summary: dict[str, Any], now: datetime) -> list[tuple[str, str, str]]:
    """(what, title, text) for each problem not alerted about in the last day."""
    found = summary.get("health") or {}
    due: list[tuple[str, str, str]] = []

    def stale(what: str) -> bool:
        last = last_alert(runs, what)
        return last is None or now - last >= REMIND_AFTER
    drop = found.get("companies_drop")
    if drop and stale("companies_drop"):
        due.append(("companies_drop", "Runs fetch far fewer companies",
                    f"This run fetched {drop['fetched']} companies; usually about {drop['usual']}. Check the generated "
                    "list (companies_generated.jsonl) and the latest harvest."))
    for name, failing in (found.get("failing") or {}).items():
        if stale(name):
            due.append((name, f"{name} failed {failing['runs']} runs in a row", failing["error"][:200]))
    return due
