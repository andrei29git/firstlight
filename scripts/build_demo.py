"""Builds the data behind `jobhunt serve --demo`: demo/state (roles, postings, scores, runs and the
wishlist), demo/config (the demo's companies and limits) and demo/tracker.json (applications,
notes and application-help drafts).

Everything is invented: the companies, their postings (on .example addresses), and the scores,
written for the example candidate in tests/example_config in the same form the scoring model
returns. Nothing is fetched and no model is called. Dates are relative to ANCHOR; the demo moves
them to the day it is started (jobhunt/demo.py), so the app always looks in use.

    uv run python scripts/build_demo.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunt import config as cfg
from jobhunt import leakcheck, pipeline, scoring, wishlist
from jobhunt.companies import Company
from jobhunt.sources.base import Job
from jobhunt.state import State

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests" / "example_config"
DEMO = ROOT / "demo"
ANCHOR = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
ACCOUNT = "subscription"


@dataclass(frozen=True)
class Employer:
    name: str
    board: str
    slug: str
    origin: str = "config"  # "config": the user's list; "generated": found in the generated list
    wishlist: bool = False


EMPLOYERS = {e.name: e for e in [
    Employer("Polderbyte", "greenhouse", "polderbyte", wishlist=True),
    Employer("Tulpwerk Systems", "lever", "tulpwerk"),
    Employer("Carpatica Software", "ashby", "carpatica", wishlist=True),
    Employer("Danubit", "recruitee", "danubit"),
    Employer("Spreewerk Analytics", "personio", "spreewerk"),
    Employer("Isarflow Robotics", "smartrecruiters", "isarflow"),
    Employer("Rivoli Ledger", "ashby", "rivoli-ledger", wishlist=True),
    Employer("Lumen Fret", "workable", "lumen-fret"),
    Employer("Grachtwise", "greenhouse", "grachtwise", origin="generated"),
    Employer("Banat Cloud", "teamtailor", "banat-cloud", origin="generated"),
    Employer("Elbmark Energy", "greenhouse", "elbmark", origin="generated"),
    Employer("Vercors Data", "lever", "vercors-data", origin="generated"),
]}


@dataclass
class Role:
    id: str
    company: str
    title: str
    place: str
    posted: int  # days before ANCHOR
    found: float  # days before ANCHOR
    posting: str
    score: dict | None = None
    model: str = "sonnet"  # "haiku+sonnet": the generated list's cascade
    department: str = "Engineering"
    employment_type: str = "Full-time"
    deadline: int | None = None  # days after ANCHOR
    extra: dict = field(default_factory=dict)


def blocker(kind: str, severity: str, detail: str) -> dict:
    return {"type": kind, "severity": severity, "detail": detail, "min_years": None}


def score(fit: int, odds: str, action: str, explanation: str, angle: str, gaps: list[str],
          blockers: list[dict] | None = None) -> dict:
    return {"fit": fit, "odds": odds, "action": action, "explanation": explanation, "angle": angle, "gaps": gaps,
            "blockers": blockers or [], "start_date": None, "deadline": None}


ROLES = [
    Role("pb-1", "Polderbyte", "Graduate Backend Engineer (Go)", "Amsterdam, Netherlands", 6, 5.2,
         """Polderbyte runs the payment rails behind a few thousand Dutch web shops: iDEAL, cards and payouts.
Our graduate programme puts you in a product team from week one, with a mentor and a written plan for your first six months.

What you will do
- Build and run Go services for payment routing, refunds and settlement.
- Design PostgreSQL schemas and the migrations that change them safely.
- Take part in code review and, after onboarding, a gentle on-call rota.

What we look for
- A degree in computer science or similar, finished or finishing this academic year.
- Experience with Go or another backend language through internships or projects.
- Comfort with SQL, Docker and CI.
- English; Dutch is not required.

We sponsor relocation to Amsterdam and offer a 30% ruling application where eligible.""",
         score(9, "medium", "apply_now",
               "A graduate backend role in Go on payments, the candidate's strongest area: the refunds service at Acme Payments is "
               "almost the same work. EU-based, English only, and the programme is built for new graduates.",
               "The Go refunds service at Acme Payments, then the route-planning API latency work at Globex Logistics.",
               ["Settlement and reconciliation", "On-call experience"])),
    Role("rl-1", "Rivoli Ledger", "Junior Software Engineer, Payments Platform", "Paris, France", 9, 8.1,
         """Rivoli Ledger builds accounting infrastructure for European marketplaces: ledgers, payouts and VAT reports.
