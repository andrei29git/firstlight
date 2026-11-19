"""Sync the pipeline's state folder with the `data` branch (section 5.4).

The state folder (`.data/`) is a git worktree of the `data` branch. Git's line merge is not used:
records are merged by key instead.
- Jobs, scores and generated companies: the newer record wins (`updated_at`, `scored_at`, `verified`).
- Run, harvest, drop and check logs: the union of both sides.
- Everything else (postings, eval and title-check results, samples): files are written once,
  so a file missing on one side is added.

A writer (a run, Rescore) merges the remote state into its own, commits on top of the remote
and pushes, retrying when someone else pushed first. A pull (the web app) merges the remote
into the local folder and never pushes. The user's own edits live in the database, never here.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .state import dump_line, parse_lines, shard, write_if_changed

BRANCH = "data"
REMOTE = "origin"
PUSH_ATTEMPTS = 4  # the first try and 3 retries
GITIGNORE = "# Local only: the index cache can be downloaded again; logs and locks are per machine.\n" \
            "harvest/index/\n*.log\n*.tmp\n.lock\n"
IGNORED_PREFIXES = ("harvest/index/", ".git", ".lock")
IGNORED_SUFFIXES = (".log", ".tmp")
FALLBACK_IDENTITY = {"GIT_AUTHOR_NAME": "jobhunt", "GIT_AUTHOR_EMAIL": "jobhunt@users.noreply.github.com",
                     "GIT_COMMITTER_NAME": "jobhunt", "GIT_COMMITTER_EMAIL": "jobhunt@users.noreply.github.com"}

KEYED = {"scores.jsonl": ("id", "scored_at"), "companies_generated.jsonl": ("identity", "verified"),
         "wishlist.jsonl": ("id", "updated_at"), "blocked.jsonl": ("id", "updated_at"),
         "accepted_companies.jsonl": ("id", "updated_at"), "preferences.jsonl": ("id", "updated_at"),
         "host_waits.jsonl": ("host", "updated_at")}
LOGS = {"runs.jsonl": ("started_at",), "harvest.jsonl": ("harvested_at",), "drops.jsonl": ("checked_at", "key"),
        "harvest/checked.jsonl": ("checked_at", "identity")}


class SyncError(Exception):
    pass


class Busy(SyncError):
    """Another process (a run, a harvest, another sync) is writing the state folder. The message
    names it when the lock says who holds it: "The harvest is writing the state (since 18:10 UTC)"."""


@dataclass
class Result:
    changed_files: list[str] = field(default_factory=list)
    pushed: bool = False
    commit: str | None = None
    attempts: int = 0
    note: str = ""


# --- Git -----------------------------------------------------------------------------------------

def git(cwd: Path, *args: str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if subprocess.run(["git", "config", "user.email"], cwd=cwd, capture_output=True).returncode != 0:
        env.update(FALLBACK_IDENTITY)  # e.g. a fresh CI machine: commit as the tool, never fail
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, input=input, env=env)
    if check and done.returncode != 0:
        raise SyncError(f"git {' '.join(args[:3])} failed: {done.stderr.decode(errors='replace').strip()[:400]}")
    return done


def is_worktree(state_dir: Path) -> bool:
    return (state_dir / ".git").exists()


def remote_files(state_dir: Path, ref: str, paths: set[str] | None = None) -> dict[str, bytes]:
    """Files of a commit, by path (all, or only `paths`). Blobs are requested by object id:
    asking cat-file for "ref:path" resolves the path every time and took 154 s for 1,200 files;
    by id it takes under a second."""
    entries = []
    for entry in git(state_dir, "ls-tree", "-r", "-z", ref).stdout.decode().split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        if paths is None or path in paths:
            entries.append((path, meta.split()[2]))
    if not entries:
        return {}
    out = git(state_dir, "cat-file", "--batch", input="".join(f"{sha}\n" for _, sha in entries).encode()).stdout
    files, pos = {}, 0
    for path, _ in entries:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        files[path] = out[header_end + 1: header_end + 1 + size]
        pos = header_end + 1 + size + 1
    return files


def remote_tree(state_dir: Path, ref: str) -> dict[str, bytes]:
    """Every file in a commit, by path."""
    return remote_files(state_dir, ref)


# --- Merging -------------------------------------------------------------------------------------

def _records(data: bytes) -> list[dict[str, Any]]:
    return parse_lines(data.decode("utf-8"))


def _dump(records: list[dict[str, Any]]) -> bytes:
    return "".join(dump_line(r) + "\n" for r in records).encode("utf-8")


def _score_id(record: dict[str, Any]) -> str:
    return record.get("id") or f"{record['key']}@{record.get('model', '')}"


def merge_keyed(ours: list[dict[str, Any]], theirs: list[dict[str, Any]], key: str, version: str) -> list[dict[str, Any]]:
    """By key; the newer version wins, ours on a tie. Sorted by key."""
    get = _score_id if key == "id" else (lambda r: r[key])
    merged = {get(r): r for r in ours}
    for record in theirs:
        k = get(record)
        if k not in merged or str(record.get(version) or "") > str(merged[k].get(version) or ""):
            merged[k] = record
    return [merged[k] for k in sorted(merged)]


def merge_log(ours: list[dict[str, Any]], theirs: list[dict[str, Any]], order: tuple[str, ...]) -> list[dict[str, Any]]:
    """The union of two append-only logs, each record once, in order."""
    seen: dict[str, dict[str, Any]] = {}
    for record in [*ours, *theirs]:
        seen.setdefault(json.dumps(record, sort_keys=True), record)
    return sorted(seen.values(), key=lambda r: tuple(str(r.get(f) or "") for f in order))


def _local_files(state_dir: Path) -> dict[str, bytes]:
    files = {}
    for path in state_dir.rglob("*"):
        rel = path.relative_to(state_dir).as_posix()
        if path.is_file() and not rel.startswith(IGNORED_PREFIXES) and not rel.endswith(IGNORED_SUFFIXES) \
                and rel != ".gitignore":
            files[rel] = path.read_bytes()
    return files


def merge_into(target: Path, incoming: dict[str, bytes]) -> list[str]:
    """Merge another copy of the state into the files under `target`. Returns changed paths."""
    current = _local_files(target)
    changed: list[str] = []

    def write(rel: str, data: bytes) -> None:
        if current.get(rel) != data:
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            changed.append(rel)

    # Jobs: all files together (one per source, or an older single jobs.jsonl), merged by key.
    def jobs(files: dict[str, bytes]) -> list[dict[str, Any]]:
        return [r for rel, data in files.items() if rel == "jobs.jsonl" or (rel.startswith("jobs/")
                and rel.endswith(".jsonl")) for r in _records(data)]  # any layout, old or new
    ours_jobs, theirs_jobs = jobs(current), jobs(incoming)
    if theirs_jobs or "jobs.jsonl" in current or any(r.count("/") == 1 for r in current if r.startswith("jobs/")):
        merged = merge_keyed(ours_jobs, theirs_jobs, "key", "updated_at")
        shards: dict[str, list[dict[str, Any]]] = {}
        for record in merged:
            shards.setdefault(shard(record), []).append(record)
        for rel, records in shards.items():
            write(rel, _dump(records))
        for rel in list(current):
            if (rel == "jobs.jsonl" or (rel.startswith("jobs/") and rel not in shards)) and (target / rel).exists():
                (target / rel).unlink()
                changed.append(rel)

    for rel, data in incoming.items():
        if rel == "jobs.jsonl" or rel.startswith("jobs/") or rel.startswith(IGNORED_PREFIXES):
            continue
        if rel in KEYED:
            key, version = KEYED[rel]
            write(rel, _dump(merge_keyed(_records(current.get(rel, b"")), _records(data), key, version)))
        elif rel in LOGS:
            write(rel, _dump(merge_log(_records(current.get(rel, b"")), _records(data), LOGS[rel])))
        elif rel not in current:
            write(rel, data)  # written once: a file only the other side has is added
    return changed


# --- Locking, pulling, pushing -------------------------------------------------------------------

@contextmanager
def state_lock(state_dir: Path, wait: bool = True, timeout: float = 600.0, holder: str = "a sync",
               now: Any = None) -> Iterator[None]:
    """One writer at a time in the state folder (a run, a harvest, a sync, a rescore). The holder
    writes who it is and since when into the lock file, so a process that finds the folder busy
    can say what it is waiting for. The file is opened without truncating it: a waiting process
    must not wipe the holder's note."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / ".lock"
    with path.open("a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait or time.monotonic() > deadline:
                    raise Busy(busy_text(lock_holder(state_dir), now)) from None
                time.sleep(0.5)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"holder": holder, "since": datetime.now(UTC).isoformat(timespec="seconds"),
                                     "pid": os.getpid()}))
            handle.flush()
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_UN)


