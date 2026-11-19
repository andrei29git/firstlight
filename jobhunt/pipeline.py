"""One run: fetch every board, pre-filter, merge duplicates, close vanished jobs, score new ones.

Fetching runs one worker per host, so each host keeps its polite delay while different hosts
are fetched at the same time. The run summary records the numbers later phases are sized on.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import accounts, blocklist, display, geo, hostwaits, interrupt, prefilter, scoring, titlecheck
from .progress import Progress, Silent
from .companies import Company
from .sources import Http, Job, SourceError, fetch
from .sources import custom as custom_source
from .sources.http import WAITS, rate_key
from .sources.lever import HOSTS as LEVER_HOSTS
from .state import State, read_jsonl

MAX_SCORE_ATTEMPTS = 3  # a posting that keeps failing to score is left alone after this


@dataclass
class Options:
    score: bool = True
    backlog: bool = False  # cheap model first, then the scheduled model for promising roles
    budget: float | None = None  # share of a 5-hour window this run may use; None = from settings
    alerts: bool = True  # send phone alerts for strong roles scored in this run (needs NTFY_TOPIC)
    cap: int | None = None  # scoring calls; None = max_calls_per_run from settings
    parallel: int | None = None  # scoring calls at once; None = from settings
    sonnet_only: bool = False  # only rescore roles the backlog model rated 6+ with the scheduled model
    ignore_weekly_warning: bool = False  # a seven-day warning does not stop this run (see UsageGuard)
    kind: str = ""  # what started the run, for the Runs page: "scheduled", "backfill", "named: ...", ...
    # Scheduled runs (GitHub Actions, 20-minute limit): stop starting company fetches after this many
    # seconds, and fetch at most this many posting texts; what is left waits for a later fetch.
    fetch_seconds: float | None = None
    max_details: int | None = None
    only: frozenset[str] = frozenset()  # normalised names: a run naming companies scores only theirs


@dataclass
class Fetched:
    company: Company
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0


BOARD_HOSTS = {"personio": "jobs.personio.de", "teamtailor": "teamtailor.com", "join": "join.com",
               "workable": "apply.workable.com", "smartrecruiters": "api.smartrecruiters.com"}


def host_of(company: Company) -> str:
    """The host a company's board is fetched from: one worker per host keeps the delay honest."""
    if company.board == "greenhouse":
        return "boards-api.greenhouse.io"
    if company.board == "lever":
        return LEVER_HOSTS[company.region]
    if company.board == "ashby":
        return "api.ashbyhq.com"
    if company.board == "recruitee":
        return "recruitee.com"  # rate-limited across all company subdomains: one worker
    if company.board in BOARD_HOSTS:
        return BOARD_HOSTS[company.board]
    return urlsplit(company.url or company.listing_url or "").netloc or company.name


def fetch_all(companies: list[Company], make_http: Callable[[], Http], want_details: Callable[[Job], bool],
              custom_max_links: int, max_hosts: int, progress: Progress | None = None,
              deadline: float | None = None, on_fetched: Callable[[Fetched], None] | None = None) -> list[Fetched]:
    """Fetch every company, one worker per host. `on_fetched` sees each company as soon as it is done
    (from the worker thread). My companies come first on each host, so their roles are ready early."""
    progress = progress or Silent()
    progress.phase("fetching", total=len(companies))
    groups: dict[str, list[Company]] = defaultdict(list)
    for company in sorted(companies, key=lambda c: c.origin == "generated"):
        groups[host_of(company)].append(company)

    def fetch_group(group: list[Company]) -> list[Fetched]:
        http = make_http()
        out = []
        for company in group:
            if interrupt.stopping():  # Ctrl+C: no new fetches; companies not fetched close nothing
                break
            if deadline is not None and time.monotonic() > deadline:
                break  # a scheduled run's fetch time is used: the rest are fetched at a later run
            started = time.monotonic()
            try:
                jobs = fetch(company, http, details=want_details, custom_max_links=custom_max_links,
                             light=company.origin in ("generated", "wishlist"))
                out.append(Fetched(company, jobs, seconds=time.monotonic() - started))
            except SourceError as e:
                out.append(Fetched(company, error=str(e), seconds=time.monotonic() - started))
            if on_fetched:
                on_fetched(out[-1])
            progress.advance()
        return out

    with ThreadPoolExecutor(max_workers=max(1, max_hosts)) as pool:
        return [f for batch in pool.map(fetch_group, groups.values()) for f in batch]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class GeneratedRules:
    """How generated-list roles are treated (`[generated]` in settings.toml)."""
    park: frozenset[str] = frozenset()
    score_parked: bool = False
    backfill_days_entry_level: int = 20  # backlog roles with entry-level wording (or no posting date)
    backfill_days_other: int = 14  # backlog roles without entry-level wording
    entry_level_only: bool = False  # the setting: every generated role needs entry-level wording
    guard_filter: bool = False  # switched on by the weekly guard: daily new roles only, never backlog roles
    blocked: frozenset[str] = frozenset()  # normalised names of blocked companies: never scored (any part)
    only: frozenset[str] = frozenset()  # a run naming companies scores only theirs (normalised names)
    paused: bool = False  # the week is nearly full: no generated-list role is scored this run
    # The weekly guard's second step (2026-10-27): boards whose generated-list companies are not
    # fetched and whose generated-list roles are not scored while the guard is on.
    paused_boards: frozenset[str] = frozenset()
    # My companies (normalised names) with many open roles, scored by the cascade in every run:
    # title check, Haiku, then Sonnet for Haiku 6+ (2026-10-28; see cascade_companies).
    cascade_mine: frozenset[str] = frozenset()
    # Of those, the ones whose Haiku 5+ (not 6+) goes on to Sonnet: the wishlist and the core
    # targets (`[scoring] cascade_core`; decided 2026-10-29).
    cascade_low_bar: frozenset[str] = frozenset()
    # Roles parked only because of these countries are scored after all when their title has
    # entry-level wording (decided 2026-10-27: ["GB"]; US stays parked).
    unpark_entry_level: frozenset[str] = frozenset()
    # The main countries (2026-10-29: NL, RO, IT, FR, DE): a graduate-worded role with a location
    # there is backlog-eligible when posted within `backfill_days_main` days, and also when Workday
    # says only "Posted 30+ Days Ago" (graduate programmes stay open for months).
    main_countries: frozenset[str] = frozenset()
    backfill_days_main: int = 60
    # Every role in the main countries is scored, graduate wording or not (2026-10-30,
    # `all_roles_in_main`): the graduate-wording filter keeps applying to the other countries.
    all_in_main: bool = False

    def needs_entry_terms(self, locations: Iterable[str]) -> bool:
        """Whether the setting's graduate-wording filter applies to a role in these places."""
        return self.entry_level_only and not (self.all_in_main and self.in_main(locations))

    def in_main(self, locations: Iterable[str]) -> bool:
        return bool(self.main_countries) and any(geo.parse(x).codes & self.main_countries for x in locations or [])

    def parks(self, title: str, locations: list[str]) -> bool:
        """Whether a generated-list role is parked: every location in a parked country, unless it
        has entry-level wording and every location is in `unpark_entry_level`."""
        if not prefilter.parked(locations, set(self.park)):
            return False
        if self.unpark_entry_level and prefilter.entry_terms(title) and \
                prefilter.parked(locations, set(self.unpark_entry_level)):
            return False
        return True

    @classmethod
    def from_settings(cls, settings: dict[str, Any], guard_filter: bool = False,
                      blocked: frozenset[str] = frozenset(), only: frozenset[str] = frozenset(),
                      paused_boards: frozenset[str] = frozenset()) -> "GeneratedRules":
        g = settings.get("generated", {})
        return cls(park=frozenset(g.get("park_countries", [])), score_parked=bool(g.get("score_parked", False)),
                   backfill_days_entry_level=int(g.get("backfill_days_entry_level", 20)),
                   backfill_days_other=int(g.get("backfill_days_other", 14)),
                   entry_level_only=bool(g.get("entry_level_only", False)), guard_filter=guard_filter,
                   blocked=blocked, only=only, paused_boards=paused_boards,
                   unpark_entry_level=frozenset(g.get("unpark_entry_level", [])),
                   main_countries=frozenset(g.get("main_countries", [])),
                   backfill_days_main=int(g.get("backfill_days_main", 60)),
                   all_in_main=bool(g.get("all_roles_in_main", False)))

    def in_cascade(self, record: dict[str, Any]) -> bool:
        """A role at one of my large companies: scored by the cascade, unless marked for Sonnet."""
        return (bool(self.cascade_mine) and not is_generated(record) and record.get("score_with") != "sonnet"
                and prefilter.normalise_company(record["company"]) in self.cascade_mine)

    def is_blocked(self, record: dict[str, Any]) -> bool:
        return bool(self.blocked) and prefilter.normalise_company(record["company"]) in self.blocked

    def left_out(self, record: dict[str, Any]) -> bool:
        """Not scored in this run: a blocked company, or not one of the companies the run names."""
        if self.is_blocked(record):
            return True
        return bool(self.only) and prefilter.normalise_company(record["company"]) not in self.only


def company_index(state: State) -> dict[tuple[str, str], set[str]]:
    """Job keys by (company, source), built once per run: scanning every job for each of
    thousands of companies would take minutes."""
    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key, record in state.jobs.items():
        index[(record["company"], record["source"])].add(key)
    return index


def backlog_eligible(title: str, posted: str | None, today: date, rules: GeneratedRules,
                     locations: Iterable[str] = (), posted_before: bool = False) -> bool:
    """The backlog rule (decided 2026-10-25): a role from a company's first fetch is scored if it
    has entry-level wording and was posted in the last `backfill_days_entry_level` days (or has no
    posting date), or has none and was posted in the last `backfill_days_other` days. In the main
    countries (2026-10-29) entry-level roles get `backfill_days_main` days, and Workday's "Posted
    30+ Days Ago" (`posted_before`: `posted` is then only a bound) counts too. (Open and not parked
    are checked elsewhere.)"""
    if prefilter.entry_terms(title):
        if rules.in_main(locations):
            return posted is None or posted_before or \
                posted[:10] >= (today - timedelta(days=rules.backfill_days_main)).isoformat()
        if posted_before and posted is None:
            return False  # "Posted 30+ Days Ago" with no bound kept (a stored Workday role): old
        return posted is None or posted[:10] >= (today - timedelta(days=rules.backfill_days_entry_level)).isoformat()
    return posted is not None and posted[:10] >= (today - timedelta(days=rules.backfill_days_other)).isoformat()


def apply_rule_changes(state: State, settings: dict[str, Any], today: date, now: str,
                       new_terms: Iterable[str] = ()) -> Counter:
    """The backlog rule as it is now, for generated-list roles already stored and never scored
    (2026-10-29). Roles skipped at their company's first fetch that the rule now covers (the main
    countries' 60 days and Workday's "Posted 30+ Days Ago") come back; roles caught only by
    `new_terms` (newly added entry-level words) are scored only when the rule covers them, whenever
    they were found. Both are marked `backlog`, so scheduled runs score them over several runs."""
    rules = GeneratedRules.from_settings(settings)
    scheduled, backlog = models(settings)
    new = set(new_terms)
    counts: Counter = Counter()
    for record in state.jobs.values():
        if (not is_generated(record) or record["closed"] or record["prefilter"] != "pass"
                or record.get("duplicate_of") or record.get("parked")):
            continue
        if any(current_score(state, record, m) for m in (scheduled, backlog)):
            continue
        terms = prefilter.entry_terms(record["title"])
        if not terms:
            if rules.all_in_main and rules.in_main(record.get("locations") or []) and not record.get("backfill_skip"):
                # Every role in the main countries (2026-10-30): the backlog takes those posted in
                # the last `backfill_days_other` days (undated ones found in that time: they came
                # as daily new roles); older ones are left out.
                days = rules.backfill_days_other
                recent = (record["posted"][:10] if record.get("posted") else record["first_seen"][:10]) \
                    >= (today - timedelta(days=days)).isoformat()
                if recent and record.get("backlog"):
                    counts["main countries, no graduate wording, recent: already in the backlog"] += 1
                    continue
                if recent:
                    record["backlog"] = True
                    counts["main countries, no graduate wording, recent: scored"] += 1
                else:
                    record["backfill_skip"] = True
                    counts["main countries, no graduate wording, older: left out"] += 1
                record["updated_at"] = now
            continue
        eligible = backlog_eligible(record["title"], record.get("posted"), today, rules, record.get("locations") or [],
                                    posted_before=record["source"] == "workday" and not record.get("posted"))
        only_new = bool(new) and set(terms) <= new
        if record.get("backfill_skip") and eligible:
            record["backfill_skip"], record["backlog"] = False, True
            counts["skipped before, scored now"] += 1
        elif only_new and not record.get("backfill_skip"):
            if eligible:
                record["backlog"] = True
                counts["new word, recent: scored"] += 1
            else:
                record["backfill_skip"] = True
                counts["new word, older: left out"] += 1
            for term in terms:
                counts[f"word {term}"] += 1
        else:
            continue
        record["updated_at"] = now
    return counts


