"""`shortlist`: the scored roles worth acting on, until the Jobs page exists.

Uses the scheduled model's score (Sonnet), never the backlog model's alone. Roles that already
have an application (any stage) are left out: matched by job key, or, for applications entered
without one, by company and normalised title. Read-only: it never changes the state or the
database, and a CSV it writes is a one-way snapshot.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import pipeline, prefilter, scoring
from .state import State


@dataclass(frozen=True)
class Applied:
    company: str
    role: str
    stage: str
    job_key: str | None

    def matches(self, job: dict[str, Any]) -> bool:
        if self.job_key:
            return self.job_key == job["key"]
        return (prefilter.normalise_company(self.company) == prefilter.normalise_company(job["company"])
                and prefilter.normalise_title(self.role) == prefilter.normalise_title(job["title"]))


def load_applied(database: Path) -> list[Applied]:
    """Every application with a stage, read-only. A missing database means none yet."""
    if not database.exists():
        return []
    import sqlite3

    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [Applied(*row) for row in conn.execute(
            "SELECT company, role, stage, job_key FROM applications WHERE stage != 'none'")]
    finally:
        conn.close()


ACTION_ORDER = ["apply_now", "apply_state_availability", "contact_recruiter_about_later_intake",
                "watch_for_reopening", "skip"]


@dataclass
class Row:
    job: dict[str, Any]
    score: dict[str, Any]
    stale: bool  # scored under an older prompt version

    @property
    def result(self) -> dict[str, Any]:
        return self.score["result"]

    @property
    def locations(self) -> str:
        return "; ".join(self.job.get("merged_locations") or self.job["locations"]) or "not stated"

    @property
    def deadline(self) -> str:
        return self.score.get("deadline") or self.job.get("deadline") or ""

    @property
    def blockers(self) -> str:
        return "; ".join(f"{b['type']} ({b['severity']}): {b['detail']}" for b in self.result["blockers"])


def rows(state: State, settings: dict[str, Any], current_version: str, min_fit: int = 7,
         actions: tuple[str, ...] = ("apply_now",), applied: list[Applied] | None = None,
         hidden: list[tuple[Row, Applied]] | None = None) -> list[Row]:
    """Matching roles, best first. Roles with an application go to `hidden` (if given) instead."""
    model, _ = pipeline.models(settings)
    found = []
    for job in state.jobs.values():
        if not pipeline.scorable(job) or not pipeline.has_result(state, job, model):
            continue
        score = pipeline.current_score(state, job, model)
        result = score["result"]
        if result["fit"] >= min_fit and result["action"] in actions:
            row = Row(job, score, stale=score.get("prompt_version") != current_version)
            match = next((a for a in applied or [] if a.matches(job)), None)
            if match is None:
                found.append(row)
            elif hidden is not None:
                hidden.append((row, match))
    found.sort(key=lambda r: (ACTION_ORDER.index(r.result["action"]), -r.result["fit"], r.deadline or "9999"))
    return found


def print_rows(found: list[Row]) -> None:
    if not found:
        print("No roles match.")
        return
    for row in found:
        r = row.result
        print(f"{r['fit']:>2}  {row.job['company']} · {row.job['title']}")
        print(f"    {row.locations}")
        details = [f"status {row.score['status']}", f"action {r['action']}"]
        if row.deadline:
            details.append(f"deadline {row.deadline}")
        if row.stale:
            details.append("scored under older rules")
        print("    " + " · ".join(details))
        if r["blockers"]:
            print(f"    blockers: {row.blockers}")
        print(f"    {row.job['url']}\n")
    print(f"{len(found)} roles.")


def write_csv(found: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Snapshot only: edits here are never read back"])
        writer.writerow(["fit", "action", "status", "company", "title", "locations", "deadline", "blockers",
                         "apply link", "odds", "explanation", "scored with", "scored at"])
        for row in found:
            r = row.result
            writer.writerow([r["fit"], r["action"], row.score["status"], row.job["company"], row.job["title"],
                             row.locations, row.deadline, row.blockers, row.job["url"], r["odds"],
                             r["explanation"], row.score["model"], row.score["scored_at"]])


def current_version(config: Path, settings: dict[str, Any]) -> str:
    return scoring.build_prompt(config, settings, model=pipeline.models(settings)[0]).version