def lock_holder(state_dir: Path) -> dict[str, Any] | None:
    """Who holds the state lock and since when, as its holder wrote it (None if it did not)."""
    try:
        found = json.loads((state_dir / ".lock").read_text() or "null")
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and found.get("holder") else None


def busy_text(holder: dict[str, Any] | None, now: Any = None) -> str:
    """"The harvest is writing the state (since 18:10 UTC)"; the day is added when it is not today."""
    if not holder:
        return "Another process is writing the state"
    who = str(holder["holder"])
    text = f"{who[:1].upper()}{who[1:]} is writing the state"
    try:
        since = datetime.fromisoformat(holder["since"]).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return text
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    when = since.strftime("%H:%M UTC") if since.date() == today else since.strftime("%-d %b %H:%M UTC")
    return f"{text} (since {when})"


def _fetch(state_dir: Path) -> str:
    git(state_dir, "fetch", "--quiet", REMOTE, BRANCH)
    return f"{REMOTE}/{BRANCH}"


def _ignored(rel: str) -> bool:
    return rel.startswith(IGNORED_PREFIXES) or rel.endswith(IGNORED_SUFFIXES) or rel == ".gitignore"


def merge_file(rel: str, ours: bytes, theirs: bytes) -> bytes:
    """One file both sides changed, merged by record (written-once files: ours)."""
    if rel == "jobs.jsonl" or rel.startswith("jobs/"):
        return _dump(merge_keyed(_records(ours), _records(theirs), "key", "updated_at"))
    if rel in KEYED:
        return _dump(merge_keyed(_records(ours), _records(theirs), *KEYED[rel]))
    if rel in LOGS:
        return _dump(merge_log(_records(ours), _records(theirs), LOGS[rel]))
    return ours


