"""Bending Spoons: `GET https://jobs.bendingspoons.com/` (2026-10-29). Not JavaScript-only after
all: the page's `__NEXT_DATA__` holds every open role (title, offices with their country, the
description, responsibilities and requirements, whether it suits students and new graduates).
robots.txt sets no rules. A role's page is /positions/{id}.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..companies import Company
from .base import Job, SourceError, WantDetails, all_details, html_to_text, unique
from .http import Http

BASE = "https://jobs.bendingspoons.com"


def fetch(company: Company, http: Http, details: WantDetails = all_details) -> list[Job]:
    if not http.allowed_by_robots(BASE + "/"):
        raise SourceError(f"robots.txt disallows {BASE}/")
    return parse(company.name, http.get(BASE + "/").text)


def roles(markup: str) -> list[dict[str, Any]]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', markup, re.DOTALL)
    if not match:
        raise SourceError("Bending Spoons: no __NEXT_DATA__ on the jobs page")
    found = json.loads(match.group(1)).get("props", {}).get("pageProps", {}).get("list")
    if not isinstance(found, list):
        raise SourceError("Bending Spoons: the jobs page's data has no list of roles")
    return found


def place(office: dict[str, Any]) -> str:
    """"Milan (Italy)" -> "Milan, Italy"."""
    title = str(office.get("title") or "")
    return re.sub(r"\s*\(([^)]+)\)\s*$", r", \1", title).strip() or str((office.get("country") or {}).get("name") or "")


def _text(value: Any) -> str:
    """A field as text: HTML, or a list of HTML snippets (one per point)."""
    if isinstance(value, list):
        return "\n".join(t for t in (_text(v) for v in value) if t)
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("description") or value.get("title"))
    return html_to_text(value) if isinstance(value, str) else ""


def parse(company: str, markup: str) -> list[Job]:
    jobs = []
    for raw in roles(markup):
        if raw.get("status") not in (None, "active") or raw.get("isEvent"):
            continue
        places = unique([place(o) for o in raw.get("officeLocations") or []])
        if raw.get("availableAsRemoteInDefaultCountries"):
            places.append("Remote")
        text = "\n\n".join(p for p in (_text(raw.get(k)) for k in (
            "highLevelDescription", "responsibilities", "requirements", "sellingPoints")) if p)
        jobs.append(Job(company=company, title=str(raw.get("jobTitle") or "").strip(), url=f"{BASE}/positions/{raw['id']}",
                        source="bendingspoons", source_id=str(raw["id"]), locations=places, description=text,
                        employment_type=", ".join(raw.get("typesOfContract") or []) or None))
    return jobs
