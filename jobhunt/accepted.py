"""Companies accepted from "Find more companies": `accepted_companies.jsonl` on the data branch
(2026-10-30).

The app writes it (like the wishlist and the block list) and pushes; every run reads it after its
pull. An accepted company is covered with the generated list's rules: fetched once a day in its
slot, graduate wording needed, title check, Haiku then Sonnet. One line per company, keyed by its
normalised name; removing keeps the line with `on = false`, so the newest change wins in a merge.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import wishlist
from .companies import Company
from .state import dumps, read_jsonl

FILE = "accepted_companies.jsonl"


def load(state_dir: Path) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in read_jsonl(state_dir / FILE)}


def active(state_dir: Path) -> list[dict[str, Any]]:
    return sorted((e for e in load(state_dir).values() if e.get("on")), key=lambda e: e["name"].lower())


def names(state_dir: Path) -> frozenset[str]:
    return frozenset(e["id"] for e in active(state_dir))


def identities(state_dir: Path) -> set[str]:
    return {e["identity"] for e in active(state_dir) if e.get("identity")}


def set_company(state_dir: Path, name: str, on: bool, config: dict[str, Any] | None = None,
                found: dict[str, Any] | None = None, now: str | None = None) -> dict[str, Any]:
    """Accept (or remove) a company with its board settings; `found` keeps what the search said."""
    name = name.strip()
    if not name:
        raise ValueError("A company name is needed")
    entries = load(state_dir)
    key = wishlist.company_id(name)
    old = entries.get(key, {})
    board = {k: v for k, v in (config or {}).items() if k in (*wishlist.BOARD_FIELDS, "countries") and v} or old.get("config")
    entry = {"id": key, "name": old.get("name") or name, "on": bool(on), "config": board or None,
             "identity": wishlist.board_identity(board), "found": found or old.get("found"),
             "updated_at": now or wishlist.now_iso()}
    entries[key] = entry
    path = state_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE}.{os.getpid()}")
    tmp.write_bytes(dumps([entries[k] for k in sorted(entries)]))
    os.replace(tmp, path)
    return entry


def companies(state_dir: Path, skip: set[str]) -> list[Company]:
    """Accepted companies to fetch in their daily slot (not those configured or wishlisted)."""
    return [Company.from_dict({"name": e["name"], **e["config"], "origin": "generated", "rules": "generated",
                                "identity": e["identity"]})
            for e in active(state_dir) if e.get("identity") and e["identity"] not in skip]
