"""Which Claude account a run used (2026-11-18), so the weekly figures, the guards and the Runs
page keep accounts apart.

GitHub's runs and the laptop's launchd job use a long-lived token (`CLAUDE_CODE_OAUTH_TOKEN`), for
which Claude reports no account: the token's account is named in `[accounts] token`. A laptop run
without a token uses whoever is logged in to the `claude` CLI there, which `claude auth status` reports
by organisation; `[accounts.orgs]` names organisations. Only the organisation id, the plan and the
name are kept, never the email, and a token only by a short fingerprint.

Scores and runs recorded before accounts were recorded count as the token's account: until
2026-11-18 every run used the same account."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from typing import Any, Callable

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CACHE_SECONDS = 600  # a login changed while the app runs is noticed within this

StatusReader = Callable[[], dict[str, Any] | None]


def claude_status() -> dict[str, Any] | None:
    """`claude auth status --json`, or None when it cannot be read (it makes no model call)."""
    try:
        done = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=20)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def default_key(settings: dict[str, Any]) -> str:
    """The token's account: GitHub's runs, the launchd job, and everything recorded before 2026-11-18."""
    return str(settings.get("accounts", {}).get("token") or "token")


def detect(settings: dict[str, Any], env: dict[str, str] | None = None,
           status: StatusReader = lambda: claude_status()) -> dict[str, Any]:
    env = os.environ if env is None else env
    names = {str(k): str(v) for k, v in (settings.get("accounts", {}).get("orgs") or {}).items()}
    token = env.get(TOKEN_ENV)
    if token:
        return {"key": default_key(settings), "how": "token",
                "fingerprint": hashlib.sha256(token.encode()).hexdigest()[:10]}
    if env.get("ANTHROPIC_API_KEY"):
        return {"key": "api key", "how": "api key"}
    found = status() or {}
    org = found.get("orgId")
    if not found.get("loggedIn") or not org:
        return {"key": "unknown", "how": "unknown"}
    return {"key": names.get(org) or f"account {org[:8]}", "how": "login", "org": org,
            "plan": found.get("subscriptionType")}


_cache: dict[str, Any] = {}


def current(settings: dict[str, Any]) -> dict[str, Any]:
    """This process's account, asked again at most every CACHE_SECONDS (the token cannot change
    within a process; a login can, while the app runs)."""
    now = time.monotonic()
    if _cache.get("at") is None or now - _cache["at"] > CACHE_SECONDS or os.environ.get(TOKEN_ENV) != _cache.get("token"):
        _cache.update(at=now, token=os.environ.get(TOKEN_ENV), account=detect(settings))
    return _cache["account"]


def key_of(record: dict[str, Any] | None, settings: dict[str, Any]) -> str:
    """The account a score or run summary was made with (older ones: the token's account)."""
    account = (record or {}).get("account")
    if isinstance(account, dict):
        return str(account.get("key") or default_key(settings))
    return str(account or default_key(settings))

