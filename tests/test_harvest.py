from __future__ import annotations

import json
from datetime import date

import pytest

from jobhunt import harvest
from jobhunt.companies import Company
from jobhunt.sources import greenhouse

from .fakes import fake_http, fixture_json

ALLOWED = {"NL", "GB", "FR", "DE", "US"}
CRAWL = "CC-MAIN-2026-43"
GH = harvest.BOARDS_BY_NAME["greenhouse"]


def index_url(query: str, extra: str) -> str:
    return f"{harvest.INDEX}/{CRAWL}-index?url={query}&output=json{extra}"


def index_routes(lines_by_query: dict[str, list[str]]) -> dict:
    routes = {}
    for query, urls in lines_by_query.items():
        routes[index_url(query, "&showNumPages=true")] = {"pages": 1, "pageSize": 5, "blocks": 1}
        routes[index_url(query, "&fl=url&page=0")] = "\n".join(json.dumps({"url": u}) for u in urls)
    return routes


@pytest.mark.parametrize("board, url, expected", [
    ("greenhouse", "https://job-boards.eu.greenhouse.io/IMC/jobs/1", "greenhouse:imc"),
    ("greenhouse", "https://boards.greenhouse.io/embed/job_board?for=stripe", "greenhouse:stripe"),
    ("greenhouse", "https://job-boards.greenhouse.io/", None),
    ("lever", "https://jobs.eu.lever.co/acme/123", "lever-eu:acme"),
    ("ashby", "https://jobs.ashbyhq.com/Mistral.ai/1", "ashby:Mistral.ai"),
    ("workday", "https://lseg.wd3.myworkdayjobs.com/en-US/Careers/job/X_1", "workday:https://lseg.wd3.myworkdayjobs.com/careers"),
    ("workday", "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/site/jobs", None),
    ("recruitee", "https://www.recruitee.com/pricing", None),
    ("teamtailor", "https://acme.teamtailor.com/jobs/1", "teamtailor:acme"),
    ("bamboohr", "https://acme.bamboohr.com/login", None),
])
def test_identify(board, url, expected):
    config = harvest.BOARDS_BY_NAME[board].identify(url)
    assert (harvest.identity(config) if config else None) == expected


def test_configured_companies_match_harvested_identities():
    configured = harvest.configured_identities([
        Company(name="LSEG", board="workday", url="https://lseg.wd3.myworkdayjobs.com/Careers"),
        Company(name="Stripe", board="greenhouse", slug="Stripe")])
    assert configured == {"workday:https://lseg.wd3.myworkdayjobs.com/careers", "greenhouse:stripe"}


def test_collect_reads_and_caches_index_pages(tmp_path):
    routes = index_routes({"job-boards.greenhouse.io/*": [
        "https://job-boards.greenhouse.io/flowtraders/jobs/1", "https://job-boards.greenhouse.io/flowtraders/jobs/2",
        "https://job-boards.greenhouse.io/acme/jobs/3"]})
    http, session = fake_http(routes)
    board = harvest.Board("greenhouse", ("job-boards.greenhouse.io/*",), GH.pattern, supported=True)
    cache = harvest.IndexCache(tmp_path)
    found, failed = harvest.collect(http, [CRAWL], [board], cache, log=lambda m: None)
    assert sorted(found) == ["greenhouse:acme", "greenhouse:flowtraders"] and failed == []
    calls = len(session.calls)
    harvest.collect(http, [CRAWL], [board], cache, log=lambda m: None)
    assert len(session.calls) == calls  # resumed from the cache: no new index requests


def test_failed_index_pages_are_reported_not_fatal(tmp_path):
    http, _ = fake_http({}, max_retries=0)
    board = harvest.Board("greenhouse", ("job-boards.greenhouse.io/*",), GH.pattern, supported=True)
    found, failed = harvest.collect(http, [CRAWL], [board], harvest.IndexCache(tmp_path), log=lambda m: None)
    assert found == {} and len(failed) == 1


