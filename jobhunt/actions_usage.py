"""GitHub Actions minutes used this month, for the Runs page (against the 3,000 included minutes).

GitHub's billing report needs a token scope the CLI login does not have, and the per-run timing
endpoint reports zeros, so the minutes are counted from each run's jobs: billing rounds every
job up to whole minutes (Linux runners count once). Read through the `gh` CLI (already logged in
on the laptop); finished runs are cached in a small local file, so each is asked about once.
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

INCLUDED_MINUTES = 3000
REFRESH_SECONDS = 30 * 60
PACE_SECONDS = 3 * 24 * 3600  # the projection follows the pace of the last 3 days

Api = Callable[[str], Any]  # path -> parsed JSON (tests pass a fake)


def gh_api(path: str) -> Any:
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[:300] or "gh api failed")
    return json.loads(done.stdout)


def repo_slug(root: Path) -> str | None:
    """owner/repo of the project's GitHub remote."""
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if "github.com" not in url:
        return None
    return url.split("github.com", 1)[1].lstrip(":/").removesuffix(".git") or None


def billed_minutes(jobs: list[dict[str, Any]]) -> int:
    total = 0
    for job in jobs:
        if job.get("started_at") and job.get("completed_at"):
            seconds = (datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
                       - datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))).total_seconds()
            total += max(1, math.ceil(seconds / 60)) if seconds > 0 else 0
    return total


def month_usage(slug: str, cache_file: Path, api: Api = gh_api, now: datetime | None = None,
                progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Minutes per workflow this month. Finished runs come from the cache; others are asked.
    `progress` sees the figure so far after each run (the first count asks about every run)."""
    now = now or datetime.now(UTC)
    month = now.strftime("%Y-%m")
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    runs: dict[str, Any] = {k: v for k, v in cache.get("runs", {}).items() if v.get("month") == month}
    page = 1
    last_dispatch = (cache.get("result") or {}).get("last_dispatch") or ""  # the external trigger's latest run
    finished: list[dict[str, Any]] = []  # the run workflow's finished runs, for Today
    while True:
        data = api(f"/repos/{slug}/actions/runs?created=>={month}-01&per_page=100&page={page}")
        for run in data.get("workflow_runs", []):
            key = str(run["id"])
            if run.get("event") == "workflow_dispatch" and run.get("name") == "run":
                last_dispatch = max(last_dispatch, str(run.get("created_at") or ""))
            if run.get("name") == "run" and run.get("status") == "completed":
                seconds = None
                if run.get("run_started_at") and run.get("updated_at"):
                    seconds = (datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                               - datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))).total_seconds()
                finished.append({"at": run.get("created_at"), "conclusion": run.get("conclusion"),
                                 "url": run.get("html_url"), "event": run.get("event"), "seconds": seconds})
            if key in runs and runs[key].get("final"):
                continue
            jobs = api(f"/repos/{slug}/actions/runs/{run['id']}/jobs").get("jobs", [])
            runs[key] = {"month": month, "workflow": run.get("name") or "?", "minutes": billed_minutes(jobs),
                         "final": run.get("status") == "completed", "started": run.get("run_started_at")}
            if progress:
                progress({"minutes": sum(r["minutes"] for r in runs.values()), "runs_counted": len(runs),
                          "runs_total": max(int(data.get("total_count") or 0), len(runs))})
                _save(cache_file, runs, cache.get("result"))
        if len(data.get("workflow_runs", [])) < 100:
            break
        page += 1
    by_workflow: dict[str, int] = {}
    for run in runs.values():
        by_workflow[run["workflow"]] = by_workflow.get(run["workflow"], 0) + run["minutes"]
    result = {"month": month, "minutes": sum(by_workflow.values()), "included": INCLUDED_MINUTES,
              "projected": projection(list(runs.values()), now),
              "by_workflow": dict(sorted(by_workflow.items())), "runs": len(runs),
              "updated_at": now.isoformat(timespec="seconds"), "last_dispatch": last_dispatch or None,
              "latest_runs": sorted(finished, key=lambda r: r["at"] or "", reverse=True)[:10]}
    _save(cache_file, runs, result)
    return result


def projection(runs: list[dict[str, Any]], now: datetime) -> int:
    """This month's minutes at its end, at the pace of the last 3 days (the same rule as run.yml's
    guard, which reads it from GitHub's run list, 2026-11-18)."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = month_start.replace(month=month_start.month % 12 + 1, year=month_start.year + (month_start.month == 12))
    used = sum(int(r.get("minutes") or 0) for r in runs)
    window = min((now - month_start).total_seconds(), PACE_SECONDS)
    recent = sum(int(r.get("minutes") or 0) for r in runs if r.get("started")
                 and (now - datetime.fromisoformat(str(r["started"]).replace("Z", "+00:00"))).total_seconds() < PACE_SECONDS)
    return round(used + (recent / window if window > 0 else 0) * (next_month - now).total_seconds())


def guard_settings(workflow: str) -> dict[str, int]:
    """The minutes guard's thresholds, read from run.yml so there is one place to change them."""
    import re
    found = {k: int(v) for k, v in re.findall(r"^\s*(ACTIONS_[A-Z_]+):\s*(\d+)\s*$", workflow, re.M)}
    return {"included": found.get("ACTIONS_INCLUDED", INCLUDED_MINUTES),
            "slow_above": found.get("ACTIONS_SLOW_ABOVE", 2800), "slow_gap_minutes": found.get("ACTIONS_SLOW_GAP_MINUTES", 220)}


def _save(cache_file: Path, runs: dict[str, Any], result: dict[str, Any] | None) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_name(cache_file.name + ".tmp")
    tmp.write_text(json.dumps({"runs": runs, "result": result}))
    tmp.replace(cache_file)


class ActionsMinutes:
    """The Runs page's view of this month's minutes, refreshed in the background."""

    def __init__(self, root: Path, cache_file: Path, api: Api = gh_api):
        self.root, self.cache_file, self.api = root, cache_file, api
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.counting: dict[str, Any] | None = None  # the figure so far while a count runs
        self._checked = 0.0
        self._lock = threading.Lock()
        if cache_file.exists():
            try:
                self.result = json.loads(cache_file.read_text()).get("result")
            except ValueError:
                pass

    def refresh(self, wait: bool = False) -> None:
        if time.monotonic() - self._checked < REFRESH_SECONDS and self._checked:
            return
        if not self._lock.acquire(blocking=wait):
            return
        self._checked = time.monotonic()

        def work() -> None:
            try:
                slug = repo_slug(self.root)
                if not slug:
                    self.error = "no GitHub remote found"
                    return
                self.counting = {"minutes": 0, "runs_counted": 0, "runs_total": 0}
                self.result = month_usage(slug, self.cache_file, self.api,
                                          progress=lambda p: setattr(self, "counting", p))
                self.error = None
            except Exception as e:  # noqa: BLE001 (shown on the page, never breaks it)
                self.error = f"could not read Actions usage ({str(e)[:200]})"
            finally:
                self.counting = None
                self._lock.release()
        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
