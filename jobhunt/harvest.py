"""`harvest`: the generated company list, built from the Common Crawl URL index (section 5.2).

1. Collect: query the index (CDX API) for each board's URL patterns over the latest crawls and
   extract company slugs. Index pages are cached on disk, so an interrupted harvest resumes.
2. Check: fetch each new slug's listing from the live API (light listings, no retries, polite
   per host). Results are appended to a log as they come, so a rerun skips what was checked.
3. Keep companies with at least one open role whose location passes the pre-filter, and save
   them to companies_generated.jsonl in the state folder.
4. Report per board: slugs found, answering, relevant, and the role volumes later steps are
   sized on. Boards without an adapter report slugs found only.

Nothing here scores anything.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

from . import hostwaits, prefilter
from .companies import Company
from .pipeline import host_of
from .sources import ADAPTERS, Http, SourceError, fetch, no_details, workday
from .progress import Silent
from .sources.http import WAITS, rate_key
from .state import read_jsonl, write_jsonl

INDEX = "https://index.commoncrawl.org"
NOT_SLUGS = {"www", "app", "api", "help", "support", "status", "cdn", "static", "assets", "blog", "docs", "careers",
             "jobs", "job", "embed", "v1", "login", "signup", "signin", "admin", "mail", "email", "info", "go",
             "about", "privacy", "terms", "favicon.ico", "robots.txt", "sitemap.xml", "en", "de", "fr", "nl",
             "search", "home", "demo", "test", "staging", "sandbox", "partners", "developers", "community"}
RECENT_DAYS = 28  # the window the harvest report counts recent roles over
CHECK_RETRIES = 2


def temporary_error(result: dict[str, Any]) -> bool:
    """A failed check worth repeating: rate limits, server errors, timeouts, connection problems.
    Not 404 (no such board) or other client errors."""
    error = result.get("error") or ""
    if not error:
        return False
    match = re.search(r"HTTP (\d{3})", error)
    return not match or match.group(1) == "429" or match.group(1).startswith("5")


@dataclass(frozen=True)
class Board:
    name: str
    queries: tuple[str, ...]  # CDX url patterns
    pattern: str  # regex on a captured URL; named groups slug, url, region
    supported: bool = False  # an adapter exists, so slugs can be checked live

    def identify(self, url: str) -> dict[str, str] | None:
        match = re.match(self.pattern, url, re.IGNORECASE)
        if not match:
            return None
        groups = {k: v for k, v in match.groupdict().items() if v}
        slug = groups.get("slug", "")
        if "slug" in match.groupdict() and (not slug or slug.lower() in NOT_SLUGS):
            return None
        config = {"board": self.name}
        if slug:
            config["slug"] = slug if self.name == "ashby" else slug.lower()  # Ashby slugs are case-sensitive
        if groups.get("url"):
            try:
                config["url"] = normalise_url(self.name, groups["url"])
            except SourceError:
                return None
        if groups.get("region"):
            config["region"] = "eu"
        return config


BOARDS = [
    Board("greenhouse", ("job-boards.greenhouse.io/*", "job-boards.eu.greenhouse.io/*", "boards.greenhouse.io/*"),
          r"^https?://(?:job-boards(?:\.eu)?|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?(?P<slug>[A-Za-z0-9_-]+)",
          supported=True),
    Board("lever", ("jobs.lever.co/*", "jobs.eu.lever.co/*"),
          r"^https?://jobs\.(?P<region>eu\.)?lever\.co/(?P<slug>[A-Za-z0-9_.-]+)", supported=True),
    Board("ashby", ("jobs.ashbyhq.com/*",), r"^https?://jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9_.%-]+)", supported=True),
    Board("workday", ("*.myworkdayjobs.com",),
          r"^(?P<url>https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday/)[A-Za-z0-9_-]+)",
          supported=True),
    Board("recruitee", ("*.recruitee.com",), r"^https?://(?P<slug>[a-z0-9-]+)\.recruitee\.com", supported=True),
    Board("smartrecruiters", ("jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"),
          r"^https?://(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[A-Za-z0-9_-]+)", supported=True),
    Board("personio", ("*.jobs.personio.de", "*.jobs.personio.com"),
          r"^https?://(?P<slug>[a-z0-9-]+)\.jobs\.personio\.(?:de|com)", supported=True),
    Board("workable", ("apply.workable.com/*",), r"^https?://apply\.workable\.com/(?P<slug>[a-z0-9_-]+)", supported=True),
    Board("teamtailor", ("*.teamtailor.com",), r"^https?://(?P<slug>[a-z0-9-]+)\.teamtailor\.com", supported=True),
    Board("homerun", ("*.homerun.co",),
          r"^https?://(?!(?:help|www|feed|api|cdn|app|404|status|blog)\.)(?P<slug>[a-z0-9-]+)\.homerun\.co", supported=True),
    Board("join", ("join.com/companies/*",), r"^https?://join\.com/companies/(?P<slug>[a-z0-9_-]+)", supported=True),
    Board("softgarden", ("*.softgarden.io",), r"^https?://(?P<slug>[a-z0-9-]+)\.softgarden\.io"),
    Board("jobylon", ("emp.jobylon.com/companies/*",), r"^https?://emp\.jobylon\.com/companies/(?P<slug>[^/?#]+)"),
    Board("pinpoint", ("*.pinpointhq.com",), r"^https?://(?P<slug>[a-z0-9-]+)\.pinpointhq\.com"),
    Board("factorial", ("*.factorialhr.com",), r"^https?://(?P<slug>[a-z0-9-]+)\.factorialhr\.com"),
    Board("rippling", ("ats.rippling.com/*",), r"^https?://ats\.rippling\.com/(?P<slug>[a-z0-9_-]+)"),
    Board("bamboohr", ("*.bamboohr.com",), r"^https?://(?P<slug>[a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)"),
    Board("breezy", ("*.breezy.hr",), r"^https?://(?P<slug>[a-z0-9-]+)\.breezy\.hr"),
    Board("jazzhr", ("*.applytojob.com",), r"^https?://(?P<slug>[a-z0-9-]+)\.applytojob\.com"),
    Board("jobvite", ("jobs.jobvite.com/*",), r"^https?://jobs\.jobvite\.com/(?P<slug>[a-z0-9_-]+)"),
    Board("gem", ("jobs.gem.com/*",), r"^https?://jobs\.gem\.com/(?P<slug>[a-z0-9_-]+)"),
]
BOARDS_BY_NAME = {b.name: b for b in BOARDS}
assert all(b.name in ADAPTERS for b in BOARDS if b.supported)


def normalise_url(board: str, url: str) -> str:
    """Workday careers URLs come with and without a locale segment: keep host and site only."""
    if board == "workday":
        site = workday.Site.from_url(url.replace("http://", "https://"))
        return site.public
    return url.rstrip("/")


def identity(config: dict[str, Any]) -> str:
    """One key per board account: greenhouse:stripe, lever-eu:acme, workday:https://acme.wd1..."""
    board = config["board"] + ("-eu" if config.get("region") else "")
    account = config.get("url") or config.get("slug") or ""
    return f"{board}:{account if config['board'] == 'ashby' else account.lower()}"


