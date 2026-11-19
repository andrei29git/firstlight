"""Blocked companies: `blocked.jsonl` on the data branch (2026-10-27).

the user blocks a company in the app (a row's menu, or Job detail); the app pushes the change, and
every run reads it: blocked companies are not fetched or scored, and their roles show nowhere.
One line per company, keyed by its normalised name; unblocking keeps the line with `on = false`,
so the newest change wins when two copies are merged.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import prefilter
from .state import dumps, read_jsonl

FILE = "blocked.jsonl"


def company_id(name: str) -> str:  # the same key as the wishlist's
    return prefilter.normalise_company(name)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load(state_dir: Path) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in read_jsonl(state_dir / FILE)}


def active(state_dir: Path) -> list[dict[str, Any]]:
    return sorted((e for e in load(state_dir).values() if e.get("on")), key=lambda e: e["name"].lower())


def names(state_dir: Path) -> frozenset[str]:
    """Normalised names of the blocked companies."""
    return frozenset(e["id"] for e in active(state_dir))


def set_company(state_dir: Path, name: str, on: bool, now: str | None = None,
                was_wishlisted: bool | None = None) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("A company name is needed")
    entries = load(state_dir)
    key = company_id(name)
    old = entries.get(key, {})
    entry = {"id": key, "name": old.get("name") or name, "on": bool(on), "updated_at": now or now_iso(),
             # Blocking took it off the wishlist: Undo (unblocking) can put it back.
             "was_wishlisted": bool(was_wishlisted if was_wishlisted is not None else old.get("was_wishlisted"))}
    entries[key] = entry
    path = state_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE}.{os.getpid()}")
    tmp.write_bytes(dumps([entries[k] for k in sorted(entries)]))
    os.replace(tmp, path)
    return entry
