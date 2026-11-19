"""The Jobs and Job detail pages, over a small state folder."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from jobhunt import config as cfg
from jobhunt import scoring, store
from jobhunt.state import State
from jobhunt.web.app import create_app

from .conftest import EXAMPLE_CONFIG
from .fakes import GOOD_SCORE, FakeClaude, claude_stream


def job(key: str, title: str, company: str = "Acme", locations=("Paris, France",), **extra) -> dict:
    return {"key": key, "company": company, "title": title, "url": f"https://jobs.example/{key}", "source": "greenhouse",
            "source_id": key, "locations": list(locations), "posted": "2026-10-20", "deadline": None,
            "department": None, "employment_type": None, "description_hash": f"h{key}", "prefilter": "pass",
            "first_seen": "2026-10-20T08:00:00+00:00", "closed": False, "closed_at": None, "duplicate_of": None,
            "merged_locations": None, "last_seen": "2026-10-25", "origin": "config", "parked": False,
            "backfill_skip": False, "updated_at": "2026-10-20T08:00:00+00:00", **extra}


def score(key: str, model: str, fit: int, action: str = "apply_now", **result) -> dict:
    blockers = result.pop("blockers", [])
    return {"id": scoring.score_id(key, model), "key": key, "model": model, "description_hash": f"h{key}",
            "prompt_version": "v", "status": scoring.status(blockers, False), "deadline": None,
            "scored_at": "2026-10-21T08:00:00+00:00", "seconds": 1, "cost_usd": 0, "tokens": {}, "rate_limit": None,
            "error": None, "result": {**GOOD_SCORE, "fit": fit, "action": action, "blockers": blockers, **result}}


@pytest.fixture
def client(paths):
    state = State(paths.state_dir)
    rows = [
        job("a", "Graduate Engineer"),
        job("b", "Data Engineer"),
        job("c", "Backend Engineer", company="Initech"),
        job("d", "Platform Engineer", locations=("London, United Kingdom",), origin="generated", parked=True),
        job("e", "Mobile Engineer", origin="generated", titlecheck={"dropped": True, "reason": "Mobile", "quote": "q"}),
        job("f", "Security Engineer"),  # not scored yet
        job("g", "Senior Engineer", prefilter="title: senior"),  # never shown
    ]
    for r in rows:
        state.jobs[r["key"]] = r
    for s in [score("a", "sonnet", 8), score("b", "haiku", 9, action="skip"), score("b", "sonnet", 6),
              score("c", "sonnet", 8, blockers=[{"type": "language", "severity": "blocking", "detail": "Dutch",
                                                 "min_years": None}], action="skip")]:
        state.scores[s["id"]] = s
    state.write_posting("ha", "Build things.\n<script>alert('x')</script>")
    state.save()
    state.append_run({"started_at": "2026-10-20T07:00:00+00:00"})
    with TestClient(create_app(paths)) as c:
        yield c


def rebuild(paths) -> None:
    """Rebuild the catalogue now (the app rebuilds it in the background after a state change)."""
    from jobhunt import catalog as cat
    cat.Catalog(paths.state_dir, paths.config, cfg.load_settings(paths.config),
                paths.database.with_name("catalog.db")).refresh(wait=True)


def rows_in(html: str) -> list[str]:
    return [line.split('data-href="/job?key=')[1].split('"')[0].split("&amp;")[0]
            for line in html.splitlines() if 'data-href="/job?key=' in line]


def test_scored_view_sorts_by_action_then_fit_and_prefers_sonnet(client):
    html = client.get("/jobs").text
    assert rows_in(html) == ["a", "b", "c"]  # apply_now (8, then Sonnet's 6 for b), then skip
    assert "Graduate Engineer" in html and "Senior Engineer" not in html and "Security Engineer" not in html


def test_unscored_views(client):
    assert rows_in(client.get("/jobs?view=waiting").text) == ["f"]
    assert rows_in(client.get("/jobs?view=apply").text) == ["a"]


def test_parked_and_dropped_roles_are_nowhere(client):
    """UK/US-only generated roles (d) and title-check drops (e) stay in .data but out of the app."""
    for url in ("/jobs?view=all", "/jobs?view=all&show_hidden=1&show_closed=1", "/jobs?view=all&q=engineer",
                "/jobs?view=all&group=UK", "/jobs?view=all&origin=generated", "/jobs?view=parked",
                "/jobs?view=dropped"):
        keys = rows_in(client.get(url).text)
        assert "d" not in keys and "e" not in keys, url
    assert client.get("/job?key=d").status_code == 404 and client.get("/job?key=e").status_code == 404
    html = client.get("/jobs").text
    assert "Parked" not in html and "Dropped by title check" not in html


def test_parked_roles_appear_when_scoring_them_is_switched_on(paths):
    import tomllib
    state = State(paths.state_dir)
    state.jobs["d"] = job("d", "Platform Engineer", locations=("London, United Kingdom",), origin="generated",
                          parked=True)
    state.save()
    settings = cfg.load_settings(EXAMPLE_CONFIG)
    settings["generated"]["score_parked"] = True
    from jobhunt.web import app as webapp
    original = webapp.cfg.load_settings
    webapp.cfg.load_settings = lambda config: settings
    try:
        with TestClient(create_app(paths)) as c:
            assert rows_in(c.get("/jobs?view=all").text) == ["d"]
    finally:
        webapp.cfg.load_settings = original


def test_counts_follow_hidden_and_applied_roles(client):
    counts_before = client.get("/jobs").text
    assert 'href="/jobs?view=scored"' in counts_before
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    client.post("/job/stage", data={"key": "b", "stage": "applied"})
    html = client.get("/jobs").text
    scored_badge = html.split('href="/jobs?view=scored"')[1].split('class="badge">')[1].split("<")[0]
    assert scored_badge == "1"  # a, b and c were scored; c hidden, b applied


def test_the_catalogue_follows_changes_to_the_state(client, paths):
    import time
    state = State(paths.state_dir)
    state.jobs["n"] = job("n", "Brand New Engineer")
    state.scores["n@sonnet"] = score("n", "sonnet", 9)
    time.sleep(0.01)
    state.save()
    for _ in range(50):  # rebuilt in the background
        if "n" in rows_in(client.get("/jobs").text):
            break
        time.sleep(0.05)
    assert rows_in(client.get("/jobs").text)[0] == "n"


def test_filters(client):
    assert rows_in(client.get("/jobs?min_fit=7").text) == ["a", "c"]
    assert rows_in(client.get("/jobs?status=blocked").text) == ["c"]
    assert rows_in(client.get("/jobs?q=initech").text) == ["c"]
    assert rows_in(client.get("/jobs?view=all&group=UK").text) == []
    assert rows_in(client.get("/jobs?view=all&origin=generated").text) == []
    assert rows_in(client.get("/jobs?q=graduate+eng").text) == ["a"]  # word prefixes


def test_detail_shows_the_score_and_escapes_the_posting(client):
    html = client.get(f"/job?key={quote('a')}").text
    assert "Backend graduate role." in html and "Build things." in html
    assert "<script>alert" not in html and "&lt;script&gt;alert" in html
    assert "linkedin.com/jobs/search" in html and "Check LinkedIn" in html
    assert "On LinkedIn?" not in html  # the dropdown left the detail page too
    html = client.get("/job?key=c").text
    assert "Dutch" in html and "status-blocked" in html


def test_unknown_job_is_404(client):
    assert client.get("/job?key=nope").status_code == 404


def test_stage_links_an_application_and_moves_the_role(client, paths):
    assert client.post("/job/stage", data={"key": "a", "stage": "to_apply"}).status_code == 200
    assert "a" in rows_in(client.get("/jobs").text)  # To apply stays on Jobs
    assert "Graduate Engineer" not in client.get("/applications").text  # To apply is not an application
    response = client.post("/job/stage", data={"key": "a", "stage": "applied"})
    assert 'data-placeholder="a"' in response.text and "<td" not in response.text  # gone from the list
    assert "a" not in rows_in(client.get("/jobs").text)
    assert "Graduate Engineer" in client.get("/applications").text
    conn = store.connect(paths.database)
    assert store.applications_by_job(conn)["a"][1] == "applied"
    conn.close()
    assert client.post("/job/stage", data={"key": "a", "stage": "none"}).status_code == 400


def sidebar_badge(html: str, href: str) -> int | None:
    """The count beside a sidebar item, or None when the item is not shown."""
    part = html.split("<nav")[1].split("</nav>")[0]
    if f'href="{href}"' not in part:
        return None
    return int(part.split(f'href="{href}"')[1].split('class="badge"')[1].split(">")[1].split("<")[0])


def stage_tab(html: str, href: str) -> int | None:
    """The count on an Applications stage tab (2026-10-29: the stages moved from the sidebar), or
    None when the tab is not shown."""
    if '<nav class="stage-tabs"' not in html:
        return None
    part = html.split('<nav class="stage-tabs"')[1].split("</nav>")[0]
    if f'href="{href}"' not in part:
        return None
    return int(part.split(f'href="{href}"')[1].split('class="count">')[1].split("<")[0].replace(",", ""))


def test_to_apply_sends_a_role_back_to_jobs_and_every_list_agrees(client, paths):
    import json

    def counts() -> dict:
        html = client.get("/applications").text
        return {"applied": stage_tab(html, "/applications?stage=applied") or 0,
                "to_apply": stage_tab(html, "/applications?stage=to_apply"),
                "all": sidebar_badge(html, "/applications"), "rows": html.count('hx-post="/applications/')}
    seeded = counts()  # the example config seeds applications of its own

    def check(applied: bool):
        now = counts()
        assert ("a" in rows_in(client.get("/jobs").text)) == (not applied)
        assert ("Graduate Engineer" in client.get("/applications").text) == applied
        assert ("Graduate Engineer" in client.get("/applications?stage=applied").text) == applied
        assert now["to_apply"] is None  # no To apply tab
        assert now["applied"] == seeded["applied"] + applied
        assert now["all"] == now["rows"] == seeded["all"] + applied

    def stored():
        c = store.connect(paths.database)
        try:
            app_id = store.applications_by_job(c)["a"][0]
            row = c.execute("SELECT stage, applied_on FROM applications WHERE id = ?", (app_id,)).fetchone()
            return app_id, row["stage"], row["applied_on"], [h["stage"] for h in store.stage_history(c, app_id)]
        finally:
            c.close()

    check(False)
    client.post("/job/stage", data={"key": "a", "stage": "applied", "list_view": "scored"})
    check(True)
    app_id, stage, applied_on, _ = stored()
    assert stage == "applied" and applied_on
    # Changed my mind, on the All applications page: the row leaves the page, with Undo.
    back = client.post(f"/applications/{app_id}", data={"stage": "to_apply"})
    assert f'data-placeholder="app-{app_id}"' in back.text
    event = json.loads(back.headers["HX-Trigger"])["application-moved"]
    assert (event["stage"], event["back_to"], event["applied_on"]) == ("to_apply", "applied", applied_on)
    check(False)
    assert stored()[1:] == ("to_apply", None, ["applied", "to_apply"])  # Applied on cleared, history kept
    client.post("/job/stage", data={"key": "a", "stage": "applied", "list_view": "scored"})
    check(True)
    assert stored()[3] == ["applied", "to_apply", "applied"]
    # To apply is offered only for found roles; a hand-added application has no Jobs row to go to.
    html = client.get("/applications").text
    assert html.count('<option value="to_apply"') == 1


def test_to_apply_can_be_cleared(client, paths):
    client.post("/job/stage", data={"key": "b", "stage": "to_apply"})
    client.post("/job/stage", data={"key": "b", "stage": "none"})
    conn = store.connect(paths.database)
    assert "b" not in store.applications_by_job(conn)
    conn.close()


def test_hide_and_on_linkedin(client):
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    assert "c" not in rows_in(client.get("/jobs").text)
    assert "c" in rows_in(client.get("/jobs?show_hidden=1").text)
    client.post("/job/mark", data={"key": "a", "on_linkedin": "yes"})
    html = client.get("/jobs").text
    assert rows_in(html) == ["a", "b"]  # On LinkedIn? no longer changes the order (c is hidden)
    assert "On LinkedIn?" not in html and "linkedin.com" not in html  # nor shows on the list


def test_hiding_leaves_a_placeholder_and_undo_puts_the_row_back(client):
    import json
    response = client.post("/job/mark", data={"key": "b", "hidden": "1", "list_view": "scored"})
    assert 'data-placeholder="b"' in response.text and "data-href" not in response.text
    event = json.loads(response.headers["HX-Trigger"])["job-visibility"]
    assert event == {"key": "b", "hidden": True, "list_view": "scored", "layout": "", "label": "Acme, Data Engineer"}
    assert rows_in(client.get("/jobs").text) == ["a", "c"]
    undo = client.post("/job/mark", data={"key": "b", "hidden": "0", "list_view": "scored", "undo": "1"})
    assert rows_in(undo.text) == ["b"] and "job-visibility" not in undo.headers["HX-Trigger"]
    assert rows_in(client.get("/jobs").text) == ["a", "b", "c"]  # back where it was


def test_the_hidden_view(client):
    html = client.get("/jobs").text
    assert 'href="/jobs?view=hidden"' not in html  # nothing hidden yet: no sidebar item
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    client.post("/job/mark", data={"key": "f", "hidden": "1"})
    html = client.get("/jobs?view=hidden").text
    assert rows_in(html) == ["c", "f"]  # scored first, then unscored
    assert html.split('id="hidden-count">')[1].split("<")[0] == "2"
    assert 'name="show_hidden"' not in html
    # Showing a role again from the Hidden list takes it off that list, with an Undo toast.
    response = client.post("/job/mark", data={"key": "c", "hidden": "0", "list_view": "hidden"})
    assert 'data-placeholder="c"' in response.text
    assert '"hidden": false' in response.headers["HX-Trigger"]
    assert rows_in(client.get("/jobs?view=hidden").text) == ["f"]
    assert "c" in rows_in(client.get("/jobs").text)
    # A stage change on the Hidden list keeps the role there until it is applied to.
    kept = client.post("/job/stage", data={"key": "f", "stage": "to_apply", "list_view": "hidden"})
    assert rows_in(kept.text) == ["f"]


def test_odds_filter(paths):
    state = State(paths.state_dir)
    for key, odds in (("h", "high"), ("m", "medium"), ("l", "low")):
        state.jobs[key] = job(key, f"{odds} engineer")
        state.scores[f"{key}@sonnet"] = score(key, "sonnet", 7, odds=odds)
    state.save()
    with TestClient(create_app(paths)) as c:
        assert rows_in(c.get("/jobs?odds=high").text) == ["h"]
        assert rows_in(c.get("/jobs?odds=low").text) == ["l"]
        assert len(rows_in(c.get("/jobs?odds=nonsense").text)) == 3  # unknown values are ignored
        assert 'name="odds"' in c.get("/jobs").text


def test_notes_from_the_detail_page(client):
    response = client.post("/job/mark", data={"key": "a", "notes": "Ask Maria for a referral", "from": "detail"},
                           follow_redirects=True)
    assert "Ask Maria for a referral" in response.text


def test_foreign_origin_is_refused(client):
    response = client.post("/job/mark", data={"key": "a", "hidden": "1"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_limit_and_on_hold_warnings(client, paths):
    conn = store.connect(paths.database)
    store.add_application(conn, company="Initech", role="Old role", stage="on_hold")
    conn.close()
    assert "contact the recruiter" in client.get("/job?key=c").text


def test_no_state_folder_yet(tmp_path):
    paths = cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "db.sqlite",
                      backups=tmp_path / "backups", state=tmp_path / "missing")
    with TestClient(create_app(paths)) as c:
        assert c.get("/jobs").status_code == 200 and "No roles here" in c.get("/jobs").text


def test_roles_matching_an_unlinked_application_leave_the_list(client, paths):
    conn = store.connect(paths.database)
    store.add_application(conn, company="Acme B.V.", role="Data Engineer (Paris)", stage="rejected")
    conn.close()
    assert "b" not in rows_in(client.get("/jobs").text)


def test_rescore_scores_again_with_the_scheduled_model(paths):
    state = State(paths.state_dir)
    key = "greenhouse:Acme:a"  # a real key: source, company, source id
    state.jobs[key] = {**job("a", "Graduate Engineer"), "key": key}
    state.write_posting("ha", "Python and Go.")
    state.save()
    fake = FakeClaude(lambda m: claude_stream({**GOOD_SCORE, "fit": 9}))
    with TestClient(create_app(paths, runner=fake)) as c:
        page = c.post("/job/rescore", data={"key": key}, follow_redirects=True).text
        assert "Rescored with sonnet: fit 9" in page
        assert State(paths.state_dir).scores[f"{key}@sonnet"]["result"]["fit"] == 9
        assert c.post("/job/rescore", data={"key": "nope"}, follow_redirects=True).text.count("No such job") >= 1


def test_rescore_needs_posting_text(paths):
    state = State(paths.state_dir)
    state.jobs["z"] = job("z", "Graduate Engineer", description_hash=None)
    state.save()
    with TestClient(create_app(paths, runner=FakeClaude())) as c:
        assert "not been fetched yet" in c.post("/job/rescore", data={"key": "z"}, follow_redirects=True).text


def test_sync_now_without_a_linked_folder_says_so(client):
    page = client.post("/sync", headers={"referer": "http://testserver/jobs"}, follow_redirects=True).text
    assert "sync --init" in page


def test_sync_now_says_who_is_writing_the_state(client, paths, monkeypatch):
    from jobhunt import sync
    monkeypatch.setattr(sync, "is_worktree", lambda folder: True)
    with sync.state_lock(paths.state_dir, holder="the harvest"):
        since = sync.lock_holder(paths.state_dir)["since"][11:16]
        page = client.post("/sync", headers={"referer": "http://testserver/jobs"}, follow_redirects=True).text
    assert f"The harvest is writing the state (since {since} UTC); synced when it is done." in page
    assert f"The harvest is writing the state (since {since} UTC)" in client.get("/runs").text


def test_a_catalogue_from_an_older_version_is_rebuilt_before_use(client, paths, monkeypatch):
    from jobhunt import catalog as cat
    client.get("/jobs")
    monkeypatch.setattr(cat, "CATALOG_VERSION", cat.CATALOG_VERSION + 1)  # as after an upgrade
    assert rows_in(client.get("/jobs?odds=medium").text) == ["a", "b", "c"]  # served by the new build
    catalog = cat.Catalog(paths.state_dir, paths.config, cfg.load_settings(paths.config),
                          paths.database.with_name("catalog.db"))
    assert catalog._built("version") == str(cat.CATALOG_VERSION)


def test_an_accidental_applied_goes_back_from_the_applied_list(client, paths):
    import json
    client.post("/job/stage", data={"key": "a", "stage": "applied"})
    conn = store.connect(paths.database)
    app_id = store.applications_by_job(conn)["a"][0]
    conn.close()
    response = client.post(f"/applications/{app_id}", data={"stage": "to_apply", "list_stage": "applied"})
    event = json.loads(response.headers["HX-Trigger"])["application-moved"]
    assert event["stage"] == "to_apply" and event["back_to"] == "applied" and event["applied_on"]
    assert "a" in rows_in(client.get("/jobs").text) and "Graduate Engineer" not in client.get("/applications").text
    conn = store.connect(paths.database)
    assert conn.execute("SELECT applied_on FROM applications WHERE id = ?", (app_id,)).fetchone()[0] is None
    conn.close()
    # Undo restores the date it had, not today's.
    client.post(f"/applications/{app_id}", data={"stage": "applied", "applied_on": "2026-10-20", "list_stage": "applied",
                                                 "undo": "1"})
    conn = store.connect(paths.database)
    assert tuple(conn.execute("SELECT stage, applied_on FROM applications WHERE id = ?", (app_id,)).fetchone()) == (
        "applied", "2026-10-20")
    conn.close()



def test_mark_as_applied_on_jobs_and_back_from_applications(client, paths):
    import json
    jobs = client.get("/jobs").text
    row = jobs.split('data-href="/job?key=a"')[1].split("</tr>")[0]
    assert "Mark as applied" in row and 'name="stage"' not in row  # the button, not a stage menu

    # Jobs: Mark as applied -> the Applied list, gone from Jobs, with Undo.
    response = client.post("/job/stage", data={"key": "a", "stage": "applied", "list_view": "scored"})
    event = json.loads(response.headers["HX-Trigger"])["job-applied"]
    assert event == {"key": "a", "previous": "none", "list_view": "scored", "layout": "", "label": "Acme, Graduate Engineer"}
    assert "a" not in rows_in(client.get("/jobs").text)
    assert "Graduate Engineer" in client.get("/applications?stage=applied").text
    undo = client.post("/job/stage", data={"key": "a", "stage": "none", "list_view": "scored", "undo": "1"})
    assert rows_in(undo.text) == ["a"] and "job-applied" not in undo.headers["HX-Trigger"]
    assert "Graduate Engineer" not in client.get("/applications").text  # Undo leaves no application behind
    assert client.post("/job/stage", data={"key": "a", "stage": "none"}).status_code == 200  # nothing to remove

    # Mark again, then change my mind on the Applications page: To apply sends it back to Jobs.
    client.post("/job/stage", data={"key": "a", "stage": "applied", "list_view": "scored"})
    conn = store.connect(paths.database)
    app_id = store.applications_by_job(conn)["a"][0]
    conn.close()
    client.post(f"/applications/{app_id}", data={"stage": "to_apply", "list_view": "applied"})
    jobs = client.get("/jobs").text
    assert "a" in rows_in(jobs)
    row = jobs.split('data-href="/job?key=a"')[1].split("</tr>")[0]
    assert "Mark as applied" in row and "To apply" not in row  # back as a plain role, no tag

    # Job detail: the same button, then a link to where the stage is changed.
    detail = client.get("/job?key=a").text
    assert "Mark as applied" in detail
    done = client.post("/job/stage", data={"key": "a", "stage": "applied", "from": "detail"}, follow_redirects=False)
    assert done.status_code == 303 and "Applications+page" in done.headers["location"]
    assert "Change stage" in client.get("/job?key=a").text


def hidden_days_ago(paths, key: str, days: int) -> None:
    """As if the role had been hidden `days` ago."""
    from datetime import datetime, timedelta
    conn = store.connect(paths.database)
    with conn:
        conn.execute("UPDATE job_marks SET hidden_at = ? WHERE job_key = ?",
                     ((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"), key))
    conn.close()


def test_a_hidden_role_shows_its_days_left_then_is_removed_for_good(client, paths):
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    assert "7 days left" in client.get("/jobs?view=hidden").text
    hidden_days_ago(paths, "c", 5)
    assert "2 days left" in client.get("/jobs?view=hidden").text
    hidden_days_ago(paths, "c", 7)
    for url in ("/jobs?view=hidden", "/jobs?view=all&show_hidden=1&show_closed=1", "/jobs?show_hidden=1",
                "/jobs?q=initech&show_hidden=1"):
        assert "c" not in rows_in(client.get(url).text), url
    html = client.get("/jobs").text
    assert 'href="/jobs?view=hidden"' not in html  # nothing left in the Hidden view: no sidebar item
    detail = client.get("/job?key=c")
    assert detail.status_code == 404 and "jobhunt hidden --restore" in detail.text
    # A run fetches it again (its record is updated): it still never comes back.
    import time
    state = State(paths.state_dir)
    state.jobs["c"] = {**state.jobs["c"], "updated_at": "2026-10-26T08:00:00+00:00", "last_seen": "2026-10-26"}
    time.sleep(0.01)
    state.save()
    from jobhunt import catalog as cat
    cat.Catalog(paths.state_dir, paths.config, {}, paths.database.with_name("catalog.db")).refresh(wait=True)
    assert "c" not in rows_in(client.get("/jobs?view=all&show_hidden=1").text)
    # A longer setting later does not bring it back: removal is recorded, not recomputed.
    conn = store.connect(paths.database)
    assert store.expire_hidden(conn, 30) == 0 and "c" in store.removed_marks(conn)
    conn.close()


def test_the_removal_record_goes_once_the_role_closes(client, paths):
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    client.post("/job/mark", data={"key": "a", "hidden": "1"})
    hidden_days_ago(paths, "c", 8)
    hidden_days_ago(paths, "a", 8)
    client.get("/jobs")  # the sweep runs with every page
    state = State(paths.state_dir)
    state.jobs["c"] = {**state.jobs["c"], "closed": True, "closed_at": "2026-10-26T08:00:00+00:00",
                       "updated_at": "2026-10-26T08:00:00+00:00"}
    state.save()
    with TestClient(create_app(paths)):  # the app's start-up tidy (also run daily)
        pass
    conn = store.connect(paths.database)
    assert set(store.removed_marks(conn)) == {"a"}  # c closed: record deleted; a still open: kept
    conn.close()


def test_list_and_restore_removed_roles_from_the_command_line(client, paths, monkeypatch, capsys):
    from jobhunt import cli
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    hidden_days_ago(paths, "c", 7)
    monkeypatch.setenv("JOBHUNT_ROOT", str(paths.root))
    monkeypatch.setenv("JOBHUNT_CONFIG", str(paths.config))
    monkeypatch.setenv("JOBHUNT_DB", str(paths.database))
    monkeypatch.setenv("JOBHUNT_STATE", str(paths.state_dir))
    assert cli.main(["hidden"]) == 0
    out = capsys.readouterr().out
    assert "Initech: Backend Engineer" in out and "--restore" in out
    assert cli.main(["hidden", "--restore", "c"]) == 0
    assert "c" in rows_in(client.get("/jobs").text)  # back on Jobs, not hidden
    assert cli.main(["hidden", "--restore", "c"]) == 1  # nothing to restore twice


def role_link(html: str, key: str) -> str:
    """The href of the role's link on a list page (or in an application's sheet)."""
    import html as htmllib
    import re
    found = re.search(r'href="(/job\?key=' + re.escape(key) + r'(?:&amp;[^"]*)?)"', html)
    assert found, f"no link to {key}"
    return htmllib.unescape(found.group(1))


def back_link(html: str) -> tuple[str, str]:
    import html as htmllib
    import re
    found = re.search(r'<a href="([^"]*)" data-back>‹ ([^<]*)</a>', html)
    return htmllib.unescape(found.group(1)), htmllib.unescape(found.group(2))


def test_back_from_a_role_returns_to_the_exact_list_it_was_opened_from(client, paths):
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    client.post("/wishlist/star", data={"key": "a", "on": "1"})
    cases = [  # (list page, a role on it, back link label)
        ("/jobs?view=apply&odds=medium&q=graduate&page=1", "a", "Apply now"),
        ("/jobs?view=scored&status=clear&min_fit=6&group=EU%2FEEA%2FCH", "b", "Scored"),
        ("/jobs?view=hidden&q=initech", "c", "Hidden"),
        ("/jobs?view=waiting", "f", "Waiting"),
        ("/jobs?view=all&company=acme", "a", "Acme"),  # a wishlist company
    ]
    for url, key, label in cases:
        link = role_link(client.get(url).text, key)
        detail = client.get(link)
        assert detail.status_code == 200, url
        assert back_link(detail.text) == (url, label), url
        # After an action on the page (notes saved), the way back is unchanged.
        saved = client.post("/job/mark", data={"key": key, "notes": "n", "from": "detail",
                                                "back": back_link(detail.text)[0]}, follow_redirects=False)
        assert back_link(client.get(saved.headers["location"]).text) == (url, label), url
        again = client.get(url)  # and the list it leads to is the same view with its filters
        assert again.status_code == 200 and key in rows_in(again.text)
    filters = client.get(cases[1][0]).text
    assert '<option value="clear" selected>' in filters and '<option value="6" selected>' in filters


def test_back_from_a_role_opened_from_an_applications_list(client, paths):
    client.post("/job/stage", data={"key": "a", "stage": "applied"})
    conn = store.connect(paths.database)
    app_id = store.applications_by_job(conn)["a"][0]
    conn.close()
    for url, label in (("/applications?stage=applied", "Applied"), ("/applications", "Applications")):
        sheet = client.get(f"/applications/{app_id}/sheet", headers={"HX-Request": "true",
                                                                    "HX-Current-URL": f"http://testserver{url}"})
        detail = client.get(role_link(sheet.text, "a"))
        assert back_link(detail.text) == (url, label)


def test_the_back_link_never_leaves_the_app(client):
    for evil in ("https://evil.example/jobs", "//evil.example/jobs", "/job?key=a", "javascript:alert(1)"):
        detail = client.get("/job", params={"key": "a", "back": evil})
        assert back_link(detail.text) == ("/jobs", "Scored"), evil


def test_the_companies_page(client, paths):
    from jobhunt import wishlist
    from jobhunt.state import write_jsonl
    wishlist.set_company(paths.state_dir, "Acme", True, {"board": "greenhouse", "slug": "acme"})
    write_jsonl(paths.state_dir / "companies_generated.jsonl", [
        {"identity": "ashby:hooli", "board": "ashby", "slug": "hooli", "name": "Hooli", "prefilter_pass": 4},
        {"identity": "workday:https://x.wd1.myworkdayjobs.com/HooliCareers", "board": "workday",
         "url": "https://x.wd1.myworkdayjobs.com/HooliCareers"}])
    html = client.get("/companies").text
    assert "Acme" in html and "wishlist" in html and "greenhouse · acme" in html
    assert "Mistral" in html and "Your list" in html  # the example config's companies
    assert "Max 3 applications per 90 days" in html  # Mistral's limit, from the example limits.toml
    found = client.get("/companies?q=hooli").text
    assert "Matching “hooli”" in found and "Generated list" in found and "Hooli" in found
    assert ">X<" in found  # a Workday site by its tenant


def test_limit_warnings_on_jobs_and_job_detail_come_from_the_registry(client, paths, tmp_path):
    """Mistral's example limits: 3 applications per 90 days, and 180 days per role."""
    import shutil
    from datetime import date, timedelta

    from jobhunt.web.app import create_app as make_app
    config = tmp_path / "config"
    shutil.copytree(paths.config, config)
    (config / "limits.toml").write_text(
        '[[limit]]\ncompany = "Acme"\nscope = "company"\nmax = 2\nwindow_days = 90\nnote = "Max 2 per 90 days."\n')
    conn = store.connect(paths.database)
    store.add_application(conn, company="Acme", role="Old role", applied_on=(date.today() - timedelta(days=10)).isoformat())
    conn.close()
    other = cfg.Paths(root=paths.root, config=config, database=paths.database, backups=paths.backups)
    with TestClient(make_app(other)) as c:
        row = c.get("/jobs").text.split('data-href="/job?key=a"')[1].split("</tr>")[0]
        assert "Uses the last slot left (1 of 2 in the last 90 days): Max 2 per 90 days." in row
        assert "Application limit at Acme: Uses the last slot left" in c.get("/job?key=a").text
        conn = store.connect(paths.database)
        store.add_application(conn, company="Acme", role="Another", applied_on=date.today().isoformat())
        conn.close()
        assert "No slot left" in c.get("/job?key=a").text