# --- 1. Collect ------------------------------------------------------------------------------

def latest_crawls(http: Http, count: int) -> list[str]:
    return [c["id"] for c in http.get_json(f"{INDEX}/collinfo.json")[:count]]


class IndexCache:
    """Index pages on disk: <root>/<crawl>/<query>/<page>.txt, one captured URL per line."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, crawl: str, query: str, page: int) -> Path:
        return self.root / crawl / re.sub(r"[^A-Za-z0-9.]+", "_", query) / f"{page}.txt"

    def pages_path(self, crawl: str, query: str) -> Path:
        return self.path(crawl, query, 0).with_name("pages.txt")


def collect(http: Http, crawls: list[str], boards: list[Board], cache: IndexCache,
            log: Callable[[str], None] = print, on_page: Callable[[], None] | None = None,
            ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Every board account seen in the index, by identity, and the index pages that failed."""
    found: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for crawl in crawls:
        for board in boards:
            for query in board.queries:
                for urls in _query_pages(http, crawl, query, cache, failed, log):
                    if on_page:
                        on_page()
                    for url in urls:
                        config = board.identify(url)
                        if config:
                            found.setdefault(identity(config), config)
    return found, failed


def _query_pages(http: Http, crawl: str, query: str, cache: IndexCache, failed: list[str],
                 log: Callable[[str], None]) -> Iterable[list[str]]:
    base = f"{INDEX}/{crawl}-index?url={quote(query, safe='*/')}&output=json"
    pages_file = cache.pages_path(crawl, query)
    if pages_file.exists():
        pages = int(pages_file.read_text())
    else:
        try:
            pages = int(http.get_json(base + "&showNumPages=true")["pages"])
        except (SourceError, KeyError, TypeError, ValueError) as e:
            failed.append(f"{crawl} {query}: page count: {e}")
            return
        pages_file.parent.mkdir(parents=True, exist_ok=True)
        pages_file.write_text(str(pages))
    for page in range(pages):
        path = cache.path(crawl, query, page)
        if not path.exists():
            try:
                body = http.get(base + f"&fl=url&page={page}").text
            except SourceError as e:
                failed.append(f"{crawl} {query} page {page}: {e}")
                continue
            urls = []
            for line in body.splitlines():
                try:
                    urls.append(json.loads(line)["url"])
                except (ValueError, KeyError, TypeError):
                    continue
            path.write_text("\n".join(urls), encoding="utf-8")
            log(f"  {crawl} {query} page {page + 1}/{pages}: {len(urls)} URLs")
        yield [u for u in path.read_text(encoding="utf-8").splitlines() if u]


