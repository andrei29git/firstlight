"""The user's wishlist companies: `wishlist.jsonl` on the data branch (decided 2026-10-25).

The app writes it and pushes it at once, so the next scheduled run sees a change; runs only read
it. One line per company, keyed by its normalised name; removing a company keeps its line with
`on = false`, so the newest change wins when two copies are merged.

A wishlist company with a board is checked every run and scored like a companies.toml company
(Sonnet directly, UK and US scored, no title check). One that is also on the generated list leaves
that list while wishlisted and goes back to its rules when removed; its scores stay. A company
without a board (none found) only gets the star, on roles found elsewhere.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import companies as co
from . import harvest, prefilter
from .companies import Company
from .state import dumps, read_jsonl

FILE = "wishlist.jsonl"
BOARD_FIELDS = ("board", "slug", "url", "region", "listing_url", "link_pattern", "location_pattern", "partial",
                "postings_only", "fetch_from")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def company_id(name: str) -> str:
    return prefilter.normalise_company(name)


def load(state_dir: Path) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in read_jsonl(state_dir / FILE)}


def active(state_dir: Path) -> list[dict[str, Any]]:
    return sorted((e for e in load(state_dir).values() if e.get("on")), key=lambda e: e["name"].lower())


def names(state_dir: Path) -> frozenset[str]:
    """Normalised names of the wishlist companies (what the stars match on)."""
    return frozenset(e["id"] for e in active(state_dir))


def _write(state_dir: Path, entries: dict[str, dict[str, Any]]) -> None:
    path = state_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE}.{os.getpid()}")
    tmp.write_bytes(dumps([entries[k] for k in sorted(entries)]))
    os.replace(tmp, path)


def board_identity(config: dict[str, Any] | None) -> str | None:
    if config and config.get("board") == "custom" and config.get("listing_url") and config.get("link_pattern"):
        return f"custom:{config['listing_url']}"  # a careers site set up by the Add sheet
    if not config or not config.get("board") or not (config.get("slug") or config.get("url")):
        return None
    clean = {k: v for k, v in config.items() if k in BOARD_FIELDS and v}
    if clean.get("url"):
        clean["url"] = harvest.normalise_url(clean["board"], clean["url"])
    return harvest.identity(clean)


def set_company(state_dir: Path, name: str, on: bool, config: dict[str, Any] | None = None,
                source: str = "app", now: str | None = None) -> dict[str, Any]:
    """Add (or remove, with on=False) a company. Adding again keeps the board found before
    unless a new one is given."""
    name = name.strip()
    if not name:
        raise ValueError("A company name is needed")
    entries = load(state_dir)
    key = company_id(name)
    old = entries.get(key, {})
    board = {k: v for k, v in (config or {}).items() if k in BOARD_FIELDS and v} or old.get("config")
    entry = {"id": key, "name": old.get("name") if old and not config else name, "on": bool(on),
             "config": board or None, "identity": board_identity(board), "source": old.get("source", source),
             "added_at": old.get("added_at") if old.get("on") or not on else (now or now_iso()),
             "updated_at": now or now_iso()}
    entries[key] = entry
    _write(state_dir, entries)
    return entry


def companies(state_dir: Path, configured: set[str]) -> list[Company]:
    """Wishlist companies to fetch every run: those with a board that companies.toml does not
    already configure (those are fetched anyway)."""
    found = []
    for entry in active(state_dir):
        if entry.get("identity") and entry["identity"] not in configured:
            found.append(Company.from_dict({"name": entry["name"], **entry["config"], "origin": "wishlist",
                                            "wishlist": True, "identity": entry["identity"]}))
    return found


def identities(state_dir: Path) -> set[str]:
    return {e["identity"] for e in active(state_dir) if e.get("identity")}


def board_for(config: Path, state_dir: Path, company: str, source: str) -> dict[str, Any] | None:
    """The board settings of a company the tool already fetches: from companies.toml, else from
    the generated list (matched by name and board)."""
    wanted = company_id(company)
    for c in co.load_companies(config):
        if company_id(c.name) == wanted and c.board:
            return {k: v for k, v in {"board": c.board, "slug": c.slug, "url": c.url, "region": c.region}.items() if v}
    return next((g["config"] for g in generated_matches(state_dir, company) if g["config"]["board"] == source), None)


def generated_matches(state_dir: Path, company: str) -> list[dict[str, Any]]:
    """Generated-list companies with this name: their name, board settings and open roles."""
    wanted = company_id(company)
    found = []
    for entry in read_jsonl(state_dir / "companies_generated.jsonl"):
        name = entry.get("name") or entry.get("slug") or entry.get("url")
        if name and company_id(name) == wanted:
            found.append({"name": name, "config": {k: entry[k] for k in BOARD_FIELDS if entry.get(k)},
                          "open_roles": entry.get("open_roles"), "identity": entry["identity"]})
    return found


def import_toml(config: Path, state_dir: Path) -> list[str]:
    """Move `wishlist = true` flags from companies.toml into the wishlist (once). The flag lines are
    removed from the file; everything else in it stays as it was."""
    flagged = [c for c in co.load_companies(config) if c.wishlist]
    for c in flagged:
        board = {k: v for k, v in {"board": c.board, "slug": c.slug, "url": c.url, "region": c.region}.items() if v}
        set_company(state_dir, c.name, True, board or None, source="companies.toml")
    if flagged:
        path = config / co.FILE
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"(?m)^wishlist\s*=\s*true[ \t]*(#.*)?\n", "", text), encoding="utf-8")
    return [c.name for c in flagged]