def changed_locally(state_dir: Path, rel: str) -> bool:
    """Whether a file in the state folder has changes not committed (so not pushed) yet."""
    if not is_worktree(state_dir):
        return False
    return bool(git(state_dir, "status", "--porcelain", "--untracked-files=all", "--", rel).stdout.strip())


def pull_locked(state_dir: Path) -> Result:
    """Pull while already holding the state lock. Only files the remote changed since our last
    sync are read: taken as they are when the laptop did not touch them, merged by record when
    both sides did."""
    ref = _fetch(state_dir)
    remote_changes = []
    for line in git(state_dir, "diff", "--name-status", "--no-renames", "-z", "HEAD", ref).stdout.decode().split("\0"):
        remote_changes.append(line)
    pairs = [(remote_changes[i], remote_changes[i + 1]) for i in range(0, len(remote_changes) - 1, 2)
             if remote_changes[i]]
    local_changes = {entry[3:] for entry in git(state_dir, "status", "--porcelain", "-z", "--untracked-files=all",
                                                "--no-renames").stdout.decode().split("\0") if entry}
    wanted = {path for status, path in pairs if status != "D" and not _ignored(path)}
    theirs = remote_files(state_dir, ref, wanted) if wanted else {}
    changed = []
    for status, rel in pairs:
        if _ignored(rel):
            continue
        target = state_dir / rel
        if status == "D":
            if rel not in local_changes and target.exists():
                target.unlink()
                changed.append(rel)
            continue
        data = merge_file(rel, target.read_bytes(), theirs[rel]) if rel in local_changes and target.exists() \
            else theirs[rel]
        if write_if_changed(target, data):
            changed.append(rel)
    if not (state_dir / ".gitignore").exists() or (state_dir / ".gitignore").read_text() != GITIGNORE:
        (state_dir / ".gitignore").write_text(GITIGNORE)
    git(state_dir, "reset", "--quiet", "--mixed", ref)  # stand on the remote's commit; files keep the merge
    return Result(changed_files=changed)


