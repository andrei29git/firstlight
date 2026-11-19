"""Leak check: candidate facts must never appear outside the config folder.

Patterns come from the config itself (leak_patterns.txt, the candidate's full name and surname,
and every value in the untracked personal.toml), so this file holds no personal strings.
An example config (candidate.example = true) has nothing to protect, so only its patterns file is used.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg

# The config folder is never scanned. Other private paths (notes kept with the code) can be listed,
# one per line, in the config's leak_ignore.txt: a line ending in "/" names a folder.
IGNORE_FILE = "leak_ignore.txt"


@dataclass(frozen=True)
class Leak:
    path: str
    line: int
    pattern: str


# Every value in the untracked personal.toml (the cover letter's name and the profile links included).
PERSONAL_KEYS = ("name", "email", "phone", "linkedin", "github", "website", "cv")


def patterns(config: Path) -> list[str]:
    found = list(cfg.load_leak_patterns(config))
    candidate = cfg.load_candidate(config).get("candidate", {})
    if not candidate.get("example", False):
        found += [re.escape(candidate[k]) for k in ("full_name", "surname") if candidate.get(k)]
        personal = cfg.load_personal(config)
        found += [re.escape(str(personal[k]).strip()) for k in PERSONAL_KEYS if str(personal.get(k) or "").strip()]
    return found


def tracked_files(root: Path) -> list[str]:
    """Files git would commit: tracked, plus untracked ones that are not ignored."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=root, capture_output=True, check=True).stdout
    return sorted({p for p in out.decode().split("\0") if p})


def ignored(config: Path) -> tuple[str, ...]:
    path = config / IGNORE_FILE
    lines = path.read_text().splitlines() if path.exists() else []
    return tuple(line.strip() for line in lines if line.strip() and not line.strip().startswith("#"))


def _is_scanned(path: str, skip: tuple[str, ...] = ()) -> bool:
    if path.startswith("config/"):
        return False
    return not any(path.startswith(s) if s.endswith("/") else path == s for s in skip)


def scan(root: Path, config: Path) -> list[Leak]:
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in patterns(config)]
    skip = ignored(config)
    leaks = []
    for rel in (r for r in tracked_files(root) if _is_scanned(r, skip)):
        path = root / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        for number, text in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            leaks += [Leak(rel, number, raw) for raw, rx in compiled if rx.search(text)]
    return leaks