def test_views_are_presets_of_one_table_with_sorting_remembered_per_view(client, paths):
    from jobhunt import catalog as cat
    apply = cat.Filters.from_query({"view": "apply"})
    assert (apply.scope, apply.min_fit, apply.action) == ("scored", 7, "apply_now")
    lifted = cat.Filters.from_query({"view": "apply", "min_fit": ""})  # "Any" lifts the preset's filter
    assert lifted.min_fit is None and lifted.action == "apply_now"
    assert cat.Filters.from_query({"view": "hidden"}).hidden == "only"
    # The same columns on every scored view, with Posted next to Found; Waiting shows Board instead
    # of the score's columns.
    client.post("/job/mark", data={"key": "c", "hidden": "1"})
    for view in ("apply", "scored", "hidden"):
        html = client.get(f"/jobs?view={view}").text
        assert 'class="w-fit"' in html and "Posted</a>" in html and ">Status</th>" in html, view
    html = client.get("/jobs?view=waiting").text
    assert 'class="w-fit"' not in html and ">Status</th>" not in html and ">Board</th>" in html and "Found</a>" in html
    # Sorting by posting date, both ways, and remembered for the view.
    state = State(paths.state_dir)
    state.jobs["a"] = {**state.jobs["a"], "posted": "2026-09-01"}
    state.jobs["b"] = {**state.jobs["b"], "posted": "2026-10-22"}
    state.save()
    cat.Catalog(paths.state_dir, paths.config, cfg.load_settings(paths.config),
                paths.database.with_name("catalog.db")).refresh(wait=True)
    newest = client.get("/jobs?view=scored&sort=posted&dir=desc")
    assert rows_in(newest.text)[0] == "b"
    assert newest.cookies.get("jobs_sort_scored") == "posted:desc"
    oldest = client.get("/jobs?view=scored&sort=posted&dir=asc")
    assert rows_in(oldest.text)[0] == "a"
    remembered = client.get("/jobs?view=scored").text
    assert "sorted by posted, oldest or lowest first" in remembered and rows_in(remembered)[0] == "a"
    assert "sorted by" not in client.get("/jobs?view=apply").text  # another view keeps its own order
    client.get("/jobs?view=scored&sort=default")
    assert "sorted by" not in client.get("/jobs?view=scored").text  # back to best first


