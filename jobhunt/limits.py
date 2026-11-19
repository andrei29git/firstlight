"""The limits registry: companies' rules on how often one may apply.

`limits.toml` holds the rules; the tracker database holds the applications (seeded ones too)
with dates and stage history. For a company, `standing` says where each rule stands now (slots
used, the next date a slot opens, a cooldown running); for a role, `warnings` says what applying
would do: use a limited slot, or break a cooldown.

How the rules are read:
- `max` + `window_days`: sent applications in the last N days; a slot opens when the oldest
  of them leaves the window.
- `max` + `window` "academic_year" or "recruiting_year": per September-to-August year.
- `window` "season" (no max): one application per recruiting year; the next opens in September.
- `max` alone: every application ever sent counts.
- `cooldown_days`: from the last application, or with `trigger = "assessment_or_interview"`
  from the last day an application was at OA done or Interviewing. An application's own
  `cooldown_until` (a date the user knows) wins.
- scope "role": only applications to the same role title count.
- A rule with none of these (Google's unquantified one) is shown as its note only.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import prefilter, stages

FILE = "limits.toml"
TRIGGER_STAGES = {"oa_done", "interviewing"}


def long_date(day) -> str:
    """"21 Sep 2027": the app's date format with the year (2026-11-18; was 2027-09-21)."""
    return f"{day.day} {day:%b %Y}"



@dataclass(frozen=True)
class Limit:
    company: str
    scope: str = "company"
    max: int | None = None
    window_days: int | None = None
    window: str | None = None
    cooldown_days: int | None = None
    trigger: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Application:
    company: str
    role: str
    applied_on: date | None
    stage: str
    history: tuple[tuple[str, date], ...] = ()  # (stage, day), oldest first
    cooldown_until: date | None = None


@dataclass
class Standing:
    limit: Limit
    used: int | None = None  # applications counted against `max`
    next_allowed: date | None = None  # a slot opens or the cooldown ends
    full: bool = False  # no slot left (now)
    cooling: bool = False  # a cooldown runs (now)
    explain: str = ""
    roles: list[str] = field(default_factory=list)  # scope "role": the roles the cooldown covers


def load(config: Path) -> list[Limit]:
    path = config / FILE
    if not path.exists():
        return []
    with path.open("rb") as f:
        return [Limit(**{k: v for k, v in raw.items() if k in Limit.__dataclass_fields__})
                for raw in tomllib.load(f).get("limit", [])]


def by_company(limits: list[Limit]) -> dict[str, list[Limit]]:
    found: dict[str, list[Limit]] = {}
    for limit in limits:
        found.setdefault(prefilter.normalise_company(limit.company), []).append(limit)
    return found


def recruiting_year(day: date) -> tuple[date, date]:
    """The September-to-August year a day falls in."""
    start = date(day.year if day.month >= 9 else day.year - 1, 9, 1)
    return start, date(start.year + 1, 9, 1)


def _sent(apps: list[Application]) -> list[Application]:
    return [a for a in apps if a.applied_on and a.stage in stages.SENT]


def _trigger_day(app: Application, trigger: str | None) -> date | None:
    if trigger == "assessment_or_interview":
        days = [d for stage, d in app.history if stage in TRIGGER_STAGES]
        return max(days) if days else None
    return app.applied_on


def standing(limit: Limit, apps: list[Application], today: date) -> Standing:
    found = Standing(limit=limit)
    sent = _sent(apps)
    if limit.cooldown_days is not None or any(a.cooldown_until for a in apps):
        ends: list[tuple[date, str]] = []
        for app in apps:
            if app.cooldown_until:
                ends.append((app.cooldown_until, app.role))
            elif limit.cooldown_days is not None and app.stage in stages.SENT:
                start = _trigger_day(app, limit.trigger)
                if start:
                    ends.append((start + timedelta(days=limit.cooldown_days), app.role))
        running = [(end, role) for end, role in ends if end > today]
        if running:
            found.cooling = limit.scope != "role"  # a role cooldown only matters for that role
            found.next_allowed = max(end for end, _ in running)
            found.roles = sorted({role for _, role in running})
            what = "after an assessment or interview" if limit.trigger == "assessment_or_interview" else "after applying"
            found.explain = (f"Cooldown {what} until {long_date(found.next_allowed)}"
                             + (f" for {', '.join(found.roles)}" if limit.scope == "role" else ""))
        return found
    if limit.max is not None or limit.window == "season":
        maximum = limit.max if limit.max is not None else 1
        if limit.window_days:
            since = today - timedelta(days=limit.window_days)
            counted = sorted(a.applied_on for a in sent if a.applied_on > since)
            found.used = len(counted)
            if found.used >= maximum:
                found.full = True
                found.next_allowed = counted[found.used - maximum] + timedelta(days=limit.window_days)
            found.explain = f"{found.used} of {maximum} in the last {limit.window_days} days"
        elif limit.window in ("academic_year", "recruiting_year", "season"):
            start, end = recruiting_year(today)
            found.used = sum(1 for a in sent if start <= a.applied_on < end)
            if found.used >= maximum:
                found.full, found.next_allowed = True, end
            found.explain = f"{found.used} of {maximum} this recruiting year (September to August)"
        else:
            found.used = len(sent)
            found.full = found.used >= maximum
            found.explain = f"{found.used} of {maximum} ever" + (": none left" if found.full else "")
        if found.full and found.next_allowed:
            found.explain += f"; next slot {found.next_allowed.isoformat()}"
        return found
    found.explain = limit.note or "a limit without numbers"
    return found


