"""Setting up a company that is not tracked yet (2026-11-18): the Companies search's "Set it up"
and the wishlist's +.

The existing detection (known boards, slugs made from the name, a web search for the careers site,
a custom site) runs in a background thread, so closing the sheet does not stop it. What it found,
or why nothing could be set up, is kept in the tracker database (`meta.company_setups`) until the user
decides, so Today can list it and the app can say when a setup finished. Nothing is added to any
list before they choose."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .. import prefilter, store

KEY = "company_setups"

# Why a setup failed, in plain words, from the detection's notes (the order matters: the first match wins).
REASONS = [
    ("robots", re.compile(r"robots\.txt", re.I), "Its robots.txt asks tools not to read its job pages"),
    ("blocked", re.compile(r"bot check|cloudflare|captcha|access denied|refus|HTTP (?:401|403|406|429)", re.I),
     "Its site answers with a bot check or refuses tools"),
    ("javascript", re.compile(r"javascript|render", re.I), "Its jobs only appear after JavaScript runs"),
    ("no_adapter", re.compile(r"cannot read yet", re.I), "It uses a hiring system the tool cannot read yet"),
    ("unreachable", re.compile(r"timed out|did not answer|could not be read|connection|HTTP 5\d\d", re.I),
     "Its site did not answer"),
]
NO_BOARD = ("no_board", "No job board or careers page with jobs was found")


def reason_for(notes: list[str]) -> tuple[str, str]:
    text = " ".join(notes)
    for key, pattern, sentence in REASONS:
        if pattern.search(text):
            return key, sentence
    return NO_BOARD


def setup_id(name: str) -> str:
    return prefilter.normalise_company(name)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- Records kept until the user decides -------------------------------------------------------------

def load(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(conn: sqlite3.Connection, records: dict[str, dict[str, Any]]) -> None:
    value = json.dumps(records, ensure_ascii=False)
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                     (KEY, value, value))


def put(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    records = load(conn)
    records[record["id"]] = record
    save(conn, records)


def drop(conn: sqlite3.Connection, company_id: str) -> dict[str, Any] | None:
    records = load(conn)
    gone = records.pop(company_id, None)
    save(conn, records)
    return gone


# --- The background work --------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    name: str
    url: str
    want: str  # "covered" or "wishlist": which choice the sheet puts first
    steps: list[str] = field(default_factory=list)
    running: bool = True
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class Setups:
    """One setup per company at a time; finished ones are announced once (the app's notice)."""

    def __init__(self, database: Path):
        self.database = database
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()

    def running(self) -> list[Task]:
        with self.lock:
            return [t for t in self.tasks.values() if t.running]

    def get(self, company_id: str) -> Task | None:
        with self.lock:
            return self.tasks.get(company_id)

    def start(self, name: str, url: str, want: str, work: Callable[[str, str | None, dict], None]) -> Task:
        """`work(name, url, context)` fills context["options"] and context["notes"] (and may call
        context["progress"](text) as it goes)."""
        company_id = setup_id(name)
        with self.lock:
            current = self.tasks.get(company_id)
            if current and current.running:
                return current
            task = Task(company_id, name, url, want)
            self.tasks[company_id] = task

        def run() -> None:
            context: dict[str, Any] = {"options": [], "notes": [], "progress": task.steps.append}
            try:
                work(name, url or None, context)
            except Exception as e:  # noqa: BLE001  (a crash is reported like any other failure)
                context["notes"].append(f"The set-up stopped: {e}")
            record = self.result(task, context)
            conn = store.connect(self.database)
            try:
                put(conn, record)
            finally:
                conn.close()
            with self.lock:
                task.running = False

        threading.Thread(target=run, name=f"setup {name}", daemon=True).start()
        return task

    @staticmethod
    def result(task: Task, context: dict[str, Any]) -> dict[str, Any]:
        options = [dict(o) for o in context["options"]]
        record = {"id": task.id, "name": task.name, "url": task.url, "want": task.want, "at": now_iso(),
                  "options": options, "notes": list(context["notes"]), "steps": list(task.steps),
                  "searched": bool(context.get("searched")), "announced": False}
        if options:
            record["status"] = "found"
        else:
            record["status"] = "failed"
            record["reason"], record["reason_text"] = reason_for(record["notes"])
            record["careers_url"] = task.url or next(
                (m.group(0) for n in record["notes"] for m in [re.search(r"https?://\S+?(?=[\s,;)]|$)", n)] if m), "")
        return record


def announcements(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Setups that finished since the app last said so; marked as said."""
    records = load(conn)
    new = [r for r in records.values() if not r.get("announced") and r.get("status") in ("found", "failed")]
    if new:
        for r in new:
            r["announced"] = True
        save(conn, records)
    return new


def announce(record: dict[str, Any]) -> str:
    if record["status"] == "found":
        first = record["options"][0]["label"] if record["options"] else ""
        return f"{record['name']}: found {first}. Review it to add it."
    return f"{record['name']} could not be set up: {record['reason_text'].lower()}."
