from __future__ import annotations

from datetime import date

import pytest

from jobhunt import config as cfg
from jobhunt import store

OPTIONS = {"ghosted_after_days": 21, "next_step_warning_days": 3}


def seed(conn, paths):
    return store.import_seed(conn, cfg.load_applications_seed(paths.config))


def views(conn, today=date(2027, 10, 10), **kw):
    return store.list_applications(conn, today=today, **OPTIONS, **kw)


def test_seed_imports_once(conn, paths):
    assert seed(conn, paths) == 5
    assert seed(conn, paths) == 0
    assert len(views(conn)) == 5


def test_same_role_in_two_cities_gets_two_keys(conn, paths):
    seed(conn, paths)
    hooli = [v for v in views(conn) if v.company == "Hooli"]
    assert {v.location for v in hooli} == {"Zurich", "London"}


def test_seed_history_marks_unknown_stage_dates(conn, paths):
    seed(conn, paths)
    oa = next(v for v in views(conn) if v.company == "Example Bank")
    history = store.stage_history(conn, oa.id)
    assert [(h["stage"], bool(h["approximate"])) for h in history] == [("applied", False), ("oa_done", True)]
    assert oa.last_change_approximate


def test_seed_keeps_contact_and_cooldown(conn, paths):
    seed(conn, paths)
    on_hold = next(v for v in views(conn) if v.stage == "on_hold")
    assert on_hold.contact == "recruiter@example.com"
    row = conn.execute("SELECT cooldown_until FROM applications WHERE company = 'Initech'").fetchone()
    assert row["cooldown_until"] == "2028-03-15"


def test_stage_change_is_recorded(conn):
    app_id = store.add_application(conn, company="Acme", role="SWE", stage="applied", applied_on="2027-10-01")
    store.update_application(conn, app_id, {"stage": "oa_pending"})
    store.update_application(conn, app_id, {"stage": "oa_pending"})  # no change, no new history row
    assert [h["stage"] for h in store.stage_history(conn, app_id)] == ["applied", "oa_pending"]


def test_reaching_a_sent_stage_fills_applied_on(conn):
    app_id = store.add_application(conn, company="Acme", role="SWE", stage="to_apply")
    assert store.get_application(conn, app_id, today=date(2027, 10, 10), **OPTIONS).applied_on is None
    store.update_application(conn, app_id, {"stage": "applied"}, today=date(2027, 10, 12))
    assert store.get_application(conn, app_id, today=date(2027, 10, 12), **OPTIONS).applied_on == "2027-10-12"


def test_ghosted_after_three_weeks_in_a_waiting_stage(conn):
    app_id = store.add_application(conn, company="Acme", role="SWE", applied_on="2027-09-01")
    conn.execute("UPDATE stage_history SET changed_at = '2027-09-01' WHERE application_id = ?", (app_id,))
    assert views(conn, today=date(2027, 9, 21))[0].ghosted is False
    assert views(conn, today=date(2027, 9, 22))[0].ghosted is True


def test_next_step_status(conn):
    app_id = store.add_application(conn, company="Acme", role="SWE", applied_on="2027-10-01")
    store.update_application(conn, app_id, {"next_step_by": "2027-10-13"})
    assert views(conn, today=date(2027, 10, 9))[0].next_step_status is None
    assert views(conn, today=date(2027, 10, 10))[0].next_step_status == "soon"
    assert views(conn, today=date(2027, 10, 14))[0].next_step_status == "past"
    store.update_application(conn, app_id, {"stage": "rejected"})
    assert views(conn, today=date(2027, 10, 14))[0].next_step_status is None  # closed stages are not due


def test_open_stages_sort_first(conn, paths):
    seed(conn, paths)
    order = [v.stage for v in views(conn)]
    assert order.index("oa_done") < order.index("applied") < order.index("rejected")


def test_duplicate_manual_application_is_refused(conn):
    store.add_application(conn, company="Acme", role="SWE", location="Paris", applied_on="2027-10-01")
    with pytest.raises(store.StoreError, match="already tracked"):
        store.add_application(conn, company="Acme", role="SWE", location="Paris", applied_on="2027-10-01")


@pytest.mark.parametrize("changes, message", [
    ({"stage": "hired"}, "Unknown stage"),
    ({"applied_on": "23/10/2027"}, "must be a date"),
    ({"company": "Other"}, "Cannot edit"),
])
def test_invalid_edits_are_refused(conn, changes, message):
    app_id = store.add_application(conn, company="Acme", role="SWE")
    with pytest.raises(store.StoreError, match=message):
        store.update_application(conn, app_id, changes)


def test_backup_keeps_the_newest_copies(conn, paths, monkeypatch):
    stamps = iter(f"2027010{i}-000000" for i in range(1, 6))

    class FakeNow:
        @staticmethod
        def now():
            class Stamp:
                def strftime(self, _fmt):
                    return next(stamps)
            return Stamp()

    monkeypatch.setattr(store, "datetime", FakeNow)
    for _ in range(5):
        store.backup(conn, paths.backups, keep=3)
    kept = sorted(p.name for p in paths.backups.iterdir())
    assert kept == ["jobhunt-20270103-000000.db", "jobhunt-20270104-000000.db", "jobhunt-20270105-000000.db"]
