"""The Companies page: my companies, the wishlist and blocked companies in one list, and a search
over the generated list, each with its board, last successful fetch, roles, applications, limits
and whether it fails in GitHub runs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import accepted, display, blocklist, limits, pipeline, prefilter, wishlist
from . import companies as co
from .state import read_jsonl


@dataclass
class CompanyRow:
    id: str  # normalised name
    name: str
    origins: list[str] = field(default_factory=list)  # "companies.toml", "wishlist", "generated list"
    board: str = ""
    local: bool = False  # fetch_from = "local"
    manual: str | None = None  # why it is checked by hand
    wishlisted: bool = False
    blocked: bool = False
    last_fetch: dict[str, Any] | None = None  # {"at", "where"} of the last fetch without an error
    last_error: str | None = None  # when the latest fetch failed
    failing_on_github: dict[str, Any] | None = None
    open_roles: int = 0
    scored_roles: int = 0
    waiting_roles: int = 0
    applications: int = 0
    latest_stage: str | None = None
    limits: list[dict[str, Any]] = field(default_factory=list)
    generated: dict[str, Any] | None = None  # the generated-list entry (board settings, relevant roles)
    label: str = ""  # the name shown (a URL-only entry is shown by its site name)
    reason: str | None = None  # companies.toml: why it is not covered (see COVERAGE)
    careers_url: str = ""  # its careers page, for checking by hand or signing up for alerts
    system: str = ""  # the hiring system without an adapter
    partial: bool = False
    note: str = ""
    remind: bool = False  # The user asked for a weekly reminder to check it by hand
    stored: dict[str, int] | None = None  # open stored roles, shown or not, by why they are left out

    @property
    def group(self) -> str:
        """Which list it is on, for the Companies filter and search (2026-11-18)."""
        if self.blocked:
            return "blocked"
        if self.wishlisted or "wishlist" in self.origins:
            return "wishlist"
        if {"companies.toml", "daily"} & set(self.origins):
            return "yours"
        if "accepted" in self.origins:
            return "found"
        return "generated"

    @property
    def group_label(self) -> str:
        return GROUPS[self.group]

    @property
    def daily(self) -> bool:
        """Fetched once a day in its slot (the generated list's rules), not every run."""
        return "wishlist" not in self.origins and bool({"daily", "accepted", "generated list"} & set(self.origins))

    @property
    def no_roles(self) -> "RolesStatus | None":
        """Why no role shows, for a covered company with none (2026-10-30)."""
        if self.open_roles or not self.covered:
            return None
        history = {"last_fetch": self.last_fetch, "last_error": self.last_error}
        return roles_status(self.stored, history, self.daily, fetched_unknown=not any(
            o in self.origins for o in ("companies.toml", "daily", "wishlist", "accepted")))

    @property
    def coverage(self) -> "Coverage":
        """Why a company is not fully covered, and what the user can do (2026-10-29)."""
        error = (self.last_error or "").lower()
        reason = self.reason
        if self.covered and not self.partial and not error:
            return Coverage("covered", "Covered", "", "")
        if self.covered and self.partial:
            return Coverage("partial", "Partial", self.note or "The listing shows only some of its jobs.", "")
        if self.covered and error:
            reason = ("robots" if "robots.txt" in error else "blocked" if re.search(r"http (?:401|403|406|429)", error)
                      else "unreachable")
        if not reason:
            note = (self.manual or self.note or "").lower()
            reason = ("robots" if "robots.txt" in note else
                      "blocked" if re.search(r"refuses|access denied|bot protection|http (?:401|403|406)", note) else
                      "javascript" if "javascript" in note or "rendered" in note else
                      "no_adapter" if "no adapter" in note else "no_board")
        text = COVERAGE[reason]
        detail = text[1]
        if reason == "no_adapter" and self.system:
            detail = f"Uses {self.system}, which the tool cannot read yet."
        elif self.covered and error:
            detail = f"Its latest fetch failed: {self.last_error}"
        return Coverage(reason, text[0], detail, text[2])

    @property
    def covered(self) -> bool:
        """Checked by a board adapter (not by hand, not a star only)."""
        return bool(self.board) and not self.manual

    @property
    def board_name(self) -> str:
        """The board as a short badge: "Greenhouse", "Custom"; "Not covered" when nothing checks it."""
        if not self.covered:
            return "Not covered"
        key = self.board.split(" · ")[0].split(" ")[0]
        return BOARD_LABELS.get(key, key.capitalize()) + (" EU" if " EU" in self.board.split(" · ")[0] else "")

    @property
    def board_note(self) -> str:
        return self.board if self.covered else (self.manual or "not checked by the tool")


GROUPS = {"wishlist": "Wishlist", "yours": "Your list", "found": "Found by search", "generated": "Generated list",
          "blocked": "Blocked"}


@dataclass
class Coverage:
    status: str  # covered, partial, no_board, robots, blocked, javascript, unreachable, no_adapter
    label: str
    detail: str
    action: str  # retry, alerts, or "" (nothing to do in the app)


COVERAGE = {
    "no_board": ("No job board found", "The tool found no job board or job links for it.", "retry"),
    "robots": ("robots.txt says no", "Its robots.txt asks automated tools not to read its job pages.", "alerts"),
    "blocked": ("Blocks tools", "Its site refuses automated requests; the tool does not work around that.",
                "alerts"),
    "javascript": ("Needs JavaScript", "Its jobs appear only after JavaScript runs, and no readable source was found.",
                   "retry"),
    "unreachable": ("Unreachable", "Its site did not answer, or timed out.", "retry"),
    "no_adapter": ("No adapter yet", "It uses a hiring system the tool cannot read yet.", ""),
}


BOARD_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday",
                "recruitee": "Recruitee", "custom": "Custom", "jibe": "Jibe", "personio": "Personio",
                "teamtailor": "Teamtailor", "workable": "Workable", "smartrecruiters": "SmartRecruiters", "join": "JOIN",
                "homerun": "Homerun", "amazon": "Amazon", "oracle": "Oracle Recruiting Cloud", "eightfold": "Eightfold",
                "avature": "Avature", "bendingspoons": "Own site", "google": "Own site", "apple": "Own site",
                "email": "Job alert email"}


