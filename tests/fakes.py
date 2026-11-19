"""A fake requests session, so adapters run end to end against saved responses without the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from jobhunt.sources import Http

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def response(url: str, status: int = 200, body: Any = "", headers: dict[str, str] | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.headers.update(headers or {})
    text = body if isinstance(body, str) else json.dumps(body)
    r._content = text.encode("utf-8")
    r.encoding = "utf-8"
    return r


class FakeSession:
    """Routes map a URL to a body, a status, or a tuple of either, served in turn (the last repeats).
    Unknown URLs get a 404, so robots.txt is 'missing' (allow all) unless routed."""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = dict(routes or {})
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kw) -> requests.Response:
        self.calls.append((method, url, kw.get("json")))
        route = self.routes.get(url)
        if isinstance(route, tuple):
            route = route[0]
            if len(self.routes[url]) > 1:
                self.routes[url] = self.routes[url][1:]
        if route is None:
            return response(url, 404)
        if isinstance(route, int):
            return response(url, route)
        if isinstance(route, requests.Response):
            return route
        return response(url, 200, route)

    def urls(self, method: str | None = None) -> list[str]:
        return [u for m, u, _ in self.calls if method is None or m == method]


def fake_http(routes: dict[str, Any] | None = None, **kw) -> tuple[Http, FakeSession]:
    session = FakeSession(routes)
    sleeps: list[float] = []
    http = Http("test-agent", per_host_delay=0, max_retries=kw.pop("max_retries", 2),
                session=session, sleep=sleeps.append, **kw)  # type: ignore[arg-type]
    http.sleeps = sleeps  # type: ignore[attr-defined]
    return http, session


# --- A fake `claude -p` ------------------------------------------------------------------------

GOOD_SCORE = {"fit": 8, "blockers": [], "odds": "medium", "action": "apply_now",
              "explanation": "Backend graduate role.", "angle": "Lead with the refunds service.",
              "gaps": [], "start_date": None, "deadline": None}


def claude_stream(output: dict | None = GOOD_SCORE, *, limit_status: str = "allowed", is_error: bool = False,
                  cost: float = 0.02, limit_type: str = "five_hour", utilization: float | None = None) -> str:
    events = [{"type": "system", "subtype": "init"},
              {"type": "rate_limit_event", "rate_limit_info": {"status": limit_status, "rateLimitType": limit_type,
                                                               **({"utilization": utilization} if utilization else {}),
                                                               "resetsAt": 1900000000}}]
    result = {"type": "result", "subtype": "success", "is_error": is_error, "result": "error text" if is_error else "",
              "total_cost_usd": cost, "duration_ms": 1200,
              "usage": {"input_tokens": 10, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0,
                        "output_tokens": 200}}
    if output is not None:
        result["structured_output"] = output
    events.append(result)
    return "\n".join(json.dumps(e) for e in events) + "\n"


class FakeClaude:
    """Records every call (argv, stdin, cwd) and answers with `respond(message) -> stdout`."""

    def __init__(self, respond=None, respond_argv=None):
        self.respond = respond or (lambda message: claude_stream())
        self.respond_argv = respond_argv  # (message, argv) -> stdout, to answer by model
        self.calls: list[dict] = []

    def __call__(self, argv, stdin, cwd, timeout, env):
        import os
        self.calls.append({"argv": argv, "stdin": stdin, "cwd": cwd, "cwd_files": os.listdir(cwd), "env": env})
        return 0, (self.respond_argv(stdin, argv) if self.respond_argv else self.respond(stdin)), ""