# --- 2. Check ---------------------------------------------------------------------------------

DEFAULT_PARK = frozenset({"GB", "US"})


def check_one(config: dict[str, Any], http: Http, allowed: set[str], today: date,
              park: frozenset[str] = DEFAULT_PARK) -> dict[str, Any]:
    """Fetch one board account's light listing and count its roles. `outside_park_pass`: roles
    passing the pre-filter with a location outside the parked countries (UK, US) or unclear."""
    company = Company.from_dict({"name": config.get("slug") or config.get("url"), **config})
    result: dict[str, Any] = {"identity": identity(config), "config": config, "checked_at": today.isoformat()}
    try:
        jobs = fetch(company, http, details=no_details, light=True)
    except SourceError as e:
        return {**result, "error": str(e)[:200]}
    recent_since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    counts: Counter[str] = Counter(open=len(jobs), outside_park_pass=0)
    for job in jobs:
        location_ok = prefilter.check_locations(job.locations, allowed).passed
        counts["location_pass"] += location_ok
        if location_ok and prefilter.check_title(job.title).passed:
            counts["prefilter_pass"] += 1
            counts["outside_park_pass"] += not prefilter.parked(job.locations, set(park))
            if job.posted is None:
                counts["prefilter_pass_undated"] += 1
            elif job.posted >= recent_since:
                counts["prefilter_pass_recent"] += 1
    name = next((j.board_company for j in jobs if j.board_company), None)
    return {**result, "name": name, **counts}


