"""`jobhunt serve --demo`: the app on invented data, with no network or Claude calls (the app's
demo switches; tests/e2e/test_sealed.py checks it).

The example candidate's config (tests/example_config), with demo/config's companies and limits on
top, and the recorded demo/state, are copied into a fresh temporary folder; demo/tracker.json fills
its database (applications, notes, a hidden role and application-help drafts). Every date in the
copy moves forward by the time since the demo was recorded, so the demo always looks in use.
`scripts/build_demo.py` writes the demo data. The folder is deleted when the server stops, and any
left over from an earlier run (a crash, a killed process) when the next demo starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import config as cfg
from . import store

TIMESTAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\+00:00|Z)?")
DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b(?!T)")


def shift(text: str, delta: timedelta) -> str:
    """Move every ISO timestamp by `delta` and every bare date by its whole days."""
    def moved_time(m: re.Match) -> str:
        at = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC) + delta
        return at.strftime("%Y-%m-%dT%H:%M:%S") + (m.group(2) or "")

    def moved_day(m: re.Match) -> str:
        return (date.fromisoformat(m.group(1)) + timedelta(days=round(delta.total_seconds() / 86400))).isoformat()
    return DAY.sub(moved_day, TIMESTAMP.sub(moved_time, text))


PREFIX = "jobhunt-demo-"
OWNER = "demo.pid"  # the process that serves it; a folder whose process has gone is left over


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove(folder: Path) -> None:
    """Delete a demo's temporary folder (only ever one of ours, in the system's temp folder)."""
    folder = Path(folder)
    if folder.name.startswith(PREFIX) and folder.parent.resolve() == Path(tempfile.gettempdir()).resolve():
        shutil.rmtree(folder, ignore_errors=True)


def remove_left_over() -> int:
    """Delete demo folders left by earlier runs (2026-11-19): those whose serving process has gone,
    or that predate the owner file. A demo still being served keeps its folder."""
    removed = 0
    for folder in Path(tempfile.gettempdir()).glob(PREFIX + "*"):
        try:
            pid = int((folder / OWNER).read_text().strip())
        except (OSError, ValueError):
            pid = None
        if folder.is_dir() and (pid is None or (pid != os.getpid() and not _alive(pid))):
            remove(folder)
            removed += 1
    return removed


def prepare(package_root: Path, now: datetime | None = None) -> cfg.Paths:
    now = now or datetime.now(UTC)
    demo = package_root / "demo"
    remove_left_over()
    folder = Path(tempfile.mkdtemp(prefix=PREFIX))
    (folder / OWNER).write_text(str(os.getpid()))
    config = folder / "config"
    shutil.copytree(package_root / "tests" / "example_config", config)
    for overlay in (demo / "config").glob("*.toml"):
        shutil.copy2(overlay, config / overlay.name)
    with (config / "settings.toml").open("a", encoding="utf-8") as settings:
        settings.write('\n[accounts]\ntoken = "subscription"\n')
    state = folder / "state"
    shutil.copytree(demo / "state", state)
    anchor = datetime.fromisoformat(json.loads((state / "demo.json").read_text())["anchor"])
    delta = now - anchor
    for path in state.rglob("*.jsonl"):  # postings keep their text (their names are its hash)
        path.write_text(shift(path.read_text(encoding="utf-8"), delta), encoding="utf-8")
    paths = cfg.Paths(root=folder, config=config, database=folder / "jobhunt.db", backups=folder / "backups", state=state)
    runs = [json.loads(line) for line in (state / "runs.jsonl").read_text().splitlines() if line.strip()]
    (folder / "actions_usage.json").write_text(json.dumps({"runs": {}, "result": {
        "month": now.strftime("%Y-%m"), "minutes": 1184, "included": 3000, "projected": 2310,
        "by_workflow": {"ci": 64, "run": 1120}, "runs": 240, "updated_at": now.isoformat(timespec="seconds"),
        "last_dispatch": None,
        "latest_runs": [{"at": r["started_at"], "conclusion": "success", "url": None, "event": "workflow_dispatch",
                         "seconds": r.get("seconds")} for r in reversed(runs) if r.get("ran_on") == "github"][:8]}}))
    tracker = json.loads(shift((demo / "tracker.json").read_text(encoding="utf-8"), delta))
    conn = store.connect(paths.database)
    try:
        load_tracker(conn, tracker, now)
    finally:
        conn.close()
    return paths


def load_tracker(conn, tracker: dict, now: datetime) -> None:
    """The demo's own data, as the app would have recorded it over the last weeks."""
    with conn:
        for item in tracker["applications"]:
            history = item["history"]
            applied = next((at[:10] for stage, at in history if stage == "applied"), None)
            key = f"job:{item['job_key']}" if item.get("job_key") else store.manual_key(
                item["company"], item["role"], item.get("location", ""), applied)
            cur = conn.execute(
                """INSERT INTO applications (key, job_key, company, role, location, stage, applied_on, next_step_by,
                                             notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, item.get("job_key"), item["company"], item["role"], item.get("location", ""), item["stage"],
                 applied, item.get("next_step_by"), item.get("notes", ""), history[0][1], history[-1][1]))
            for stage, at in history:
                conn.execute("INSERT INTO stage_history (application_id, stage, changed_at, approximate, source) "
                             "VALUES (?, ?, ?, 0, 'web')", (cur.lastrowid, stage, at))
        conn.execute("INSERT INTO meta (key, value) VALUES ('seed_imported', ?)", (now.isoformat(timespec="seconds"),))
    for key, notes in tracker.get("notes", {}).items():
        store.set_job_mark(conn, key, notes=notes)
    for key in tracker.get("hidden", []):
        store.set_job_mark(conn, key, hidden="1")
    for key, questions in tracker.get("questions", {}).items():
        store.save_helper_questions(conn, key, "greenhouse", questions)
    for d in tracker.get("drafts", []):
        question = next((q["label"] for q in tracker.get("questions", {}).get(d["job_key"], []) if q["id"] == d["qid"]), None)
        store.save_helper_draft(conn, d["job_key"], d["kind"], d["qid"], d["answer"], d.get("choices") or [],
                                d.get("missing") or [], question=question)
    for key, messages in tracker.get("messages", {}).items():
        for role, content in messages:
            store.add_helper_message(conn, key, role, content)
    # The last visit to Today was two days ago, so the newest roles show as new.
    store.today_since(conn, now - timedelta(days=2, hours=3))