def test_bulk_hide_mark_applied_and_block_each_with_undo(client, paths):
    import json

    from jobhunt import blocklist, wishlist
    def bulk(action, keys, **extra):
        response = client.post("/jobs/bulk", data={"action": action, "keys": keys, **extra})
        assert response.status_code == 200, response.text
        return json.loads(response.headers["HX-Trigger"])["jobs-bulk"]

    done = bulk("hide", ["a", "b"])
    assert rows_in(client.get("/jobs").text) == ["c"]
    bulk("hide", done["keys"], undo="1")
    assert set(rows_in(client.get("/jobs").text)) == {"a", "b", "c"}

    client.post("/job/stage", data={"key": "b", "stage": "to_apply"})
    done = bulk("applied", ["a", "b"])
    assert done["previous"] == {"a": "none", "b": "to_apply"}
    conn = store.connect(paths.database)
    assert {k: s for k, (_, s) in store.applications_by_job(conn).items()} == {"a": "applied", "b": "applied"}
    conn.close()
    bulk("applied", done["keys"], undo="1", previous=json.dumps(done["previous"]))
    conn = store.connect(paths.database)
    assert {k: s for k, (_, s) in store.applications_by_job(conn).items()} == {"b": "to_apply"}  # a: as before
    conn.close()

    wishlist.set_company(paths.state_dir, "Acme", True)
    done = bulk("block", ["a", "c"])  # Acme and Initech
    assert done["names"] == ["Acme", "Initech"] and blocklist.names(paths.state_dir) == {"acme", "initech"}
    assert rows_in(client.get("/jobs?view=all").text) == [] and wishlist.names(paths.state_dir) == frozenset()
    bulk("block", done["keys"], undo="1")
    assert blocklist.names(paths.state_dir) == frozenset() and wishlist.names(paths.state_dir) == {"acme"}
    assert client.post("/jobs/bulk", data={"action": "explode", "keys": ["a"]}).status_code == 400


