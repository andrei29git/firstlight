"""Per-country funnel of open roles (first run 2026-10-29, saved here 2026-10-30): stored, passing
the pre-filter, graduate wording (or yours), skipped by the backlog rule, dropped by the title
check, waiting, scored, fit 6+ and 7+, with the blockers seen; split into your companies and the
generated list, and the boards the passing roles come from. Read-only.

    uv run python scripts/country_funnel.py [NL RO IT FR DE]
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

from jobhunt import config as cfg
from jobhunt import geo, pipeline, prefilter
from jobhunt.state import State

LANGUAGES = ("dutch", "german", "french", "italian", "romanian")


def main(countries: list[str]) -> None:
    paths = cfg.Paths.from_env()
    rules = pipeline.GeneratedRules.from_settings(cfg.load_settings(paths.config))
    state = State.read_only(paths.state_dir)
    by_key: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for score in state.scores.values():
        by_key[score["key"]][score["model"]] = score

    def shown(key: str) -> dict | None:
        for model in ("sonnet", "haiku"):
            result = by_key.get(key, {}).get(model, {}).get("result")
            if result:
                return result
        return None

    funnel = {c: collections.Counter() for c in countries}
    companies = {c: collections.defaultdict(set) for c in countries}
    boards = {c: collections.Counter() for c in countries}
    dropped = {c: collections.Counter() for c in countries}
    blockers = {c: collections.Counter() for c in countries}
    blocking = {c: collections.Counter() for c in countries}
    for r in state.jobs.values():
        if r.get("closed"):
            continue
        places = set().union(*[geo.parse(x).codes for x in r.get("locations") or []] or [set()])
        for c in places & set(countries):
            f, who = funnel[c], ("generated" if pipeline.is_generated(r) else "yours")
            f[f"1 stored ({who})"] += 1
            companies[c][who].add(r["company"])
            if r.get("prefilter") != "pass":
                f[f"2x pre-filter dropped ({who})"] += 1
                dropped[c][r["prefilter"].split(":")[0]] += 1
                continue
            if r.get("duplicate_of"):
                f[f"2x duplicate ({who})"] += 1
                continue
            f[f"2 passes the pre-filter ({who})"] += 1
            boards[c][r["source"]] += 1
            scored = bool(by_key.get(r["key"]))
            if pipeline.is_generated(r) and not prefilter.entry_terms(r["title"]) \
                    and rules.needs_entry_terms(r.get("locations") or []):
                f[f"3x no graduate wording, filtered ({who})"] += 1
                if not scored:
                    continue
            else:
                f[f"3 scored by the rules ({who})"] += 1
            if r.get("backfill_skip"):
                f[f"4x backlog rule skipped ({who})"] += 1
                if not scored:
                    continue
            if (r.get("titlecheck") or {}).get("dropped"):
                f[f"5x title check dropped ({who})"] += 1
                continue
            result = shown(r["key"])
            if not result:
                f[f"6x waiting ({who})"] += 1
                continue
            f[f"6 scored ({who})"] += 1
            if result["fit"] >= 6:
                f[f"7 fit 6+ ({who})"] += 1
            if result["fit"] >= 7:
                f[f"7 fit 7+ ({who})"] += 1
            for b in result.get("blockers") or []:
                blockers[c][f"{b['type']}/{b['severity']}"] += 1
                if b["severity"] == "blocking" and b["type"] in ("language", "eligibility", "experience", "enrolment"):
                    detail = b["detail"].lower()
                    kind = next((lang for lang in LANGUAGES if lang in detail), None) or (
                        "enrolled/student" if "enrol" in detail or "student" in detail
                        else "years" if "year" in detail else "other")
                    blocking[c][f"{b['type']}: {kind}"] += 1
    for c in countries:
        print(f"\n=== {c}: companies with open roles there: yours {len(companies[c]['yours'])}, "
              f"generated {len(companies[c]['generated'])}")
        for key in sorted(funnel[c]):
            print(f"  {key:45s} {funnel[c][key]:7,}")
        print("  passing roles by board:", boards[c].most_common(8))
        print("  pre-filter drops:", dropped[c].most_common(4))
        print("  blockers:", blockers[c].most_common(6))
        print("  blocking detail:", blocking[c].most_common(6))


if __name__ == "__main__":
    main(sys.argv[1:] or ["NL", "RO", "IT", "FR", "DE"])
