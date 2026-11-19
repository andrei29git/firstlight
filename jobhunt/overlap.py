"""Scoring while the fetch is still running (`[scoring] score_while_fetching`).

A run fetches thousands of companies (Greenhouse alone takes about 20 minutes, one host at a time),
then scores. This starts the scoring work whose turn is certain to come first, as soon as it is
ready, so that part overlaps the fetch:

1. In a backlog run, Sonnet for roles Haiku rated 6+ in earlier runs (known before the fetch),
   highest Haiku fit first.
2. Roles at my companies (companies.toml and the wishlist), first in every run's order.
3. The title check of generated-list roles, in full batches.
4. The first score of generated-list roles with graduate or entry-level wording, which come
   before every other generated role in the order.

A role is ready only when every company with its name has been fetched and duplicates across
those boards and cities are merged, so a copy is never scored. Everything else waits for the end
of the fetch and the usual phases (`pipeline.score_all`), which pick up what is left in the usual
priority order. All of it shares one usage guard, so the budget is the same as without overlap.
"""

from __future__ import annotations

import heapq
import itertools
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import pipeline, prefilter, scoring, titlecheck
from .state import State

FIRST_6 = "during fetch: fit 6+ from earlier runs"
MINE = "during fetch: your companies and wishlist"
CHECK = "during fetch: generated titlecheck"
ENTRY = "during fetch: generated (entry-level wording)"
# The generated list's other roles that may be scored (every role in the main countries), after the
# entry-level ones (2026-10-31): until then they waited for the few minutes after the fetch, so the
# fetch's time went unused once the entry-level backlog was done.
OTHER = "during fetch: generated (other roles)"
MINE_OTHER = "during fetch: your companies and wishlist (marked for the other model)"
RANK = {FIRST_6: 0, MINE: 1, CHECK: 2, ENTRY: 3, OTHER: 4}


