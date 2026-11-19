"""Run summaries (`runs.jsonl`) as rows for the Runs page."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import accounts, actions_usage, pipeline


@dataclass
class RunRow:
    index: int  # position in runs.jsonl (newest last), used to open one run
    started_at: str
    ran_on: str
    kind: str
    seconds: float | None
    boards: dict[str, Counter] = field(default_factory=dict)  # fetched, new, closed, errors per board
    calls: int = 0
    deferred: int = 0
    usage: float = 0.0  # share of a 5-hour window
    alerts: int = 0
    errors: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    mine_failed: bool = False  # one of your companies (not the generated list) or a Claude call failed
    account: str = ""  # the Claude account it used (2026-11-18; older runs: the token's account)
    launchd: bool = False  # the laptop's scheduled run

    @property
    def totals(self) -> Counter:
        total: Counter = Counter()
        for counts in self.boards.values():
            total.update(counts)
        return total


def kind_of(run: dict[str, Any]) -> str:
    if run.get("kind"):
        return run["kind"]
    phases = run.get("scoring") or {}
    if run.get("backlog_stopped") or "backlog" in phases or "fit 6+ from earlier runs" in phases:
        return "backfill" if run.get("companies") or "backlog" in phases else "Sonnet only"
    return "run"


def row(index: int, run: dict[str, Any], share_per_call: dict[str, float], per_usd: float | None = None) -> RunRow:
    boards: dict[str, Counter] = defaultdict(Counter)
    errors: list[str] = []
    mine_failed = False
    for name, entry in (run.get("sources") or {}).items():
        if entry.get("rules") == "generated":  # counted in "generated" below; its error is named here
            if entry.get("error"):
                errors.append(f"{name}: {entry['error']}")
            continue
        counts = boards[entry.get("board") or "?"]
        counts["companies"] += 1
        for key in ("fetched", "new", "closed", "passed"):
            counts[key] += int(entry.get(key) or 0)
        if entry.get("error"):
            counts["errors"] += 1
            mine_failed = True
            errors.append(entry["error"] if str(entry["error"]).startswith(name) else f"{name}: {entry['error']}")
    for board, entry in (run.get("generated") or {}).items():
        counts = boards[board]
        for key in ("companies", "fetched", "new", "closed", "passed", "errors"):
            counts[key] += int(entry.get(key) or 0)
    if run.get("generated_errors"):
        errors.append(f"{len(run['generated_errors'])} generated-list companies failed to fetch")
    phases = pipeline.run_phases(run)
    named = {k: v for k, v in (run.get("scoring") or {}).items() if isinstance(v, dict)} \
        if "calls" not in (run.get("scoring") or {}) else {"scoring": run["scoring"]}
    stops = list(dict.fromkeys(str(p["stopped"]) for p in phases if p.get("stopped")))
    for p in phases:
        errors += [f"failed call: {f[:200]}" for f in p.get("failures") or []][:3]
        mine_failed = mine_failed or bool(p.get("failures"))
    notes = [run[k] for k in ("backlog_stopped", "interrupted", "weekly_warning_ignored", "weekly_warning_below")
             if run.get(k)]
    notes += pipeline.wait_notes(run)
    guard = run.get("weekly_guard") or {}
    if guard.get("paused_boards"):
        notes.append("Weekly guard on: " + ", ".join(pipeline.BOARD_NAMES.get(b, b) for b in guard["paused_boards"])
                     + " paused for the generated list"
                     + (f" ({guard['companies_not_fetched']} companies not fetched)" if guard.get("companies_not_fetched") else ""))
    seven = run.get("seven_day") or {}
    if isinstance(seven.get("utilization"), (int, float)) and not run.get("weekly_warning_ignored"):
        notes.append(f"Claude reported the weekly limit at {seven['utilization']:.0%}")
    alerts = run.get("alerts") or {}
    return RunRow(
        index=index, started_at=run.get("started_at", ""), ran_on=run.get("ran_on") or "laptop",
        kind=kind_of(run), seconds=run.get("seconds"), boards=dict(sorted(boards.items())),
        calls=sum(int(p.get("calls") or 0) for p in phases),
        deferred=sum(int(p.get("deferred") or 0) for p in phases if not p.get("titlecheck")),
        usage=round(sum(pipeline.phase_usage(p, share_per_call, per_usd) for p in phases), 4),
        alerts=int(alerts.get("sent") or 0) if isinstance(alerts, dict) else 0,
        errors=errors, stops=stops, notes=notes, record=run, mine_failed=mine_failed,
        # each phase with its usage as the tool now counts it (from its cost where recorded)
        phases={k: {**p, "usage": pipeline.phase_usage(p, share_per_call, per_usd)} for k, p in named.items()})


def cron_ticks(expr: str, start: datetime, hours: int = 48, timezone: str | None = None):
    """The times (UTC) at or after `start` matching a cron expression of the form "M H * * *", where
    H is "*", "*/N", "a,b" or a number (enough for the schedules this tool uses), read in
    `timezone` (UTC when none; summer time followed); none for other forms."""
    fields = expr.split()
    if len(fields) != 5 or not fields[0].isdigit() or fields[2:] != ["*", "*", "*"]:
        return
    minute, hour = int(fields[0]), fields[1]
    if hour == "*":
        wanted = set(range(24))
    elif hour.startswith("*/") and hour[2:].isdigit() and int(hour[2:]) > 0:
        wanted = set(range(0, 24, int(hour[2:])))
    elif all(h.isdigit() for h in hour.split(",")):
        wanted = {int(h) for h in hour.split(",")}
    else:
        return
    zone = ZoneInfo(timezone) if timezone else UTC
    local = start.astimezone(zone).replace(minute=minute, second=0, microsecond=0)
    for step in range(hours + 1):
        tick = (local.replace(tzinfo=None) + timedelta(hours=step)).replace(tzinfo=zone)
        moment = tick.astimezone(UTC)
        if moment >= start and tick.hour in wanted:
            yield moment


def health(runs: list[dict[str, Any]], workflow: str, now: datetime, external: str | None = None,
           external_timezone: str | None = None) -> dict[str, Any]:
    """Time since the last GitHub run finished (orange after 3 hours, red after 6) and when the
    next one is expected: the first tick of the external trigger (cron-job.org, `[schedule]
    external_trigger`) or of run.yml's backup schedule at least 100 minutes after it, since a run
    started sooner ends at once. While the Actions minutes guard spaces runs out (the latest run
    says so, 2026-11-18), the gap is its longer one and the alarms come 2 hours later."""
    github = [r for r in runs if r.get("ran_on") == "github" and r.get("started_at")]
    last, slow = None, False
    if github:
        latest = max(github, key=lambda r: r["started_at"])
        last = datetime.fromisoformat(latest["started_at"]) + timedelta(seconds=float(latest.get("seconds") or 0))
        slow = bool((latest.get("actions_minutes") or {}).get("slow"))
    gap = actions_usage.guard_settings(workflow)["slow_gap_minutes"] if slow else 100
    extra = 2 if slow else 0
    age = (now - last).total_seconds() / 3600 if last else None
    level = "none" if age is None else "red" if age >= 6 + extra else "orange" if age >= 3 + extra else "ok"
    schedules = [(expr, "cron-job.org", external_timezone) for expr in [external] if expr]
    schedules += [(m.group(1), "GitHub schedule", None) for m in [re.search(r'cron:\s*"([^"]+)"', workflow)] if m]
    start = max(now, last + timedelta(minutes=gap)) if last else now
    found = [(tick, by) for expr, by, zone in schedules for tick in [next(cron_ticks(expr, start, timezone=zone), None)]
             if tick]
    expected, by = min(found) if found else (None, None)
    return {"last": last.isoformat(timespec="seconds") if last else None, "hours": age, "level": level,
            "expected": expected.isoformat(timespec="seconds") if expected else None, "expected_by": by, "slow": slow}


PHASE_NAMES = {
    "scheduled": "Your companies and wishlist",
    "during fetch: your companies and wishlist": "Your companies and wishlist, while fetching",
    "during fetch: your companies and wishlist (marked for the other model)": "Your companies and wishlist, while fetching",
    "during fetch: generated (entry-level wording)": "Generated list, graduate roles, while fetching",
    "during fetch: generated (other roles)": "Generated list, other roles, while fetching",
    "during fetch: generated titlecheck": "Title check, while fetching",
    "during fetch: fit 6+ from earlier runs": "Sonnet for earlier Haiku 6+, while fetching",
    "before fetch: titlecheck": "Title check, before fetching",
    "generated titlecheck": "Title check, generated list",
    "generated": "Generated list, first score",
    "generated rescore first": "Generated list, Sonnet for earlier Haiku 6+",
    "wishlist, Sonnet for earlier Haiku scores": "Wishlist, Sonnet for earlier Haiku scores",
    "generated rescore": "Generated list, Sonnet for Haiku 6+",
    "your large companies titlecheck": "Title check, your large companies",
    "your large companies": "Your large companies, first score",
    "your large companies rescore": "Your large companies, Sonnet for Haiku 6+",
    "backlog": "Backlog, first score",
    "rescore": "Backlog, Sonnet for Haiku 6+",
    "fit 6+ from earlier runs": "Sonnet for earlier Haiku 6+",
    "your companies and wishlist, Sonnet directly": "Your companies and wishlist (Sonnet)",
}


def phase_name(key: str) -> str:
    return PHASE_NAMES.get(key, key[:1].upper() + key[1:])


def board_name(key: str) -> str:
    return pipeline.BOARD_NAMES.get(key, {"custom": "Custom", "jibe": "Jibe"}.get(key, key.capitalize()))


def triggers(minutes: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The run workflow's latest runs on GitHub, as the triggers that started them and what came of
    each: a run that ended within a minute found a run pushed less than 100 minutes before."""
    found = []
    for run in (minutes or {}).get("latest_runs") or []:
        by = {"workflow_dispatch": "cron-job.org", "schedule": "GitHub schedule"}.get(run.get("event"), run.get("event"))
        seconds = run.get("seconds")
        if run.get("conclusion") == "failure":
            outcome = "failed"
        elif run.get("conclusion") == "success" and seconds is not None and seconds < 60:
            outcome = "ended at once (a run had pushed under 100 minutes before)"
        elif run.get("conclusion") == "success":
            outcome = "ran"
        else:
            outcome = str(run.get("conclusion") or "?")
        found.append({"at": run.get("at"), "by": by, "outcome": outcome, "url": run.get("url"), "seconds": seconds})
    return found


def latest_seven_day(runs: list[dict[str, Any]], from_scores: dict[str, Any] | None) -> dict[str, Any] | None:
    found = [r["seven_day"] for r in runs if isinstance((r.get("seven_day") or {}).get("utilization"), (int, float))]
    candidates = found + ([from_scores] if from_scores else [])
    return max(candidates, key=lambda s: s.get("at", "")) if candidates else None


def rows(runs: list[dict[str, Any]], settings: dict[str, Any]) -> list[RunRow]:
    """Newest first."""
    share = {k: float(v) for k, v in settings.get("usage", {}).get("share_per_call", {}).items()}
    per_usd = pipeline.per_usd_of(settings)
    found = []
    for i, r in reversed(list(enumerate(runs))):
        item = row(i, r, share, per_usd)
        item.account, item.launchd = accounts.key_of(r, settings), bool(r.get("launchd"))
        found.append(item)
    return found