You will join the Payments Platform team (eight engineers) that owns money movement end to end.

Your work
- Python and Go services behind our payouts API, on PostgreSQL and Kafka.
- Idempotent, well-tested code: every euro has to add up.
- Work with product and finance on new payment methods.

You
- Graduated recently or graduate within the next year, in CS or a related field.
- Have built a backend service in Python or Go, in an internship or a serious project.
- Professional French is a plus: the finance team works in French.

Hybrid, three days a week in our Paris office near the Louvre.""",
         score(9, "medium", "apply_now",
               "Payments backend in Python and Go for junior engineers: a direct match for the candidate's internships. "
               "French helps with the finance team; the candidate's B1 may be enough, but say so.",
               "The refunds service at Acme Payments, framed around correctness and idempotency.",
               ["Kafka", "Accounting concepts (double-entry ledgers)"],
               [blocker("language", "check", "Professional French is a plus; the candidate has French at B1.")])),
    Role("cs-1", "Carpatica Software", "Graduate Software Engineer (Python)", "Cluj-Napoca, Romania", 4, 3.4,
         """Carpatica Software builds scheduling and dispatch software for regional transport companies across Central Europe.
Join our 2027 graduate intake in Cluj-Napoca.

- Backend work in Python (FastAPI) on PostgreSQL, deployed on Kubernetes.
- A rotation through two teams in the first year: routing and integrations.
- Pairing, code review and a training budget from day one.

Requirements: a computer science degree (final-year students welcome), Python, SQL, Git. English at work; Romanian is not needed.""",
         score(8, "high", "apply_now",
               "Python and FastAPI on PostgreSQL in a graduate intake that welcomes final-year students, in an EU country with "
               "English as the working language. The routing team overlaps with the candidate's route-planning internship.",
               "The route-planning API at Globex Logistics (p95 latency from 900 ms to 210 ms).",
               ["Kubernetes beyond the basics"])),
    Role("sp-1", "Spreewerk Analytics", "Junior Platform Engineer (Kubernetes)", "Berlin, Germany", 12, 11.0,
         """Spreewerk Analytics turns retail footfall data into forecasts for 400 stores across Germany.
Our platform team keeps the data pipelines and 60 services running on Kubernetes.

You will
- Improve our deployment tooling (GitHub Actions, Argo CD, Terraform).
- Write the internal tools developers use every day, mostly in Go and Python.
- Help us move from hand-written dashboards to proper SLOs.

You have
- One year of experience running services on Kubernetes in production, or equivalent internship experience.
- Go or Python, Docker, Linux.
English-speaking team, office in Kreuzberg, two days a week.""",
         score(8, "medium", "apply_now",
               "Platform and developer-tooling work in Go and Python, the candidate's stated interests, with GitHub Actions "
               "and Docker already on the profile. The Kubernetes experience asked for is the main question.",
               "CI and deployment work around the Acme Payments refunds service, and Kubernetes basics.",
               ["Terraform", "Argo CD", "Production Kubernetes"],
               [blocker("experience", "check", "Asks for one year of production Kubernetes, or equivalent internship experience.")])),
    Role("tw-1", "Tulpwerk Systems", "Developer Tools Engineer, New Grad", "Utrecht, Netherlands", 3, 2.6,
         """Tulpwerk Systems makes the build and test platform that 2,000 engineers at our customers use every day.
As a new graduate on the Developer Tools team you will work on our CLI, the build cache and the test-results service.

- Go and TypeScript; services on PostgreSQL.
- You own features from design note to release.
- Fully English-speaking; hybrid in Utrecht.

We hire new graduates every autumn and spring. Internship experience with CI systems is a plus.""",
         score(8, "medium", "apply_now",
               "Developer tooling in Go for new graduates: exactly the candidate's interest, with some TypeScript on the profile. "
               "English-speaking and in the EU.",
               "GitHub Actions and testing work from the internships, and the Go service at Acme Payments.",
               ["Build systems and caching"])),
    Role("db-1", "Danubit", "Junior Backend Developer (Go)", "Bucharest, Romania", 15, 14.2,
         """Danubit builds the booking engine behind 300 hotels in Romania and Bulgaria.
We are looking for a junior backend developer to join the availability team.