def test_rows_carry_what_selection_and_the_keyboard_need(client):
    html = client.get("/jobs").text
    row = html.split('data-href="/job?key=a"')[1].split("</tr>")[0]
    assert 'data-key="a"' in row and 'class="row-select"' in row and 'data-company="Acme"' in row
    assert 'class="select-page"' in html and 'data-bulk="block"' in html and "j/k move" in html


def test_apply_now_leaves_out_roles_only_in_the_us_unless_where_asks(client, paths):
    state = State(paths.state_dir)
    state.jobs["u"] = job("u", "Graduate Developer", company="Initrode", locations=("New York, NY",))
    state.scores[scoring.score_id("u", "sonnet")] = score("u", "sonnet", 9)
    state.save()
    rebuild(paths)
    assert rows_in(client.get("/jobs?view=apply").text) == ["a"]  # u: every location in the US
    assert rows_in(client.get("/jobs?view=apply&group=").text) == ["u", "a"]  # Where: Anywhere
    assert rows_in(client.get("/jobs?view=apply&group=US").text) == ["u"]
    html = client.get("/jobs?view=apply").text
    assert '<option value="not_us" selected>Outside the US</option>' in html
    assert 'href="/jobs?view=apply"' in html and "Apply now</span>" in html


def test_status_and_odds_are_badges_in_their_own_columns_and_the_action_shows_only_when_not_apply_now(client):
    html = client.get("/jobs").text
    # 2026-10-29: a dot and the word for status, plain text for odds (no pills).
    assert '<span class="status status-clear"><span class="status-dot" aria-hidden="true"></span>Clear</span>' in html
    assert 'class="odds odds-medium"' in html and ">Medium</span>" in html and ">Odds</th>" in html
    assert 'class="secondary small action-line" title="The score\'s suggested action, beside the status of its blockers">Suggests: skip</div>' in html  # b and c
    assert 'title="Apply now">Apply now</div>' not in html  # a: apply now is the default, not repeated


