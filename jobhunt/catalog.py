"""Found jobs for the Jobs pages: an indexed SQLite catalogue built from the pipeline's state.

The state folder (`.data/`) holds well over 100,000 roles, so the pages never scan it per request.
Whenever its files (or companies.toml, limits.toml) change, the catalogue database (`catalog.db`,
beside the tracker database, disposable) is rebuilt in the background from the state, read-only.
Requests then use indexes for every view, filter and sort, a full-text index for search, and
per-view counts computed at build time.

Left out entirely: roles failing the pre-filter, duplicates, roles parked because every location is
in the UK or US (unless `[generated] score_parked` is on), and roles the title check dropped
(audit them in drops.jsonl). The user's own edits (hidden, notes, stages) stay in their
database and are joined in at query time.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import accounts, display, limits, pipeline, preferences, prefilter, scoring, stages
from .state import State, read_jsonl
from .store import JobMark

ACTION_ORDER = ["apply_now", "apply_state_availability", "contact_recruiter_about_later_intake",
                "watch_for_reopening", "skip"]
ACTION_LABELS = {"apply_now": "Apply now", "apply_state_availability": "Apply, state availability",
                 "contact_recruiter_about_later_intake": "Contact recruiter", "watch_for_reopening": "Watch",
                 "skip": "Skip"}
VIEWS = {
    "scored": "Scored",
    "apply": "Apply now",
    "waiting": "Waiting",
    "skipped": "Not scored",
    "all": "All roles",
    "hidden": "Hidden",
}
QUERY_TIME_VIEWS = {"hidden"}  # counted per request from the user's marks, not at build time
# The sidebar's views are presets of the one Jobs table: sets of filters (2026-10-27). A filter
# given in the query wins over the preset's (so "Any" can lift one).
PRESETS: dict[str, dict[str, Any]] = {
    # Roles whose locations are all in the US are left out by default (2026-10-28); the Where
    # filter's "Anywhere" or "US" brings them back.
    "apply": {"scope": "scored", "min_fit": 7, "action": "apply_now", "group": "not_us"},
    "scored": {"scope": "scored"},
    "waiting": {"scope": "waiting"},
    "skipped": {"scope": "skipped"},
    "all": {"scope": "any"},
    "hidden": {"scope": "any", "hidden": "only"},
}
SCOPES = {"scored": "Scored", "waiting": "Waiting to be scored", "skipped": "Not scored (old or filtered out)", "any": "Any"}
SORTS = {"default": "Best first", "fit": "Fit", "deadline": "Deadline", "posted": "Posted", "found": "Found"}
SCORED_ORDER = "j.action_rank, j.fit DESC, j.deadline IS NULL, j.deadline, j.company COLLATE NOCASE"
ODDS = ("high", "medium", "low")
CATALOG_VERSION = 11  # bump when the schema changes, so an older catalogue is rebuilt
PAGE_SIZE = 100
WATCH_RANK = ACTION_ORDER.index("watch_for_reopening")
UNSCORED_RANK = len(ACTION_ORDER)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE jobs (
    key TEXT PRIMARY KEY, company TEXT NOT NULL, company_norm TEXT NOT NULL, title TEXT NOT NULL,
    title_norm TEXT NOT NULL, grp TEXT NOT NULL, origin TEXT NOT NULL, view TEXT NOT NULL,
    fit INTEGER, status TEXT, action TEXT, action_rank INTEGER NOT NULL, deadline TEXT,
    first_seen TEXT NOT NULL, sort_date TEXT NOT NULL, graduate INTEGER NOT NULL, closed INTEGER NOT NULL,
    new INTEGER NOT NULL, record TEXT NOT NULL, score TEXT, odds TEXT, posted TEXT
);
CREATE INDEX jobs_scored ON jobs(view, closed, action_rank, fit DESC, deadline);
CREATE INDEX jobs_unscored ON jobs(view, closed, graduate DESC, sort_date DESC);
CREATE INDEX jobs_all ON jobs(closed, graduate DESC, sort_date DESC);
CREATE INDEX jobs_fit ON jobs(fit);
CREATE INDEX jobs_status ON jobs(status);
CREATE INDEX jobs_odds ON jobs(odds);
CREATE INDEX jobs_posted ON jobs(posted);
CREATE INDEX jobs_group ON jobs(grp);
CREATE INDEX jobs_origin ON jobs(origin);
CREATE INDEX jobs_new ON jobs(new);
CREATE INDEX jobs_names ON jobs(company_norm, title_norm);
-- Every open stored role per company, shown or not, by why it is left out (the Companies page).
CREATE TABLE company_roles (
    company_norm TEXT PRIMARY KEY, stored INTEGER NOT NULL, location INTEGER NOT NULL, parked INTEGER NOT NULL,
    title INTEGER NOT NULL, titlecheck INTEGER NOT NULL, duplicate INTEGER NOT NULL
);
CREATE VIRTUAL TABLE jobs_fts USING fts5(key UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2');
"""


