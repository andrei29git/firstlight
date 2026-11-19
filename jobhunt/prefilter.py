"""Cheap rules that remove only obvious noise before AI scoring.

Everything that needs judgement (experience, language, timing, visa, grades, enrolment) is left
to the AI. A rule here must be unambiguous: when in doubt, a job passes.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Iterable

from . import geo

SENIOR = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|leads?|managers?|head\s+of|directors?|vice\s+president|vp|architects?)\b",
    re.IGNORECASE)
# Titles that contain a seniority word but are not senior.
NOT_SENIOR = re.compile(r"\bmember\s+of\s+(?:the\s+)?technical\s+staff\b", re.IGNORECASE)

NON_ENGINEERING = re.compile(
    r"\b(?:sales|account\s+(?:executive|manager|management|director)|marketing|recruit(?:er|ers|ing|ment)|"
    r"talent\s+acquisition|hr|human\s+resources|people\s+partner|legal|counsel|paralegal|lawyer|"
    r"financ(?:e|ial)\s+analyst|customer\s+(?:support|service|success)|receptionist)\b", re.IGNORECASE)
# Words that make a title engineering even next to a non-engineering word ("Marketing Data Engineer").
# Deliberately not "technical" or "IT", so "Technical Recruiter" is still dropped.
ENGINEERING = re.compile(
    r"\b(?:engineer(?:ing)?|developer|software|swe|sde|programmer|data|scientist|machine\s+learning|ml|ai|"
    r"devops|sre|platform|backend|back-end|frontend|front-end|full[\s-]?stack|quant(?:itative)?|"
    r"security|cloud|infrastructure)\b", re.IGNORECASE)

GRADUATE_WORDING = re.compile(
    r"\b(?:graduate|grad|new\s+grad|intern(?:ship)?|working\s+student|werkstudent|stagiaire|stage|"
    r"trainee|apprentice(?:ship)?|junior|jr\.?|entry[\s-]level|early\s+careers?|student|campus|"
    r"20\d\d\s+start|class\s+of\s+20\d\d)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str | None = None  # why it was dropped, e.g. "title: senior", "location: IN, SG"

    @property
    def category(self) -> str | None:
        return self.reason.split(":", 1)[0] if self.reason else None


PASS = Verdict(True)


# Pages some career sites publish among their jobs that are not openings (ING: "Meet a Trainee",
# "ING's Early Career Event"). Phrases, not single words: "Events and Issues Management Officer"
# is a job.
NOT_A_JOB = re.compile(
    r"\b(?:meet\s+(?:a|an|our|the)\b|(?:career|recruitment|recruiting|hiring)\s+events?\b|open\s+(?:day|house)s?\b|"
    r"webinars?\b|info(?:rmation)?\s+(?:session|evening|day)s?\b|talent\s+(?:community|pool|network)\b|"
    r"(?:open|general|speculative|unsolicited)\s+applications?\b|career\s+fairs?\b)", re.IGNORECASE)


def check_title(title: str) -> Verdict:
    not_a_job = NOT_A_JOB.search(title)
    if not_a_job:
        return Verdict(False, f"title: not a job opening ({not_a_job.group(0).lower()})")
    senior = SENIOR.search(NOT_SENIOR.sub(" ", title))
    if senior:
        return Verdict(False, f"title: {senior.group(0).lower()}")
    non_engineering = NON_ENGINEERING.search(title)
    if non_engineering and not ENGINEERING.search(title):
        return Verdict(False, f"title: non-engineering ({non_engineering.group(0).lower()})")
    return PASS


def check_locations(locations: Iterable[str], allowed_codes: set[str]) -> Verdict:
    """Passes if any location is allowed or unrecognised, or there are none."""
    locations = [loc for loc in locations if loc and loc.strip()]
    if not locations:
        return PASS
    named: set[str] = set()
    for location in locations:
        verdict = geo.allowed(location, allowed_codes)
        if verdict is not False:
            return PASS
        named |= geo.parse(location).codes
    shown = sorted(c for c in named if "-" not in c) or ["other region"]
    return Verdict(False, f"location: {', '.join(shown)}")


def check(title: str, locations: Iterable[str], allowed_codes: set[str]) -> Verdict:
    verdict = check_title(title)
    return verdict if not verdict.passed else check_locations(locations, allowed_codes)


def has_graduate_wording(title: str) -> bool:
    return bool(GRADUATE_WORDING.search(title))


# Entry-level terms for the generated list's graduate-wording filter (proposed, measured per term).
# Local terms: stage (FR), stagiair (NL), praktikum/praktikant (DE), absolvent (DE, CZ, PL),
# berufseinsteiger (DE), becario (ES), stagista (IT).
ENTRY_TERMS: dict[str, re.Pattern[str]] = {term: re.compile(pattern, re.IGNORECASE) for term, pattern in {
    "graduate": r"\bgraduates?\b", "junior": r"\b(?:junior|jr\.?)\b", "intern": r"\binterns?\b",
    "internship": r"\binternships?\b", "new grad": r"\bnew[\s-]+grad", "associate": r"\bassociates?\b",
    "entry level": r"\bentry[\s-]+level\b", "early career": r"\bearly[\s-]+careers?\b",
    "engineer I": r"\b(?:engineer|developer)\s+(?:I|1)\b(?![\w.-])", "trainee": r"\btrainees?(?:ship)?\b",
    "apprentice": r"\bapprentice(?:ship)?s?\b", "university": r"\buniversity\b", "campus": r"\bcampus\b",
    "2027": r"\b2027\b", "stage": r"\bstages?\b", "stagiair": r"\bstagiai?re?s?\b",
    "praktikum": r"\bpraktikum\b", "praktikant": r"\bpraktikant(?:in)?\b", "absolvent": r"\babsolvent(?:in)?\b",
    "berufseinsteiger": r"\bberufseinsteiger(?:in)?\b", "becario": r"\bbecari[oa]s?\b",
    "stagista": r"\bstagist[ai]\b",
    # Local words for the main countries (2026-10-29): Dutch, German, French and Italian.
    "afstudeerstage": r"\bafstudeerstages?\b", "afstudeeropdracht": r"\bafstudeeropdracht(?:en)?\b",
    "starter": r"\bstarters?\b", "werkstudent": r"\bwerkstudent(?:in|en|innen)?\b",
    "einsteiger": r"\beinsteiger(?:in|innen)?\b", "alternance": r"\baltern(?:ance|ant|ante|ants|antes)\b",
    "tirocinio": r"\btirocin(?:io|i|ante|anti)\b",
}.items()}


def entry_terms(title: str) -> list[str]:
    """The entry-level terms a title contains (empty: no graduate wording)."""
    return [term for term, pattern in ENTRY_TERMS.items() if pattern.search(title)]


EU_GROUP, UK_GROUP, US_GROUP, UNKNOWN_GROUP, OTHER_GROUP = "EU/EEA/CH", "UK", "US", "unknown", "other"


def country_group(locations: Iterable[str], allowed_codes: set[str]) -> str:
    """Where a role is, for reporting: EU/EEA/CH if any location is (or Europe), else UK, else US,
    else unknown (no location, unrecognised, or unqualified remote), else other."""
    codes: set[str] = set()
    unknown = False
    for location in locations:
        place = geo.parse(location) if location and location.strip() else None
        if place is None or not place.codes:
            unknown = True
            continue
        codes |= place.codes
    if "EU-REGION" in codes or codes & (allowed_codes - {"GB", "US"}):
        return EU_GROUP
    if "GB" in codes:
        return UK_GROUP
    if "US" in codes:
        return US_GROUP
    return UNKNOWN_GROUP if unknown or not codes else OTHER_GROUP


def parked(locations: Iterable[str], park_codes: set[str]) -> bool:
    """Parked (stored, not scored) when every location names only parked countries. A role with any
    other location, an unrecognised one, unqualified "Remote", or none at all is scored."""
    locations = [loc for loc in locations if loc and loc.strip()]
    if not locations or not park_codes:
        return False
    for location in locations:
        place = geo.parse(location)
        if place.wildcard or not place.codes or not place.codes <= park_codes:
            return False
    return True


# --- Duplicates -------------------------------------------------------------------------------

def normalise_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)  # "(f/m/x)", "(London)"
    title = re.sub(r"[^\w]+", " ", title)
    return " ".join(title.split())


TITLE_PLACE_SUFFIX = re.compile(r"\s+(?:-|–|—|\||/|@)\s+([^-–—|/@]+)$")


def base_title(title: str) -> str:
    """The title without a trailing place: "Software Engineer, AI Data - Chicago, IL, USA" becomes
    "Software Engineer, AI Data". Boards that post one role per city often put the city there."""
    while (match := TITLE_PLACE_SUFFIX.search(title)) and geo.parse(match.group(1)).recognised:
        title = title[:match.start()]
    return title


@functools.lru_cache(maxsize=65536)  # called for every role in every selection: a few thousand names
def normalise_company(name: str) -> str:
    name = re.sub(r"[^\w]+", " ", name.lower())
    name = re.sub(r"\b(?:inc|ltd|llc|gmbh|bv|b v|ag|sa|plc|group|limited|corp|corporation)\b", " ", name)
    return " ".join(name.split())


def similar_descriptions(a: str, b: str, threshold: float = 0.85) -> bool:
    """Same posting text apart from small edits (usually the city name)."""
    if not a or not b:
        return False
    wa, wb = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return len(wa & wb) / max(1, len(wa | wb)) >= threshold


# Page-title leftovers after a role's title (2026-11-19): "Solution Developer (Java) | SEB, Vilnius",
# "Home Health Aide | Middlesex County NJ | BAYADA Home Health Care". A trailing part goes only when it
# is nothing but the company's name and places; "Backend Engineer - Grafana Enterprise" keeps its team.
TITLE_SEPARATOR = re.compile(r"\s+(?:\||–|—|-)\s+")
PLACE_WORDS = {"remote", "hybrid", "onsite", "on site", "on-site", "home based", "home-based"}


def _is_place(chunk: str, where: str, codes: set[str]) -> bool:
    """A place the role itself is in: named in its locations, or a place in one of their countries
    (a common word that is also a town somewhere, like "Enterprise", does not count)."""
    chunk = chunk.strip().strip(".").lower()
    if chunk in PLACE_WORDS:
        return True
    if len(chunk.replace(" ", "")) < 3 or len(chunk.split()) > 3:
        return False  # "F/H", "m/w/d": gender markers, not places
    if chunk in where:
        return True
    return bool(geo.parse(chunk).codes & codes)


def _is_leftover(part: str, company: str, where: str, codes: set[str]) -> bool:
    company_norm = normalise_company(company)
    if company_norm and normalise_company(part) == company_norm:
        return True
    company_words = company_norm.split()
    rest = []
    for chunk in (c for c in re.split(r"[,/()]+", part) if c.strip()):
        words = [w for w in normalise_company(chunk).split() if w not in company_words]
        if words:
            rest.append(" ".join(words))
    return all(_is_place(r, where, codes) for r in rest)


LEADING_COMPANY = re.compile(r"^(?P<name>[^:|–—]{1,60}?)\s*(?::|\s[|–—-])\s+")


def tidy_title(title: str, company: str, locations: Iterable[str] = ()) -> str:
    """The title without trailing parts that only repeat the company or the role's places, and
    without a leading "Company - ", "Company: " or "Company | " (2026-11-19: exactly the company's
    name; "SEB Tech Trainee Programme" keeps its name)."""
    lead = LEADING_COMPANY.match(title)
    if lead and normalise_company(company) and normalise_company(lead["name"]) == normalise_company(company) \
            and len(title[lead.end():].strip()) >= 3:
        title = title[lead.end():]
    locations = list(locations)
    where = " | ".join(locations).lower()
    codes = set().union(*(geo.parse(loc).codes for loc in locations)) if locations else set()
    while True:
        matches = list(TITLE_SEPARATOR.finditer(title))
        if not matches:
            return title
        last = matches[-1]
        head, part = title[:last.start()], title[last.end():]
        if len(head.strip()) < 3 or not _is_leftover(part, company, where, codes):
            return title
        title = head.rstrip()
