"""`jobhunt serve --demo` (2026-11-18): the example candidate with the recorded demo/state, in a
temporary folder, with Claude calls switched off and none of the tool's own tasks on Today."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from jobhunt import cli, demo
from jobhunt.web.app import create_app


def test_the_demo_runs_on_recorded_data_in_a_temporary_folder():
    paths = cli.demo_paths()
    assert paths.config.parent == paths.root and (paths.config / "profile.md").exists()  # a copy of the example config
    assert paths.state_dir != cli.PACKAGE_ROOT / "demo" / "state" and (paths.state_dir / "scores.jsonl").exists()
    with TestClient(create_app(paths, runner=cli.demo_runner, demo=True)) as client:
        jobs = client.get("/jobs?view=all")
        assert jobs.status_code == 200 and "roles" in jobs.text
        today = client.get("/")
        assert today.status_code == 200 and "cron-job.org" not in today.text
        assert "Demo: recorded data" in client.get("/jobs?view=all").text
    demo.remove(paths.root)
    assert not paths.root.exists()


def test_the_demo_makes_no_claude_calls():
    code, _, error = cli.demo_runner(["claude"], "", ".", 1.0, {})
    assert code != 0 and "Demo mode" in error


def test_the_demo_looks_in_use_on_any_day():
    from datetime import UTC, datetime, timedelta

    assert demo.shift("2026-11-19T15:00:00+00:00 and 2026-11-12", timedelta(days=10, hours=2)) == \
        "2026-11-29T17:00:00+00:00 and 2026-11-22"
    paths = demo.prepare(cli.PACKAGE_ROOT, now=datetime.now(UTC) + timedelta(days=40))
    with TestClient(create_app(paths, runner=cli.demo_runner, demo=True)) as client:
        apply = client.get("/jobs?view=apply").text
        assert apply.count('class="row job"') == 8  # the strong roles not applied to yet, whatever the date
        apps = client.get("/applications").text
        assert "Interviewing" in apps and "Kestrel Pay" in apps
        companies = client.get("/companies").text
        assert "Polderbyte" in companies and "Mistral" not in companies  # only invented companies
    demo.remove(paths.root)


def test_the_demo_runs_on_its_own_port_and_takes_its_folder_with_it(monkeypatch):
    """2026-11-19: port 8001, so the browser keeps the demo's theme and remembered settings apart
    from the real app's on 8000; the temporary folder is deleted when the server stops."""
    import signal

    import uvicorn
    seen = {}
    monkeypatch.setattr(signal, "signal", lambda *args: None)  # the test process keeps its own handlers
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, log_level: seen.update(host=host, port=port))
    folders = []
    real_paths = cli.demo_paths
    monkeypatch.setattr(cli, "demo_paths", lambda: folders.append(real_paths()) or folders[-1])
    cli.main(["serve", "--demo"])
    assert seen == {"host": "127.0.0.1", "port": cli.DEMO_PORT} and cli.DEMO_PORT == 8001 != cli.PORT
    assert not folders[0].root.exists()


def test_demo_folders_left_over_from_earlier_runs_are_removed():
    def folder(owner: int | None) -> Path:
        made = Path(tempfile.mkdtemp(prefix=demo.PREFIX))
        if owner is not None:
            (made / demo.OWNER).write_text(str(owner))
        return made
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead, older, running = folder(int(gone.stdout)), folder(None), folder(os.getppid())
    try:
        demo.remove_left_over()
        assert not dead.exists() and not older.exists()
        assert running.exists()  # a demo still being served keeps its folder
    finally:
        demo.remove(running)
    demo.remove(Path("/tmp/not-ours"))  # only our own folders, in the temp folder, are ever deleted


def test_a_role_to_apply_is_not_an_application_anywhere():
    """2026-11-19: Danubit was "1 application · To apply" on Companies while Applications left it
    out and its role said "Not applied". To apply is not an application (decided 2026-10-25):
    Companies and the limits leave it out too, and the role says To apply."""
    paths = cli.demo_paths()
    try:
        with TestClient(create_app(paths, runner=cli.demo_runner, demo=True)) as client:
            jobs = client.get("/jobs?view=all&company=danubit").text
            key = next(k for k in re.findall(r'data-key="([^"]+)"', jobs) if "db-1" in k)
            role = client.get("/job", params={"key": key}).text
            companies = client.get("/companies").text
            applications = client.get("/applications").text
        danubit = companies.split('data-company-id="danubit"')[1].split("</tr>")[0]
        assert "application" not in danubit  # no "1 application · To apply"
        assert "Danubit" not in applications
        assert "<span>To apply</span>" in role and "Not applied" not in role
    finally:
        demo.remove(paths.root)


def test_find_more_companies_in_the_demo_cannot_search_and_ticks_the_candidates_countries():
    paths = cli.demo_paths()
    try:
        with TestClient(create_app(paths, runner=cli.demo_runner, demo=True)) as client:
            sheet = client.get("/companies/find").text
            settings = client.get("/settings").text
    finally:
        demo.remove(paths.root)
    assert "Estimated cost" not in sheet and "hx-post" not in sheet.split("</form>")[0].split(">")[0]
    assert '<button class="btn btn-primary" type="submit" disabled' in sheet
    ticked = re.findall(r'name="country" value="(\w+)" checked', sheet)
    assert ticked == ["NL", "DE", "FR", "ES", "RO"]  # the candidate's own, so the section is not empty
    github = settings.split("<dt>GitHub runs</dt>")[1].split("</dd>")[0]
    assert "Not used in the demo" in github
