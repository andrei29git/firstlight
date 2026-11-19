"""SQLite store.

The database is the only home of the user's own edits (stages, dates, notes), so it is backed up
on every start and once a day while the app runs, and nothing else ever writes to those tables.
"""

from __future__ import annotations

import hashlib
import re
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import stages

MIGRATIONS: tuple[str, ...] = (
    # 1: applications and their stage history
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE applications (
        id             INTEGER PRIMARY KEY,
        key            TEXT NOT NULL UNIQUE,
        job_key        TEXT,
        company        TEXT NOT NULL,
        role           TEXT NOT NULL,
        location       TEXT NOT NULL DEFAULT '',
        stage          TEXT NOT NULL,
        applied_on     TEXT,
        next_step_by   TEXT,
        notes          TEXT NOT NULL DEFAULT '',
        contact        TEXT NOT NULL DEFAULT '',
        cooldown_until TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE TABLE stage_history (
        id             INTEGER PRIMARY KEY,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        stage          TEXT NOT NULL,
        changed_at     TEXT NOT NULL,
        approximate    INTEGER NOT NULL DEFAULT 0,
        source         TEXT NOT NULL
    );
    CREATE INDEX stage_history_by_application ON stage_history(application_id, changed_at);
    """,
    # 2: the user's marks on found jobs (the pipeline never writes these)
    """
    CREATE TABLE job_marks (
        job_key     TEXT PRIMARY KEY,
        hidden      INTEGER NOT NULL DEFAULT 0,
        on_linkedin TEXT,             -- 'yes', 'no' or NULL (not checked)
        notes       TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX applications_by_job ON applications(job_key);
    """,
    # 3: when a role was hidden, and when it was removed (hidden for [tracker] hidden_days). The
    # row stays as the record that keeps a removed role off every page, even when runs fetch it again.
    """
    ALTER TABLE job_marks ADD COLUMN hidden_at TEXT;
    ALTER TABLE job_marks ADD COLUMN removed_at TEXT;
    UPDATE job_marks SET hidden_at = updated_at WHERE hidden = 1;
    """,
    # 4: application help (2026-10-28): the chat per role, drafts, the role's questions, and each
    # call's usage. The user's own data: only here, never on the data branch.
    """
    CREATE TABLE helper_messages (
        id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
        missing TEXT, created_at TEXT NOT NULL
    );
    CREATE INDEX helper_messages_by_job ON helper_messages(job_key, id);
    CREATE TABLE helper_drafts (
        job_key TEXT NOT NULL, kind TEXT NOT NULL, qid TEXT NOT NULL, answer TEXT NOT NULL,
        choices TEXT, missing TEXT, edited INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY (job_key, kind, qid)
    );
    CREATE TABLE helper_questions (
        job_key TEXT PRIMARY KEY, source TEXT NOT NULL, questions TEXT NOT NULL, letter_words TEXT,
        fetched_at TEXT NOT NULL
    );
    CREATE TABLE helper_calls (
        id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
        seconds REAL, cost_usd REAL, share REAL, error TEXT, created_at TEXT NOT NULL
    );
    """,
    # 5: the helper's questions as an editable list (2026-10-29): the question text each draft was
    # made for (a changed question marks its draft out of date), the facts the user gave for a draft
    # ("Needed from you"), and whether pasted questions were reviewed before drafting.
    """
    ALTER TABLE helper_drafts ADD COLUMN question TEXT;
    ALTER TABLE helper_drafts ADD COLUMN facts TEXT;
    ALTER TABLE helper_questions ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 1;
    """,
    # 6: how many questions a helper call drafted (2026-11-19), so the time shown while drafting
    # comes from recent calls.
    """
    ALTER TABLE helper_calls ADD COLUMN items INTEGER;
    """,
)

EDITABLE = ("stage", "applied_on", "next_step_by", "notes")


class StoreError(ValueError):
    """A request the store refuses, with a message fit to show to the user."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {number}")


def backup(conn: sqlite3.Connection, backups: Path, keep: int) -> Path:
    """Copy the live database with SQLite's backup API and prune old copies."""
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"jobhunt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with sqlite3.connect(target) as dest:
        conn.backup(dest)
    dest.close()
    for old in sorted(backups.glob("jobhunt-*.db"))[:-keep]:
        old.unlink()
    return target


# --- keys and validation ---------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def manual_key(company: str, role: str, location: str, applied_on: str | None) -> str:
    """Key for an application the tool did not find itself."""
    digest = hashlib.sha1(f"{role}|{location}|{applied_on or ''}".encode()).hexdigest()[:10]
    return f"manual:{_slug(company)}:{digest}"


def _date_or_none(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise StoreError(f"{field} must be a date (YYYY-MM-DD), got {value!r}") from None


def _stage(value: str) -> str:
    if not stages.is_valid(value):
        raise StoreError(f"Unknown stage {value!r}")
    return value


# --- writing ---------------------------------------------------------------------------------

def _insert_history(conn, application_id: int, stage: str, changed_at: str, approximate: bool, source: str):
    conn.execute(
        "INSERT INTO stage_history (application_id, stage, changed_at, approximate, source) VALUES (?, ?, ?, ?, ?)",
        (application_id, stage, changed_at, int(approximate), source),
    )


def import_seed(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Import applications made before the tool existed. Runs once per database; returns rows added."""
    if conn.execute("SELECT 1 FROM meta WHERE key = 'seed_imported'").fetchone():
        return 0
    added = 0
    now = now_iso()
    with conn:
        for row in rows:
            stage = _stage(row["stage"])
            applied_on = _date_or_none(row.get("applied_on"), "applied_on")
            location = row.get("location", "")
            key = manual_key(row["company"], row.get("role", ""), location, applied_on)
            cur = conn.execute(
                """INSERT INTO applications (key, company, role, location, stage, applied_on, notes, contact,
                                             cooldown_until, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, row["company"], row.get("role", ""), location, stage, applied_on, row.get("notes", ""),
                 row.get("contact", ""), _date_or_none(row.get("cooldown_until"), "cooldown_until"), now, now),
            )
            app_id = cur.lastrowid
            # The sheet only recorded the application date; later stage dates are unknown.
            if applied_on:
                _insert_history(conn, app_id, "applied", applied_on, approximate=False, source="seed")
            if stage != "applied" or not applied_on:
                _insert_history(conn, app_id, stage, applied_on or now, approximate=True, source="seed")
            added += 1
        conn.execute("INSERT INTO meta (key, value) VALUES ('seed_imported', ?)", (now,))
    return added


def add_application(conn: sqlite3.Connection, *, company: str, role: str, location: str = "",
                    applied_on: str | None = None, stage: str = "applied", notes: str = "",
                    today: date | None = None, job_key: str | None = None) -> int:
    """A new application. With `job_key` it is linked to a role the tool found (one per role)."""
    company, role, location = company.strip(), role.strip(), location.strip()
    if not company or not role:
        raise StoreError("Company and role are required")
    stage = _stage(stage)
    applied_on = _date_or_none(applied_on, "applied_on")
    if applied_on is None and stage in stages.SENT:
        applied_on = (today or date.today()).isoformat()
    key = f"job:{job_key}" if job_key else manual_key(company, role, location, applied_on)
    now = now_iso()
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO applications (key, job_key, company, role, location, stage, applied_on, notes,
                                            created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, job_key, company, role, location, stage, applied_on, notes.strip(), now, now),
            )
            _insert_history(conn, cur.lastrowid, stage, now, approximate=False, source="web")
    except sqlite3.IntegrityError:
        raise StoreError(f"{company}: {role} ({location or 'no location'}, {applied_on}) is already tracked") from None
    return cur.lastrowid


def update_application(conn: sqlite3.Connection, app_id: int, changes: dict[str, Any],
                       today: date | None = None) -> None:
    """Apply edits from the web app. A stage change is recorded in stage_history."""
    current = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if current is None:
        raise StoreError(f"No application with id {app_id}")
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise StoreError(f"Cannot edit {', '.join(sorted(unknown))}")

    values: dict[str, Any] = {}
    if "stage" in changes:
        values["stage"] = _stage(changes["stage"])
    for field in ("applied_on", "next_step_by"):
        if field in changes:
            values[field] = _date_or_none(changes[field], field)
    if "notes" in changes:
        values["notes"] = str(changes["notes"]).strip()

    stage_changed = "stage" in values and values["stage"] != current["stage"]
    applied_on = values.get("applied_on", current["applied_on"])
    if stage_changed and values["stage"] in stages.SENT and not applied_on:
        values["applied_on"] = (today or date.today()).isoformat()
    if stage_changed and values["stage"] not in stages.SENT and "applied_on" not in changes:
        values["applied_on"] = None  # back to To apply: it was not actually sent (the history keeps both changes)

    values = {k: v for k, v in values.items() if v != current[k]}
    if not values:
        return
    now = now_iso()
    assignments = ", ".join(f"{field} = ?" for field in values)
    with conn:
        conn.execute(f"UPDATE applications SET {assignments}, updated_at = ? WHERE id = ?",
                     (*values.values(), now, app_id))
        if stage_changed:
            _insert_history(conn, app_id, values["stage"], now, approximate=False, source="web")


# --- reading ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class ApplicationView:
    id: int
    company: str
    role: str
    location: str
    stage: str
    stage_label: str
    applied_on: str | None
    next_step_by: str | None
    notes: str
    contact: str
    found: bool  # linked to a role the tool found (To apply sends it back to Jobs)
    job_key: str | None
    last_change: str | None
    last_change_approximate: bool
    days_since_update: int | None
    ghosted: bool
    next_step_status: str | None  # "past", "soon" or None


def _day(value: str) -> date:
    return date.fromisoformat(value[:10])


def list_applications(conn: sqlite3.Connection, *, today: date, ghosted_after_days: int,
                      next_step_warning_days: int, stage: str | None = None,
                      include_to_apply: bool = False) -> list[ApplicationView]:
    """Applications for the Applications page. To apply is not an application there (decided
    2026-10-25): a role set back to To apply lives on Jobs, its record and history kept here."""
    rows = conn.execute(
        """SELECT a.*, h.changed_at AS last_change, h.approximate AS last_change_approximate
           FROM applications a
           LEFT JOIN stage_history h ON h.id = (
               SELECT id FROM stage_history WHERE application_id = a.id
               ORDER BY changed_at DESC, id DESC LIMIT 1)"""
        + ("" if include_to_apply else " WHERE a.stage != 'to_apply'")
    ).fetchall()
    views = []
    for r in rows:
        if stage and r["stage"] != stage:
            continue
        days = (today - _day(r["last_change"])).days if r["last_change"] else None
        next_status = None
        if r["next_step_by"] and stages.BY_KEY[r["stage"]].open:
            remaining = (_day(r["next_step_by"]) - today).days
            next_status = "past" if remaining < 0 else "soon" if remaining <= next_step_warning_days else None
        views.append(ApplicationView(
            id=r["id"], company=r["company"], role=r["role"], location=r["location"], stage=r["stage"],
            stage_label=stages.BY_KEY[r["stage"]].label, applied_on=r["applied_on"],
            next_step_by=r["next_step_by"], notes=r["notes"], contact=r["contact"], found=bool(r["job_key"]), job_key=r["job_key"],
            last_change=r["last_change"][:10] if r["last_change"] else None,
            last_change_approximate=bool(r["last_change_approximate"]),
            days_since_update=days,
            ghosted=r["stage"] in stages.WAITING and days is not None and days >= ghosted_after_days,
            next_step_status=next_status,
        ))
    views.sort(key=lambda v: (stages.ORDER[v.stage], -_day(v.applied_on).toordinal() if v.applied_on else 0,
                              v.company.lower()))
    return views


def get_application(conn: sqlite3.Connection, app_id: int, **kwargs) -> ApplicationView:
    for view in list_applications(conn, include_to_apply=True, **kwargs):
        if view.id == app_id:
            return view
    raise StoreError(f"No application with id {app_id}")


def stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT stage, COUNT(*) AS n FROM applications WHERE stage != 'to_apply' "
                        "GROUP BY stage").fetchall()
    found = {r["stage"]: r["n"] for r in rows}
    return {s.key: found.get(s.key, 0) for s in stages.STAGES}


def stage_history(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT stage, changed_at, approximate, source FROM stage_history WHERE application_id = ? "
        "ORDER BY changed_at, id", (app_id,)
    ).fetchall()


# --- Found jobs: the user's marks and stages ----------------------------------------------------------

@dataclass(frozen=True)
class JobMark:
    hidden: bool = False
    on_linkedin: str | None = None
    notes: str = ""
    hidden_at: str | None = None
    removed_at: str | None = None  # hidden long enough: gone from every page (restore from the command line)

    def days_left(self, today: date, hidden_days: int) -> int | None:
        """Days until a hidden role is removed (0: removed at the next sweep)."""
        if not self.hidden or not self.hidden_at or self.removed_at:
            return None
        return max(0, hidden_days - (today - _day(self.hidden_at)).days)


def _mark(row: sqlite3.Row) -> JobMark:
    return JobMark(bool(row["hidden"]), row["on_linkedin"], row["notes"], row["hidden_at"], row["removed_at"])


ON_LINKEDIN = (None, "yes", "no")


def job_marks(conn: sqlite3.Connection) -> dict[str, JobMark]:
    return {r["job_key"]: _mark(r) for r in conn.execute("SELECT * FROM job_marks")}


def set_job_mark(conn: sqlite3.Connection, job_key: str, **changes: Any) -> JobMark:
    """Change hidden, on_linkedin or notes for one found job."""
    unknown = set(changes) - {"hidden", "on_linkedin", "notes"}
    if unknown:
        raise StoreError(f"Cannot edit {', '.join(sorted(unknown))}")
    if "on_linkedin" in changes:
        changes["on_linkedin"] = changes["on_linkedin"] or None
        if changes["on_linkedin"] not in ON_LINKEDIN:
            raise StoreError("On LinkedIn must be yes, no or blank")
    if "hidden" in changes:
        changes["hidden"] = 1 if changes["hidden"] in (True, 1, "1", "true", "on", "yes") else 0
    current = job_marks_for(conn, job_key)
    merged = {"hidden": int(current.hidden), "on_linkedin": current.on_linkedin, "notes": current.notes, **changes}
    now = now_iso()
    # Hiding starts the clock to removal; showing again (or restoring) clears both dates.
    hidden_at = (current.hidden_at if current.hidden else now) if merged["hidden"] else None
    removed_at = current.removed_at if merged["hidden"] else None
    with conn:
        conn.execute("""INSERT INTO job_marks (job_key, hidden, on_linkedin, notes, updated_at, hidden_at, removed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_key) DO UPDATE SET hidden = excluded.hidden, on_linkedin = excluded.on_linkedin,
                        notes = excluded.notes, updated_at = excluded.updated_at, hidden_at = excluded.hidden_at,
                        removed_at = excluded.removed_at""",
                     (job_key, merged["hidden"], merged["on_linkedin"], str(merged["notes"]).strip(), now,
                      hidden_at, removed_at))
    return job_marks_for(conn, job_key)


def expire_hidden(conn: sqlite3.Connection, hidden_days: int, today: date | None = None) -> int:
    """Remove roles hidden for `hidden_days` or more. Their row stays (with removed_at) so they
    never show again; a later change of the setting does not bring them back."""
    cutoff = ((today or date.today()) - timedelta(days=hidden_days)).isoformat()
    with conn:
        return conn.execute("UPDATE job_marks SET removed_at = ? WHERE hidden = 1 AND removed_at IS NULL "
                            "AND hidden_at IS NOT NULL AND substr(hidden_at, 1, 10) <= ?",
                            (now_iso(), cutoff)).rowcount


def removed_marks(conn: sqlite3.Connection) -> dict[str, JobMark]:
    return {r["job_key"]: _mark(r) for r in
            conn.execute("SELECT * FROM job_marks WHERE removed_at IS NOT NULL ORDER BY removed_at")}


def forget_removed(conn: sqlite3.Connection, closed_keys: set[str]) -> int:
    """Delete the records of removed roles that have closed on their board: nothing left to keep away."""
    keys = [k for k in removed_marks(conn) if k in closed_keys]
    with conn:
        conn.executemany("DELETE FROM job_marks WHERE job_key = ?", [(k,) for k in keys])
    return len(keys)


def restore_removed(conn: sqlite3.Connection, job_key: str) -> bool:
    """Bring a removed role back onto the Jobs pages (no longer hidden)."""
    if job_key not in removed_marks(conn):
        return False
    set_job_mark(conn, job_key, hidden=0)
    return True


def job_marks_for(conn: sqlite3.Connection, job_key: str) -> JobMark:
    row = conn.execute("SELECT * FROM job_marks WHERE job_key = ?", (job_key,)).fetchone()
    return _mark(row) if row else JobMark()


def applications_by_job(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """Application id and stage for every found job that has one."""
    return {r["job_key"]: (r["id"], r["stage"])
            for r in conn.execute("SELECT id, job_key, stage FROM applications WHERE job_key IS NOT NULL")}


def set_job_stage(conn: sqlite3.Connection, *, job_key: str, company: str, role: str, location: str,
                  stage: str, today: date | None = None, undo: bool = False) -> int | None:
    """Set the stage of a found job: creates its application on first use, updates it after.
    "none" removes an application that is only To apply; a sent application cannot go back to none,
    except as the Undo of marking it applied a moment ago."""
    existing = applications_by_job(conn).get(job_key)
    if stage == "none":
        if existing is None:
            return None
        app_id, current = existing
        if current != "to_apply" and not undo:
            raise StoreError("This application has been sent; change its stage on the Applications page")
        with conn:
            conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        return None
    if existing is None:
        return add_application(conn, company=company, role=role, location=location, stage=stage,
                               today=today, job_key=job_key)
    update_application(conn, existing[0], {"stage": stage}, today=today)
    return existing[0]


def unlinked_applications(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(company, role, stage) of applications not linked to a found job (seeded or added by hand)."""
    return [(r["company"], r["role"], r["stage"])
            for r in conn.execute("SELECT company, role, stage FROM applications WHERE job_key IS NULL")]


def on_hold_companies(conn: sqlite3.Connection) -> set[str]:
    """Companies with an application On hold (a new role there means: contact the recruiter)."""
    return {r["company"] for r in conn.execute("SELECT company FROM applications WHERE stage = 'on_hold'")}


def today_since(conn: sqlite3.Connection, now: datetime | None = None, update: bool = True) -> str:
    """When the user last looked at the Today page, for its "new since your last visit" (UTC, ISO).
    Opening the page more than 30 minutes after the current visit began starts a new visit, so
    reloading keeps the same roles new; the first visit ever looks back one day."""
    now = now or datetime.now(UTC)
    stored = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('today_visit', 'today_previous')").fetchall())
    visit, previous = stored.get("today_visit"), stored.get("today_previous")
    if visit is None:
        visit, previous = now.isoformat(timespec="seconds"), (now - timedelta(days=1)).isoformat(timespec="seconds")
    elif now - datetime.fromisoformat(visit) > timedelta(minutes=30):
        visit, previous = now.isoformat(timespec="seconds"), visit
    if update:
        with conn:
            for key, value in (("today_visit", visit), ("today_previous", previous)):
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                             (key, value, value))
    return previous or (now - timedelta(days=1)).isoformat(timespec="seconds")


# --- Application help ---------------------------------------------------------------------------

def helper_messages(conn: sqlite3.Connection, job_key: str) -> list[dict[str, Any]]:
    return [{**dict(r), "missing": json.loads(r["missing"] or "[]")} for r in conn.execute(
        "SELECT id, role, content, missing, created_at FROM helper_messages WHERE job_key = ? ORDER BY id", (job_key,))]


def add_helper_message(conn: sqlite3.Connection, job_key: str, role: str, content: str,
                       missing: list[str] | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO helper_messages (job_key, role, content, missing, created_at) VALUES (?, ?, ?, ?, ?)",
                     (job_key, role, content, json.dumps(missing or []), now_iso()))


def clear_helper_messages(conn: sqlite3.Connection, job_key: str) -> None:
    with conn:
        conn.execute("DELETE FROM helper_messages WHERE job_key = ?", (job_key,))


def helper_drafts(conn: sqlite3.Connection, job_key: str) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["kind"], r["qid"]): {**dict(r), "choices": json.loads(r["choices"] or "[]"),
                                    "missing": json.loads(r["missing"] or "[]"), "facts": json.loads(r["facts"] or "{}")}
            for r in conn.execute("SELECT * FROM helper_drafts WHERE job_key = ?", (job_key,))}


def save_helper_draft(conn: sqlite3.Connection, job_key: str, kind: str, qid: str, answer: str,
                      choices: list[str] | None = None, missing: list[str] | None = None, edited: bool = False,
                      question: str | None = None) -> None:
    """A draft (or the user's edit of one). `question`: the question's text it was drafted for."""
    with conn:
        conn.execute(
            """INSERT INTO helper_drafts (job_key, kind, qid, answer, choices, missing, edited, updated_at, question)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_key, kind, qid) DO UPDATE SET answer = excluded.answer,
               choices = COALESCE(excluded.choices, helper_drafts.choices),
               missing = COALESCE(excluded.missing, helper_drafts.missing), edited = excluded.edited,
               updated_at = excluded.updated_at, question = COALESCE(excluded.question, helper_drafts.question)""",
            (job_key, kind, qid, answer, None if choices is None else json.dumps(choices),
             None if missing is None else json.dumps(missing), int(edited), now_iso(), question))


def save_helper_facts(conn: sqlite3.Connection, job_key: str, kind: str, qid: str, facts: dict[str, str]) -> None:
    """Facts the user gave for a draft ("Needed from you"), kept with it for its redrafts."""
    with conn:
        conn.execute(
            """INSERT INTO helper_drafts (job_key, kind, qid, answer, facts, updated_at) VALUES (?, ?, ?, '', ?, ?)
               ON CONFLICT(job_key, kind, qid) DO UPDATE SET facts = excluded.facts""",
            (job_key, kind, qid, json.dumps(facts), now_iso()))


def delete_helper_draft(conn: sqlite3.Connection, job_key: str, kind: str, qid: str) -> None:
    with conn:
        conn.execute("DELETE FROM helper_drafts WHERE job_key = ? AND kind = ? AND qid = ?", (job_key, kind, qid))


def helper_questions(conn: sqlite3.Connection, job_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM helper_questions WHERE job_key = ?", (job_key,)).fetchone()
    if not row:
        return None
    return {"source": row["source"], "questions": json.loads(row["questions"]), "fetched_at": row["fetched_at"],
            "letter_words": json.loads(row["letter_words"]) if row["letter_words"] else None,
            "reviewed": bool(row["reviewed"])}


def save_helper_questions(conn: sqlite3.Connection, job_key: str, source: str, questions: list[dict[str, Any]],
                          reviewed: bool = True) -> None:
    with conn:
        conn.execute(
            """INSERT INTO helper_questions (job_key, source, questions, fetched_at, reviewed) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(job_key) DO UPDATE SET source = excluded.source, questions = excluded.questions,
               fetched_at = excluded.fetched_at, reviewed = excluded.reviewed""",
            (job_key, source, json.dumps(questions), now_iso(), int(reviewed)))


def update_helper_questions(conn: sqlite3.Connection, job_key: str, questions: list[dict[str, Any]],
                            reviewed: bool | None = None) -> None:
    """The list after an edit, a delete, a merge or an added question (its source stays)."""
    with conn:
        conn.execute("UPDATE helper_questions SET questions = ? WHERE job_key = ?", (json.dumps(questions), job_key))
        if reviewed is not None:
            conn.execute("UPDATE helper_questions SET reviewed = ? WHERE job_key = ?", (int(reviewed), job_key))


def save_letter_words(conn: sqlite3.Connection, job_key: str, low: int, high: int) -> None:
    with conn:
        conn.execute(
            """INSERT INTO helper_questions (job_key, source, questions, letter_words, fetched_at)
               VALUES (?, 'none', '[]', ?, ?) ON CONFLICT(job_key) DO UPDATE SET letter_words = excluded.letter_words""",
            (job_key, json.dumps([low, high]), now_iso()))


def record_helper_call(conn: sqlite3.Connection, job_key: str, kind: str, model: str, seconds: float,
                       cost_usd: float, share: float, error: str | None, items: int | None = None) -> None:
    with conn:
        conn.execute("""INSERT INTO helper_calls (job_key, kind, model, seconds, cost_usd, share, error, created_at, items)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (job_key, kind, model, seconds, cost_usd, share, error,
                                                                datetime.now(UTC).isoformat(timespec="seconds"), items))


def helper_seconds(conn: sqlite3.Connection, kind: str, limit: int = 10) -> list[tuple[float, int | None]]:
    """(seconds, questions drafted) of the latest successful helper calls of one kind, newest first."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT seconds, items FROM helper_calls WHERE kind = ? AND error IS NULL AND seconds > 0 "
        "ORDER BY created_at DESC LIMIT ?", (kind, limit))]


def helper_usage(conn: sqlite3.Connection, since: str) -> dict[str, float]:
    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(share), 0), COALESCE(SUM(cost_usd), 0) FROM helper_calls "
                       "WHERE created_at >= ?", (since,)).fetchone()
    return {"calls": row[0], "windows": round(row[1], 3), "cost_usd": round(row[2], 2)}


# --- Local preferences (2026-11-19): the Settings page's choices that only this app uses ----------

def preference(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"pref:{key}",)).fetchone()
    return json.loads(row[0]) if row else default


def set_preference(conn: sqlite3.Connection, key: str, value: Any) -> None:
    raw = json.dumps(value)
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                     (f"pref:{key}", raw, raw))


# --- Companies checked by hand (2026-10-29) ------------------------------------------------------

def manual_checks(conn: sqlite3.Connection) -> dict[str, str]:
    """Companies the user wants a weekly reminder to check by hand: normalised name -> name."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'manual_checks'").fetchone()
    return json.loads(row[0]) if row else {}


def set_manual_check(conn: sqlite3.Connection, company_id: str, name: str, on: bool) -> None:
    checks = manual_checks(conn)
    if on:
        checks[company_id] = name
    else:
        checks.pop(company_id, None)
    value = json.dumps(dict(sorted(checks.items())))
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('manual_checks', ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                     (value, value))


def manual_checks_done(conn: sqlite3.Connection, at: str | None = None) -> str | None:
    """When the user last said they checked them (with `at`: record it)."""
    if at is not None:
        with conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('manual_checks_done', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = ?", (at, at))
        return at
    row = conn.execute("SELECT value FROM meta WHERE key = 'manual_checks_done'").fetchone()
    return row[0] if row else None


# --- Find more companies (2026-10-30) --------------------------------------------------------------

def company_search(conn: sqlite3.Connection) -> dict[str, Any]:
    """The company search's state: `history` (each search's cost), `results` (the latest
    suggestions, each with its `decision`), `rejected` (normalised name -> name, never suggested
    again) and `since` (when the feature started, for the three-monthly reminder before any search)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'company_search'").fetchone()
    data = json.loads(row[0]) if row else {}
    data.setdefault("history", [])
    data.setdefault("results", [])
    data.setdefault("rejected", {})
    if "since" not in data:
        data["since"] = datetime.now(UTC).isoformat(timespec="seconds")
        save_company_search(conn, data)
    return data


def save_company_search(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    value = json.dumps(data, ensure_ascii=False)
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('company_search', ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                     (value, value))
