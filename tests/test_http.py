from __future__ import annotations

import pytest
import requests

from jobhunt.sources import Http, SourceError

from .fakes import FakeSession, fake_http, response

URL = "https://api.example.com/jobs"


def test_retries_429_and_5xx_with_backoff():
    http, session = fake_http({URL: (429, 503, {"ok": True})})
    assert http.get_json(URL) == {"ok": True}
    assert len(session.calls) == 3
    assert http.sleeps == [2.0, 4.0]


def test_honours_retry_after():
    http, _ = fake_http({URL: (response(URL, 429, headers={"Retry-After": "7"}), "{}")})
    http.get_json(URL)
    assert http.sleeps == [7.0]


def test_gives_up_after_max_retries():
    http, session = fake_http({URL: 500}, max_retries=2)
    with pytest.raises(SourceError, match="HTTP 500"):
        http.get(URL)
    assert len(session.calls) == 3


def test_does_not_retry_client_errors():
    http, session = fake_http({URL: 404})
    with pytest.raises(SourceError, match="HTTP 404"):
        http.get(URL)
    assert len(session.calls) == 1


def test_retries_connection_errors():
    class Flaky(FakeSession):
        def request(self, method, url, **kw):
            if not self.calls:
                self.calls.append((method, url, None))
                raise requests.ConnectionError("reset")
            return super().request(method, url, **kw)

    session = Flaky({URL: "{}"})
    http = Http("test-agent", per_host_delay=0, session=session, sleep=lambda s: None)  # type: ignore[arg-type]
    assert http.get_json(URL) == {}


def test_waits_between_requests_to_the_same_host():
    now = [100.0]
    sleeps = []
    session = FakeSession({URL: "{}", "https://other.example.com/": "{}"})
    http = Http("test-agent", per_host_delay=1.0, session=session,  # type: ignore[arg-type]
                sleep=sleeps.append, clock=lambda: now[0])
    http.get(URL)
    now[0] += 0.25
    http.get("https://other.example.com/")  # another host: no wait
    http.get(URL)
    assert sleeps == [0.75]


def test_invalid_json_is_a_source_error():
    http, _ = fake_http({URL: "<html>not json</html>"})
    with pytest.raises(SourceError, match="invalid JSON"):
        http.get_json(URL)


def test_sends_the_user_agent():
    http, session = fake_http()
    assert session.headers["User-Agent"] == "test-agent"


def test_robots():
    http, session = fake_http({"https://x.example/robots.txt": "User-agent: *\nDisallow: /private/\n"})
    assert http.allowed_by_robots("https://x.example/jobs/1")
    assert not http.allowed_by_robots("https://x.example/private/1")
    assert session.urls().count("https://x.example/robots.txt") == 1  # cached per origin


def test_missing_robots_allows_everything():
    http, _ = fake_http()
    assert http.allowed_by_robots("https://x.example/anything")


def test_public_apis_get_the_shorter_delay():
    now = [100.0]
    sleeps = []
    gh = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    rc = "https://acme.recruitee.com/api/offers/"
    session = FakeSession({gh: "{}", rc: "{}", URL: "{}"})
    http = Http("test-agent", per_host_delay=1.0, public_api_delay=0.25, session=session,  # type: ignore[arg-type]
                sleep=sleeps.append, clock=lambda: now[0])
    for url in (gh, gh, rc, rc, URL, URL):
        http.get(url)
    assert sleeps == [0.25, 0.25, 1.0]


def test_public_api_hosts():
    from jobhunt.sources.http import is_public_api
    assert is_public_api("api.ashbyhq.com") and is_public_api("bunq.recruitee.com")
    assert not is_public_api("lseg.wd3.myworkdayjobs.com") and not is_public_api("recruitee.com.evil.example")


def test_recruitee_subdomains_share_one_delay():
    now = [100.0]
    sleeps = []
    a, b = "https://acme.recruitee.com/api/offers/", "https://other.recruitee.com/api/offers/"
    session = FakeSession({a: "{}", b: "{}"})
    http = Http("test-agent", per_host_delay=1.0, public_api_delay=0.25, session=session,  # type: ignore[arg-type]
                sleep=sleeps.append, clock=lambda: now[0])
    http.get(a)
    http.get(b)  # a different subdomain, same rate limit
    assert sleeps == [0.25]


def test_robots_follow_the_standard_longest_match_wins():
    from jobhunt.sources.robots import Robots
    microsoft = Robots.from_text("User-agent: *\nDisallow: /\nAllow: /$\nAllow: /careers\nAllow: /api/pcsx\n")
    assert microsoft.allowed("jobhunt/1.0", "https://x.example/api/pcsx/search?domain=microsoft.com")
    assert microsoft.allowed("jobhunt/1.0", "https://x.example/") and microsoft.allowed("jobhunt/1.0", "https://x.example/careers/job/1")
    assert not microsoft.allowed("jobhunt/1.0", "https://x.example/api/other")
    avature = Robots.from_text("User-agent: *\nAllow: /careers\nDisallow: /careers/*qtvc=\n")
    assert avature.allowed("jobhunt/1.0", "https://x.example/careers/SearchJobs?jobOffset=10")
    assert not avature.allowed("jobhunt/1.0", "https://x.example/careers/SearchJobs?qtvc=abc")
    named = Robots.from_text("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /bolt/\nCrawl-delay: 5\n")
    assert named.allowed("jobhunt/1.0 (personal job search)", "https://x.example/jobs/1")  # GPTBot's group is not ours
    assert not named.allowed("jobhunt/1.0", "https://x.example/bolt/x") and named.crawl_delay("jobhunt") == 5.0
    mine = Robots.from_text("User-agent: jobhunt\nDisallow: /jobs/\n\nUser-agent: *\nAllow: /\n")
    assert not mine.allowed("jobhunt/1.0 (personal job search)", "https://x.example/jobs/1")
    assert Robots.from_text("User-agent: *\nDisallow: /x\nAllow: /x\n").allowed("jobhunt", "https://x.example/x")  # a tie: allow


def test_a_bot_check_page_is_not_retried():
    # 2026-11-18: Blackstone's and Palo Alto's Workday answered "Security Check: Enable JavaScript and
    # cookies to continue" with a 429; retrying only deepened the block.
    page = response(URL, 429, headers={"Content-Type": "text/html"},
                    body="<html><body>Security Check Enable JavaScript and cookies to continue</body></html>")
    http, session = fake_http({URL: page})
    with pytest.raises(SourceError, match="bot check"):
        http.get(URL)
    assert len(session.calls) == 1 and http.sleeps == []


def test_a_host_delay_can_cover_every_host_under_a_domain():
    http = Http("agent", per_host_delay=1.0, host_delays={".myworkdayjobs.com": 2.0, "apply.workable.com": 5.0})
    assert http.delay_for("lseg.wd3.myworkdayjobs.com") == 2.0 and http.delay_for("apply.workable.com") == 5.0
    assert http.delay_for("boards-api.greenhouse.io") is None
