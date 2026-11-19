"""`eval`: score the hand-scored calibration roles and compare with the candidate's own answers.

Each role is scored several times (scores vary between calls) with its own example left out of
the prompt, so the model never sees the answer it is graded against. Only roles with a saved
posting (`posting = "calibration/<file>.txt"`, relative to the config folder) can be scored.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import scoring
from .sources.base import Job


@dataclass
class Case:
    example: dict[str, Any]
    expected_status: str
    expected_action: str
    scores: list[scoring.Score] = field(default_factory=list)

    @property
    def role(self) -> str:
        return self.example["role"]

    @property
    def fits(self) -> list[int]:
        return [s.result["fit"] for s in self.scores if s.result]

    @property
    def ok(self) -> list[scoring.Score]:
        return [s for s in self.scores if s.result]


def cases(config: Path, consider_start_date: bool) -> tuple[list[Case], list[str]]:
    """Calibration roles that have a posting file, and the roles that do not."""
    found, missing = [], []
    for ex in scoring.load_examples(config):
        posting = ex.get("posting")
        if not posting or not (config / posting).exists():
            missing.append(ex["role"])
            continue
        action = ex["action"] if consider_start_date else ex.get("action_without_timing", ex["action"])
        found.append(Case(ex, scoring.status(ex.get("blockers", []), consider_start_date), action))
    return found, missing


def run_eval(config: Path, settings: dict[str, Any], runs: int = 3, parallel: int = 4,
             runner: scoring.Runner = scoring.run_subprocess, timeout: float = 180.0,
             progress=None) -> tuple[list[Case], list[str]]:
    consider = bool(settings.get("scoring", {}).get("consider_start_date", False))
    found, missing = cases(config, consider)
    work = []
    for case in found:
        prompt = scoring.build_prompt(config, settings, exclude_role=case.role)
        job = Job(company="", title=case.role, url="", source="calibration", source_id=case.role,
                  description=(config / case.example["posting"]).read_text(encoding="utf-8"))
        work += [(case, job, prompt)] * runs
    if progress:
        progress.phase(f"eval: {settings.get('scoring', {}).get('model', '')}", total=len(work))

    def one(w):
        score = scoring.score_job(w[1], w[2], runner, timeout)
        if progress:
            progress.advance()
        return w[0], score

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for case, score in pool.map(one, work):
            case.scores.append(score)
    if progress:
        progress.done()
    return found, missing


# Graduation dates are deliberately absent: a graduation window is eligibility, not timing.
TIMING_WORDS = re.compile(r"start(?:s|ing)?\s+(?:date|now|immediately|in|on|before|by)|immediate\s+start|"
                          r"start-now|now-hire|intake|availab(?:le|ility)\s+(?:from|in|until)", re.IGNORECASE)


def timing_changed_action(result: dict[str, Any]) -> bool:
    """With start dates ignored, did timing decide the action anyway? A timing-only action, or a
    skip with no hard blocker whose explanation is about when the role starts."""
    if result["action"] in scoring.TIMING_ACTIONS:
        return True
    hard = [b for b in result["blockers"] if b["severity"] == "blocking" and b["type"] != "timing"]
    return result["action"] == "skip" and not hard and bool(TIMING_WORDS.search(result["explanation"]))


def agreement(found: list[Case], consider_start_date: bool = False) -> dict[str, Any]:
    def rate(hits: int, total: int) -> str:
        return f"{hits}/{total}" + (f" ({100 * hits / total:.0f}%)" if total else "")

    scored = [(c, s) for c in found for s in c.ok]
    confirmed_odds = [(c, s) for c, s in scored if c.example.get("odds_source") == "candidate"]
    spreads = [max(c.fits) - min(c.fits) for c in found if len(c.fits) > 1]
    return {
        "fit within 1": rate(sum(abs(s.result["fit"] - c.example["fit"]) <= 1 for c, s in scored), len(scored)),
        "status": rate(sum(s.status == c.expected_status for c, s in scored), len(scored)),
        "odds (all)": rate(sum(s.result["odds"] == c.example["odds"] for c, s in scored), len(scored)),
        "odds (confirmed by the candidate)": rate(sum(s.result["odds"] == c.example["odds"] for c, s in confirmed_odds),
                                                  len(confirmed_odds)),
        "action": rate(sum(s.result["action"] == c.expected_action for c, s in scored), len(scored)),
        "watch_for_reopening corrected to skip": rate(sum(s.watch_to_skip > 0 for _, s in scored), len(scored)),
        "experience blockers without years, moved to gaps": rate(
            sum(s.experience_to_gaps > 0 for _, s in scored), len(scored)),
        **({} if consider_start_date else {
            "timing blockers reported (want 0)": rate(
                sum(s.timing_blockers_dropped > 0 for _, s in scored), len(scored)),
            "timing changed the action (want 0)": rate(
                sum(timing_changed_action(s.result) for _, s in scored), len(scored)),
        }),
        "mean fit spread": round(statistics.mean(spreads), 2) if spreads else None,
        "max fit spread": max(spreads) if spreads else None,
        "failed calls": sum(len(c.scores) - len(c.ok) for c in found),
        "cost_usd": round(sum(s.cost_usd for c in found for s in c.scores), 4),
    }


def save(found: list[Case], folder: Path) -> Path:
    """Every score with its full output, for looking into disagreements later."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps([{
        "role": c.role, "want": {"fit": c.example["fit"], "odds": c.example["odds"], "status": c.expected_status,
                                 "action": c.expected_action},
        "scores": [{"status": s.status, "error": s.error, "result": s.result,
                    "timing_blockers_dropped": s.timing_blockers_dropped,
                    "experience_to_gaps": s.experience_to_gaps, "watch_to_skip": s.watch_to_skip,
                    "timing_changed_action":
                    bool(s.result) and timing_changed_action(s.result)} for s in c.scores],
    } for c in found], indent=1, ensure_ascii=False), encoding="utf-8")
    return path