def pull(state_dir: Path, wait: bool = True, holder: str = "a sync") -> Result:
    """Merge the remote state into the local folder. Never pushes."""
    if not is_worktree(state_dir):
        raise SyncError(f"{state_dir} is not linked to the data branch yet: run `jobhunt sync --init`")
    with state_lock(state_dir, wait=wait, holder=holder):
        return pull_locked(state_dir)


def push_locked(state_dir: Path, message: str) -> Result:
    """Merge with the remote, commit on top of it and push; retry if someone pushed first.
    The caller holds the state lock."""
    last_error = ""
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        result = pull_locked(state_dir)
        result.attempts = attempt
        git(state_dir, "add", "-A")
        if git(state_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
            result.note = "nothing to push"
            return result
        base = git(state_dir, "rev-parse", "HEAD").stdout.decode().strip()
        git(state_dir, "commit", "--quiet", "-m", message)
        done = git(state_dir, "push", "--quiet", REMOTE, f"HEAD:refs/heads/{BRANCH}", check=False)
        if done.returncode == 0:
            result.pushed = True
            result.commit = git(state_dir, "rev-parse", "--short", "HEAD").stdout.decode().strip()
            return result
        last_error = done.stderr.decode(errors="replace").strip()[:300]
        # Someone pushed first. Unwind our commit (the files keep our changes), so the next pull
        # compares against the last remote commit and merges our changes as local ones.
        git(state_dir, "reset", "--quiet", "--mixed", base)
    raise SyncError(f"push rejected {PUSH_ATTEMPTS} times: {last_error}")


def push(state_dir: Path, message: str, holder: str = "a sync") -> Result:
    if not is_worktree(state_dir):
        raise SyncError(f"{state_dir} is not linked to the data branch yet: run `jobhunt sync --init`")
    with state_lock(state_dir, holder=holder):
        return push_locked(state_dir, message)


def init(repo_root: Path, state_dir: Path) -> Result:
    """Turn a plain state folder into a worktree of the data branch, merging what is already in
    the folder with the branch. The old folder is kept beside it until you delete it."""
    if is_worktree(state_dir):
        return Result(note="already linked to the data branch")
    git(repo_root, "fetch", "--quiet", REMOTE, BRANCH)
    for block in git(repo_root, "worktree", "list", "--porcelain").stdout.decode().split("\n\n"):
        if f"branch refs/heads/{BRANCH}" in block:
            path = block.splitlines()[0].removeprefix("worktree ")
            raise SyncError(f"the data branch is already checked out at {path}; remove that checkout first "
                            f"(git worktree remove {path})")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    fresh = state_dir.with_name(f"{state_dir.name}.sync-{stamp}")
    git(repo_root, "worktree", "add", "--quiet", "-B", BRANCH, str(fresh), f"{REMOTE}/{BRANCH}")
    if state_dir.exists():
        merge_into(fresh, _local_files(state_dir))
        # Local-only folders (the index cache) move across untouched.
        if (state_dir / "harvest" / "index").exists():
            (fresh / "harvest").mkdir(exist_ok=True)
            (state_dir / "harvest" / "index").rename(fresh / "harvest" / "index")
        state_dir.rename(state_dir.with_name(f"{state_dir.name}.before-sync-{stamp}"))
    git(repo_root, "worktree", "move", str(fresh), str(state_dir))
    (state_dir / ".gitignore").write_text(GITIGNORE)
    result = push(state_dir, "Link the local state folder to the data branch")
    result.note = "linked" + (f"; the old folder is kept as {state_dir.name}.before-sync-{stamp}" if
                              (state_dir.with_name(f"{state_dir.name}.before-sync-{stamp}")).exists() else "")
    return result
