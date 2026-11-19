"""Locating and loading the config folder.

The config folder holds everything about the candidate. Code never hardcodes candidate facts;
it reads them from here. Tests point JOBHUNT_CONFIG at tests/example_config instead.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Paths:
    root: Path
    config: Path
    database: Path
    backups: Path
    state: Path | None = None  # the pipeline's state folder (the data branch's contents)

    @property
    def state_dir(self) -> Path:
        return self.state or self.root / ".data"

    @classmethod
    def from_env(cls) -> "Paths":
        """Resolve paths from the environment, defaulting to the current directory as the project root."""
        root = Path(os.environ.get("JOBHUNT_ROOT", Path.cwd())).resolve()
        config = Path(os.environ.get("JOBHUNT_CONFIG", root / "config")).resolve()
        tracker = load_settings(config).get("tracker", {})
        database = Path(os.environ.get("JOBHUNT_DB", root / tracker.get("database", "jobhunt.db"))).resolve()
        backups = (root / tracker.get("backups", "backups")).resolve()
        state = Path(os.environ.get("JOBHUNT_STATE", root / tracker.get("state", ".data"))).resolve()
        return cls(root=root, config=config, database=database, backups=backups, state=state)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(config: Path) -> dict[str, Any]:
    return _load_toml(config / "settings.toml")


def load_candidate(config: Path) -> dict[str, Any]:
    return _load_toml(config / "candidate.toml")


def load_applications_seed(config: Path) -> list[dict[str, Any]]:
    path = config / "applications_seed.toml"
    if not path.exists():
        return []
    return _load_toml(path).get("application", [])


def load_personal(config: Path) -> dict[str, Any]:
    """Untracked contact details. Missing on CI and on fresh checkouts, which is fine."""
    path = config / "personal.toml"
    return _load_toml(path) if path.exists() else {}


def load_leak_patterns(config: Path) -> list[str]:
    path = config / "leak_patterns.txt"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