@dataclass
class RolesStatus:
    label: str  # "Not fetched yet", "No roles in your countries", "Fetch failed: HTTP 403"
    detail: str  # on hover
    tone: str = ""  # "error" for a failed fetch


TITLE_WORDS = "senior, lead or non-engineering titles"


def roles_status(stored: dict[str, int] | None, history: dict[str, Any] | None, daily: bool,
                 fetched_unknown: bool = False) -> RolesStatus:
    """What to say when a company shows no open role: never fetched, its fetch failed, its board
    is empty, or the roles it has are all left out (outside your countries, UK or US only under
    the generated list's rules, titles the filters drop, the title check)."""
    stored, history = stored or {}, history or {}
    total = int(stored.get("stored") or 0)
    error = history.get("last_error")
    if not total:
        if error:
            return RolesStatus(f"Fetch failed: {short_error(error)}", str(error), "error")
        if not history.get("last_fetch"):
            if fetched_unknown:  # a harvested company: its fetches are summarised per board
                return RolesStatus("No open roles stored", "Runs have stored no open role for it.")
            return RolesStatus("Not fetched yet", "Fetched once a day in its slot: its first fetch is within a day."
                               if daily else "Its first fetch comes with the next run (every 2 hours).")
        return RolesStatus("No open roles", "Its last fetch found no open roles on its board.")
    location, parked = int(stored.get("location") or 0), int(stored.get("parked") or 0)
    title, checked, duplicate = (int(stored.get(k) or 0) for k in ("title", "titlecheck", "duplicate"))
    parts = [f"{n:,} {what}" for n, what in (
        (location, "outside your countries"), (parked, "only in the UK or US (not scored with the generated list's rules)"),
        (title, TITLE_WORDS), (checked, "dropped by the title check"), (duplicate, "copies of another role")) if n]
    detail = f"{total:,} open role{'s' if total != 1 else ''} found: " + "; ".join(parts) + "."
    if location == total:
        return RolesStatus("No roles in your countries", detail)
    if location + parked == total:
        return RolesStatus("Only UK or US roles", detail)
    if title + duplicate == total:
        return RolesStatus("No roles pass the title filters", detail)
    return RolesStatus("None passes the filters", detail)


