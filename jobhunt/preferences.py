"""Preferences set on the Settings page that runs need too (2026-11-19): `preferences.jsonl` on the
data branch, which the app writes and pushes at once (like the wishlist) and every run reads after
its pull. One line per preference, merged by its newest `updated_at`.

- `apply_min_fit`: the Apply now threshold. The same number decides phone alerts, so what reaches
  the phone always matches Apply now (`apply_now` below).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import prefilter
from .state import read_jsonl, write_jsonl

FILE = "preferences.jsonl"
DEFAULTS: dict[str, Any] = {"apply_min_fit": 7}
LIMITS = {"apply_min_fit": (1, 10)}


def load(state_dir: Path) -> dict[str, Any]:
    return {r["id"]: r.get("value") for r in read_jsonl(state_dir / FILE) if "id" in r}


def get(state_dir: Path, key: str) -> Any:
    value = load(state_dir).get(key, DEFAULTS[key])
    low, high = LIMITS.get(key, (None, None))
    if low is not None:
        try:
            value = min(high, max(low, int(value)))
        except (TypeError, ValueError):
            value = DEFAULTS[key]
    return value


def set_value(state_dir: Path, key: str, value: Any, now: str | None = None) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"Unknown preference {key}")
    low, high = LIMITS.get(key, (None, None))
    if low is not None and not (low <= int(value) <= high):
        raise ValueError(f"{key} must be between {low} and {high}")
    records = {r["id"]: r for r in read_jsonl(state_dir / FILE) if "id" in r}
    records[key] = {"id": key, "value": value, "updated_at": now or datetime.now(UTC).isoformat(timespec="seconds")}
    state_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(state_dir / FILE, sorted(records.values(), key=lambda r: r["id"]))


def apply_min_fit(state_dir: Path) -> int:
    return int(get(state_dir, "apply_min_fit"))


def apply_now(fit: int | None, action: str | None, status: str | None, group: str, min_fit: int) -> bool:
    """Apply now, and a phone alert: fit at the threshold or more, the suggested action is to apply
    now, it is not blocked, and it is not in the US only."""
    return (fit or 0) >= min_fit and action == "apply_now" and status != "blocked" and group != "US"


def record_group(record: dict[str, Any], allowed: set[str]) -> str:
    return prefilter.country_group(record.get("merged_locations") or record.get("locations") or [], allowed)
