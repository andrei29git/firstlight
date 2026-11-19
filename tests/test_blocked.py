"""Blocking a company: its roles leave every view and count, runs neither fetch nor score it,
blocking a wishlist company asks first, and unblocking brings everything back."""

from __future__ import annotations

import argparse
import json

from jobhunt import blocklist, cli, pipeline, sync, wishlist
from jobhunt import config as cfg
from jobhunt.state import State

from .conftest import EXAMPLE_CONFIG
from .test_jobs_web import client, rows_in  # noqa: F401 (fixture)
from .test_pipeline import FLOW, do_run, listing
from .test_pipeline import settings as sequential_settings  # noqa: F401 (fixture)


def block(client, name, on=True, **extra):  # noqa: F811
    return client.post("/blocked/set", data={"name": name, "on": "1" if on else "0", **extra},
                       headers={"HX-Request": "true"})


def test_a_blocked_company_is_nowhere_and_unblocking_brings_it_back(client, paths):  # noqa: F811
    client.post("/job/mark", data={"key": "f", "hidden": "1"})
    response = block(client, "Acme")
    events = json.loads(response.headers["HX-Trigger"])
    assert events["company-blocked"] == {"id": "acme", "name": "Acme", "on": True}
    for url in ("/jobs", "/jobs?view=all&show_hidden=1&show_closed=1", "/jobs?view=hidden", "/jobs?view=waiting",
                "/jobs?q=acme", "/jobs?view=all&company=acme"):
        assert not {"a", "b", "f"} & set(rows_in(client.get(url).text)), url
    html = client.get("/jobs").text
    badge = html.split('href="/jobs?view=scored"')[1].split('class="badge">')[1].split("<")[0]
    assert badge == "1"  # a and b were scored with c; only Initech's c is left
    assert 'href="/jobs?view=hidden"' not in html  # f was the only hidden role
    assert client.get("/job?key=a").status_code == 404
    sheet = client.get("/blocked/sheet").text
    assert "Acme" in sheet and "3 open roles" in sheet and "Blocked" in html
    undo = block(client, "Acme", on=False, undo="1")
    assert "company-blocked" not in json.loads(undo.headers["HX-Trigger"])  # no second toast
    assert {"a", "b"} <= set(rows_in(client.get("/jobs").text))
    assert blocklist.names(paths.state_dir) == frozenset()


def test_blocking_a_wishlist_company_asks_first_and_undo_restores_it(client, paths):  # noqa: F811
    client.post("/wishlist/star", data={"key": "a", "on": "1"})
    row = client.get("/jobs").text.split('data-href="/job?key=a"')[1].split("</tr>")[0]
    assert "hx-confirm=" in row and "is on your wishlist" in row
    other = client.get("/jobs").text.split('data-href="/job?key=c"')[1].split("</tr>")[0]
    assert "hx-confirm=" not in other  # Initech is not on the wishlist: no question
    assert "is on your wishlist" in client.get("/job?key=a").text  # Job detail asks too
    block(client, "Acme")
    assert wishlist.names(paths.state_dir) == frozenset()
    block(client, "Acme", on=False, undo="1")
    assert wishlist.names(paths.state_dir) == {"acme"}


def test_blocking_from_job_detail_returns_to_the_list_with_undo(client):  # noqa: F811
    done = client.post("/blocked/set", data={"name": "Initech", "on": "1", "from": "detail",
                                             "back": "/jobs?view=apply&odds=low"}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/jobs?view=apply&odds=low&blocked_toast=Initech"


def test_runs_neither_fetch_nor_score_blocked_companies(tmp_path, sequential_settings):  # noqa: F811
    state_dir = tmp_path / "state"
    blocklist.set_company(state_dir, "Flow Traders", True)
    summary, _, claude = do_run(tmp_path, sequential_settings, listing(("Graduate Engineer", "Paris, France")))
    assert claude.calls == [] and summary["scoring"]["scheduled"]["pending"] == 0
    assert pipeline.status(State(state_dir), sequential_settings, pipeline.datetime.now(pipeline.UTC)
                           )["parts"]["my companies"].get("waiting for a first score", 0) == 0
    paths = cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "db", backups=tmp_path / "b")
    args = argparse.Namespace(no_fetch=False, sonnet_only=False, names=[], local_only=False, generated="none")
    assert "Winton" in {c.name for c in cli.run_targets(args, paths, sequential_settings, state_dir, say=print)}
    blocklist.set_company(state_dir, "Winton", True)
    names = {c.name for c in cli.run_targets(args, paths, sequential_settings, state_dir, say=lambda _: None)}
    assert "Winton" not in names and "Mistral" in names
    blocklist.set_company(state_dir, "Flow Traders", False)  # unblocked: scored at the next run
    summary, _, claude = do_run(tmp_path, sequential_settings, listing(("Graduate Engineer", "Paris, France")))
    assert len(claude.calls) == 1


def test_the_newest_block_or_unblock_wins_when_merged():
    laptop = [{"id": "acme", "name": "Acme", "on": False, "updated_at": "2026-10-27T10:00:00+00:00"}]
    remote = [{"id": "acme", "name": "Acme", "on": True, "updated_at": "2026-10-27T09:00:00+00:00"}]
    merged = sync.merge_file(blocklist.FILE, sync._dump(laptop), sync._dump(remote))
    assert [r["on"] for r in sync._records(merged)] == [False]
