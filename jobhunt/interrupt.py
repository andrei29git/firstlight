"""Ctrl+C during a long command: stop cleanly the first time, at once the second.

The first Ctrl+C sets STOP. Nothing new starts after that (no company fetch, no scoring call),
the calls in progress finish, and the command saves and pushes what it has as usual. Claude runs
in its own session, so the terminal's Ctrl+C does not reach it and kill a call half-way.
The second Ctrl+C stops the Claude processes and exits at once (exit code 130); anything not yet
saved is lost.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from typing import Iterator

STOP = threading.Event()
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def stopping() -> bool:
    return STOP.is_set()


def track(process: subprocess.Popen) -> None:
    with _children_lock:
        _children.add(process)


def untrack(process: subprocess.Popen) -> None:
    with _children_lock:
        _children.discard(process)


def kill_children() -> None:
    with _children_lock:
        for process in list(_children):
            with contextlib.suppress(OSError):
                process.kill()


def _say(message: str) -> None:
    # Written directly: the handler may run while the progress line holds its own lock.
    sys.stderr.write("\r\x1b[2K" + message + "\n" if sys.stderr.isatty() else message + "\n")
    sys.stderr.flush()


@contextlib.contextmanager
def graceful(what: str = "the calls in progress") -> Iterator[threading.Event]:
    """Within this block, Ctrl+C asks for a clean stop instead of raising KeyboardInterrupt."""
    if threading.current_thread() is not threading.main_thread():
        yield STOP
        return

    def handler(signum, frame) -> None:
        if STOP.is_set():
            _say("Second Ctrl+C: stopping now. Scores not saved yet are lost.")
            kill_children()
            os._exit(130)
        STOP.set()
        _say(f"Ctrl+C: finishing {what}, then saving and pushing. Press Ctrl+C again to quit at once.")

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield STOP
    finally:
        signal.signal(signal.SIGINT, previous)
        STOP.clear()
