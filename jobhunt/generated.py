"""The generated company list (from `harvest`) as fetchable companies, and which are due this run.

Generated companies are checked once a day: each has a stable slot from a hash of its identity,
and a run fetches the slot that matches its time of day (12 slots for runs every 2 hours).
Boards in `[generated] every_days` are checked every N days instead (decided 2026-10-27: Workable
every 3 days, at its slower delay), on the same time of day. Boards in `outside_park_only` keep
only companies with roles outside the parked countries (UK, US): as the harvest counted them, or,
for companies checked before it counted, as the roles runs stored (pipeline.run).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .companies import Company
from .state import SLOTS_PER_DAY, company_slot, read_jsonl

FILE = "companies_generated.jsonl"


def load(state_root: Path, settings: dict[str, Any], configured: set[str]) -> list[Company]:
    """Generated companies to fetch: not already in companies.toml, not on an excluded board."""
    generated = settings.get("generated", {})
    excluded = set(generated.get("exclude_boards", []))
    outside_only = set(generated.get("outside_park_only", []))
    companies = []
    for entry in read_jsonl(state_root / FILE):
        if entry["board"] in excluded or entry["identity"] in configured:
            continue
        if entry["board"] in outside_only and entry.get("outside_park_pass") == 0:
            continue  # the harvest found roles only in the UK and US
        config = {k: entry[k] for k in ("board", "slug", "url", "region") if entry.get(k)}
        if entry["board"] == "workday":  # searched for entry-level roles, not read in full
            config.update(search=generated.get("workday_search", ["graduate", "intern", "junior"]),
                          search_pages=int(generated.get("workday_search_pages", 2)))
        name = entry.get("name") or entry.get("slug") or entry.get("url")
        companies.append(Company.from_dict({"name": name, **config, "origin": "generated",
                                            "identity": entry["identity"]}))
    return companies


def slot(company: Company, slots: int = SLOTS_PER_DAY) -> int:
    """The run of the day that fetches this company; its jobs live in the matching state file."""
    return company_slot(company.board or "", company.name, slots)


def current_slot(now: datetime, slots: int = SLOTS_PER_DAY) -> int:
    return (now.hour * slots // 24) % slots


def spread(companies: list[Company], slot_index: int, every_runs: dict[str, int]) -> list[Company]:
    """Your companies and wishlist ones on a board fetched every N runs (`[fetching] every_runs`;
    Workday 2 since 2026-11-18, after its tenants answered a fetch every 2 hours with 429s and bot
    checks): each comes round on one run in N, the board's companies spread over the runs by name."""
    found = []
    for company in companies:
        every = max(1, int(every_runs.get(company.board or "", 1)))
        if every == 1 or company_slot(company.board or "", company.name, every) == slot_index % every:
            found.append(company)
    return found


def due(companies: list[Company], slot_index: int, slots: int = SLOTS_PER_DAY, day: int = 0,
        every_days: dict[str, int] | None = None) -> list[Company]:
    """The companies this run fetches. `day` is a day number (date.toordinal()); a board checked
    every N days has N times the slots, so each company keeps its time of day and comes round on
    one day in N."""
    found = []
    for company in companies:
        every = max(1, int((every_days or {}).get(company.board or "", 1)))
        wide = company_slot(company.board or "", company.name, slots * every)
        if wide % slots == slot_index and wide // slots == day % every:
            found.append(company)
    return found
