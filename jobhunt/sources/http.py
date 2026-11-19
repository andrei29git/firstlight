"""Polite HTTP: an honest User-Agent, a delay between requests to the same host, retries with
backoff on 429 and 5xx, and a robots.txt check for custom career sites."""

from __future__ import annotations

import copy
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .base import SourceError
from .robots import Robots

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60.0

# Public job board APIs, built to be consumed by job sites: these get the shorter
# `public_api_delay` (429s still back off). A leading dot matches any subdomain.
PUBLIC_API_HOSTS = ("boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com",
                    ".recruitee.com", "api.smartrecruiters.com", ".jobs.personio.de", ".jobs.personio.com",
                    ".teamtailor.com", "feed.homerun.co")


def is_public_api(host: str) -> bool:
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUBLIC_API_HOSTS)


def rate_key(host: str) -> str:
    """The unit a board rate-limits by. Recruitee limits across all its company subdomains, so
    every *.recruitee.com host shares one delay (measured 2026-10-25: parallel subdomains got 429);
    Personio and Teamtailor subdomains share one too, to be safe. The bare domain ("recruitee.com")
    names the same unit."""
    return next((h for h in PUBLIC_API_HOSTS if h.startswith(".") and (host.endswith(h) or host == h[1:])), host)


class HostWaiting(SourceError):
    """The host asked not to be contacted for longer than a retry is worth (a long Retry-After)."""


