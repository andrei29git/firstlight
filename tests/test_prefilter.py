from __future__ import annotations

import pytest

from jobhunt import geo, pipeline, prefilter

ALLOWED = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
           "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH", "GB", "US"}


@pytest.mark.parametrize("title, passed", [
    ("Software Engineer, New Grad", True),
    ("Senior Software Engineer", False),
    ("Sr. Backend Engineer", False),
    ("Sr Backend Engineer", False),
    ("Staff Engineer", False),
    ("Principal Data Scientist", False),
    ("Tech Lead, Payments", False),
    ("Engineering Manager", False),
    ("Head of Platform", False),
    ("Director of Engineering", False),
    ("VP Engineering", False),
    ("Vice President, Technology", False),
    ("Solutions Architect", False),
    # Near misses: whole words only.
    ("Leadership Development Programme - Technology", True),
    ("Israel Graduate Programme", True),  # contains "sr" inside a word
    ("Staffing Engineer", True),
    ("Leading Edge Graduate Developer", True),
    ("Member of Technical Staff", True),
    # Non-engineering, unless an engineering word is there too.
    ("Account Executive", False),
    ("Sales Development Representative", False),
    ("Technical Recruiter", False),
    ("Marketing Intern", False),
    ("HR Business Partner", False),
    ("Legal Counsel", False),
    ("Customer Success Associate", False),
    ("Marketing Data Engineer", True),
    ("Sales Engineering Intern", True),
    ("Quantitative Researcher Intern", True),
])
def test_titles(title, passed):
    assert prefilter.check_title(title).passed is passed


def test_title_reason_names_the_word():
    assert prefilter.check_title("Senior Software Engineer").reason == "title: senior"
    assert prefilter.check_title("Recruiter").reason == "title: non-engineering (recruiter)"


@pytest.mark.parametrize("locations, passed", [
    (["Paris, France"], True),
    (["Bengaluru, Karnataka, India"], False),
    (["Bengaluru, India", "London, United Kingdom"], True),  # any allowed location passes
    (["Remote"], True),
    (["Remote - India"], False),
    (["Remote, US"], True),
    (["Remote (EMEA)"], True),
    (["APAC"], False),
    ([], True),
    ([""], True),
    (["Somewhere Unrecognised"], True),  # unknown goes to the AI
    (["Singapore", "Somewhere Unrecognised"], True),
    (["Toronto, CA"], False),
    (["San Jose, CA"], True),
    (["Berlin, DE"], True),
    (["Indianapolis, IN"], True),
    (["London, Ontario, Canada"], False),
    (["GBR-Edinburgh-5th Floor, Exchange Tower"], True),
    (["MYS-Penang-One Precinct, Malaysia"], False),
    (["Tel Aviv-Yafo, Israel"], False),
    (["Sydney, New South Wales"], False),
])
def test_locations(locations, passed):
    assert prefilter.check_locations(locations, ALLOWED).passed is passed


def test_location_reason_lists_countries():
    verdict = prefilter.check_locations(["Bengaluru, India", "Singapore"], ALLOWED)
    assert verdict.reason == "location: IN, SG"


def test_check_runs_title_before_location():
    assert prefilter.check("Senior Engineer", ["Singapore"], ALLOWED).reason == "title: senior"


@pytest.mark.parametrize("title", ["Software Engineer, New Grad", "Graduate Programme 2027", "SWE Intern",
                                   "Werkstudent Backend", "Junior Developer", "2027 Start: Analyst"])
def test_graduate_wording(title):
    assert prefilter.has_graduate_wording(title)


def test_no_graduate_wording():
    assert not prefilter.has_graduate_wording("Software Engineer, Payments")


def test_normalise_title_drops_bracketed_parts():
    assert prefilter.normalise_title("Graduate Programme (f/m/x) - Berlin") == "graduate programme berlin"
    assert prefilter.normalise_title("Research Intern (Mandarin)") == "research intern"


def test_similar_descriptions():
    a = ("Join our team in London building payment systems with Python and Go. You will design APIs, "
         "review code, run services in production, write tests, work with product managers, learn from "
         "senior engineers, and ship features used by millions of merchants across Europe every day. "
         "We offer mentoring, a learning budget, flexible hours and a relocation package.")
    b = a.replace("London", "Paris")
    assert prefilter.similar_descriptions(a, b)
    assert not prefilter.similar_descriptions(a, "A completely different role about mobile apps in Swift.")
    assert not prefilter.similar_descriptions(a, "")


def test_geo_parse_is_cached_and_consistent():
    assert geo.parse("Paris, France") is geo.parse("Paris, France")


