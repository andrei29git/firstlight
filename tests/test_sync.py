"""Sync against a local bare repository standing in for GitHub."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from jobhunt import sync
from jobhunt.state import State

ENV = {**os.environ, **sync.FALLBACK_IDENTITY}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True).stdout.decode()


def job(key: str, updated_at: str = "2026-10-25T08:00:00+00:00", title: str = "Engineer", source: str = "greenhouse"):
    return {"key": key, "company": "Acme", "title": title, "url": "u", "source": source, "source_id": key,
            "locations": [], "prefilter": "pass", "first_seen": "2026-10-25T08:00:00+00:00", "closed": False,
            "updated_at": updated_at, "description_hash": None}


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "--quiet", "--bare", str(bare))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", "--quiet", str(bare), str(seed))
    (seed / "README.md").write_text("code\n")
    sh(seed, "add", "-A")
    sh(seed, "commit", "--quiet", "-m", "main")
    sh(seed, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    sh(seed, "switch", "--quiet", "--orphan", "data")
    (seed / "README.md").write_text("state\n")
    (seed / "jobs.jsonl").write_text(json.dumps(job("greenhouse:Acme:1")) + "\n")  # the older single file
    (seed / "runs.jsonl").write_text(json.dumps({"started_at": "2026-10-24T00:00:00+00:00"}) + "\n")
    sh(seed, "add", "-A")
    sh(seed, "commit", "--quiet", "-m", "snapshot")
    sh(seed, "push", "--quiet", "origin", "data")
    return bare


def clone(tmp_path, remote, name):
    work = tmp_path / name
    sh(tmp_path, "clone", "--quiet", str(remote), str(work))
    return work


def remote_jobs(tmp_path, remote) -> set[str]:
    check = clone(tmp_path, remote, f"check{len(list(tmp_path.iterdir()))}")
    sh(check, "switch", "--quiet", "data")
    keys = set()
    for path in (check / "jobs").rglob("*.jsonl"):
        keys |= {json.loads(line)["key"] for line in path.read_text().splitlines()}
    assert not (check / "jobs.jsonl").exists()
    return keys


def test_init_merges_a_plain_folder_into_the_branch(tmp_path, remote):
    work = clone(tmp_path, remote, "work")
    state = State(work / ".data")
    state.jobs["greenhouse:Acme:2"] = job("greenhouse:Acme:2")
    state.save()
    state.append_run({"started_at": "2026-10-25T00:00:00+00:00"})
    (work / ".data" / "harvest" / "index").mkdir(parents=True)
    (work / ".data" / "harvest" / "index" / "cache.txt").write_text("big")

    result = sync.init(work, work / ".data")
    assert result.pushed and sync.is_worktree(work / ".data")
    assert remote_jobs(tmp_path, remote) == {"greenhouse:Acme:1", "greenhouse:Acme:2"}
    assert len(State(work / ".data").runs()) == 2
    assert (work / ".data" / "harvest" / "index" / "cache.txt").exists()  # local only, not pushed
    assert "harvest/index" not in sh(work / ".data", "ls-files")
    assert list(tmp_path.glob("work/.data.before-sync-*"))


def test_two_writers_and_pulls(tmp_path, remote):
    one, two = clone(tmp_path, remote, "one"), clone(tmp_path, remote, "two")
    sync.init(one, one / ".data")
    sync.init(two, two / ".data")

    s1 = State(one / ".data")
    s1.jobs["lever:Acme:a"] = job("lever:Acme:a", source="lever")
    s1.save()
    sync.push(one / ".data", "run on one")
    s2 = State(two / ".data")
    s2.jobs["greenhouse:Acme:b"] = job("greenhouse:Acme:b")
    s2.save()
    sync.push(two / ".data", "run on two")  # merges one's job before pushing
    assert remote_jobs(tmp_path, remote) == {"greenhouse:Acme:1", "lever:Acme:a", "greenhouse:Acme:b"}

    result = sync.pull(one / ".data")
    assert "greenhouse:Acme:b" in State(one / ".data").jobs and result.changed_files
    assert sync.pull(one / ".data").changed_files == []  # idempotent
    assert sh(one / ".data", "status", "--porcelain") == ""


def test_a_rejected_push_is_retried(tmp_path, remote, monkeypatch):
    one, two = clone(tmp_path, remote, "one"), clone(tmp_path, remote, "two")
    sync.init(one, one / ".data")
    sync.init(two, two / ".data")
    s2 = State(two / ".data")
    s2.jobs["greenhouse:Acme:late"] = job("greenhouse:Acme:late")
    s2.save()

    real_git = sync.git
    raced = []

    def racing_git(cwd, *args, **kw):
        if args[:1] == ("push",) and cwd == two / ".data" and not raced:
            raced.append(True)  # someone else pushes between our merge and our push
            s1 = State(one / ".data")
            s1.jobs["greenhouse:Acme:early"] = job("greenhouse:Acme:early")
            s1.save()
            monkeypatch.setattr(sync, "git", real_git)
            sync.push(one / ".data", "run on one")
            monkeypatch.setattr(sync, "git", racing_git)
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(sync, "git", racing_git)
    result = sync.push(two / ".data", "run on two")
    assert result.pushed and result.attempts == 2
    assert {"greenhouse:Acme:late", "greenhouse:Acme:early"} <= remote_jobs(tmp_path, remote)


def test_newer_record_wins_and_logs_combine():
    ours = [job("k", "2026-10-25T09:00:00+00:00", title="ours")]
    theirs = [job("k", "2026-10-25T10:00:00+00:00", title="theirs"), job("j")]
    merged = sync.merge_keyed(ours, theirs, "key", "updated_at")
    assert [(r["key"], r["title"]) for r in merged] == [("j", "Engineer"), ("k", "theirs")]
    assert sync.merge_keyed(theirs[:1], ours, "key", "updated_at")[0]["title"] == "theirs"  # older loses
    runs = sync.merge_log([{"started_at": "b"}, {"started_at": "a"}], [{"started_at": "a"}, {"started_at": "c"}],
                          ("started_at",))
    assert [r["started_at"] for r in runs] == ["a", "b", "c"]
    scores = sync.merge_keyed([{"key": "k", "model": "haiku", "scored_at": "1"}],
                              [{"id": "k@haiku", "key": "k", "model": "haiku", "scored_at": "2"}], "id", "scored_at")
    assert len(scores) == 1 and scores[0]["scored_at"] == "2"


def test_pull_touches_only_the_state_folder(tmp_path, remote):
    work = clone(tmp_path, remote, "work")
    database = work / "jobhunt.db"
    database.write_bytes(b"the user's edits")
    sync.init(work, work / ".data")
    sync.pull(work / ".data")
    assert database.read_bytes() == b"the user's edits"


def test_lock_makes_a_waiting_free_pull_report_busy(tmp_path, remote):
    work = clone(tmp_path, remote, "work")
    sync.init(work, work / ".data")
    with sync.state_lock(work / ".data"):
        with pytest.raises(sync.Busy):
            sync.pull(work / ".data", wait=False)


def test_the_lock_says_who_holds_it_and_since_when(tmp_path):
    from datetime import UTC, datetime
    folder = tmp_path / ".data"
    with sync.state_lock(folder, holder="the harvest"):
        held = sync.lock_holder(folder)
        assert held["holder"] == "the harvest" and held["pid"] > 0
        with pytest.raises(sync.Busy) as busy:
            with sync.state_lock(folder, wait=False, holder="the app's sync"):
                pass
        since = datetime.fromisoformat(held["since"])
        assert str(busy.value) == f"The harvest is writing the state (since {since.strftime('%H:%M')} UTC)"
        assert sync.lock_holder(folder)["holder"] == "the harvest"  # the waiting process left the note alone
    assert sync.lock_holder(folder) is None  # released: the note is gone
    other_day = {"holder": "a backfill run", "since": "2026-10-26T23:05:00+00:00"}
    assert sync.busy_text(other_day, datetime(2026, 10, 27, 9, tzinfo=UTC)) == \
        "A backfill run is writing the state (since 26 Oct 23:05 UTC)"
    assert sync.busy_text(None) == "Another process is writing the state"


def test_pull_needs_init(tmp_path):
    with pytest.raises(sync.SyncError, match="sync --init"):
        sync.pull(tmp_path / ".data")


def test_a_pull_reads_only_what_the_remote_changed(tmp_path, remote, monkeypatch):
    one, two = clone(tmp_path, remote, "one"), clone(tmp_path, remote, "two")
    sync.init(one, one / ".data")
    sync.init(two, two / ".data")
    State(one / ".data").save()  # the remote starts in the older single-file layout: migrate it once
    sync.push(one / ".data", "migrate the layout")
    sync.pull(two / ".data")
    s1 = State(one / ".data")
    s1.scores["k@haiku"] = {"id": "k@haiku", "key": "k", "model": "haiku", "scored_at": "1"}
    s1.save()
    sync.push(one / ".data", "scores only")
    # The laptop changed a job file the remote did not touch.
    s2 = State(two / ".data")
    s2.jobs["greenhouse:Acme:local"] = job("greenhouse:Acme:local")
    s2.save()
    requested = []
    real = sync.remote_files
    monkeypatch.setattr(sync, "remote_files", lambda d, ref, paths=None: requested.append(paths) or real(d, ref, paths))
    result = sync.pull(two / ".data")
    assert requested == [{"scores.jsonl"}] and result.changed_files == ["scores.jsonl"]
    after = State(two / ".data")
    assert "k@haiku" in after.scores and "greenhouse:Acme:local" in after.jobs  # both sides kept


def test_saving_leaves_unchanged_files_alone(tmp_path):
    state = State(tmp_path / "s")
    state.jobs["greenhouse:Acme:1"] = job("greenhouse:Acme:1")
    state.jobs["greenhouse:Gen:1"] = {**job("greenhouse:Gen:1"), "company": "Gen", "origin": "generated"}
    state.save()
    files = sorted(p.relative_to(tmp_path / "s").as_posix() for p in (tmp_path / "s" / "jobs").rglob("*.jsonl"))
    from jobhunt.state import company_slot
    assert files == ["jobs/config/greenhouse.jsonl", f"jobs/generated/{company_slot('greenhouse', 'Gen'):02d}.jsonl"]
    stamps = {p: p.stat().st_mtime_ns for p in (tmp_path / "s").rglob("*.jsonl")}
    import time
    time.sleep(0.01)
    State(tmp_path / "s").save()
    assert {p: p.stat().st_mtime_ns for p in (tmp_path / "s").rglob("*.jsonl")} == stamps


def test_older_layouts_are_read_and_replaced(tmp_path):
    folder = tmp_path / "s"
    (folder / "jobs").mkdir(parents=True)
    (folder / "jobs" / "greenhouse.jsonl").write_text(json.dumps(job("greenhouse:Acme:1", "2026-01-01")) + "\n")
    (folder / "jobs.jsonl").write_text(json.dumps(job("greenhouse:Acme:1", "2026-02-01", title="newer")) + "\n")
    state = State(folder)
    assert state.jobs["greenhouse:Acme:1"]["title"] == "newer"
    state.save()
    assert not (folder / "jobs.jsonl").exists() and not (folder / "jobs" / "greenhouse.jsonl").exists()
    assert (folder / "jobs" / "config" / "greenhouse.jsonl").exists()


def test_line_separators_inside_values_never_split_a_record(tmp_path):
    """One posting held "\\x85" (2026-10-28): str.splitlines() split its record and every run stopped."""
    from jobhunt.state import parse_lines, read_jsonl, write_jsonl
    records = [{"key": "a", "title": "Engineer\x85Intern", "text": "one two three"}, {"key": "b"}]
    write_jsonl(tmp_path / "x.jsonl", records)
    raw = (tmp_path / "x.jsonl").read_text(encoding="utf-8")
    assert raw.count("\n") == 2 and "\x85" not in raw and "\\u0085" in raw
    assert read_jsonl(tmp_path / "x.jsonl") == records
    written_before = '{"key": "a", "title": "Engineer\x85Intern"}\n{"key": "b"}\n'  # as runs wrote it until the fix
    assert parse_lines(written_before) == [{"key": "a", "title": "Engineer\x85Intern"}, {"key": "b"}]
    assert sync._records(written_before.encode()) == parse_lines(written_before)