def test_check_one_counts_roles(tmp_path):
    http, _ = fake_http({greenhouse.listing_url("flowtraders", content=False): fixture_json("greenhouse_flowtraders.json")})
    result = harvest.check_one({"board": "greenhouse", "slug": "flowtraders"}, http, ALLOWED, date(2026, 10, 24))
    # 3 roles: AI Engineering Manager (Amsterdam), Core Data Engineer (Amsterdam), Corporate Receptionist (New York)
    assert result["open"] == 3 and result["location_pass"] == 3 and result["prefilter_pass"] == 1
    assert result["prefilter_pass_recent"] == 1 and result["name"] == "Flow Traders"


def test_check_one_records_errors():
    http, _ = fake_http({}, max_retries=0)
    assert "HTTP 404" in harvest.check_one({"board": "greenhouse", "slug": "gone"}, http, ALLOWED, date.today())["error"]


def test_generated_list_keeps_relevant_and_learned():
    today = date(2026, 10, 24)
    checked = {
        "greenhouse:a": {"identity": "greenhouse:a", "config": {"board": "greenhouse", "slug": "a"},
                         "checked_at": "2026-10-24", "open": 5, "location_pass": 2, "prefilter_pass": 1},
        "greenhouse:b": {"identity": "greenhouse:b", "config": {"board": "greenhouse", "slug": "b"},
                         "checked_at": "2026-10-24", "open": 4, "location_pass": 0},
        "greenhouse:c": {"identity": "greenhouse:c", "config": {"board": "greenhouse", "slug": "c"},
                         "checked_at": "2026-10-24", "error": "HTTP 404"},
    }
    previous = [{"identity": "lever:learned", "board": "lever", "slug": "learned", "origin": "learned"},
                {"identity": "greenhouse:a", "first_seen": "2026-09-01", "origin": "crawl"}]
    out = {g["identity"]: g for g in harvest.generated_list(checked, previous, today)}
    assert set(out) == {"greenhouse:a", "lever:learned"}  # b has no role in an allowed country, c is gone
    assert out["greenhouse:a"]["first_seen"] == "2026-09-01" and out["greenhouse:a"]["last_relevant"] == "2026-10-24"


def test_whole_harvest_skips_configured_and_recently_checked(tmp_path):
    routes = index_routes({q: [] for b in harvest.BOARDS for q in b.queries})
    routes.update(index_routes({"job-boards.greenhouse.io/*": [
        "https://job-boards.greenhouse.io/flowtraders/jobs/1", "https://job-boards.greenhouse.io/stripe/jobs/2"]}))
    routes[greenhouse.listing_url("flowtraders", content=False)] = fixture_json("greenhouse_flowtraders.json")
    http, session = fake_http(routes)
    run = harvest.Harvest(state_root=tmp_path, allowed=ALLOWED, configured={"greenhouse:stripe"},
                          make_http=lambda: http, index_http=http, log=lambda m: None)
    summary = run.run([CRAWL], today=date(2026, 10, 24))
    gh = summary["boards"]["greenhouse"]
    assert (gh["found"], gh["already_configured"], gh["checked"], gh["relevant"]) == (2, 1, 1, 1)
    assert summary["boards"]["softgarden"] == {"supported": False, "found": 0}
    listing_calls = sum("boards-api" in u for u in session.urls())
    assert run.run([CRAWL], today=date(2026, 10, 25))["checked_now"] == 0  # checked recently: not again
    assert sum("boards-api" in u for u in session.urls()) == listing_calls
    generated = [json.loads(line) for line in (tmp_path / "companies_generated.jsonl").read_text().splitlines()]
    assert [g["identity"] for g in generated] == ["greenhouse:flowtraders"] and generated[0]["name"] == "Flow Traders"


