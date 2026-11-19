from __future__ import annotations

import json
from datetime import date

from jobhunt import store
from jobhunt.web.app import ago, short_date


def first_id(paths, company):
    c = store.connect(paths.database)
    try:
        return c.execute("SELECT id FROM applications WHERE company = ?", (company,)).fetchone()["id"]
    finally:
        c.close()


def test_startup_imports_seed_and_backs_up(client, paths):
    page = client.get("/applications")
    assert page.status_code == 200
    assert "Example Bank" in page.text and "Pied Piper" in page.text
    assert len(list(paths.backups.glob("jobhunt-*.db"))) == 1


def test_stage_filter(client):
    page = client.get("/applications?stage=rejected")
    assert "Initech" in page.text and "Hooli" not in page.text


def test_inline_stage_change_returns_row_and_records_history(client, paths):
    app_id = first_id(paths, "Initech")
    response = client.post(f"/applications/{app_id}", data={"stage": "interviewing", "notes": "Round 1"})
    assert response.status_code == 200
    assert json.loads(response.headers["HX-Trigger"]) == {"applications-changed": ""}  # stays on the page
    assert "capsule stage-interviewing" in response.text
    sheet = client.get(f"/applications/{app_id}/sheet").text
    assert 'class="timeline-stage">Interviewing' in sheet
    assert "Round 1" in sheet


def test_invalid_edit_returns_message(client, paths):
    app_id = first_id(paths, "Initech")
    response = client.post(f"/applications/{app_id}", data={"stage": "hired"})
    assert response.status_code == 400
    assert "Unknown stage" in response.text


def test_add_application(client):
    response = client.post("/applications", data={
        "company": "Vandelay", "role": "Graduate SWE", "location": "Berlin",
        "applied_on": "2027-10-05", "stage": "applied",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "Vandelay" in response.text


def test_add_duplicate_shows_error(client):
    data = {"company": "Vandelay", "role": "Graduate SWE", "location": "Berlin", "applied_on": "2027-10-05"}
    client.post("/applications", data=data)
    response = client.post("/applications", data=data, follow_redirects=True)
    assert "already tracked" in response.text


def test_user_text_is_escaped(client, paths):
    app_id = first_id(paths, "Initech")
    client.post(f"/applications/{app_id}", data={"notes": "<script>alert(1)</script>"})
    page = client.get("/applications").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_cross_origin_post_is_refused(client, paths):
    app_id = first_id(paths, "Initech")
    response = client.post(f"/applications/{app_id}", data={"stage": "withdrawn"},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.post(f"/applications/{app_id}", data={"stage": "withdrawn"},
                           headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_same_origin_post_is_allowed(client, paths):
    app_id = first_id(paths, "Initech")
    response = client.post(f"/applications/{app_id}", data={"stage": "withdrawn"},
                           headers={"Origin": "http://testserver"})
    assert response.status_code == 200


def test_unknown_host_is_refused(client):
    response = client.get("/applications", headers={"Host": "attacker.example"})
    assert response.status_code == 400


def test_sheet_for_unknown_application_is_404(client):
    assert client.get("/applications/9999/sheet").status_code == 404


def test_page_groups_open_and_closed(client):
    page = client.get("/applications").text
    assert page.index("In progress") < page.index("Closed")


def test_follow_up_view_lists_only_ghosted(client, paths):
    c = store.connect(paths.database)
    c.execute("UPDATE stage_history SET changed_at = '2000-01-01'")
    c.execute("UPDATE applications SET stage = 'rejected' WHERE company != 'Hooli'")
    c.commit()
    c.close()
    page = client.get("/applications?view=stale").text
    assert "Needs follow-up" in page
    assert "Hooli" in page and "Example Bank" not in page


def test_partials_render(client):
    assert client.get("/applications/sidebar").status_code == 200
    assert client.get("/applications/up-next").status_code == 200


def test_short_date_and_ago():
    today = date(2027, 10, 24)
    assert short_date("2027-10-07", today) == "7 Oct"
    assert short_date("2026-10-07", today) == "7 Oct 2026"
    assert short_date(None, today) == ""
    assert [ago(0), ago(1), ago(16)] == ["Today", "Yesterday", "16 days ago"]


def test_static_assets_are_fingerprinted_and_revalidated(client):
    page = client.get("/applications").text
    assert "/static/style.css?v=" in page and "/static/app.js?v=" in page
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "no-cache"


def test_table_dates_fit_narrow_columns():
    from jobhunt.web.app import table_date
    today = date(2026, 10, 27)
    assert table_date("2026-10-07", today) == "7 Oct" and table_date("2025-06-26", today) == "Jun 2025"
