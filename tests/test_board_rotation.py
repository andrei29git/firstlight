"""Workable's generated companies (decided 2026-10-27): checked every 3 days at their usual time of
day, and only those with roles outside the UK and US."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from jobhunt import generated, pipeline
from jobhunt.companies import Company
from jobhunt.state import State, write_jsonl

from .test_generated import settings  # noqa: F401 (fixture)


def companies(board: str, n: int = 300) -> list[Company]:
    return [Company(name=f"Co {i}", board=board, slug=f"co{i}", origin="generated") for i in range(n)]


def test_a_board_checked_every_3_days_keeps_its_time_of_day():
    every = {"workable": 3}
    listed = companies("workable")
    seen: dict[str, list[tuple[int, int]]] = {}
    for day in range(6):
        for slot in range(12):
            for c in generated.due(listed, slot, day=day, every_days=every):
                seen.setdefault(c.name, []).append((day, slot))
    assert len(seen) == len(listed)
    for c in listed:
        days = [d for d, _ in seen[c.name]]
        assert len(days) == 2 and days[1] - days[0] == 3  # every third day
        assert {s for _, s in seen[c.name]} == {generated.slot(c)}  # same run of the day, same jobs file
    daily = companies("greenhouse", 50)
    assert sum(len(generated.due(daily, s, day=7, every_days=every)) for s in range(12)) == 50  # others daily


def test_the_harvest_count_leaves_out_companies_with_only_uk_and_us_roles(tmp_path, settings):  # noqa: F811
    rows = [{"identity": "workable:a", "board": "workable", "slug": "a", "outside_park_pass": 0},
            {"identity": "workable:b", "board": "workable", "slug": "b", "outside_park_pass": 3},
            {"identity": "workable:c", "board": "workable", "slug": "c"},  # checked before the count: unknown
            {"identity": "greenhouse:d", "board": "greenhouse", "slug": "d", "outside_park_pass": 0}]
    write_jsonl(tmp_path / generated.FILE, rows)
    rules = {**settings, "generated": {**settings["generated"], "outside_park_only": ["workable"]}}
    assert sorted(c.slug for c in generated.load(tmp_path, rules, set())) == ["b", "c", "d"]


def record(company: str, location: str, last_seen: str, **kw) -> dict:
    key = f"workable:{company}:{location}"
    return {"key": key, "company": company, "source": "workable", "origin": "generated", "title": "Graduate Engineer",
            "locations": [location], "prefilter": "pass", "closed": False, "last_seen": last_seen, **kw}


def test_stored_roles_decide_until_the_harvest_has_counted(tmp_path, settings):  # noqa: F811
    rules = {**settings, "generated": {**settings["generated"], "outside_park_only": ["workable"],
                                       "park_countries": ["GB", "US"]}}
    today = date(2026, 10, 28)
    state = State(tmp_path / "s")
    for r in [record("US Only", "New York, NY", "2026-10-27"),
              record("Has Berlin", "New York, NY", "2026-10-27"), record("Has Berlin", "Berlin, Germany", "2026-10-27"),
              record("Closed Paris", "Paris, France", "2026-10-27", closed=True),
              record("Old UK", "London, United Kingdom", "2026-09-01")]:
        state.jobs[r["key"]] = r
    skipped = pipeline.only_outside_park(state, rules, today)
    assert skipped == {("US Only", "workable"), ("Closed Paris", "workable")}  # Old UK: looked at again (30 days)
    none = {**rules, "generated": {**rules["generated"], "outside_park_only": []}}
    assert pipeline.only_outside_park(state, none, today) == set()  # no board listed: nothing skipped
