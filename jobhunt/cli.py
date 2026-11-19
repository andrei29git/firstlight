"""Command line entry point: `jobhunt <command>`."""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import date
from pathlib import Path

from . import config as cfg
from . import leakcheck

HOST = "127.0.0.1"  # never 0.0.0.0: the app has no login
PORT = 8000
DEMO_PORT = 8001  # its own address, so the browser keeps its settings apart from the real app's


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def demo_runner(argv: list[str], stdin: str, cwd: str, timeout: float, env: dict[str, str]) -> tuple[int, str, str]:
    """Claude calls in demo mode: none."""
    return 1, "", "Demo mode: Claude calls are switched off (the scores shown were recorded)."


def demo_paths() -> cfg.Paths:
    """The demo's data in a fresh temporary folder (jobhunt/demo.py)."""
    from . import demo

    return demo.prepare(PACKAGE_ROOT)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    paths = demo_paths() if args.demo else cfg.Paths.from_env()
    # The demo on its own port (2026-11-19), so its theme and remembered settings, kept by the
    # browser per address, never mix with the real app's.
    port = args.port or (DEMO_PORT if args.demo else PORT)
    print(f"Firstlight on http://{HOST}:{port}  (database {paths.database})"
          + ("\nDemo: an invented candidate and pre-recorded data; no network or Claude calls." if args.demo else ""))
    app = create_app(paths, runner=demo_runner, demo=True) if args.demo else create_app(paths)
    # uvicorn passes a stop signal on once it has shut down; ending normally lets the clean-up run.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        uvicorn.run(app, host=HOST, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        if args.demo:  # its temporary folder goes with it
            from . import demo
            demo.remove(paths.root)
    return 0


def cmd_leakcheck(args: argparse.Namespace) -> int:
    paths = cfg.Paths.from_env()
    leaks = leakcheck.scan(paths.root, paths.config)
    for leak in leaks:
        print(f"{leak.path}:{leak.line}: matches leak pattern {leak.pattern!r}")
    if leaks:
        print(f"\n{len(leaks)} leak(s) found. Candidate facts belong in config/ only.", file=sys.stderr)
        return 1
    print("No leaks found.")
    return 0


def _http(paths: cfg.Paths):
    from .sources import Http

    return Http.from_settings(cfg.load_settings(paths.config))


def cmd_verify(args: argparse.Namespace) -> int:
    """Fetch each configured board's listing (no detail pages), report counts, record `verified`."""
    from . import companies as co
    from .sources import SourceError, fetch, no_details

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    http = _http(paths)
    wanted = {n.lower() for n in args.names}
    targets = [c for c in co.load_companies(paths.config)
               if c.fetchable and (not wanted or c.name.lower() in wanted)]
    missing = wanted - {c.name.lower() for c in targets}
    if missing:
        print(f"Not found or not fetchable (no board, manual or todo): {', '.join(sorted(missing))}", file=sys.stderr)
    today = date.today()
    failures = 0
    for company in targets:
        try:
            jobs = fetch(company, http, details=no_details,
                         custom_max_links=settings.get("fetching", {}).get("custom_max_links", 50))
        except SourceError as e:
            failures += 1
            print(f"✗ {company.name:20} {company.board:11} {e}")
            continue
        print(f"✓ {company.name:20} {company.board:11} {len(jobs):4} jobs")
        if args.list:
            for job in sorted(jobs, key=lambda j: j.title.lower()):
                print(f"      {job.title}  ·  {'; '.join(job.locations) or '?'}")
        if not args.dry_run:
            co.set_verified(paths.config, company.name, today)
    skipped = [c.name for c in co.load_companies(paths.config) if not c.fetchable and not wanted]
    if skipped:
        print(f"\nSkipped {len(skipped)} without a fetchable board (todo, manual or no board).")
    return 1 if failures else 0


def cmd_detect(args: argparse.Namespace) -> int:
    from . import detect
    from .sources import SourceError

    if not args.url and not args.name:
        print("Give a careers URL, a --name to probe, or both.", file=sys.stderr)
        return 2
    paths = cfg.Paths.from_env()
    try:
        findings, final_url = detect.detect(args.url, _http(paths), name=args.name)
    except SourceError as e:
        print(f"Could not read {args.url}: {e}", file=sys.stderr)
        return 1
    if final_url and final_url != args.url:
        print(f"Redirected to {final_url}")
    if not findings:
        print("No known job board or job links found. The page may render with JavaScript: "
              "try --name to probe the public boards, "
              "find the jobs page by hand, or mark the company manual = true.")
        return 1
    name = args.name or "COMPANY"
    for finding in findings:
        if finding.board == "custom" and not finding.supported:
            print(f"\n• its own careers site: could not be set up ({finding.error})")
            continue
        if not finding.supported:
            print(f"\n• {finding.board} {finding.config.get('slug', '')}: recognised, no adapter yet")
            continue
        status = f"{finding.jobs} jobs" if finding.works else f"failed: {finding.error}"
        print(f"\n• {finding.board}: {status}")
        for note in finding.notes:
            print(f"  {note}")
        for title, link in finding.samples[:5] or ([("", finding.sample)] if finding.sample else []):
            print(f"  e.g. {title + ': ' if title else ''}{link}")
        if finding.works:
            if finding.board == "custom":
                print("  Check link_pattern before using it; it is a guess from the links on the page.")
            print("\n" + detect.toml_block(name, finding, date.today()))
    return 0 if any(f.works for f in findings) else 1


def _state(args: argparse.Namespace, paths: cfg.Paths):
    from .state import State

    return State((paths.root / args.state).resolve())


def _scoring_options(settings: dict, args: argparse.Namespace) -> tuple[int, float]:
    scoring = settings.get("scoring", {})
    return (args.parallel or int(scoring.get("parallel", 4)), float(scoring.get("call_timeout_seconds", 180)))


def print_scoring(stats: dict, phase: str = "") -> None:
    print(f"\nScoring{f' ({phase})' if phase else ''} with {stats['model']} [{stats['prompt_version']}]: {stats['calls']} calls ({stats['ok']} ok, {stats['failed']} failed), "
          f"{stats['deferred']} deferred of {stats['pending']} pending")
    if stats["calls"]:
        print(f"  {stats['wall_seconds']}s wall time with {stats['parallel']} in parallel; "
              f"{stats['seconds_per_call']}s per call; API-equivalent cost ${stats['cost_usd']}")
        print(f"  tokens {stats['tokens']}")
        print(f"  subscription limit status {dict(stats['rate_limit'])} (windows: {', '.join(stats['rate_limit_windows'])})")
        print(f"  statuses {dict(stats['statuses'])}; fits {dict(sorted(stats['fits'].items()))}")
    if stats["errors"]:
        print(f"  errors {dict(stats['errors'])}")
    if stats.get("retries"):
        print(f"  {stats['retries']} calls retried after a failure of Claude's own")
    for failure in stats.get("failures", [])[:3]:
        print(f"  failed call: {failure[:300]}")
    print(f"  about {stats['window_share_spent']:.0%} of a 5-hour window used by this run (estimated)")
    if stats.get("stopped"):
        print(f"  stopped early: {stats['stopped']}. The rest waits for the next run.")


def run_kind(args: argparse.Namespace, scheduled: bool) -> str:
    """What started the run, in a few words for the Runs page."""
    if args.sonnet_only:
        kind = "Sonnet only"
    elif args.backfill:
        kind = "backfill"
    elif scheduled:
        kind = "scheduled"
    elif args.no_fetch:
        kind = "scoring only"
    else:
        kind = f"run, generated list: {args.generated}"
    if args.names:
        kind += ": " + ", ".join(args.names)
    if args.local_only:
        kind += " (local sites)"
    return kind