def test_companies_show_a_board_badge_roles_in_words_and_covered_first(client, paths):
    from jobhunt import wishlist
    wishlist.set_company(paths.state_dir, "Acme", True, {"board": "greenhouse", "slug": "acme"})
    html = client.get("/companies").text
    assert '<span class="board-name" title="greenhouse · acme">Greenhouse</span>' in html
    assert "coverage-no_board" in html and ">No job board found</button>" in html  # Stripe: todo
    assert "3 open</span>" in html and ">2 scored</span>" in html  # Acme: a, b and f open; a and b scored
    covered, uncovered = html.index(">Greenhouse</span>"), html.index(">No job board found</button>")
    assert covered < uncovered


def test_coverage_reasons_actions_and_the_weekly_check_by_hand(client, paths):
    from jobhunt import companies_view as cv
    row = cv.CompanyRow(id="uber", name="Uber", manual="Careers site refuses non-browser requests (HTTP 406).",
                        careers_url="https://example.org/careers")
    assert (row.coverage.status, row.coverage.action) == ("blocked", "alerts")
    assert cv.CompanyRow(id="q", name="Q", manual="robots.txt disallows /careers").coverage.status == "robots"
    assert cv.CompanyRow(id="b", name="B", manual="x", reason="no_adapter", system="SAP SuccessFactors").coverage.detail \
        == "Uses SAP SuccessFactors, which the tool cannot read yet."
    partial = cv.CompanyRow(id="l", name="L", board="custom · x", partial=True, note="Newest 45 jobs only.")
    assert (partial.coverage.status, partial.coverage.detail) == ("partial", "Newest 45 jobs only.")
    failing = cv.CompanyRow(id="f", name="F", board="greenhouse · f", last_error="ReadTimeout after 5 attempts")
    assert failing.coverage.status == "unreachable" and failing.coverage.action == "retry"
    # The sheet for an uncovered company of the example config, its reminder, and Today's weekly card.
    sheet = client.get("/companies/coverage?company=stripe").text
    assert "No job board found" in sheet and 'name="url"' in sheet and "Remind me to check manually" in sheet
    assert "Check by hand" not in client.get("/today").text
    client.post("/companies/remind", data={"company": "stripe", "on": "1"})
    today = client.get("/today").text
    assert "Check by hand" in today and "<span class=\"today-title\">Stripe</span>" in today and "#i-external" in today
    client.post("/today/checked")
    assert "Check by hand" not in client.get("/today").text  # until next week
    assert "checked weekly by hand" in client.get("/companies").text



def test_applications_groups_are_tables_with_the_same_columns(client):
    html = client.get("/applications").text
    assert html.count('<table class="list apps') == 2 and "In progress" in html and "Closed" in html
    columns = [part.split("</colgroup>")[0] for part in html.split("<colgroup>")[1:]]
    # The same fixed widths, so they line up; a group without next steps leaves out that column (2026-11-19).
    assert len(columns) == 2 and columns[0].replace('<col class="w-next">', "") == columns[1].replace('<col class="w-next">', "")
    assert '<nav class="stage-tabs"' in html and '<a class="item" href="/applications?stage=' not in html


def test_one_company_list_shows_roles_alone_and_its_chip_the_proper_name(client, paths):
    html = client.get("/jobs?view=scored&company=acme").text
    assert "one-company" in html and "Company: Acme" in html
    assert 'class="row-title role-title"' in html