@dataclass
class JobRow:
    record: dict[str, Any]
    score: dict[str, Any] | None
    group: str
    mark: JobMark
    stage: str | None  # the linked (or name-matched) application's stage, if any
    wishlist: bool
    limit_notes: list[str]
    on_hold: bool
    new: bool
    stored_view: str | None = None  # as the catalogue placed it (it knows the graduate-wording filter)

    @property
    def key(self) -> str:
        return self.record["key"]

    @property
    def company_id(self) -> str:
        return prefilter.normalise_company(self.record["company"])

    @property
    def result(self) -> dict[str, Any] | None:
        return self.score["result"] if self.score else None

    @property
    def fit(self) -> int | None:
        return self.result["fit"] if self.result else None

    @property
    def action(self) -> str | None:
        if not self.result:
            return None
        return "watch_for_reopening" if self.record["closed"] else self.result["action"]

    @property
    def status(self) -> str | None:
        return self.score["status"] if self.score else None

    @property
    def locations(self) -> list[str]:
        return self.record.get("merged_locations") or self.record["locations"]

    @property
    def company_name(self) -> str:
        """The company as shown (a nameless Workday site by its tenant)."""
        # Generated-list names come from the board (often its slug); yours are as you wrote them.
        return display.company_label(self.record["company"], from_board=self.record.get("origin") == "generated")

    @property
    def places(self) -> list[str]:
        return display.clean_locations(self.locations)

    @property
    def place_text(self) -> str:
        return display.short_places(self.locations)

    @property
    def blocker_text(self) -> str:
        """The blockers, one per line, for the status badge's hover."""
        return "\n".join(f"{b['type'].capitalize()} ({b['severity']}): {b['detail']}"
                         for b in (self.result or {}).get("blockers") or [])

    @property
    def deadline(self) -> str | None:
        return (self.score or {}).get("deadline") or self.record.get("deadline")

    @property
    def graduate(self) -> bool:
        return bool(prefilter.entry_terms(self.record["title"]))

    @property
    def view(self) -> str:
        return self.stored_view or view_of(self.record, self.score)

    @property
    def limit_warning(self) -> str | None:
        if self.on_hold:
            return "On hold application: contact recruiter"
        return self.limit_notes[0] if self.limit_notes else None


def view_of(record: dict[str, Any], score: dict[str, Any] | None, graduate_only: bool = False,
            exempt: Callable[[list[str]], bool] | None = None) -> str:
    """Scored; waiting to be scored; or not scored (skipped as old at a company's first fetch, or,
    with the generated list's graduate-wording filter on, without graduate wording: those never
    will be, so they are not counted as waiting; 2026-10-29)."""
    if score:
        return "scored"
    if record.get("backfill_skip"):
        return "skipped"
    if graduate_only and record.get("origin") == "generated" and not prefilter.entry_terms(record["title"]) \
            and not (exempt and exempt(record.get("locations") or [])):  # every role in the main countries
        return "skipped"
    return "waiting"