def standings(limits: list[Limit], apps: list[Application], today: date) -> list[Standing]:
    return [standing(limit, apps, today) for limit in limits]


def warnings(limits: list[Limit], apps: list[Application], role: str, today: date) -> list[str]:
    """What applying to `role` at this company would do, in plain words (empty: nothing limited)."""
    found = []
    wanted = prefilter.normalise_title(role)
    for state in standings(limits, apps, today):
        limit = state.limit
        if state.cooling:
            found.append(f"Cooldown until {long_date(state.next_allowed)}: {limit.note or state.explain}")
        elif limit.scope == "role" and state.next_allowed and any(prefilter.normalise_title(r) == wanted
                                                                   for r in state.roles):
            found.append(f"You applied to this role: wait until {long_date(state.next_allowed)} ({limit.note})")
        elif state.full:
            when = f"; next slot {state.next_allowed.isoformat()}" if state.next_allowed else ""
            found.append(f"No slot left ({state.explain.split(';')[0]}){when}: {limit.note}")
        elif state.used is not None and limit.max is not None:
            left = limit.max - state.used
            found.append(f"Uses {'the last slot' if left == 1 else f'1 of the {left} slots'} left "
                         f"({state.explain}): {limit.note}")
        elif limit.window == "season" and state.used == 0:
            found.append(f"Uses this season's application: {limit.note}")
        elif state.used is None and not state.next_allowed and not limit.cooldown_days:
            found.append(limit.note)
    return found


def applications_from_store(conn) -> dict[str, list[Application]]:
    """Every application (seeded and tracked) by normalised company, with its stage history. A role
    marked To apply is not an application (decided 2026-10-25, as on the Applications page): it is
    neither counted on Companies nor against a limit (2026-11-19)."""
    history: dict[int, list[tuple[str, date]]] = {}
    for row in conn.execute("SELECT application_id, stage, changed_at FROM stage_history ORDER BY changed_at, id"):
        history.setdefault(row["application_id"], []).append((row["stage"], date.fromisoformat(row["changed_at"][:10])))
    found: dict[str, list[Application]] = {}
    for row in conn.execute("SELECT id, company, role, applied_on, stage, cooldown_until FROM applications "
                            "WHERE stage != 'to_apply'"):
        found.setdefault(prefilter.normalise_company(row["company"]), []).append(Application(
            company=row["company"], role=row["role"],
            applied_on=date.fromisoformat(row["applied_on"]) if row["applied_on"] else None,
            stage=row["stage"], history=tuple(history.get(row["id"], [])),
            cooldown_until=date.fromisoformat(row["cooldown_until"]) if row["cooldown_until"] else None))
    return found


def summary(registry: dict[str, list[Limit]], apps: dict[str, list[Application]], today: date
            ) -> dict[str, list[Standing]]:
    """Every limited company's standings (normalised name -> standings)."""
    return {norm: standings(limits, apps.get(norm, []), today) for norm, limits in registry.items()}


def as_dicts(found: list[Standing]) -> list[dict[str, Any]]:
    return [{"company": s.limit.company, "scope": s.limit.scope, "note": s.limit.note, "explain": s.explain,
             "next_allowed": s.next_allowed.isoformat() if s.next_allowed else None, "full": s.full,
             "cooling": s.cooling} for s in found]