class Overlap:
    def __init__(self, config: Path, settings: dict[str, Any], state: State, options: pipeline.Options,
                 runner: scoring.Runner, rules: pipeline.GeneratedRules, guard: scoring.UsageGuard,
                 fetching: set[str], mine: set[str]):
        """`fetching`: normalised names of the companies this run fetches; their roles wait for
        `ready` (fetched, duplicates merged), the rest can start at once. `mine`: those that are my
        companies; their roles start only when all of them are in, so their order is kept."""
        self.state, self.options, self.runner, self.rules, self.guard = state, options, runner, rules, guard
        self.settings = settings
        scoring_settings = settings.get("scoring", {})
        self.scheduled, self.backlog = pipeline.models(settings)
        self.both = list(dict.fromkeys([self.scheduled, self.backlog]))
        self.main = scoring.build_prompt(config, settings, model=self.scheduled)
        self.cheap = (scoring.build_prompt(config, settings, model=self.backlog)
                      if self.backlog != self.scheduled else self.main)
        self.mine_prompt = self.cheap if options.backlog else self.main
        self.check_prompt, self.profile = titlecheck.build_prompt(config, settings)
        self.batch_size = int(settings.get("titlecheck", {}).get("batch_size", 100))
        self.timeout = float(scoring_settings.get("call_timeout_seconds", 180))
        self.min_fit = int(scoring_settings.get("backlog_rescore_min_fit", 6))
        self.parallel = options.parallel or int(scoring_settings.get("parallel", 4))
        cap = options.cap if options.cap is not None else int(scoring_settings.get("max_calls_per_run", 100))
        self.cap = cap
        self.pool = ThreadPoolExecutor(max_workers=max(1, self.parallel))
        self.queue: list[tuple] = []  # (rank, order..., n, phase, record)
        self.counter = itertools.count()
        self.queued: set[str] = set()
        self.mine_waiting = set(mine)
        self.mine_held: list[tuple[dict[str, Any], tuple]] = []
        self.check_buffer: list[dict[str, Any]] = []
        self.in_flight: dict[Future, tuple[str, Any]] = {}
        self.results: dict[str, list] = {FIRST_6: [], MINE: [], ENTRY: [], OTHER: []}
        self.check_outcome = titlecheck.Outcome()
        self.check_records: list[dict[str, Any]] = []
        self.calls = {phase: 0 for phase in RANK}
        self.busy_seconds = 0.0
        self._busy_since: float | None = None
        self.started = time.monotonic()
        self.earlier = (pipeline.worth_rescoring(state, self.backlog, self.min_fit, self.scheduled,
                                                 lambda r: pipeline.mine(r, self.rules) or self._kept(r))
                        if (options.backlog or options.sonnet_only) and self.backlog != self.scheduled else None)
        # Roles from earlier runs at companies this run does not fetch can start at once, and so can
        # Sonnet for earlier Haiku 6+ (known before the fetch; one that closes now is skipped).
        for record in list(state.jobs.values()):
            if (prefilter.normalise_company(record["company"]) not in fetching
                    or (self.earlier is not None and self.earlier(record))):
                self._consider(record)
        self._release_mine()

    # --- What may start now ----------------------------------------------------------------

    def _generated(self, record: dict[str, Any]) -> bool:
        return pipeline.generated_ok(record, self.rules)

    def _kept(self, record: dict[str, Any]) -> bool:
        return (self._generated(record) and bool(record.get("titlecheck"))
                and not record["titlecheck"]["dropped"])

    def _push(self, phase: str, record: dict[str, Any], order: tuple) -> None:
        if record["key"] in self.queued:
            return
        self.queued.add(record["key"])
        heapq.heappush(self.queue, (RANK[phase], *order, next(self.counter), phase, record))

    def _consider(self, record: dict[str, Any]) -> None:
        """Queue a role if its turn is certain to come before the rest of the fetch's roles."""
        if record["key"] in self.queued:
            return
        if self.earlier is not None and self.earlier(record):
            fit = pipeline.current_score(self.state, record, self.backlog)["result"]["fit"]
            self._push(FIRST_6, record, (-fit, *pipeline.priority(record)))
            return
        if self.options.sonnet_only:
            return
        if pipeline.mine(record, self.rules):
            if self.rules.in_cascade(record):
                return  # a large company of mine: title check and Haiku after the fetch
            model = pipeline.my_first_model(record, self.options.backlog, self.scheduled, self.backlog)
            if model is not None and pipeline.unscored(self.state, self.both, model)(record):
                self.queued.add(record["key"])
                self.mine_held.append((record, pipeline.priority(record)))
            return
        if not self._generated(record):
            return
        if "titlecheck" not in record:
            self.check_buffer.append(record)
            self.queued.add(record["key"])
        elif self._kept(record) and pipeline.unscored(self.state, self.both, self.backlog)(record):
            order = pipeline.priority(record)
            self._push(OTHER if order[0] else ENTRY, record, order)  # graduate or entry-level wording first

    def ready(self, norm: str, records: list[dict[str, Any]]) -> None:
        """A company group is fetched and its duplicates merged: queue what may start now."""
        for record in records:
            self._consider(record)
        self.mine_waiting.discard(norm)
        self._release_mine()

    def _release_mine(self) -> None:
        if self.mine_waiting:
            return  # my companies' roles start together, in priority order
        # Every company of mine is in now: which are large (the cascade, after the fetch) is known.
        self.rules = replace(self.rules, cascade_mine=pipeline.cascade_companies(self.state, self.settings))
        for record, order in self.mine_held:
            if self.rules.in_cascade(record):
                self.queued.discard(record["key"])
                continue
            self.queued.discard(record["key"])
            self._push(MINE, record, order)
        self.mine_held = []

    # --- Running ---------------------------------------------------------------------------

    def step(self) -> None:
        """Record finished calls and start new ones (called often from the run's main loop)."""
        self._collect(block=False)
        while len(self.check_buffer) >= self.batch_size and len(self.in_flight) < self.parallel:
            self._start_check(self.check_buffer[:self.batch_size])
            del self.check_buffer[:self.batch_size]
        while self.queue and len(self.in_flight) < self.parallel and not self.guard.stopped:
            *_, phase, record = heapq.heappop(self.queue)
            if self.calls[phase] >= self.cap:
                continue
            prompt = {FIRST_6: self.main, MINE: self._mine_prompt(record), ENTRY: self.cheap, OTHER: self.cheap}[phase]
            if not record.get("description_hash") or record.get("duplicate_of") or record["closed"]:
                continue
            job = pipeline.job_from_record(record, self.state.read_posting(record["description_hash"]))
            self.calls[phase] += 1
            self._submit(self.pool.submit(self._score, job, prompt), (phase, record))
        self._track_busy()

    def _mine_prompt(self, record: dict[str, Any]) -> scoring.Prompt:
        model = pipeline.my_first_model(record, self.options.backlog, self.scheduled, self.backlog)
        return self.main if model == self.scheduled else self.cheap

    def _start_check(self, records: list[dict[str, Any]]) -> None:
        items = [titlecheck.Item(key=r["key"], company=r["company"], title=r["title"], department=r.get("department"),
                                 locations=tuple(r.get("merged_locations") or r["locations"])) for r in records]
        self.calls[CHECK] += 1
        self._submit(self.pool.submit(titlecheck.check_batch, items, self.check_prompt, self.runner,
                                      300.0, self.guard), (CHECK, (records, items)))

    def _submit(self, future: Future, what: tuple[str, Any]) -> None:
        self.in_flight[future] = what

    def _score(self, job, prompt: scoring.Prompt) -> scoring.Score | None:
        if not self.guard.allow(prompt.model):
            return None
        score = scoring.score_job(job, prompt, self.runner, self.timeout)
        self.guard.observe(score.rate_limit)
        self.guard.record(score, prompt.model)
        return score

    def _collect(self, block: bool) -> None:
        if not self.in_flight:
            return
        done, _ = wait(list(self.in_flight), timeout=None if block else 0, return_when=FIRST_COMPLETED)
        for future in done:
            phase, payload = self.in_flight.pop(future)
            result = future.result()
            if phase == CHECK:
                records, items = payload
                titlecheck.absorb(self.check_outcome, items, result, self.profile)
                if result is None:
                    for record in records:  # refused by the guard: the usual phase checks them later
                        self.queued.discard(record["key"])
                    continue
                single = titlecheck.Outcome()
                titlecheck.absorb(single, items, result, self.profile)
                pipeline.record_titlecheck(self.state, records, single, self.check_prompt.version)
                self.check_records += records
                for record in records:
                    self.queued.discard(record["key"])
                    self._consider(record)  # kept, with entry-level wording: its first score may start
                continue
            if result is None:
                self.queued.discard(payload["key"])
                continue
            pipeline.store_score(self.state, result)
            self.results[phase].append(result)

    def _track_busy(self) -> None:
        now = time.monotonic()
        if self.in_flight and self._busy_since is None:
            self._busy_since = now
        elif not self.in_flight and self._busy_since is not None:
            self.busy_seconds += now - self._busy_since
            self._busy_since = None

    def finish(self) -> dict[str, dict[str, Any]]:
        """The fetch is done: let the calls in progress finish (queued work goes to the usual phases)."""
        while self.in_flight:
            self._collect(block=True)
        self._track_busy()
        self.pool.shutdown()
        phases: dict[str, dict[str, Any]] = {}
        share = self.guard.share_per_call
        mine = self.results[MINE]
        self.results[MINE] = [r for r in mine if r.model == self.mine_prompt.model]
        self.results[MINE_OTHER] = [r for r in mine if r.model != self.mine_prompt.model]
        other_prompt = self.main if self.mine_prompt is self.cheap else self.cheap
        for phase, prompt in ((FIRST_6, self.main), (MINE, self.mine_prompt), (MINE_OTHER, other_prompt),
                              (ENTRY, self.cheap), (OTHER, self.cheap)):
            results = self.results.get(phase, [])
            if results:
                phases[phase] = pipeline.score_stats(prompt, results, len(results), self.guard,
                                                     len(results) * share.get(prompt.model, 0.0),
                                                     self.parallel, self.busy_seconds)
        outcome = self.check_outcome
        if outcome.calls:
            phases[CHECK] = {"model": self.check_prompt.model, "prompt_version": self.check_prompt.version,
                             "titlecheck": True, "pending": len(self.check_records),
                             "checked": len(self.check_records) - len(outcome.unchecked),
                             "dropped": len(outcome.drops), "rejected_ungrounded": len(outcome.rejected),
                             "calls": outcome.calls, "failed": outcome.failed_calls,
                             "cost_usd": round(outcome.cost_usd, 4),
                             "window_share_spent": round(outcome.calls * share.get("titlecheck", 0.0), 4),
                             "stopped": self.guard.stopped, "failures": outcome.failures[:10]}
        return phases
