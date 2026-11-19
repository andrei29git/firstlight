"""The wishlist: its file on the data branch, how runs treat wishlist companies, and the app's
stars, sidebar and Add sheet."""

from __future__ import annotations

import argparse
import html as htmllib
import json
import shutil

from fastapi.testclient import TestClient

from jobhunt import cli, sync, wishlist
from jobhunt import config as cfg
from jobhunt import detect as det
from jobhunt.companies import Company
from jobhunt.state import State, write_jsonl
from jobhunt.web.app import create_app

from .conftest import EXAMPLE_CONFIG
from .test_generated import ACME, by_title, run, settings  # noqa: F401 (fixture)
from .test_jobs_web import client, job, rows_in, score  # noqa: F401 (fixture)
from .test_sync import remote  # noqa: F401 (fixture)

WISHED_ACME = Company(name="Acme", board="greenhouse", slug="acme", origin="wishlist", wishlist=True,
                      identity="greenhouse:acme")


def models_called(claude) -> list[str]:
    return [c["argv"][c["argv"].index("--model") + 1] for c in claude.calls]


# --- The file -------------------------------------------------------------------------------------

def test_add_remove_and_add_again(tmp_path):
    entry = wishlist.set_company(tmp_path, "Acme Corp", True, {"board": "greenhouse", "slug": "acme"}, now="1")
    assert entry["identity"] == "greenhouse:acme" and wishlist.names(tmp_path) == {entry["id"]}
    wishlist.set_company(tmp_path, "acme corp", False, now="2")
    assert wishlist.active(tmp_path) == [] and wishlist.load(tmp_path)[entry["id"]]["on"] is False  # kept, off
    again = wishlist.set_company(tmp_path, "Acme Corp", True, now="3")
    assert again["config"] == {"board": "greenhouse", "slug": "acme"} and again["added_at"] == "3"


def test_newest_change_wins_when_merged():
    laptop = [{"id": "acme", "name": "Acme", "on": False, "updated_at": "2026-10-25T10:00:00+00:00"}]
    remote = [{"id": "acme", "name": "Acme", "on": True, "updated_at": "2026-10-25T09:00:00+00:00"},
              {"id": "initech", "name": "Initech", "on": True, "updated_at": "2026-10-25T09:00:00+00:00"}]
    merged = sync.merge_file(wishlist.FILE, sync._dump(laptop), sync._dump(remote))
    assert {r["id"]: r["on"] for r in sync._records(merged)} == {"acme": False, "initech": True}


def test_flags_move_out_of_companies_toml(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config)
    assert wishlist.import_toml(config, tmp_path / "state") == ["Stripe"]
    assert "wishlist" not in (config / "companies.toml").read_text().split("# ---")[-1].replace("# wishlist", "")
    assert [e["name"] for e in wishlist.active(tmp_path / "state")] == ["Stripe"]
    assert wishlist.import_toml(config, tmp_path / "state") == []  # once


def test_runs_fetch_wishlist_companies_every_time_and_not_from_the_generated_list(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    write_jsonl(state / "companies_generated.jsonl", [
        {"identity": "greenhouse:acme", "board": "greenhouse", "slug": "acme", "name": "Acme"},
        {"identity": "greenhouse:initech", "board": "greenhouse", "slug": "initech", "name": "Initech"}])
    wishlist.set_company(state, "Acme", True, {"board": "greenhouse", "slug": "acme"})
    wishlist.set_company(state, "Winton", True, {"board": "greenhouse", "slug": "winton"})  # in companies.toml
    wishlist.set_company(state, "Nameonly", True)
    paths = cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "db", backups=tmp_path / "b")
    args = argparse.Namespace(no_fetch=False, sonnet_only=False, names=[], local_only=False, generated="all")
    targets = cli.run_targets(args, paths, cfg.load_settings(EXAMPLE_CONFIG), state, say=lambda _: None)
    origins = {(c.name, c.origin) for c in targets}
    assert ("Acme", "wishlist") in origins and ("Acme", "generated") not in origins
    assert ("Initech", "generated") in origins and ("Winton", "config") in origins
    assert sum(c.name == "Winton" for c in targets) == 1 and "Nameonly" not in {c.name for c in targets}


# --- Runs -----------------------------------------------------------------------------------------

def test_a_wishlisted_generated_company_is_scored_like_my_companies_and_back_again(tmp_path, settings):
    _, state, _, _ = run(tmp_path, settings)  # on the generated list: title check, Haiku, Sonnet; UK parked
    jobs = by_title(state)
    assert jobs["Data Engineer"]["parked"] and jobs["Mobile Engineer"]["titlecheck"]["dropped"]

    summary, state, claude, _ = run(tmp_path, settings, companies=(WISHED_ACME,))
    jobs = by_title(state)
    assert {r["origin"] for r in jobs.values()} == {"wishlist"} and not jobs["Data Engineer"]["parked"]
    assert sorted(models_called(claude)) == ["sonnet", "sonnet"]  # UK role and the title-check drop, directly
    assert "generated titlecheck" not in summary["scoring"]
    assert set(state.scores_for(jobs["Data Engineer"]["key"])) == {"sonnet"}
    assert not state.scores_for(jobs["Backend Engineer"]["key"])  # left out by the backlog rule: still out
    assert all(state_file.startswith("jobs/config/") for state_file in
               [p.relative_to(tmp_path / "state").as_posix() for p in (tmp_path / "state" / "jobs").rglob("*.jsonl")
                if "acme" in p.read_text().lower()])

    _, state, claude, _ = run(tmp_path, settings)  # removed: back to the generated-list rules
    jobs = by_title(state)
    assert {r["origin"] for r in jobs.values()} == {"generated"} and jobs["Data Engineer"]["parked"]
    assert claude.calls == [] and set(state.scores_for(jobs["Data Engineer"]["key"])) == {"sonnet"}  # kept


