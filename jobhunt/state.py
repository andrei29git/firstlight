"""Pipeline state in a folder of JSON Lines files: the `data` branch's contents (section 5.4).

Lines are sorted by key so commits diff cleanly. Jobs are split so that files stay small and a
scheduled run rewrites few of them: configured companies' jobs go to `jobs/config/<source>.jsonl`,
generated-list jobs to `jobs/generated/<slot>.jsonl`, by the same daily slot that decides which run
fetches the company. Older layouts (`jobs.jsonl`, `jobs/<source>.jsonl`) are read and replaced on
the next save. A file whose content did not change is not rewritten.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SLOTS_PER_DAY = 12  # one per scheduled run (every 2 hours)


def company_slot(board: str, company: str, slots: int = SLOTS_PER_DAY) -> int:
    """A generated company's daily slot: which of the day's runs fetches it, and which file holds its jobs."""
    return int(hashlib.sha1(f"{board}:{company}".encode()).hexdigest(), 16) % slots


def shard(record: dict[str, Any]) -> str:
    """The file a job record lives in, relative to the state folder."""
    if record.get("origin") == "generated":
        return f"jobs/generated/{company_slot(record['source'], record['company']):02d}.jsonl"
    return f"jobs/config/{record['source']}.jsonl"


def dumps(records: list[dict[str, Any]]) -> bytes:
    return "".join(dump_line(r) + "\n" for r in records).encode("utf-8")


def write_if_changed(path: Path, data: bytes) -> bool:
    """Write atomically, and only when the content differs (unchanged files keep their timestamps)."""
    if path.exists() and path.stat().st_size == len(data) and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


# JSON allows these unescaped, but Python's str.splitlines() (and some editors) break lines at
# them: one posting with "\x85" in it split a record in three and stopped every run (2026-10-28).
# Records are written with them escaped, and files are read line by line at "\n" only.
_SEPARATORS = (("\x85", "\\u0085"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029"))


def dump_line(record: dict[str, Any]) -> str:
    """One record as one JSON line (sorted keys, so commits diff cleanly), without a newline."""
    text = json.dumps(record, sort_keys=True, ensure_ascii=False)
    for raw, escaped in _SEPARATORS:
        text = text.replace(raw, escaped)
    return text


def parse_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.split("\n") if line.strip()]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return parse_lines(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(dump_line(r) + "\n" for r in records),
                   encoding="utf-8")
    tmp.replace(path)


class State:
    def __init__(self, root: Path, create: bool = True):
        self.root = root
        if create:
            root.mkdir(parents=True, exist_ok=True)
            (root / "postings").mkdir(exist_ok=True)
        self.jobs: dict[str, dict[str, Any]] = {}
        for path in [root / "jobs.jsonl", *sorted((root / "jobs").rglob("*.jsonl"))]:
            for record in read_jsonl(path):
                old = self.jobs.get(record["key"])  # a job in two files (mid-migration): the newer wins
                if old is None or str(record.get("updated_at") or "") >= str(old.get("updated_at") or ""):
                    self.jobs[record["key"]] = record
        # One line per job and model; lines written before ids existed get one from key and model.
        self.scores: dict[str, dict[str, Any]] = {
            r.get("id") or f"{r['key']}@{r['model']}": r for r in read_jsonl(root / "scores.jsonl")}

    @classmethod
    def read_only(cls, root: Path) -> "State":
        """Load without creating anything (the catalogue only reads the state)."""
        return cls(root, create=False)

    def save(self) -> None:
        shards: dict[str, list[dict[str, Any]]] = {}
        for key in sorted(self.jobs):
            shards.setdefault(shard(self.jobs[key]), []).append(self.jobs[key])
        for rel, records in shards.items():
            write_if_changed(self.root / rel, dumps(records))
        for stale in (self.root / "jobs").rglob("*.jsonl"):
            if stale.relative_to(self.root).as_posix() not in shards:
                stale.unlink()
        (self.root / "jobs.jsonl").unlink(missing_ok=True)
        write_if_changed(self.root / "scores.jsonl", dumps([{"id": k, **self.scores[k]} for k in sorted(self.scores)]))

    def scores_for(self, key: str) -> dict[str, dict[str, Any]]:
        """This job's scores by model."""
        return {r["model"]: r for r in self.scores.values() if r["key"] == key}

    def append_run(self, summary: dict[str, Any]) -> None:
        with (self.root / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(dump_line(summary) + "\n")

    def runs(self) -> list[dict[str, Any]]:
        return read_jsonl(self.root / "runs.jsonl")

    def posting_path(self, digest: str) -> Path:
        return self.root / "postings" / f"{digest}.txt"

    def write_posting(self, digest: str, text: str) -> None:
        path = self.posting_path(digest)
        if not path.exists():  # written once: the name is the hash of the text
            path.write_text(text, encoding="utf-8")

    def read_posting(self, digest: str | None) -> str:
        if not digest:
            return ""
        path = self.posting_path(digest)
        return path.read_text(encoding="utf-8") if path.exists() else ""