def check_all(configs: list[dict[str, Any]], make_http: Callable[[], Http], allowed: set[str], today: date,
              on_result: Callable[[dict[str, Any]], None], max_hosts: int = 8,
              park: frozenset[str] = DEFAULT_PARK) -> dict[str, tuple[datetime, int]]:
    """One worker per host (the polite delay holds per host), results reported as they come.
    Accounts on a host that asked to wait are not checked (nor recorded): they stay unchecked for
    a later harvest. Returns those hosts with the time the wait ends and the accounts left."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for config in configs:
        groups[host_of(Company.from_dict({"name": "x", **config}))].append(config)
    waited: dict[str, tuple[datetime, int]] = {}
    lock = threading.Lock()

    def work(group: list[dict[str, Any]]) -> None:
        # Retries cover rate limits and server errors only; a 404 (no such board) fails at once.
        http = make_http().with_retries(CHECK_RETRIES)
        key = rate_key(host_of(Company.from_dict({"name": "x", **group[0]})))
        for i, config in enumerate(group):
            until = WAITS.until(key)
            if until:
                with lock:
                    waited[key] = (until, len(group) - i)
                return
            result = check_one(config, http, allowed, today, park)
            if WAITS.until(key) and "asked to wait" in str(result.get("error", "")):
                with lock:
                    waited[key] = (WAITS.until(key), len(group) - i)
                return  # the account that met the refusal stays unchecked too
            on_result(result)

    with ThreadPoolExecutor(max_workers=max(1, max_hosts)) as pool:
        list(pool.map(work, groups.values()))
    return waited


# --- 3 and 4. Keep, save, report --------------------------------------------------------------

def configured_identities(companies: list[Company]) -> set[str]:
    ids = set()
    for c in companies:
        if c.board == "custom" and c.listing_url:  # as the wishlist names a careers site (2026-10-30)
            ids.add(f"custom:{c.listing_url}")
        elif c.board and (c.slug or c.url):
            config = {"board": c.board, "slug": c.slug,
                      "url": normalise_url(c.board, c.url) if c.url else None, "region": c.region}
            ids.add(identity({k: v for k, v in config.items() if v}))
    return ids


def generated_list(checked: dict[str, dict[str, Any]], previous: list[dict[str, Any]],
                   today: date) -> list[dict[str, Any]]:
    """Relevant companies from this harvest. A `learned` company (added by the learning loop) is
    kept while it still answers; crawl companies that stopped answering drop out."""
    before = {r["identity"]: r for r in previous}
    out = {}
    for key, result in checked.items():
        if result.get("error") or not result.get("location_pass"):
            continue
        old = before.get(key, {})
        out[key] = {
            "identity": key, **result["config"], "name": result.get("name") or old.get("name"),
            "origin": old.get("origin", "crawl"), "first_seen": old.get("first_seen", today.isoformat()),
            "verified": result["checked_at"], "open_roles": result.get("open", 0),
            "location_pass": result.get("location_pass", 0), "prefilter_pass": result.get("prefilter_pass", 0),
            "prefilter_pass_recent": result.get("prefilter_pass_recent", 0),
            "prefilter_pass_undated": result.get("prefilter_pass_undated", 0),
            # None for checks made before 2026-10-27 (unknown): runs then judge by the roles they stored.
            "outside_park_pass": result.get("outside_park_pass"),
            "last_relevant": result["checked_at"] if result.get("prefilter_pass") else old.get("last_relevant"),
        }
    for key, old in before.items():
        if old.get("origin") == "learned" and key not in out and not (checked.get(key) or {}).get("error"):
            out[key] = old
    return [out[k] for k in sorted(out)]


def report(found: dict[str, dict[str, Any]], configured: set[str], checked: dict[str, dict[str, Any]],
           generated: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    per: dict[str, dict[str, Any]] = {}
    for board in BOARDS:
        ids = [k for k, c in found.items() if c["board"] == board.name]
        row: dict[str, Any] = {"supported": board.supported, "found": len(ids)}
        if board.supported:
            new = [k for k in ids if k not in configured]
            results = [checked[k] for k in new if k in checked]
            answering = [r for r in results if not r.get("error") and r.get("open")]
            relevant = [g for g in generated if g["board"] == board.name]
            row.update(already_configured=len(ids) - len(new), checked=len(results), answering=len(answering),
                       relevant=len(relevant), open_roles=sum(g["open_roles"] for g in relevant),
                       prefilter_pass=sum(g["prefilter_pass"] for g in relevant),
                       prefilter_pass_recent=sum(g["prefilter_pass_recent"] for g in relevant),
                       prefilter_pass_undated=sum(g["prefilter_pass_undated"] for g in relevant))
        per[board.name] = row
    return per


# A harvest replaces the generated list with the companies it found again. On 2026-11-02 Common
# Crawl's index answered 504 for every board, so it found none and wrote the list empty: runs
# fetched 44 companies instead of about 750 for 16 days. Now such a list is not written.
MAX_SHRINK = 0.10


def refusal(previous: list[dict[str, Any]], generated: list[dict[str, Any]], failed: list[str]) -> str | None:
    """Why a harvest's new list must not replace the old one, or None when it may."""
    if failed:
        return f"{len(failed)} Common Crawl index page{'s' if len(failed) != 1 else ''} failed, so companies would go missing"
    if previous and len(generated) < (1 - MAX_SHRINK) * len(previous):
        return f"the list would shrink from {len(previous):,} to {len(generated):,} companies (more than {MAX_SHRINK:.0%})"
    return None


