"""Local web app: the tracker that replaces the old spreadsheet.

Bound to 127.0.0.1 only. Two guards keep other websites open in the browser from changing data
through localhost: the Host header must be a local name (stops DNS rebinding), and state-changing
requests must not come from another origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import re
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import catalog as cat
from .. import config as cfg
from .. import pipeline, scoring, stages, store
from .. import sync as data_sync
from .. import accepted, accounts, actions_usage, blocklist, companies_view, display, helper, limits, runs_view, wishlist
from .. import companies as company_config
from ..state import read_jsonl
from ..state import State
from . import fetchnow
from . import setups as setup_records

HERE = Path(__file__).parent
APP_NAME = "Firstlight"  # the name shown in the app (one place: the identity pass, 2026-11-19)
LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACK_PATHS = ("/jobs", "/applications", "/today")  # the pages a role can be opened from


def safe_back(value: str | None) -> str:
    """Where Job detail's back link goes: a list page of this app (with its query), never elsewhere."""
    if value:
        parts = urlsplit(value)
        if not parts.scheme and not parts.netloc and parts.path in BACK_PATHS:
            return value
    return "/jobs"


def page_url(request: Request) -> str:
    """This page's path and query; for an htmx request, those of the page it was made from."""
    current = request.headers.get("hx-current-url")
    parts = urlsplit(current) if current else request.url
    return parts.path + (f"?{parts.query}" if parts.query else "")


def job_url(key: str, back: str | None = None) -> str:
    return "/job?" + urlencode({"key": key, **({"back": back} if back and back != "/jobs" else {})})