def short_error(error: str) -> str:
    """The kind of a fetch error in a few words."""
    text = str(error)
    match = re.search(r"HTTP \d{3}", text)
    if match:
        return match.group(0)
    if "robots.txt" in text:
        return "robots.txt"
    if re.search(r"timed? ?out", text, re.IGNORECASE):
        return "timed out"
    return text[:40] + ("…" if len(text) > 40 else "")


def board_text(config: dict[str, Any] | None) -> str:
    if not config or not config.get("board"):
        return ""
    board = config["board"] + (" EU" if config.get("region") else "")
    detail = config.get("slug") or config.get("url") or config.get("listing_url") or ""
    return f"{board} · {detail}" if detail else board


fetch_history = pipeline.fetch_history


_GENERATED: dict[Path, tuple[float, list[dict[str, Any]]]] = {}


def generated_entries(state_dir: Path) -> list[dict[str, Any]]:
    """The generated list, read again only when its file changes (the search runs as the user types)."""
    path = state_dir / "companies_generated.jsonl"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return []
    cached = _GENERATED.get(path)
    if cached is None or cached[0] != stamp:
        cached = (stamp, read_jsonl(path))
        _GENERATED[path] = cached
    return cached[1]


def matches(row: "CompanyRow", query: str) -> bool:
    wanted = prefilter.normalise_company(query)
    return bool(wanted) and (wanted in row.id or wanted in prefilter.normalise_company(row.label or row.name))


def entry_name(entry: dict[str, Any]) -> str:
    return entry.get("name") or entry.get("slug") or entry.get("url") or entry["identity"]


def display_name(entry: dict[str, Any]) -> str:
    """Generated entries without a name are known by their URL; show its last path segment."""
    if entry.get("name") or entry.get("slug"):
        return display.company_label(entry_name(entry), from_board=True)
    return display.company_label(str(entry.get("url") or "").rstrip("/"))


def search_generated(entries: list[dict[str, Any]], query: str, limit: int = 50) -> list[dict[str, Any]]:
    wanted = prefilter.normalise_company(query)
    if not wanted:
        return []
    hits = [e for e in entries if wanted in prefilter.normalise_company(entry_name(e))
            or wanted in str(e.get("slug") or "").lower()]
    hits.sort(key=lambda e: (not prefilter.normalise_company(entry_name(e)).startswith(wanted),
                             -(e.get("prefilter_pass") or 0)))
    return hits[:limit]


def your_rows(config: Path, state_dir: Path, wished: dict[str, dict[str, Any]] | None = None,
              blocked: dict[str, dict[str, Any]] | None = None) -> dict[str, CompanyRow]:
    """Your companies (companies.toml, accepted, wishlist, blocked) by normalised name, with what
    covers each. The one source of coverage for Companies, the sidebar and a company's own page
    (2026-11-19): a wishlist entry with no board of its own is covered by its companies.toml entry."""
    wished = {e["id"]: e for e in wishlist.active(state_dir)} if wished is None else wished
    blocked = {e["id"]: e for e in blocklist.active(state_dir)} if blocked is None else blocked
    yours: dict[str, CompanyRow] = {}

    def row_for(name: str) -> CompanyRow:
        norm = prefilter.normalise_company(name)
        if norm not in yours:
            yours[norm] = CompanyRow(id=norm, name=name)
        return yours[norm]

    for company in co.load_companies(config):
        row = row_for(company.name)
        row.origins.append("companies.toml" if company.extra.get("rules") != "generated" else "daily")
        row.board = board_text({"board": company.board, "slug": company.slug, "url": company.url,
                                "region": company.region, "listing_url": company.listing_url})
        row.local = company.fetch_from == "local"
        row.partial, row.note = company.partial, company.note or ""
        row.reason = company.extra.get("reason")
        row.careers_url = str(company.extra.get("careers_url") or "")
        row.system = str(company.extra.get("system") or "")
        if company.manual or not company.board:
            row.manual = company.note or "checked by hand"
    for entry in accepted.active(state_dir):
        row = row_for(entry["name"])
        row.origins.append("accepted")
        row.board = row.board or board_text(entry.get("config") or {})
        row.careers_url = row.careers_url or str((entry.get("found") or {}).get("careers_url") or "")
    for norm, entry in wished.items():
        row = row_for(entry["name"])
        row.origins.append("wishlist")
        row.wishlisted = True
        row.careers_url = row.careers_url or str(entry.get("careers_url") or "")
        if entry.get("config"):
            row.board = row.board or board_text(entry["config"])
            row.local = row.local or entry["config"].get("fetch_from") == "local"
            row.manual = None if entry.get("identity") else row.manual
        elif not row.board:
            row.manual = row.manual or "no board found: star only"
    for norm, entry in blocked.items():
        row_for(entry["name"]).blocked = True
    return yours


