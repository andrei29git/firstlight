"""Ctrl+C during a run: the calls in progress finish, everything made so far is saved, and a
second Ctrl+C quits at once."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from jobhunt import interrupt, pipeline, scoring
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .fakes import FakeClaude, fake_http
from .test_generated import NOW, answer, by_title, routes, settings  # noqa: F401 (fixture)
from .test_wishlist import WISHED_ACME


@pytest.fixture(autouse=True)
def no_stop_left_behind():
    interrupt.STOP.clear()
    yield
    interrupt.STOP.clear()


def run(tmp_path, settings, claude):  # noqa: F811
    http, session = fake_http(routes())
    summary = pipeline.run(EXAMPLE_CONFIG, settings, [WISHED_ACME], State(tmp_path / "state"),
                           pipeline.Options(parallel=1), make_http=lambda: http, runner=claude, now=NOW)
    return summary, session


def test_ctrl_c_while_scoring_finishes_the_call_and_saves_it(tmp_path, settings):  # noqa: F811
    def pressed_during_the_call(message, argv):
        interrupt.STOP.set()  # Ctrl+C arrives while this call is running
        return answer(message, argv)

    claude = FakeClaude(respond_argv=pressed_during_the_call)
    summary, _ = run(tmp_path, settings, claude)
    assert len(claude.calls) == 1  # three roles were waiting: one call made, none started after
    ok = sum(p.get("ok", 0) for p in summary["scoring"].values())
    assert ok == 1 and summary["scoring"]["scheduled"]["stopped"] == "stopped by Ctrl+C"
    assert summary["interrupted"].startswith("stopped by Ctrl+C")
    saved = State(tmp_path / "state")  # on disk, as the push would send it
    assert sum(1 for s in saved.scores.values() if s.get("result")) == 1
    assert len(saved.jobs) == 4 and saved.runs()[-1]["interrupted"]
    assert not any(s.get("attempts") for s in saved.scores.values())  # nothing counted against the roles


def test_ctrl_c_before_fetching_starts_nothing_new(tmp_path, settings):  # noqa: F811
    interrupt.STOP.set()
    claude = FakeClaude(respond_argv=answer)
    summary, session = run(tmp_path, settings, claude)
    assert session.calls == [] and claude.calls == [] and summary["interrupted"]
    assert State(tmp_path / "state").runs()  # the run is still recorded


def test_claude_runs_in_its_own_session(tmp_path):
    """So the terminal's Ctrl+C reaches only the tool, never a call half-way."""
    code, out, _ = scoring.run_subprocess(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"], "", str(tmp_path), 30,
        dict(os.environ))
    assert code == 0 and out.strip() == "True"


def test_first_ctrl_c_asks_to_stop_and_the_second_quits(tmp_path):
    script = textwrap.dedent("""
        import os, signal, time
        from jobhunt import interrupt
        with interrupt.graceful():
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)
            print("still running, stop asked:", interrupt.stopping(), flush=True)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(5)
            print("not reached", flush=True)
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
                          cwd=os.getcwd())
    assert done.returncode == 130
    assert done.stdout.strip() == "still running, stop asked: True"
    assert "finishing the calls in progress" in done.stderr and "Second Ctrl+C" in done.stderr


def test_ctrl_c_behaves_normally_outside_a_run():
    with interrupt.graceful():
        interrupt.STOP.set()
    assert not interrupt.stopping()  # cleared on the way out
    import signal
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