@pytest.mark.parametrize("title, terms", [
    ("Software Engineer, New Grad", ["new grad"]),
    ("Graduate Software Engineer 2027", ["graduate", "2027"]),
    ("Software Engineer I", ["engineer I"]),
    ("Software Engineer II", []),
    ("Software Engineer in Test", []),
    ("Werkstudent / Praktikant Backend", ["praktikant", "werkstudent"]),
    ("Stage - Développeur Python", ["stage"]),
    ("Becario Desarrollo", ["becario"]),
    ("Stagista Software", ["stagista"]),
    ("Absolvent:in IT", ["absolvent"]),
    ("Associate Software Engineer", ["associate"]),
    ("Senior Backend Engineer", []),
])
def test_entry_terms(title, terms):
    assert prefilter.entry_terms(title) == terms


@pytest.mark.parametrize("locations, group", [
    (["Paris, France"], "EU/EEA/CH"), (["London, United Kingdom"], "UK"), (["New York, NY, US"], "US"),
    (["London, UK", "Berlin, Germany"], "EU/EEA/CH"), (["Remote"], "unknown"), ([], "unknown"),
    (["Remote, Europe"], "EU/EEA/CH"), (["Singapore"], "other"), (["Zurich"], "EU/EEA/CH"),
])
def test_country_group(locations, group):
    assert prefilter.country_group(locations, ALLOWED) == group


@pytest.mark.parametrize("locations, is_parked", [
    (["London, United Kingdom"], True), (["New York, NY, US", "London"], True), (["Remote, US"], True),
    (["London", "Amsterdam, Netherlands"], False),  # one location outside the UK and US: scored
    (["Remote"], False), (["Somewhere Unrecognised"], False), ([], False),
])
def test_parked(locations, is_parked):
    assert prefilter.parked(locations, {"GB", "US"}) is is_parked


@pytest.mark.parametrize("location, codes", [
    ("Batavia", {"US"}),  # a town of one country only (the GeoNames table)
    ("Tempe", {"US"}),
    ("Bodø", {"NO"}),
    ("Böblingen & Remote", {"DE"}),
    ("DE_Ulm", {"DE"}),
    ("Kempten (Allgäu)", {"DE"}),
    ("CA.ON.Mississauga.2251 Speakman Drive", {"CA"}),  # Workday's dotted codes
    ("Albany Office", set()),  # Albany is in the US, Australia and New Zealand: left to the AI
    ("Main Campus", set()),
])
def test_towns_without_a_country_are_placed_when_only_one_country_has_them(location, codes):
    assert geo.parse(location).codes == codes


def test_a_us_town_alone_parks_a_generated_role():
    rules = pipeline.GeneratedRules(park=frozenset({"US", "GB"}))
    assert rules.parks("Data Engineer", ["Batavia"]) and not rules.parks("Data Engineer", ["Batavia", "Bodø"])


def test_page_title_leftovers_are_stripped_but_teams_and_markers_stay():
    from jobhunt.prefilter import tidy_title
    assert tidy_title("AI Engineer with passion of AIOps | SEB, Stockholm", "SEB", ["Stockholm, Sweden"]) == \
        "AI Engineer with passion of AIOps"
    assert tidy_title("Data Engineer - Mollie", "Mollie", ["Amsterdam"]) == "Data Engineer"
    assert tidy_title("Graduate Engineer - Amsterdam", "Polderbyte", ["Amsterdam, Netherlands"]) == "Graduate Engineer"
    # A team, a common word that is also a town elsewhere, and a gender marker all stay.
    assert tidy_title("Staff Backend Engineer - Grafana Enterprise", "Grafana Labs", ["Canada"]) == \
        "Staff Backend Engineer - Grafana Enterprise"
    assert tidy_title("Acheteur Corps d'État - F/H", "Spie", ["Paris, France"]) == "Acheteur Corps d'État - F/H"
    assert tidy_title("Software Engineer - Backend", "Acme", ["Berlin"]) == "Software Engineer - Backend"


def test_a_leading_company_name_goes_only_when_it_is_exactly_the_name():
    from jobhunt.prefilter import tidy_title
    for title in ("SEB - Data Engineer", "SEB: Data Engineer", "SEB | Data Engineer"):
        assert tidy_title(title, "SEB", ["Stockholm, Sweden"]) == "Data Engineer"
    assert tidy_title("SEB Tech Trainee Programme 2027-28: Data Engineer | SEB, Stockholm", "SEB", ["Stockholm, Sweden"]) \
        == "SEB Tech Trainee Programme 2027-28: Data Engineer"
    assert tidy_title("Rolls-Royce: Graduate Engineer", "Rolls-Royce", ["Derby"]) == "Graduate Engineer"
    assert tidy_title("Backend: Payments", "SEB", []) == "Backend: Payments"