- Go microservices, PostgreSQL, Redis.
- REST and gRPC APIs consumed by our partners.
- Mentoring from senior engineers, a clear growth path.

Requirements: up to two years of experience, Go or a strong wish to work in it, SQL. English required.""",
         score(7, "high", "apply_now",
               "A junior Go backend role in the EU, open to candidates without full-time experience. Hotel booking is a new "
               "domain, but the work is the same kind of API and database engineering.",
               "The Go refunds service at Acme Payments.",
               ["gRPC", "Redis"])),
    Role("if-1", "Isarflow Robotics", "Graduate Software Engineer, Fleet Backend", "Munich, Germany", 7, 6.3,
         """Isarflow Robotics builds autonomous floor robots for warehouses. The Fleet Backend team runs the services that send
missions to 1,500 robots and collect their telemetry.

- Python and Go services, PostgreSQL and TimescaleDB, MQTT.
- Graduate programme with a mentor and a structured first year.
- German at B2 or better: our customers and much of the team work in German.

A computer science degree, finished or finishing in 2027.""",
         score(7, "medium", "apply_now",
               "Backend work in Python and Go for graduates, close to the candidate's route-planning experience. The role asks "
               "for German at B2, which is not on the profile.",
               "The route-planning API at Globex Logistics: fleets of vehicles, latency and reliability.",
               ["MQTT", "Time-series databases"],
               [blocker("language", "check", "German at B2 or better is required; the profile lists no German.")])),
    Role("lf-1", "Lumen Fret", "Software Engineer Intern, Route Optimisation (6 months)", "Lyon, France", 5, 4.4,
         """Lumen Fret plans the routes of 900 trucks a day across France and Spain.
Join the optimisation team for a six-month internship.

- Python services that turn our solver's output into driver plans.
- Measure and speed up the planning API.
- You must be enrolled at a university for the whole internship (internship agreement required).""",
         score(8, "high", "apply_now",
               "Route optimisation in Python, almost the same work as the Globex Logistics internship, at a company that "
               "operates in Spain and France. An internship agreement means the candidate must still be enrolled.",
               "The route-planning API latency work at Globex Logistics.",
               ["Operations research solvers"],
               [blocker("enrolment", "check", "Requires enrolment and an internship agreement for the whole six months.")])
         | {"start_date": "2027-03-01"},
         department="Optimisation", employment_type="Internship", deadline=5),
    Role("gw-1", "Grachtwise", "Graduate Data Engineer", "Rotterdam, Netherlands", 2, 1.3,
         """Grachtwise forecasts water levels and maintenance work for Dutch canal and harbour authorities.
Graduate data engineers build the pipelines that bring sensor data into our models.

- Python, PostgreSQL, dbt and Airflow.
- Clean data contracts with the data science team.
- English-speaking team in Rotterdam, hybrid.

Open to graduates of 2026 and 2027.""",
         score(7, "medium", "apply_now",
               "A graduate data engineering role in Python and PostgreSQL, open to the candidate's graduation year. Pipelines "
               "rather than services, but close to the candidate's backend work.",
               "PostgreSQL and API work at both internships.",
               ["dbt", "Airflow"]),
         model="haiku+sonnet"),
    Role("db-2", "Danubit", "Graduate Software Engineer, Payments", "Bucharest, Romania", 3, 2.1,
         """Danubit's payments team takes deposits and refunds for 300 hotels. Graduate role: Go services, PostgreSQL,
payment provider integrations. Final-year students are welcome to apply; English at work.""",
         score(8, "high", "apply_now",
               "Payments backend in Go for graduates: the same kind of work as the candidate's refunds service at Acme "
               "Payments, in an EU country, open to final-year students.",
               "The Go refunds service at Acme Payments.", ["Payment provider integrations"]), deadline=0),
    Role("vd-2", "Vercors Data", "Graduate Backend Engineer (Go)", "Grenoble, France", 2, 0.9,
         """Vercors Data's platform team serves forecasts to 80 ski resorts through a public API. Graduate backend engineer:
Go, PostgreSQL, Docker; small team, English or French.""",
         score(7, "high", "apply_now",
               "A graduate Go backend role on a public API, with English accepted. A small company, so odds are good.",
               "The route-planning API at Globex Logistics.", ["Public API design at scale"]),
         model="haiku+sonnet"),
    # Mid and low fits, some blocked
    Role("em-1", "Elbmark Energy", "Junior Frontend Engineer (React)", "Hamburg, Germany", 10, 9.5,
         """Elbmark Energy builds the customer portal for 200,000 households on our dynamic electricity tariff.
