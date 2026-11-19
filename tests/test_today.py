"""The Today page: new strong roles since the last visit, deadlines, applications to act on, and
the tool's own tasks."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from jobhunt import store, today
from jobhunt.sources.http import WAITS
from jobhunt.state import State, write_jsonl

from .test_jobs_web import client, job, rebuild, rows_in, score  # noqa: F401 (fixture)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
WORKFLOW = 'on:\n  schedule:\n    - cron: "41 * * * *"\n'
SETTINGS = {"schedule": {"external_trigger": "11 */2 * * *", "trigger_token_expires": "2027-10-01"},
            "generated": {"exclude_boards": ["smartrecruiters", "join"]}}
RECENT_RUN = {"started_at": "2026-10-28T11:11:00+00:00", "seconds": 300, "ran_on": "github", "kind": "scheduled"}


def titles(items):
    return [i.title for i in items]


def test_a_visit_keeps_the_same_new_roles_until_30_minutes_have_passed():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    first = store.today_since(conn, NOW)
    assert first == (NOW - timedelta(days=1)).isoformat(timespec="seconds")  # the first visit looks back a day
    assert store.today_since(conn, NOW + timedelta(minutes=20)) == first  # a reload: same visit
    assert store.today_since(conn, NOW + timedelta(hours=2)) == NOW.isoformat(timespec="seconds")  # a new visit
    # A look from the sidebar (no update) says what the next visit would see, without starting one.
    later = (NOW + timedelta(hours=2)).isoformat(timespec="seconds")
    assert store.today_since(conn, NOW + timedelta(hours=5), update=False) == later
    assert store.today_since(conn, NOW + timedelta(hours=2, minutes=10)) == NOW.isoformat(timespec="seconds")


def test_quiet_when_runs_are_on_time_and_triggers_arrive(tmp_path):
    items = today.tool_items(SETTINGS, tmp_path, [RECENT_RUN], WORKFLOW,
                             {"last_dispatch": "2026-10-28T10:11:20Z"}, None, NOW)
    assert items == []


def test_missing_runs_late_triggers_and_an_expiring_token_are_flagged(tmp_path):
    old = {**RECENT_RUN, "started_at": "2026-10-28T05:00:00+00:00"}
    settings = {**SETTINGS, "schedule": {**SETTINGS["schedule"], "trigger_token_expires": "2026-11-05"}}
    items = today.tool_items(settings, tmp_path, [old], WORKFLOW, {"last_dispatch": "2026-10-27T18:20:16Z"}, None, NOW)
    assert titles(items) == ["No GitHub run for 7 hours", "Scheduled runs are not being started",
                             "The cron-job.org token expires in 8 days"]
    assert items[0].tone == "red" and items[0].href == "/runs" and "27 Oct 18:20 UTC" in items[1].detail
    unset = {**SETTINGS, "schedule": {"external_trigger": "11 */2 * * *"}}
    assert "Note when the cron-job.org token expires" in titles(
        today.tool_items(unset, tmp_path, [RECENT_RUN], WORKFLOW, None, None, NOW))  # no Actions count yet: no trigger check


def test_failing_companies_and_a_nearly_full_week(tmp_path):
    failing = [{**RECENT_RUN, "started_at": f"2026-10-28T0{h}:00:00+00:00",
                "sources": {"Initech": {"board": "custom", "error": "HTTP 403 from https://initech.example"}}}
               for h in range(1, 5)]
    status = {"week_nearly_full": {"message": "Claude warned the weekly limit is 90% used",
                                   "resets_at": "2026-10-29T01:00:00+00:00"}}
    items = today.tool_items(SETTINGS, tmp_path, failing + [RECENT_RUN], WORKFLOW, None, status, NOW)
    assert titles(items) == ["Initech: blocked on GitHub", "The week is nearly full"]
    assert "4 of the last" in items[0].detail and "resets 29 Oct 01:00 UTC" in items[1].detail


def test_a_company_github_cannot_fetch_is_one_plain_line_saying_whether_the_laptop_made_up_for_it(tmp_path):
    def item(laptop_entry):
        runs = [{**RECENT_RUN, "started_at": f"2026-10-28T0{h}:00:00+00:00",
                 "sources": {"Initech": {"board": "workday", "error": "HTTP 429 from https://initech.example"}}}
                for h in range(1, 4)]
        if laptop_entry is not None:
            runs.append({"ran_on": "laptop", "started_at": "2026-10-28T05:00:00+00:00", "sources": {"Initech": laptop_entry}})
        runs.append({**RECENT_RUN, "sources": {"Initech": {"board": "workday", "error": "HTTP 429 from https://initech.example"}}})
        return next(i for i in today.tool_items(SETTINGS, tmp_path, runs, WORKFLOW, None, None, NOW) if "Initech" in i.title)
    fine = item({"board": "workday", "fetched": 4})
    assert (fine.title, fine.tone) == ("Initech: blocked on GitHub, fetched from your laptop", "gray")
    assert "HTTP 429" in fine.detail and "nothing is missed" in fine.detail  # the error sits in the details
    both = item({"board": "workday", "error": "timed out"})
    assert (both.title, both.tone) == ("Initech: blocked on GitHub and on your laptop", "red")
    assert item(None).title == "Initech: blocked on GitHub"


def harvest_summary(when: str, checked: int) -> dict:
    return {"harvested_at": when, "boards": {"workable": {"found": 5366, "already_configured": 0, "checked": checked}}}


def test_a_workable_batch_is_due_once_its_wait_has_passed_and_a_day_has_gone(tmp_path):
    write_jsonl(tmp_path / "harvest.jsonl", [harvest_summary("2026-10-27T19:47:00+00:00", 2819)])
    write_jsonl(tmp_path / "host_waits.jsonl", [{"host": "apply.workable.com", "until": "2026-10-28T19:00:00+00:00",
                                                 "slow": False, "updated_at": "2026-10-27T19:46:45+00:00"}])
    waiting = today.workable_batch(SETTINGS, tmp_path, NOW)
    assert waiting.title == "Workable batch from 28 Oct 19:00 UTC" and waiting.tone == "gray" and "2,547" in waiting.detail
    WAITS.load([])
    later = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
    due = today.workable_batch(SETTINGS, tmp_path, later)
    assert due.title == "A Workable batch is due" and "--board workable --limit-per-board 500" in due.command
    write_jsonl(tmp_path / "harvest.jsonl", [harvest_summary("2026-10-28T19:47:00+00:00", 3319)])
    assert today.workable_batch(SETTINGS, tmp_path, later) is None  # a batch ran in the last 20 hours
    write_jsonl(tmp_path / "harvest.jsonl", [harvest_summary("2026-10-29T19:47:00+00:00", 5366)])
    assert today.workable_batch(SETTINGS, tmp_path, later + timedelta(days=2)) is None  # all checked


def test_reminders_show_from_their_date_until_their_board_is_on(tmp_path):
    reminder = {"text": "Decide on SmartRecruiters", "from": date(2026, 10, 30), "link": "/runs",
                "unless_board_on": "smartrecruiters"}
    settings = {**SETTINGS, "today": {"reminder": [reminder]}}
    assert titles(today.tool_items(settings, tmp_path, [RECENT_RUN], WORKFLOW, None, None, NOW)) == []
    on_the_day = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    run = {**RECENT_RUN, "started_at": "2026-10-30T08:11:00+00:00"}
    assert titles(today.tool_items(settings, tmp_path, [run], WORKFLOW, None, None, on_the_day)) == ["Decide on SmartRecruiters"]
    switched_on = {**settings, "generated": {"exclude_boards": ["join"]}}
    assert titles(today.tool_items(switched_on, tmp_path, [run], WORKFLOW, None, None, on_the_day)) == []


def test_the_today_page_lists_new_strong_roles_and_links_back(client, paths):
    assert client.get("/", follow_redirects=False).headers["location"] == "/today"
    state = State(paths.state_dir)
    fresh = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    state.jobs["n"] = job("n", "Graduate Engineer", company="Hooli", first_seen=fresh)
    state.scores["n@sonnet"] = score("n", "sonnet", 8)
    state.save()
    rebuild(paths)
    html = client.get("/today").text
    assert "New on Apply now (fit 7+) since your last visit" in html and "Deadlines in the next 7 days" in html
    assert 'href="/job?key=n&amp;back=%2Ftoday"' in html  # found 2 hours ago: within the first visit's day
    assert 'href="/job?key=a&amp;back=%2Ftoday"' not in html  # found a week ago
    assert 'class="item selected" href="/today"' in html
    detail = client.get("/job?key=a&back=/today").text
    assert "Today" in detail


def test_the_jobs_page_takes_todays_filters(client, paths):
    state = State(paths.state_dir)
    soon = (date.today() + timedelta(days=3)).isoformat()
    state.jobs["d7"] = job("d7", "Graduate Analyst", company="Hooli")
    state.scores["d7@sonnet"] = score("d7", "sonnet", 7, deadline=soon)
    state.jobs["d7"]["deadline"] = soon
    state.save()
    rebuild(paths)
    html = client.get("/jobs?view=scored&min_fit=6&due_within=7&sort=deadline&dir=asc").text
    assert rows_in(html) == ["d7"] and "Only deadlines in the next 7 days" in html
    since = client.get("/jobs?view=apply&since=2026-10-20T09:00:00%2B00:00").text
    assert rows_in(since) == [] and "Only roles found since" in since  # a was found at 08:00


def test_runs_that_failed_on_github_are_flagged_from_the_actions_count(tmp_path):
    minutes = {"last_dispatch": "2026-10-28T11:11:03Z", "latest_runs": [
        {"at": "2026-10-28T11:11:04Z", "conclusion": "failure", "url": "https://github.com/x/runs/3"},
        {"at": "2026-10-28T09:11:04Z", "conclusion": "failure", "url": "https://github.com/x/runs/2"},
        {"at": "2026-10-28T08:20:00Z", "conclusion": "success", "url": "https://github.com/x/runs/1"}]}
    items = today.tool_items(SETTINGS, tmp_path, [RECENT_RUN], WORKFLOW, minutes, None, NOW)
    assert titles(items) == ["The last 2 runs on GitHub failed"]
    assert items[0].href == "https://github.com/x/runs/3" and items[0].tone == "red"
    fixed = {**minutes, "latest_runs": [{"at": "2026-10-28T11:30:00Z", "conclusion": "success"}] + minutes["latest_runs"]}
    assert today.tool_items(SETTINGS, tmp_path, [RECENT_RUN], WORKFLOW, fixed, None, NOW) == []


def test_triggers_are_judged_at_the_time_of_the_count_not_now(tmp_path):
    # The count is from 21:30 and saw a trigger at 21:11: hours later, that is no alarm by itself.
    old_count = {"updated_at": "2026-10-28T21:30:00+00:00", "last_dispatch": "2026-10-28T21:11:03Z"}
    assert today.tool_items(SETTINGS, tmp_path, [RECENT_RUN], WORKFLOW, old_count, None, NOW) == []
    late = {"updated_at": "2026-10-29T09:30:00+00:00", "last_dispatch": "2026-10-28T21:11:03Z"}
    assert "Scheduled runs are not being started" in titles(
        today.tool_items(SETTINGS, tmp_path, [RECENT_RUN], WORKFLOW, late, None, NOW))


def test_a_one_off_failure_from_older_code_is_not_flagged():
    from jobhunt import pipeline
    fail = lambda code: {"ran_on": "github", "code": code, "sources": {"Blackstone": {"board": "workday", "error": "KeyError"}}}  # noqa: E731
    ok = lambda code: {"ran_on": "github", "code": code, "sources": {"Blackstone": {"board": "workday"}}}  # noqa: E731
    assert pipeline.failing_on_github([fail("old"), ok("new"), ok("new")]) == {}  # once, and fixed since
    assert "Blackstone" in pipeline.failing_on_github([ok("old"), fail("new")])  # with the current code
    assert "Blackstone" in pipeline.failing_on_github([fail("a"), fail("b"), fail("b")])  # twice or more
    # Cleared as soon as its latest fetch on GitHub worked (2026-11-18), whatever failed before.
    assert pipeline.failing_on_github([fail("a"), fail("b"), ok("c")]) == {}
    laptop_ok = {"ran_on": "laptop", "sources": {"Blackstone": {"board": "workday"}}}
    assert pipeline.failing_on_github([fail("a"), fail("b"), laptop_ok]) == {}  # moved to the laptop, works there