def test_a_new_wishlist_company_follows_the_backlog_rule_at_its_first_fetch(tmp_path, settings):
    summary, state, claude, session = run(tmp_path, settings, companies=(WISHED_ACME,))
    jobs = by_title(state)
    assert jobs["Backend Engineer"]["backfill_skip"] and not jobs["Graduate Engineer"]["backfill_skip"]
    assert sorted(models_called(claude)) == ["sonnet"] * 3  # Graduate, Mobile and the UK role; no title check
    assert not any(u.endswith("/jobs/4") for u in session.urls())  # no detail page for the skipped role


# --- Pushing ----------------------------------------------------------------------------------------

def test_a_change_is_pushed_for_the_next_run(tmp_path, remote):  # noqa: F811
    from .test_sync import clone, sh
    bare = remote
    work = clone(tmp_path, bare, "work")
    sync.init(work, work / ".data")
    wishlist.set_company(work / ".data", "Acme", True, {"board": "greenhouse", "slug": "acme"})
    assert sync.changed_locally(work / ".data", wishlist.FILE)
    with sync.state_lock(work / ".data"):
        sync.push_locked(work / ".data", "Wishlist: add Acme")
    assert not sync.changed_locally(work / ".data", wishlist.FILE)
    runner = clone(tmp_path, bare, "actions")  # what a scheduled run checks out
    sh(runner, "switch", "--quiet", "data")
    assert [e["name"] for e in wishlist.active(runner)] == ["Acme"]


# --- The app ----------------------------------------------------------------------------------------

def test_star_a_role_and_every_list_follows(client, paths):  # noqa: F811
    response = client.post("/wishlist/star", data={"key": "a", "on": "1", "list_view": "scored"})
    assert 'class="star-btn on"' in response.text
    events = json.loads(response.headers["HX-Trigger"])
    assert events["wishlist-changed"]["name"] == "Acme" and events["wishlist-updated"]["on"] is True
    html = client.get("/jobs").text
    assert html.count('class="star-btn on"') == 2  # both Acme roles (a, b); Initech's is not
    assert 'href="/jobs?view=all&amp;company=acme"' in html  # in the sidebar
    assert rows_in(client.get("/jobs?view=all&company=acme").text) == ["a", "b", "f"]
    assert rows_in(client.get("/jobs?wishlist=1").text) == ["a", "b"]
    removed = client.post("/wishlist/set", data={"name": "Acme", "on": "0"}, headers={"HX-Request": "true"})
    assert removed.status_code == 200 and json.loads(removed.headers["HX-Trigger"])["wishlist-changed"]["on"] is False
    assert 'class="star-btn on"' not in client.get("/jobs").text
    undo = client.post("/wishlist/set", data={"name": "Acme", "on": "1", "undo": "1"}, headers={"HX-Request": "true"})
    assert "wishlist-changed" not in json.loads(undo.headers["HX-Trigger"])  # no second toast
    assert client.get("/jobs").text.count('class="star-btn on"') == 2


def test_star_from_job_detail(client):  # noqa: F811
    response = client.post("/wishlist/star", data={"key": "c", "on": "1", "from": "detail"}, follow_redirects=False)
    assert response.status_code == 303 and "notice=Added+Initech" in response.headers["location"]
    assert "On wishlist" in client.get("/job?key=c").text


def test_adding_a_company_the_tool_knows(client, paths):  # noqa: F811
    write_jsonl(paths.state_dir / "companies_generated.jsonl", [
        {"identity": "ashby:hooli", "board": "ashby", "slug": "hooli", "name": "Hooli", "open_roles": 12}])
    html = client.post("/wishlist/find", data={"name": "hooli"}).text
    assert "on the generated list, 12 open roles" in html and "save the name only" in html
    choice = htmllib.unescape(html.split('name="choice" value=\'')[1].split("'")[0])
    saved = client.post("/wishlist/set", data={"on": "1", "choice": choice}, follow_redirects=False)
    assert saved.status_code == 303 and "company=hooli" in saved.headers["location"]
    entry = wishlist.load(paths.state_dir)["hooli"]
    assert entry["identity"] == "ashby:hooli" and entry["name"] == "Hooli" and entry["source"] == "generated list"


def test_adding_an_unknown_company_runs_detect_and_saves_nothing_until_chosen(client, paths, monkeypatch):  # noqa: F811
    found = det.Finding(board="lever", config={"board": "lever", "slug": "globex", "region": "eu"}, supported=True,
                        jobs=7, sample="https://jobs.eu.lever.co/globex/1")
    calls = []
    monkeypatch.setattr(det, "detect", lambda url, http, name=None: calls.append((url, name)) or ([found], url))
    html = client.post("/wishlist/find", data={"name": "Globex", "url": "https://globex.example/careers"}).text
    assert calls == [("https://globex.example/careers", "Globex")]
    assert "lever globex (EU): 7 jobs" in html and "jobs.eu.lever.co/globex/1" in html
    assert wishlist.load(paths.state_dir) == {}  # shown, not saved
    nothing = det.Finding(board="teamtailor", config={"board": "teamtailor"}, supported=False)
    monkeypatch.setattr(det, "detect", lambda url, http, name=None: ([nothing], url))
    html = client.post("/wishlist/find", data={"name": "Initrode"}).text
    # A name alone that nothing answers for: the reasons are shown and the name is saved star-only.
    assert "teamtailor, which the tool cannot read yet" in html and "Saved with the star only" in html
    assert "A web search found no careers page: no search in tests." in html
    entry = wishlist.load(paths.state_dir)["initrode"]
    assert entry["on"] and not entry.get("identity") and entry["source"] == "name only"