Junior frontend engineer: React, TypeScript, design system work.""",
         score(4, "medium", "skip",
               "Mostly frontend work in React; the candidate wants backend and platform roles and has only some TypeScript.",
               "The TypeScript the candidate has used.", ["React", "Frontend design systems"]),
         model="haiku", deadline=-3),
    Role("vd-1", "Vercors Data", "Machine Learning Intern", "Grenoble, France", 13, 12.0,
         """Vercors Data builds forecasting models for ski resorts. Six-month machine learning internship: PyTorch, model
evaluation, some data engineering. Enrolled students only (internship agreement).""",
         score(5, "medium", "skip",
               "Python, but the work is machine learning research rather than backend engineering, which the profile does not "
               "list as an interest.", "Python and data work from the internships.", ["PyTorch", "Model evaluation"],
               [blocker("enrolment", "check", "Enrolled students only, with an internship agreement.")]),
         model="haiku", employment_type="Internship"),
    Role("bc-1", "Banat Cloud", "Junior DevOps Engineer", "Timișoara, Romania", 8, 7.2,
         """Banat Cloud runs managed Kubernetes for Romanian public institutions.
Junior DevOps engineer: Terraform, Kubernetes, on-call. Fluent Romanian is required for customer work.""",
         score(6, "medium", "skip",
               "Platform work the candidate would like, but it requires fluent Romanian, which the candidate does not speak.",
               "Docker and GitHub Actions work.", ["Terraform", "On-call"],
               [blocker("language", "blocking", "Fluent Romanian is required for customer work.")]),
         model="haiku+sonnet"),
    Role("cs-2", "Carpatica Software", "QA Automation Engineer", "Iași, Romania", 20, 19.0,
         """QA automation engineer for our dispatch apps: Playwright tests, test plans, release checks.""",
         score(5, "high", "skip",
               "An EU role open to graduates, but testing rather than backend development.",
               "Testing done around the Acme Payments refunds service.", ["Playwright", "Test planning"])),
    Role("if-2", "Isarflow Robotics", "Embedded Software Graduate (C++)", "Munich, Germany", 7, 6.3,
         """Embedded C++ on the robots' motion controllers, real-time Linux. German at B2.""",
         score(3, "low", "skip",
               "Embedded C++ is outside the candidate's stack, and the role asks for German at B2.",
               "Performance work on the route-planning API.", ["C++", "Real-time systems"],
               [blocker("language", "check", "German at B2 or better is required.")])),
    Role("sp-2", "Spreewerk Analytics", "Werkstudent Data Engineering (m/w/d)", "Berlin, Germany", 16, 15.4,
         """Working student in data engineering, 20 hours a week. You must be enrolled at a German university.""",
         score(6, "medium", "skip",
               "Interesting data work, but a working-student contract needs enrolment at a German university.",
               "Python and PostgreSQL from the internships.", ["Airflow"],
               [blocker("enrolment", "blocking", "Enrolment at a German university is required for a Werkstudent contract.")]),
         employment_type="Part-time"),
    Role("pb-2", "Polderbyte", "Mobile Engineer (iOS), Early Career", "Amsterdam, Netherlands", 6, 5.2,
         """Swift and SwiftUI on our merchant app.""",
         score(2, "medium", "skip", "Mobile development, which the candidate is not interested in.",
               "None that fits.", ["Swift", "iOS"])),
    # Waiting to be scored
    Role("rl-2", "Rivoli Ledger", "Backend Engineer, Risk (Junior)", "Paris, France", 1, 0.3,
         """Junior backend engineer on fraud and risk rules: Python, PostgreSQL, rules engines."""),
    Role("tw-2", "Tulpwerk Systems", "Graduate Site Reliability Engineer", "Utrecht, Netherlands", 1, 0.2,
         """SRE for our build platform: Kubernetes, Prometheus, incident reviews. New graduates welcome."""),
    Role("gw-2", "Grachtwise", "Junior Python Developer", "Eindhoven, Netherlands", 0, 0.1,
         """Python services for sensor ingestion. Junior level, English-speaking."""),
]