@dataclass
class Filters:
    view: str = "scored"  # the preset the page started from (its sidebar item)
    scope: str = "scored"  # scored, waiting, skipped or any
    q: str = ""
    status: str = ""
    action: str = ""
    min_fit: int | None = None
    odds: str = ""
    group: str = ""
    origin: str = ""
    company: str = ""  # a normalised company name (the wishlist's links)
    wishlist: bool = False
    new: bool = False
    since: str = ""  # found at or after this time (ISO), for the Today page's "new since your last visit"
    due_within: int | None = None  # a deadline from today to this many days ahead
    hidden: str = "exclude"  # exclude, include or only
    show_closed: bool = False
    sort: str = "default"
    dir: str = "desc"
    page: int = 1

    @property
    def show_hidden(self) -> bool:
        return self.hidden != "exclude"

    @classmethod
    def from_query(cls, query: dict[str, str], remembered_sort: str = "") -> "Filters":
        """The preset's filters, with any filter the query names on top. `remembered_sort` is
        "field:dir", used when the query names no sort."""
        view = query.get("view") or "scored"
        view = view if view in PRESETS else "scored"
        given = {**PRESETS[view], **{k: v for k, v in query.items() if k != "view"}}

        def flag(name: str) -> bool:
            return str(given.get(name, "")) in ("1", "on", "true", "yes")
        try:
            min_fit = int(given["min_fit"]) if given.get("min_fit") not in (None, "") else None
        except ValueError:
            min_fit = None
        try:
            page = max(1, int(query.get("page") or 1))
        except ValueError:
            page = 1
        hidden = str(given.get("hidden") or ("include" if flag("show_hidden") else "exclude"))
        sort, direction = str(query.get("sort") or "").strip(), str(query.get("dir") or "").strip()
        if not sort and remembered_sort and ":" in remembered_sort:
            sort, direction = remembered_sort.split(":", 1)
        scope = str(given.get("scope") or "any")
        try:
            due_within = int(given["due_within"]) if given.get("due_within") not in (None, "") else None
        except ValueError:
            due_within = None
        since = str(given.get("since") or "").strip()
        try:
            datetime.fromisoformat(since)
        except ValueError:
            since = ""
        return cls(view=view, scope=scope if scope in SCOPES else "any", q=str(given.get("q", "")).strip(),
                   status=str(given.get("status", "")), action=str(given.get("action", "")), min_fit=min_fit,
                   odds=str(given.get("odds", "")) if given.get("odds", "") in ODDS else "",
                   group=str(given.get("group", "")), origin=str(given.get("origin", "")),
                   company=prefilter.normalise_company(str(given.get("company", "")).strip()) if str(given.get("company", "")).strip() else "",
                   wishlist=flag("wishlist"), new=flag("new"),
                   hidden=hidden if hidden in ("exclude", "include", "only") else "exclude",
                   show_closed=flag("show_closed"), since=since, due_within=due_within,
                   sort=sort if sort in SORTS else "default",
                   dir="asc" if direction == "asc" else "desc", page=page)

    @property
    def order(self) -> str:
        """SQL ORDER BY for the chosen sort (ties broken the default way)."""
        way = "ASC" if self.dir == "asc" else "DESC"
        if self.sort == "fit":
            return f"j.fit IS NULL, j.fit {way}, j.deadline IS NULL, j.deadline"
        if self.sort == "deadline":
            return f"j.deadline IS NULL, j.deadline {way}, j.fit DESC"
        if self.sort == "posted":
            return f"j.posted IS NULL, j.posted {way}, j.fit DESC"
        if self.sort == "found":
            return f"j.first_seen {way}, j.fit DESC"
        return SCORED_ORDER if self.scope in ("scored", "any") else "j.graduate DESC, j.sort_date DESC"


@dataclass
class Page:
    rows: list[JobRow]
    total: int
    page: int
    pages: int
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class UserData:
    """The user's own data the pages join in: marks, stages, unlinked applications, on-hold companies,
    and the wishlist (normalised company names, from the data branch)."""
    marks: dict[str, JobMark]
    applications: dict[str, tuple[int, str]]
    on_hold: set[str]
    unlinked: list[tuple[str, str, str]]
    wishlist: frozenset[str] = frozenset()
    blocked: frozenset[str] = frozenset()  # normalised names: their roles show nowhere
    # Every application by normalised company, for the limits registry's warnings.
    limit_apps: dict[str, list] = field(default_factory=dict)