def test_a_harvest_whose_index_fails_keeps_the_old_list(tmp_path):
    # 2026-11-02: every index page answered 504, the list was written empty, and runs fetched 44
    # companies instead of about 750 for 16 days.
    old = [{"identity": f"greenhouse:c{i}", "board": "greenhouse", "slug": f"c{i}", "origin": "crawl"} for i in range(20)]
    path = tmp_path / "companies_generated.jsonl"
    path.write_text("".join(json.dumps(g) + "\n" for g in old))
    http, _ = fake_http({}, max_retries=0)  # every index page fails
    run = harvest.Harvest(state_root=tmp_path, allowed=ALLOWED, configured=set(), make_http=lambda: http,
                          index_http=http, log=lambda m: None)
    summary = run.run([CRAWL], today=date(2026, 11, 2))
    assert "index page" in summary["refused"] and summary["relevant"] == 20
    assert [json.loads(line)["identity"] for line in path.read_text().splitlines()] == [g["identity"] for g in old]
    last = json.loads((tmp_path / "harvest.jsonl").read_text().splitlines()[-1])
    assert last["refused"] == summary["refused"]


def test_a_harvest_that_would_shrink_the_list_much_is_refused():
    old = [{"identity": str(i)} for i in range(100)]
    assert "shrink from 100 to 89" in harvest.refusal(old, old[:89], [])
    assert harvest.refusal(old, old[:90], []) is None and harvest.refusal([], [], []) is None


def test_sample_records_roles_with_groups_terms_and_parking(tmp_path):
    http, _ = fake_http({greenhouse.listing_url("flowtraders", content=False): fixture_json("greenhouse_flowtraders.json")})
    entry = {"identity": "greenhouse:flowtraders", "board": "greenhouse", "slug": "flowtraders", "name": "Flow Traders"}
    record = harvest.sample_one(entry, http, ALLOWED, {"GB", "US"}, date(2026, 10, 24))
    roles = {r["title"]: r for r in record["roles"]}
    assert roles["Core Data Engineer"]["pass"] and roles["Core Data Engineer"]["group"] == "EU/EEA/CH"
    assert roles["Corporate Receptionist"]["group"] == "US" and roles["Corporate Receptionist"]["parked"]
    assert roles["Core Data Engineer"]["age"] == 1 and record["seconds"] >= 0


def test_sample_accounts_is_per_board_and_repeatable():
    generated = [{"identity": f"greenhouse:{i}", "board": "greenhouse"} for i in range(10)]
    generated += [{"identity": f"ashby:{i}", "board": "ashby"} for i in range(2)]
    first = harvest.sample_accounts(generated, 3)
    assert len(first) == 5 and first == harvest.sample_accounts(generated, 3)


@pytest.mark.parametrize("error, temporary", [
    ("HTTP 429 from https://x.recruitee.com", True), ("HTTP 503 from x after 3 attempts", True),
    ("ReadTimeout from x after 3 attempts", True), ("HTTP 404 from x", False), ("", False),
])
def test_temporary_errors(error, temporary):
    assert harvest.temporary_error({"error": error}) is temporary


def test_recheck_errors_repeats_rate_limited_checks(tmp_path):
    routes = index_routes({q: [] for b in harvest.BOARDS for q in b.queries})
    routes.update(index_routes({"job-boards.greenhouse.io/*": ["https://job-boards.greenhouse.io/flowtraders/jobs/1"]}))
    url = greenhouse.listing_url("flowtraders", content=False)
    routes[url] = (429, 429, 429, fixture_json("greenhouse_flowtraders.json"))
    http, _ = fake_http(routes)
    run = harvest.Harvest(state_root=tmp_path, allowed=ALLOWED, configured=set(), make_http=lambda: http,
                          index_http=http, log=lambda m: None)
    assert run.run([CRAWL], today=date(2026, 10, 24))["relevant"] == 0  # 3 tries, all rate limited
    run.recheck_errors = True
    assert run.run([CRAWL], today=date(2026, 10, 24))["relevant"] == 1