# Measured rates for estimating a backlog (docs/measurements.md): the title check keeps 81% of
# entry-worded roles and 65% of the rest; Haiku sends 20.5% of entry-worded generated roles
# (night 2, 2026-10-25: 25 of 122) and 9% of other roles to Sonnet.
TITLECHECK_KEEP = {True: 0.81, False: 0.65}
TO_SONNET = {True: 0.205, False: 0.09}


def backlog_plan(state: State, settings: dict[str, Any], today: date, other_days: int | None = None,
                 entry_days: int | None = None) -> dict[str, Any]:
    """What the backlog rule would add among generated roles skipped at their company's first
    fetch: still open, not parked, never scored. `other_days` and `entry_days` narrow the rule's
    windows for this backlog only. Nothing is changed."""
    rules = GeneratedRules.from_settings(settings)
    if other_days is not None:
        rules = replace(rules, backfill_days_other=other_days)
    if entry_days is not None:
        rules = replace(rules, backfill_days_entry_level=entry_days)
    scheduled, backlog = models(settings)
    share = {k: float(v) for k, v in settings.get("usage", {}).get("share_per_call", {}).items()}
    parts: dict[str, list[dict[str, Any]]] = {"entry-level": [], "other": []}
    for record in state.jobs.values():
        if (not is_generated(record) or not record.get("backfill_skip") or record["closed"]
                or record["prefilter"] != "pass" or record.get("duplicate_of") or record.get("parked")):
            continue
        if any(has_result(state, record, m) for m in (scheduled, backlog)):
            continue
        if backlog_eligible(record["title"], record.get("posted"), today, rules, record.get("locations") or [],
                            posted_before=record["source"] == "workday" and not record.get("posted")):
            parts["entry-level" if prefilter.entry_terms(record["title"]) else "other"].append(record)
    report: dict[str, Any] = {"parts": {}, "records": parts}
    for name, records in parts.items():
        entry = name == "entry-level"
        kept = round(len(records) * TITLECHECK_KEEP[entry])
        to_sonnet = round(kept * TO_SONNET[entry])
        windows = (-(-len(records) // 100) * share.get("titlecheck", 0.0025) + kept * share.get(backlog, 0.0012)
                   + to_sonnet * share.get(scheduled, 0.004))
        report["parts"][name] = {"roles": len(records), "with_text": sum(bool(r.get("description_hash")) for r in records),
                                 "title_check_keeps": kept, "haiku_calls": kept, "sonnet_calls": to_sonnet,
                                 "windows": round(windows, 3)}
    return report


def mark_company(state: State, company: str, score_with: str, now: str, model_names: list[str]) -> dict[str, int]:
    """Make a company's skipped roles (left out by the backlog rule at its first fetch) eligible,
    and mark how every role of it not scored yet is scored: score_with "sonnet" (Sonnet directly)
    or "cascade" (Haiku first, Sonnet for 6+; only backlog runs score it). Never scores itself."""
    wanted = prefilter.normalise_company(company)
    counts = Counter()
    for record in state.jobs.values():
        if prefilter.normalise_company(record["company"]) != wanted:
            continue
        if record["closed"] or record["prefilter"] != "pass" or record.get("duplicate_of"):
            continue
        if any((state.scores.get(scoring.score_id(record["key"], m)) or {}).get("result") for m in model_names):
            counts["already scored"] += 1
            continue
        if record.get("backfill_skip"):
            counts["skipped, now eligible"] += 1
        else:
            counts["eligible already"] += 1
        record.update(backfill_skip=False, backlog=True, score_with=score_with, updated_at=now)
        counts["without text yet"] += not record.get("description_hash")
        counts["graduate wording"] += bool(prefilter.entry_terms(record["title"]))
    return dict(counts)


def apply_backlog(state: State, records: list[dict[str, Any]], now: str) -> int:
    """Make these backlog roles eligible for scoring; the next runs score them."""
    for record in records:
        record["backfill_skip"] = False
        record["backlog"] = True
        record["updated_at"] = now
    return len(records)


def update_jobs(state: State, fetched: Fetched, allowed: set[str], now: str,
                rules: GeneratedRules = GeneratedRules(),
                index: dict[tuple[str, str], set[str]] | None = None) -> dict[str, int]:
    """Upsert the fetched jobs and close the company's jobs that vanished. Returns counts.
    For a generated company only roles passing the pre-filter are stored (the rest are counted,
    and removed if stored earlier), so the state stays a manageable size."""
    index = index if index is not None else company_index(state)
    counts: Counter[str] = Counter()
    today = now[:10]
    seen = set()
    last_seen_floor = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    company = fetched.company
    generated = company.origin == "generated"
    # A generated (or newly wishlisted) company's first fetch is its backlog: only roles the
    # backlog rule allows are scored (backlog_eligible); the rest are stored but skipped.
    own = index[(company.name, company.board)]
    first_fetch = company.origin in ("generated", "wishlist") and not own
    for job in fetched.jobs:
        seen.add(job.key)
        old = state.jobs.get(job.key, {})
        digest = scoring.description_hash(job.description) if job.description else old.get("description_hash")
        locations = job.locations or old.get("locations", [])
        if job.source == "custom" and old and not job.description:
            # A custom site's listing knows a role only by its link: the title and place made from
            # the URL never replace those read from the role's page at an earlier fetch.
            job = replace(job, title=old["title"])
            locations = old.get("locations") or locations
        tidy = prefilter.tidy_title(job.title, job.company, locations)  # "… | SEB, Vilnius" (2026-11-19)
        if tidy != job.title:
            job = replace(job, title=tidy)
        verdict = prefilter.check(job.title, locations, allowed)
        if generated and not verdict.passed:
            counts["fetched"] += 1
            counts[f"dropped {verdict.category}"] += 1
            if old:
                del state.jobs[job.key]
                own.discard(job.key)
            continue
        record = {
            "key": job.key, "company": job.company, "title": job.title, "url": job.url, "source": job.source,
            "source_id": job.source_id, "locations": locations,
            "posted": job.posted or old.get("posted"), "deadline": job.deadline or old.get("deadline"),
            "department": job.department or old.get("department"),
            "employment_type": job.employment_type or old.get("employment_type"),
            "description_hash": digest, "prefilter": verdict.reason or "pass",
            "first_seen": old.get("first_seen", now), "closed": False, "closed_at": None,
            "duplicate_of": old.get("duplicate_of"), "merged_locations": old.get("merged_locations"),
            # Refreshed at most weekly, so an unchanged job's line (and the data branch) stays the
            # same from run to run; closing does not depend on it.
            "last_seen": old["last_seen"] if old.get("last_seen", "") > last_seen_floor else today,
            "origin": company.origin,
            "account": company.account,
            "parked": (generated and rules.parks(job.title, locations))
                      or prefilter.parked(locations, set(company.park_countries)),
            # Decided once, at the company's first fetch, so it is never scored later by surprise.
            "backfill_skip": old.get("backfill_skip", first_fetch and not backlog_eligible(
                job.title, job.posted or job.posted_before, date.fromisoformat(today), rules, locations,
                posted_before=not job.posted and bool(job.posted_before))),
            # A role from a company's first fetch: the weekly guard's filter leaves it alone.
            "backlog": old.get("backlog", first_fetch),
        }
        # Decisions made about the role, kept across fetches (a queued rescore too: until 2026-10-31
        # every fetch of its company dropped the mark).
        for kept in ("titlecheck", "score_with", RESCORE_FIELD):
            if kept in old:
                record[kept] = old[kept]
        if verdict.passed and job.description:
            state.write_posting(digest, job.description)
        changed = {k: v for k, v in record.items() if old.get(k) != v}
        record["updated_at"] = now if changed or not old else old.get("updated_at", now)
        state.jobs[job.key] = record
        own.add(job.key)
        counts["fetched"] += 1
        counts["passed" if verdict.passed else f"dropped {verdict.category}"] += 1
        if verdict.passed and record["parked"]:
            counts["parked"] += 1
        if not old:
            counts["new"] += 1
            counts["new passed"] += verdict.passed
    partial = fetched.company.partial or bool(fetched.company.search)
    # A partial listing (first page only, or a search) cannot prove a job has gone at once: a role
    # it has not shown for 21 days counts as gone (last_seen is refreshed weekly while it shows).
    stale_before = (date.fromisoformat(today) - timedelta(days=21)).isoformat()
    for key in own - seen:
        record = state.jobs[key]
        if record.get("account", company.account) != company.account:
            continue  # the same company's other board of this type: not this fetch's to close
        if partial and record.get("last_seen", today) >= stale_before:
            continue
        if not record["closed"]:
            record.update(closed=True, closed_at=now, updated_at=now)
            counts["closed"] += 1
    return dict(counts)


def mark_duplicates(state: State, records: Iterable[dict[str, Any]] | None = None) -> int:
    """The same role posted once per city (or on two boards) becomes one job with all locations.
    The earliest seen copy is kept; the rest point to it and are never scored. `records` limits
    this to some companies (every record of each, so the result is the same as for the whole state)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in (state.jobs.values() if records is None else records):
        record["duplicate_of"] = None
        record["merged_locations"] = None
        if not record["closed"] and record["prefilter"] == "pass" and record.get("description_hash"):
            groups[(prefilter.normalise_company(record["company"]),
                    prefilter.normalise_title(prefilter.base_title(record["title"])))].append(record)
    duplicates = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (r["first_seen"], r["key"]))
        heads: list[tuple[dict[str, Any], str]] = []
        for record in members:
            text = state.read_posting(record["description_hash"])
            head = next((h for h, h_text in heads if record["description_hash"] == h["description_hash"]
                         or prefilter.similar_descriptions(text, h_text)), None)
            if head is None:
                heads.append((record, text))
                continue
            record["duplicate_of"] = head["key"]
            head["merged_locations"] = list(dict.fromkeys((head["merged_locations"] or head["locations"])
                                                          + record["locations"]))
            duplicates += 1
    return duplicates


def job_from_record(record: dict[str, Any], description: str) -> Job:
    return Job(company=record["company"], title=record["title"], url=record["url"], source=record["source"],
               source_id=record["source_id"], locations=record.get("merged_locations") or record["locations"],
               description=description, posted=record.get("posted"), deadline=record.get("deadline"),
               department=record.get("department"), employment_type=record.get("employment_type"))


def scorable(record: dict[str, Any]) -> bool:
    """Open, passed the pre-filter, not a duplicate, and has posting text (a job without text yet
    is filled in on a later run)."""
    return (not record["closed"] and record["prefilter"] == "pass" and not record.get("duplicate_of")
            and bool(record.get("description_hash")))


def is_generated(record: dict[str, Any]) -> bool:
    return record.get("origin") == "generated"


def generated_ok(record: dict[str, Any], rules: GeneratedRules) -> bool:
    """A generated-list role that may be scored: not parked (UK/US only) unless parked roles are
    switched on, not skipped as backlog, and with an entry-level term if a filter asks for one.
    The weekly guard's filter does not apply to backlog roles (decided 2026-10-25): they are
    scored at the backlog's own pace, and their usage still counts towards the guard."""
    needs_terms = (rules.needs_entry_terms(record.get("locations") or [])
                   or (rules.guard_filter and not record.get("backlog")))
    return (not rules.paused and record.get("source") not in rules.paused_boards
            and scorable(record) and is_generated(record) and not rules.left_out(record)
            and (rules.score_parked or not record.get("parked"))
            and not record.get("backfill_skip")
            and (not needs_terms or bool(prefilter.entry_terms(record["title"]))))


def current_score(state: State, record: dict[str, Any], model: str) -> dict[str, Any] | None:
    """The model's score for the job's present text, if any (successful or not)."""
    score = state.scores.get(scoring.score_id(record["key"], model))
    return score if score and score.get("description_hash") == record["description_hash"] else None


def has_result(state: State, record: dict[str, Any], model: str) -> bool:
    score = current_score(state, record, model)
    return bool(score and score.get("result"))


def given_up(state: State, record: dict[str, Any], model: str) -> bool:
    score = current_score(state, record, model)
    return bool(score and not score.get("result") and score.get("attempts", 1) >= MAX_SCORE_ATTEMPTS)


Select = Callable[[dict[str, Any]], bool]


def unscored(state: State, models: list[str], model: str, eligible: Select = scorable) -> Select:
    """Score each role once: no model has scored its present text yet. New text re-scores; a new
    prompt version only makes the score stale (see `stale`). `eligible` narrows which roles count."""
    return lambda r: (eligible(r) and not any(has_result(state, r, m) for m in models)
                      and not given_up(state, r, model))


def configured(record: dict[str, Any]) -> bool:
    """A companies.toml or wishlist role (scored by Sonnet directly). Roles a backlog rule left
    unscored stay so, also when a generated company is wishlisted."""
    return (scorable(record) and not is_generated(record) and not record.get("backfill_skip")
            and not record.get("parked"))  # a company's `park_countries` (Apple's US roles)


def mine(record: dict[str, Any], rules: GeneratedRules) -> bool:
    """A role at my companies that may be scored (not at a blocked company)."""
    return configured(record) and not rules.left_out(record)


def my_first_model(record: dict[str, Any], backlog_run: bool, scheduled: str, backlog: str) -> str | None:
    """The model that scores a role at my companies first. A backlog run starts with the backlog
    model (Haiku, then Sonnet for 6+), unless the role is marked score_with = "sonnet"; a scheduled
    run uses Sonnet, but leaves roles marked score_with = "cascade" to backlog runs."""
    wanted = record.get("score_with")
    if backlog_run:
        return scheduled if wanted == "sonnet" else backlog
    return None if wanted == "cascade" else scheduled


def worth_rescoring(state: State, first: str, min_fit: int, model: str, eligible: Select = scorable) -> Select:
    """Roles the cheap model rated at least `min_fit`, not yet scored by `model`."""
    def select(r: dict[str, Any]) -> bool:
        if not eligible(r) or has_result(state, r, model) or given_up(state, r, model):
            return False
        score = current_score(state, r, first)
        return bool(score and score.get("result") and score["result"]["fit"] >= min_fit)
    return select


def stale(state: State, prompt: scoring.Prompt) -> Select:
    """Roles this model scored under an older prompt version: picked up only by `rescore`."""
    def select(r: dict[str, Any]) -> bool:
        score = current_score(state, r, prompt.model) if scorable(r) else None
        return bool(score and score.get("result") and score.get("prompt_version") != prompt.version)
    return select


RESCORE_FIELD = "rescore_requested"


def score_text(result: dict[str, Any]) -> str:
    """What a score says in words: its explanation, blockers and gaps."""
    return " ".join([result.get("explanation") or ""] + [b.get("detail") or "" for b in result.get("blockers") or []]
                    + [str(g) for g in result.get("gaps") or []])


def queue_rescores(state: State, settings: dict[str, Any], now: str, matching: "re.Pattern[str] | None" = None,
                   matching_min_fit: int = 0, min_fit: int | None = None) -> Counter:
    """Mark open roles for a rescore spread over the scheduled runs (2026-10-29): those whose shown
    score's text matches `matching` at fit `matching_min_fit` or more, and those at fit `min_fit` or
    more. The shown score's model rescores it; the mark goes once the score is current."""
    counts: Counter = Counter()
    for record in state.jobs.values():
        if not scorable(record) or record.get(RESCORE_FIELD):
            continue
        score = preferred_score(state, record, settings)
        if not score or not score.get("result"):
            continue
        fit = score["result"]["fit"]
        hit = bool(matching and matching.search(score_text(score["result"])) and fit >= matching_min_fit)
        if hit or (min_fit is not None and fit >= min_fit):
            record[RESCORE_FIELD] = {"model": score["model"], "at": now}
            record["updated_at"] = now
            counts[score["model"]] += 1
    return counts


def requested_rescore(state: State, prompt: scoring.Prompt) -> Select:
    """Roles marked by `queue_rescores` for this model whose score is still from an older prompt."""
    def select(r: dict[str, Any]) -> bool:
        wanted = r.get(RESCORE_FIELD)
        if not wanted or wanted.get("model") != prompt.model or not scorable(r):
            return False
        score = current_score(state, r, prompt.model)
        return bool(score and score.get("result") and score.get("prompt_version") != prompt.version)
    return select


def clear_done_rescores(state: State, prompts: list[scoring.Prompt], now: str) -> int:
    """Drop the mark from roles rescored (or closed) since: the queue empties itself."""
    by_model = {p.model: p for p in prompts}
    done = 0
    for r in state.jobs.values():
        wanted = r.get(RESCORE_FIELD)
        if not wanted:
            continue
        prompt = by_model.get(wanted.get("model"))
        if prompt is None:
            continue
        score = current_score(state, r, prompt.model) if scorable(r) else None
        if not scorable(r) or not score or score.get("prompt_version") == prompt.version:
            del r[RESCORE_FIELD]
            r["updated_at"] = now
            done += 1
    return done


def wishlist_names(root: Path) -> frozenset[str]:
    """The wishlist's companies (normalised names); none for a state folder without one."""
    from . import wishlist
    try:
        return wishlist.names(root)
    except (OSError, ValueError):
        return frozenset()


def models(settings: dict[str, Any]) -> tuple[str, str]:
    """(scheduled model, backlog model)."""
    scoring_settings = settings.get("scoring", {})
    scheduled = str(scoring_settings.get("model", "haiku"))
    return scheduled, str(scoring_settings.get("backlog_model", scheduled))


def preferred_score(state: State, record: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any] | None:
    """The score to show: the scheduled model's (Sonnet) where it exists, else the backlog model's."""
    for model in dict.fromkeys(models(settings)):
        if has_result(state, record, model):
            return current_score(state, record, model)
    return None


def priority(record: dict[str, Any]) -> tuple:
    """Graduate or entry-level wording first (the backlog's entry-level part), then the newest."""
    title = record["title"]
    return (not (prefilter.has_graduate_wording(title) or prefilter.entry_terms(title)),
            -_day_number(record.get("posted") or record["first_seen"][:10]))


def _day_number(day: str) -> int:
    try:
        return datetime.fromisoformat(day[:10]).toordinal()
    except ValueError:
        return 0


def score_pending(state: State, prompt: scoring.Prompt, select: Select, cap: int, parallel: int,
                  runner: scoring.Runner, timeout: float, guard: scoring.UsageGuard | None = None,
                  progress: Progress | None = None, label: str = "scoring",
                  order: Callable[[dict[str, Any]], Any] = priority) -> dict[str, Any]:
    pending = [r for r in state.jobs.values() if select(r)]
    pending.sort(key=order)
    chosen = pending[:cap]
    jobs = [job_from_record(r, state.read_posting(r["description_hash"])) for r in chosen]
    started = time.monotonic()
    guard = guard or scoring.UsageGuard()
    spent_before = guard.spent
    progress = progress or Silent()
    if jobs:
        progress.phase(f"{label}: {prompt.model}", total=len(jobs), budget=lambda: (guard.spent, guard.max_share))
    results = scoring.score_many(jobs, prompt, parallel=parallel, runner=runner, timeout=timeout, guard=guard,
                                 on_score=lambda _: progress.advance())
    for score in results:
        store_score(state, score)
    return score_stats(prompt, results, len(pending), guard, guard.spent - spent_before, parallel,
                       time.monotonic() - started)


ACCOUNT: dict[str, Any] | None = None  # this process's Claude account, recorded on each score (2026-11-18)


def use_account(settings: dict[str, Any]) -> dict[str, Any]:
    """Note which Claude account this process scores with (`accounts.current`)."""
    global ACCOUNT
    ACCOUNT = accounts.current(settings)
    return ACCOUNT


def store_score(state: State, score: scoring.Score) -> None:
    if score.infrastructure:
        return  # Claude's failure, not the role's: nothing recorded, tried again next run
    record = score.to_record()
    if ACCOUNT:
        record["account"] = ACCOUNT["key"]
    old = state.scores.get(score.id)
    if score.result is None:
        if old and old.get("result") and old.get("description_hash") == score.description_hash:
            return  # a failed rescore never replaces a good score
        same_text = old and old.get("description_hash") == score.description_hash
        record["attempts"] = (old.get("attempts", 1) + 1) if same_text else 1
    state.scores[score.id] = record


def score_stats(prompt: scoring.Prompt, results: list[scoring.Score], pending: int, guard: scoring.UsageGuard,
                spent: float, parallel: int, wall: float) -> dict[str, Any]:
    ok = [s for s in results if s.result is not None]
    return {
        "model": prompt.model, "prompt_version": prompt.version, "stopped": guard.stopped,
        "window_share_spent": round(spent, 4),
        "pending": pending, "calls": len(results), "ok": len(ok), "failed": len(results) - len(ok),
        "deferred": pending - len(results), "parallel": parallel, "wall_seconds": round(wall, 1),
        "seconds_per_call": round(sum(s.seconds for s in results) / len(results), 1) if results else None,
        "cost_usd": round(sum(s.cost_usd for s in results), 4),
        "tokens": dict(sum((Counter(s.tokens) for s in results), Counter())),
        "rate_limit": Counter(str((s.rate_limit or {}).get("status")) for s in results),
        "rate_limit_windows": sorted({str((s.rate_limit or {}).get("rateLimitType")) for s in results}),
        "errors": Counter(s.error.split(":")[0] for s in results if s.error),
        "retries": sum(s.call_attempts - 1 for s in results),
        # What failed calls printed, so a stop for "Claude is not available" can be understood.
        "failures": [f"{s.error}: {s.diagnostic}"[:800] for s in results if s.diagnostic][:10],
        "statuses": Counter(s.status for s in ok),
        "fits": Counter(s.result["fit"] for s in ok),
    }


def run_phases(run: dict[str, Any]) -> list[dict[str, Any]]:
    """A run record's scoring phases, whatever its format. Runs before 2026-10-24 18:00 stored
    one flat stats dict ({"calls": 8, ...}) instead of phases ({"scheduled": {...}}); runs that
    did not score have none."""
    scoring_stats = run.get("scoring")
    if not isinstance(scoring_stats, dict):
        return []
    if "calls" in scoring_stats:  # the older flat format: one phase (its nested dicts are stats, not phases)
        return [scoring_stats]
    return [v for v in scoring_stats.values() if isinstance(v, dict)]


def phase_usage(phase: dict[str, Any], share_per_call: dict[str, float], per_usd: float | None = None) -> float:
    """Share of a 5-hour window a scoring phase used. Since 2026-10-30 (with `per_usd`, the rate
    measured from Claude's own five-hour readings) from its API-equivalent cost, also for runs
    recorded before then, whose recorded share came from fixed per-call figures that had grown
    up to nine times too high as calls got cheaper. Otherwise as recorded, or for runs from before
    the usage guard (2026-10-24), estimated from its calls and the share per call. Flat-format runs
    name no model; they all predate Sonnet scoring, so they count as Haiku."""
    if per_usd is not None and phase.get("cost_usd") is not None:
        return float(phase["cost_usd"]) * per_usd
    if phase.get("window_share_spent") is not None:
        return float(phase["window_share_spent"])
    kind = "titlecheck" if phase.get("titlecheck") else str(phase.get("model") or "haiku")
    return int(phase.get("calls") or 0) * float(share_per_call.get(kind, share_per_call.get("haiku", 0.0012)))


def per_usd_of(settings: dict[str, Any]) -> float | None:
    """Share of a five-hour window per dollar of API-equivalent cost (`[usage] window_share_per_usd`)."""
    value = settings.get("usage", {}).get("window_share_per_usd")
    return float(value) if value is not None else None


def is_scheduled(run: dict[str, Any]) -> bool:
    """A scheduled run: recorded as such, or (before runs recorded their kind) one on GitHub."""
    return run.get("kind") == "scheduled" or (not run.get("kind") and run.get("ran_on") == "github")


WEEK_SECONDS = 7 * 24 * 3600
LIMIT_NAMES = {"weekly": "weekly limit", "second": "second weekly limit"}


def claude_weeks(state: State, now: datetime, account: str | None = None,
                 settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Claude's own seven-day figures, newest of each, by limit (2026-10-30):

    - "weekly": the subscription's week, which every call has reported since late October 2026
      (`rate_limit.unifiedWindows.seven_day`; before, only warnings carried it), the user's own Claude
      Code use included.
    - "second": a seven-day limit Claude reports on its own (`rateLimitType` "seven_day" with no
      `unifiedWindows`) whose reset is not a whole number of weeks from the weekly limit's. Seen
      first on 2026-10-30 from the laptop, for Haiku and Sonnet calls alike: 26% resetting
      5 November 18:00 UTC while the weekly limit stood at 33% resetting 4 November 01:00. Claude
      does not name it (most likely a per-model weekly limit).

    Each reading after its reset counts as 0 until a call reports again. With `account`, only
    that account's readings (2026-11-18: GitHub and the laptop can use different accounts)."""
    readings: list[tuple[str, dict[str, Any], str | None]] = []  # (at, reading, kind when known)
    weekly_resets: set[int] = set()
    for score in state.scores.values():
        if account is not None and accounts.key_of(score, settings or {}) != account:
            continue
        limit, at = score.get("rate_limit") or {}, score.get("scored_at", "")
        unified = (limit.get("unifiedWindows") or {}).get("seven_day") or {}
        if isinstance(unified.get("utilization"), (int, float)):
            readings.append((at, unified, "weekly"))
            if isinstance(unified.get("resetsAt"), (int, float)):
                weekly_resets.add(int(unified["resetsAt"]))
        elif limit.get("rateLimitType") == "seven_day" and isinstance(limit.get("utilization"), (int, float)):
            readings.append((at, limit, None))
    found: dict[str, dict[str, Any]] = {}
    for at, reading, kind in readings:
        if kind is None:
            resets = reading.get("resetsAt")
            same_week = isinstance(resets, (int, float)) and (
                not weekly_resets or any((int(resets) - r) % WEEK_SECONDS == 0 for r in weekly_resets))
            kind = "weekly" if same_week else "second"
        if kind not in found or at > found[kind]["at"]:
            resets = reading.get("resetsAt")
            found[kind] = {"utilization": float(reading["utilization"]), "at": at, "status": reading.get("status"),
                           "limit": kind, "resets_at": datetime.fromtimestamp(resets, UTC).isoformat(timespec="seconds")
                           if isinstance(resets, (int, float)) else None}
    return {kind: fresh_week(reading, now) for kind, reading in found.items()}


def claude_week(state: State, now: datetime, account: str | None = None,
                settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The weekly figure that binds: the higher of Claude's seven-day limits (`claude_weeks`), with
    every limit under "limits". None when Claude has reported none. With `account`, that account's."""
    weeks = claude_weeks(state, now, account, settings)
    if not weeks:
        return None
    binding = max(weeks.values(), key=lambda w: (w["utilization"], w["limit"] == "weekly"))
    return {**binding, "limits": weeks, **({"account": account} if account is not None else {})}


def claude_week_by_account(state: State, now: datetime, settings: dict[str, Any], days: int = 7) -> dict[str, dict[str, Any]]:
    """Claude's weekly figure for each account that scored in the last `days` days, the token's
    account (GitHub's runs) first."""
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    seen = {accounts.key_of(s, settings) for s in state.scores.values() if str(s.get("scored_at", "")) >= since}
    order = sorted(seen, key=lambda k: (k != accounts.default_key(settings), k))
    weeks = {key: claude_week(state, now, key, settings) for key in order}
    return {key: week for key, week in weeks.items() if week}


def limits_text(claude: dict[str, Any] | None) -> str:
    """"33% of the weekly limit (resets 4 Nov 01:00 UTC) and 28% of the second weekly limit (...)"."""
    if not claude:
        return ""
    weeks = claude.get("limits") or {claude.get("limit", "weekly"): claude}
    parts = []
    for kind in ("weekly", "second"):
        week = weeks.get(kind)
        if week:
            resets = datetime.fromisoformat(week["resets_at"]).strftime("%-d %b %H:%M UTC") if week.get("resets_at") else ""
            parts.append(f"{week['utilization']:.0%} of the {LIMIT_NAMES[kind]}" + (f" (resets {resets})" if resets else "")
                         + (" (reset since its last figure)" if week.get("reset") else ""))
    return " and ".join(parts)


def fresh_week(week: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """A stored `claude_week` reading as of `now`: 0 once its week has reset."""
    if week and week.get("resets_at") and datetime.fromisoformat(week["resets_at"]) <= now:
        return {**week, "utilization": 0.0, "reset": True}
    return week


def weekly_guard(state: State, settings: dict[str, Any], now: datetime,
                 claude: dict[str, Any] | None = None, account: str | None = None) -> dict[str, Any]:
    """The weekly guard. Since 2026-10-29 it follows Claude's own seven-day figure (`claude_week`,
    which includes the user's own Claude use): at `weekly_guard_share` (75%) it goes on, below
    `weekly_guard_off_share` (65%) it goes off again, in between it stays as it was. Without any
    figure from Claude it falls back to the tool's own estimate for its scheduled runs. While on,
    the generated list needs graduate wording and its companies on `[generated] guard_pause_boards`
    are not fetched or scored; your companies and the wishlist are never paused. The backlog runs'
    hard stop reads Claude's figure too (2026-10-30), the tool's estimate (all runs) only without one.
    Since 2026-11-18 everything is per Claude account (`account`, by default the token's: GitHub's
    runs): another account's use neither switches the guard nor counts towards it."""
    account = account or accounts.default_key(settings)
    usage = settings.get("usage", {})
    limit = float(usage.get("windows_per_week", 12.5))
    on_at, off_at = float(usage.get("weekly_guard_share", 0.6)), float(usage.get("weekly_guard_off_share", 0.5))
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    runs = [r for r in state.runs() if accounts.key_of(r, settings) == account]
    share_per_call = {k: float(v) for k, v in usage.get("share_per_call", {}).items()}
    per_usd = per_usd_of(settings)
    week = [r for r in runs if str(r.get("started_at", "")) >= since]
    used = sum(phase_usage(phase, share_per_call, per_usd) for r in week for phase in run_phases(r))
    # The guard counts scheduled runs only (decided 2026-10-27): backlog runs are one-offs and
    # must not keep the daily boards paused. The total still drives the backlog runs' hard stop.
    used_scheduled = sum(phase_usage(phase, share_per_call, per_usd) for r in week if is_scheduled(r)
                         for phase in run_phases(r))
    share = used / limit if limit else 0.0
    scheduled_share = used_scheduled / limit if limit else 0.0
    previous = next((bool(r["weekly_guard"].get("filter_on")) for r in reversed(runs)
                     if isinstance(r.get("weekly_guard"), dict)), False)
    claude = fresh_week(claude, now) if claude is not None else (
        claude_week(state, now, account, settings) if hasattr(state, "scores") else None)
    measure = claude["utilization"] if claude else scheduled_share
    filter_on = True if measure >= on_at else False if measure < off_at else previous
    # The tool's runs over the week Claude's figure covers (since its limit last reset), so the Runs
    # page can say how much of that figure is the tool's: the rolling 7 days above can reach back
    # before the reset and come out above Claude's own figure (83% against 37% on 2026-10-30).
    tool_claude_week = None
    if claude and claude.get("resets_at"):
        resets = datetime.fromisoformat(claude["resets_at"])
        week_start = (resets if claude.get("reset") else resets - timedelta(days=7)).isoformat(timespec="seconds")
        tool_claude_week = round(sum(phase_usage(phase, share_per_call, per_usd) for r in runs
                                     if str(r.get("started_at", "")) >= week_start for phase in run_phases(r))
                                 / limit, 3) if limit else 0.0
    generated = settings.get("generated", {})
    pause = sorted(set(generated.get("guard_pause_boards", [])) - set(generated.get("exclude_boards", [])))
    return {"used_windows": round(used, 2), "share_of_week": round(share, 3), "filter_on": filter_on,
            "scheduled_windows": round(used_scheduled, 2), "scheduled_share": round(scheduled_share, 3),
            "changed": filter_on != previous, "on_at": on_at, "off_at": off_at,
            "claude": claude, "measure": "claude" if claude else "estimate", "tool_claude_week": tool_claude_week,
            "account": account,
            "entry_level_only": bool(generated.get("entry_level_only", False)),
            "pause_boards": pause, "paused_boards": pause if filter_on else []}


BOARD_NAMES = {"email": "Job alert email", "workday": "Workday", "personio": "Personio", "teamtailor": "Teamtailor", "workable": "Workable",
               "smartrecruiters": "SmartRecruiters", "join": "JOIN", "greenhouse": "Greenhouse", "lever": "Lever",
               "ashby": "Ashby", "recruitee": "Recruitee", "homerun": "Homerun"}


RECHECK_DAYS = 30  # a company skipped for having only UK and US roles is looked at again after this


def only_outside_park(state: State, settings: dict[str, Any], today: date) -> set[tuple[str, str]]:
    """(company, board) of generated companies on `[generated] outside_park_only` boards whose
    stored roles show none passing the pre-filter outside the parked countries (UK, US): they are
    not fetched, until their roles are RECHECK_DAYS old (then fetched once to look again)."""
    generated = settings.get("generated", {})
    boards = set(generated.get("outside_park_only", []))
    if not boards:
        return set()
    park = set(generated.get("park_countries", []))
    seen: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in state.jobs.values():
        if record.get("source") in boards and record.get("origin") == "generated":
            seen[(record["company"], record["source"])].append(record)
    since = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    found = set()
    for key, records in seen.items():
        outside = any(not r["closed"] and r.get("prefilter") == "pass" and not prefilter.parked(r.get("locations") or [], park)
                      for r in records)
        recent = max((str(r.get("last_seen") or r.get("first_seen") or "")[:10] for r in records), default="") >= since
        if not outside and recent:
            found.add(key)
    return found


def workday_local_search(state: State, settings: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Local entry-level words for each generated Workday site, by the countries its stored roles
    are in (`[generated] workday_search_local`, 2026-10-29): Workday's search needs every word of a
    query to match, so each word is its own search, one page each."""
    words = {code: list(ws) for code, ws in settings.get("generated", {}).get("workday_search_local", {}).items()}
    if not words:
        return {}
    found: dict[str, set[str]] = defaultdict(set)
    for record in state.jobs.values():
        if record.get("source") == "workday" and record.get("origin") == "generated" and not record["closed"]:
            for location in record.get("locations") or []:
                found[record["company"]] |= geo.parse(location).codes & set(words)
    return {company: tuple(dict.fromkeys(w for code in sorted(codes) for w in words[code]))
            for company, codes in found.items() if codes}


def wait_notes(run: dict[str, Any]) -> list[str]:
    """Hosts that asked to wait and boards taken off the generated list, as lines for the run
    output and the Runs page."""
    notes = []
    for host, wait in (run.get("host_waits") or {}).items():
        until = datetime.fromisoformat(wait["until"]).strftime("%-d %b %H:%M UTC")
        notes.append(f"{host} {'asked to wait' if wait.get('new') else 'is waiting'} until {until}"
                     + (f": {wait['companies_not_fetched']} companies not fetched" if wait.get("companies_not_fetched") else ""))
    if run.get("backlog_boost"):
        boost = run["backlog_boost"]
        left = boost.get("waiting_left", boost.get("backlog_left", 0))
        notes.append(f"{left:,} roles waiting: this run could use {boost['share']:.0%} of a window")
    if run.get("only_uk_us_not_fetched"):
        notes.append(f"{run['only_uk_us_not_fetched']} generated companies with roles only in the UK and US not fetched")
    for board, info in (run.get("boards_dropped") or {}).items():
        notes.append(f"{BOARD_NAMES.get(board, board)} is off the generated list (refused again on its longer delay)"
                     + (f": {info['companies_not_fetched']} companies not fetched" if info.get("companies_not_fetched") else ""))
    return notes


def guard_words(guard: dict[str, Any]) -> str:
    """The weekly guard's state in a sentence, for run output, `jobhunt status` and the Runs page."""
    boards = ", ".join(BOARD_NAMES.get(b, b) for b in guard.get("pause_boards") or [])
    on_at, off_at = f"{guard.get('on_at', 0.4):.0%}", f"{guard.get('off_at', 0.3):.0%}"
    words = ("Graduate-wording filter for the generated list: always on. " if guard.get("entry_level_only")
             else f"Graduate-wording filter for the generated list: {'ON' if guard['filter_on'] else 'off'}. ")
    claude = guard.get("claude")
    if claude:  # Claude's own figure (2026-10-29); both weekly limits since 2026-10-30
        what = "the week (Claude's figure, your own use included)"
        scheduled = f" Claude reports {limits_text(claude)}"
    else:
        what = "the week by scheduled runs (the tool's estimate)"
        scheduled = (f" Scheduled runs used {guard['scheduled_share']:.0%} of the week"
                     if isinstance(guard.get("scheduled_share"), (int, float)) else "")
    if guard["filter_on"] and guard.get("paused_boards"):
        words += (f"Weekly guard ON: {boards} paused for the generated list (your companies and wishlist are "
                  f"still scored) until less than {off_at} of {what} is used.{scheduled}.")
    elif guard["filter_on"]:
        words += f"Weekly guard ON until less than {off_at} of {what} is used.{scheduled}."
    else:
        words += (f"Weekly guard off" + (f": at {on_at} of {what} it pauses {boards} for the generated list."
                                         if boards else ".") + scheduled + ("." if scheduled else ""))
    if guard.get("changed"):
        words += f" (Switched {'on' if guard['filter_on'] else 'off'} in this run.)"
    return words


def titlecheck_pending(state: State, config: Path, settings: dict[str, Any], select: Select, parallel: int,
                       runner: scoring.Runner, guard: scoring.UsageGuard,
                       progress: Progress | None = None) -> dict[str, Any]:
    """Screen pending generated-list roles in batches. Each checked role gets a `titlecheck`
    decision; drops are also appended to drops.jsonl with their reason and profile quote.
    Roles in batches that failed or were not started stay pending."""
    pending = sorted((r for r in state.jobs.values() if select(r)), key=priority)
    prompt, _ = titlecheck.build_prompt(config, settings)
    spent_before = guard.spent
    items = [titlecheck.Item(key=r["key"], company=r["company"], title=r["title"], department=r.get("department"),
                             locations=tuple(r.get("merged_locations") or r["locations"])) for r in pending]
    progress = progress or Silent()
    if items:
        size = int(settings.get("titlecheck", {}).get("batch_size", 100))
        progress.phase(f"title check ({len(items)} roles)", total=-(-len(items) // size),
                       budget=lambda: (guard.spent, guard.max_share))
    outcome = (titlecheck.check(items, config, settings, runner, parallel, guard, on_batch=progress.advance)
               if items else titlecheck.Outcome(stopped=guard.stopped))
    record_titlecheck(state, pending, outcome, prompt.version)
    unchecked = set(outcome.unchecked)
    drops = {d.key: d for d in outcome.drops}
    return {"model": prompt.model, "prompt_version": prompt.version, "titlecheck": True, "pending": len(pending),
            "checked": len(pending) - len(unchecked), "dropped": len(drops), "rejected_ungrounded": len(outcome.rejected),
            "calls": outcome.calls, "failed": outcome.failed_calls, "cost_usd": round(outcome.cost_usd, 4),
            "window_share_spent": round(guard.spent - spent_before, 4), "stopped": guard.stopped,
            "failures": outcome.failures[:10]}


def week_nearly_full(state: State, now: datetime, stop_at: float = 0.75, account: str | None = None,
                     settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Claude's latest word on the limits, from the stored scores: when it was a refusal about a
    seven-day limit, or a warning at or above `stop_at` (the weekly guard's threshold; 2026-10-30:
    Claude warns about a second weekly limit from about 25%), and the week has not reset since, the
    week is nearly full until a later call reports the five-hour window again."""
    latest = None
    for score in state.scores.values():
        if account is not None and accounts.key_of(score, settings or {}) != account:
            continue
        limit = score.get("rate_limit") or {}
        if limit.get("rateLimitType") and (latest is None or score.get("scored_at", "") > latest[0]):
            latest = (score.get("scored_at", ""), limit)
    if latest is None:
        return None
    at, limit = latest
    if limit.get("rateLimitType") != "seven_day" or limit.get("status") not in ("allowed_warning", "rejected"):
        return None
    if limit.get("status") == "allowed_warning" and isinstance(limit.get("utilization"), (int, float)) \
            and limit["utilization"] < stop_at:
        return None
    resets = limit.get("resetsAt")
    if isinstance(resets, (int, float)) and datetime.fromtimestamp(resets, UTC) <= now:
        return None
    used = limit.get("utilization")
    return {"at": at, "utilization": used,
            "resets_at": datetime.fromtimestamp(resets, UTC).isoformat(timespec="seconds")
            if isinstance(resets, (int, float)) else None,
            "message": f"Claude warned the weekly limit is {used:.0%} used" if isinstance(used, (int, float))
            else "Claude warned the weekly limit is nearly used"}


def could_be_scored(r: dict[str, Any], rules: GeneratedRules) -> bool:
    """Whether a role will be scored once its posting text is in: open, passing the pre-filter,
    not a duplicate, skipped as old, dropped by the title check or blocked; for the generated
    list also not parked and, with the graduate-wording filter on, worded for graduates (before
    2026-10-28 the status counted those too, so the backlog looked four times its size)."""
    if r["closed"] or r["prefilter"] != "pass" or r.get("duplicate_of") or r.get("backfill_skip"):
        return False
    if (r.get("titlecheck") or {}).get("dropped") or rules.is_blocked(r):
        return False
    if r.get("parked") and not is_generated(r):
        return False  # a company's `park_countries`
    if is_generated(r):
        if r.get("parked") and not rules.score_parked:
            return False
        if rules.needs_entry_terms(r.get("locations") or []) and not prefilter.entry_terms(r["title"]):
            return False
    return True


def cascade_companies(state: State, settings: dict[str, Any], now: datetime | None = None) -> frozenset[str]:
    """My companies (companies.toml and the wishlist) scored by the cascade, less `[scoring]
    cascade_exempt`: more than `cascade_above_open` open roles passing the pre-filter (2026-10-28:
    Sonnet for ING alone took most of every run's budget), or more than `cascade_above_new_per_day`
    new ones a day on average over the last week (2026-10-29: Google, 42 open but 23 new in a day)."""
    scoring_settings = settings.get("scoring", {})
    above, per_day = scoring_settings.get("cascade_above_open"), scoring_settings.get("cascade_above_new_per_day")
    if above is None and per_day is None:
        return frozenset()
    exempt = {prefilter.normalise_company(n) for n in scoring_settings.get("cascade_exempt", [])}
    week_ago = ((now or datetime.now(UTC)) - timedelta(days=7)).isoformat(timespec="seconds")
    open_roles: Counter[str] = Counter()
    new_roles: Counter[str] = Counter()
    for r in state.jobs.values():
        if is_generated(r) or r.get("prefilter") != "pass" or r.get("duplicate_of"):
            continue
        name = prefilter.normalise_company(r["company"])
        open_roles[name] += not r["closed"]
        new_roles[name] += str(r.get("first_seen") or "") >= week_ago
    large = {n for n, k in open_roles.items() if above is not None and k > int(above)}
    busy = {n for n, k in new_roles.items() if per_day is not None and k / 7 > float(per_day)}
    return frozenset((large | busy) - exempt)


def cascade_low_bar(state: State, settings: dict[str, Any]) -> frozenset[str]:
    """The wishlist and the core targets: their Haiku 5+ goes on to Sonnet."""
    core = {prefilter.normalise_company(n) for n in settings.get("scoring", {}).get("cascade_core", [])}
    wished = {e["id"] for e in read_jsonl(state.root / "wishlist.jsonl") if e.get("on")}
    return frozenset(core | wished)


def place_unknown(r: dict[str, Any]) -> bool:
    """A Workday role whose listing names no country ("Main Campus", "3 Locations"): its detail page
    has the country field, so it is read before the title check (2026-10-29: 750 of the 1,488
    Workday roles waiting, many of them in the US)."""
    return r.get("source") == "workday" and not any(geo.parse(x).recognised for x in r.get("locations") or [])


def needs_title_check(r: dict[str, Any], rules: GeneratedRules) -> bool:
    """A generated-list role (or one at my large companies) that will be scored once checked and
    has no title-check decision yet. The check needs only the title, company, department and
    locations, so it runs before the posting text is fetched (2026-10-29): text is then fetched
    only for roles that pass."""
    if "titlecheck" in r or not could_be_scored(r, rules) or rules.left_out(r):
        return False
    if is_generated(r) and place_unknown(r) and not r.get("description_hash"):
        return False  # its detail page comes first: it names the country, and US-only roles are parked
    if is_generated(r):
        if rules.guard_filter and not r.get("backlog") and not prefilter.entry_terms(r["title"]):
            return False  # held back by the weekly guard's filter (daily roles only)
        return not rules.paused and r.get("source") not in rules.paused_boards
    return rules.in_cascade(r)


def backlog_left(state: State, settings: dict[str, Any]) -> int:
    """Generated-list backlog roles still to score: waiting for their text, the title check or a
    first score."""
    rules = GeneratedRules.from_settings(settings)
    scheduled, backlog = models(settings)
    first = unscored(state, list(dict.fromkeys([scheduled, backlog])), backlog)
    return sum(1 for r in state.jobs.values()
               if is_generated(r) and r.get("backlog") and could_be_scored(r, rules)
               and (not r.get("description_hash") or "titlecheck" not in r or first(r)))


WAITING = ("waiting for posting text", "waiting for the title check", "waiting for a first score")


def waiting_left(state: State, settings: dict[str, Any], now: datetime | None = None) -> int:
    """Every role still to score, yours and the generated list's: waiting for its text, the title
    check or a first score (the Waiting count). A large one lets scheduled runs use more of a window;
    2026-11-18: counting only roles marked backlog missed the 3,600 the generated list's restore
    brought, as they were new roles, not a first fetch's."""
    parts = status(state, settings, now or datetime.now(UTC))["parts"]
    return sum(int(counts.get(key, 0)) for counts in parts.values() for key in WAITING)


def status(state: State, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What is waiting, for `jobhunt status`: per part (my companies, generated list), roles
    waiting for posting text, the title check, a first score and a Sonnet rescore; the backlog's
    progress; and this week's usage."""
    guard_state = weekly_guard(state, settings, now)
    rules = GeneratedRules.from_settings(settings, guard_filter=bool(guard_state["filter_on"]),
                                         blocked=blocklist.names(state.root))
    rules = replace(rules, cascade_mine=cascade_companies(state, settings))
    scheduled, backlog = models(settings)
    both = list(dict.fromkeys([scheduled, backlog]))
    min_fit = int(settings.get("scoring", {}).get("backlog_rescore_min_fit", 6))
    parts = {"my companies": Counter(), "generated list": Counter()}

    first = unscored(state, both, backlog)
    sonnet = worth_rescoring(state, backlog, min_fit, scheduled)
    for r in state.jobs.values():
        part = parts["generated list" if is_generated(r) else "my companies"]
        if not could_be_scored(r, rules):
            continue
        if r.get("backlog"):
            part["backlog total"] += 1
        if not r.get("description_hash"):
            part["waiting for posting text"] += 1
            continue
        if is_generated(r) and not generated_ok(r, rules):
            continue  # held back by the weekly guard's filter
        if (is_generated(r) or rules.in_cascade(r)) and "titlecheck" not in r:
            part["waiting for the title check"] += 1
        elif first(r):
            part["waiting for a first score"] += 1
        elif sonnet(r):
            part["waiting for Sonnet (Haiku 6+)"] += 1
        elif r.get("backlog"):
            part["backlog done"] += 1
    return {"parts": {name: dict(c) for name, c in parts.items()}, "weekly_guard": guard_state,
            "models": {"scheduled": scheduled, "backlog": backlog},
            "failing_on_github": failing_on_github(state.runs()), "week_nearly_full": week_nearly_full(
                state, now, float(settings.get("usage", {}).get("weekly_guard_share", 0.75)),
                accounts.default_key(settings), settings)}


def code_version(config: Path) -> str | None:
    """The commit the run's code is at (GitHub sets it; on the laptop, git)."""
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:12]
    try:
        import subprocess
        done = subprocess.run(["git", "-C", str(config.parent), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10)
        return done.stdout.strip()[:12] or None
    except (OSError, subprocess.SubprocessError):
        return None


def fetch_history(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each company named in run summaries (your companies, the wishlist and,
    since 2026-10-30, your companies with the generated list's rules): its last fetch
    without an error, and the latest error if the latest fetch failed."""
    found: dict[str, dict[str, Any]] = {}
    for run in sorted(runs, key=lambda r: r.get("started_at", "")):
        for name, entry in (run.get("sources") or {}).items():
            item = found.setdefault(prefilter.normalise_company(name), {})
            if entry.get("error"):
                item["last_error"] = entry["error"]
            else:
                item["last_fetch"] = {"at": run.get("started_at"), "where": run.get("ran_on") or "laptop"}
                item.pop("last_error", None)
    return found


def github_failed_latest(runs: list[dict[str, Any]]) -> set[str]:
    """Companies whose latest fetch on GitHub failed: the next laptop run fetches them too (2026-11-18),
    so a site that refuses GitHub now and then still gets fetched."""
    latest: dict[str, bool] = {}
    for run in runs:
        if run.get("ran_on") == "github":
            for name, entry in (run.get("sources") or {}).items():
                latest[name] = bool(entry.get("error"))
    return {name for name, failed in latest.items() if failed}


LOCAL_KIND = "(local sites)"


def last_local_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The latest `run --local-only` on the laptop (the launchd job's, or one run by hand)."""
    return next((r for r in reversed(runs) if r.get("ran_on") == "laptop" and str(r.get("kind", "")).endswith(LOCAL_KIND)),
                None)


def failing_on_github(runs: list[dict[str, Any]], last: int = 12) -> dict[str, dict[str, Any]]:
    """My companies (and wishlist ones) whose fetch failed in recent scheduled runs on GitHub: a
    site that blocks cloud servers is fetched from the laptop instead (fetch_from = "local")."""
    github = [r for r in runs if r.get("ran_on") == "github"][-last:]
    laptop_ok = {name for r in runs if r.get("ran_on") == "laptop"
                 for name, entry in (r.get("sources") or {}).items() if "error" not in entry}
    # The latest laptop fetch of each company, "ok" or "failed": a company GitHub cannot fetch is
    # fetched in the next laptop run too, so Today can say whether anything is missed (2026-11-18).
    laptop_latest = {name: "failed" if "error" in entry else "ok" for r in runs if r.get("ran_on") == "laptop"
                     for name, entry in (r.get("sources") or {}).items()}
    # The code the latest run used: a failure from older code may already be fixed (2026-10-29).
    current = next((r.get("code") for r in reversed(github) if r.get("code")), None)
    failing: dict[str, dict[str, Any]] = {}
    for run in github:
        for name, entry in (run.get("sources") or {}).items():
            if "error" in entry:
                found = failing.setdefault(name, {"failed": 0, "runs": len(github), "error": "", "current": False,
                                                  "works_on_laptop": name in laptop_ok,
                                                  "laptop": laptop_latest.get(name)})
                found["failed"] += 1
                found["error"] = entry["error"]
                found["current"] = found["current"] or (current is not None and run.get("code") == current)
    # Flagged when it failed with the current code, or in two or more of the recent runs; cleared as
    # soon as its latest fetch worked, on GitHub or on the laptop (2026-11-18: LSEG and BlackRock,
    # now fetched from the laptop only, would otherwise stay flagged for good).
    latest_ok = {name for name in failing
                 if (last := next((run["sources"][name] for run in reversed(runs) if name in (run.get("sources") or {})), None))
                 is not None and "error" not in last}
    return {name: f for name, f in failing.items() if (f["current"] or f["failed"] >= 2) and name not in latest_ok}


def record_titlecheck(state: State, records: list[dict[str, Any]], outcome: titlecheck.Outcome, version: str) -> None:
    """Each checked role gets a `titlecheck` decision; drops also go to drops.jsonl."""
    unchecked = set(outcome.unchecked)
    drops = {d.key: d for d in outcome.drops}
    now = now_iso()
    with (state.root / "drops.jsonl").open("a", encoding="utf-8") as log:
        for record in records:
            if record["key"] in unchecked:
                continue
            drop = drops.get(record["key"])
            record["titlecheck"] = {"version": version, "dropped": drop is not None, "checked_at": now}
            record["updated_at"] = now
            if drop:
                record["titlecheck"].update(reason=drop.reason, quote=drop.profile_quote)
                log.write(json.dumps({"key": record["key"], "company": record["company"], "title": record["title"],
                                      "reason": drop.reason, "quote": drop.profile_quote, "version": version,
                                      "checked_at": now}, ensure_ascii=False) + "\n")


def apply_score_policies(state: State, settings: dict[str, Any]) -> int:
    """Bring stored scores in line with post-processing settings switched on after they were made
    (today: visa_never_blocks). Returns how many scores changed."""
    scoring_settings = settings.get("scoring", {})
    if not scoring_settings.get("visa_never_blocks"):
        return 0
    consider = bool(scoring_settings.get("consider_start_date", False))
    changed = 0
    for record in state.scores.values():
        result = record.get("result")
        if not result:
            continue
        fixed, n = scoring.visa_as_check(result)
        if n:
            record["result"] = fixed
            record["status"] = scoring.status(fixed["blockers"], consider)
            changed += 1
    return changed


def score_all(config: Path, settings: dict[str, Any], state: State, options: Options, runner: scoring.Runner,
              rules: GeneratedRules | None = None, progress: Progress | None = None,
              guard: scoring.UsageGuard | None = None) -> dict[str, dict[str, Any]]:
    """Every scoring phase of a run, sharing one usage guard; returns stats per phase.

    Configured companies: a scheduled run scores new roles with the scheduled model (Sonnet); a
    backlog run scores with the backlog model (Haiku), then rescores with Sonnet every role it rated
    `backlog_rescore_min_fit` or more. Generated-list roles always go through the title check, then
    Haiku, then Sonnet for Haiku 6+.

    A backlog run first gives Sonnet the roles Haiku rated 6+ in earlier runs (highest Haiku fit
    first), so promising roles reach Apply now before any budget goes on new Haiku scoring.
    `sonnet_only` runs only that phase."""
    rules = rules or GeneratedRules.from_settings(settings)
    scoring_settings = settings.get("scoring", {})
    scheduled, backlog = models(settings)
    both = list(dict.fromkeys([scheduled, backlog]))
    cap = options.cap if options.cap is not None else int(scoring_settings.get("max_calls_per_run", 100))
    parallel = options.parallel or int(scoring_settings.get("parallel", 4))
    if guard is None:  # a run shares one guard with the scoring done while fetching
        guard = scoring.UsageGuard.from_settings(
            settings, "backlog" if options.backlog or options.sonnet_only else "scheduled")
        if options.budget is not None:
            guard.max_share = options.budget
        guard.ignore_weekly_warning = options.ignore_weekly_warning
    common = dict(cap=cap, parallel=parallel, runner=runner,
                  timeout=float(scoring_settings.get("call_timeout_seconds", 180)), guard=guard, progress=progress)
    min_fit = int(scoring_settings.get("backlog_rescore_min_fit", 6))
    main = scoring.build_prompt(config, settings, model=scheduled)
    cheap = scoring.build_prompt(config, settings, model=backlog) if backlog != scheduled else main
    phases: dict[str, dict[str, Any]] = {}

    def generated(r: dict[str, Any]) -> bool:
        return generated_ok(r, rules)

    def my(r: dict[str, Any]) -> bool:
        return mine(r, rules)

    def kept(r: dict[str, Any]) -> bool:
        return generated(r) and bool(r.get("titlecheck")) and not r["titlecheck"]["dropped"]

    def big(r: dict[str, Any]) -> bool:  # one of my large companies: the cascade
        return my(r) and rules.in_cascade(r)

    def big_kept(r: dict[str, Any]) -> bool:
        return big(r) and bool(r.get("titlecheck")) and not r["titlecheck"]["dropped"]

    def big_cascade() -> None:
        """Title check, then the cheap model, then Sonnet for its 6+, for my large companies."""
        if not any(big(r) for r in state.jobs.values()):
            return
        phases["your large companies titlecheck"] = titlecheck_pending(
            state, config, settings, lambda r: big(r) and needs_title_check(r, rules), parallel, runner, guard, progress)
        if not guard.stopped:
            phases["your large companies"] = score_pending(state, cheap, unscored(state, both, backlog, big_kept),
                                                           label="your large companies, first pass", **common)
        if not guard.stopped and cheap is not main:
            low = int(scoring_settings.get("cascade_core_min_fit", min_fit))

            def low_bar(r: dict[str, Any]) -> bool:
                return big_kept(r) and prefilter.normalise_company(r["company"]) in rules.cascade_low_bar
            wanted = worth_rescoring(state, backlog, min_fit, scheduled, lambda r: big_kept(r) and not low_bar(r))
            wanted_low = worth_rescoring(state, backlog, low, scheduled, low_bar)
            phases["your large companies rescore"] = score_pending(
                state, main, lambda r: wanted(r) or wanted_low(r),
                label="your large companies, fit 6+ again (5+ for the wishlist and core targets)", **common)

    def best_first(r: dict[str, Any]) -> tuple:  # highest backlog-model fit first, then the usual order
        return (-current_score(state, r, backlog)["result"]["fit"], *priority(r))

    if (options.backlog or options.sonnet_only) and backlog != scheduled:
        phases["fit 6+ from earlier runs"] = score_pending(
            state, main, worth_rescoring(state, backlog, min_fit, scheduled, lambda r: my(r) or kept(r)),
            label="fit 6+ from earlier runs", order=best_first, **common)
        if options.sonnet_only or guard.stopped:
            return phases

    if options.backlog and backlog != scheduled:
        if any(my(r) and r.get("score_with") == "sonnet" for r in state.jobs.values()):
            phases["your companies and wishlist, Sonnet directly"] = score_pending(
                state, main, unscored(state, both, scheduled, lambda r: my(r) and r.get("score_with") == "sonnet"),
                label="your companies and wishlist, Sonnet directly", **common)
        big_cascade()
        phases["backlog"] = score_pending(
            state, cheap, unscored(state, both, backlog,
                                   lambda r: my(r) and r.get("score_with") != "sonnet" and not big(r)),
            label="your companies and wishlist, first pass", **common)
        if not guard.stopped:
            phases["rescore"] = score_pending(
                state, main, worth_rescoring(state, backlog, min_fit, scheduled, my),
                label="your companies and wishlist, fit 6+ again", **common)
    else:
        phases["scheduled"] = score_pending(
            state, main, unscored(state, both, scheduled,
                                  lambda r: my(r) and r.get("score_with") != "cascade" and not big(r)),
            label="your companies and wishlist", **common)
        if not guard.stopped:
            big_cascade()
        # A starred company's roles scored only by Haiku (while it followed the generated list's
        # rules, before it was starred) get Sonnet once (2026-11-18): the wishlist is scored by
        # Sonnet directly. Large companies keep the cascade.
        starred = wishlist_names(state.root)
        if starred and not guard.stopped and cheap is not main:
            phases["wishlist, Sonnet for earlier Haiku scores"] = score_pending(
                state, main, lambda r: (my(r) and not big(r) and prefilter.normalise_company(r["company"]) in starred
                                        and has_result(state, r, backlog) and not has_result(state, r, scheduled)
                                        and not given_up(state, r, scheduled)),
                label="wishlist, Sonnet for earlier Haiku scores", **common)

    # Rescores asked for with `jobhunt rescore --queue`, a few each scheduled run, highest fit first.
    per_run = int(scoring_settings.get("rescore_per_run", 30))
    if per_run and not options.backlog and not guard.stopped and any(r.get(RESCORE_FIELD) for r in state.jobs.values()):
        for prompt in [main] if cheap is main else [main, cheap]:  # a Prompt holds a dict: not hashable
            if guard.stopped:
                break
            select = requested_rescore(state, prompt)
            if any(select(r) for r in state.jobs.values()):
                phases[f"requested rescore {prompt.model}"] = score_pending(
                    state, prompt, select, label=f"requested rescore ({prompt.model})",
                    order=lambda r, m=prompt.model: (-current_score(state, r, m)["result"]["fit"], *priority(r)),
                    **{**common, "cap": per_run})
        clear_done_rescores(state, [main, cheap], datetime.now(UTC).isoformat(timespec="seconds"))

    if not guard.stopped and any(generated(r) or (is_generated(r) and needs_title_check(r, rules))
                                 for r in state.jobs.values()):
        phases["generated titlecheck"] = titlecheck_pending(
            state, config, settings, lambda r: is_generated(r) and needs_title_check(r, rules), parallel, runner,
            guard, progress)
        # Sonnet for Haiku 6+ from earlier (and from during this fetch) first, highest Haiku fit first
        # (2026-10-31): with a large backlog the time limit stopped every run in the Haiku phase, so
        # the most promising roles never reached Sonnet, Apply now or the alerts.
        earlier = worth_rescoring(state, backlog, min_fit, scheduled, kept)
        if not guard.stopped and cheap is not main and any(earlier(r) for r in state.jobs.values()):
            phases["generated rescore first"] = score_pending(
                state, main, earlier, label="generated list, fit 6+ from earlier", order=best_first, **common)
        if not guard.stopped:
            phases["generated"] = score_pending(state, cheap, unscored(state, both, backlog, kept),
                                                label="generated list", **common)
        if not guard.stopped and cheap is not main:
            phases["generated rescore"] = score_pending(
                state, main, worth_rescoring(state, backlog, min_fit, scheduled, kept),
                label="generated list, fit 6+ again", **common)
    return phases


def run(config: Path, settings: dict[str, Any], companies: list[Company], state: State, options: Options,
        make_http: Callable[[], Http] | None = None, runner: scoring.Runner = scoring.run_subprocess,
        now: datetime | None = None, progress: Progress | None = None) -> dict[str, Any]:
    progress = progress or Silent()
    started = time.monotonic()
    now_dt = now or datetime.now(UTC)
    now = now_dt.isoformat(timespec="seconds")
    allowed = set(settings.get("locations", {}).get("allowed_countries", []))
    fetching = settings.get("fetching", {})
    make_http = make_http or (lambda: Http.from_settings(settings))
    claude_account = use_account(settings)  # which Claude account this run uses (2026-11-18)
    display.use_names(c.name for c in companies)  # alerts show your names as you wrote them
    guard_state = weekly_guard(state, settings, now_dt, account=claude_account["key"])
    rules = GeneratedRules.from_settings(settings, guard_filter=bool(guard_state["filter_on"]),
                                         blocked=blocklist.names(state.root), only=options.only,
                                         paused_boards=frozenset(guard_state["paused_boards"]))
    rules = replace(rules, cascade_mine=cascade_companies(state, settings, now_dt),
                    cascade_low_bar=cascade_low_bar(state, settings))
    if rules.paused_boards:
        held = [c for c in companies if c.origin == "generated" and c.board in rules.paused_boards]
        companies = [c for c in companies if not (c.origin == "generated" and c.board in rules.paused_boards)]
        guard_state = {**guard_state, "companies_not_fetched": len(held)}
    policy_fixes = apply_score_policies(state, settings)

    stop_at = float(settings.get("usage", {}).get("backlog_stop_share", 0.6))
    nearly_full = None if options.ignore_weekly_warning else week_nearly_full(
        state, now_dt, float(settings.get("usage", {}).get("weekly_guard_share", 0.75)), claude_account["key"], settings)
    if nearly_full and (options.backlog or options.sonnet_only):
        # The week is nearly full (Claude said so, and it counts the user's own use too): backlog
        # runs wait until it clears.
        summary = {"started_at": now, "companies": 0, "errors": 0, "jobs": {}, "duplicates": 0, "sources": {},
                   "weekly_guard": guard_state, "seconds": round(time.monotonic() - started, 1), "account": claude_account,
                   "ran_on": "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "laptop",
                   "backlog_stopped": f"{nearly_full['message']} ({nearly_full['at'][:16]}): backlog runs wait until "
                                      "the week clears (--ignore-weekly-warning to run anyway)"}
        state.save()
        state.append_run(summary)
        progress.done()
        return summary
    if nearly_full:
        rules = replace(rules, paused=True)  # scheduled runs score only your companies and the wishlist
    # Hard stop: a backlog run does not start when the week is already well used, so the user is not
    # locked out of Claude for days. Scheduled runs carry on. Since 2026-10-30 it reads Claude's own
    # seven-day figure (the user's own use included), as the weekly guard does; the tool's estimate ran
    # at more than twice it (72% against 33%) and only counts when Claude has given no figure.
    claude_used = (guard_state.get("claude") or {}).get("utilization")
    week_used = float(claude_used) if claude_used is not None else guard_state["share_of_week"]
    if (options.backlog or options.sonnet_only) and week_used >= stop_at:
        reason = (f"Claude reports {limits_text(guard_state['claude'])}" if claude_used is not None
                  else f"the tool used {week_used:.0%} of the weekly limit in the last 7 days (its own estimate)")
        summary = {"started_at": now, "companies": 0, "errors": 0, "jobs": {}, "duplicates": 0, "sources": {},
                   "weekly_guard": guard_state, "seconds": round(time.monotonic() - started, 1), "account": claude_account,
                   "ran_on": "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "laptop",
                   "backlog_stopped": f"{reason} (backlog runs stop at {stop_at:.0%})"}
        if policy_fixes:
            summary["score_policy_fixes"] = policy_fixes
        state.save()
        state.append_run(summary)
        progress.done()
        return summary

    # Hosts that asked to be left alone (a long Retry-After) are not contacted until the time has
    # passed, on GitHub or the laptop; a board that refused again on its longer delay is off the
    # generated list (hostwaits.py).
    hostwaits.load(state.root)
    waits_now, dropped = hostwaits.waiting(), hostwaits.dropped_boards()
    not_fetched: Counter[str] = Counter()
    dropped_skipped: Counter[str] = Counter()
    only_outside = only_outside_park(state, settings, now_dt.date())
    local_words = workday_local_search(state, settings)
    outside_skipped = 0
    kept_companies = []
    for company in companies:
        key = rate_key(host_of(company))
        if key in waits_now:
            not_fetched[key] += 1
        elif company.origin == "generated" and company.board in dropped:
            dropped_skipped[company.board] += 1
        elif company.origin == "generated" and (company.name, company.board) in only_outside:
            outside_skipped += 1
        elif company.origin == "generated" and company.board == "workday" and company.name in local_words:
            kept_companies.append(replace(company, local_search=local_words[company.name]))
        else:
            kept_companies.append(company)
    companies = kept_companies
    if dropped:
        rules = replace(rules, paused_boards=rules.paused_boards | frozenset(dropped))

    generated_names = {c.name for c in companies if c.origin == "generated"}
    backlog_names = generated_names | {c.name for c in companies if c.origin == "wishlist"}
    company_parks = {c.name: set(c.park_countries) for c in companies if c.park_countries}
    seen_companies = {(r["company"], r["source"]) for r in state.jobs.values()}

    details_lock = threading.Lock()
    details_fetched = Counter()

    def want_details(job: Job) -> bool:
        """Fetch a detail page only for a role that could be scored and has no posting text yet.
        Roles the backlog rule skipped, the title check dropped or scoring gave up on never will be:
        fetching their text at every daily fetch once took 14,530 requests (2026-10-25)."""
        known = state.jobs.get(job.key)
        if known and known.get("source") == "custom" and known.get("description_hash") \
                and known["title"] == custom_source.title_from_url(known["url"]) and not known["closed"]:
            pass  # its title was overwritten from the URL (fixed 2026-10-27): read the page once more
        elif known and (known.get("description_hash") or known.get("backfill_skip")
                      or (known.get("titlecheck") or {}).get("dropped")
                      or given_up(state, known, models(settings)[0]) or given_up(state, known, models(settings)[1])):
            return False
        if not prefilter.check(job.title, job.locations, allowed).passed:
            return False
        if job.company in generated_names and not rules.score_parked and rules.parks(job.title, job.locations):
            return False
        if job.company in company_parks and prefilter.parked(job.locations, company_parks[job.company]):
            return False  # a company's `park_countries`: stored, not scored
        if job.company in generated_names and not prefilter.entry_terms(job.title) and (
                rules.needs_entry_terms(job.locations)
                or (rules.guard_filter and (job.company, job.source) in seen_companies)):
            return False  # not scored: 83% of the texts fetched were these (2026-10-29)
        if known and is_generated(known) and needs_title_check(known, rules):
            return False  # its title check comes first; the text follows at a later fetch if it passes
        if job.company in backlog_names and (job.company, job.source) not in seen_companies and not backlog_eligible(
                job.title, job.posted or job.posted_before, now_dt.date(), rules, job.locations,
                posted_before=not job.posted and bool(job.posted_before)):
            return False  # first fetch: the backlog rule leaves it unscored
        with details_lock:
            if options.max_details is not None and details_fetched["details"] >= options.max_details:
                details_fetched["deferred"] += 1
                return False  # this run's share is used: a later fetch of the company gets it
            details_fetched["details"] += 1
        return True

    deadline = time.monotonic() + options.fetch_seconds if options.fetch_seconds else None
    totals: Counter[str] = Counter()
    sources: dict[str, Any] = {}
    generated_totals: dict[str, Counter[str]] = defaultdict(Counter)
    generated_errors: dict[str, str] = {}
    index = company_index(state)

    def account(result: Fetched, counts: dict[str, int]) -> None:
        totals.update(counts)
        if result.company.origin == "generated":  # thousands of companies: summarised per board
            board = generated_totals[result.company.board]
            board["companies"] += 1
            board["seconds"] += result.seconds
            board.update(counts)
            if result.error:
                board["errors"] += 1
                generated_errors[result.company.identity or result.company.name] = result.error
            if result.company.extra.get("rules") != "generated":
                return  # a harvested company; yours with the generated rules are recorded by name too
        entry = {"board": result.company.board, "seconds": round(result.seconds, 1)}
        if result.company.origin == "generated":
            entry["rules"] = "generated"  # also in the board's totals under "generated"
        if result.error:
            entry["error"] = result.error
        entry.update(counts)
        sources[result.company.name] = entry

    # Each company is taken into the state as soon as it is fetched; with score_while_fetching,
    # scoring whose turn is certain starts then too (overlap.py).
    arrived: queue.Queue[Fetched] = queue.Queue()
    fetched: list[Fetched] = []
    failure: list[BaseException] = []

    def fetch_everything() -> None:
        try:
            fetched.extend(fetch_all(companies, make_http, want_details, int(fetching.get("custom_max_links", 50)),
                                     int(fetching.get("max_parallel_hosts", 8)), progress, deadline=deadline,
                                     on_fetched=arrived.put))
        except BaseException as e:  # noqa: BLE001 (re-raised in the main thread)
            failure.append(e)

    norm_of = {c.name: prefilter.normalise_company(c.name) for c in companies}
    left = Counter(norm_of.values())
    by_norm: dict[str, list[str]] = defaultdict(list)
    for key, record in state.jobs.items():
        if prefilter.normalise_company(record["company"]) in left:
            by_norm[prefilter.normalise_company(record["company"])].append(key)
    guard = scoring.UsageGuard.from_settings(settings, "backlog" if options.backlog or options.sonnet_only
                                             else "scheduled")
    if options.fetch_seconds:  # a scheduled run: no call starts after this, so it ends within the Actions limit
        guard.deadline = started + float(settings.get("scoring", {}).get("scheduled_max_run_seconds", 840))
    boost = None
    if options.budget is not None:
        guard.max_share = options.budget
    elif not (options.backlog or options.sonnet_only):
        # A large queue (2026-10-28; every waiting role since 2026-11-18): a scheduled run may use
        # more of a window until it is below the threshold; the weekly guard and Claude's own warnings
        # still stop it.
        usage = settings.get("usage", {})
        above, share = usage.get("scheduled_backlog_above"), float(usage.get("scheduled_backlog_share", 0))
        if above is not None and share > guard.max_share:
            remaining = waiting_left(state, settings)
            if remaining > int(above):
                guard.max_share = share
                boost = {"waiting_left": remaining, "share": share}
    if boost:
        # More calls at once and more per phase while the backlog is large (2026-10-31): the 14-minute
        # limit, not the budget, was ending scheduled runs, and Haiku scored its 100 during the fetch
        # in under 5 minutes, then waited. A --parallel or --cap given for the run still wins.
        scoring_settings = settings.get("scoring", {})
        options = replace(
            options,
            parallel=options.parallel or int(scoring_settings.get("parallel_backlog", scoring_settings.get("parallel", 4))),
            cap=options.cap if options.cap is not None else int(scoring_settings.get(
                "max_calls_per_run_backlog", scoring_settings.get("max_calls_per_run", 100))))
        boost.update(parallel=options.parallel, cap=options.cap)
    guard.ignore_weekly_warning = options.ignore_weekly_warning
    early = None
    if options.score and settings.get("scoring", {}).get("score_while_fetching", True) and companies:
        from .overlap import Overlap
        mine = {norm_of[c.name] for c in companies if c.origin != "generated"}
        early = Overlap(config, settings, state, options, runner, rules, guard, set(left), mine)
    pre_phases: dict[str, Any] = {}
    if options.score and not options.sonnet_only and not guard.stopped:
        # The title check of stored roles at the companies this run fetches, before the fetch, so
        # their texts are fetched only if they pass.
        due = {c.name for c in companies}
        pre_select = lambda r: r["company"] in due and needs_title_check(r, rules)  # noqa: E731
        if any(pre_select(r) for r in state.jobs.values()):
            pre_phases["before fetch: titlecheck"] = titlecheck_pending(
                state, config, settings, pre_select,
                options.parallel or int(settings.get("scoring", {}).get("parallel", 4)), runner, guard, progress)
    if boost and options.max_details is not None:
        options = replace(options, max_details=max(options.max_details, int(
            settings.get("fetching", {}).get("scheduled_max_details_backlog", options.max_details))))
    worker = threading.Thread(target=fetch_everything, name="fetch", daemon=True)
    worker.start()
    while worker.is_alive() or not arrived.empty():
        try:
            result = arrived.get(timeout=0.2)
        except queue.Empty:
            result = None
        if result is not None:
            counts = {} if result.error else update_jobs(state, result, allowed, now, rules, index)
            account(result, counts)
            norm = norm_of[result.company.name]
            by_norm[norm].extend(j.key for j in result.jobs if j.key in state.jobs)
            left[norm] -= 1
            if left[norm] == 0 and early is not None:  # every board of this company is in: merge, then score
                group = [state.jobs[k] for k in dict.fromkeys(by_norm[norm]) if k in state.jobs]
                mark_duplicates(state, group)
                early.ready(norm, group)
        if early is not None:
            early.step()
    worker.join()
    if failure:
        raise failure[0]
    fetch_seconds = time.monotonic() - started
    early_phases = early.finish() if early is not None else {}
    progress.phase("updating the state")

    duplicates = mark_duplicates(state)

    summary: dict[str, Any] = {
        "started_at": now, "companies": len(companies), "errors": sum(1 for f in fetched if f.error),
        "fetch_seconds": round(fetch_seconds, 1), "jobs": dict(totals), "duplicates": duplicates,
        "sources": sources, "weekly_guard": guard_state,
        "details_fetched": details_fetched["details"],
        "ran_on": "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "laptop",
        "code": code_version(config), "account": claude_account,
    }
    if os.environ.get("JOBHUNT_LAUNCHD"):
        summary["launchd"] = True  # the laptop's scheduled run (launchd.py)
    if os.environ.get("JOBHUNT_ACTIONS_PROJECTED"):  # the workflow's Actions minutes guard (run.yml)
        summary["actions_minutes"] = {"used": int(os.environ.get("JOBHUNT_ACTIONS_USED") or 0),
                                      "projected": int(os.environ["JOBHUNT_ACTIONS_PROJECTED"]),
                                      "slow": os.environ.get("JOBHUNT_ACTIONS_SLOW") == "true"}
    if options.kind:
        summary["kind"] = options.kind
    if nearly_full:
        summary["week_nearly_full"] = (f"{nearly_full['message']}: only your companies and the wishlist were "
                                       "scored; the generated list waits until the week clears")
    if details_fetched["deferred"]:
        summary["details_deferred"] = details_fetched["deferred"]
    if len(fetched) < len(companies):
        summary["fetch_not_started"] = len(companies) - len(fetched)
    if policy_fixes:
        summary["score_policy_fixes"] = policy_fixes
    if boost:
        summary["backlog_boost"] = boost
    if generated_totals:
        summary["generated"] = {b: {k: (round(v, 1) if k == "seconds" else v) for k, v in c.items()}
                                for b, c in generated_totals.items()}
        summary["generated_errors"] = generated_errors
    if options.score:
        scoring_started = time.monotonic()
        # Again after the fetch: a company's first fetch can take it past the threshold.
        rules = replace(rules, cascade_mine=cascade_companies(state, settings, now_dt))
        summary["scoring"] = {**pre_phases, **early_phases,
                              **score_all(config, settings, state, options, runner, rules, progress, guard)}
        if rules.cascade_mine:
            summary["cascade_companies"] = sorted(rules.cascade_mine)
        if early is not None:
            # What the same run would have taken without overlap: the scoring done while fetching
            # would have come after the fetch instead.
            after = time.monotonic() - scoring_started
            summary["overlap"] = {"scored_while_fetching": sum(p.get("calls", 0) for p in early_phases.values()),
                                  "busy_seconds_while_fetching": round(early.busy_seconds, 1),
                                  "scoring_seconds_after_fetch": round(after, 1)}
    if outside_skipped:
        summary["only_uk_us_not_fetched"] = outside_skipped
    waits_after = hostwaits.waiting()
    reported = set(waits_now) | set(WAITS.new)
    if reported:
        summary["host_waits"] = {
            host: {"until": (waits_after.get(host) or waits_now.get(host)).isoformat(timespec="seconds"),
                   "companies_not_fetched": not_fetched.get(host, 0), "new": host in WAITS.new}
            for host in sorted(reported) if waits_after.get(host) or waits_now.get(host)}
    dropped_now = hostwaits.dropped_boards()
    if dropped_now:
        summary["boards_dropped"] = {board: {"since": r.get("asked_at"), "companies_not_fetched": dropped_skipped.get(board, 0)}
                                     for board, r in dropped_now.items()}
    from . import health

    problems = health.check(state.runs(), summary)  # a silent failure must not last (2026-11-18)
    if problems:
        summary["health"] = problems
    if options.alerts:
        from . import alerts

        progress.phase("alerts")
        summary["alerts"] = alerts.send(state, settings, since=now, now=datetime.now(UTC).isoformat(timespec="seconds"))
        for what, title, text in health.alerts_due(state.runs(), summary, datetime.fromisoformat(now)):
            if alerts.notify(settings, title, text):
                summary["health"].setdefault("alerted", []).append(what)
        untold = [b for b, r in dropped_now.items() if not r.get("notified")]
        if untold:
            names = ", ".join(BOARD_NAMES.get(b, b) for b in untold)
            if alerts.notify(settings, f"{names} taken off the generated list",
                             f"{names} refused again at the slower pace (a long Retry-After), so its companies on the "
                             "generated list are no longer fetched. Your companies and wishlist are not affected."):
                hostwaits.mark_notified(untold)
    hostwaits.save(state.root)
    if guard.seven_day:
        summary["seven_day"] = guard.seven_day
    if guard.weekly_warning_ignored is not None:
        used = guard.weekly_warning_ignored
        summary["weekly_warning_ignored"] = (f"Claude warned the weekly limit is {used:.0%} used; scoring went on "
                                             "(--ignore-weekly-warning)" if isinstance(used, float) else
                                             "Claude warned the weekly limit is nearly used; scoring went on "
                                             "(--ignore-weekly-warning)")
    if guard.weekly_warnings_below is not None:
        summary["weekly_warning_below"] = (f"Claude warned a weekly limit is {guard.weekly_warnings_below:.0%} used; "
                                           f"scoring went on (runs stop from {guard.weekly_stop_at:.0%})")
    if interrupt.stopping():
        summary["interrupted"] = "stopped by Ctrl+C: calls in progress finished, the rest waits for the next run"
    summary["seconds"] = round(time.monotonic() - started, 1)
    if "overlap" in summary:
        summary["overlap"]["seconds_without_overlap"] = round(
            summary["seconds"] + summary["overlap"]["busy_seconds_while_fetching"], 1)
    progress.phase("saving")
    state.save()
    progress.done()
    state.append_run(summary)
    return summary