def run_cap(args: argparse.Namespace, settings: dict) -> int | None:
    """Scoring calls per phase: --cap if given; none when --budget is given (the budget is the only
    limit then); else backfill_max_calls for backlog runs and max_calls_per_run (None) otherwise."""
    if args.cap is not None:
        return args.cap
    if args.budget is not None:
        return sys.maxsize
    scoring = settings.get("scoring", {})
    return int(scoring.get("backfill_max_calls", 400)) if args.backfill or args.sonnet_only else None


def run_targets(args: argparse.Namespace, paths: cfg.Paths, settings: dict, state_dir: Path, say=print) -> list:
    """The companies a run fetches: companies.toml, the wishlist (every run) and this run's slot of
    the generated list (wishlist companies leave it while wishlisted)."""
    from . import blocklist
    from . import companies as co
    from . import harvest, prefilter, wishlist

    if args.no_fetch or args.sonnet_only:  # score what is already pending
        return []
    blocked = blocklist.names(state_dir)
    targets = _run_targets(args, paths, settings, state_dir, say, co, harvest, wishlist)
    skipped = [c for c in targets if prefilter.normalise_company(c.name) in blocked]
    if skipped:
        say(f"Blocked: {len(skipped)} companies not fetched.")
    return [c for c in targets if prefilter.normalise_company(c.name) not in blocked]


def _run_targets(args, paths, settings, state_dir, say, co, harvest, wishlist) -> list:
    from dataclasses import replace

    from . import prefilter

    configured = co.load_companies(paths.config)
    starred = wishlist.names(state_dir)
    # Your companies with the generated list's rules join the daily slots, unless starred.
    configured = [replace(c, origin="config") if c.origin == "generated" and prefilter.normalise_company(c.name) in starred
                  else c for c in configured]
    daily = [c for c in configured if c.fetchable and c.origin == "generated"]
    fetchable = [c for c in configured if c.fetchable and c.origin != "generated"]
    configured_ids = harvest.configured_identities(configured)
    wished = wishlist.companies(state_dir, configured_ids)
    from . import accepted  # companies accepted from "Find more companies", with the generated list's rules
    skip = configured_ids | wishlist.identities(state_dir)
    found = accepted.companies(state_dir, skip)
    wanted = {n.lower() for n in args.names}
    if wanted:  # named companies are fetched wherever they are configured to be fetched from
        return [c for c in fetchable + daily + wished + found if c.name.lower() in wanted]
    targets = [c for c in fetchable + wished if (c.fetch_from == "local") == args.local_only]
    if args.local_only:
        # Also every company whose latest GitHub fetch failed (2026-11-18): a site that refuses
        # GitHub now and then still gets fetched, from the laptop.
        from . import pipeline
        from .state import State

        failed = pipeline.github_failed_latest(State.read_only(state_dir).runs()) if state_dir.exists() else set()
        local = targets + [c for c in daily if c.fetch_from == "local"]
        fallback = [c for c in fetchable + wished + daily + found if c.fetch_from != "local" and c.name in failed]
        if fallback:
            say(f"Also fetched here, as their latest GitHub fetch failed: {', '.join(c.name for c in fallback)}.")
        return local + fallback
    wished_here = [c for c in wished if c.fetch_from != "local"]
    if wished_here:
        say(f"Wishlist: fetching {len(wished_here)} companies not in companies.toml.")
    if args.generated == "due":  # a scheduled run: boards fetched every N runs take their turn
        from datetime import UTC, datetime

        from . import generated

        every_runs = settings.get("fetching", {}).get("every_runs", {})
        if every_runs:
            kept = generated.spread(targets, generated.current_slot(datetime.now(UTC)), every_runs)
            if len(kept) < len(targets):
                say(f"Spread across runs: {len(targets) - len(kept)} companies wait for their turn.")
            targets = kept
    if args.generated != "none":
        from datetime import UTC, datetime

        from . import generated

        listed = generated.load(state_dir, settings, skip | accepted.identities(state_dir))
        listed += [c for c in daily if c.fetch_from != "local"] + found
        now = datetime.now(UTC)
        due = listed if args.generated == "all" else generated.due(
            listed, generated.current_slot(now), day=now.date().toordinal(),
            every_days=settings.get("generated", {}).get("every_days", {}))
        say(f"Generated list: fetching {len(due)} of {len(listed)} companies ({args.generated}).")
        targets += due
    return targets


def cmd_run(args: argparse.Namespace) -> int:
    from . import pipeline, prefilter

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    cap = run_cap(args, settings)
    if args.max_pages:
        settings = {**settings, "fetching": {**settings.get("fetching", {}), "custom_max_links": args.max_pages}}
    fetching = settings.get("fetching", {})
    scheduled = args.generated == "due" and not args.backfill and not args.names and not args.local_only
    options = pipeline.Options(score=not args.no_score, backlog=args.backfill, cap=cap, parallel=args.parallel,
                               budget=args.budget, sonnet_only=args.sonnet_only,
                               ignore_weekly_warning=args.ignore_weekly_warning,
                               fetch_seconds=float(fetching.get("scheduled_max_seconds", 600)) if scheduled else None,
                               max_details=int(fetching.get("scheduled_max_details", 400)) if scheduled else None,
                               # a run naming companies scores only their roles
                               only=frozenset(prefilter.normalise_company(n) for n in args.names),
                               kind=run_kind(args, scheduled))
    from . import sync
    from .progress import Progress

    from . import interrupt

    progress = Progress()
    state_dir = (paths.root / args.state).resolve()
    syncing = settings.get("sync", {}).get("enabled", True) and not args.no_sync and sync.is_worktree(state_dir)
    # Ctrl+C: finish the calls in progress, then save and push as usual (a second Ctrl+C quits).
    pushed_ok = False
    holder = ("a Sonnet-only run" if args.sonnet_only else "a backfill run" if args.backfill else "a run")
    with interrupt.graceful("the fetches and calls in progress"), sync.state_lock(state_dir, holder=holder):
        if syncing:
            progress.phase("sync: pulling the data branch")
            try:
                pulled = sync.pull_locked(state_dir)
                progress.log(f"Pulled the data branch ({len(pulled.changed_files)} files changed).")
            except sync.SyncError as e:
                progress.log(f"Could not pull the data branch ({e}); running on the local state.")
        targets = run_targets(args, paths, settings, state_dir, progress.log)  # after the pull: latest wishlist
        summary = pipeline.run(paths.config, settings, targets, _state(args, paths), options, progress=progress)
        if syncing:
            calls = sum(p.get("calls", 0) for p in summary.get("scoring", {}).values())
            message = (f"Run {summary['started_at']}: {(summary.get('jobs') or {}).get('new', 0)} new roles, "
                       f"{calls} scoring calls")
            progress.phase("sync: pushing to the data branch")
            try:
                pushed = sync.push_locked(state_dir, message)
                pushed_ok = True
                progress.log(f"Pushed to the data branch ({pushed.commit or pushed.note}).")
            except sync.SyncError as e:
                progress.log(f"Could not push ({e}); the state is kept locally and goes with the next push.")
            progress.done()

    print_summary(summary, args.state, pushed_ok=pushed_ok, syncing=syncing)
    return 0


