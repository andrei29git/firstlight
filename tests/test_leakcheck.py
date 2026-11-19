from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jobhunt import config as cfg
from jobhunt import leakcheck

from .conftest import EXAMPLE_CONFIG

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repository with a non-example config for a fictional person."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    config = tmp_path / "config"
    shutil.copytree(EXAMPLE_CONFIG, config)
    candidate = (config / "candidate.toml").read_text()
    candidate = candidate.replace("example = true", "example = false")
    candidate = candidate.replace('full_name = "Andrei Example"', 'full_name = "Zed Quillfeather"')
    candidate = candidate.replace('surname = "Example"', 'surname = "Quillfeather"')
    (config / "candidate.toml").write_text(candidate)
    (config / "leak_patterns.txt").write_text("# private\nSecretCorp\n\\b4\\.21\\b\n")
    (config / "personal.toml").write_text('name = ""\nemail = "zed@example.org"\nphone = "+00 1234"\n'
                                          'linkedin = "https://www.linkedin.com/in/zed-q"\ngithub = ""\n')
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    return tmp_path


def leaks(repo: Path) -> set[tuple[str, str]]:
    return {(leak.path, leak.pattern) for leak in leakcheck.scan(repo, repo / "config")}


def test_clean_repository_passes(repo):
    (repo / "app.py").write_text("print('hello')\n")
    assert leaks(repo) == set()


@pytest.mark.parametrize("text, pattern", [
    ("works at SecretCorp", "SecretCorp"),
    ("grade 4.21 overall", r"\b4\.21\b"),
    ("Author: Zed Quillfeather", "Zed\\ Quillfeather"),
    ("QUILLFEATHER", "Quillfeather"),
    ("mail zed@example.org", r"zed@example\.org"),
    ("see https://www.linkedin.com/in/zed-q", re.escape("https://www.linkedin.com/in/zed-q")),
])
def test_leaks_are_found(repo, text, pattern):
    (repo / "app.py").write_text(f"# {text}\n")
    assert ("app.py", pattern) in leaks(repo)


def test_first_name_alone_is_not_a_leak(repo):
    (repo / "app.py").write_text("# Zed wrote this\n")
    assert leaks(repo) == set()


def test_config_listed_private_files_and_ignored_files_are_not_scanned(repo):
    (repo / "config" / "notes.md").write_text("SecretCorp\n")
    (repo / "config" / "leak_ignore.txt").write_text("# private notes\nNOTES.md\ndocs/private/\n")
    (repo / "NOTES.md").write_text("SecretCorp\n")
    (repo / "docs" / "private").mkdir(parents=True)
    (repo / "docs" / "private" / "runs.md").write_text("SecretCorp\n")
    (repo / "ignored.txt").write_text("SecretCorp\n")
    assert leaks(repo) == set()
    (repo / "docs" / "notes.md").write_text("SecretCorp\n")  # the rest of docs/ is scanned
    assert {path for path, _ in leaks(repo)} == {"docs/notes.md"}


def test_example_config_only_uses_its_patterns_file():
    candidate = cfg.load_candidate(EXAMPLE_CONFIG)["candidate"]
    assert candidate["example"] is True
    assert leakcheck.patterns(EXAMPLE_CONFIG) == []


def test_repository_has_no_leaks():
    """The one test that reads the real config: it needs the real patterns to protect them."""
    if not (REPO_ROOT / "config" / "candidate.toml").exists():
        pytest.skip("no config folder")
    found = leakcheck.scan(REPO_ROOT, REPO_ROOT / "config")
    assert not found, "\n".join(f"{l.path}:{l.line} matches {l.pattern!r}" for l in found)
