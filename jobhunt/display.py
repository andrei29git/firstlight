"""How names, places, dates and numbers are shown, the same on every page (2026-10-29)."""

from __future__ import annotations

import functools
import html
import re
from datetime import date
from pathlib import Path
from typing import Iterable

from markupsafe import Markup
from urllib.parse import urlsplit

from . import geo

SHORT_COUNTRIES = {"united kingdom": "UK", "great britain": "UK", "united states": "US",
                   "united states of america": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US"}
SEPARATORS = " ,;·|-–—/"
COUNTRY_NAMES = {n for names in geo.COUNTRIES.values() for n in names.split("|")} | {"uk", "us"}


NAMES_FILE = Path(__file__).parent / "data" / "company_names.tsv"
_own_names: dict[str, str] = {}  # names you wrote (companies.toml, the wishlist), by key; see use_names
WORKDAY_WORDS = {"careers", "career", "site", "sites", "external", "externe", "extern", "jobs", "job", "search",
                 "page", "opportunities", "recruiting", "portal", "global", "ext", "en", "us"}


def name_key(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", name.lower())


@functools.cache
def known_names() -> dict[str, str]:
    """Curated display names for generated companies known only by a slug or tenant."""
    names = {}
    for line in NAMES_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "\t" in line:
            key, shown = line.split("\t", 1)
            names[name_key(key)] = shown.strip()
    return names


def use_names(names: Iterable[str]) -> None:
    """Names as you wrote them (companies.toml and the wishlist): shown exactly so everywhere, so
    "bunq" stays lowercase and a slug-like name you typed is not changed (2026-11-19)."""
    _own_names.clear()
    _own_names.update({name_key(n): n for n in names if n and not n.startswith(("http://", "https://"))})


def company_label(name: str, from_board: bool = False) -> str:
    """The one display name for a company, on every page (2026-11-19). In order: the name as you
    wrote it in companies.toml or the wishlist; a curated name for a known slug or tenant; for a
    company known only by its Workday URL, the site's own words when they spell the tenant
    ("https://fticonsulting.wd108.myworkdayjobs.com/FTIConsultingCareers" -> "FTI Consulting"),
    or else the tenant ("Adobe"); an all-lowercase slug made readable ("almedia" -> "Almedia");
    any other name as it is. The stored name, part of each role's key, never changes.
    `from_board` is kept for callers; every name follows the same rule now."""
    if not name:
        return name
    key = name_key(name)
    if key in _own_names:
        return _own_names[key]
    if not name.startswith(("http://", "https://")):
        if key in known_names():
            return known_names()[key]
        return tidy_slug(name) if name == name.lower() and " " not in name.strip() else name
    parts = urlsplit(name)
    host = parts.netloc.lower()
    labels = host.split(".")
    tenant = labels[0] if "myworkdayjobs" in host or "workday" in host else (labels[-2] if len(labels) > 1 else host)
    tenant = re.sub(r"[-_]?\d+$", "", tenant)
    if name_key(tenant) in known_names():
        return known_names()[name_key(tenant)]
    site = parts.path.strip("/").split("/")[-1] if parts.path.strip("/") else ""
    words = [w for w in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", site)
             if w.lower() not in WORKDAY_WORDS]
    spelled = name_key("".join(words))
    if words and len(spelled) >= 4 and name_key(tenant).startswith(spelled) and len(name_key(tenant)) - len(spelled) <= 3 \
            and any(c.isupper() for c in site):
        return " ".join(words)
    words = [w for w in re.split(r"[-_]+", tenant) if w]
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in words) or name


def tidy_slug(name: str) -> str:
    """A company name that is only a board slug, made readable for display: a numeric suffix
    dropped ("wiremind-1714146283" -> "Wiremind"), short all-lowercase names in capitals ("seb" ->
    "SEB"), others capitalised ("ateme" -> "Ateme"). A name with capitals or spaces is a proper
    name from the board and stays as it is."""
    trimmed = re.sub(r"[-_]\d{4,}$", "", name.strip()) or name
    if trimmed != trimmed.lower() or " " in trimmed:
        return trimmed
    words = [w for w in re.split(r"[-_]+", trimmed) if w]
    return " ".join(w.upper() if len(w) <= 3 and w.isalpha() else w[:1].upper() + w[1:] for w in words) or name


def capitalised(name: str) -> str:
    """An all-lowercase name with a capital first letter; any other name as it is."""
    return name[:1].upper() + name[1:] if name and name == name.lower() and name[:1].isalpha() else name


def company_name_typed(name: str) -> str:
    """A company name as typed into a search: all lowercase becomes Title Case ("acme robotics" to
    "Acme Robotics"); a name with any capital ("bol.com" aside) is kept as written."""
    name = " ".join(name.split())
    return " ".join(w[:1].upper() + w[1:] for w in name.split(" ")) if name == name.lower() and "." not in name else name


# English names for cities boards write in their own language or without diacritics.
CITY_NAMES = {
    "iasi": "Iași", "iași": "Iași", "bucuresti": "Bucharest", "bucurești": "Bucharest", "bucureşti": "Bucharest",
    "cluj": "Cluj-Napoca", "cluj napoca": "Cluj-Napoca", "timisoara": "Timișoara", "timișoara": "Timișoara",
    "brasov": "Brașov", "brașov": "Brașov", "constanta": "Constanța", "galati": "Galați", "targu mures": "Târgu Mureș",
    "münchen": "Munich", "munchen": "Munich", "köln": "Cologne", "nürnberg": "Nuremberg", "frankfurt am main": "Frankfurt",
    "den haag": "The Hague", "'s-gravenhage": "The Hague", "s-gravenhage": "The Hague", "'s-hertogenbosch": "Den Bosch",
    "milano": "Milan", "roma": "Rome", "torino": "Turin", "napoli": "Naples", "firenze": "Florence", "genova": "Genoa",
    "warszawa": "Warsaw", "kraków": "Krakow", "wrocław": "Wroclaw", "praha": "Prague", "brno": "Brno", "wien": "Vienna",
    "lisboa": "Lisbon", "bruxelles": "Brussels", "brussel": "Brussels", "genève": "Geneva", "zürich": "Zurich",
    "københavn": "Copenhagen", "göteborg": "Gothenburg", "athina": "Athens", "sevilla": "Seville",
}
CODE_TOKEN = re.compile(r"^[A-Z]{2,3}\b\s*")  # "NLD Amsterdam", "PL - Warsaw"
COUNTY = re.compile(r"^(?:county|province|provincia|judetul|județul|state of)\b", re.IGNORECASE)


def country_name(code: str) -> str | None:
    """"RO" -> "Romania", "GB" -> "United Kingdom": the English name first in geo.COUNTRIES."""
    names = geo.COUNTRIES.get(code)
    if not names:
        return None
    first = names.split("|")[0]
    return {"czech republic": "Czechia"}.get(first, " ".join(w.capitalize() for w in first.split()))


def _is_country(part: str) -> str | None:
    """The code when the part is a country by name or code ("Romania", "ROU", "NL"), else None."""
    low = part.strip().lower()
    for code, names in geo.COUNTRIES.items():
        if low in names.split("|"):
            return code
    up = part.strip().upper()
    if up in geo.ALPHA3:
        return geo.ALPHA3[up]
    return up if len(up) == 2 and up in geo.COUNTRIES else None


def place_label(text: str) -> str:
    """One place as "City, Country" in English (2026-11-18): "Iasi, ROU" -> "Iași, Romania",
    "München, BY, Germany" -> "Munich, Germany", "PL - Warsaw, Poland" -> "Warsaw, Poland"; only the
    country when no city is named ("Netherlands"). A place it cannot read is shown as cleaned."""
    place = geo.parse(text or "")
    if place.wildcard or not place.codes:
        return clean_location(text)
    parts = [p for p in (q.strip() for q in geo.PART_SPLIT.split(text)) if p]
    named = [c for c in (_is_country(p) for p in parts if len(p.strip()) > 3) if c]  # a country written out
    codes = [c for c in place.codes if c in geo.COUNTRIES]
    country = named[0] if named else (codes[0] if len(codes) == 1 else None)
    if country is None:
        return clean_location(text)
    city = None
    for part in parts:
        candidate = " ".join(geo.PLACE_NOISE.sub(" ", CODE_TOKEN.sub("", part)).split())
        if len(candidate) < 3 or _is_country(candidate) or COUNTY.match(candidate) or candidate.isupper() and len(candidate) <= 3:
            continue
        known = geo.towns().get(candidate.lower())
        if known in (None, country) and candidate.lower() not in geo.US_STATES:
            city = CITY_NAMES.get(candidate.lower(), capitalised(candidate))
            break
    name = country_name(country) or country
    if city:
        return f"{city}, {name}"
    return f"Remote, {name}" if re.search(r"\bremote\b", text, re.IGNORECASE) else name


def clean_location(text: str) -> str:
    """"Vienna, Vienna, Austria" -> "Vienna, Austria"; "London, United Kingdom, GB" -> "London, UK";
    trailing separators gone."""
    parts = [p.strip() for p in re.split(r"\s*,\s*", (text or "").strip().strip(SEPARATORS)) if p.strip(SEPARATORS)]
    out: list[str] = []
    for part in parts:
        part = SHORT_COUNTRIES.get(part.lower(), part)
        if out and part.lower() == out[-1].lower():
            continue  # "Vienna, Vienna"
        if out and part in geo.COUNTRIES and out[-1].lower() in COUNTRY_NAMES \
                and geo.parse(out[-1]).codes == {part}:
            continue  # a code repeating the country named before it: "UK, GB", "Germany, DE"
        out.append(part)
    return ", ".join(out)


def clean_locations(locations: list[str]) -> list[str]:
    return list(dict.fromkeys(c for c in (place_label(x) for x in locations or []) if c))


LIST_START = re.compile(r"^\s*(?:[-•*·▪●◦–]|\d+[.)]\s|[a-z][.)]\s)")


def reflow(text: str) -> str:
    """Posting text whose source broke lines mid-sentence, joined again (2026-11-18): a line
    continues the one before when that line is long and does not end a sentence, or when the line
    starts in lower case. Blank lines, list items and short lines (headings) stay as they are."""
    out = []
    for para in re.split(r"\n[ \t]*\n", text or ""):
        merged: list[str] = []
        for line in para.split("\n"):
            line = line.rstrip()
            prev = merged[-1] if merged else ""
            starts_lower = line.lstrip()[:1].islower()
            if prev.strip() and line.strip() and not LIST_START.match(line) and len(prev) >= 40 \
                    and (starts_lower or not prev.endswith((".", ":", "!", "?", ";"))):
                merged[-1] = prev + " " + line.strip()
            else:
                merged.append(line)
        out.append("\n".join(merged))
    return "\n\n".join(out)


BULLET = re.compile(r"^\s*(?:[-*•·▪◦–]|\d{1,2}[.)])\s+")
NUMBERED = re.compile(r"^\s*\d{1,2}[.)]\s+")


def _inline(text: str) -> str:
    """Escaped text with **bold** as <strong> (all a reply or posting needs inline)."""
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"<strong>\1</strong>", escaped)