def rows(config: Path, state_dir: Path, runs: list[dict[str, Any]], apps: dict[str, list[limits.Application]],
         counts: dict[str, tuple[int, ...]], today: date, query: str = "",
         generated: list[dict[str, Any]] | None = None,
         stored: dict[str, dict[str, int]] | None = None) -> tuple[list[CompanyRow], list[CompanyRow]]:
    """(yours: companies.toml, wishlist and blocked companies; found: generated-list companies
    matching `query`)."""
    wished = {e["id"]: e for e in wishlist.active(state_dir)}
    blocked = {e["id"]: e for e in blocklist.active(state_dir)}
    registry = limits.by_company(limits.load(config))
    history = fetch_history(runs)
    failing = {prefilter.normalise_company(n): f for n, f in pipeline.failing_on_github(runs).items()}
    yours = your_rows(config, state_dir, wished, blocked)

    found: list[CompanyRow] = []
    if query:
        for entry in search_generated(generated if generated is not None else generated_entries(state_dir), query):
            norm = prefilter.normalise_company(entry_name(entry))
            if norm in yours:  # already in your lists: shown once, from there
                continue
            row = CompanyRow(id=norm, name=entry_name(entry), label=display_name(entry), origins=["generated list"],
                             board=board_text(entry), wishlisted=norm in wished, blocked=norm in blocked,
                             generated=entry)
            found.append(row)

    for row in [*yours.values(), *found]:
        item = history.get(row.id, {})
        row.last_fetch, row.last_error = item.get("last_fetch"), item.get("last_error")
        row.failing_on_github = failing.get(row.id)
        found_counts = tuple(counts.get(row.id, ())) + (0, 0, 0)
        row.open_roles, row.scored_roles, row.waiting_roles = found_counts[:3]
        row.stored = (stored or {}).get(row.id)
        company_apps = apps.get(row.id, [])
        row.applications = len(company_apps)
        if company_apps:
            latest = max(company_apps, key=lambda a: (a.history[-1][1] if a.history else a.applied_on or date.min))
            row.latest_stage = latest.stage
        if row.id in registry:
            row.limits = limits.as_dicts(limits.standings(registry[row.id], company_apps, today))
    # By name, as in the sidebar (2026-11-19: sorting covered companies first put one checked by hand
    # at the very end, where it looked missing); blocked last. The Coverage column says what checks each.
    ordered = sorted(yours.values(), key=lambda r: (r.blocked, r.name.lower()))
    return ordered, found


def wishlist_coverage(config: Path, state_dir: Path) -> dict[str, CompanyRow]:
    """Each wishlist entry's company row (by the entry's id), for the sidebar and a company's page."""
    entries = wishlist.active(state_dir)
    yours = your_rows(config, state_dir, wished={e["id"]: e for e in entries})
    return {e["id"]: yours[prefilter.normalise_company(e["name"])] for e in entries}


_names_stamp: tuple = ()


def register_names(config: Path, state_dir: Path) -> None:
    """Tell `display` which names you wrote yourself (companies.toml, accepted companies and the
    wishlist), so every page shows them as written; reread only when those files change."""
    global _names_stamp
    files = [config / co.FILE, state_dir / wishlist.FILE, state_dir / accepted.FILE]
    stamp = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
    if stamp == _names_stamp:
        return
    names = [c.name for c in co.load_companies(config)] if files[0].exists() else []
    names += [e["name"] for e in wishlist.load(state_dir).values() if e.get("name")]
    names += [e["name"] for e in accepted.active(state_dir) if e.get("name")]
    display.use_names(names)
    _names_stamp = stamp