def print_summary(summary: dict, state_label: str, pushed_ok: bool = False, syncing: bool = False) -> None:
    """The end of `jobhunt run`, for every kind of run: one that fetched, one that only scored
    (--no-fetch, --sonnet-only) and one that stopped before starting (backlog_stopped)."""
    from . import pipeline

    if summary.get("backlog_stopped"):  # stopped before fetching or scoring: say why first
        print(f"Backlog run not started: {summary['backlog_stopped']}.")
    for name, entry in (summary.get("sources") or {}).items():
        if "error" in entry:
            print(f"✗ {name:20} {entry['board']:11} {entry['error']}")
        else:
            print(f"✓ {name:20} {entry['board']:11} {entry.get('fetched', 0):4} jobs, {entry.get('passed', 0):3} pass, "
                  f"{entry.get('new', 0):4} new, {entry.get('closed', 0)} closed  ({entry.get('seconds', 0)}s)")
    for board, g in summary.get("generated", {}).items():
        print(f"  generated {board:11} {g.get('companies', 0):5} companies, {g.get('fetched', 0):6} jobs, "
              f"{g.get('passed', 0):5} pass, {g.get('parked', 0):5} parked (UK/US), {g.get('new', 0):5} new, "
              f"{g.get('errors', 0)} errors ({g.get('seconds', 0):.0f}s)")
    jobs = summary.get("jobs") or {}
    if summary.get("companies"):  # a run that fetched (not --no-fetch, --sonnet-only or stopped)
        print(f"\nFetched {jobs.get('fetched', 0)} jobs from {summary.get('companies', 0)} companies in "
              f"{summary.get('fetch_seconds', 0)}s ({summary.get('errors', 0)} errors). Pre-filter: {jobs.get('passed', 0)} pass, "
              + ", ".join(f"{v} {k}" for k, v in sorted(jobs.items()) if k.startswith("dropped")) + ".")
        print(f"New: {jobs.get('new', 0)} ({jobs.get('new passed', 0)} pass). Closed: {jobs.get('closed', 0)}. "
              f"Duplicates merged: {summary.get('duplicates', 0)}.")
    elif not summary.get("backlog_stopped"):
        print("\nNo fetching in this run: scoring what is already stored.")
    if summary.get("fetch_not_started") or summary.get("details_deferred"):
        print(f"Scheduled-run fetch limits: {summary.get('fetch_not_started', 0)} companies left for a later run, "
              f"{summary.get('details_deferred', 0)} posting texts left for a later fetch.")
    if summary.get("week_nearly_full"):
        print(summary["week_nearly_full"] + ".")
    for line in pipeline.wait_notes(summary):
        print(line + ".")
    if summary.get("weekly_warning_ignored"):
        print(summary["weekly_warning_ignored"] + ".")
    if summary.get("weekly_warning_below"):
        print(summary["weekly_warning_below"] + ".")
    if summary.get("interrupted"):
        made = sum(p.get("ok", 0) for p in summary.get("scoring", {}).values())
        print(f"Run stopped early (Ctrl+C): the {made} scores made in this run are saved"
              + (" and pushed." if pushed_ok else " here (not pushed: they go with the next push)." if syncing else "."))
    if summary.get("score_policy_fixes"):
        print(f"Visa blockers turned into checks on {summary['score_policy_fixes']} stored scores (visa_never_blocks).")
    if "alerts" in summary:
        al = summary["alerts"]
        if al.get("sent") or al.get("note") or al.get("errors"):
            print(f"\nAlerts: {al.get('sent', 0)} sent" + (f" for {al['roles']} roles" if al.get("roles") else "")
                  + (f"; {al['note']}" if al.get("note") else "") + (f"; errors {al['errors']}" if al.get("errors") else ""))
    guard = summary.get("weekly_guard") or {}
    if guard:
        print(f"\nWeekly guard: the tool used about {guard.get('used_windows', 0)} windows in the last 7 days "
              f"({guard.get('share_of_week', 0):.0%} of the weekly limit). {pipeline.guard_words(guard)}"
              + (f" {guard['companies_not_fetched']} generated-list companies not fetched."
                 if guard.get("companies_not_fetched") else ""))
    for phase, stats in summary.get("scoring", {}).items():
        if stats.get("titlecheck"):
            print(f"\nTitle check ({phase}): {stats['checked']} of {stats['pending']} roles checked in {stats['calls']} calls, "
                  f"{stats['dropped']} dropped, {stats['rejected_ungrounded']} drops rejected as ungrounded; "
                  f"about {stats['window_share_spent']:.0%} of a 5-hour window.")
            if stats.get("stopped"):
                print(f"  stopped early: {stats['stopped']}. The rest waits for the next run.")
            continue
        print_scoring(stats, phase)
    overlap = summary.get("overlap")
    if overlap and overlap.get("scored_while_fetching"):
        print(f"\nScored while fetching: {overlap['scored_while_fetching']} calls. Run took {summary.get('seconds', 0)}s; "
              f"about {overlap['seconds_without_overlap']}s if scoring had waited for the fetch.")
    print(f"\nRun took {summary.get('seconds', 0)}s. State in {state_label}/")


def cmd_rescore(args: argparse.Namespace) -> int:
    from . import pipeline, scoring

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    if args.queue:
        return queue_rescores(args, paths, settings)
    state = _state(args, paths)
    parallel, timeout = _scoring_options(settings, args)
    cap = args.cap if args.cap is not None else int(settings.get("scoring", {}).get("max_calls_per_run", 100))
    from . import interrupt

    pipeline.use_account(settings)  # scores record the Claude account (2026-11-18)
    with interrupt.graceful():  # Ctrl+C: finish the calls in progress and save them
        guard = scoring.UsageGuard()
        for model in dict.fromkeys(pipeline.models(settings)):  # each model re-scores its own stale scores
            prompt = scoring.build_prompt(paths.config, settings, model=model)
            stats = pipeline.score_pending(state, prompt, pipeline.stale(state, prompt), cap=cap, parallel=parallel,
                                           runner=scoring.run_subprocess, timeout=timeout, guard=guard)
            print_scoring(stats, "rescore")
        state.save()
    return 0


def queue_rescores(args: argparse.Namespace, paths: cfg.Paths, settings: dict) -> int:
    """Mark roles for a rescore that the scheduled runs spread out (`[scoring] rescore_per_run` a
    run, highest fit first). Reports by default; --apply writes and pushes."""
    import re
    from datetime import UTC, datetime

    from . import pipeline, sync
    from .state import State

    state_dir = (paths.root / args.state).resolve()
    matching = re.compile(args.matching, re.IGNORECASE) if args.matching else None
    if matching is None and args.min_fit is None:
        print("Say which roles: --matching TEXT (with --matching-min-fit) and/or --min-fit N.")
        return 2
    with sync.state_lock(state_dir, holder="the rescore queue"):
        if args.apply and sync.is_worktree(state_dir):
            sync.pull_locked(state_dir)
        state = State(state_dir) if args.apply else State.read_only(state_dir)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        counts = pipeline.queue_rescores(state, settings, now, matching, args.matching_min_fit, args.min_fit)
        share = scoring_share(settings)
        windows = sum(n * share.get(model, 0.004) for model, n in counts.items())
        per_run = int(settings.get("scoring", {}).get("rescore_per_run", 30))
        runs = max(-(-n // per_run) for n in counts.values()) if counts else 0
        print(f"{sum(counts.values())} roles to rescore ({', '.join(f'{n} {m}' for m, n in sorted(counts.items()))}): "
              f"about {windows / 12.5:.1%} of a week, over about {runs} scheduled runs ({per_run} a run per model).")
        if not args.apply:
            print("Nothing marked: add --apply to queue them.")
            return 0
        state.save()
        if sync.is_worktree(state_dir):
            sync.push_locked(state_dir, f"Rescore queue: {sum(counts.values())} roles")
            print("Queued and pushed: the next scheduled runs pick them up.")
    return 0


def scoring_share(settings: dict) -> dict[str, float]:
    """A call's share of a five-hour window per model (measured 2026-10-24: Sonnet 0.4%, Haiku 0.12%)."""
    shares = settings.get("usage", {}).get("share_per_call", {})
    return {"sonnet": 0.004, "haiku": 0.0012, **{k: float(v) for k, v in shares.items()}}


def cmd_score(args: argparse.Namespace) -> int:
    import json

    from . import scoring
    from .sources.base import Job

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    text = open(args.file, encoding="utf-8").read()
    job = Job(company=args.company or "", title=args.title or "", url="", source="file", source_id=args.file,
              locations=[args.location] if args.location else [], description=text)
    score = scoring.score_job(job, scoring.build_prompt(paths.config, settings))
    if score.error:
        print(f"Scoring failed: {score.error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": score.status, **score.result}, indent=2, ensure_ascii=False))
    print(f"\n{score.seconds}s, API-equivalent ${score.cost_usd:.4f}, tokens {score.tokens}, "
          f"limit {(score.rate_limit or {}).get('status')}", file=sys.stderr)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evaluate

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    if args.model:
        settings.setdefault("scoring", {})["model"] = args.model
    if args.thinking is not None:
        settings.setdefault("scoring", {})["max_thinking_tokens"] = args.thinking
    parallel, timeout = _scoring_options(settings, args)
    from .progress import Progress

    found, missing = evaluate.run_eval(paths.config, settings, runs=args.runs, parallel=parallel, timeout=timeout,
                                       progress=Progress())
    if not found:
        print("No calibration role has a saved posting yet: add `posting = \"calibration/<file>.txt\"` "
              "to examples in calibration.toml.", file=sys.stderr)
        return 1
    print(f"{'Role':60} {'want':>4}  got        status        action")
    for case in found:
        fits = ",".join(str(f) for f in case.fits) or "-"
        statuses = ",".join(sorted({s.status for s in case.ok})) or "-"
        actions = ",".join(sorted({s.result["action"] for s in case.ok})) or "-"
        print(f"{case.role[:60]:60} {case.example['fit']:>4}  {fits:10} {statuses:13} {actions}"
              f"  (want {case.expected_status}, {case.expected_action})")
    print()
    consider = bool(settings.get("scoring", {}).get("consider_start_date", False))
    for name, value in evaluate.agreement(found, consider).items():
        print(f"{name:36} {value}")
    if missing:
        print(f"\nNot scored (no posting saved): {len(missing)} roles.")
    saved = evaluate.save(found, (paths.root / args.state).resolve() / "eval")
    print(f"Full results: {saved}")
    return 0




