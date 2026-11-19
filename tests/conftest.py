from __future__ import annotations

from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from jobhunt import config as cfg
from jobhunt import store
from jobhunt.web.app import create_app

EXAMPLE_CONFIG = Path(__file__).parent / "example_config"


@pytest.fixture(autouse=True)
def no_alert_topic(monkeypatch):
    """Tests never send phone alerts, even where NTFY_TOPIC is set."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


@pytest.fixture(autouse=True)
def no_host_waits():
    """Hosts that asked to wait are remembered per process: every test starts with none."""
    from jobhunt.sources.http import WAITS
    WAITS.load([])
    yield
    WAITS.load([])


@pytest.fixture(autouse=True)
def no_web_search(monkeypatch):
    """The wishlist's careers-site search never calls claude in tests; a test sets its own answer."""
    from jobhunt import discover
    monkeypatch.setattr(discover, "find_careers_site",
                        lambda name, settings, runner=None, timeout=0: discover.Found(note="no search in tests"))


@pytest.fixture(autouse=True)
def token_account(monkeypatch):
    """Runs in tests use a token (the token's account, like GitHub's runs): `claude auth status` is
    never asked, and no account is remembered from another test."""
    from jobhunt import accounts, pipeline
    monkeypatch.setenv(accounts.TOKEN_ENV, "test-token")
    monkeypatch.setattr(accounts, "claude_status", lambda: None)
    accounts._cache.clear()
    monkeypatch.setattr(pipeline, "ACCOUNT", None)


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch):
    """Retries of failed Claude calls do not wait in tests."""
    from jobhunt import scoring
    monkeypatch.setattr(scoring, "_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests use saved responses (tests/fakes.py); a real request is a bug in the test or the code."""
    def refuse(self, method, url, *args, **kwargs):
        raise AssertionError(f"test tried to reach the network: {method} {url}")
    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture
def paths(tmp_path: Path) -> cfg.Paths:
    """Example config with a throwaway database. Tests never read the real config/."""
    return cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "test.db",
                     backups=tmp_path / "backups")


@pytest.fixture
def conn(paths):
    c = store.connect(paths.database)
    yield c
    c.close()


@pytest.fixture
def client(paths):
    with TestClient(create_app(paths)) as c:
        yield c


@pytest.fixture(autouse=True)
def no_company_search(monkeypatch):
    """"Find more companies" never calls claude in tests; a test sets its own answer."""
    from jobhunt import company_search, scoring
    monkeypatch.setattr(company_search, "web_search",
                        lambda model="sonnet", timeout=0: lambda group, text: scoring.CallResult(error="no search in tests"))
