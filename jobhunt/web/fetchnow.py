""""Fetch and score now" for a company just added to the wishlist (2026-10-29).

Both steps are ordinary named runs on this laptop (`jobhunt run NAME --no-score`, then
`jobhunt run NAME --no-fetch --budget B`) in a background process, so the data lock, the sync, the
filters and the usage guard behave exactly as in any run. The app polls a task's state; the
scoring step starts only when the user confirms, after seeing how many roles wait and the estimated
usage.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROGRESS = re.compile(r"^\[\d\d:\d\d:\d\d\]\s+(.*)$")


@dataclass
class Task:
    company: str  # the name runs match on
    company_id: str
    stage: str = "fetching"  # fetching, fetched, scoring, done, error
    started: float = field(default_factory=time.monotonic)
    line: str = ""  # the run's latest progress line
    error: str = ""
    counts: dict[str, Any] = field(default_factory=dict)  # after the fetch: found, passing, waiting, estimate
    results: dict[str, Any] = field(default_factory=dict)  # after scoring: scored, fits

    @property
    def seconds(self) -> int:
        return int(time.monotonic() - self.started)


class Runner:
    """At most one task per company; each step is a `jobhunt run` in its own process."""

    def __init__(self, root: Path, env: dict[str, str] | None = None,
                 command: Callable[[list[str]], list[str]] | None = None):
        self.root = root
        self.env = env or {}
        self.command = command or (lambda args: [sys.executable, "-c", "from jobhunt.cli import main; main()", *args])
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()

    def get(self, company_id: str) -> Task | None:
        with self.lock:
            return self.tasks.get(company_id)

    def start_fetch(self, company: str, company_id: str, after: Callable[[Task], None]) -> Task:
        task = Task(company=company, company_id=company_id)
        with self.lock:
            self.tasks[company_id] = task
        self._spawn(task, ["run", company, "--no-score"], "fetched", after)
        return task

    def start_scoring(self, task: Task, budget: float, after: Callable[[Task], None]) -> None:
        task.stage, task.line, task.started = "scoring", "", time.monotonic()
        self._spawn(task, ["run", task.company, "--no-fetch", "--budget", f"{budget:.3f}"], "done", after)

    def _spawn(self, task: Task, args: list[str], next_stage: str, after: Callable[[Task], None]) -> None:
        def work() -> None:
            try:
                process = subprocess.Popen(self.command(args), cwd=self.root, env={**os.environ, **self.env},
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            except OSError as e:
                task.stage, task.error = "error", f"could not start the run: {e}"
                return
            tail: list[str] = []
            for raw in process.stdout or []:
                text = raw.rstrip()
                match = PROGRESS.match(text)
                if match:
                    task.line = match.group(1)
                elif text:
                    tail = (tail + [text])[-6:]
            if process.wait() != 0:
                task.stage, task.error = "error", " ".join(tail)[-400:] or f"the run ended with code {process.returncode}"
                return
            try:
                after(task)
                task.stage = next_stage
            except Exception as e:  # counting from the state failed: still say what happened
                task.stage, task.error = "error", f"{type(e).__name__}: {e}"

        threading.Thread(target=work, daemon=True, name=f"fetch-now {task.company}").start()