def create_app(paths: cfg.Paths, runner: scoring.Runner = scoring.run_subprocess, demo: bool = False) -> FastAPI:
    """`runner` runs `claude -p` (tests pass a fake). `demo` (`jobhunt serve --demo`): the example
    candidate with recorded scores; the tool's own tasks (runs, triggers, harvests) are left out."""
    all_settings = cfg.load_settings(paths.config)
    settings = all_settings.get("tracker", {})
    catalog = cat.Catalog(paths.state_dir, paths.config, all_settings,
                          path=paths.database.with_name("catalog.db"))
    sync_settings = all_settings.get("sync", {})
    sync_status: dict = {"at": None, "ok": None, "message": "", "busy": ""}

    def sync_enabled() -> bool:
        return bool(sync_settings.get("enabled", True)) and data_sync.is_worktree(paths.state_dir)

    def pull_now(wait: bool = False) -> None:
        """Merge the data branch into the local state folder (never pushes; the user's database is
        untouched)."""
        if not sync_enabled():
            sync_status.update(ok=None, message="Demo: recorded data" if demo
                               else "Not linked to the data branch yet (jobhunt sync --init)")
            return
        try:
            result = data_sync.pull(paths.state_dir, wait=wait, holder="the app's sync")
            if result.changed_files:
                catalog.refresh()  # rebuilt in the background; pages keep using the previous one
            sync_status.update(at=datetime.now(), ok=True, busy="",
                               message=f"{len(result.changed_files)} file{'s' if len(result.changed_files) != 1 else ''} updated")
            if data_sync.changed_locally(paths.state_dir, wishlist.FILE):  # an earlier push did not go through
                push_wishlist("Wishlist (pushed by a later sync)")
        except data_sync.Busy as e:
            sync_status.update(ok=None, busy=str(e), message=f"{e}; synced when it is done")
        except data_sync.SyncError as e:
            sync_status.update(at=datetime.now(), ok=False, message=str(e)[:200])
    def push_wishlist(message: str) -> None:
        """Push the state folder (for the wishlist) so the next scheduled run sees the change. When a
        run on this laptop holds the state, the change goes with its push or the next sync."""
        try:
            with data_sync.state_lock(paths.state_dir, timeout=5, holder="the app (pushing the wishlist)"):
                result = data_sync.push_locked(paths.state_dir, message)
            sync_status.update(at=datetime.now(), ok=True, busy="",
                               message="Change pushed to the data branch" if result.pushed else (result.note or "Up to date"))
        except data_sync.Busy as e:
            sync_status.update(ok=None, busy=str(e), message=f"Change saved here; pushed when it is done ({e})")
        except data_sync.SyncError as e:
            sync_status.update(ok=False, message=f"Change saved here, not pushed yet: {e}"[:200])

    def wishlist_changed(message: str) -> None:
        if sync_enabled():
            threading.Thread(target=push_wishlist, args=(message,), daemon=True).start()

    fetch_now = fetchnow.Runner(paths.root)
    # The demo is sealed (2026-11-19): nothing it offers reaches the network or Claude. Each path
    # that would says so where it is offered; these are the words.
    demo_off = {
        "search": "Switched off in the demo: this would search the web with your Claude account.",
        "boards": "Switched off in the demo: this would try the company's name on every job board and read its "
                  "careers page, over the internet. Only the demo's own companies are looked up.",
        "questions": "Switched off in the demo: this would load the questions from Greenhouse over the internet.",
        "fetch": "Switched off in the demo: this would fetch the company's roles over the internet and score them "
                 "with your Claude account.",
        "rescore": "Rescoring is switched off in the demo: the scores shown were recorded.",
    }
    if demo:
        def refused(*args, **kwargs):
            raise RuntimeError("Fetch and score now is switched off in the demo")
        fetch_now.start_fetch = fetch_now.start_scoring = refused  # a missed path fails loudly, never reaches out

    def company_roles(company_id: str) -> tuple[list[dict], State]:
        state = State.read_only(paths.state_dir)
        return [r for r in state.jobs.values()
                if prefilter_norm(r["company"]) == company_id and not r["closed"]], state

    def count_fetched(task: fetchnow.Task) -> None:
        """After the fetch: roles found, passing the filters, and waiting for a score, with the
        estimated usage of scoring them (Sonnet directly, or the cascade for a large company)."""
        roles, state = company_roles(task.company_id)
        scheduled, backlog = pipeline.models(all_settings)
        passing = [r for r in roles if r["prefilter"] == "pass" and not r.get("duplicate_of")]
        waiting = [r for r in passing if pipeline.scorable(r) and not any(
            pipeline.has_result(state, r, m) for m in (scheduled, backlog))]
        share = {"sonnet": 0.004, "haiku": 0.0012, "titlecheck": 0.0025,
                 **{k: float(v) for k, v in all_settings.get("usage", {}).get("share_per_call", {}).items()}}
        cascade = len(passing) > int(all_settings.get("scoring", {}).get("cascade_above_open", 50))
        if cascade:
            kept = len(waiting) * pipeline.TITLECHECK_KEEP[True]
            windows = -(-len(waiting) // 100) * share["titlecheck"] + kept * share[backlog] + kept * 0.2 * share[scheduled]
        else:
            windows = len(waiting) * share[scheduled]
        week = float(all_settings.get("usage", {}).get("windows_per_week", 12.5))
        task.counts = {"found": len(roles), "passing": len(passing), "waiting": len(waiting),
                       "without_text": sum(1 for r in passing if not r.get("description_hash")),
                       "windows": windows, "week_share": windows / week if week else 0.0, "cascade": cascade}

    def count_scored(task: fetchnow.Task) -> None:
        roles, state = company_roles(task.company_id)
        fits = []
        for r in roles:
            score = pipeline.preferred_score(state, r, all_settings)
            if score and score.get("result"):
                fits.append(score["result"]["fit"])
        threshold = cat.PRESETS["apply"]["min_fit"]
        task.results = {"scored": len(fits), "seven_plus": sum(f >= threshold for f in fits), "threshold": threshold,
                        "six": sum(f == threshold - 1 for f in fits)}
        threading.Thread(target=catalog.refresh, daemon=True).start()

    # Days before hidden roles are removed: [tracker] hidden_days, or the Settings page's choice (local).
    prefs = {"hidden_days": int(settings.get("hidden_days", 7))}
    with contextlib.closing(store.connect(paths.database)) as c:
        prefs["hidden_days"] = int(store.preference(c, "hidden_days", prefs["hidden_days"]))
    actions_minutes = actions_usage.ActionsMinutes(paths.root, paths.database.with_name("actions_usage.json"))
    if demo:  # the demo shows its recorded minutes and never asks GitHub
        actions_minutes.refresh = lambda wait=False: None

    def tidy_hidden(conn) -> None:
        """Remove roles hidden for `hidden_days`, and forget removed roles that have closed."""
        store.expire_hidden(conn, prefs["hidden_days"])
        store.forget_removed(conn, catalog.closed(set(store.removed_marks(conn))))

    keep_backups = settings.get("keep_backups", 30)
    view_options = {
        "ghosted_after_days": settings.get("ghosted_after_days", 21),
        "next_step_warning_days": settings.get("next_step_warning_days", 3),
    }

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = store.connect(paths.database)
        store.backup(conn, paths.backups, keep_backups)
        store.import_seed(conn, cfg.load_applications_seed(paths.config))
        conn.close()

        async def daily_backup():
            while True:
                await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
                c = store.connect(paths.database)
                try:
                    store.backup(c, paths.backups, keep_backups)
                    tidy_hidden(c)
                finally:
                    c.close()

        async def periodic_sync():
            interval = max(1, int(sync_settings.get("interval_minutes", 15))) * 60
            while True:
                await asyncio.to_thread(pull_now)
                await asyncio.sleep(interval)

        await asyncio.to_thread(catalog.refresh, True)  # the first build, before the first page
        actions_minutes.refresh()  # counted in the background from the start
        conn = store.connect(paths.database)
        try:
            tidy_hidden(conn)
        finally:
            conn.close()
        tasks = [asyncio.create_task(daily_backup()), asyncio.create_task(periodic_sync())]
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            own = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (origin and origin != own) or request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("Cross-origin request refused", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def own_names(request: Request, call_next):
        # Every page shows a company by the one display name; yours as you wrote them (2026-11-19).
        if not request.url.path.startswith("/static/"):
            companies_view.register_names(paths.config, paths.state_dir)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Static URLs carry a content fingerprint, but browsers must still never reuse a stale copy.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["stages"] = stages.STAGES
    templates.env.globals["demo"] = demo
    templates.env.globals["demo_off"] = demo_off
    templates.env.globals["app_name"] = APP_NAME
    from .. import preferences as _preferences
    cat.use_apply_min_fit(_preferences.apply_min_fit(paths.state_dir))
    templates.env.globals["apply_min_fit"] = lambda: cat.PRESETS["apply"]["min_fit"]
    templates.env.globals["next_step_warning_days"] = settings.get("next_step_warning_days", 3)
    templates.env.globals["stage_labels"] = {s.key: s.label for s in stages.STAGES}
    templates.env.globals["asset_version"] = asset_version(HERE / "static")
    # One date format on every page (2026-10-29): "7 Oct" this year, "Jun 2025" otherwise.
    templates.env.filters["short_date"] = display.nice_date
    templates.env.filters["zip_pairs"] = lambda values: [(v, v) for v in values]
    templates.env.filters["nice_date"] = display.nice_date
    templates.env.filters["recent_date"] = display.recent_date
    templates.env.filters["num"] = display.number
    templates.env.filters["company"] = display.company_label
    templates.env.filters["reflow"] = display.reflow
    templates.env.filters["rich"] = display.rich_text
    templates.env.filters["long_date"] = display.long_date
    templates.env.filters["places"] = display.places_text
    templates.env.filters["angle"] = display.angle_text
    templates.env.filters["gap_text"] = display.gap_text
    templates.env.filters["run_kind"] = display.run_kind
    templates.env.filters["capitalised"] = display.capitalised
    templates.env.globals["is_old"] = display.is_old
    templates.env.globals["phase_name"] = runs_view.phase_name
    templates.env.globals["board_name"] = runs_view.board_name
    templates.env.globals["due_soon"] = display.due_soon
    templates.env.globals["is_past"] = display.is_past
    templates.env.filters["due"] = display.due_label
    templates.env.filters["start_date"] = display.start_date_text
    templates.env.filters["due_tone"] = display.due_tone
    templates.env.globals["every_runs"] = all_settings.get("fetching", {}).get("every_runs", {})
    templates.env.filters["table_date"] = display.nice_date
    templates.env.filters["when"] = when
    templates.env.filters["duration"] = duration
    templates.env.filters["ago"] = ago
    templates.env.globals["views"] = cat.VIEWS
    templates.env.globals["action_labels"] = cat.ACTION_LABELS
    templates.env.globals["job_stage_options"] = cat.stage_options()
    templates.env.globals["linkedin_url"] = cat.linkedin_url
    templates.env.globals["job_url"] = job_url
    templates.env.globals["hidden_days_left"] = lambda mark: mark.days_left(date.today(), prefs["hidden_days"])

    def db() -> Iterator:
        conn = store.connect(paths.database)
        try:
            yield conn
        finally:
            conn.close()

    def options() -> dict:
        return {"today": date.today(), **view_options}

    def user_data(conn) -> cat.UserData:
        store.expire_hidden(conn, prefs["hidden_days"])  # a role passes its days while the app runs
        return cat.UserData(marks=store.job_marks(conn), applications=store.applications_by_job(conn),
                         on_hold=store.on_hold_companies(conn), unlinked=store.unlinked_applications(conn),
                         wishlist=wishlist.names(paths.state_dir), blocked=blocklist.names(paths.state_dir),
                         limit_apps=limits.applications_from_store(conn))

    def sidebar_context(conn) -> dict:
        apps = store.list_applications(conn, **options())
        mine = user_data(conn)
        open_roles = catalog.wishlist_counts(mine)
        entries = wishlist.active(paths.state_dir)
        coverage = companies_view.wishlist_coverage(paths.config, paths.state_dir)
        empty = {e["id"] for e in entries if coverage[e["id"]].covered and not open_roles.get(e["id"])}
        stored, history = (catalog.company_roles(empty), catalog.fetch_history()) if empty else ({}, {})
        why_empty = {n: companies_view.roles_status(stored.get(n), history.get(n), daily=False) for n in empty}
        return {
            "why_empty": why_empty,
            "counts": store.stage_counts(conn),
            "total": len(apps),
            "stale": sum(a.ghosted for a in apps),
            "job_counts": catalog.counts(mine),
            "wishlist": [(e, open_roles.get(e["id"], 0)) for e in entries],
            "wishlist_coverage": coverage,
            "wishlist_ids": {e["id"] for e in entries},
            "blocked_count": len(blocklist.active(paths.state_dir)),
            "sync_status": sync_status,
            "today_count": today_context(conn, update=False)["today_count"],
            # A company being set up: the sidebar polls until it finishes, for the app's notice.
            "setup_watch": bool(setups.running()) or any(
                not r.get("announced") and r.get("status") in ("found", "failed") for r in setup_records.load(conn).values()),
        }

    workflow_file = paths.root / ".github" / "workflows" / "run.yml"
    TODAY_ROWS = 8

    def today_context(conn, update: bool) -> dict:
        """What the Today page lists; the sidebar uses only its count (`update` False: a look at
        the count does not start a new visit)."""
        from .. import today as td
        now = datetime.now(UTC)
        mine = user_data(conn)
        since = store.today_since(conn, now, update=update)
        sections = []
        new = catalog.page(cat.Filters.from_query({"view": "apply", "since": since}), mine)
        sections.append(("new", td.Section(
            f"New on Apply now (fit {cat.PRESETS['apply']['min_fit']}+) since your last visit", more_href="/jobs?" + urlencode({"view": "apply", "since": since}),
            more_label=f"All {new.total} in Apply now" if new.total > TODAY_ROWS else ""), new.rows[:TODAY_ROWS], new.total))
        deadline_fit = max(1, cat.PRESETS["apply"]["min_fit"] - 1)  # one below Apply now (2026-11-19)
        due = catalog.page(cat.Filters.from_query({"view": "scored", "min_fit": str(deadline_fit), "due_within": "7",
                                                   "sort": "deadline", "dir": "asc"}), mine)
        sections.append(("deadlines", td.Section(
            f"Deadlines in the next 7 days (fit {deadline_fit}+)",
            more_href=f"/jobs?view=scored&min_fit={deadline_fit}&due_within=7&sort=deadline&dir=asc",
            more_label=f"All {due.total}" if due.total > TODAY_ROWS else ""), due.rows[:TODAY_ROWS], due.total))
        apps = store.list_applications(conn, **options())

        def app_href(a) -> str:
            """An application opens its own role (2026-11-19), or its sheet on Applications when the
            tool did not find the role."""
            if a.job_key and catalog.get(a.job_key, user_data(conn)):
                return "/job?" + urlencode({"key": a.job_key, "back": "/today"})
            return "/applications?" + urlencode({"open": a.id})
        app_items = [td.Item(f"{display.company_label(a.company)}: {'overdue' if a.next_step_status == 'past' else 'next step'} "
                             f"{short_date(a.next_step_by)}", a.role, app_href(a),
                             "red" if a.next_step_status == "past" else "orange")
                     for a in sorted((a for a in apps if a.next_step_status), key=lambda a: a.next_step_by)]
        stale = [a for a in apps if a.ghosted]
        if stale:
            app_items.append(td.Item(f"{len(stale)} application{'s' if len(stale) != 1 else ''} to follow up",
                                     "No change in 21 days or more: " + ", ".join(display.company_label(a.company) for a in stale[:5])
                                     + ("…" if len(stale) > 5 else ""),
                                     app_href(stale[0]) if len(stale) == 1 else "/applications?view=stale", "orange"))
        tools = [] if demo else td.tool_items(
            all_settings, paths.state_dir, read_jsonl(paths.state_dir / "runs.jsonl"),
            workflow_file.read_text() if workflow_file.exists() else "", actions_minutes.result, catalog.status(), now,
            local_companies=[c.name for c in company_config.load_companies(paths.config) if c.fetch_from == "local" and c.fetchable])
        search_item = None if demo else company_search_reminder(conn)
        if search_item:
            tools.append(search_item)
        tools.extend(setup_items(conn))
        checks = manual_check_list(conn)
        # The sidebar's count: what needs the user (grey items are for information only).
        count = (new.total + due.total + len(app_items) + sum(1 for i in tools if i.tone != "gray")
                 + (1 if checks["due"] else 0))
        return {"since": since, "role_sections": sections, "app_items": app_items, "tool_items": tools,
                "today_count": count, "checks": checks}

    def setup_items(conn) -> list:
        """Company set-ups that wait for the user: a choice, a failure to decide on, or a job alert to create."""
        from .. import today as td
        items = []
        for r in setup_records.load(conn).values():
            href = "/companies?" + urlencode({"setup": r["id"]})
            if r.get("status") == "found":
                items.append(td.Item(f"{r['name']}: found a way to check it, waiting for your choice",
                                     "; ".join(o["label"] for o in r.get("options", [])[:3]), href, "blue"))
            elif r.get("status") == "failed":
                items.append(td.Item(f"{r['name']} could not be set up",
                                     f"{r.get('reason_text', '')}. Choose job alerts, a weekly check by hand or the name only.",
                                     href, "orange"))
            elif r.get("status") == "alert":
                items.append(td.Item(f"Create a job alert for {r['name']}",
                                     "On its careers site, sign up for job alerts with your address; a Gmail filter puts "
                                     "them under your job-alert label. Mark it done in the set-up sheet.", href, "blue"))
        return items

    def company_search_reminder(conn):
        """Every three months: suggest a new "Find more companies" search (none runs by itself)."""
        data = store.company_search(conn)
        last = data["history"][-1]["at"] if data["history"] else data["since"]
        days = (datetime.now(UTC) - datetime.fromisoformat(last)).days
        every = int(all_settings.get("company_search", {}).get("remind_days", 90))
        if days < every:
            return None
        detail = (f"The last company search was {days} days ago." if data["history"]
                  else "No company search yet.") + " New employers may have opened offices or started graduate hiring."
        from .. import today as td
        return td.Item("Time for a new company search", detail, "/companies?find=1", "blue")

    def manual_check_list(conn) -> dict:
        """Companies to check by hand: due a week after the user last said they checked them."""
        wanted = store.manual_checks(conn)
        if not wanted:
            return {"due": False, "companies": []}
        done = store.manual_checks_done(conn)
        due = done is None or datetime.now(UTC) - datetime.fromisoformat(done) >= timedelta(days=7)
        runs = read_jsonl(paths.state_dir / "runs.jsonl")
        rows = {r.id: r for r in companies_view.rows(paths.config, paths.state_dir, runs, {}, {}, date.today())[0]}
        companies = []
        for company_id, name in wanted.items():
            row = rows.get(company_id)
            url = ((row.careers_url if row else "") or setup_records.load(conn).get(company_id, {}).get("careers_url")
                   or "https://www.google.com/search?" + urlencode({"q": f"{name} careers"}))
            companies.append({"id": company_id, "name": name, "url": url,
                              "status": row.coverage.label if row else "", "searched": not (row and row.careers_url)})
        return {"due": due, "companies": companies, "done": done}

    def up_next_context(conn) -> dict:
        due = [a for a in store.list_applications(conn, **options()) if a.next_step_status]
        return {"due": sorted(due, key=lambda a: a.next_step_by)}

    @app.get("/")
    def index():
        return RedirectResponse("/today", status_code=303)

    @app.get("/today", response_class=HTMLResponse)
    def today_page(request: Request, conn=Depends(db)):
        actions_minutes.refresh()  # in the background, at most every 30 minutes (the trigger check reads it)
        found = today_context(conn, update=True)
        return templates.TemplateResponse(request, "today.html", {
            **found, "selected_page": "today", "selected_job_view": None, **sidebar_context(conn)})

    # --- Jobs ----------------------------------------------------------------------------------

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, conn=Depends(db)):
        view = request.query_params.get("view") or "scored"
        cookie = f"jobs_sort_{view}"
        params = dict(request.query_params)
        if params.get("company") and not catalog.has_company(params["company"]):
            # A display name typed or picked in the Company filter: the stored name it stands for.
            params["company"] = catalog.company_by_label(params["company"]) or params["company"]
        if params.get("sort") and not params.get("dir"):
            params["dir"] = "asc" if params["sort"] == "deadline" else "desc"
        filters = cat.Filters.from_query(params, request.cookies.get(cookie, ""))
        page = catalog.page(filters, user_data(conn))
        preset = cat.PRESETS.get(filters.view, {})
        # An empty value is kept when the view's preset sets that filter: "Anywhere" lifts its "Outside the US".
        query = {k: v for k, v in params.items() if k != "page" and (v or k in preset)}
        entry = wishlist.load(paths.state_dir).get(filters.company) if filters.company else None
        company_cov = (companies_view.wishlist_coverage(paths.config, paths.state_dir).get(filters.company)
                       if entry and entry.get("on") else None)
        title = (entry["name"] if entry else page.rows[0].company_name if page.rows else filters.company) \
            if filters.company else cat.VIEWS[filters.view]
        chips, more_count = filter_chips(filters, query, preset, title if filters.company else "")
        # Reset filters: back to the view's preset (and best first), keeping the search; only when
        # a filter other than the search is set.
        search_chip = f"Search: {filters.q}" if filters.q else None
        reset_url = ("/jobs?" + urlencode({"view": filters.view, "sort": "default", **({"q": filters.q} if filters.q else {})})
                     if any(label != search_chip for label, _ in chips) else "")

        def sort_url(field: str) -> str:
            """A column header's link: sort by it, or flip the direction if it is the sort already."""
            flip = "asc" if filters.sort == field and filters.dir == "desc" else "desc"
            return "/jobs?" + urlencode({**{k: v for k, v in query.items() if k not in ("sort", "dir")},
                                         "sort": field, "dir": flip})
        response = templates.TemplateResponse(request, "jobs.html", {
            "f": filters, "page": page, "title": title, "company_entry": entry, "company_cov": company_cov, "back_url": page_url(request),
            "notice": request.query_params.get("notice"), "scopes": cat.SCOPES, "sort_url": sort_url,
            "fetch_task": fetch_now.get(filters.company) if filters.company else None, "holder": "", "week": None,
            "guard_at": float(all_settings.get("usage", {}).get("weekly_guard_share", 0.75)),
            "presets": cat.PRESETS, "sorts": cat.SORTS, "chips": chips, "more_count": more_count, "reset_url": reset_url,
            "company_names": catalog.company_names(),
            "more_open": more_count > 0,
            "page_url": lambda n: "/jobs?" + urlencode({**query, "page": n}),
            "selected_job_view": None if filters.company else filters.view,
            "selected_company": filters.company or None, **sidebar_context(conn)})
        if request.query_params.get("sort"):  # remembered for this view
            if filters.sort == "default":
                response.delete_cookie(cookie)
            else:
                response.set_cookie(cookie, f"{filters.sort}:{filters.dir}", max_age=365 * 24 * 3600,
                                    samesite="lax", httponly=True)
        return response

    MORE_FILTERS = ("scope", "status", "action", "origin", "hidden", "sort", "wishlist", "new", "show_closed")

    def filter_chips(f: cat.Filters, query: dict, preset: dict, company_name: str = "") -> tuple[list[tuple[str, str]], int]:
        """Filters that differ from the view's preset, as (label, link that removes it), and how
        many of them sit under More filters."""
        group_names = {"not_us": "Outside the US", "unknown": "Not stated", "": "Anywhere"}
        values = {
            "min_fit": (f.min_fit, f"Fit at least {f.min_fit}" if f.min_fit else "Any fit"),
            "odds": (f.odds, f"Odds: {f.odds.capitalize()}" if f.odds else "Any odds"),
            "group": (f.group, f"Where: {group_names.get(f.group, f.group)}"),
            "scope": (f.scope, f"Scope: {cat.SCOPES.get(f.scope, f.scope)}"),
            "status": (f.status, f"Status: {f.status.capitalize()}" if f.status else "Any status"),
            "action": (f.action, f"Action: {cat.ACTION_LABELS.get(f.action, f.action)}" if f.action else "Any action"),
            "origin": (f.origin, {"config": "My companies", "generated": "Generated list"}.get(f.origin, "All companies")),
            "hidden": (f.hidden, {"include": "Hidden included", "only": "Only hidden", "exclude": "Hidden left out"}[f.hidden]),
            "wishlist": (f.wishlist, "Wishlist only"), "new": (f.new, "New since last run"),
            "show_closed": (f.show_closed, "Closed roles"), "q": (f.q, f"Search: {f.q}"),
            "since": (f.since, f"Found since {display.nice_date(f.since)}"),
            "due_within": (f.due_within, f"Deadline within {f.due_within} days"),
            "sort": (f.sort if f.sort != "default" else "", f"Sorted by {cat.SORTS.get(f.sort, f.sort).lower()}"),
        }
        defaults = {"scope": preset.get("scope", "any"), "hidden": preset.get("hidden", "exclude")}
        chips, more = [], 0
        for key, (value, label) in values.items():
            default = defaults.get(key, preset.get(key, None if key in ("min_fit", "due_within") else ""))
            if key in ("wishlist", "new", "show_closed"):
                default = False
            if value == default or (not value and not default):
                continue
            rest = {k: v for k, v in query.items() if k != key and not (key == "sort" and k == "dir")}
            chips.append((label, "/jobs?" + urlencode(rest)))
            more += key in MORE_FILTERS
        if f.company:
            chips.append((f"Company: {company_name or f.company}", "/jobs?" + urlencode({k: v for k, v in query.items() if k != "company"})))
        return chips, more

    def job_or_404(key: str, conn):
        found = catalog.get(key, user_data(conn))
        if found is None:
            return None, PlainTextResponse("No such job (it may have been removed by a run)", status_code=404)
        if found[0].company_id in blocklist.names(paths.state_dir):
            return None, PlainTextResponse(
                f"{found[0].record['company']} is blocked: its roles show nowhere. Unblock it on Companies, "
                "under Blocked.", status_code=404)
        if found[0].mark.removed_at:
            return None, PlainTextResponse(
                f"This role was hidden for {prefs['hidden_days']} days and removed. To bring it back: "
                f"jobhunt hidden --restore '{key}'", status_code=404)
        return found, None

    @app.post("/sync")
    async def sync_now(request: Request) -> Response:
        await asyncio.to_thread(pull_now, False)
        back = request.headers.get("referer") or "/jobs"
        return RedirectResponse(back if back.startswith(str(request.base_url)) else "/jobs", status_code=303)

    @app.post("/job/rescore")
    async def job_rescore(request: Request) -> Response:
        """Score one role again with the scheduled model, here on the laptop, and push the result
        through the data branch so the next sync does not undo it."""
        form = await request.form()
        key = str(form.get("key", ""))

        def work() -> str:
            with data_sync.state_lock(paths.state_dir, timeout=60, holder="a rescore"):
                linked = sync_enabled()
                if linked:
                    with contextlib.suppress(data_sync.SyncError):
                        data_sync.pull_locked(paths.state_dir)
                state = State(paths.state_dir)
                record = state.jobs.get(key)
                if record is None:
                    return "error=" + "No such job"
                text = state.read_posting(record.get("description_hash"))
                if not text:
                    return "error=" + "The posting text has not been fetched yet, so it cannot be scored"
                if demo:
                    return "error=" + demo_off["rescore"]
                model = pipeline.models(all_settings)[0]
                score = scoring.score_job(pipeline.job_from_record(record, text),
                                          scoring.build_prompt(paths.config, all_settings, model=model), runner)
                if score.result is None:
                    return "error=" + display.sentence(f"Scoring failed: {score.error}")
                state.scores[score.id] = {**score.to_record(), "account": accounts.current(all_settings)["key"]}
                state.save()
                if linked:
                    try:
                        data_sync.push_locked(paths.state_dir, f"Rescore {key}")
                    except data_sync.SyncError as e:
                        return "notice=" + f"Rescored (fit {score.result['fit']}), but not pushed yet: {e}"
                return "notice=" + f"Rescored with {model}: fit {score.result['fit']}"

        try:
            outcome = await asyncio.to_thread(work)
        except data_sync.Busy as e:
            outcome = "error=" + f"{e}; rescore again when it is done"
        kind, message = outcome.split("=", 1)
        return job_redirect(key, form, **{kind: message})

    @app.get("/job", response_class=HTMLResponse)
    def job_page(request: Request, key: str, notice: str | None = None, error: str | None = None,
                 back: str | None = None, tab: str = "questions", conn=Depends(db)):
        found, error_page = job_or_404(key, conn)
        if error_page:  # a page, not bare text: the role's link may come from an old bookmark (2026-11-19)
            back_url = safe_back(back)
            response = templates.TemplateResponse(request, "missing.html", {
                "heading": "This role is not here any more", "message": error_page.body.decode(),
                "back": back_url, "back_label": back_label(back_url) if back_url else "Apply now",
                "selected_page": None, "selected_job_view": None, **sidebar_context(conn)}, status_code=404)
            return response
        row, posting = found
        app_id = (store.applications_by_job(conn).get(key) or (None, None))[0]
        back = safe_back(back)
        return templates.TemplateResponse(request, "job.html", {
            "row": row, "posting": posting, "history": store.stage_history(conn, app_id) if app_id else [],
            "notice": notice, "error": error, "back": back, "back_label": back_label(back),
            "help_tab": tab if tab in HELP_TABS else "questions",
            "selected_job_view": None, **sidebar_context(conn)})

    # --- Application help ----------------------------------------------------------------------------

    helper_jobs: dict[tuple[str, str], dict] = {}  # (job key, kind) -> running, error, started
    profile_offers: dict[str, list[str]] = {}  # job key -> facts the user gave, offered for profile.md (asked first)
    helper_lock = threading.Lock()
    HELP_TABS = ("questions", "letter", "chat")

    def greenhouse_ref(record: dict) -> tuple[str, str] | None:
        """(board slug, job id) of a Greenhouse role, for its questions."""
        if record.get("source") != "greenhouse":
            return None
        account = str(record.get("account") or "")
        if account.startswith("greenhouse:") and account.split(":", 2)[2]:
            return account.split(":", 2)[2], str(record["source_id"])
        match = re.search(r"greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)", str(record.get("url") or ""))
        return (match.group(1), match.group(2)) if match else None

    def help_role(row, posting: str) -> "helper.Role":
        r = row.result or {}
        return helper.Role(company=row.record["company"], title=row.record["title"], locations=list(row.locations),
                           posting=posting or "", explanation=r.get("explanation", ""), angle=r.get("angle", ""),
                           gaps=list(r.get("gaps") or []), fit=r.get("fit"))

    def help_context(key: str, tab: str, conn) -> dict | None:
        found, _ = job_or_404(key, conn)
        if not found:
            return None
        row, posting = found
        saved = store.helper_questions(conn, key)
        questions = [helper.Question.from_dict(q) for q in (saved or {}).get("questions", [])]
        drafts = store.helper_drafts(conn, key)
        jobs = {kind: j for (k, kind), j in helper_jobs.items() if k == key}
        low, high = (saved or {}).get("letter_words") or helper.LETTER_WORDS
        week = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
        wanted = [q for q in questions if q.kind in helper.DRAFTED]
        # From the latest successful calls of each kind (2026-11-19), per question for answers.
        estimates = {kind: helper.estimate_seconds(kind, store.helper_seconds(conn, kind), len(wanted))
                     for kind in HELP_TABS}
        now = datetime.now(UTC)
        return {"key": key, "tab": tab if tab in HELP_TABS else "questions", "row": row, "has_posting": bool(posting),
                "reviewed": (saved or {}).get("reviewed", True), "copy_all": helper.copy_all(questions, drafts),
                "profile_offer": profile_offers.get(key, []), "estimates": estimates,
                "elapsed": {kind: int((now - j["started"]).total_seconds()) for kind, j in jobs.items()
                            if j["running"] and j.get("started")},
                "questions": questions, "source": (saved or {}).get("source"), "fetched_at": (saved or {}).get("fetched_at"),
                "drafts": drafts, "messages": store.helper_messages(conn, key),
                "running": {kind for kind, j in jobs.items() if j["running"]},
                "errors": {kind: {"message": j["error"], "detail": j.get("detail"), "retry": bool(j.get("work"))}
                           for kind, j in jobs.items() if not j["running"] and j.get("error")},
                "greenhouse": greenhouse_ref(row.record) is not None, "letter_low": low, "letter_high": high,
                "model": all_settings.get("helper", {}).get("model", helper.DEFAULT_MODEL),
                "usage": store.helper_usage(conn, week), "drafted_kinds": helper.DRAFTED, "words": helper.words}

    def help_panel(request: Request, key: str, tab: str, conn, error: str | None = None,
                   notice: str | None = None, note: str | None = None) -> Response:
        context = help_context(key, tab, conn)
        if context is None:
            return PlainTextResponse("No such job", status_code=404)
        return templates.TemplateResponse(request, "_help.html", {**context, "error": error, "notice": notice,
                                                                  "note": note})

    def demo_help_note(kind: str, key: str, conn) -> Markup:
        """In the demo, drafting and chat are switched off (2026-11-19): said plainly, with a link to
        the role whose recorded examples show what they make. No call is made, so nothing is retried."""
        what = ("Chat is switched off in the demo: it would send your profile, this posting and your message to Claude."
                if kind == "chat" else
                "Drafting is switched off in the demo: it would send your profile and this posting to Claude.")
        query = ("SELECT job_key FROM helper_messages ORDER BY id LIMIT 1" if kind == "chat" else
                 "SELECT job_key FROM helper_drafts WHERE kind = ? ORDER BY job_key LIMIT 1")
        row = conn.execute(query, () if kind == "chat" else ("letter" if kind == "letter" else "question",)).fetchone()
        example = row[0] if row else None
        if example == key:
            return Markup(html.escape(what) + " The " + ("conversation" if kind == "chat" else "drafts") + " shown were recorded.")
        found = catalog.get(example, user_data(conn)) if example else None
        if not found:
            return Markup(html.escape(what))
        label = f"{found[0].company_name}: {found[0].record['title']}"
        tab = "letter" if kind == "letter" else kind
        return Markup(f'{html.escape(what)} Recorded examples: <a href="/job?key={quote(example)}&amp;tab={tab}#help">'
                      f"{html.escape(label)}</a>.")

    def start_help(key: str, kind: str, work, items: int | None = None) -> bool:
        """Run one helper call in the background (one per role and kind at a time). A failure is kept
        as a plain sentence with Claude's own words as its details, and with the same work, so Try
        again repeats exactly that call (2026-11-19)."""
        with helper_lock:
            if helper_jobs.get((key, kind), {}).get("running"):
                return False
            helper_jobs[(key, kind)] = {"running": True, "error": None, "started": datetime.now(UTC), "work": work,
                                        "items": items}

        def run() -> None:
            conn = store.connect(paths.database)
            error = detail = None
            try:
                result = work(conn)
                store.record_helper_call(conn, key, kind, all_settings.get("helper", {}).get("model", helper.DEFAULT_MODEL),
                                         result.seconds, result.cost_usd, result.share, result.error, items)
                error = result.plain_error
                detail = " · ".join(dict.fromkeys(d for d in (result.error, result.diagnostic) if d)) or None
            except Exception as e:  # noqa: BLE001 (shown in the panel)
                error, detail = "Something went wrong in the app while drafting, so nothing was changed.", f"{type(e).__name__}: {e}"[:600]
            finally:
                conn.close()
                with helper_lock:
                    helper_jobs[(key, kind)] = {"running": False, "error": error, "detail": detail, "started": None,
                                                "work": work if error else None, "items": items}
        threading.Thread(target=run, daemon=True).start()
        return True

    @app.post("/job/help/retry", response_class=HTMLResponse)
    async def job_help_retry(request: Request, conn=Depends(db)):
        """Try again: the call that failed, as it was (the same questions, letter length or message)."""
        form = await request.form()
        key, kind = str(form.get("key", "")), str(form.get("kind", ""))
        if demo:
            return help_panel(request, key, kind if kind in HELP_TABS else "questions", conn, note=demo_help_note(kind, key, conn))
        job = helper_jobs.get((key, kind)) or {}
        if job.get("work") and not job.get("running"):
            start_help(key, kind, job["work"], job.get("items"))
        return help_panel(request, key, "letter" if kind == "letter" else kind if kind in HELP_TABS else "questions", conn)

    def help_inputs(key: str, conn) -> tuple:
        found, _ = job_or_404(key, conn)
        if not found:
            raise KeyError(key)
        row, posting = found
        return help_role(row, posting)

    @app.get("/job/help", response_class=HTMLResponse)
    def job_help(request: Request, key: str, tab: str = "questions", conn=Depends(db)):
        return help_panel(request, key, tab, conn)

    @app.post("/job/help/chat", response_class=HTMLResponse)
    async def job_help_chat(request: Request, conn=Depends(db)):
        form = await request.form()
        key, message = str(form.get("key", "")), str(form.get("message", "")).strip()
        if demo:
            return help_panel(request, key, "chat", conn, note=demo_help_note("chat", key, conn))
        if message:
            role = help_inputs(key, conn)
            history = store.helper_messages(conn, key)
            store.add_helper_message(conn, key, "user", message)

            def work(c):
                result = helper.chat(paths.config, all_settings, role, history, message, runner)
                if result.output:
                    store.add_helper_message(c, key, "assistant", result.output["reply"], result.output["missing"])
                return result
            start_help(key, "chat", work)
        return help_panel(request, key, "chat", conn)

    @app.post("/job/help/chat/clear", response_class=HTMLResponse)
    async def job_help_chat_clear(request: Request, conn=Depends(db)):
        key = str((await request.form()).get("key", ""))
        store.clear_helper_messages(conn, key)
        return help_panel(request, key, "chat", conn)

    @app.post("/job/help/questions/load", response_class=HTMLResponse)
    async def job_help_load(request: Request, conn=Depends(db)):
        """A Greenhouse role's application questions, from its public job API."""
        key = str((await request.form()).get("key", ""))
        if demo:
            return help_panel(request, key, "questions", conn, note=demo_off["questions"])
        found, _ = job_or_404(key, conn)
        ref = greenhouse_ref(found[0].record) if found else None
        if not ref:
            return help_panel(request, key, "questions", conn, error="Only Greenhouse publishes its questions: paste them instead")
        from ..sources import Http, SourceError
        try:
            data = await asyncio.to_thread(Http.from_settings(all_settings).get_json,
                                           f"https://boards-api.greenhouse.io/v1/boards/{ref[0]}/jobs/{ref[1]}?questions=true")
        except SourceError as e:
            return help_panel(request, key, "questions", conn, error=f"Could not load the questions: {e}")
        questions = helper.greenhouse_questions(data, helper.details(paths.config))
        store.save_helper_questions(conn, key, "greenhouse", [q.to_dict() for q in questions])
        return help_panel(request, key, "questions", conn)

    @app.post("/job/help/questions/paste", response_class=HTMLResponse)
    async def job_help_paste(request: Request, conn=Depends(db)):
        form = await request.form()
        key = str(form.get("key", ""))
        questions = helper.pasted_questions(str(form.get("text", "")))
        if not questions:
            return help_panel(request, key, "questions", conn, error="Paste the questions first")
        store.save_helper_questions(conn, key, "pasted", [q.to_dict() for q in questions], reviewed=False)
        return help_panel(request, key, "questions", conn)

    @app.post("/job/help/questions/change", response_class=HTMLResponse)
    async def job_help_change(request: Request, conn=Depends(db)):
        """Edit, delete or merge a question, add one, or confirm the list after a paste. An edited
        question's draft is out of date until it is redrafted (the template compares the texts)."""
        form = await request.form()
        key, qid, action = str(form.get("key", "")), str(form.get("qid", "")), str(form.get("action", ""))
        saved = store.helper_questions(conn, key) or {}
        questions = saved.get("questions", [])
        if action == "edit":
            questions = helper.edit_question(questions, qid, str(form.get("label", "")))
        elif action == "delete":
            questions = helper.delete_question(questions, qid)
            store.delete_helper_draft(conn, key, "question", qid)
        elif action == "merge":
            following = next((questions[i + 1]["id"] for i, q in enumerate(questions[:-1]) if q["id"] == qid), None)
            questions = helper.merge_with_next(questions, qid)
            if following:
                store.delete_helper_draft(conn, key, "question", following)
        elif action == "add":
            questions = helper.add_question(questions, str(form.get("label", "")))
        store.update_helper_questions(conn, key, questions, reviewed=True if action == "reviewed" else None)
        return help_panel(request, key, "questions", conn)

    @app.post("/job/help/facts", response_class=HTMLResponse)
    async def job_help_facts(request: Request, conn=Depends(db)):
        """"Needed from you", filled in: kept with the draft, which is redone with them; the facts
        are then offered for profile.md (written only when the user says so)."""
        form = await request.form()
        key, kind, qid = str(form.get("key", "")), str(form.get("kind", "question")), str(form.get("qid", ""))
        if demo:
            return help_panel(request, key, "letter" if kind == "letter" else "questions", conn,
                              note=demo_help_note("letter" if kind == "letter" else "questions", key, conn))
        facts = {str(form.get(f"fact_{i}_name", "")): str(form.get(f"fact_{i}", "")).strip() for i in range(20)
                 if form.get(f"fact_{i}_name") and str(form.get(f"fact_{i}", "")).strip()}
        if not facts:
            return help_panel(request, key, "letter" if kind == "letter" else "questions", conn,
                              error="Fill in at least one of the facts first")
        drafts = store.helper_drafts(conn, key)
        merged = {**(drafts.get((kind, qid)) or {}).get("facts", {}), **facts}
        store.save_helper_facts(conn, key, kind, qid, merged)
        profile_offers[key] = list(dict.fromkeys(profile_offers.get(key, []) + helper.profile_lines(facts)))
        role = help_inputs(key, conn)
        if kind == "letter":
            low, high = (store.helper_questions(conn, key) or {}).get("letter_words") or helper.LETTER_WORDS

            def work(c):
                result = helper.draft_letter(paths.config, all_settings, role, low, high, runner, facts=merged)
                if result.output:
                    store.save_helper_draft(c, key, "letter", "letter", result.output["letter"], [], result.output["missing"])
                return result
            start_help(key, "letter", work)
            return help_panel(request, key, "letter", conn)
        questions = [helper.Question.from_dict(q) for q in (store.helper_questions(conn, key) or {}).get("questions", [])]
        wanted = [q for q in questions if q.id == qid and q.kind in helper.DRAFTED]
        if wanted:
            def work(c):
                result = helper.draft_answers(paths.config, all_settings, role, wanted, runner, facts={qid: merged})
                for a in (result.output or {}).get("answers", []):
                    store.save_helper_draft(c, key, "question", str(a["id"]), a["answer"], a["choices"], a["missing"],
                                            question=wanted[0].label)
                return result
            start_help(key, "questions", work, items=1)
        return help_panel(request, key, "questions", conn)

    @app.post("/job/help/profile", response_class=HTMLResponse)
    async def job_help_profile(request: Request, conn=Depends(db)):
        """Add the offered facts to profile.md, or not: only on the user's click."""
        form = await request.form()
        key, tab = str(form.get("key", "")), str(form.get("tab", "questions"))
        lines = profile_offers.pop(key, [])
        if form.get("add") and lines:
            helper.add_to_profile(paths.config, lines, date.today().isoformat())
            return help_panel(request, key, tab, conn, notice=f"Added {len(lines)} fact{'s' if len(lines) != 1 else ''} "
                              "to profile.md: future drafts know them. Scores are kept (their prompt version is now older).")
        return help_panel(request, key, tab, conn)

    @app.post("/job/help/letter/pdf")
    async def job_help_letter_pdf(request: Request, conn=Depends(db)) -> Response:
        """The letter as a one-page PDF, with the contact header from personal.toml (read now, never stored)."""
        from .. import letter_pdf
        form = await request.form()
        key = str(form.get("key", ""))
        found, error = job_or_404(key, conn)
        if error:
            return error
        body = str(form.get("answer", ""))
        drafts = store.helper_drafts(conn, key)
        if body.strip() and body != (drafts.get(("letter", "letter")) or {}).get("answer"):
            store.save_helper_draft(conn, key, "letter", "letter", body, edited=True)
        personal = cfg.load_personal(paths.config)
        candidate = cfg.load_candidate(paths.config).get("candidate", {}) if (paths.config / "candidate.toml").exists() else {}
        contact = {"name": str(personal.get("name") or candidate.get("full_name") or ""),
                   **{k: str(personal.get(k) or "") for k in ("email", "phone", "linkedin")}}
        company = found[0].company_name
        try:
            pdf = await asyncio.to_thread(letter_pdf.build, contact, company, body, date.today())
        except letter_pdf.LetterError as e:
            return PlainTextResponse(str(e), status_code=400)
        except Exception as e:  # noqa: BLE001 (no browser, for example)
            return PlainTextResponse(f"Could not make the PDF: {e}", status_code=500)
        name = letter_pdf.file_name(contact["name"] or "Cover letter", company)
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.post("/job/help/questions/draft", response_class=HTMLResponse)
    async def job_help_draft(request: Request, conn=Depends(db)):
        """Draft every question that needs the model (edited answers are kept), or redraft one."""
        form = await request.form()
        key, only = str(form.get("key", "")), str(form.get("qid", "")).strip()
        if demo:
            return help_panel(request, key, "questions", conn, note=demo_help_note("questions", key, conn))
        saved = store.helper_questions(conn, key) or {}
        questions = [helper.Question.from_dict(q) for q in saved.get("questions", [])]
        drafts = store.helper_drafts(conn, key)
        if not saved.get("reviewed", True):
            return help_panel(request, key, "questions", conn, error="Check the questions first (Looks right)")
        wanted = [q for q in questions if q.kind in helper.DRAFTED and (
            q.id == only if only else not (drafts.get(("question", q.id)) or {}).get("edited"))]
        if wanted:
            role = help_inputs(key, conn)
            facts = {q.id: (drafts.get(("question", q.id)) or {}).get("facts", {}) for q in wanted}
            labels = {q.id: q.label for q in wanted}

            def work(c):
                result = helper.draft_answers(paths.config, all_settings, role, wanted, runner, facts=facts)
                for a in (result.output or {}).get("answers", []):
                    store.save_helper_draft(c, key, "question", str(a["id"]), a["answer"], a["choices"], a["missing"],
                                            question=labels.get(str(a["id"])))
                return result
            start_help(key, "questions", work, items=len(wanted))
        return help_panel(request, key, "questions", conn)

    @app.post("/job/help/letter", response_class=HTMLResponse)
    async def job_help_letter(request: Request, conn=Depends(db)):
        form = await request.form()
        key = str(form.get("key", ""))
        if demo:
            return help_panel(request, key, "letter", conn, note=demo_help_note("letter", key, conn))
        try:
            low, high = sorted((max(50, min(1000, int(form.get("low") or 250))), max(50, min(1000, int(form.get("high") or 350)))))
        except ValueError:
            low, high = helper.LETTER_WORDS
        store.save_letter_words(conn, key, low, high)
        role = help_inputs(key, conn)

        letter_facts = (store.helper_drafts(conn, key).get(("letter", "letter")) or {}).get("facts", {})

        def work(c):
            result = helper.draft_letter(paths.config, all_settings, role, low, high, runner, facts=letter_facts)
            if result.output:
                store.save_helper_draft(c, key, "letter", "letter", result.output["letter"], [], result.output["missing"])
            return result
        start_help(key, "letter", work)
        return help_panel(request, key, "letter", conn)

    @app.post("/job/help/save")
    async def job_help_save(request: Request, conn=Depends(db)) -> Response:
        """An answer or letter the user edited: kept as theirs (Draft all leaves it alone)."""
        form = await request.form()
        kind = "letter" if form.get("kind") == "letter" else "question"
        store.save_helper_draft(conn, str(form.get("key", "")), kind, str(form.get("qid", "letter")),
                                str(form.get("answer", "")), edited=True)
        return Response(status_code=204)

    def back_label(back: str) -> str:
        parts = urlsplit(back)
        query = dict(parse_qsl(parts.query))
        if parts.path == "/today":
            return "Today"
        if parts.path == "/applications":
            if stages.is_valid(query.get("stage", "")):
                return stages.BY_KEY[query["stage"]].label
            return "Needs follow-up" if query.get("view") == "stale" else "Applications"
        if query.get("company"):
            entry = wishlist.load(paths.state_dir).get(query["company"])
            return entry["name"] if entry else query["company"]
        return cat.VIEWS.get(query.get("view") or "scored", "Jobs")

    def job_redirect(key: str, form, **extra: str) -> RedirectResponse:
        """Back to Job detail after one of its actions, keeping where it was opened from."""
        back = safe_back(str(form.get("back") or ""))
        params = {"key": key, **({"back": back} if back != "/jobs" else {}), **{k: v for k, v in extra.items() if v}}
        return RedirectResponse("/job?" + urlencode(params), status_code=303)

    @app.post("/job/stage", response_class=HTMLResponse)
    async def job_stage(request: Request, conn=Depends(db)):
        form = await request.form()
        key, stage = str(form.get("key", "")), str(form.get("stage", ""))
        found, error = job_or_404(key, conn)
        if error:
            return error
        row, _ = found
        previous, undo = row.stage or "none", bool(form.get("undo"))
        try:
            store.set_job_stage(conn, job_key=key, company=row.record["company"], role=row.record["title"],
                                location="; ".join(row.locations), stage=stage, undo=undo)
        except store.StoreError as e:
            return PlainTextResponse(str(e), status_code=400)
        if form.get("from") == "detail":
            notice = "Marked as applied: it is on the Applications page now" if stage == "applied" else None
            return job_redirect(key, form, notice=notice or "")
        marked = stage in stages.SENT and previous not in stages.SENT and not undo
        return job_row_response(request, key, conn, str(form.get("list_view", "")),
                                marked_from=previous if marked else None, layout=str(form.get("layout", "")))

    @app.post("/job/mark", response_class=HTMLResponse)
    async def job_mark(request: Request, conn=Depends(db)):
        form = await request.form()
        key = str(form.get("key", ""))
        found, error = job_or_404(key, conn)
        if error:
            return error
        changes = {k: str(form[k]) for k in ("hidden", "on_linkedin", "notes") if k in form}
        try:
            store.set_job_mark(conn, key, **changes)
        except store.StoreError as e:
            return PlainTextResponse(str(e), status_code=400)
        if form.get("autosave"):  # the notes on Job detail, saved as the user types
            return Response(status_code=204)
        if form.get("from") == "detail":
            return job_redirect(key, form)
        return job_row_response(request, key, conn, str(form.get("list_view", "")),
                                hiding="hidden" in changes and not form.get("undo"), layout=str(form.get("layout", "")))

    @app.post("/jobs/bulk")
    async def jobs_bulk(request: Request, conn=Depends(db)) -> Response:
        """Hide, mark as applied or block (their companies) several roles at once; with `undo`, the
        reverse (`previous` holds each role's stage before marking it applied)."""
        form = await request.form()
        action, undo = str(form.get("action", "")), bool(form.get("undo"))
        keys = [str(k) for k in form.getlist("keys") if k]
        if action not in ("hide", "applied", "block") or not keys:
            return PlainTextResponse("Choose roles and an action", status_code=400)
        previous = json.loads(str(form.get("previous") or "{}"))
        rows = {}
        for key in keys:
            found = catalog.get(key, user_data(conn))
            if found is not None:
                rows[key] = found[0]
        companies = sorted({row.record["company"] for row in rows.values()})
        if action == "hide":
            for key in rows:
                store.set_job_mark(conn, key, hidden=0 if undo else 1)
        elif action == "applied":
            for key, row in rows.items():
                before = row.stage or "none"
                previous.setdefault(key, before)
                stage = previous.get(key, "none") if undo else "applied"
                if undo or before not in stages.SENT:
                    store.set_job_stage(conn, job_key=key, company=row.record["company"], role=row.record["title"],
                                        location="; ".join(row.locations), stage=stage, undo=undo)
        else:  # block: the roles' companies
            for name in companies:
                key = blocklist.company_id(name)
                was_wished = key in wishlist.names(paths.state_dir)
                before = blocklist.load(paths.state_dir).get(key, {})
                if not undo and was_wished:
                    wishlist.set_company(paths.state_dir, name, False, source="block")
                if undo and before.get("was_wishlisted"):
                    wishlist.set_company(paths.state_dir, name, True, source="block")
                blocklist.set_company(paths.state_dir, name, not undo, was_wishlisted=was_wished if not undo else False)
            wishlist_changed(f"Blocked: {'remove' if undo else 'add'} {', '.join(companies)}")
        events: dict = {"applications-changed": ""}
        detail = {"action": action, "undo": undo, "keys": list(rows), "previous": previous,
                  "companies": [blocklist.company_id(c) for c in companies], "names": companies}
        events["jobs-bulk"] = detail
        response = Response(status_code=200)
        response.headers["HX-Trigger"] = json.dumps(events)
        return response

    def job_row_response(request: Request, key: str, conn, list_view: str, hiding: bool = False,
                         marked_from: str | None = None, layout: str = "") -> Response:
        """The updated row, or something empty when the change moved it off the list it is on.

        Hiding (or showing again, on the Hidden list) leaves an empty placeholder row where the role
        was and asks the page for an Undo toast; Undo swaps the role back into that placeholder.
        Marking a role applied does the same (`marked_from`: its stage before, which Undo restores)."""
        row, _ = catalog.get(key, user_data(conn))
        applied = bool(row.stage and row.stage != "to_apply")
        on_list = not applied and (row.mark.hidden if list_view == "hidden" else not row.mark.hidden)
        events: dict = {"applications-changed": ""}
        if on_list:
            response = templates.TemplateResponse(request, "_job_row.html", {"row": row, "list_view": list_view,
                                                                              "row_layout": layout,
                                                                              "back_url": page_url(request)})
        elif hiding and not applied:
            response = templates.TemplateResponse(request, "_job_placeholder.html", {"row": row})
            events["job-visibility"] = {"key": key, "hidden": row.mark.hidden, "list_view": list_view, "layout": layout,
                                        "label": f"{row.record['company']}, {row.record['title']}"}
        elif marked_from is not None and applied:
            response = templates.TemplateResponse(request, "_job_placeholder.html", {"row": row})
            events["job-applied"] = {"key": key, "previous": marked_from, "list_view": list_view, "layout": layout,
                                     "label": f"{row.record['company']}, {row.record['title']}"}
        else:
            response = HTMLResponse("")
        response.headers["HX-Trigger"] = json.dumps(events)  # ASCII: other letters are escaped
        return response

    # --- Wishlist --------------------------------------------------------------------------------

    def wishlist_events(entry: dict, undo: bool, **extra) -> dict:
        """Refresh the sidebar, set every star of the company, and (unless this is an Undo) offer Undo."""
        change = {"id": entry["id"], "name": entry["name"], "on": entry["on"], **extra}
        events: dict = {"applications-changed": "", "wishlist-updated": change}
        if not undo:
            events["wishlist-changed"] = change
        return events

    @app.post("/wishlist/star", response_class=HTMLResponse)
    async def wishlist_star(request: Request, conn=Depends(db)):
        """The star on a role (Jobs row or Job detail): wishlist its company, or remove it."""
        form = await request.form()
        key = str(form.get("key", ""))
        found, error = job_or_404(key, conn)
        if error:
            return error
        row, _ = found
        on = form.get("on") in ("1", "true", "on")
        company = row.record["company"]
        board = wishlist.board_for(paths.config, paths.state_dir, company, row.record["source"]) if on else None
        entry = wishlist.set_company(paths.state_dir, company, on, board, source="star")
        wishlist_changed(f"Wishlist: {'add' if on else 'remove'} {company}")
        if form.get("from") == "detail":
            notice = f"{'Added' if on else 'Removed'} {entry['name']} {'to' if on else 'from'} the wishlist"
            return job_redirect(key, form, notice=notice)
        row, _ = catalog.get(key, user_data(conn))
        response = templates.TemplateResponse(request, "_job_row.html",
                                              {"row": row, "list_view": str(form.get("list_view", "")),
                                               "row_layout": str(form.get("layout", "")), "back_url": page_url(request)})
        response.headers["HX-Trigger"] = json.dumps(wishlist_events(entry, bool(form.get("undo")), key=key))
        return response

    @app.post("/wishlist/set")
    async def wishlist_set(request: Request) -> Response:
        """Add a company found by the Add sheet, remove one from the sidebar, or undo either."""
        form = await request.form()
        on = form.get("on") in ("1", "true", "on")
        name, board, source = str(form.get("name", "")), None, "app"
        if on and form.get("choice"):  # from the Add sheet: the board the user chose (or the name only)
            try:
                choice = json.loads(str(form["choice"]))
                name, board, source = choice["name"], choice.get("config") or None, choice.get("source") or "app"
            except (ValueError, KeyError, TypeError):
                return PlainTextResponse("Unreadable choice", status_code=400)
        try:
            entry = wishlist.set_company(paths.state_dir, name, on, board, source=source)
        except ValueError as e:
            return PlainTextResponse(str(e), status_code=400)
        wishlist_changed(f"Wishlist: {'add' if on else 'remove'} {entry['name']}")
        if on and form.get("fetch_now") and entry.get("identity") and not demo:
            # "Fetch and score now": fetch it on this laptop at once; scoring waits for the user's go.
            fetch_now.start_fetch(entry["name"], entry["id"], count_fetched)
            return RedirectResponse("/jobs?" + urlencode({"view": "all", "company": entry["id"], "fetch": "1"}),
                                    status_code=303)
        if request.headers.get("hx-request"):
            response = Response(status_code=200)
            response.headers["HX-Trigger"] = json.dumps(wishlist_events(entry, bool(form.get("undo"))))
            return response
        notice = (f"Added {entry['name']} to the wishlist" + (
            ": checked every run from the next one" if entry.get("identity") else
            " (no board: its roles from other sources get the star)")) if on else f"Removed {entry['name']}"
        return RedirectResponse("/jobs?" + urlencode({"view": "all", "company": entry["id"], "notice": notice}),
                                status_code=303)

    def fetch_panel(request: Request, company_id: str) -> HTMLResponse:
        task = fetch_now.get(company_id)
        if task is None:
            return HTMLResponse("")
        week = pipeline.claude_week(State.read_only(paths.state_dir), datetime.now(UTC)) if task.stage == "fetched" else None
        return templates.TemplateResponse(request, "_fetch_panel.html", {
            "task": task, "holder": data_sync.busy_text(data_sync.lock_holder(paths.state_dir)) if data_sync.lock_holder(paths.state_dir) else "",
            "week": week, "guard_at": float(all_settings.get("usage", {}).get("weekly_guard_share", 0.75))})

    @app.get("/wishlist/progress", response_class=HTMLResponse)
    def wishlist_progress(request: Request, company: str):
        return fetch_panel(request, company)

    @app.post("/wishlist/score", response_class=HTMLResponse)
    async def wishlist_score(request: Request):
        """Score what "Fetch and score now" found, after the user confirmed: one named run for the
        company, with a budget a little above the estimate (the usage guard still applies)."""
        form = await request.form()
        if demo:
            return PlainTextResponse(demo_off["fetch"], status_code=400)
        task = fetch_now.get(str(form.get("company", "")))
        if task is None or task.stage != "fetched" or not task.counts.get("waiting"):
            return PlainTextResponse("Nothing to score for this company now", status_code=400)
        budget = min(0.5, max(0.02, task.counts["windows"] * 1.25))
        fetch_now.start_scoring(task, budget, count_scored)
        return fetch_panel(request, task.company_id)

    @app.get("/wishlist/add", response_class=HTMLResponse)
    def wishlist_add(request: Request, name: str = "", url: str = ""):
        return templates.TemplateResponse(request, "_wishlist_sheet.html", {"step": "ask", "name": name, "url": url})

    @app.post("/wishlist/find", response_class=HTMLResponse)
    async def wishlist_find(request: Request):
        """What the tool knows about a company, or what `detect` finds; nothing is saved yet."""
        form = await request.form()
        name, url = str(form.get("name", "")).strip(), str(form.get("url", "")).strip() or None
        context: dict = {"step": "found", "name": name, "url": url or "", "options": [], "notes": []}
        if not name:
            return templates.TemplateResponse(request, "_wishlist_sheet.html",
                                              {**context, "step": "ask", "error": "Give the company's name"})
        current = wishlist.load(paths.state_dir).get(wishlist.company_id(name))
        if current and current.get("on"):
            context["notes"].append(f"{current['name']} is already on the wishlist.")
        known_options(name, context)
        if not context["options"]:
            await asyncio.to_thread(find_boards, name, url, context)
        if form.get("retry") and not context["options"]:
            return templates.TemplateResponse(request, "_wishlist_sheet.html", {**context, "step": "retry_failed",
                                                                                "company_id": str(form.get("company_id", ""))})
        if not context["options"] and not (current and current.get("on")):
            # Nothing works: say why and keep the name, so its roles found elsewhere get the star.
            entry = wishlist.set_company(paths.state_dir, name, True, None, source="name only")
            wishlist_changed(f"Wishlist: add {entry['name']} (name only)")
            response = templates.TemplateResponse(request, "_wishlist_sheet.html", {**context, "step": "saved",
                                                                                    "entry": entry})
            response.headers["HX-Trigger"] = json.dumps(wishlist_events(entry, False))
            return response
        return templates.TemplateResponse(request, "_wishlist_sheet.html", context)

    def known_options(name: str, context: dict) -> None:
        """Boards the tool already knows for the name: companies.toml and the generated list."""
        for c in cfg_companies():
            if wishlist.company_id(c.name) == wishlist.company_id(name) and c.board:
                board = {k: v for k, v in {"board": c.board, "slug": c.slug, "url": c.url, "region": c.region}.items() if v}
                context["options"].append({"label": f"{c.board} {c.slug or c.url or ''} (in companies.toml)",
                                           "config": board, "name": c.name, "source": "companies.toml"})
                break
        for g in wishlist.generated_matches(paths.state_dir, name):
            context["options"].append({"label": f"{g['config']['board']} {g['config'].get('slug') or g['config'].get('url')}"
                                                f" (on the generated list, {g['open_roles']} open roles)",
                                       "config": g["config"], "name": g["name"], "source": "generated list"})

    def find_boards(name: str, url: str | None, context: dict) -> None:
        """`detect` on the careers page, or name-based slugs on every supported board; with a name
        alone and nothing found, a web search (WebSearch only) for the careers site, which `detect`
        then reads. Adds the options and the reasons to `context`."""
        from .. import detect, discover
        from ..sources import Http, SourceError
        context["detected"] = True
        if demo:
            context["notes"].append(demo_off["boards"])
            return
        http = Http.from_settings(all_settings)
        step = context.get("progress") or (lambda text: None)

        def add(findings, via: str = "") -> None:
            for f in findings:
                if f.supported and f.works and f.board != "custom" and f.jobs:
                    label = (f"{f.board} {f.config.get('slug') or f.config.get('url', '')}"
                             f"{' (EU)' if f.config.get('region') else ''}: {f.jobs} jobs")
                    context["options"].append({"label": label, "config": f.config, "name": name, "samples": f.samples[:3],
                                               "sample": None if f.samples else f.sample,
                                               "notes": f.notes + ([via] if via else []), "source": "detect"})
                elif f.board == "custom" and f.works and f.jobs:
                    where = "its sitemap" if f.config["listing_url"].endswith(".xml") else "its job pages"
                    context["options"].append({
                        "label": f"its own careers site, read from {where}: {f.jobs} jobs"
                                 + (" (a search page: may show only some)" if f.config.get("partial") else ""),
                        "config": f.config, "name": name, "samples": f.samples, "source": "careers site",
                        "notes": [f"Listing: {f.config['listing_url']}", f"Job links: {f.config['link_pattern']}",
                                  "Titles come from the links until each posting is read."] + ([via] if via else [])})
                elif f.board == "custom":
                    context["notes"].append(display.sentence(f"Its careers site could not be set up: {f.error}"))
                elif not f.supported:
                    context["notes"].append(f"It uses {f.board}, which the tool cannot read yet.")

        step(f"Reading {url}" if url else f"Trying every job board with names made from “{name}”")
        try:
            findings, _ = detect.detect(url, http, name)
        except SourceError as e:
            findings = []
            context["notes"].append(display.sentence(f"Could not read {url}: {detect.explain(e)}"))
        add(findings)
        if context["options"] or url:
            return
        context["notes"].append("No job board answered to names made from the company's name.")
        if demo:
            context["notes"].append("The web search for its careers site is switched off in the demo.")
            return
        step("Searching the web for its careers site")
        found = discover.find_careers_site(name, all_settings, runner)
        context["searched"] = True
        if found.error:
            context["notes"].append(display.sentence(f"The web search did not work: {found.error}"))
            return
        if not found.urls:
            context["notes"].append(display.sentence(f"A web search found no careers page{': ' + found.note if found.note else ''}"))
            return
        for link in found.urls:
            via = f"Found with a web search: {link}" + (f" ({found.note})" if found.note else "")
            step(f"Reading {link}")
            try:
                more, _ = detect.detect(link, http, None)
            except SourceError as e:
                context["notes"].append(display.sentence(f"A web search pointed to {link}, which could not be read: {detect.explain(e)}"))
                continue
            add(more, via)
            if context["options"]:
                return
            context["notes"].append(f"A web search pointed to {link}, but no jobs could be read from it.")

    # --- Companies -------------------------------------------------------------------------------

    @app.get("/companies", response_class=HTMLResponse)
    def companies_page(request: Request, q: str = "", group: str = "all", conn=Depends(db)):
        """Every company in one place (2026-11-18): yours, filtered by list, or one search across
        them and the generated list; a search that finds nothing offers to set the company up."""
        q = q.strip()
        runs = read_jsonl(paths.state_dir / "runs.jsonl")
        apps = limits.applications_from_store(conn)
        generated = companies_view.generated_entries(paths.state_dir) if q else []
        norms = {prefilter_norm(c.name) for c in cfg_companies()} | set(wishlist.names(paths.state_dir)) \
            | set(blocklist.names(paths.state_dir)) | set(accepted.names(paths.state_dir)) \
            | {prefilter_norm(companies_view.entry_name(e)) for e in companies_view.search_generated(generated, q)}
        yours, found = companies_view.rows(paths.config, paths.state_dir, runs, apps, catalog.company_counts(norms),
                                           date.today(), q, generated, catalog.company_roles(norms))
        reminders = store.manual_checks(conn)
        for row in yours:
            row.remind = row.id in reminders
        group_counts = {key: sum(r.group == key for r in yours) for key in ("wishlist", "yours", "found", "blocked")}
        if group not in group_counts:
            group = "all"
        if q:
            shown = [r for r in yours if companies_view.matches(r, q)] + found
        else:
            shown = [r for r in yours if group == "all" or r.group == group]
        return templates.TemplateResponse(request, "companies.html", {
            "yours": yours, "shown": shown, "q": q, "group": group, "group_counts": group_counts,
            "groups": companies_view.GROUPS, "generated_total": len(companies_view.generated_entries(paths.state_dir)),
            "back_url": page_url(request), "open_find": request.query_params.get("find") == "1",
            "selected_page": "companies", "selected_job_view": None, **sidebar_context(conn)})

    def company_row(company_id: str, conn):
        runs = read_jsonl(paths.state_dir / "runs.jsonl")
        yours, _ = companies_view.rows(paths.config, paths.state_dir, runs, {}, {}, date.today())
        row = next((r for r in yours if r.id == company_id), None)
        if row is not None:
            row.remind = row.id in store.manual_checks(conn)
        return row

    @app.get("/companies/coverage", response_class=HTMLResponse)
    def companies_coverage(request: Request, company: str, conn=Depends(db)):
        """Why a company of yours is not fully covered, and what can be done (a sheet)."""
        row = company_row(company, conn)
        if row is None:
            return PlainTextResponse("No such company on your list or wishlist", status_code=404)
        return templates.TemplateResponse(request, "_coverage_sheet.html", {"c": row, "cov": row.coverage})

    @app.post("/companies/remind", response_class=HTMLResponse)
    async def companies_remind(request: Request, conn=Depends(db)):
        """Remind me to check manually: on Today's weekly list, or off it."""
        form = await request.form()
        company, on = str(form.get("company", "")), form.get("on") in ("1", "true", "on")
        row = company_row(company, conn)
        if row is None:
            return PlainTextResponse("No such company on your list or wishlist", status_code=404)
        store.set_manual_check(conn, row.id, row.name, on)
        row.remind = on
        response = templates.TemplateResponse(request, "_coverage_sheet.html", {"c": row, "cov": row.coverage})
        response.headers["HX-Trigger"] = json.dumps({"applications-changed": ""})
        return response

    # --- Find more companies (2026-10-30) ---------------------------------------------------------

    search_state: dict = {"task": None}  # one search at a time

    def search_context(conn, **extra) -> dict:
        from .. import company_search as cs
        data = store.company_search(conn)
        usage = all_settings.get("usage", {})
        # The candidate's own countries, ticked (2026-11-19): the main countries, or the search's own
        # default where the config names none (the example candidate's).
        main = list(all_settings.get("generated", {}).get("main_countries", [])
                    or all_settings.get("company_search", {}).get("countries", []))
        decided = {r["id"]: r.get("decision") for r in data["results"]}
        return {"main_countries": main, "more_countries": [c for c in (cs.MORE_COUNTRIES if main else cs.COUNTRY_NAMES) if c not in main],
                "country_names": cs.COUNTRY_NAMES, "domains": cs.DOMAIN_LABELS,
                "estimate": cs.estimate(data["history"], float(usage.get("windows_per_week", 12.5))),
                "history": data["history"][::-1][:5], "results": data["results"], "decided": decided,
                "rejected": data["rejected"], "task": search_state["task"], "board_labels": companies_view.BOARD_LABELS,
                "week": (catalog.status() or {}).get("claude_week"), **extra}

    def search_sheet(request: Request, conn, step: str | None = None, **extra):
        context = search_context(conn, **extra)
        task = context["task"]
        if step is None:
            step = ("running" if task and task.stage in ("searching", "checking")
                    else "results" if any(not r.get("decision") for r in context["results"]) else "ask")
        return templates.TemplateResponse(request, "_find_sheet.html", {**context, "step": step})

    def finish_search(task) -> None:
        """Keep the search's cost and its suggestions (in the tracker database)."""
        from .. import company_search as cs
        conn = store.connect(paths.database)
        try:
            data = store.company_search(conn)
            usage = all_settings.get("usage", {})
            window = task.cost_usd * helper.WINDOW_SHARE_PER_USD
            data["history"].append(cs.record(task, window, window / float(usage.get("windows_per_week", 12.5))))
            data["history"] = data["history"][-50:]
            data["results"] = [{**r, "id": cs.norm(r["name"]), "decision": None} for r in task.suggestions]
            store.save_company_search(conn, data)
        finally:
            conn.close()

    def mine_names() -> set[str]:
        from .. import accepted
        return ({prefilter_norm(c.name) for c in cfg_companies()} | set(wishlist.names(paths.state_dir))
                | set(blocklist.names(paths.state_dir)) | set(accepted.names(paths.state_dir)))

    @app.get("/companies/find", response_class=HTMLResponse)
    def companies_find(request: Request, new: int = 0, conn=Depends(db)):
        return search_sheet(request, conn, "ask" if new else None)

    @app.post("/companies/find/start", response_class=HTMLResponse)
    async def companies_find_start(request: Request, conn=Depends(db)):
        from .. import company_search as cs
        from ..sources import Http
        form = await request.form()
        if demo:  # it calls claude -p with web search directly, not through the app's runner
            return search_sheet(request, conn, "ask", error=demo_off["search"])
        countries = [c for c in form.getlist("country") if isinstance(c, str) and c in cs.COUNTRY_NAMES]
        domains = [d for d in form.getlist("domain") if isinstance(d, str) and d in cs.DOMAINS]
        if not countries or not domains:
            return search_sheet(request, conn, "ask", error="Pick at least one country and one field")
        running = search_state["task"]
        if running and running.stage in ("searching", "checking"):
            return search_sheet(request, conn, "running")
        data = store.company_search(conn)
        mine = mine_names()
        known = cs.known_names(paths.state_dir, mine, set(data["rejected"]))
        exclude = sorted({c.name for c in cfg_companies()} | {e["name"] for e in wishlist.active(paths.state_dir)}
                         | {e["name"] for e in blocklist.active(paths.state_dir)} | set(data["rejected"].values()))
        task = cs.Task(countries=countries, domains=domains)
        search_state["task"] = task
        model = all_settings.get("company_search", {}).get("model", "sonnet")

        def work() -> None:
            try:
                cs.run(task, cs.web_search(model), lambda: Http.from_settings(all_settings), known, exclude)
            except Exception as e:  # shown in the sheet; the search is not retried by itself
                task.stage, task.error = "error", f"The search stopped: {e}"[:300]
            finish_search(task)

        threading.Thread(target=work, daemon=True).start()
        return search_sheet(request, conn, "running")

    @app.get("/companies/find/progress", response_class=HTMLResponse)
    def companies_find_progress(request: Request, conn=Depends(db)):
        task = search_state["task"]
        if task is None:
            return search_sheet(request, conn)
        if task.stage in ("searching", "checking"):
            return search_sheet(request, conn, "running")
        return search_sheet(request, conn, "error" if task.stage == "error" and not task.suggestions else "results",
                            just_finished=True)

    @app.post("/companies/find/decide", response_class=HTMLResponse)
    async def companies_find_decide(request: Request, conn=Depends(db)):
        """Cover it (the generated list's rules), add it to the wishlist, reject it, or undo."""
        from .. import accepted
        form = await request.form()
        company, decision = str(form.get("company", "")), str(form.get("decision", ""))
        data = store.company_search(conn)
        result = next((r for r in data["results"] if r["id"] == company), None)
        if result is None or decision not in ("covered", "wishlist", "rejected", "undo"):
            return PlainTextResponse("No such suggestion", status_code=404)
        previous = result.get("decision")
        found = {k: result.get(k) for k in ("domain", "city", "why", "entry_level", "careers_url")}
        if decision == "undo":
            if previous == "covered":
                accepted.set_company(paths.state_dir, result["name"], False)
                wishlist_changed(f"Companies: {result['name']} no longer covered")
            elif previous == "wishlist":
                wishlist.set_company(paths.state_dir, result["name"], False, None, source="company search")
                wishlist_changed(f"Wishlist: remove {result['name']}")
            data["rejected"].pop(company, None)
            result["decision"] = None
        elif previous:
            return PlainTextResponse("Already decided; undo it first", status_code=409)
        elif decision == "covered":
            accepted.set_company(paths.state_dir, result["name"], True, result.get("config"), found=found)
            wishlist_changed(f"Companies: cover {result['name']} (found by search)")
        elif decision == "wishlist":
            wishlist.set_company(paths.state_dir, result["name"], True, result.get("config"), source="company search")
            wishlist_changed(f"Wishlist: add {result['name']} (found by search)")
        else:
            data["rejected"][company] = result["name"]
        if decision != "undo":
            result["decision"] = decision
        store.save_company_search(conn, data)
        response = search_sheet(request, conn, "results")
        response.headers["HX-Trigger"] = json.dumps({"applications-changed": "", "companies-changed": ""})
        return response

    # --- Set up a company that is not tracked yet (2026-11-18) ------------------------------------

    setups = setup_records.Setups(paths.database)

    def setup_work(name: str, url: str | None, context: dict) -> None:
        context["progress"]("Looking among the companies the tool already knows")
        known_options(name, context)
        if not context["options"]:
            find_boards(name, url, context)

    def setup_sheet(request: Request, conn, company_id: str = "", name: str = "", url: str = "",
                    want: str = "covered", step: str | None = None, **extra) -> HTMLResponse:
        task = setups.get(company_id) if company_id else None
        record = setup_records.load(conn).get(company_id) if company_id else None
        if step is None:
            step = ("running" if task and task.running else
                    record["status"] if record and record.get("status") in ("found", "failed", "alert") else "ask")
        if record and step in ("found", "failed") and not record.get("announced"):
            record["announced"] = True  # seen in the sheet: no notice needed
            setup_records.put(conn, record)
        return templates.TemplateResponse(request, "_setup_sheet.html", {
            "step": step, "task": task, "record": record, "id": company_id,
            "name": (record or {}).get("name") or (task.name if task else name), "url": url or (record or {}).get("url", ""),
            "want": (record or {}).get("want") or want, **extra})

    @app.get("/companies/setup", response_class=HTMLResponse)
    def companies_setup(request: Request, name: str = "", url: str = "", want: str = "covered", id: str = "",
                        step: str = "", conn=Depends(db)):
        if step == "ask":  # try again, with its careers page
            return setup_sheet(request, conn, name=display.company_name_typed(name), url=url, want=want, step="ask")
        company_id = id or (setup_records.setup_id(name) if name.strip() else "")
        if company_id and not setups.get(company_id) and company_id not in setup_records.load(conn) \
                and company_id in mine_names():
            return setup_sheet(request, conn, company_id, name=name, step="tracked")
        return setup_sheet(request, conn, company_id, name=display.company_name_typed(name), url=url, want=want)

    @app.post("/companies/setup/start", response_class=HTMLResponse)
    async def companies_setup_start(request: Request, conn=Depends(db)):
        form = await request.form()
        name, url = display.company_name_typed(str(form.get("name", ""))), str(form.get("url", "")).strip()
        want = "wishlist" if form.get("want") == "wishlist" else "covered"
        if not name:
            return setup_sheet(request, conn, step="ask", url=url, want=want, error="Give the company's name")
        company_id = setup_records.setup_id(name)
        setup_records.drop(conn, company_id)  # a new try replaces an earlier result
        setups.start(name, url, want, setup_work)
        response = setup_sheet(request, conn, company_id, step="running")
        response.headers["HX-Trigger"] = json.dumps({"setup-started": ""})
        return response

    @app.get("/companies/setup/progress", response_class=HTMLResponse)
    def companies_setup_progress(request: Request, id: str, conn=Depends(db)):
        return setup_sheet(request, conn, id)

    @app.get("/companies/setup/watch")
    def companies_setup_watch(conn=Depends(db)) -> Response:
        """Polled by the sidebar while a setup runs: says once when each finishes (status 286 stops the polling)."""
        new = setup_records.announcements(conn)
        response = Response(status_code=200 if setups.running() else 286)
        if new:
            response.headers["HX-Trigger"] = json.dumps({"setup-finished": {"items": [
                {"id": r["id"], "status": r["status"], "message": setup_records.announce(r)} for r in new]}})
        return response

    @app.post("/companies/setup/decide", response_class=HTMLResponse)
    async def companies_setup_decide(request: Request, conn=Depends(db)):
        """Cover it, wishlist it, keep the name only, or (when nothing could be read) job alerts or a
        weekly check by hand; `done` and `dismiss` clear what Today shows."""
        from .. import accepted
        form = await request.form()
        company_id, decision = str(form.get("id", "")), str(form.get("decision", ""))
        record = setup_records.load(conn).get(company_id)
        if record is None:
            return PlainTextResponse("Nothing to decide for this company", status_code=404)
        name = record["name"]
        events = {"companies-changed": "", "applications-changed": ""}
        if decision in ("covered", "wishlist"):
            try:
                choice = json.loads(str(form.get("choice", "")))
            except ValueError:
                return PlainTextResponse("Choose one of the boards found", status_code=400)
            config, chosen = choice.get("config"), choice.get("name") or name
            if not config:
                return PlainTextResponse("Choose one of the boards found", status_code=400)
            if decision == "covered":
                accepted.set_company(paths.state_dir, chosen, True, config, found={"careers_url": record.get("url") or None})
                wishlist_changed(f"Companies: cover {chosen} (set up)")
                message = f"{chosen} is covered: checked daily from the next run."
            else:
                entry = wishlist.set_company(paths.state_dir, chosen, True, config, source="set-up")
                wishlist_changed(f"Wishlist: add {chosen} (set up)")
                setup_records.drop(conn, company_id)
                if form.get("fetch_now") and entry.get("identity") and not demo:
                    fetch_now.start_fetch(entry["name"], entry["id"], count_fetched)
                    response = Response(status_code=200)
                    response.headers["HX-Redirect"] = "/jobs?" + urlencode({"view": "all", "company": entry["id"], "fetch": "1"})
                    return response
                message = f"{chosen} is on the wishlist: checked every run from the next one."
            setup_records.drop(conn, company_id)
        elif decision == "name_only":
            wishlist.set_company(paths.state_dir, name, True, None, source="name only")
            wishlist_changed(f"Wishlist: add {name} (name only)")
            setup_records.drop(conn, company_id)
            message = f"{name} is on the wishlist by name: its roles found elsewhere get the star."
        elif decision == "alerts":
            record.update(status="alert", announced=True)
            setup_records.put(conn, record)
            return setup_sheet(request, conn, company_id, step="alert")
        elif decision == "manual":
            store.set_manual_check(conn, company_id, name, True)
            record.update(status="manual", announced=True)  # kept for its careers page on the weekly list
            setup_records.put(conn, record)
            message = f"{name} is on Today's weekly list to check by hand."
        elif decision in ("done", "dismiss"):
            setup_records.drop(conn, company_id)
            message = f"Job alert for {name} noted." if decision == "done" else f"Dropped the set-up of {name}."
        else:
            return PlainTextResponse("Unknown choice", status_code=400)
        response = setup_sheet(request, conn, company_id, step="done", message=message)
        response.headers["HX-Trigger"] = json.dumps(events)
        return response

    # --- Settings (2026-11-19) -------------------------------------------------------------------

    def preferences_changed(message: str) -> None:
        """preferences.jsonl goes to the data branch at once, like the wishlist, so the next run uses it."""
        if sync_enabled():
            threading.Thread(target=push_wishlist, args=(message,), daemon=True).start()

    def settings_context(conn) -> dict:
        from .. import launchd
        laptop_scheduled = None
        if not demo and sys.platform == "darwin":
            try:
                laptop_scheduled = "token" if launchd.has_token() else "login"
            except OSError:
                laptop_scheduled = None
        login = accounts.current(all_settings) if not demo else {"key": "subscription", "how": "token"}
        head = None
        if not demo and data_sync.is_worktree(paths.state_dir):
            done = subprocess.run(["git", "-C", str(paths.state_dir), "log", "-1", "--format=%cI%x09%s"],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode == 0 and done.stdout.strip():
                at, _, subject = done.stdout.strip().partition("\t")
                head = {"at": at, "subject": subject}
        return {"apply_min_fit": cat.PRESETS["apply"]["min_fit"], "hidden_days": prefs["hidden_days"],
                "token_account": accounts.default_key(all_settings), "login": login,
                "laptop_scheduled": laptop_scheduled, "data_head": head, "linked": sync_enabled()}

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: str = "", conn=Depends(db)):
        return templates.TemplateResponse(request, "settings.html", {
            **settings_context(conn), "saved": saved, "selected_page": "settings", "selected_job_view": None,
            **sidebar_context(conn)})

    @app.post("/settings")
    async def settings_save(request: Request, conn=Depends(db)) -> Response:
        from .. import preferences
        form = await request.form()
        live = request.headers.get("hx-request") == "true"  # saved as you change it (2026-11-19)

        def status(text: str, error: bool = False) -> HTMLResponse:
            return HTMLResponse(f'<p id="settings-status" class="settings-status secondary small{" error-text" if error else ""}" '
                                f'role="status" aria-live="polite">{html.escape(text)}</p>')
        try:
            threshold = int(str(form.get("apply_min_fit", "")))
            days = int(str(form.get("hidden_days", "")))
        except ValueError:
            if live:
                return status("Not saved: give whole numbers.", error=True)
            return PlainTextResponse("Give whole numbers", status_code=400)
        if not (1 <= threshold <= 10) or not (1 <= days <= 90):
            if live:
                return status("Not saved: hidden roles are kept 1 to 90 days.", error=True)
            return PlainTextResponse("The threshold is 1 to 10, and hidden roles are kept 1 to 90 days", status_code=400)
        saved = []
        if threshold != cat.PRESETS["apply"]["min_fit"]:
            preferences.set_value(paths.state_dir, "apply_min_fit", threshold)
            cat.use_apply_min_fit(threshold)
            threading.Thread(target=catalog.refresh, daemon=True).start()
            preferences_changed(f"Settings: Apply now and phone alerts from fit {threshold}")
            saved.append("threshold")
        if days != prefs["hidden_days"]:
            store.set_preference(conn, "hidden_days", days)
            prefs["hidden_days"] = days
            saved.append("hidden")
        if live:
            if "threshold" in saved:
                return status(f"Saved: Apply now and phone alerts follow fit {threshold}+"
                              + (" from the next run." if sync_enabled() else "."))
            return status(f"Saved: hidden roles are removed after {days} days." if saved else "Saved.")
        return RedirectResponse("/settings?" + urlencode({"saved": ",".join(saved) or "none"}), status_code=303)

    @app.post("/today/checked")
    async def today_checked(conn=Depends(db)) -> Response:
        store.manual_checks_done(conn, datetime.now(UTC).isoformat(timespec="seconds"))
        return RedirectResponse("/today", status_code=303)

    def prefilter_norm(name: str) -> str:
        from .. import prefilter
        return prefilter.normalise_company(name)

    # --- Runs ----------------------------------------------------------------------------------

    RUNS_PER_PAGE = 50

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, page: int = 1, where: str = "", kind: str = "", conn=Depends(db)):
        actions_minutes.refresh()  # in the background, at most every 30 minutes
        runs = read_jsonl(paths.state_dir / "runs.jsonl")
        every = runs_view.rows(runs, all_settings)
        kinds = sorted({r.kind for r in every})
        rows = [r for r in every if (not where or r.ran_on == where) and (not kind or r.kind == kind)]
        workflow_file = paths.root / ".github" / "workflows" / "run.yml"
        health = runs_view.health(runs, workflow_file.read_text() if workflow_file.exists() else "", datetime.now(UTC),
                                  external=all_settings.get("schedule", {}).get("external_trigger"),
                                  external_timezone=all_settings.get("schedule", {}).get("external_trigger_timezone"))
        waiting = catalog.status()
        pages = max(1, -(-len(rows) // RUNS_PER_PAGE))
        page = min(max(1, page), pages)

        class _Runs:  # weekly_guard reads only the run summaries
            def runs(self):
                return runs
        guard = pipeline.weekly_guard(_Runs(), all_settings, datetime.now(UTC), claude=(waiting or {}).get("claude_week"))
        slug = actions_usage.repo_slug(paths.root)
        return templates.TemplateResponse(request, "runs.html", {
            "rows": rows[(page - 1) * RUNS_PER_PAGE: page * RUNS_PER_PAGE], "page": page, "pages": pages,
            "runs_total": len(rows), "all_total": len(every), "kinds": kinds, "where": where, "kind": kind,
            "health": health, "seven_day": runs_view.latest_seven_day(runs, (waiting or {}).get("seven_day")),
            "minutes": actions_minutes.result, "minutes_error": actions_minutes.error, "counting": actions_minutes.counting,
            **minutes_guard_context(runs), "guard": guard, "guard_text": pipeline.guard_words({**guard, "changed": False}),
            "laptop": pipeline.last_local_run(runs), "laptop_log": (paths.root / "logs" / "local-run.log").exists(),
            "triggers": runs_view.triggers(actions_minutes.result),
            "waiting_total": sum(int(part.get(k, 0)) for part in ((waiting or {}).get("parts") or {}).values()
                                 for k in ("waiting for posting text", "waiting for the title check",
                                           "waiting for a first score", "waiting for Sonnet (Haiku 6+)")),
            "helper_usage": store.helper_usage(conn, (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")),
            "guard_boards": ", ".join(pipeline.BOARD_NAMES.get(b, b) for b in guard["paused_boards"]),
            "windows_per_week": float(all_settings.get("usage", {}).get("windows_per_week", 12.5)),
            "waiting": waiting, "workflow_url": f"https://github.com/{slug}/actions/workflows/run.yml" if slug else None,
            "last_github": next((r for r in rows if r.ran_on == "github"), None),
            "selected_page": "runs", "selected_job_view": None, **sidebar_context(conn)})

    @app.get("/runs/minutes", response_class=HTMLResponse)
    def runs_minutes(request: Request):
        """The Actions minutes tile alone: the page polls it while a count runs."""
        return templates.TemplateResponse(request, "_minutes_tile.html", {
            "minutes": actions_minutes.result, "minutes_error": actions_minutes.error,
            "counting": actions_minutes.counting, **minutes_guard_context(read_jsonl(paths.state_dir / "runs.jsonl"))})

    def minutes_guard_context(runs: list[dict]) -> dict:
        """run.yml's Actions minutes guard: its thresholds, and whether the latest GitHub run was slowed."""
        workflow = paths.root / ".github" / "workflows" / "run.yml"
        latest = next((r for r in reversed(runs) if r.get("ran_on") == "github"), {})
        return {"minutes_guard": actions_usage.guard_settings(workflow.read_text() if workflow.exists() else ""),
                "actions_slow": bool((latest.get("actions_minutes") or {}).get("slow"))}

    @app.get("/runs/{index}/sheet", response_class=HTMLResponse)
    def run_sheet(index: int, request: Request):
        runs = read_jsonl(paths.state_dir / "runs.jsonl")
        if not 0 <= index < len(runs):
            return PlainTextResponse("No such run", status_code=404)
        row = runs_view.row(index, runs[index], {k: float(v) for k, v in
                                                 all_settings.get("usage", {}).get("share_per_call", {}).items()},
                            pipeline.per_usd_of(all_settings))
        row.launchd = bool(runs[index].get("launchd"))
        return templates.TemplateResponse(request, "_run_sheet.html",
                                          {"r": row, "account": accounts.key_of(runs[index], all_settings)})

    # --- Blocked companies ---------------------------------------------------------------------

    @app.post("/blocked/set")
    async def blocked_set(request: Request) -> Response:
        """Block a company (its roles leave every view; runs skip it) or unblock it, with Undo.
        Blocking a wishlist company takes it off the wishlist; unblocking puts it back."""
        form = await request.form()
        name, on, undo = str(form.get("name", "")).strip(), form.get("on") in ("1", "true", "on"), bool(form.get("undo"))
        if not name:
            return PlainTextResponse("A company name is needed", status_code=400)
        key = blocklist.company_id(name)
        was_wished = key in wishlist.names(paths.state_dir)
        before = blocklist.load(paths.state_dir).get(key, {})
        if on and was_wished:
            wishlist.set_company(paths.state_dir, name, False, source="block")
        if not on and before.get("was_wishlisted"):
            wishlist.set_company(paths.state_dir, name, True, source="block")
        entry = blocklist.set_company(paths.state_dir, name, on, was_wishlisted=was_wished if on else False)
        wishlist_changed(f"Blocked: {'add' if on else 'remove'} {entry['name']}")
        if form.get("from") == "detail":
            back = safe_back(str(form.get("back") or ""))  # its list, with an Undo toast there
            separator = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{separator}{urlencode({'blocked_toast': entry['name']})}", status_code=303)
        change = {"id": key, "name": entry["name"], "on": on}
        events: dict = {"applications-changed": "", "company-block-updated": change}
        if not undo:
            events["company-blocked"] = change
        response = Response(status_code=200)
        response.headers["HX-Trigger"] = json.dumps(events)
        return response

    @app.get("/blocked/sheet", response_class=HTMLResponse)
    def blocked_sheet(request: Request, conn=Depends(db)):
        counts = catalog.blocked_counts(user_data(conn))
        return templates.TemplateResponse(request, "_blocked_sheet.html", {
            "blocked": [(e, counts.get(e["id"], 0)) for e in blocklist.active(paths.state_dir)]})

    def cfg_companies():
        from .. import companies as co
        return co.load_companies(paths.config)

    # --- Applications ----------------------------------------------------------------------------

    @app.get("/applications", response_class=HTMLResponse)
    def applications_page(request: Request, stage: str | None = None, view: str | None = None,
                          error: str | None = None, open_app: int | None = Query(None, alias="open"), conn=Depends(db)):
        if stage and not stages.is_valid(stage):
            stage = None
        if view != "stale" or stage:
            view = None
        apps = store.list_applications(conn, stage=stage, **options())
        if view == "stale":
            apps = [a for a in apps if a.ghosted]
        if stage:
            title = stages.BY_KEY[stage].label
            groups = [(None, apps)]
        elif view == "stale":
            title = "Needs follow-up"
            groups = [(None, apps)]
        else:
            title = "Applications"
            # In progress: the latest update first (2026-11-19), as the group's heading says.
            in_progress = sorted((a for a in apps if stages.BY_KEY[a.stage].open),
                                 key=lambda a: (a.last_change or a.applied_on or ""), reverse=True)
            groups = [("In progress", in_progress),
                      ("Closed", [a for a in apps if not stages.BY_KEY[a.stage].open])]
        return templates.TemplateResponse(request, "applications.html", {
            "title": title,
            "groups": [(name, items) for name, items in groups if items or name is None],
            "shown": len(apps),
            "selected_stage": stage,
            "selected_view": view,
            "selected_page": "applications", "selected_job_view": None,
            "error": error, "open_app": open_app,
            "today": date.today().isoformat(),
            **sidebar_context(conn),
            **up_next_context(conn),
        })

    @app.get("/applications/sidebar", response_class=HTMLResponse)
    def applications_sidebar(request: Request, stage: str | None = None, view: str | None = None,
                             job_view: str | None = None, company: str | None = None, page: str | None = None,
                             conn=Depends(db)):
        return templates.TemplateResponse(request, "_sidebar.html", {
            "selected_stage": stage or None, "selected_view": view or None, "selected_job_view": job_view or None,
            "selected_company": company or None, "selected_page": page or None,
            **sidebar_context(conn)})

    @app.get("/applications/up-next", response_class=HTMLResponse)
    def applications_up_next(request: Request, conn=Depends(db)):
        return templates.TemplateResponse(request, "_upnext.html", up_next_context(conn))

    @app.post("/applications/{app_id}", response_class=HTMLResponse)
    async def update_application(app_id: int, request: Request, conn=Depends(db)):
        form = await request.form()
        changes = {k: form[k] for k in store.EDITABLE if k in form}
        list_stage = str(form.get("list_stage", ""))  # the stage list the row is shown on, if any
        try:
            before = store.get_application(conn, app_id, **options())
            store.update_application(conn, app_id, changes)
            view = store.get_application(conn, app_id, **options())
        except store.StoreError as e:
            return PlainTextResponse(str(e), status_code=400)
        events: dict = {"applications-changed": ""}
        if view.stage == "to_apply" or (list_stage and view.stage != list_stage):
            # Off this list (To apply: off the Applications page, back to Jobs): leave a placeholder
            # where the row was, for Undo to fill again.
            response = HTMLResponse(f'<tr class="app-placeholder" data-placeholder="app-{app_id}" hidden></tr>')
            if not form.get("undo"):
                events["application-moved"] = {"id": app_id, "label": f"{view.company}, {view.role}",
                                               "stage": view.stage, "stage_label": view.stage_label,
                                               "back_to": before.stage, "list_stage": list_stage,
                                               "applied_on": before.applied_on or ""}
        else:
            response = templates.TemplateResponse(request, "_row.html", {"a": view, "list_stage": list_stage})
            if view.next_step_by and not before.next_step_by:
                response.headers["HX-Refresh"] = "true"  # its group may now need the Next step column
        response.headers["HX-Trigger"] = json.dumps(events)
        return response

    @app.get("/applications/{app_id}/sheet", response_class=HTMLResponse)
    def application_sheet(app_id: int, request: Request, conn=Depends(db)):
        try:
            view = store.get_application(conn, app_id, **options())
        except store.StoreError as e:
            return PlainTextResponse(str(e), status_code=404)
        return templates.TemplateResponse(request, "_sheet.html", {
            "a": view, "history": store.stage_history(conn, app_id), "back_url": page_url(request)})

    @app.post("/applications")
    async def add_application(request: Request, conn=Depends(db)) -> Response:
        form = await request.form()
        try:
            store.add_application(
                conn,
                company=str(form.get("company", "")), role=str(form.get("role", "")),
                location=str(form.get("location", "")), applied_on=str(form.get("applied_on", "")) or None,
                stage=str(form.get("stage", "applied")), notes=str(form.get("notes", "")),
            )
        except store.StoreError as e:
            return RedirectResponse(f"/applications?{urlencode({'error': str(e)})}", status_code=303)
        return RedirectResponse("/applications", status_code=303)

    return app


def when(value: str | None) -> str:
    """'27 Oct 13:54' in local time, from an ISO timestamp."""
    if not value:
        return ""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    return f"{moment.day} {moment:%b %H:%M}"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


def table_date(value: str | None, today: date | None = None) -> str:
    """For narrow table columns: '7 Oct' this year, 'Jun 2025' otherwise."""
    if not value:
        return ""
    d = date.fromisoformat(value[:10])
    return f"{d.day} {d:%b}" if d.year == (today or date.today()).year else f"{d:%b} {d.year}"


def short_date(value: str | None, today: date | None = None) -> str:
    """'7 Oct' this year, '7 Oct 2025' otherwise."""
    if not value:
        return ""
    d = date.fromisoformat(value[:10])
    today = today or date.today()
    return f"{d.day} {d:%b}" + ("" if d.year == today.year else f" {d.year}")


def ago(days: int | None) -> str:
    if days is None:
        return ""
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


def asset_version(static: Path) -> str:
    """Fingerprint of the static files, appended to their URLs so a changed file is never served from cache."""
    digest = hashlib.sha1()
    for path in sorted(static.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]