def _is_heading(line: str, following: str | None) -> bool:
    """A short line that introduces what follows: ends with ":" ("What you will do:"), or has no
    closing punctuation and is followed by a list ("Your work" before "- Python …")."""
    text = line.strip()
    if not text or BULLET.match(text) or len(text) > 70 or len(text.split()) > 8:
        return False
    if text.endswith(":"):
        return True
    return not text.endswith((".", ",", ";", "!", "?")) and following is not None and bool(BULLET.match(following))


def rich_text(text: str) -> Markup:
    """Plain text as simple, safe HTML (2026-11-19): paragraphs, lists from "-", "*" or "•" lines (or
    "1." lines), headings from short introducing lines, **bold**; runs of blank lines collapse to
    one. Everything is escaped first, so a posting or a reply cannot add markup of its own."""
    lines = [line.rstrip() for line in reflow(text or "").replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    ordered = False

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        if items:
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{tag}>")
            items.clear()

    for i, line in enumerate(lines):
        following = next((n for n in lines[i + 1:] if n.strip()), None)
        if not line.strip():
            flush_para()
            flush_list()
            continue
        if BULLET.match(line):
            flush_para()
            if not items:
                ordered = bool(NUMBERED.match(line))
            items.append(BULLET.sub("", line, count=1).strip())
            continue
        if items and line.startswith((" ", "\t")):  # a list item's own second line
            items[-1] += " " + line.strip()
            continue
        flush_list()
        if _is_heading(line, following):
            flush_para()
            out.append(f'<h3 class="text-heading">{_inline(line.strip().rstrip(":"))}</h3>')
            continue
        para.append(line.strip())
    flush_para()
    flush_list()
    return Markup("".join(out))


def short_places(locations: list[str]) -> str:
    """"Dublin, Ireland +3": the first place and how many more (the full list goes on hover)."""
    places = clean_locations(locations)
    if not places:
        return ""
    return places[0] + (f" +{len(places) - 1}" if len(places) > 1 else "")


def day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def nice_date(value: str | None, today: date | None = None) -> str:
    """One date format everywhere: "7 Oct" this year, "Jun 2025" otherwise."""
    d = day(value)
    if d is None:
        return ""
    return f"{d.day} {d:%b}" if d.year == (today or date.today()).year else f"{d:%b} {d.year}"


def recent_date(value: str | None, today: date | None = None) -> str:
    """Past dates in tables: "Today", "Yesterday", "3 days ago" within a week, else nice_date."""
    d = day(value)
    if d is None:
        return ""
    days = ((today or date.today()) - d).days
    if 0 <= days <= 6:
        return "Today" if days == 0 else "Yesterday" if days == 1 else f"{days} days ago"
    return nice_date(value, today)


def is_old(value: str | None, today: date | None = None, days: int = 183) -> bool:
    """A posting date more than about six months back (shown in grey)."""
    d = day(value)
    return d is not None and ((today or date.today()) - d).days > days


def due_soon(value: str | None, today: date | None = None, days: int = 7) -> bool:
    d = day(value)
    return d is not None and 0 <= (d - (today or date.today())).days <= days


def long_date(value: str | None) -> str:
    """"7 Oct 2026": a date with its year, for date fields."""
    d = day(value)
    return f"{d.day} {d:%b %Y}" if d else ""


def is_past(value: str | None, today: date | None = None) -> bool:
    """A date before today (a deadline that has passed)."""
    d = day(value)
    return d is not None and d < (today or date.today())


def start_date_text(value: str | None) -> str:
    """A start date as the model gave it, in the app's date format: "2027-10-01" -> "1 Oct 2027",
    "2027-10" -> "Oct 2027"; words ("September 2027", "flexible") as they are."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return long_date(text)
    if re.fullmatch(r"\d{4}-\d{2}", text):
        d = day(text + "-01")
        return f"{d:%b %Y}" if d else text
    return text


def due_label(value: str | None, today: date | None = None) -> str:
    """A deadline as a short label, the same everywhere (2026-11-19): "Due today", "Due tomorrow",
    "Due 24 Nov", or "Deadline passed"."""
    d = day(value)
    if d is None:
        return ""
    days = (d - (today or date.today())).days
    if days < 0:
        return "Deadline passed"
    return "Due today" if days == 0 else "Due tomorrow" if days == 1 else f"Due {nice_date(value, today)}"


def due_tone(value: str | None, today: date | None = None, soon_days: int = 7) -> str:
    """"past" (red), "soon" (amber, today included) or "" for a deadline."""
    return "past" if is_past(value, today) else "soon" if due_soon(value, today, soon_days) else ""


def number(value: int | float | None) -> str:
    """One number format: 1,783."""
    if value is None:
        return ""
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"


def places_text(text: str) -> str:
    """A free-text location (an application's), each place as "City, Country"."""
    return "; ".join(place_label(p) for p in re.split(r"\s*;\s*", text or "") if p.strip())


def angle_text(text: str) -> str:
    """The score's angle without a leading "Lead with", which the page already says (2026-11-18)."""
    rest = re.sub(r"^\s*lead with\b[:\s]*", "", text or "", flags=re.IGNORECASE)
    return rest[:1].upper() + rest[1:] if rest else text


def run_kind(kind: str | None) -> str:
    """A run's kind in a word or two for the Runs table; the full text is in its sheet (2026-11-18)."""
    text = (kind or "").strip()
    if not text or text == "scheduled":
        return "Scheduled"
    if "(local sites)" in text:
        return "Laptop sites"
    if text.startswith("scoring only"):
        return "Scoring only"
    if text.startswith("run, generated list: due:"):
        names = text.split(":", 2)[2].strip().split(", ")
        return f"Named: {names[0]}" + (f" +{len(names) - 1}" if len(names) > 1 else "")
    return text[:1].upper() + text[1:30] + ("…" if len(text) > 30 else "")


def sentence(text: str) -> str:
    """A message as one plain sentence: tool prefixes like "claude exited 1: " dropped, a capital
    first letter and exactly one full stop ("…switched off (the scores…).." read badly)."""
    import re
    text = re.sub(r"\bclaude exited \d+:\s*", "", " ".join(str(text).split()))
    text = re.sub(r"(?<=: )Demo mode: ", "", text)
    text = text.rstrip(" .")
    return (text[:1].upper() + text[1:] + ".") if text else ""


# Words that keep their capital in a gap ("Kafka Streams", "German at B2"): technologies, companies
# and languages that read as names. Acronyms and camel-case names (SQL, TypeScript) keep theirs anyway.
PROPER_WORDS = {
    "airflow", "ansible", "angular", "argo", "aws", "azure", "c", "c#", "c++", "databricks", "django", "docker",
    "dutch", "elasticsearch", "english", "excel", "fastapi", "flask", "french", "gcp", "german", "git", "github",
    "gitlab", "go", "golang", "google", "grafana", "hadoop", "helm", "italian", "java", "javascript", "jenkins",
    "jira", "kafka", "kotlin", "kubernetes", "linux", "microsoft", "node", "node.js", "oracle", "polish",
    "postgres", "postgresql", "prometheus", "python", "rails", "react", "redis", "romanian", "ruby", "rust",
    "salesforce", "scala", "snowflake", "solr", "spanish", "spark", "spring", "swedish", "swift", "tableau",
    "terraform", "vue", "windows",
}


def gap_text(gap: str) -> str:
    """A gap as it reads in a sentence (2026-11-19): "No production cloud experience" becomes "no
    production cloud experience", but "SQL", "Kafka", "TypeScript" or "German at B2" keep their capital."""
    gap = gap.strip()
    words = gap.split()
    if not words:
        return gap
    first = words[0]
    keep = ((len(words) == 1 and "-" not in first)  # a single word is nearly always a name: Solr, Oracle
            or first.strip("(),.:;").lower() in PROPER_WORDS
            or any(c.isupper() for c in first[1:]) or any(c.isdigit() for c in first)
            or not first[:1].isupper())
    return gap if keep else first[:1].lower() + gap[1:]