class Catalog:
    def __init__(self, state_dir: Path, config: Path, settings: dict[str, Any], path: Path):
        self.state_dir = state_dir
        self.config = config
        self.settings = settings
        self.path = path
        self.allowed = set(settings.get("locations", {}).get("allowed_countries", []))
        self.score_parked = bool(settings.get("generated", {}).get("score_parked", False))
        self.rules = pipeline.GeneratedRules.from_settings(settings)
        self._build_lock = threading.Lock()

    # --- Building ------------------------------------------------------------------------------

    def _stamp(self) -> str:
        files = [self.state_dir / "jobs.jsonl", self.state_dir / "scores.jsonl", self.state_dir / "runs.jsonl",
                 *sorted((self.state_dir / "jobs").rglob("*.jsonl")), self.config / "companies.toml",
                 self.config / "limits.toml", self.state_dir / preferences.FILE]
        use_apply_min_fit(preferences.apply_min_fit(self.state_dir))
        return json.dumps([(p.name, p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                           for p in files] + [self.score_parked, self.rules.all_in_main, CATALOG_VERSION])

    def _built(self, key: str) -> str | None:
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(self.path)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            finally:
                conn.close()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def _built_stamp(self) -> str | None:
        return self._built("stamp")

    def refresh(self, wait: bool = False) -> None:
        """Rebuild if the state changed. With no catalogue yet, the first build always waits;
        later rebuilds run in the background while pages use the previous catalogue. A catalogue
        from an older version of this code is never used: its schema may lack columns."""
        if self._stamp() == self._built_stamp():
            return
        if wait or not self.path.exists() or self._built("version") != str(CATALOG_VERSION):
            with self._build_lock:
                stamp = self._stamp()
                if stamp != self._built_stamp():  # it may have been built while we waited
                    self._build(stamp)
            return
        if self._build_lock.acquire(blocking=False):
            def build_in_background() -> None:
                try:
                    self._build(self._stamp())
                finally:
                    self._build_lock.release()
            threading.Thread(target=build_in_background, daemon=True).start()

    def _build(self, stamp: str) -> None:
        tmp = self.path.with_name(f"{self.path.name}.building-{os.getpid()}-{threading.get_ident()}")
        try:
            tmp.unlink(missing_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(tmp)
            conn.executescript(SCHEMA)
            state = State.read_only(self.state_dir) if self.state_dir.exists() else None
            runs = read_jsonl(self.state_dir / "runs.jsonl") if self.state_dir.exists() else []
            graduate_only = bool(self.settings.get("generated", {}).get("entry_level_only"))
            last_run = runs[-1]["started_at"] if runs else ""
            rows, texts = [], []
            left_out: dict[str, Counter[str]] = defaultdict(Counter)
            for record in (state.jobs.values() if state else []):
                why = self._left_out(record)
                if not record["closed"]:
                    tally = left_out[prefilter.normalise_company(record["company"])]
                    tally["stored"] += 1
                    if why:
                        tally[why] += 1
                if why:
                    continue
                score = pipeline.preferred_score(state, record, self.settings)
                if score and score.get("result") and self.settings.get("scoring", {}).get("visa_never_blocks"):
                    fixed, n = scoring.visa_as_check(score["result"])
                    if n:  # stored before the setting: shown as the run will store it
                        score = {**score, "result": fixed, "status": scoring.status(
                            fixed["blockers"], bool(self.settings.get("scoring", {}).get("consider_start_date")))}
                result = (score or {}).get("result")
                company_norm = prefilter.normalise_company(record["company"])
                locations = record.get("merged_locations") or record["locations"]
                tidy = prefilter.tidy_title(record["title"], record["company"], locations)
                if tidy != record["title"]:  # shown tidy now; stored tidy at the company's next fetch
                    record = {**record, "title": tidy}
                closed = bool(record["closed"])
                action = result["action"] if result else None
                rank = (WATCH_RANK if closed else ACTION_ORDER.index(action)) if result else UNSCORED_RANK
                rows.append((
                    record["key"], record["company"], company_norm, record["title"],
                    prefilter.normalise_title(record["title"]), prefilter.country_group(locations, self.allowed),
                    record.get("origin", "config"), view_of(record, score, graduate_only,
                            lambda places: not self.rules.needs_entry_terms(places)), result["fit"] if result else None,
                    score["status"] if score else None, action, rank,
                    (score or {}).get("deadline") or record.get("deadline"), record["first_seen"],
                    (record.get("posted") or record["first_seen"])[:10],
                    int(bool(prefilter.entry_terms(record["title"]))), int(closed),
                    int(bool(last_run and record["first_seen"] >= last_run)),
                    json.dumps(record, ensure_ascii=False), json.dumps(score, ensure_ascii=False) if score else None,
                    result["odds"] if result else None, (record.get("posted") or "")[:10] or None))
                texts.append((record["key"], " ".join([record["company"], record["title"], *locations])))
            conn.executemany(f"INSERT INTO jobs VALUES ({','.join('?' * 22)})", rows)
            conn.executemany("INSERT INTO jobs_fts (key, text) VALUES (?, ?)", texts)
            conn.executemany("INSERT INTO company_roles VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [(norm, t["stored"], t["location"], t["parked"], t["title"], t["titlecheck"], t["duplicate"])
                              for norm, t in left_out.items()])
            counts = {}
            for view in VIEWS.keys() - QUERY_TIME_VIEWS:
                where, params = _view_clause(view)
                counts[view] = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE j.closed = 0 AND {where}",
                                            params).fetchone()[0]
            waiting = pipeline.status(state, self.settings, datetime.now(UTC)) if state else None
            if waiting is not None:
                waiting["seven_day"] = latest_seven_day(state)
                # Per Claude account since 2026-11-18; "claude_week" is the token's (GitHub's runs).
                waiting["claude_weeks"] = pipeline.claude_week_by_account(state, datetime.now(UTC), self.settings)
                waiting["claude_week"] = pipeline.claude_week(
                    state, datetime.now(UTC), accounts.default_key(self.settings), self.settings)
            conn.executemany("INSERT INTO meta VALUES (?, ?)",
                             [("stamp", stamp), ("last_run", last_run), ("counts", json.dumps(counts)),
                              ("fetch_history", json.dumps(pipeline.fetch_history(runs))),
                              ("version", str(CATALOG_VERSION)), ("status", json.dumps(waiting))])
            conn.commit()
            conn.execute("ANALYZE")
            conn.close()
            os.replace(tmp, self.path)  # pages already reading keep the old file until they finish
        finally:
            tmp.unlink(missing_ok=True)

    def _shown(self, record: dict[str, Any]) -> bool:
        return self._left_out(record) is None

    def _left_out(self, record: dict[str, Any]) -> str | None:
        """Why a stored role is kept out of the app (location, parked, title, titlecheck,
        duplicate), or None when it is shown."""
        if record.get("duplicate_of"):
            return "duplicate"
        if record["prefilter"] != "pass":
            return "location" if record["prefilter"].startswith("location") else "title"
        if record.get("parked") and not self.score_parked:
            return "parked"  # UK/US only: stored for later, kept out of the app
        if record.get("origin") == "generated" and not self.score_parked:
            # The location rules as they are now (a town's country, 2026-10-29), before the
            # company's next fetch stores the same decision on the role.
            locations = record.get("locations") or []
            verdict = prefilter.check(record["title"], locations, self.allowed)
            if not verdict.passed:
                return "location" if verdict.reason.startswith("location") else "title"
            if self.rules.parks(record["title"], locations):
                return "parked"
        return "titlecheck" if (record.get("titlecheck") or {}).get("dropped") else None

    # --- Querying ------------------------------------------------------------------------------

    def _connect(self, user: UserData) -> sqlite3.Connection:
        self.refresh()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TEMP TABLE marks (key TEXT PRIMARY KEY, hidden INTEGER);  -- 1 hidden, 2 removed
            CREATE TEMP TABLE applied (key TEXT PRIMARY KEY, stage TEXT);
            CREATE TEMP TABLE wishlist (norm TEXT PRIMARY KEY);
            CREATE TEMP TABLE blocked (norm TEXT PRIMARY KEY);""")
        conn.executemany("INSERT INTO wishlist VALUES (?)", [(n,) for n in user.wishlist])
        conn.executemany("INSERT INTO blocked VALUES (?)", [(n,) for n in user.blocked])
        conn.executemany("INSERT INTO marks VALUES (?, ?)",
                         [(k, 2 if m.removed_at else int(m.hidden)) for k, m in user.marks.items()])
        applied = {k: s for k, (_, s) in user.applications.items()}
        # Applications without a job key match roles by company and title: a few indexed lookups
        # turn them into keys once per request, instead of a check on every role.
        for company, role, stage in user.unlinked:
            for (key,) in conn.execute("SELECT key FROM jobs WHERE company_norm = ? AND title_norm = ?",
                                       (prefilter.normalise_company(company), prefilter.normalise_title(role))):
                applied.setdefault(key, stage)
        conn.executemany("INSERT INTO applied VALUES (?, ?)", list(applied.items()))
        return conn

    def page(self, filters: Filters, user: UserData) -> Page:
        conn = self._connect(user)
        try:
            counts = self._counts(conn)
            where, params = self._where(filters)
            total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", params).fetchone()[0]
            pages = max(1, -(-total // PAGE_SIZE))
            page = min(filters.page, pages)
            rows = conn.execute(f"SELECT j.* FROM jobs j WHERE {where} ORDER BY {filters.order} LIMIT ? OFFSET ?",
                                [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
            notes = self._limit_notes()
            return Page(rows=[self._row(r, user, notes) for r in rows], total=total, page=page, pages=pages,
                        counts=counts)
        finally:
            conn.close()

    def counts(self, user: UserData) -> dict[str, int]:
        conn = self._connect(user)
        try:
            return self._counts(conn)
        finally:
            conn.close()

    def company_names(self) -> list[str]:
        """Every company with an open role, by its display name: the Jobs pages' company filter
        suggests them."""
        return sorted(self.company_choices().values(), key=str.lower)

    def company_choices(self) -> dict[str, str]:
        """{stored name: display name} for every company with an open role (2026-11-19)."""
        self.refresh()
        if not self.path.exists():
            return {}
        conn = sqlite3.connect(self.path)
        try:
            names = [name for (name,) in conn.execute(
                "SELECT MIN(company) FROM jobs WHERE closed = 0 GROUP BY company_norm")]
        finally:
            conn.close()
        return {name: display.company_label(name) for name in names}

    def has_company(self, name: str) -> bool:
        """Whether any stored role has this company (by its normalised name)."""
        self.refresh()
        if not self.path.exists():
            return False
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute("SELECT 1 FROM jobs WHERE company_norm = ? LIMIT 1",
                                (prefilter.normalise_company(name),)).fetchone() is not None
        finally:
            conn.close()

    def company_by_label(self, label: str) -> str | None:
        """The stored name a display name stands for ("FTI Consulting" -> its Workday URL)."""
        key = display.name_key(label)
        return next((name for name, shown in self.company_choices().items() if display.name_key(shown) == key), None)

    def company_counts(self, norms: set[str]) -> dict[str, tuple[int, int, int]]:
        """(open, scored, waiting to be scored) roles per company (normalised name), for the Companies page."""
        self.refresh()
        if not norms or not self.path.exists():
            return {}
        conn = sqlite3.connect(self.path)
        try:
            found = {}
            names = sorted(norms)
            for i in range(0, len(names), 500):
                chunk = names[i:i + 500]
                for norm, open_roles, scored, waiting in conn.execute(
                        f"SELECT company_norm, COUNT(*), SUM(view = 'scored'), SUM(view = 'waiting') FROM jobs "
                        f"WHERE closed = 0 AND company_norm IN ({','.join('?' * len(chunk))}) GROUP BY company_norm", chunk):
                    found[norm] = (open_roles, scored or 0, waiting or 0)
            return found
        finally:
            conn.close()

    def company_roles(self, norms: set[str]) -> dict[str, dict[str, int]]:
        """Open stored roles per company, shown or not, with the count left out for each reason."""
        self.refresh()
        if not norms or not self.path.exists():
            return {}
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            found = {}
            names = sorted(norms)
            for i in range(0, len(names), 500):
                chunk = names[i:i + 500]
                for row in conn.execute(f"SELECT * FROM company_roles WHERE company_norm IN "
                                        f"({','.join('?' * len(chunk))})", chunk):
                    found[row["company_norm"]] = {k: row[k] for k in row.keys() if k != "company_norm"}
            return found
        finally:
            conn.close()

    def fetch_history(self) -> dict[str, dict[str, Any]]:
        """Per company of yours: its last fetch without an error and the latest error (run summaries)."""
        self.refresh()
        found = self._built("fetch_history")
        return json.loads(found) if found else {}

    def blocked_counts(self, user: UserData) -> dict[str, int]:
        """Open roles per blocked company (normalised name), for the Blocked sheet."""
        conn = self._connect(user)
        try:
            return dict(conn.execute("""SELECT company_norm, COUNT(*) FROM jobs WHERE closed = 0
                                        AND company_norm IN (SELECT norm FROM blocked) GROUP BY company_norm""").fetchall())
        finally:
            conn.close()

    def wishlist_counts(self, user: UserData) -> dict[str, int]:
        """Open roles on the Jobs lists per wishlist company (normalised name)."""
        conn = self._connect(user)
        try:
            return dict(conn.execute(
                """SELECT j.company_norm, COUNT(*) FROM jobs j WHERE j.closed = 0
                   AND j.company_norm IN (SELECT norm FROM wishlist)
                   AND j.company_norm NOT IN (SELECT norm FROM blocked)
                   AND j.key NOT IN (SELECT key FROM marks WHERE hidden >= 1)
                   AND j.key NOT IN (SELECT key FROM applied WHERE stage != 'to_apply')
                   GROUP BY j.company_norm""").fetchall())
        finally:
            conn.close()

    def _counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Counts per view (open roles) from the build, less the few roles the user hid or applied to:
        only those are looked up, never every role."""
        built = json.loads(conn.execute("SELECT value FROM meta WHERE key = 'counts'").fetchone()[0])
        adjust = conn.execute(
            """SELECT j.view, j.fit, j.action, j.status, j.closed, j.grp FROM jobs j WHERE j.closed = 0 AND j.key IN (
                   SELECT key FROM marks WHERE hidden >= 1 UNION SELECT key FROM applied WHERE stage != 'to_apply'
                   UNION SELECT key FROM jobs WHERE company_norm IN (SELECT norm FROM blocked))"""
        ).fetchall()
        for row in adjust:
            for view in VIEWS.keys() - QUERY_TIME_VIEWS:
                if _in_view(row, view):
                    built[view] -= 1
        built["hidden"] = conn.execute(
            """SELECT COUNT(*) FROM jobs j WHERE j.closed = 0 AND j.key IN (SELECT key FROM marks WHERE hidden = 1)
               AND j.company_norm NOT IN (SELECT norm FROM blocked)
               AND j.key NOT IN (SELECT key FROM applied WHERE stage != 'to_apply')""").fetchone()[0]
        return built

    def _where(self, f: Filters) -> tuple[str, list[Any]]:
        clauses, params = ["1 = 1"], []
        if f.scope != "any":
            clauses.append("j.view = ?")
            params.append(f.scope)
        if not f.show_closed:
            clauses.append("j.closed = 0")
        clauses.append("j.company_norm NOT IN (SELECT norm FROM blocked)")  # blocked: nowhere
        if f.hidden == "only":
            clauses.append("j.key IN (SELECT key FROM marks WHERE hidden = 1)")
        elif f.hidden == "exclude":
            clauses.append("j.key NOT IN (SELECT key FROM marks WHERE hidden >= 1)")
        else:  # removed roles stay off every list, the "Hidden" checkbox included
            clauses.append("j.key NOT IN (SELECT key FROM marks WHERE hidden = 2)")
        clauses.append("j.key NOT IN (SELECT key FROM applied WHERE stage != 'to_apply')")
        if f.status:
            clauses.append("j.status = ?")
            params.append(f.status)
        if f.action:
            clauses.append("(CASE WHEN j.closed THEN 'watch_for_reopening' ELSE j.action END) = ?")
            params.append(f.action)
        if f.odds:
            clauses.append("j.odds = ?")
            params.append(f.odds)
        if f.min_fit is not None:
            clauses.append("j.fit >= ?")
            params.append(f.min_fit)
        if f.view == "apply" and not f.status:  # Apply now leaves out blocked roles, as alerts do
            clauses.append("COALESCE(j.status, '') != 'blocked'")
        if f.group == "not_us":
            clauses.append("j.grp != 'US'")
        elif f.group:
            clauses.append("j.grp = ?")
            params.append(f.group)
        if f.origin == "config":  # my companies: companies.toml and the wishlist
            clauses.append("j.origin IN ('config', 'wishlist')")
        elif f.origin:
            clauses.append("j.origin = ?")
            params.append(f.origin)
        if f.company:
            clauses.append("j.company_norm = ?")
            params.append(f.company)
        if f.wishlist:
            clauses.append("j.company_norm IN (SELECT norm FROM wishlist)")
        if f.new:
            clauses.append("j.new = 1")
        if f.since:
            clauses.append("j.first_seen >= ?")
            params.append(f.since)
        if f.due_within is not None:
            today = date.today()
            clauses.append("j.deadline >= ? AND j.deadline <= ?")
            params += [today.isoformat(), (today + timedelta(days=f.due_within)).isoformat()]
        words = [w for w in f.q.replace('"', " ").split() if w]
        if words:  # full-text index; each word may be the start of a longer word
            clauses.append("j.key IN (SELECT key FROM jobs_fts WHERE jobs_fts MATCH ?)")
            params.append(" AND ".join(f'"{w}"*' for w in words))
        return " AND ".join(clauses), params

    def _row(self, r: sqlite3.Row, user: UserData, notes: dict[str, list]) -> JobRow:
        """`notes`: the limits registry by normalised company (what applying would do is worked
        out per role from the user's applications)."""
        on_hold = {prefilter.normalise_company(c) for c in user.on_hold}
        name_stage = next((s for c, t, s in user.unlinked if prefilter.normalise_company(c) == r["company_norm"]
                           and prefilter.normalise_title(t) == r["title_norm"]), None) if user.unlinked else None
        return JobRow(record=json.loads(r["record"]), score=json.loads(r["score"]) if r["score"] else None,
                      stored_view=r["view"],
                      group=r["grp"], mark=user.marks.get(r["key"], JobMark()),
                      stage=(user.applications.get(r["key"]) or (None, None))[1] or name_stage,
                      wishlist=r["company_norm"] in user.wishlist,
                      limit_notes=limits.warnings(notes[r["company_norm"]], user.limit_apps.get(r["company_norm"], []),
                                                  r["title"], date.today()) if r["company_norm"] in notes else [],
                      on_hold=r["company_norm"] in on_hold, new=bool(r["new"]))

    def status(self) -> dict[str, Any] | None:
        """What `jobhunt status` shows (roles waiting at each step), as of the last build."""
        self.refresh()
        found = self._built("status")
        return json.loads(found) if found else None

    def closed(self, keys: set[str]) -> set[str]:
        """Which of these roles have closed on their board."""
        if not keys or not self.path.exists():
            return set()
        conn = sqlite3.connect(self.path)
        try:
            found = set()
            keys_list = sorted(keys)
            for i in range(0, len(keys_list), 500):
                chunk = keys_list[i:i + 500]
                found |= {k for (k,) in conn.execute(
                    f"SELECT key FROM jobs WHERE closed = 1 AND key IN ({','.join('?' * len(chunk))})", chunk)}
            return found
        finally:
            conn.close()

    def titles(self, keys: list[str]) -> dict[str, tuple[str, str]]:
        """(company, title) for roles in the catalogue."""
        if not keys or not self.path.exists():
            return {}
        conn = sqlite3.connect(self.path)
        try:
            return {k: (c, t) for k, c, t in conn.execute(
                f"SELECT key, company, title FROM jobs WHERE key IN ({','.join('?' * len(keys))})", keys)}
        finally:
            conn.close()

    def _limit_notes(self) -> dict[str, list]:
        return limits.by_company(limits.load(self.config))

    def get(self, key: str, user: UserData) -> tuple[JobRow, str] | None:
        conn = self._connect(user)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        job = self._row(row, user, self._limit_notes())
        digest = job.record.get("description_hash")
        posting = self.state_dir / "postings" / f"{digest}.txt"
        return job, posting.read_text(encoding="utf-8") if digest and posting.exists() else ""