class HostWaits:
    """Hosts that answered 429 or 503 with a Retry-After longer than MAX_RETRY_AFTER: none of them
    is contacted again until that time (2026-10-27: Workable asked for 23 hours, and retrying
    every minute ignored it). One record per process, shared by every Http in it; runs and
    harvests load the stored waits at the start and store them at the end (hostwaits.py)."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.clock = clock
        self._lock = threading.Lock()
        self._waits: dict[str, dict[str, Any]] = {}
        self.new: set[str] = set()  # hosts that asked during this process

    def load(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            self._waits = {r["host"]: dict(r) for r in records if r.get("host")}
            self.new = set()

    def until(self, key: str) -> datetime | None:
        record = self._waits.get(key)
        if not record:
            return None
        until = datetime.fromisoformat(record["until"])
        return until if until > self.clock() else None

    def record(self, key: str, seconds: float, slow: bool) -> datetime:
        now = self.clock()
        until = now + timedelta(seconds=seconds)
        with self._lock:
            earlier = self._waits.get(key, {})
            self._waits[key] = {**earlier, "host": key, "until": until.isoformat(timespec="seconds"),
                                "asked_at": now.isoformat(timespec="seconds"), "retry_after": int(seconds),
                                "slow": bool(slow) or bool(earlier.get("slow")),
                                "refusals": int(earlier.get("refusals", 0)) + 1,
                                "updated_at": now.isoformat(timespec="seconds")}
            self.new.add(key)
        return until

    def update(self, key: str, **fields: Any) -> None:
        with self._lock:
            if key in self._waits:
                self._waits[key].update(fields)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for _, r in sorted(self._waits.items())]


WAITS = HostWaits()


def when(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%-d %b %H:%M UTC")


class Http:
    def __init__(self, user_agent: str, per_host_delay: float = 1.0, max_retries: int = 4,
                 timeout: float = 30.0, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 public_api_delay: float | None = None, host_delays: dict[str, float] | None = None,
                 waits: HostWaits | None = None):
        self.user_agent = user_agent
        self.per_host_delay = per_host_delay
        self.public_api_delay = per_host_delay if public_api_delay is None else public_api_delay
        # A longer delay for hosts that limit harder (`[fetching] host_delays`, by rate key).
        self.host_delays = {rate_key(h): float(d) for h, d in (host_delays or {}).items()}
        self.waits = waits if waits is not None else WAITS
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.sleep = sleep
        self.clock = clock
        self._last_request: dict[str, float] = {}
        self._crawl_delay: dict[str, float] = {}  # from robots.txt (custom sites): never faster than asked
        self._robots: dict[str, Robots | None] = {}

    @classmethod
    def from_settings(cls, settings: dict[str, Any], **kw) -> "Http":
        fetching = settings.get("fetching", {})
        return cls(user_agent=fetching.get("user_agent", "jobhunt"),
                   per_host_delay=float(fetching.get("per_host_delay", 1.0)),
                   public_api_delay=float(fetching.get("public_api_delay", fetching.get("per_host_delay", 1.0))),
                   host_delays=fetching.get("host_delays", {}),
                   max_retries=int(fetching.get("max_retries", 4)), **kw)

    def without_retries(self) -> "Http":
        """The same client (session, per-host timing) failing fast: for probing guesses."""
        return self.with_retries(0)

    def with_retries(self, max_retries: int) -> "Http":
        clone = copy.copy(self)
        clone.max_retries = max_retries
        return clone

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            delay = self.delay_for(host) or (self.public_api_delay if is_public_api(host) else self.per_host_delay)
            delay = max(delay, self._crawl_delay.get(host, 0.0))
            remaining = delay - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request[host] = self.clock()

    def delay_for(self, host: str) -> float | None:
        """`[fetching] host_delays` for this host: by name, or by a key starting with "." for every
        host under it (".myworkdayjobs.com": each Workday tenant, 2026-11-18)."""
        if host in self.host_delays:
            return self.host_delays[host]
        return next((d for h, d in self.host_delays.items() if h.startswith(".") and host.endswith(h)), None)

    def request(self, method: str, url: str, **kw) -> requests.Response:
        host = rate_key(urlsplit(url).netloc)
        waiting = self.waits.until(host)
        if waiting:
            raise HostWaiting(f"{host} asked not to be contacted until {when(waiting)}")
        kw.setdefault("timeout", self.timeout)
        problem = ""
        retry_response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.sleep(self._backoff(attempt, retry_response))
            self._wait_for_host(host)
            retry_response = None
            try:
                response = self.session.request(method, url, **kw)
            except requests.RequestException as e:
                problem = type(e).__name__
                continue
            if response.status_code in (403, 429) and bot_check(response):
                # A "security check" page (Cloudflare in front of some Workday tenants, 2026-11-18):
                # retrying only deepens the block, and it is never worked around. The next run tries again.
                raise SourceError(f"HTTP {response.status_code} from {url}: a bot check (security check page), not retried")
            if response.status_code in RETRY_STATUSES:
                asked = _retry_after(response)
                if asked is not None and asked > MAX_RETRY_AFTER:
                    until = self.waits.record(host, asked, slow=host in self.host_delays)
                    raise HostWaiting(f"{host} asked to wait {asked / 3600:.1f} hours (HTTP {response.status_code}, "
                                      f"Retry-After), so it is left alone until {when(until)}")
                problem = f"HTTP {response.status_code}"
                retry_response = response
                continue
            if response.status_code >= 400:
                raise SourceError(f"HTTP {response.status_code} from {url}")
            return response
        raise SourceError(f"{problem} from {url} after {self.max_retries + 1} attempts")

    @staticmethod
    def _backoff(attempt: int, response: requests.Response | None) -> float:
        asked = _retry_after(response) if response is not None else None
        if asked is not None:
            return min(asked, MAX_RETRY_AFTER)
        return min(2.0 ** attempt, MAX_RETRY_AFTER)

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw) -> Any:
        return _json(self.get(url, **kw), url)

    def post_json(self, url: str, payload: Any, **kw) -> Any:
        return _json(self.request("POST", url, json=payload, **kw), url)

    def allowed_by_robots(self, url: str) -> bool:
        """robots.txt check, for custom career sites only (public ATS APIs are meant to be consumed).
        A missing or unreadable robots.txt allows everything."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rules: Robots | None = None
            try:
                rules = Robots.from_text(self.get(origin + "/robots.txt").text)
            except SourceError:
                rules = None
            self._robots[origin] = rules
            asked = rules.crawl_delay(self.user_agent) if rules is not None else None
            if asked:
                self._crawl_delay[rate_key(parts.netloc)] = float(asked)
        rules = self._robots[origin]
        return rules is None or rules.allowed(self.user_agent, url)


BOT_CHECK = re.compile(r"enable javascript and cookies to continue|security check|attention required|just a moment\.\.\.",
                       re.IGNORECASE)


def bot_check(response: requests.Response) -> bool:
    """An HTML challenge page rather than the site's answer."""
    if "html" not in response.headers.get("Content-Type", "").lower():
        return False
    return bool(BOT_CHECK.search(response.text[:5000]))


def _retry_after(response: requests.Response) -> float | None:
    """Retry-After in seconds (a number of seconds or an HTTP date), else None."""
    value = (response.headers.get("Retry-After") or "").strip()
    if value.isdigit():
        return float(value)
    if value:
        from email.utils import parsedate_to_datetime
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None
    return None


def _json(response: requests.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as e:
        raise SourceError(f"invalid JSON from {url}") from e
