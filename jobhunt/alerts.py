"""Phone alerts through ntfy for strong new roles (section 5.6).

A role alerts when the scheduled model (Sonnet) scored it in this run and it belongs on Apply now:
fit at the Apply now threshold or more (the Settings page, `preferences.jsonl` on the data branch,
2026-11-19), the suggested action is to apply now, it is not blocked and not in the US only. So
what reaches the phone always matches Apply now. It also must never have alerted before
(`alerted_at` on the job).
Roles scored earlier, for example by a backfill, never alert. The topic comes from the
NTFY_TOPIC environment variable (a GitHub secret); without it nothing is sent. Sending failures
are reported in the run summary and never fail the run.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import requests

from . import display, pipeline, preferences
from .state import State

Post = Callable[[str, dict[str, Any]], None]


def post_json(url: str, payload: dict[str, Any]) -> None:
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()


def notify(settings: dict[str, Any], title: str, text: str, post: Post | None = None, topic: str | None = None) -> bool:
    """One plain notification (not about a role). False when there is no topic or it failed."""
    topic = topic if topic is not None else os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return False
    server = str(settings.get("alerts", {}).get("ntfy_server", "https://ntfy.sh")).rstrip("/")
    try:
        (post or post_json)(server, {"topic": topic, "title": title, "message": text})
    except Exception:  # noqa: BLE001 (a failed notice is tried again by the next run)
        return False
    return True


def candidates(state: State, settings: dict[str, Any], since: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(job, score) for roles the scheduled model scored at or after `since`, worth an alert."""
    min_fit = preferences.apply_min_fit(state.root)
    allowed = set(settings.get("locations", {}).get("allowed_countries", []))
    model, _ = pipeline.models(settings)
    found = []
    for record in state.jobs.values():
        if record.get("alerted_at") or not pipeline.scorable(record):
            continue
        score = pipeline.current_score(state, record, model)
        if not score or not score.get("result") or score.get("scored_at", "") < since:
            continue
        result = score["result"]
        if not preferences.apply_now(result["fit"], result.get("action"), score.get("status"),
                                     preferences.record_group(record, allowed), min_fit):
            continue
        found.append((record, score))
    found.sort(key=lambda pair: (-pair[1]["result"]["fit"], pair[0]["company"].lower()))
    return found


def message(record: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    result = score["result"]
    place = "; ".join((record.get("merged_locations") or record["locations"])[:2])
    title = " · ".join(p for p in (f"Fit {result['fit']}", display.company_label(record["company"]), record["title"], place) if p)
    checks = [b["type"] for b in result["blockers"]]
    body = result["explanation"]
    if checks:
        body += f"\nCheck: {', '.join(dict.fromkeys(checks))}."
    if score.get("deadline"):
        body += f"\nDeadline {score['deadline']}."
    return {"title": title[:250], "message": body[:1000], "click": record["url"],
            "priority": 4 if result["fit"] >= 9 else 3, "tags": ["briefcase"]}


def send(state: State, settings: dict[str, Any], since: str, now: str, post: Post = post_json,
         topic: str | None = None) -> dict[str, Any]:
    alerts = settings.get("alerts", {})
    topic = topic if topic is not None else os.environ.get("NTFY_TOPIC", "")
    found = candidates(state, settings, since)
    if not found:
        return {"sent": 0}
    if not topic:
        return {"sent": 0, "waiting": len(found), "note": "NTFY_TOPIC is not set: no alerts sent"}
    server = str(alerts.get("ntfy_server", "https://ntfy.sh")).rstrip("/")
    limit = int(alerts.get("max_per_run", 5))
    sent, errors = 0, []
    for record, score in found[:limit]:
        try:
            post(server, {"topic": topic, **message(record, score)})
            sent += 1
        except requests.RequestException as e:
            errors.append(f"{record['key']}: {type(e).__name__}")
            continue
        record["alerted_at"] = now
        record["updated_at"] = now
    more = found[limit:]
    if more and not errors:
        try:
            post(server, {"topic": topic, "title": f"and {len(more)} more strong roles",
                          "message": "Open the Jobs page on your laptop: " + ", ".join(
                              f"{display.company_label(r['company'])} ({s['result']['fit']})" for r, s in more[:8]),
                          "priority": 3, "tags": ["briefcase"]})
            sent += 1
            for record, _ in more:  # summarised in one alert: they do not alert again
                record["alerted_at"] = now
                record["updated_at"] = now
        except requests.RequestException as e:
            errors.append(f"summary: {type(e).__name__}")
    return {"sent": sent, "roles": len(found), **({"errors": errors} if errors else {})}