# --- The whole harvest ------------------------------------------------------------------------

@dataclass
class Harvest:
    state_root: Path
    allowed: set[str]
    configured: set[str]
    make_http: Callable[[], Http]
    index_http: Http
    log: Callable[[str], None] = print
    progress: Any = None  # a progress.Progress; None prints nothing extra
    recheck_days: int = 25
    recheck_errors: bool = False  # repeat checks that failed for a temporary reason
    skip_boards: frozenset[str] = frozenset()  # found and counted, but not checked live (e.g. Workday)
    only_boards: frozenset[str] = frozenset()  # when set, only these boards are checked live
    park: frozenset[str] = DEFAULT_PARK  # the generated list's parked countries ([generated] park_countries)
    limit_per_board: int | None = None  # for a trial run
    boards: list[Board] = field(default_factory=lambda: list(BOARDS))

    @property
    def folder(self) -> Path:
        return self.state_root / "harvest"

    def load_checked(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in read_jsonl(self.folder / "checked.jsonl"):
            latest[record["identity"]] = record  # later lines win
        return latest

    def run(self, crawls: list[str], today: date | None = None) -> dict[str, Any]:
        today = today or datetime.now(UTC).date()
        started = time.monotonic()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log(f"Collecting from {', '.join(crawls)}")
        progress = self.progress or Silent()
        progress.phase("reading the Common Crawl index (pages)")
        found, failed = collect(self.index_http, crawls, self.boards, IndexCache(self.folder / "index"), self.log,
                                on_page=progress.advance)
        self.log(f"Found {len(found)} board accounts; {len(failed)} index pages failed (rerun to retry them).")

        checked = self.load_checked()
        # Hosts that asked to wait are left alone; boards that refused again on their longer
        # delay are off the generated list and not checked (hostwaits.py).
        hostwaits.load(self.state_root)
        dropped = hostwaits.dropped_boards()
        skip = self.skip_boards | frozenset(dropped)
        fresh_since = (today - timedelta(days=self.recheck_days)).isoformat()
        to_check: list[dict[str, Any]] = []
        per_board: Counter[str] = Counter()
        for key, config in sorted(found.items()):
            board = BOARDS_BY_NAME[config["board"]]
            if not board.supported or key in self.configured or board.name in skip:
                continue
            if self.only_boards and board.name not in self.only_boards:
                continue
            if key in checked and checked[key]["checked_at"] >= fresh_since and not (
                    self.recheck_errors and temporary_error(checked[key])):
                continue
            if self.limit_per_board is not None and per_board[board.name] >= self.limit_per_board:
                continue
            per_board[board.name] += 1
            to_check.append(config)
        self.log(f"Checking {len(to_check)} accounts live ({len(checked)} already checked recently)...")
        progress.phase("checking accounts live", total=len(to_check))

        log_path = self.folder / "checked.jsonl"
        done = 0
        with log_path.open("a", encoding="utf-8") as out:
            def on_result(result: dict[str, Any]) -> None:
                nonlocal done
                out.write(json.dumps(result, sort_keys=True) + "\n")
                out.flush()
                checked[result["identity"]] = result
                done += 1
                progress.advance()
                if done % 1000 == 0 and not progress.live:
                    self.log(f"  checked {done}/{len(to_check)}")
            waited = check_all(to_check, self.make_http, self.allowed, today, on_result, park=self.park)
        hostwaits.save(self.state_root)

        generated_path = self.state_root / "companies_generated.jsonl"
        previous = read_jsonl(generated_path)
        generated = generated_list({k: v for k, v in checked.items() if k in found}, previous, today)
        progress.done()
        refused = refusal(previous, generated, failed)
        if refused:
            self.log(f"Not written: {refused}. The list stays as it was ({len(previous)} companies).")
        else:
            write_jsonl(generated_path, generated)
        summary = {"harvested_at": datetime.now(UTC).isoformat(timespec="seconds"), "crawls": crawls,
                   "accounts_found": len(found), "index_pages_failed": failed, "checked_now": len(to_check),
                   "relevant": len(previous if refused else generated), "seconds": round(time.monotonic() - started, 1),
                   "boards": report(found, self.configured, checked, generated)}
        if refused:
            summary["refused"] = refused
        if waited:
            summary["host_waits"] = {host: {"until": until.isoformat(timespec="seconds"), "not_checked": count,
                                            "new": host in WAITS.new} for host, (until, count) in waited.items()}
        if dropped:
            summary["boards_dropped"] = sorted(dropped)
        with (self.state_root / "harvest.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, sort_keys=True) + "\n")
        return summary


# --- Sampled re-check: role-level volumes for the sizing decisions ---------------------------

def sample_accounts(generated: list[dict[str, Any]], per_board: int, seed: int = 42,
                    boards: set[str] | None = None) -> list[dict[str, Any]]:
    import random

    rng = random.Random(seed)
    by_board: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in generated:
        if boards is None or entry["board"] in boards:
            by_board[entry["board"]].append(entry)
    chosen = []
    for board in sorted(by_board):
        entries = sorted(by_board[board], key=lambda e: e["identity"])
        chosen += rng.sample(entries, min(per_board, len(entries)))
    return chosen


def sample_one(entry: dict[str, Any], http: Http, allowed: set[str], park: set[str], today: date) -> dict[str, Any]:
    config = {k: entry[k] for k in ("board", "slug", "url", "region") if entry.get(k)}
    company = Company.from_dict({"name": entry.get("name") or config.get("slug") or config.get("url"), **config})
    started = time.monotonic()
    account: dict[str, Any] = {"identity": entry["identity"], "board": entry["board"]}
    try:
        jobs = fetch(company, http, details=no_details, light=True)
    except SourceError as e:
        return {**account, "error": str(e)[:200], "seconds": round(time.monotonic() - started, 2), "roles": []}
    roles = []
    for job in jobs:
        title_ok = prefilter.check_title(job.title).passed
        location_ok = prefilter.check_locations(job.locations, allowed).passed
        age = (today - date.fromisoformat(job.posted)).days if job.posted else None
        roles.append({"title": job.title, "department": job.department, "locations": job.locations,
                      "age": age, "pass": title_ok and location_ok,
                      "group": prefilter.country_group(job.locations, allowed),
                      "parked": prefilter.parked(job.locations, park), "terms": prefilter.entry_terms(job.title)})
    return {**account, "name": company.name, "seconds": round(time.monotonic() - started, 2), "roles": roles}


def sample(state_root: Path, entries: list[dict[str, Any]], make_http: Callable[[], Http], allowed: set[str],
           park: set[str], today: date, log: Callable[[str], None] = print, progress: Any = None) -> Path:
    """Fetch the sampled accounts' light listings (one worker per host) into a JSON Lines file."""
    boards = sorted({e["board"] for e in entries})
    suffix = f"-{'-'.join(boards)}" if len(boards) == 1 else ""
    out_path = state_root / "harvest" / f"sample-{today.isoformat()}{suffix}.jsonl"
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        groups[host_of(Company.from_dict({"name": "x", **{k: entry[k] for k in ("board", "slug", "url", "region")
                                                           if entry.get(k)}}))].append(entry)
    done = 0
    progress = progress or Silent()
    progress.phase("sampling accounts", total=len(entries))
    with out_path.open("w", encoding="utf-8") as out:
        def work(group: list[dict[str, Any]]) -> None:
            nonlocal done
            http = make_http().with_retries(CHECK_RETRIES)
            for entry in group:
                record = sample_one(entry, http, allowed, park, today)
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                done += 1
                progress.advance()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, groups.values()))
    progress.done()
    return out_path
