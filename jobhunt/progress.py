"""Progress for long commands: the current phase, done out of total, elapsed time, usage budget.

In a terminal it is one live line, redrawn at most 10 times a second. Anywhere else (GitHub
Actions, a log file) it prints a plain line when a phase starts and ends, and every
`interval` seconds in between. No dependencies.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, TextIO

Budget = Callable[[], tuple[float, float]]  # (share of a 5-hour window spent, share allowed)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class Progress:
    def __init__(self, stream: TextIO | None = None, live: bool | None = None, interval: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.stream = stream or sys.stderr
        self.live = self.stream.isatty() if live is None else live
        self.interval = interval
        self.clock = clock
        self.started = clock()
        self._lock = threading.Lock()
        self._phase = ""
        self._total: int | None = None
        self._done = 0
        self._phase_started = self.started
        self._budget: Budget | None = None
        self._last_draw = 0.0
        self._last_plain = 0.0
        self._drawn = False

    # --- What callers use ----------------------------------------------------------------------

    def phase(self, name: str, total: int | None = None, budget: Budget | None = None) -> None:
        """Start a phase (fetching, title check, Haiku, Sonnet, sync...). Ends the previous one."""
        with self._lock:
            if self._phase:
                self._finish_phase()
            self._phase, self._total, self._done = name, total, 0
            self._phase_started = self.clock()
            self._budget = budget
            if self.live:
                self._draw(force=True)
            else:
                self._plain("started")

    def advance(self, n: int = 1) -> None:
        with self._lock:
            self._done += n
            if self.live:
                self._draw()
            elif self.clock() - self._last_plain >= self.interval:
                self._plain()

    def log(self, message: str) -> None:
        """A message that stays on screen (the live line is redrawn under it)."""
        with self._lock:
            if self.live and self._drawn:
                self.stream.write("\r\x1b[2K")
            self.stream.write(message + "\n")
            if self.live and self._phase:
                self._draw(force=True)
            self.stream.flush()

    def done(self) -> None:
        with self._lock:
            if self._phase:
                self._finish_phase()
            self._phase = ""

    # --- Drawing -------------------------------------------------------------------------------

    def _status(self) -> str:
        parts = [self._phase]
        if self._total:
            parts.append(f"{self._done}/{self._total} ({100 * self._done // max(1, self._total)}%)")
        elif self._done:
            parts.append(f"{self._done} done")
        parts.append(f"{_duration(self.clock() - self._phase_started)} (total {_duration(self.clock() - self.started)})")
        if self._budget:
            spent, allowed = self._budget()
            limit = f" of {allowed:.0%}" if allowed != float("inf") else ""
            parts.append(f"budget {spent:.1%}{limit} of a 5-hour window")
        return " · ".join(parts)

    def _bar(self, width: int = 20) -> str:
        if not self._total:
            return ""
        filled = min(width, width * self._done // max(1, self._total))
        return "[" + "█" * filled + "·" * (width - filled) + "] "

    def _draw(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_draw < 0.1:
            return
        self._last_draw = now
        self.stream.write("\r\x1b[2K" + self._bar() + self._status())
        self.stream.flush()
        self._drawn = True

    def _plain(self, what: str = "") -> None:
        self._last_plain = self.clock()
        self.stream.write(f"[{time.strftime('%H:%M:%S')}] {self._status()}{(' ' + what) if what else ''}\n")
        self.stream.flush()

    def _finish_phase(self) -> None:
        if self.live:
            self._draw(force=True)
            self.stream.write("\n")
            self._drawn = False
        else:
            self._plain("finished")
        self.stream.flush()


class Silent(Progress):
    """For tests and library callers: records nothing, prints nothing."""

    def __init__(self) -> None:
        super().__init__(stream=None, live=False)

    def phase(self, name: str, total: int | None = None, budget: Budget | None = None) -> None:
        pass

    def advance(self, n: int = 1) -> None:
        pass

    def log(self, message: str) -> None:
        pass

    def done(self) -> None:
        pass
