"""Hosts that ask to be left alone (a Retry-After over a minute) are, until the time has passed, in
runs and harvests alike; a board that refuses again on its longer delay leaves the generated list."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from jobhunt import alerts, harvest, hostwaits, pipeline, runs_view, sync
from jobhunt.companies import Company
from jobhunt.sources import SourceError, fetch, workable
from jobhunt.sources.http import WAITS, HostWaiting
from jobhunt.state import State, read_jsonl, write_jsonl

from .conftest import EXAMPLE_CONFIG
from .fakes import FakeClaude, fake_http, fixture_json, response
from .test_generated import settings  # noqa: F401 (fixture)

LISTING = workable.listing_url("thinkwell")
REFUSED = response(LISTING, 429, "", {"Retry-After": "83944"})
WORKABLE = Company(name="ThinkWell", board="workable", slug="thinkwell", origin="generated",
                   identity="workable:thinkwell")


def test_a_long_retry_after_leaves_the_host_alone_until_then():
    http, session = fake_http({LISTING: REFUSED})
    with pytest.raises(HostWaiting, match=r"asked to wait 23\.3 hours"):
        fetch(WORKABLE, http)
    assert len(session.calls) == 1  # no retries after a minute
    with pytest.raises(HostWaiting, match="asked not to be contacted until"):
        fetch(Company(name="Other", board="workable", slug="other"), http)
    assert len(session.calls) == 1  # nothing sent to the host while it waits
    record = WAITS.records()[0]
    assert record["host"] == "apply.workable.com" and record["retry_after"] == 83944 and not record["slow"]


def test_a_short_retry_after_is_still_waited_out_and_retried():
    http, session = fake_http({LISTING: (response(LISTING, 429, "", {"Retry-After": "30"}),
                                         fixture_json("workable_thinkwell_widget.json"))})
    assert len(fetch(WORKABLE, http, details=lambda job: False)) == 3
    assert 30.0 in http.sleeps and not WAITS.records()


def test_a_host_delay_slows_one_host_only():
    http, _ = fake_http({LISTING: fixture_json("workable_thinkwell_widget.json")},
                        host_delays={"apply.workable.com": 5.0})
    fetch(WORKABLE, http, details=lambda job: False)
    fetch(WORKABLE, http, details=lambda job: False)
    assert http.sleeps and max(http.sleeps) == pytest.approx(5.0, abs=0.1)


def run(tmp_path, settings, companies, routes, **kw):  # noqa: F811
    http, session = fake_http(routes, **kw)
    summary = pipeline.run(EXAMPLE_CONFIG, settings, companies, State(tmp_path / "state"), pipeline.Options(),
                           make_http=lambda: http, runner=FakeClaude())
    return summary, session


def test_runs_skip_a_waiting_host_and_store_new_waits(tmp_path, settings):  # noqa: F811
    summary, session = run(tmp_path, settings, [WORKABLE], {LISTING: REFUSED})
    assert summary["host_waits"]["apply.workable.com"]["new"]
    stored = read_jsonl(tmp_path / "state" / hostwaits.FILE)
    assert stored[0]["host"] == "apply.workable.com"
    notes = runs_view.row(0, summary, {}).notes
    assert any(n.startswith("apply.workable.com asked to wait until") for n in notes)

    WAITS.load([])  # a later run (another process) reads the stored wait
    summary, session = run(tmp_path, settings, [WORKABLE], {LISTING: fixture_json("workable_thinkwell_widget.json")})
    assert session.calls == [] and summary["host_waits"]["apply.workable.com"]["companies_not_fetched"] == 1
    assert not summary["host_waits"]["apply.workable.com"]["new"]


def test_a_wait_that_has_passed_is_ignored(tmp_path, settings):  # noqa: F811
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    (tmp_path / "state").mkdir()
    write_jsonl(tmp_path / "state" / hostwaits.FILE, [{"host": "apply.workable.com", "until": past, "slow": False,
                                                       "updated_at": past}])
    summary, session = run(tmp_path, settings, [WORKABLE], {LISTING: fixture_json("workable_thinkwell_widget.json")})
    assert session.calls and "host_waits" not in summary


def test_a_refusal_on_the_longer_delay_takes_the_board_off_the_generated_list(tmp_path, settings, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(alerts, "post_json", lambda url, payload: sent.append(payload))
    summary, _ = run(tmp_path, settings, [WORKABLE], {LISTING: REFUSED}, host_delays={"apply.workable.com": 5.0})
    assert summary["boards_dropped"]["workable"] and [p["title"] for p in sent] == ["Workable taken off the generated list"]
    assert read_jsonl(tmp_path / "state" / hostwaits.FILE)[0]["notified"]

    # Long after the wait: a generated Workable company is still not fetched; a wishlist one is.
    later = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    records = read_jsonl(tmp_path / "state" / hostwaits.FILE)
    write_jsonl(tmp_path / "state" / hostwaits.FILE, [{**records[0], "until": later}])
    WAITS.load([])
    wished = Company(name="ThinkWell", board="workable", slug="thinkwell", origin="wishlist", wishlist=True)
    routes = {LISTING: fixture_json("workable_thinkwell_widget.json")}
    summary, session = run(tmp_path, settings, [WORKABLE], routes)
    assert session.calls == [] and summary["boards_dropped"]["workable"]["companies_not_fetched"] == 1
    assert len(sent) == 1  # told once
    summary, session = run(tmp_path, settings, [wished], routes)
    assert session.calls  # the wishlist is not affected


def test_the_harvest_stops_checking_a_host_that_asks_to_wait(tmp_path):
    refused = {"board": "workable", "slug": "thinkwell"}
    later = {"board": "workable", "slug": "other"}
    http, session = fake_http({LISTING: REFUSED})
    results = []
    waited = harvest.check_all([refused, later], lambda: http, {"DE"}, date(2026, 10, 27), results.append)
    assert results == [] and waited["apply.workable.com"][1] == 2  # both stay unchecked for a later harvest
    assert len(session.calls) == 1


def test_waits_are_merged_by_host_on_the_data_branch():
    assert sync.KEYED["host_waits.jsonl"] == ("host", "updated_at")
