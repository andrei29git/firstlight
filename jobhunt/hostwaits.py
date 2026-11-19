"""Hosts that asked to be left alone for a while (a Retry-After longer than a minute), kept on the
data branch (`host_waits.jsonl`, merged by host, newest change wins) so every run and harvest,
on GitHub or the laptop, leaves them alone until the time has passed.

A board whose host refuses again while it is already on a longer delay (`[fetching] host_delays`)
is taken off the generated list (decided 2026-10-27 for Workable): the record keeps `slow = true`
and its companies are no longer fetched or checked, until the record is removed by hand. The
first run that sees it tells the user through ntfy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .sources.http import WAITS, HostWaits
from .state import read_jsonl, write_jsonl

FILE = "host_waits.jsonl"
# The host each board's listing is fetched from, as the fetcher keys its delays and waits.
BOARD_HOSTS = {"greenhouse": "boards-api.greenhouse.io", "ashby": "api.ashbyhq.com", "recruitee": ".recruitee.com",
               "personio": ".jobs.personio.de", "teamtailor": ".teamtailor.com", "join": "join.com",
               "workable": "apply.workable.com", "smartrecruiters": "api.smartrecruiters.com",
               "homerun": "feed.homerun.co"}


def load(state_root: Path, waits: HostWaits = WAITS) -> None:
    waits.load(read_jsonl(state_root / FILE))


def save(state_root: Path, waits: HostWaits = WAITS) -> None:
    records = waits.records()
    if records or (state_root / FILE).exists():
        write_jsonl(state_root / FILE, records)


def waiting(waits: HostWaits = WAITS) -> dict[str, datetime]:
    """Hosts under a wait now, with the time it ends."""
    found = {}
    for record in waits.records():
        until = waits.until(record["host"])
        if until:
            found[record["host"]] = until
    return found


def dropped_boards(waits: HostWaits = WAITS) -> dict[str, dict[str, Any]]:
    """Boards taken off the generated list: their host refused again on its longer delay."""
    slow = {r["host"]: r for r in waits.records() if r.get("slow")}
    return {board: slow[host] for board, host in BOARD_HOSTS.items() if host in slow}


def mark_notified(boards: list[str], waits: HostWaits = WAITS) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for host in {BOARD_HOSTS[b] for b in boards}:
        waits.update(host, notified=True, updated_at=now)
