""""Find more companies": a web search for employers, checked with the tool's own fetching, then
accepted (covered with the generated list's rules, or the wishlist) or rejected (remembered)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta

from jobhunt import accepted, cli, company_search as cs, scoring, store, wishlist
from jobhunt import config as cfg
from jobhunt.state import write_jsonl

from .conftest import EXAMPLE_CONFIG
from .fakes import FakeClaude, claude_stream
from .test_jobs_web import client  # noqa: F401 (fixture)


def candidate(name, **extra):
    return {"name": name, "careers_url": f"https://{name.lower()}.example/careers", "domain": "software",
            "city": "Utrecht", "why": "Builds payment systems.", "entry_level": "graduate programme", **extra}


def test_the_search_may_use_web_search_and_nothing_else_and_names_no_candidate():
    claude = FakeClaude(respond=lambda message: claude_stream({"companies": [candidate("Globex")]}))
    result = scoring.call_claude(cs.prompt(), cs.message("growing", ["NL"], ["software"], ["Acme"]), claude,
                                 check=cs.check)
    assert result.output["companies"][0]["name"] == "Globex"
    argv = claude.calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch" and argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert argv[argv.index("--model") + 1] == "sonnet" and claude.calls[0]["cwd_files"] == []
    text = claude.calls[0]["stdin"]
    assert "Netherlands" in text and "backend and software engineering" in text and "Acme" in text


def test_known_companies_are_never_suggested(tmp_path):
    write_jsonl(tmp_path / "companies_generated.jsonl", [{"identity": "greenhouse:initech", "name": "Initech Group",
                                                          "slug": "initech"}])
    known = cs.known_names(tmp_path, {"acme"}, {"globex"})
    assert cs.known_as("Acme", known) == "yours" and cs.known_as("Globex", known) == "rejected"
    assert cs.known_as("Initech", known) == "on the generated list" and cs.known_as("Hooli", known) is None


def test_suggestions_need_readable_jobs_in_the_countries_with_entry_level_evidence_first():
    checked = [candidate("A", entry_level="not seen"), candidate("B", problem="no open roles in these countries"),
               candidate("C"), *[candidate(f"D{i}") for i in range(12)]]
    ranked = cs.rank(checked)
    assert len(ranked) == cs.MAX_SUGGESTIONS and "B" not in {r["name"] for r in ranked}
    assert ranked[0]["name"] == "C" and "A" not in {r["name"] for r in ranked}


def test_aggregator_pages_are_not_read_as_the_employer(monkeypatch):
    seen = []
    monkeypatch.setattr(cs.detect, "detect", lambda url, http, name=None: (seen.append(url), ([], url))[1])
    monkeypatch.setattr(cs.detect, "probe", lambda name, http: seen.append(("probe", name)) or [])
    out = cs.verify(candidate("Hooli", careers_url="https://www.glassdoor.com/Jobs/Hooli"), None, {"NL"})
    assert seen == [("probe", "Hooli")] and out["problem"] == "no job board or job links found"


def test_the_estimate_uses_the_last_searches_once_there_are_some():
    first = cs.estimate([])
    assert first["cost_usd"] == round(cs.DEFAULT_COST_PER_CALL * 2, 2) and first["window"] == round(first["cost_usd"] * 0.1, 3)
    later = cs.estimate([{"cost_usd": 3.0, "seconds": 300}])
    assert later == {"cost_usd": 3.0, "window": 0.3, "week": 0.024, "minutes": 5}


def test_accepted_companies_are_fetched_in_the_daily_slots_with_the_generated_rules(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    accepted.set_company(state, "Globex", True, {"board": "greenhouse", "slug": "globex", "note": "x"})
    accepted.set_company(state, "Hooli", True, {"board": "lever", "slug": "hooli"})
    accepted.set_company(state, "Hooli", False)
    accepted.set_company(state, "Acme", True, {"board": "greenhouse", "slug": "acme"})
    wishlist.set_company(state, "Acme", True, {"board": "greenhouse", "slug": "acme"})  # the wishlist wins
    paths = cfg.Paths(root=tmp_path, config=EXAMPLE_CONFIG, database=tmp_path / "db", backups=tmp_path / "b")
    args = argparse.Namespace(no_fetch=False, sonnet_only=False, names=[], local_only=False, generated="all")
    targets = cli.run_targets(args, paths, cfg.load_settings(EXAMPLE_CONFIG), state, say=lambda _: None)
    origins = {(c.name, c.origin) for c in targets}
    assert ("Globex", "generated") in origins and "Hooli" not in {c.name for c in targets}
    assert ("Acme", "wishlist") in origins and ("Acme", "generated") not in origins
    named = cli.run_targets(argparse.Namespace(**{**vars(args), "names": ["Globex"]}), paths,
                            cfg.load_settings(EXAMPLE_CONFIG), state, say=lambda _: None)
    assert [c.name for c in named] == ["Globex"]


def wait_for_results(client):  # noqa: F811
    for _ in range(100):
        page = client.get("/companies/find/progress").text
        if "Finding companies" not in page:
            return page
        time.sleep(0.02)
    raise AssertionError("the search did not finish")


def test_a_search_shows_its_cost_first_then_suggestions_to_accept_or_reject(client, paths, monkeypatch):  # noqa: F811
    asked = client.get("/companies/find").text
    assert "Estimated cost" in asked and "five-hour window" in asked and 'value="NL"' in asked
    monkeypatch.setattr(cs, "web_search", lambda model="sonnet", timeout=0: lambda group, text: scoring.CallResult(
        output={"companies": [candidate("Globex" if group == "established" else "Hooli"), candidate("Winton")]},
        cost_usd=1.2))
    monkeypatch.setattr(cs, "verify", lambda c, http, countries: {
        **c, "board": "greenhouse", "config": {"board": "greenhouse", "slug": c["name"].lower()}, "jobs_total": 9,
        "jobs_here": 4, "samples": [("Graduate Engineer", "https://example.com/1")]})
    started = client.post("/companies/find/start", data={"country": ["NL", "RO"], "domain": ["software", "fintech"]})
    assert started.status_code == 200
    page = wait_for_results(client)
    assert "Globex" in page and "Hooli" in page and "Winton" not in page  # Winton is in companies.toml
    assert "This search used about 24% of a five-hour window" in page and "4 open roles in these countries" in page
    with store.connect(paths.database) as conn:
        history = store.company_search(conn)["history"]
    assert history[-1]["cost_usd"] == 2.4 and history[-1]["countries"] == ["NL", "RO"] and history[-1]["suggestions"] == 2

    covered = client.post("/companies/find/decide", data={"company": "globex", "decision": "covered"})
    assert "Covered: checked daily" in covered.text and "companies-changed" in covered.headers["HX-Trigger"]
    assert [e["name"] for e in accepted.active(paths.state_dir)] == ["Globex"]
    assert accepted.active(paths.state_dir)[0]["identity"] == "greenhouse:globex"
    assert "Found by search" in client.get("/companies").text

    client.post("/companies/find/decide", data={"company": "hooli", "decision": "rejected"})
    with store.connect(paths.database) as conn:
        assert store.company_search(conn)["rejected"] == {"hooli": "Hooli"}
    client.post("/companies/find/decide", data={"company": "globex", "decision": "undo"})
    assert accepted.active(paths.state_dir) == []
    client.post("/companies/find/decide", data={"company": "globex", "decision": "wishlist"})
    assert [e["name"] for e in wishlist.active(paths.state_dir)] == ["Globex"]
    assert client.post("/companies/find/decide", data={"company": "globex", "decision": "rejected"}).status_code == 409

    # The next search leaves out the rejected company and tells the web search to skip it.
    told = []
    monkeypatch.setattr(cs, "web_search", lambda model="sonnet", timeout=0: lambda group, text: told.append(text) or
                        scoring.CallResult(output={"companies": [candidate("Hooli")]}, cost_usd=1.0))
    client.post("/companies/find/start", data={"country": ["NL"], "domain": ["software"]})
    page = wait_for_results(client)
    assert "Hooli" in told[0] and "None of the employers found could be suggested" in page


def test_a_failed_search_says_so_and_saves_nothing(client, paths):  # noqa: F811
    assert client.post("/companies/find/start", data={"country": ["NL"]}).text.count("Pick at least one") == 1
    client.post("/companies/find/start", data={"country": ["NL"], "domain": ["data"]})
    page = wait_for_results(client)
    assert "The web search did not work: no search in tests" in page
    assert accepted.active(paths.state_dir) == []


def test_today_suggests_a_new_search_every_three_months(client, paths):  # noqa: F811
    assert "Time for a new company search" not in client.get("/today").text
    with store.connect(paths.database) as conn:
        data = store.company_search(conn)
        data["history"] = [{"at": (datetime.now(UTC) - timedelta(days=91)).isoformat(timespec="seconds")}]
        store.save_company_search(conn, data)
    page = client.get("/today").text
    assert "Time for a new company search" in page and "91 days ago" in page and "/companies?find=1" in page
    assert 'hx-trigger="click, load"' in client.get("/companies?find=1").text
    assert json.loads(json.dumps(data))  # the state stays plain JSON


# --- Why a company shows no roles (2026-10-30) ----------------------------------------------------

def test_a_company_without_roles_says_why():
    from jobhunt.companies_view import roles_status
    assert roles_status(None, None, daily=False).label == "Not fetched yet"
    assert "within a day" in roles_status(None, {}, daily=True).detail
    failed = roles_status(None, {"last_error": "Acme: HTTP 403 from https://acme.example/jobs"}, daily=False)
    assert failed.label == "Fetch failed: HTTP 403" and failed.tone == "error"
    assert roles_status({"stored": 0}, {"last_fetch": {"at": "x"}}, daily=False).label == "No open roles"
    outside = roles_status({"stored": 32, "location": 32}, {"last_fetch": {"at": "x"}}, daily=False)
    assert outside.label == "No roles in your countries" and outside.detail == "32 open roles found: 32 outside your countries."
    assert roles_status({"stored": 5, "location": 2, "parked": 3}, None, daily=True).label == "Only UK or US roles"
    mixed = roles_status({"stored": 4, "location": 1, "title": 2, "titlecheck": 1}, None, daily=True)
    assert mixed.label == "None passes the filters" and "2 senior, lead or non-engineering titles" in mixed.detail


def test_the_catalogue_counts_every_stored_role_per_company_with_why_it_is_left_out(paths):
    from jobhunt import catalog as cat
    from jobhunt.state import State
    from .test_jobs_web import job
    state = State(paths.state_dir)
    for r in [job("a", "Graduate Engineer", company="Globex"),
              job("b", "Senior Engineer", company="Globex", prefilter="title: senior"),
              job("c", "Engineer", company="Globex", locations=("Mumbai, India",), prefilter="location: IN"),
              job("d", "Engineer", company="Hooli", locations=("Austin, TX, United States",), origin="generated",
                  parked=True)]:
        state.jobs[r["key"]] = r
    state.save()
    catalog = cat.Catalog(paths.state_dir, EXAMPLE_CONFIG, cfg.load_settings(EXAMPLE_CONFIG),
                          path=paths.root / "catalog.db")
    found = catalog.company_roles({"globex", "hooli"})
    assert found["globex"] == {"stored": 3, "location": 1, "parked": 0, "title": 1, "titlecheck": 0, "duplicate": 0}
    assert found["hooli"]["stored"] == 1 and found["hooli"]["parked"] == 1
    assert catalog.company_counts({"globex", "hooli"}) == {"globex": (1, 0, 1)}


def test_your_companies_with_the_generated_rules_are_recorded_by_name_in_runs_but_counted_once():
    from jobhunt import runs_view
    run = {"started_at": "2026-10-30T13:00:00+00:00",
           "sources": {"Globex": {"board": "greenhouse", "rules": "generated", "fetched": 9, "error": "HTTP 500"}},
           "generated": {"greenhouse": {"companies": 1, "fetched": 9}}}
    row = runs_view.row(0, run, {})
    assert "Globex: HTTP 500" in row.errors
    from jobhunt import pipeline
    assert pipeline.fetch_history([run])["globex"]["last_error"] == "HTTP 500"


def test_a_custom_site_in_your_list_and_on_the_wishlist_is_fetched_once(tmp_path):
    from jobhunt import harvest
    from jobhunt.companies import Company
    site = {"board": "custom", "listing_url": "https://careers.globex.example/sitemap.xml", "link_pattern": "^https://"}
    state = tmp_path / "state"
    state.mkdir()
    wishlist.set_company(state, "Globex", True, site)
    configured = harvest.configured_identities([Company.from_dict({"name": "Globex", **site})])
    assert wishlist.companies(state, configured) == []
