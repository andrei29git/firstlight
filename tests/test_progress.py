from __future__ import annotations

import io

from jobhunt.progress import Progress


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_plain_output_prints_phases_and_every_interval():
    out, clock = io.StringIO(), Clock()
    progress = Progress(stream=out, live=False, interval=30, clock=clock)
    progress.phase("fetching", total=100)
    for _ in range(10):
        clock.now += 5
        progress.advance(10)  # one line once 30 seconds have passed since the last
    progress.phase("scoring: haiku", total=4, budget=lambda: (0.12, 0.4))
    progress.done()
    lines = out.getvalue().splitlines()
    assert lines[0].endswith("fetching · 0/100 (0%) · 0s (total 0s) started")
    assert any("fetching · 60/100 (60%) · 30s" in line for line in lines)
    assert "finished" in lines[-3] and "budget 12.0% of 40% of a 5-hour window" in lines[-1]
    assert "\r" not in out.getvalue()


def test_live_output_redraws_one_line():
    out, clock = io.StringIO(), Clock()
    progress = Progress(stream=out, live=True, clock=clock)
    progress.phase("checking accounts live", total=4)
    for _ in range(4):
        clock.now += 1
        progress.advance()
    progress.log("a message stays")
    progress.done()
    text = out.getvalue()
    assert "\r\x1b[2K[" in text and "4/4 (100%)" in text and "a message stays\n" in text
    assert text.endswith("\n")


def test_unknown_total_counts_up():
    out = io.StringIO()
    progress = Progress(stream=out, live=False, interval=0)
    progress.phase("reading the Common Crawl index (pages)")
    progress.advance()
    assert "1 done" in out.getvalue()