def latest_seven_day(state: State) -> dict[str, Any] | None:
    """The latest reading Claude gave about the seven-day limit, from the stored scores."""
    latest = None
    for score in state.scores.values():
        limit = score.get("rate_limit") or {}
        if limit.get("rateLimitType") == "seven_day" and isinstance(limit.get("utilization"), (int, float)):
            if latest is None or score.get("scored_at", "") > latest["at"]:
                latest = {"utilization": limit["utilization"], "status": limit.get("status"), "at": score.get("scored_at", "")}
    return latest


def use_apply_min_fit(min_fit: int) -> None:
    """The Apply now threshold (the Settings page; preferences.jsonl on the data branch, 2026-11-19)."""
    PRESETS["apply"]["min_fit"] = int(min_fit)


def _view_clause(view: str) -> tuple[str, list[Any]]:
    if view in ("all", "hidden"):  # the hidden view's own clause is added in `_where`
        return "1 = 1", []
    if view == "apply":  # the same rule as phone alerts (preferences.apply_now)
        return ("j.view = 'scored' AND j.fit >= ? AND j.action = 'apply_now' AND j.closed = 0 AND j.grp != 'US' "
                "AND COALESCE(j.status, '') != 'blocked'", [PRESETS["apply"]["min_fit"]])
    return "j.view = ?", [view]


def _in_view(row: sqlite3.Row, view: str) -> bool:
    if view == "all":
        return True
    if view == "apply":
        return (row["view"] == "scored" and not row["closed"]
                and preferences.apply_now(row["fit"], row["action"], row["status"], row["grp"], PRESETS["apply"]["min_fit"]))
    return row["view"] == view


def linkedin_url(row: JobRow) -> str:
    from urllib.parse import quote_plus
    return f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(row.record['title'] + ' ' + row.record['company'])}"


def stage_options() -> list[tuple[str, str]]:
    return [("none", "—")] + [(s.key, s.label) for s in stages.STAGES]
