"""One company search, setting up a company not tracked yet, and the sidebar, Today, Jobs rows and
Job detail."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from jobhunt import accepted, store, wishlist
from jobhunt.state import write_jsonl
from jobhunt.web import setups
from jobhunt.web.app import create_app

from .test_jobs_web import client  # noqa: F401  (a client with roles a, b, c)

GENERATED = [{"identity": "ashby:hooli", "board": "ashby", "slug": "hooli", "name": "Hooli", "prefilter_pass": 4}]
FOUND = {"id": "acme", "name": "Acme", "url": "", "want": "covered", "at": "2026-11-18T20:00:00+00:00",
         "status": "found", "announced": False, "searched": False, "notes": [], "steps": [],
         "options": [{"label": "greenhouse acme: 4 jobs", "config": {"board": "greenhouse", "slug": "acme"},
                      "name": "Acme", "source": "detect", "samples": [["Graduate Engineer", "https://example.com/1"]]}]}


def client_for(paths) -> TestClient:
    return TestClient(create_app(paths))


def test_one_search_finds_your_companies_and_the_generated_list(paths):
    wishlist.set_company(paths.state_dir, "Acme", True, {"board": "greenhouse", "slug": "acme"})
    write_jsonl(paths.state_dir / "companies_generated.jsonl", GENERATED)
    with client_for(paths) as client:
        acme = client.get("/companies?q=acm").text
        hooli = client.get("/companies?q=hooli").text
        wished = client.get("/companies?group=wishlist").text
        none = client.get("/companies?q=zzqq").text
    assert "Acme" in acme and ">Wishlist<" in acme and "Hooli" not in acme
    assert "Hooli" in hooli and ">Generated list<" in hooli
    assert "Acme" in wished and "Mistral" not in wished  # the filter by list
    assert "No company called “zzqq” is tracked" in none and "/companies/setup?name=zzqq" in none


def test_a_setup_runs_in_the_background_and_keeps_what_it_found(paths):
    store.connect(paths.database).close()
    runner = setups.Setups(paths.database)

    def work(name, url, context):
        context["progress"]("Trying job boards")
        context["options"].append(FOUND["options"][0])
    runner.start("Acme", "", "covered", work)
    for _ in range(100):
        if not runner.running():
            break
        time.sleep(0.02)
    conn = store.connect(paths.database)
    record = setups.load(conn)["acme"]
    assert record["status"] == "found" and record["steps"] == ["Trying job boards"]
    assert [r["id"] for r in setups.announcements(conn)] == ["acme"] and setups.announcements(conn) == []  # said once


def test_a_failed_setup_says_why_in_plain_words():
    assert setups.reason_for(["Could not read https://x.example: HTTP 403 (bot check)"])[0] == "blocked"
    assert setups.reason_for(["Its robots.txt disallows /jobs"])[0] == "robots"
    assert setups.reason_for(["Its careers site could not be set up: jobs appear only after JavaScript runs."])[0] == "javascript"
    assert setups.reason_for(["A web search found no careers page."]) == setups.NO_BOARD


def test_choosing_cover_adds_it_to_the_found_by_search_list_and_clears_today(paths):
    conn = store.connect(paths.database)
    setups.put(conn, FOUND)
    with client_for(paths) as client:
        assert "Acme: found a way to check it" in client.get("/today").text
        sheet = client.get("/companies/setup?id=acme").text
        assert "Cover it" in sheet and "Graduate Engineer" in sheet
        done = client.post("/companies/setup/decide", data={"id": "acme", "decision": "covered",
                                                            "choice": '{"name": "Acme", "config": {"board": "greenhouse", "slug": "acme"}}'})
        assert "covered" in done.text
        assert "Acme" not in client.get("/today").text
    assert "acme" in accepted.names(paths.state_dir) and setups.load(conn) == {}


def test_a_failed_setup_offers_alerts_a_weekly_check_or_the_name(paths):
    conn = store.connect(paths.database)
    failed = {**FOUND, "status": "failed", "options": [], "reason": "blocked",
              "reason_text": "Its site answers with a bot check or refuses tools", "careers_url": "https://acme.example/jobs"}
    setups.put(conn, failed)
    with client_for(paths) as client:
        sheet = client.get("/companies/setup?id=acme").text
        assert "does not work around that" in sheet and "Get job alerts by email" in sheet
        client.post("/companies/setup/decide", data={"id": "acme", "decision": "alerts"})
        assert "Create a job alert for Acme" in client.get("/today").text
        client.post("/companies/setup/decide", data={"id": "acme", "decision": "done"})
        assert "job alert for Acme" not in client.get("/today").text
    setups.put(conn, failed)
    with client_for(paths) as client:
        client.post("/companies/setup/decide", data={"id": "acme", "decision": "manual"})
    assert store.manual_checks(conn) == {"acme": "Acme"}


def test_a_company_already_tracked_is_not_set_up_again(client):  # noqa: F811
    assert "Already on your lists" in client.get("/companies/setup?name=Mistral").text


def test_the_watch_stops_polling_once_nothing_runs(paths):
    conn = store.connect(paths.database)
    setups.put(conn, FOUND)
    with client_for(paths) as client:
        first = client.get("/companies/setup/watch")
        assert first.status_code == 286 and "setup-finished" in first.headers["HX-Trigger"]
        assert "HX-Trigger" not in client.get("/companies/setup/watch").headers


def test_the_sidebar_keeps_its_footer_and_folds_the_wishlist(client):  # noqa: F811
    html = client.get("/today").text
    nav = html[html.index('class="source-list"'):html.index('class="sidebar-footer"')]
    assert nav.index("All applications") < nav.index(">Companies<") < nav.index(">Runs<") < nav.index("wish-group")
    assert 'data-remember="sidebar-wishlist"' in nav and "Tool</h2>" not in nav


def test_notes_save_as_you_type(client, paths):  # noqa: F811
    response = client.post("/job/mark", data={"key": "a", "notes": "Ask about the team", "autosave": "1"})
    assert response.status_code == 204
    assert store.job_marks_for(store.connect(paths.database), "a").notes == "Ask about the team"


def test_job_detail_has_one_line_of_gaps_and_rescore_on_the_score_line(client):  # noqa: F811
    html = client.get("/job?key=a").text
    assert "Scored by" in html and "Rescore</button>" in html and "Save notes" not in html


def test_jobs_can_be_filtered_by_company_by_name(client):  # noqa: F811
    html = client.get("/jobs?view=all").text
    assert 'list="company-names"' in html and '<option value="Acme">' in html
    one = client.get("/jobs?view=all&company=ACME").text  # typed as shown: matched by its normalised name
    assert "Company: Acme" in one


def test_the_settings_page_sets_the_threshold_on_the_data_branch_and_hidden_days_locally(client, paths):  # noqa: F811
    from jobhunt import catalog as cat
    from jobhunt import preferences, sync
    page = client.get("/settings").text
    assert "Apply now threshold" in page and 'value="7" selected' in page and "Remove hidden roles after" in page
    before = client.get("/jobs?view=apply").text.count('class="row job"')
    done = client.post("/settings", data={"apply_min_fit": "9", "hidden_days": "14"})
    assert done.status_code == 200 and "Saved" in done.text
    assert preferences.apply_min_fit(paths.state_dir) == 9 and cat.PRESETS["apply"]["min_fit"] == 9
    assert client.get("/jobs?view=apply").text.count('class="row job"') <= before
    assert "Fit 9+" in client.get("/jobs?view=apply").text and 'name="min_fit"' not in client.get("/jobs?view=apply").text
    assert store.preference(store.connect(paths.database), "hidden_days") == 14
    # The next run sees the change: the data branch keeps the newer preference when both sides changed.
    ours = (paths.state_dir / preferences.FILE).read_bytes()
    theirs = b'{"id": "apply_min_fit", "updated_at": "2020-01-01T00:00:00+00:00", "value": 6}\n'
    merged = sync.merge_file(preferences.FILE, ours, theirs)
    assert b'"value": 9' in merged and b'"value": 6' not in merged
    client.post("/settings", data={"apply_min_fit": "7", "hidden_days": "7"})  # back for other tests
    assert client.post("/settings", data={"apply_min_fit": "12", "hidden_days": "7"}).status_code == 400