def cmd_shortlist(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import shortlist

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    applied = [] if args.include_applied else shortlist.load_applied(paths.database)
    hidden: list = []
    found = shortlist.rows(_state(args, paths), settings, shortlist.current_version(paths.config, settings),
                           min_fit=args.min_fit, actions=tuple(args.action or ["apply_now"]),
                           applied=applied, hidden=hidden)
    shortlist.print_rows(found)
    if hidden:
        print(f"\nLeft out {len(hidden)} with an application already (--include-applied shows them):")
        for row, match in hidden:
            print(f"  {row.job['company']} · {row.job['title']}  ←  your application: {match.role} ({match.stage})")
    if args.csv:
        path = Path(args.csv)
        shortlist.write_csv(found, path)
        print(f"Written to {path} (a snapshot: edits there are never read back).")
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    from . import companies as co
    from . import harvest
    from .sources import Http

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    user_agent = settings.get("fetching", {}).get("user_agent", "jobhunt")
    # The index server is slow and often answers 504: one request at a time, patient retries.
    index_http = Http(user_agent, per_host_delay=1.0, max_retries=6, timeout=180)
    crawls = args.crawl or harvest.latest_crawls(index_http, args.crawls)
    from .progress import Progress

    progress = Progress()
    run = harvest.Harvest(
        state_root=(paths.root / args.state).resolve(), progress=progress,
        allowed=set(settings.get("locations", {}).get("allowed_countries", [])),
        configured=harvest.configured_identities(co.load_companies(paths.config)),
        make_http=lambda: Http.from_settings(settings), index_http=index_http,
        log=progress.log, recheck_days=args.recheck_days,
        recheck_errors=args.recheck_errors, limit_per_board=args.limit_per_board,
        skip_boards=(frozenset(settings.get("generated", {}).get("exclude_boards", [])) if args.skip_excluded
                     else frozenset()) | frozenset(args.skip_board or []),
        only_boards=frozenset(args.board or []),
        park=frozenset(settings.get("generated", {}).get("park_countries", ["GB", "US"])))
    from . import sync

    with sync.state_lock((paths.root / args.state).resolve(), holder="the harvest"):
        summary = run.run(crawls)
    print(f"\nHarvest of {', '.join(crawls)} took {summary['seconds']:.0f}s: {summary['accounts_found']} accounts found, "
          f"{summary['relevant']} relevant companies in companies_generated.jsonl.")
    print(f"\n{'board':16} {'found':>6} {'config':>6} {'checked':>7} {'answer':>6} {'relevant':>8} "
          f"{'open':>7} {'pass':>6} {'pass<28d':>8} {'undated':>7}")
    rows = sorted(summary["boards"].items(), key=lambda kv: -kv[1].get("relevant", -1) * 10**6 - kv[1]["found"])
    for board, r in rows:
        if r["supported"]:
            print(f"{board:16} {r['found']:6} {r['already_configured']:6} {r['checked']:7} {r['answering']:6} "
                  f"{r['relevant']:8} {r['open_roles']:7} {r['prefilter_pass']:6} {r['prefilter_pass_recent']:8} "
                  f"{r['prefilter_pass_undated']:7}")
        else:
            print(f"{board:16} {r['found']:6}   no adapter: not checked")
    if summary["index_pages_failed"]:
        print(f"\n{len(summary['index_pages_failed'])} index pages failed; rerun to retry them (cached pages are kept).")
    if summary.get("refused"):
        from . import alerts

        print(f"\nThe generated list was NOT replaced: {summary['refused']}. It keeps its {summary['relevant']:,} "
              "companies; rerun the harvest later.")
        alerts.notify(settings, "Harvest not written",
                      f"The generated list was kept as it was ({summary['relevant']:,} companies): "
                      f"{summary['refused']}. Rerun the harvest later.")
    for host, wait in summary.get("host_waits", {}).items():
        print(f"\n{host} {'asked to wait' if wait['new'] else 'is waiting'} until {wait['until'][:16]} UTC: "
              f"{wait['not_checked']} accounts left unchecked for a later harvest.")
    for board in summary.get("boards_dropped", []):
        print(f"\n{board} is off the generated list: it refused again on its longer delay (host_waits.jsonl).")
    return 0


def cmd_titlecheck(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime

    from . import titlecheck

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    from .progress import Progress

    report = titlecheck.validate(_state(args, paths), paths.config, settings,
                                 parallel=args.parallel or int(settings.get("scoring", {}).get("parallel", 4)),
                                 departments=not args.no_department, progress=Progress())
    print(f"Screened {report['roles']} scored roles and {report['calibration_roles']} calibration roles in "
          f"{report['calls']} calls (${report['cost_usd']}; {report['failed_calls']} failed, "
          f"{report['unchecked']} roles unchecked).")
    if report["stopped"]:
        print(f"Stopped early: {report['stopped']}")
    share = report["dropped"] / report["roles"] if report["roles"] else 0
    print(f"Dropped {report['dropped']} of {report['roles']} scored roles ({share:.0%}); fit of the dropped: "
          f"{report['dropped_fit']}")
    print(f"Proposed drops rejected because their profile quote is not in profile.md: {report['rejected_ungrounded']}")
    for key in ("sonnet_6+", "sonnet_7+", "calibration_6+", "calibration_7+"):
        print(f"  {key:16} {report[key]}")
        for line in report[f"{key}_lost"]:
            print(f"      lost: {line}")
    folder = (paths.root / args.state).resolve() / "titlecheck"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = "" if report["departments"] else "-no-department"
    path = folder / f"validate-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}{suffix}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Every drop with its reason and profile quote: {path}")
    return 0


def cmd_harvest_sample(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from . import harvest
    from .progress import Progress
    from .sources import Http
    from .state import read_jsonl

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    state_root = (paths.root / args.state).resolve()
    generated = read_jsonl(state_root / "companies_generated.jsonl")
    entries = harvest.sample_accounts(generated, args.per_board, boards=set(args.board) if args.board else None)
    print(f"Sampling {len(entries)} of {len(generated)} relevant accounts...", flush=True)
    path = harvest.sample(state_root, entries, lambda: Http.from_settings(settings),
                          set(settings.get("locations", {}).get("allowed_countries", [])),
                          set(settings.get("generated", {}).get("park_countries", [])),
                          datetime.now(UTC).date(), progress=Progress())
    print(f"Written to {path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from . import sync

    paths = cfg.Paths.from_env()
    state_dir = (paths.root / args.state).resolve()
    try:
        if args.init:
            result = sync.init(paths.root, state_dir)
            print(f"State folder linked to the data branch: {result.note} ({result.commit or 'no new commit'}).")
        elif args.push:
            result = sync.push(state_dir, args.message)
            print(f"Pushed: {result.commit or result.note}.")
        else:
            result = sync.pull(state_dir)
            print(f"Pulled: {len(result.changed_files)} files changed.")
    except sync.SyncError as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Check the two secrets a scheduled run needs: one tiny scoring call through `claude -p` with
    the run's exact flags, and (with --alert) one test notification to NTFY_TOPIC."""
    import os

    from . import alerts, scoring
    from .sources.base import Job

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    ok = True
    model = settings.get("scoring", {}).get("backlog_model", "haiku")
    prompt = scoring.build_prompt(paths.config, settings, model=model)
    job = Job(company="Selftest", title="Graduate Software Engineer", url="", source="selftest", source_id="1",
              locations=["Amsterdam, Netherlands"], description="Graduate role building backend services in Python.")
    score = scoring.score_job(job, prompt)
    if score.result:
        print(f"Claude: ok ({model}, fit {score.result['fit']}, {score.seconds}s, "
              f"limit status {(score.rate_limit or {}).get('status')})")
    else:
        ok = False
        print(f"Claude: FAILED: {score.error}")
    if args.alert:
        topic = os.environ.get("NTFY_TOPIC", "")
        if not topic:
            ok = False
            print("ntfy: FAILED: NTFY_TOPIC is not set")
        else:
            server = str(settings.get("alerts", {}).get("ntfy_server", "https://ntfy.sh")).rstrip("/")
            try:
                alerts.post_json(server, {"topic": topic, "title": "Firstlight: test notification",
                                          "message": "Alerts from scheduled runs will arrive like this.",
                                          "tags": ["white_check_mark"]})
                print("ntfy: ok (check your phone)")
            except Exception as e:  # report any failure plainly
                ok = False
                print(f"ntfy: FAILED: {type(e).__name__}: {e}")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Roles waiting at each step, the backlog's progress and this week's usage (read-only)."""
    from datetime import UTC, datetime

    from . import pipeline, sync
    from .state import State

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    state_dir = (paths.root / args.state).resolve()
    if args.pull and sync.is_worktree(state_dir):
        print(f"Pulled the data branch ({len(sync.pull(state_dir).changed_files)} files changed).")
    found = pipeline.status(State.read_only(state_dir), settings, datetime.now(UTC))
    rows = ["waiting for posting text", "waiting for the title check", "waiting for a first score",
            "waiting for Sonnet (Haiku 6+)"]
    parts = found["parts"]
    print(f"{'':32}{'yours':>14}{'generated list':>16}")
    for row in rows:
        mine, generated = parts["my companies"].get(row, 0), parts["generated list"].get(row, 0)
        if row == "waiting for the title check":
            mine = "–"
        print(f"{row:32}{mine:>14}{generated:>16}")
    for name, part in parts.items():
        total = part.get("backlog total", 0)
        if total:
            done = part.get("backlog done", 0)
            shown = "your companies and wishlist" if name == "my companies" else name
            print(f"\nBacklog ({shown}): {done} of {total} done ({done / total:.0%}); the rest is in the rows above.")
    if found.get("week_nearly_full"):
        week = found["week_nearly_full"]
        print(f"\nWeek nearly full: {week['message']} ({week['at'][:16]}"
              + (f", resets {week['resets_at'][:16]}" if week.get("resets_at") else "") + "). Backlog runs wait; "
              "scheduled runs score only your companies and wishlist until it clears.")
    for name, f in found["failing_on_github"].items():
        hint = " It works from the laptop: set fetch_from = \"local\"." if f["works_on_laptop"] else ""
        print(f"\nFailing on GitHub: {name}, in {f['failed']} of the last {f['runs']} scheduled runs "
              f"({f['error'][:120]}).{hint}")
    from . import hostwaits
    hostwaits.load(state_dir)
    for host, until in hostwaits.waiting().items():
        print(f"\nWaiting: {host} asked to be left alone until {until.isoformat(timespec='minutes')[:16]} UTC; "
              "runs and harvests do not contact it until then.")
    for board in hostwaits.dropped_boards():
        print(f"\n{pipeline.BOARD_NAMES.get(board, board)} is off the generated list: it refused again on its "
              "longer delay (remove its line from host_waits.jsonl to try again).")
    guard = found["weekly_guard"]
    windows_per_week = float(settings.get("usage", {}).get("windows_per_week", 12.5))
    claude = guard.get("claude")
    if claude:
        print(f"\nThis week: Claude reports {pipeline.limits_text(claude)} (your own Claude use included; "
              f"as of {claude['at'][:16].replace('T', ' ')} UTC).")
    print(f"{'' if claude else chr(10)}The tool's own calls: about {guard['used_windows']} of ~{windows_per_week:g} "
          f"five-hour windows in the last 7 days ({guard['share_of_week']:.0%}). "
          f"{pipeline.guard_words({**guard, 'changed': False})}")
    return 0


def cmd_repair_text(args: argparse.Namespace) -> int:
    """Repair stored titles, locations, departments and company names that were UTF-8 read as
    Latin-1 ("MÃ¼nchen"; Personio's feed until 2026-10-29), and titles ending in page-title leftovers
    ("… | SEB, Vilnius"). Reports by default; --apply writes and pushes. Posting texts are replaced
    at each company's next fetch."""
    from datetime import UTC, datetime

    from . import sync
    from . import prefilter
    from .sources.base import repair_mojibake
    from .state import State, read_jsonl, write_jsonl

    paths = cfg.Paths.from_env()
    state_dir = (paths.root / args.state).resolve()
    with sync.state_lock(state_dir, holder="the text repair"):
        if args.apply and sync.is_worktree(state_dir):
            sync.pull_locked(state_dir)
        state = State(state_dir)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        fixed: dict[str, int] = {}
        scored = suffixes = 0
        for record in state.jobs.values():
            changed = False
            for field in ("title", "company", "department"):
                if isinstance(record.get(field), str) and repair_mojibake(record[field]) != record[field]:
                    record[field] = repair_mojibake(record[field])
                    changed = True
            tidy = prefilter.tidy_title(record["title"], record["company"], record.get("locations") or [])
            if tidy != record["title"] and not record["closed"]:  # "… | SEB, Vilnius" (2026-11-19)
                record["title"] = tidy
                record["updated_at"] = now
                suffixes += 1
            for field in ("locations", "merged_locations"):
                if isinstance(record.get(field), list):
                    new = [repair_mojibake(x) if isinstance(x, str) else x for x in record[field]]
                    if new != record[field]:
                        record[field] = new
                        changed = True
            if changed:
                record["updated_at"] = now
                fixed[record["source"]] = fixed.get(record["source"], 0) + 1
                scored += any(state.scores.get(f"{record['key']}@{m}") for m in ("haiku", "sonnet"))
        generated = read_jsonl(state_dir / "companies_generated.jsonl")
        names = 0
        for entry in generated:
            if isinstance(entry.get("name"), str) and repair_mojibake(entry["name"]) != entry["name"]:
                entry["name"] = repair_mojibake(entry["name"])
                names += 1
        print(f"Records to repair: {sum(fixed.values())} ({', '.join(f'{k} {v}' for k, v in sorted(fixed.items())) or 'none'}); "
              f"{scored} of them scored (their text is replaced at the next fetch, so they are scored again); "
              f"{names} company names on the generated list; {suffixes} open roles' titles lose a page-title "
              "leftover (scores are kept: the posting text is the same).")
        if not args.apply:
            print("Nothing written: add --apply to repair them.")
            return 0
        state.save()
        if names:
            write_jsonl(state_dir / "companies_generated.jsonl", generated)
        if sync.is_worktree(state_dir):
            result = sync.push_locked(state_dir, "Repair stored text: Latin-1 mix-ups and page-title leftovers in titles")
            print("Pushed." if result.pushed else (result.note or "Nothing to push."))
    return 0


def cmd_hidden(args: argparse.Namespace) -> int:
    """List the roles removed after being hidden for [tracker] hidden_days, or restore some."""
    from . import catalog as cat
    from . import store

    paths = cfg.Paths.from_env()
    conn = store.connect(paths.database)
    try:
        default = int(cfg.load_settings(paths.config).get("tracker", {}).get("hidden_days", 7))
        store.expire_hidden(conn, int(store.preference(conn, "hidden_days", default)))  # the Settings page's choice
        for key in args.restore or []:
            if store.restore_removed(conn, key):
                print(f"Restored {key}: it is on the Jobs pages again.")
            else:
                print(f"{key} is not a removed role (see `jobhunt hidden`).", file=sys.stderr)
                return 1
        if args.restore:
            return 0
        removed = store.removed_marks(conn)
    finally:
        conn.close()
    names = cat.Catalog(paths.state_dir, paths.config, {}, paths.database.with_name("catalog.db")).titles(list(removed))
    if not removed:
        print("No removed roles.")
    for key, mark in removed.items():
        company, title = names.get(key, ("?", "(not in the catalogue)"))
        print(f"{mark.removed_at[:10]}  {company}: {title}\n            {key}")
    if removed:
        print("\nRestore one with: jobhunt hidden --restore '<key>'")
    return 0


def cmd_wishlist(args: argparse.Namespace) -> int:
    """List the wishlist (on the data branch), or move companies.toml's wishlist flags into it."""
    from . import wishlist

    paths = cfg.Paths.from_env()
    state_dir = (paths.root / args.state).resolve()
    if args.import_toml:
        moved = wishlist.import_toml(paths.config, state_dir)
        print(f"Moved {len(moved)} companies from companies.toml to the wishlist: {', '.join(moved) or 'none'}.")
        if moved:
            print("Push it with `jobhunt sync --push` so the scheduled runs see it, and commit companies.toml.")
    for entry in wishlist.active(state_dir):
        board = entry["identity"] or "no board: star only"
        print(f"★ {entry['name']:24} {board}")
    return 0


def mark_companies(args: argparse.Namespace, settings: dict, state_dir) -> int:
    """`backlog --company NAME --score-with sonnet|cascade [--apply]`: a company's skipped roles."""
    from datetime import UTC, datetime

    from . import pipeline, sync
    from .state import State

    share = settings.get("usage", {}).get("share_per_call", {})
    scheduled, backlog = pipeline.models(settings)
    with sync.state_lock(state_dir, holder="the backlog command"):
        state = State(state_dir) if args.apply else State.read_only(state_dir)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        total = 0.0
        for name in args.company:
            counts = pipeline.mark_company(state, name, args.score_with, now, [scheduled, backlog])
            roles = counts.get("skipped, now eligible", 0) + counts.get("eligible already", 0)
            entry = counts.get("graduate wording", 0)
            if args.score_with == "sonnet":
                cost = roles * share.get(scheduled, 0.004)
            else:  # Haiku on all, then Sonnet for the share Haiku sends on (measured on night 2)
                sonnet_calls = entry * pipeline.TO_SONNET[True] + (roles - entry) * pipeline.TO_SONNET[False]
                cost = roles * share.get(backlog, 0.0012) + sonnet_calls * share.get(scheduled, 0.004)
            total += cost
            print(f"{name}: {counts} -> {args.score_with}, about {cost:.2f} of a 5-hour window")
        print(f"Together about {total:.2f} of a window.")
        if args.apply:
            state.save()
            print("Marked. A backfill run scores them: \"sonnet\" roles with Sonnet directly, \"cascade\" roles "
                  "with Haiku first and Sonnet for 6+.")
        else:
            print("Nothing changed: add --apply to mark them.")
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    """Report what the backlog rule adds to the generated list's backlog (and, with --apply,
    make those roles eligible). Never scores anything itself."""
    from datetime import UTC, datetime

    from . import pipeline, sync

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    state_dir = (paths.root / args.state).resolve()
    usage = settings.get("usage", {})
    per_week = float(usage.get("windows_per_week", 12.5))
    per_night = float(usage.get("backlog_window_share", 0.4))
    if args.company:
        return mark_companies(args, settings, state_dir)
    if args.reapply:
        return reapply_rules(args, settings, state_dir)
    with sync.state_lock(state_dir, holder="the backlog command"):
        state = _state(args, paths)
        plan = pipeline.backlog_plan(state, settings, datetime.now(UTC).date(), other_days=args.other_days,
                                     entry_days=args.entry_days)
        windows = [f"entry-level {args.entry_days or settings.get('generated', {}).get('backfill_days_entry_level', 20)} days or undated",
                   f"other {args.other_days or settings.get('generated', {}).get('backfill_days_other', 14)} days"]
        print(f"Backlog rule: {'; '.join(windows)}")
        total = {"roles": 0, "title_check_keeps": 0, "haiku_calls": 0, "sonnet_calls": 0, "windows": 0.0}
        print(f"{'part':12} {'roles':>6} {'text now':>8} {'kept':>6} {'Haiku':>6} {'Sonnet':>6} {'windows':>8} "
              f"{'week':>6} {'nights':>7}")
        for name, part in plan["parts"].items():
            for k in total:
                total[k] += part[k]
            print(f"{name:12} {part['roles']:6} {part['with_text']:8} {part['title_check_keeps']:6} "
                  f"{part['haiku_calls']:6} {part['sonnet_calls']:6} {part['windows']:8.2f} "
                  f"{part['windows'] / per_week:6.0%} {part['windows'] / per_night:7.1f}")
        print(f"{'total':12} {total['roles']:6} {'':8} {total['title_check_keeps']:6} {total['haiku_calls']:6} "
              f"{total['sonnet_calls']:6} {total['windows']:8.2f} {total['windows'] / per_week:6.0%} "
              f"{total['windows'] / per_night:7.1f}")
        print(f"(week: share of the weekly limit, about {per_week:g} windows; nights: backlog runs at "
              f"{per_night:.0%} of a window each)")
        if args.apply:
            args.part = args.part or ["entry-level", "other"]
            chosen = [r for name, records in plan["records"].items() if name in args.part for r in records]
            now = datetime.now(UTC).isoformat(timespec="seconds")
            count = pipeline.apply_backlog(state, chosen, now)
            state.save()
            print(f"\nMarked {count} roles ({', '.join(args.part)}) for scoring; backlog runs score them.")
    return 0

NEW_ENTRY_TERMS = ("afstudeerstage", "afstudeeropdracht", "starter", "werkstudent", "einsteiger", "alternance", "tirocinio")


def reapply_rules(args: argparse.Namespace, settings: dict, state_dir) -> int:
    """The backlog rule as it is now, for generated roles already stored and never scored: the main
    countries' wider window, and the entry-level words added on 2026-10-29 (recent roles only).
    Reports by default; --apply writes and pushes, and scheduled runs score them a share at a time."""
    from datetime import UTC, datetime

    from . import pipeline, sync
    from .state import State

    with sync.state_lock(state_dir, holder="the backlog command"):
        if args.apply and sync.is_worktree(state_dir):
            sync.pull_locked(state_dir)
        state = State(state_dir) if args.apply else State.read_only(state_dir)
        now = datetime.now(UTC)
        counts = pipeline.apply_rule_changes(state, settings, now.date(), now.isoformat(timespec="seconds"),
                                             NEW_ENTRY_TERMS)
        for key in ("skipped before, scored now", "new word, recent: scored", "new word, older: left out",
                    "main countries, no graduate wording, recent: scored",
                    "main countries, no graduate wording, recent: already in the backlog",
                    "main countries, no graduate wording, older: left out"):
            print(f"{key:52} {counts.get(key, 0):6}")
        words = {k[5:]: v for k, v in counts.items() if k.startswith("word ")}
        print("by new word:", ", ".join(f"{w} {n}" for w, n in sorted(words.items(), key=lambda kv: -kv[1])) or "none")
        if not args.apply:
            print("Nothing changed: add --apply to write and push.")
            return 0
        state.save()
        if sync.is_worktree(state_dir):
            sync.push_locked(state_dir, "Backlog rule re-applied (main countries, new entry-level words)")
            print("Written and pushed: scheduled runs score them over the next runs.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description="Firstlight: finds, scores and tracks graduate and junior engineering roles")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="start the local web app")
    serve.add_argument("--port", type=int, default=None, help=f"default {PORT}, or {DEMO_PORT} with --demo")
    serve.add_argument("--demo", action="store_true",
                       help="run on a made-up candidate with recorded scores (no Claude, no real data)")
    serve.set_defaults(handler=cmd_serve)

    leak = commands.add_parser("leakcheck", help="fail if candidate facts appear outside config/")
    leak.set_defaults(handler=cmd_leakcheck)

    verify = commands.add_parser("verify", help="check each configured board answers, and record the date")
    verify.add_argument("names", nargs="*", help="only these companies (default: all with a board)")
    verify.add_argument("--list", action="store_true", help="print every job title found")
    verify.add_argument("--dry-run", action="store_true", help="do not write verified dates")
    verify.set_defaults(handler=cmd_verify)

    det = commands.add_parser("detect", help="recognise the job board behind a careers page")
    det.add_argument("url", nargs="?", help="careers page")
    det.add_argument("--name", help="company name: used in the config block, and to probe public "
                                    "boards when the page shows none")
    det.set_defaults(handler=cmd_detect)

    run = commands.add_parser("run", help="fetch every board, pre-filter, score new roles")
    run.add_argument("names", nargs="*", help="only these companies (fetched and scored)")
    run.add_argument("--state", default=".data", help="state folder (default .data)")
    run.add_argument("--backfill", action="store_true",
                     help="score the backlog: backlog_model first, then the scheduled model for promising roles")
    run.add_argument("--local-only", action="store_true", help='only companies with fetch_from = "local"')
    run.add_argument("--no-score", action="store_true", help="fetch and pre-filter only")
    run.add_argument("--no-fetch", action="store_true", help="score pending roles without fetching")
    run.add_argument("--generated", choices=["due", "all", "none"], default="due",
                     help="generated-list companies to fetch: this run's slot (default), all of them, or none")
    run.add_argument("--cap", type=int,
                     help="maximum scoring calls per phase (default: [scoring] max_calls_per_run, "
                          "or none when --budget is given)")
    run.add_argument("--no-sync", action="store_true", help="do not pull or push the data branch")
    run.add_argument("--budget", type=float,
                     help="share of a 5-hour window this run may use, e.g. 0.6 (default: [usage] in settings)")
    run.add_argument("--parallel", type=int, help="scoring calls at once")
    run.add_argument("--max-pages", type=int,
                     help="posting pages a custom site may fetch this run (default [fetching] custom_max_links)")
    run.add_argument("--ignore-weekly-warning", action="store_true",
                     help="this run only: a warning about the seven-day limit does not stop scoring (a refusal, "
                          "a five-hour warning and --budget still do)")
    run.add_argument("--sonnet-only", action="store_true",
                     help="only rescore roles the backlog model rated 6+ with the scheduled model "
                          "(no fetching, no new backlog scoring); default budget as --backfill")
    run.set_defaults(handler=cmd_run)

    rescore = commands.add_parser("rescore", help="re-score roles scored with an older prompt version")
    rescore.add_argument("--state", default=".data")
    rescore.add_argument("--cap", type=int)
    rescore.add_argument("--parallel", type=int)
    rescore.add_argument("--queue", action="store_true",
                         help="mark roles for the scheduled runs to rescore a few at a time, instead of now")
    rescore.add_argument("--matching", help="with --queue: roles whose score's text matches this (a regular expression)")
    rescore.add_argument("--matching-min-fit", type=int, default=0, help="with --matching: only at this fit or more")
    rescore.add_argument("--min-fit", type=int, help="with --queue: also every role at this fit or more")
    rescore.add_argument("--apply", action="store_true", help="with --queue: write the marks and push")
    rescore.set_defaults(handler=cmd_rescore)

    score = commands.add_parser("score", help="score one posting saved as a text file")
    score.add_argument("file")
    score.add_argument("--title")
    score.add_argument("--company")
    score.add_argument("--location")
    score.set_defaults(handler=cmd_score)

    short = commands.add_parser("shortlist", help="list scored roles worth acting on (Sonnet fit 7+, apply_now)")
    short.add_argument("--min-fit", type=int, default=7)
    short.add_argument("--action", action="append", choices=["apply_now", "apply_state_availability",
                                                              "contact_recruiter_about_later_intake",
                                                              "watch_for_reopening", "skip"],
                       help="actions to include (repeatable; default apply_now)")
    short.add_argument("--csv", help="also write a CSV snapshot, e.g. exports/shortlist.csv")
    short.add_argument("--state", default=".data")
    short.add_argument("--include-applied", action="store_true", help="also show roles you already applied to")
    short.set_defaults(handler=cmd_shortlist)

    sy = commands.add_parser("sync", help="pull the data branch into the state folder (or --init, --push)")
    sy.add_argument("--init", action="store_true", help="link the state folder to the data branch (once)")
    sy.add_argument("--push", action="store_true", help="push local state changes")
    sy.add_argument("--message", default="Sync local state", help="commit message for --push")
    sy.add_argument("--state", default=".data")
    sy.set_defaults(handler=cmd_sync)

    st = commands.add_parser("selftest", help="check claude -p (and with --alert, ntfy) work, e.g. in Actions")
    st.add_argument("--alert", action="store_true", help="also send one test notification to NTFY_TOPIC")
    st.set_defaults(handler=cmd_selftest)

    bl = commands.add_parser("backlog", help="report (or --apply) what the backlog rule adds; never scores")
    bl.add_argument("--apply", action="store_true", help="mark the roles for scoring (backlog runs score them)")
    bl.add_argument("--part", action="append", choices=["entry-level", "other"],
                    help="with --apply: only these parts (repeatable; default both)")
    bl.add_argument("--other-days", type=int, help="window for roles without entry-level wording (default: settings)")
    bl.add_argument("--entry-days", type=int, help="window for entry-level roles (default: settings)")
    bl.add_argument("--company", action="append",
                    help="instead: a company's roles not scored yet, including those its first fetch skipped")
    bl.add_argument("--score-with", choices=["sonnet", "cascade"], default="cascade",
                    help="with --company: Sonnet directly, or Haiku first then Sonnet for 6+ (default)")
    bl.add_argument("--reapply", action="store_true",
                    help="instead: apply the current rule (main countries, new words) to stored roles never scored")
    bl.add_argument("--state", default=".data")
    bl.set_defaults(handler=cmd_backlog, part=None)

    stt = commands.add_parser("status", help="roles waiting at each step, backlog progress, this week's usage")
    stt.add_argument("--pull", action="store_true", help="pull the data branch first")
    stt.add_argument("--state", default=".data")
    stt.set_defaults(handler=cmd_status)

    ml = commands.add_parser("mail", help="job alert emails from one Gmail label (read-only, laptop only)",
                             description=MAIL_SETUP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ml.add_argument("action", choices=["setup", "auth", "fetch"], help="setup: show the steps; auth: sign in once; fetch: read new alerts")
    ml.add_argument("--no-score", action="store_true", help="add the roles without scoring them")
    ml.add_argument("--state", default=".data")
    ml.set_defaults(handler=cmd_mail)

    ld = commands.add_parser("launchd", help="macOS: run --local-only at 08:00 and 20:00 (and after a wake)")
    ld.add_argument("action", choices=["install", "token", "status", "uninstall"])
    ld.set_defaults(handler=cmd_launchd)

    rt = commands.add_parser("repair-text", help="repair stored text that was UTF-8 read as Latin-1")
    rt.add_argument("--apply", action="store_true", help="write the repairs and push them")
    rt.add_argument("--state", default=".data")
    rt.set_defaults(handler=cmd_repair_text)

    hd = commands.add_parser("hidden", help="list roles removed after being hidden, or --restore them")
    hd.add_argument("--restore", action="append", metavar="KEY", help="bring a removed role back (repeatable)")
    hd.set_defaults(handler=cmd_hidden)

    wl = commands.add_parser("wishlist", help="list the wishlist (edited in the app)")
    wl.add_argument("--import-toml", action="store_true", help="move wishlist = true flags from companies.toml (once)")
    wl.add_argument("--state", default=".data")
    wl.set_defaults(handler=cmd_wishlist)

    hv = commands.add_parser("harvest", help="build the generated company list from the Common Crawl index")
    hv.add_argument("--crawls", type=int, default=3, help="how many of the latest crawls to read (default 3)")
    hv.add_argument("--crawl", action="append", help="a specific crawl id (repeatable), e.g. CC-MAIN-2026-43")
    hv.add_argument("--state", default=".data")
    hv.add_argument("--recheck-days", type=int, default=25, help="re-check accounts checked longer ago than this")
    hv.add_argument("--skip-excluded", action="store_true",
                    help="do not check boards the generated list excludes ([generated] exclude_boards)")
    hv.add_argument("--recheck-errors", action="store_true",
                    help="repeat checks that failed for a temporary reason (rate limit, server error, timeout)")
    hv.add_argument("--limit-per-board", type=int, help="check at most this many new accounts per board (a batch)")
    hv.add_argument("--board", action="append", help="check only this board (repeatable), e.g. --board workable")
    hv.add_argument("--skip-board", action="append", help="do not check this board (repeatable)")
    hv.set_defaults(handler=cmd_harvest)

    tc = commands.add_parser("titlecheck", help="measure the batch title check on roles already scored")
    tc.add_argument("--validate", action="store_true", required=True,
                    help="screen the scored roles and calibration roles and report what would be dropped")
    tc.add_argument("--state", default=".data")
    tc.add_argument("--parallel", type=int)
    tc.add_argument("--no-department", action="store_true", help="leave departments out (as for light listings)")
    tc.set_defaults(handler=cmd_titlecheck)

    hs = commands.add_parser("harvest-sample", help="re-check a random sample of generated accounts, role by role")
    hs.add_argument("--per-board", type=int, default=300)
    hs.add_argument("--board", action="append", help="only this board (repeatable); written to its own file")
    hs.add_argument("--state", default=".data")
    hs.set_defaults(handler=cmd_harvest_sample)

    ev = commands.add_parser("eval", help="score the calibration roles and report agreement")
    ev.add_argument("--runs", type=int, default=3, help="scores per role (default 3)")
    ev.add_argument("--parallel", type=int)
    ev.add_argument("--state", default=".data", help="where full results are saved (under eval/)")
    ev.add_argument("--model", help="override the scoring model (e.g. sonnet), to compare")
    ev.add_argument("--thinking", type=int, help="override max_thinking_tokens (0 = off), to compare")
    ev.set_defaults(handler=cmd_eval)
    return parser


MAIL_SETUP = """Setup (once, on this laptop):
 1. Google Cloud console (console.cloud.google.com): create a project, enable the Gmail API.
 2. OAuth consent screen: External, testing mode, add your own Gmail address as a test user.
 3. Credentials: create an OAuth client ID of type "Desktop app"; download its JSON as
    .gmail/client_secret.json in this folder (git-ignored).
 4. In Gmail: create the label named in [mail] label (default "Job alerts"), sign up for the job
    alerts of the companies the tool cannot read, and add a filter that files them under it.
 5. uv run jobhunt mail auth   (read-only access; the token stays in .gmail/)
Then: uv run jobhunt mail fetch"""


def cmd_mail(args: argparse.Namespace) -> int:
    """Job alert emails from one Gmail label (read-only): sign in once, then fetch."""
    from datetime import UTC, datetime

    from . import companies as co
    from . import mail, pipeline, scoring, sync
    from .state import State

    paths = cfg.Paths.from_env()
    settings = cfg.load_settings(paths.config)
    ms = mail.Settings.from_settings(settings, paths.root)
    try:
        if args.action == "auth":
            mail.authorise(ms)
            return 0
        if args.action == "setup":
            print(MAIL_SETUP)
            return 0
        token = mail.access_token(ms)
    except mail.MailError as e:
        print(f"{e}\n\n{MAIL_SETUP}" if "auth" in str(e) or "client" in str(e).lower() else e)
        return 1
    state_dir = (paths.root / args.state).resolve()
    gmail = mail.Gmail(token)
    companies = co.load_companies(paths.config)
    with sync.state_lock(state_dir, holder="the job alert emails"):
        if sync.is_worktree(state_dir):
            sync.pull_locked(state_dir)
        try:
            found, read = mail.read_alerts(gmail, ms, mail.known_sites(companies))
        except mail.MailError as e:
            print(e)
            return 1
        state = State(state_dir)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        allowed = set(settings.get("locations", {}).get("allowed_countries", []))
        added = 0
        for name, jobs in found.by_company.items():
            company = co.Company(name=name, board="email", partial=True,
                              note="roles from its job alert emails (provisional)")
            counts = pipeline.update_jobs(state, pipeline.Fetched(company, jobs), allowed, now)
            added += counts.get("new", 0)
            print(f"{name}: {len(jobs)} roles in its alerts, {counts.get('new', 0)} new, "
                  f"{counts.get('passed', 0)} pass the pre-filter")
        print(f"{found.new_messages} new of {found.messages} messages under \"{ms.label}\"; {added} new roles.")
        if not args.no_score:
            pipeline.use_account(settings)
            guard = scoring.UsageGuard.from_settings(settings, "scheduled")
            prompt = scoring.build_prompt(paths.config, settings, model=pipeline.models(settings)[0])
            stats = pipeline.score_pending(
                state, prompt, lambda r: r.get("source") == mail.SOURCE and pipeline.unscored(
                    state, list(dict.fromkeys(pipeline.models(settings))), prompt.model)(r),
                cap=100, parallel=4, runner=scoring.run_subprocess,
                timeout=float(settings.get("scoring", {}).get("call_timeout_seconds", 180)), guard=guard,
                label="job alert emails")
            print(f"Scored {stats.get('calls', 0)} roles (provisional: from title and email text only).")
        state.save()
        if sync.is_worktree(state_dir):
            sync.push_locked(state_dir, f"Job alert emails: {found.new_messages} messages, {added} new roles")
    mail.remember(ms, read)  # after the push: a failed run reads the same messages again
    return 0


def cmd_launchd(args: argparse.Namespace) -> int:
    """The laptop's scheduled `run --local-only` (macOS launchd), twice a day and after a wake."""
    from . import launchd

    root = cfg.Paths.from_env().root
    try:
        if args.action == "install":
            path = launchd.install(root)
            print(f"Installed {path}: uv run jobhunt run --local-only at 08:00 and 20:00, and after a wake "
                  f"when one was missed. Output: {root / launchd.LOG}.")
        elif args.action == "token":
            print("The laptop's scheduled runs use one fixed Claude account: the token you paste here.\n"
                  "Make one with `claude setup-token`, signing in with the account the scheduled runs should use\n"
                  "(the one named in [accounts] token; your own `claude` login is not changed). It is kept in\n"
                  f"the login Keychain as \"{launchd.KEYCHAIN_SERVICE}\", never in a file.")
            if not launchd.store_token():
                print("Not stored.", file=sys.stderr)
                return 1
            print("Stored: the next launchd run uses it.")
        elif args.action == "uninstall":
            print("Removed." if launchd.uninstall() else "Not installed.")
        else:
            print(launchd.status(root))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
