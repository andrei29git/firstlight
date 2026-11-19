"""Volume tables from the sampled re-check (`jobhunt harvest-sample`), scaled to all relevant accounts
per board. Usage rates per role are the measurements in docs/measurements.md. Run: uv run python
scripts/sample_volumes.py"""
import json, glob, collections, statistics, sys
from pathlib import Path
from jobhunt.state import read_jsonl
from jobhunt import prefilter

ROOT = Path(".data")  # run from the project root
# The main sample, with any later per-board sample file (sample-<date>-<board>.jsonl) replacing that board.
files = sorted(glob.glob(str(ROOT / "harvest/sample-*.jsonl")))
main = [f for f in files if f.endswith(tuple(f"{d}.jsonl" for d in "0123456789"))][-1]
records = [json.loads(l) for l in open(main)]
for f in files:
    if f != main and not f.endswith(tuple(f"{d}.jsonl" for d in "0123456789")):
        board = f.rsplit("-", 1)[1].removesuffix(".jsonl")
        records = [r for r in records if r["board"] != board] + [json.loads(l) for l in open(f)]
        print(f"(using {Path(f).name} for {board})")
generated = read_jsonl(ROOT / "companies_generated.jsonl")
relevant = collections.Counter(g["board"] for g in generated)
# Usage per role (share of a 5-hour window), from docs/measurements.md sections 4 and 6, and this session:
TITLE, HAIKU, SONNET = 0.0025 / 100, 0.0012, 0.004
KEEP = {True: 0.81, False: 0.65}   # title check keep rate with / without entry terms (no departments)
TO_SONNET = {True: 0.31, False: 0.09}  # Haiku rates 6+ with / without entry terms
WEEK = 12.5

boards = sorted({r["board"] for r in records})
summary = {}
for b in boards:
    recs = [r for r in records if r["board"] == b]
    ok = [r for r in recs if "error" not in r]
    scale = relevant[b] / len(ok)
    roles = [x for r in ok for x in r["roles"]]
    passing = [x for x in roles if x["pass"]]
    scored = [x for x in passing if not x["parked"]]
    def recent(xs, days): return [x for x in xs if x["age"] is not None and x["age"] <= days]
    groups = collections.Counter(x["group"] for x in passing)
    terms = collections.Counter(t for x in recent(scored, 28) for t in x["terms"])
    s = dict(sampled=len(recs), errors=len(recs) - len(ok), scale=scale,
             sec_per_account=statistics.mean(r["seconds"] for r in ok), sec_median=statistics.median(r["seconds"] for r in ok),
             open=len(roles) * scale, passing=len(passing) * scale,
             groups={g: n * scale for g, n in groups.items()},
             per_day={}, terms={t: n * scale / 28 for t, n in terms.items()})
    for label, xs in (("pass", passing), ("pass, UK/US parked", scored)):
        for days in (7, 28):
            r = recent(xs, days)
            s["per_day"][(label, days, "all")] = len(r) * scale / days
            s["per_day"][(label, days, "terms")] = sum(bool(x["terms"]) for x in r) * scale / days
    # usage per day (windows), UK/US parked, 28-day rate
    r28 = recent(scored, 28)
    def windows(rows, filt):
        tot = 0.0
        for x in rows:
            t = bool(x["terms"])
            if filt and not t: continue
            tot += TITLE + KEEP[t] * (HAIKU + TO_SONNET[t] * SONNET)
        return tot * scale / 28
    s["windows_day_nofilter"] = windows(r28, False)
    s["windows_day_filter"] = windows(r28, True)
    r7 = recent(scored, 7)
    s["backlog7_nofilter"] = sum(TITLE + KEEP[bool(x["terms"])] * (HAIKU + TO_SONNET[bool(x["terms"])] * SONNET) for x in r7) * scale
    s["backlog7_filter"] = sum(TITLE + KEEP[True] * (HAIKU + TO_SONNET[True] * SONNET) for x in r7 if x["terms"]) * scale
    summary[b] = s

print(f"{'board':10} {'sampled':>7} {'err':>4} {'relevant':>8} {'s/acct':>6} {'median':>6} {'open':>9} {'pass':>8}  EU/EEA/CH      UK       US  unknown   other")
for b, s in summary.items():
    g = s["groups"]
    print(f"{b:10} {s['sampled']:7} {s['errors']:4} {relevant[b]:8} {s['sec_per_account']:6.2f} {s['sec_median']:6.2f} {s['open']:9.0f} {s['passing']:8.0f}  {g.get('EU/EEA/CH',0):9.0f} {g.get('UK',0):7.0f} {g.get('US',0):8.0f} {g.get('unknown',0):8.0f} {g.get('other',0):7.0f}")
print("\nNew roles a day (28-day rate; 7-day rate in brackets):")
for b, s in summary.items():
    d = s["per_day"]
    print(f"{b:10} pass {d[('pass',28,'all')]:7.0f} ({d[('pass',7,'all')]:6.0f}) | parked: all {d[('pass, UK/US parked',28,'all')]:6.0f} ({d[('pass, UK/US parked',7,'all')]:6.0f})  with entry terms {d[('pass, UK/US parked',28,'terms')]:5.0f} ({d[('pass, UK/US parked',7,'terms')]:5.0f})")
print("\nWindows a week, UK/US parked (title check -> Haiku -> Sonnet for 6+):")
for name, pick in (("all boards", boards), ("without Workday", [b for b in boards if b != "workday"]), ("Workday only", ["workday"])):
    nf = sum(summary[b]["windows_day_nofilter"] for b in pick) * 7
    f = sum(summary[b]["windows_day_filter"] for b in pick) * 7
    b7n = sum(summary[b]["backlog7_nofilter"] for b in pick); b7f = sum(summary[b]["backlog7_filter"] for b in pick)
    print(f"  {name:16} no filter {nf:6.1f} ({nf/WEEK:5.0%})  graduate wording {f:5.1f} ({f/WEEK:5.0%})   7-day backlog: {b7n:5.1f} / {b7f:4.1f} windows")
print("\nEntry terms in new parked-scored roles a day (28-day rate), without Workday / Workday:")
allterms = collections.Counter(); wd = collections.Counter()
for b, s in summary.items():
    for t, n in s["terms"].items():
        (wd if b == "workday" else allterms)[t] += n
for t in prefilter.ENTRY_TERMS:
    print(f"  {t:18} {allterms.get(t,0):7.1f} {wd.get(t,0):8.1f}")
json.dump({b: {k: (v if not isinstance(v, dict) else {str(kk): vv for kk, vv in v.items()}) for k, v in s.items()} for b, s in summary.items()},
          open(ROOT / "harvest/sample-summary.json", "w"), indent=1)