# The user's own data: applications at different stages, notes, a hidden role and helper drafts.
TRACKER = {
    "applications": [
        {"role": "pb-1", "stage": "interviewing", "history": [("applied", 4.9), ("oa_pending", 3.8), ("oa_done", 3.0),
                                                              ("interviewing", 1.2)],
         "next_step_by": -2, "notes": "Technical interview with the payments team: system design and a Go exercise."},
        {"role": "cs-1", "stage": "applied", "history": [("applied", 3.0)]},
        {"role": "if-1", "stage": "oa_pending", "history": [("applied", 5.5), ("oa_pending", 2.0)], "next_step_by": -4,
         "notes": "Online assessment: 90 minutes, Python."},
        {"role": "db-1", "stage": "to_apply", "history": [("to_apply", 1.0)]},
        {"company": "Kestrel Pay", "role": "Backend Intern", "location": "Lisbon, Portugal", "stage": "rejected",
         "history": [("applied", 34.0), ("rejected", 20.0)]},
        {"company": "Aalto Freight", "role": "Junior Software Engineer", "location": "Helsinki, Finland", "stage": "applied",
         "history": [("applied", 27.0)]},
    ],
    "notes": {"rl-1": "Mention the French at B1 in the cover letter, and that I am improving it."},
    "hidden": ["pb-2"],
    "questions": {"pb-1": [
        {"id": "q1", "label": "Why do you want to join Polderbyte's graduate programme?", "kind": "text",
         "required": True, "max_words": 150},
        {"id": "q2", "label": "Tell us about a time you made a system faster or more reliable.", "kind": "text",
         "required": True},
        {"id": "q3", "label": "Do you need a visa to work in the Netherlands?", "kind": "choice", "required": True,
         "options": ["Yes", "No"]},
    ]},
    # A chat about the role, with a reply that uses paragraphs, a list and bold (2026-11-19).
    "messages": {"pb-1": [
        ["user", "Which of my projects should I mention for this role, and why?"],
        ["assistant", "Lead with **the refunds service at Acme Payments**: it is the closest to what Polderbyte's "
                      "graduates build.\n\n- It is Go on payments, the team's own stack.\n- Idempotent requests and "
                      "tests that prove the money adds up answer the posting's \"build and run\" line.\n- You own the "
                      "service end to end, which suits a programme that gives ownership from week one.\n\nThen the "
                      "route-planning API at Globex Logistics, for the p95 latency work: it shows you measure before "
                      "you change things."],
    ]},
    "drafts": [
        {"role": "pb-1", "kind": "question", "qid": "q1",
         "answer": "I want to build payment systems that people can rely on, and Polderbyte's rails sit exactly there. "
                   "At Acme Payments I am building a refunds service in Go, and the part I enjoy most is the care it takes: "
                   "idempotent requests, clear states and tests that prove the money adds up. Your graduate programme "
                   "would let me keep doing that work at a larger scale, with a mentor and real ownership from the first "
                   "weeks. I also like that routing, refunds and settlement live in one team, so I could follow a payment "
                   "from the shop to the payout. Amsterdam and an English-speaking team make it a place I can start "
                   "contributing to straight away."},
        {"role": "pb-1", "kind": "question", "qid": "q2",
         "answer": "At Globex Logistics I worked on the route-planning API, whose slowest requests took about 900 ms at "
                   "the 95th percentile. I profiled the endpoint, found that most of the time went into repeated database "
                   "queries for each stop, and replaced them with one batched query and a small cache for reference "
                   "data. I added a latency test to CI so the gain would not slip back. The p95 came down to 210 ms, "
                   "and the planners using the tool noticed the difference the same week."},
        {"role": "pb-1", "kind": "question", "qid": "q3", "answer": "No", "choices": ["No"]},
        {"role": "rl-1", "kind": "letter", "qid": "letter",
         "answer": "Dear Rivoli Ledger team,\n\nI am applying for the Junior Software Engineer role on the Payments "
                   "Platform. I am in the final year of a BSc in Computer Science and work part time as a backend intern "
                   "at Acme Payments, where I am building a refunds service in Go. Getting every state transition right, "
                   "and proving it with tests, is the part of the job I care about most, and it is what your platform "
                   "asks for.\n\nBefore that, at Globex Logistics, I brought the p95 latency of a route-planning API "
                   "down from 900 ms to 210 ms by batching queries and adding a latency check to CI. I work in Python "
                   "and Go every day, on PostgreSQL and Docker.\n\nMy French is at B1 and I am working on it, so I "
                   "could follow the finance team's discussions and grow into them.\n\nThank you for your time; I would "
                   "be glad to talk.\n\n[Your name]",
         "missing": ["Your name as it should appear under the letter"]},
    ],
}

