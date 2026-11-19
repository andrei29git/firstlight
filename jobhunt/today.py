"""The Today page: what needs the user now, each item linking to where they act on it.

Roles and applications come from the catalogue and the tracker database (built in the web app);
this module adds the tool's own tasks: runs missing or failing, the external trigger not
arriving, its token expiring, a Workable harvest batch due, reminders from settings.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import accounts, hostwaits, pipeline, runs_view
from .sources.http import WAITS
from .state import read_jsonl

TRIGGER_LATE = timedelta(hours=3)  # the external trigger runs every 2 hours
LAPTOP_LATE = timedelta(hours=36)  # the launchd job runs twice a day
TOKEN_WARN_DAYS = 14
BATCH_EVERY = timedelta(hours=20)  # one Workable harvest batch a day
CRAWLS = ("CC-MAIN-2026-43", "CC-MAIN-2026-39", "CC-MAIN-2026-34")
TOKENS_PAGE = "https://github.com/settings/personal-access-tokens"
CRON_JOBS = "https://console.cron-job.org/jobs"


@dataclass
class Item:
    title: str
    detail: str = ""
    href: str = ""
    tone: str = "blue"  # blue, orange, red or gray
    command: str = ""  # a command to run on the laptop, shown to copy
    external: bool = False


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)
    more_href: str = ""
    more_label: str = ""


def when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%-d %b %H:%M UTC")


def tool_items(settings: dict[str, Any], state_dir: Path, runs: list[dict[str, Any]], workflow: str,
               minutes: dict[str, Any] | None, status: dict[str, Any] | None, now: datetime,
               today: date | None = None, local_companies: list[str] | None = None) -> list[Item]:
    today = today or now.date()
    items: list[Item] = []
    schedule = settings.get("schedule", {})
    external = schedule.get("external_trigger")

    health = runs_view.health(runs, workflow, now, external=external,
                              external_timezone=schedule.get("external_trigger_timezone"))
    if health["level"] in ("orange", "red"):
        items.append(Item(f"No GitHub run for {health['hours']:.0f} hours",
                          f"The last one finished {when(datetime.fromisoformat(health['last']))}; runs are due every "
                          + ("4 hours while the month's Actions minutes run short." if health.get("slow") else "2 hours."), "/runs", "red" if health["level"] == "red" else "orange"))
    if external and minutes and "last_dispatch" in minutes:
        last = minutes.get("last_dispatch")
        # Judged at the time of the count, not now: the count is refreshed in the background, at
        # most every 30 minutes, so an old count must not raise a false alarm (2026-10-29).
        counted = datetime.fromisoformat(str(minutes.get("updated_at") or now.isoformat()).replace("Z", "+00:00"))
        late = not last or counted - datetime.fromisoformat(last.replace("Z", "+00:00")) > TRIGGER_LATE
        if late:
            items.append(Item("Scheduled runs are not being started",
                              (f"cron-job.org should start a run every 2 hours. The last run it started was {when(datetime.fromisoformat(last.replace('Z', '+00:00')))}"
                               if last else "No run started by it this month") + ". "
                              "Its history shows why: 401 is the token, 404 the URL or method, 422 the body.",
                              CRON_JOBS, "red", external=True))

    # Runs that crashed write no summary: their result comes from GitHub (the Actions count).
    latest = [r for r in (minutes or {}).get("latest_runs") or [] if r.get("conclusion") not in ("skipped", "cancelled")]
    failed = 0
    for run in latest:
        if run.get("conclusion") != "failure":
            break
        failed += 1
    if failed:
        items.append(Item(f"The last {failed} run{'s' if failed != 1 else ''} on GitHub failed" if failed > 1
                          else "The last run on GitHub failed",
                          f"The latest started {when(datetime.fromisoformat(str(latest[0]['at']).replace('Z', '+00:00')))}. "
                          "Its log on GitHub shows why.", latest[0].get("url") or "/runs", "red", external=True))

    # Companies fetched from the laptop only depend on its runs (the launchd job, 2026-11-18).
    if local_companies:
        last = pipeline.last_local_run(runs)
        since = now - datetime.fromisoformat(last["started_at"]) if last else None
        if since is None or since > LAPTOP_LATE:
            names = ", ".join(local_companies[:6]) + ("…" if len(local_companies) > 6 else "")
            items.append(Item(f"No laptop run for {since.total_seconds() / 3600:.0f} hours" if since else "No laptop run yet",
                              f"{names} are fetched from the laptop only. The launchd job runs at 08:00 and 20:00 and "
                              "after a wake; its log is logs/local-run.log.", "/runs",
                              "red" if since is None or since > 2 * LAPTOP_LATE else "orange",
                              command="uv run jobhunt launchd status"))

    # The laptop's scheduled runs use one fixed account (a token in the Keychain, 2026-11-18): say
    # when the latest one did not, so a different login does not quietly spend another account.
    latest_launchd = next((r for r in reversed(runs) if r.get("launchd")), None)
    if latest_launchd:
        used, wanted = accounts.key_of(latest_launchd, settings), accounts.default_key(settings)
        if used != wanted:
            items.append(Item(f"The laptop's scheduled run used {used}, not {wanted}",
                              f"It ran {when(datetime.fromisoformat(latest_launchd['started_at']))} with the claude "
                              f"login, as no {wanted} token is stored. Store one so these runs always use {wanted}:",
                              "/runs", "orange", command="uv run jobhunt launchd token"))

    expires = schedule.get("trigger_token_expires")
    if external and not expires:
        items.append(Item("Note when the cron-job.org token expires",
                          "Add trigger_token_expires = YYYY-MM-DD under [schedule] in config/settings.toml, so this "
                          f"page warns {TOKEN_WARN_DAYS} days ahead.", TOKENS_PAGE, "gray", external=True))
    elif expires:
        expiry = expires if isinstance(expires, date) else date.fromisoformat(str(expires))
        left = (expiry - today).days
        if left <= TOKEN_WARN_DAYS:
            items.append(Item(f"The cron-job.org token {'expired' if left < 0 else f'expires in {left} days'}",
                              "Create a new fine-grained token (this repository, Actions: read and write), paste it "
                              "into the cron-job.org job's Authorization header and update trigger_token_expires.",
                              TOKENS_PAGE, "red" if left < 3 else "orange", external=True))

    # Health of the latest scheduled run (2026-11-18): far fewer companies, or mine failing in a row.
    latest_health = next(((r.get("health") or {}) for r in reversed(runs) if r.get("kind") == "scheduled"), {})
    drop = latest_health.get("companies_drop")
    if drop:
        items.append(Item("Runs fetch far fewer companies than usual",
                          f"The latest run fetched {drop['fetched']} companies; usually about {drop['usual']}. "
                          "Check the generated list and the latest harvest.", "/runs", "red"))
    in_a_row = latest_health.get("failing") or {}
    for name, failing in pipeline.failing_on_github(runs).items():
        items.append(failing_item(name, failing, in_a_row.get(name)))
    harvests = read_jsonl(state_dir / "harvest.jsonl")
    if harvests and harvests[-1].get("refused"):
        last = harvests[-1]
        items.append(Item("This month's company harvest was not saved",
                          f"{when(datetime.fromisoformat(last['harvested_at']))}: {last['refused']}. The generated list "
                          f"kept its {last.get('relevant', 0):,} companies; rerun the harvest when Common Crawl answers.",
                          "/runs", "red", command="uv run jobhunt harvest --crawls 1 --skip-excluded"))

    week = (status or {}).get("week_nearly_full")
    if week:
        items.append(Item("The week is nearly full",
                          f"{week['message']}. Scheduled runs score only your companies and the wishlist until it "
                          "clears" + (f" (resets {when(datetime.fromisoformat(week['resets_at']))})"
                                      if week.get("resets_at") else "") + ".", "/runs", "orange"))

    hostwaits.load(state_dir)
    for board, record in hostwaits.dropped_boards().items():
        name = pipeline.BOARD_NAMES.get(board, board)
        items.append(Item(f"{name} is off the generated list",
                          f"It refused again on its slower delay ({record.get('asked_at', '')[:10]}). Remove its line "
                          "from host_waits.jsonl on the data branch to try again.", "/runs", "orange"))
    batch = workable_batch(settings, state_dir, now)
    if batch:
        items.append(batch)

    for reminder in settings.get("today", {}).get("reminder", []):
        start = reminder.get("from")
        if start and today < (start if isinstance(start, date) else date.fromisoformat(str(start))):
            continue
        board = reminder.get("unless_board_on")
        if board and board not in settings.get("generated", {}).get("exclude_boards", []):
            continue  # done: the board is on
        items.append(Item(reminder["text"], reminder.get("detail", ""), reminder.get("link", ""), "blue"))
    return items


BLOCKED = re.compile(r"HTTP (403|429)|bot check|Cloudflare|captcha", re.I)


def failing_item(name: str, failing: dict[str, Any], in_a_row: dict[str, Any] | None) -> Item:
    """One plain line for a company GitHub's runs cannot fetch, saying whether the laptop's runs
    (which fetch it as well) make up for it; the error itself is in the details (2026-11-18)."""
    word = "blocked" if BLOCKED.search(failing.get("error") or "") else "failing"
    count = (f"{in_a_row['runs']} runs in a row" if in_a_row
             else f"{failing['failed']} of the last {failing['runs']} runs")
    laptop = failing.get("laptop")
    if laptop == "ok":
        title, tone = f"{name}: {word} on GitHub, fetched from your laptop", "gray"
        after = "Its latest laptop fetch worked, so nothing is missed between laptop runs and GitHub's next try."
    elif laptop == "failed":
        title, tone = f"{name}: {word} on GitHub and on your laptop", "red"
        after = "Its latest laptop fetch failed too: open its careers page to check it still exists."
    else:
        title, tone = f"{name}: {word} on GitHub", "red" if in_a_row else "orange"
        after = "The next laptop run fetches it as well (08:00 and 20:00)."
    return Item(title, f"Failed on GitHub in {count}: {(failing.get('error') or '')[:160]}. {after}", "/companies", tone)


def workable_batch(settings: dict[str, Any], state_dir: Path, now: datetime) -> Item | None:
    """Workable's remaining accounts are checked in daily batches once its wait has passed."""
    if "workable" in settings.get("generated", {}).get("exclude_boards", []):
        return None
    hostwaits.load(state_dir)
    if "workable" in hostwaits.dropped_boards():
        return None
    harvests = read_jsonl(state_dir / "harvest.jsonl")
    if not harvests:
        return None
    latest = max(harvests, key=lambda h: h.get("harvested_at", ""))
    board = (latest.get("boards") or {}).get("workable") or {}
    remaining = int(board.get("found", 0)) - int(board.get("already_configured", 0)) - int(board.get("checked", 0))
    if remaining <= 0:
        return None
    record = next((r for r in WAITS.records() if r["host"] == "apply.workable.com"), None)
    waiting = datetime.fromisoformat(record["until"]) if record else None
    if waiting and waiting > now:
        return Item(f"Workable batch from {when(waiting)}",
                    f"Workable asked to be left alone until then; {remaining:,} accounts are left to check in batches "
                    "of 500 (about 42 minutes each).", "/runs", "gray")
    if now - datetime.fromisoformat(latest["harvested_at"]) < BATCH_EVERY:
        return None
    crawls = " ".join(f"--crawl {c}" for c in CRAWLS)
    return Item("A Workable batch is due",
                f"{remaining:,} Workable accounts are left to check; one batch of 500 takes about 42 minutes. Run it on "
                "the laptop:", "", "blue",
                command=f"uv run jobhunt harvest {crawls} --board workable --limit-per-board 500 --recheck-errors && "
                        "uv run jobhunt sync --push")