LIMITS = '''# Example application limits for the demo's companies.

[[limit]]
company = "Polderbyte"
scope = "company"
max = 2
window_days = 180
note = "At most two applications in six months."
'''


def when(days: float) -> str:
    return (ANCHOR - timedelta(days=days)).isoformat(timespec="seconds")


def companies_toml() -> str:
    lines = ["# The demo's companies: all of them invented. The generated list's companies are in the state.\n"]
    for e in EMPLOYERS.values():
        if e.origin == "config":
            lines.append(f'[[company]]\nname = "{e.name}"\nboard = "{e.board}"\nslug = "{e.slug}"\n')
    return "\n".join(lines)


def runs() -> list[dict]:
    """A day of runs: scheduled ones on GitHub every two hours and one on the laptop."""
    found = []
    for i, hours in enumerate(range(23, 0, -2)):
        started = ANCHOR - timedelta(hours=hours, minutes=-11)
        new = [3, 0, 1, 5, 2, 0, 4, 1, 2, 6, 1, 3][i]
        found.append({
            "started_at": started.isoformat(timespec="seconds"), "ran_on": "github", "kind": "scheduled",
            "seconds": 240 + 37 * (i % 4), "fetch_seconds": 150 + 20 * (i % 3), "companies": 9 + i % 3, "errors": 0,
            "jobs": {"fetched": 312 + i, "new": new, "passed": 41, "closed": i % 2},
            "sources": {e.name: {"board": e.board, "fetched": 12 + i % 5} for e in EMPLOYERS.values() if e.origin == "config"},
            "generated": {"greenhouse": {"companies": 2, "fetched": 60, "new": new // 2, "passed": 9},
                          "lever": {"companies": 1, "fetched": 25, "new": 0, "passed": 3}},
            "scoring": {"scheduled": {"model": "sonnet", "calls": new, "cost_usd": round(0.021 * new, 4), "pending": new,
                                      "deferred": 0, "stopped": None}},
            "details_fetched": new, "account": {"key": ACCOUNT, "how": "token"},
        })
    found.append({
        "started_at": (ANCHOR - timedelta(hours=7)).isoformat(timespec="seconds"), "ran_on": "laptop", "launchd": True,
        "kind": "run, generated list: due " + pipeline.LOCAL_KIND, "seconds": 95, "companies": 2, "errors": 0,
        "jobs": {"fetched": 40, "new": 0, "passed": 6, "closed": 0}, "sources": {}, "account": {"key": ACCOUNT, "how": "token"},
    })
    return sorted(found, key=lambda r: r["started_at"])


def build() -> int:
    settings = cfg.load_settings(EXAMPLE)
    allowed = set(settings.get("locations", {}).get("allowed_countries", []))
    versions = {m: scoring.build_prompt(EXAMPLE, settings, model=m).version for m in ("sonnet", "haiku")}
    consider = bool(settings.get("scoring", {}).get("consider_start_date", False))
    target = DEMO / "state.new"
    shutil.rmtree(target, ignore_errors=True)
    state = State(target)
    keys: dict[str, str] = {}
    jobs_of: dict[str, list[tuple[Role, Job]]] = {}
    for role in ROLES:
        e = EMPLOYERS[role.company]
        job = Job(company=e.name, title=role.title, url=f"https://careers.{e.slug}.example/jobs/{role.id}",
                  source=e.board, source_id=role.id, locations=[role.place], description=role.posting,
                  posted=(ANCHOR - timedelta(days=role.posted)).date().isoformat(), department=role.department,
                  employment_type=role.employment_type,
                  deadline=(ANCHOR + timedelta(days=role.deadline)).date().isoformat() if role.deadline is not None else None)
        jobs_of.setdefault(e.name, []).append((role, job))
    for name, pairs in jobs_of.items():
        e = EMPLOYERS[name]
        company = Company(name=e.name, board=e.board, slug=e.slug, origin=e.origin,
                          identity=f"{e.board}:{e.slug}" if e.origin == "generated" else None)
        pipeline.update_jobs(state, pipeline.Fetched(company, [job for _, job in pairs]), allowed, when(0.1))
        for role, job in pairs:
            key = next((k for k, r in state.jobs.items() if r["company"] == e.name and r["source_id"] == role.id), None)
            if key is None or state.jobs[key]["prefilter"] != "pass":
                print(f"{role.id}: did not pass the pre-filter", file=sys.stderr)
                return 1
            keys[role.id] = key
            record = state.jobs[key]
            record.update(first_seen=when(role.found), updated_at=when(role.found))
            if e.origin == "generated":
                record["titlecheck"] = {"version": "demo", "dropped": False, "checked_at": when(role.found - 0.01)}
            if not role.score:
                continue
            for i, model in enumerate(role.model.split("+")):
                result = {**role.score, "deadline": job.deadline}
                if model == "haiku" and role.model == "haiku+sonnet":
                    result = {**result, "fit": max(6, result["fit"] - 1)}  # Haiku's first look, then Sonnet's
                s = scoring.Score(key=key, description_hash=record["description_hash"], prompt_version=versions[model],
                                  model=model, result=result, status=scoring.status(result["blockers"], consider),
                                  deadline=job.deadline, scored_at=when(role.found - 0.02 * (i + 1)),
                                  seconds=18.0 if model == "sonnet" else 7.5,
                                  cost_usd=0.021 if model == "sonnet" else 0.0014, tokens={}, rate_limit=None)
                state.scores[s.id] = {**s.to_record(), "account": ACCOUNT}
    for e in EMPLOYERS.values():
        if e.wishlist:
            wishlist.set_company(target, e.name, True, {"board": e.board, "slug": e.slug}, source="demo",
                                 now=when(20))
    for run in runs():
        state.append_run(run)
    state.save()

    # The companies on the generated list, as the harvest records them.
    (target / "companies_generated.jsonl").write_text("".join(
        json.dumps({"identity": f"{e.board}:{e.slug}", "board": e.board, "slug": e.slug, "name": e.name,
                    "verified": when(30)[:10], "relevant": 3, "prefilter_pass": 3}) + "\n"
        for e in EMPLOYERS.values() if e.origin == "generated"))
    (target / "demo.json").write_text(json.dumps({"anchor": ANCHOR.isoformat()}) + "\n")

    tracker = json.loads(json.dumps(TRACKER))
    for item in tracker["applications"]:
        if item["role"] in keys:
            role = next(r for r in ROLES if r.id == item["role"])
            item.update(job_key=keys[role.id], company=role.company, role=role.title, location=role.place)
        item["history"] = [[stage, when(days)] for stage, days in item["history"]]
        if "next_step_by" in item:
            item["next_step_by"] = when(item["next_step_by"])[:10]
    tracker["notes"] = {keys[k]: v for k, v in tracker["notes"].items()}
    tracker["hidden"] = [keys[k] for k in tracker["hidden"]]
    tracker["questions"] = {keys[k]: v for k, v in tracker["questions"].items()}
    for d in tracker["drafts"]:
        d["job_key"] = keys[d["role"]]
    tracker["messages"] = {keys[k]: v for k, v in tracker["messages"].items()}
    tracker["updated_at"] = when(0.5)

    config = DEMO / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "companies.toml").write_text(companies_toml())
    (config / "limits.toml").write_text(LIMITS)
    (config / "applications_seed.toml").write_text("# The demo's applications are in demo/tracker.json.\n")
    (DEMO / "tracker.json").write_text(json.dumps(tracker, indent=1, ensure_ascii=False) + "\n")

    shutil.rmtree(DEMO / "state", ignore_errors=True)
    target.rename(DEMO / "state")
    found = leaks()
    if found:
        print("Private strings found in the demo:\n  " + "\n  ".join(found), file=sys.stderr)
        return 1
    scored = sum(1 for r in ROLES if r.score)
    print(f"Wrote demo/: {len(ROLES)} roles ({scored} scored, {sum(1 for r in ROLES if r.score and r.score['fit'] >= 7)} "
          f"at fit 7 or more), {len(TRACKER['applications'])} applications, {len(TRACKER['drafts'])} drafts.")
    return 0


def leaks() -> list[str]:
    """Lines of the demo matching this repository's private leak patterns (none in a public copy)."""
    config = ROOT / "config"
    if not (config / "candidate.toml").exists():
        return []
    compiled = [re.compile(p, re.IGNORECASE) for p in leakcheck.patterns(config)]
    return [f"{path.relative_to(ROOT)}:{n}" for path in sorted(DEMO.rglob("*")) if path.is_file()
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1) if any(rx.search(line) for rx in compiled)]


if __name__ == "__main__":
    sys.exit(build())
