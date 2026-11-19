# Measurements

Numbers the design is sized on, with the date each was measured. Aggregates only: per-role detail
(which roles, which scores) stays in the private state folder (`.data/eval/`, `.data/titlecheck/`),
because the calibration set is private. "Window" means the Claude subscription's 5-hour usage window
on the Pro plan.

## 1. Fetching (2026-10-24, 26 configured companies)

| Measurement | Value |
|---|---|
| First `verify` of 26 boards (listings only) | about 6,700 open roles in 77 s |
| First full fetch, including Workday and custom detail pages | 5,763 roles in 212 s (LSEG's Workday listing and details 211 s) |
| Later fetch, details already stored | 54 s (LSEG's 34 Workday listing pages: 35 s) |
| Greenhouse listing with and without `content=true` (one board) | 351 KB and 49 KB |

## 2. Pre-filter (2026-10-24, 26 configured companies)

| Measurement | Value |
|---|---|
| Open roles | 5,763 |
| Pass the pre-filter | 1,250 (22%) |
| Dropped on title / on location | 4,074 / 439 |
| Duplicates merged (same role in several cities) | 182 |
| New roles a day / new passing roles a day (posting dates, last 14 days) | about 54 / about 11 |
| Passing roles with graduate or junior wording in the title (term list of 2026-10-24) | 18% |

## 3. Scoring speed and cost (2026-10-24)

| Measurement | Value |
|---|---|
| Haiku, extended thinking on (the CLI's default) | about 70 s and 3,000 to 10,000 output tokens a call |
| Haiku, `--effort low` | about 64 s, about 5,900 output tokens: barely better |
| Haiku, thinking off (`MAX_THINKING_TOKENS=0`), same posting | 7.5 s, about 400 output tokens, same fit |
| Haiku, thinking off, at 1, 4 and 8 calls in parallel | 9 to 10 s a call; 8 in parallel average 1.55 s per role |
| Haiku API-equivalent cost | about $0.011 to $0.014 a call |
| Sonnet | 18 to 20 s a call, about $0.04 |

## 4. Subscription usage (Pro, read with `/usage`, 2026-10-24)

| Run | Calls | 5-hour window | Week | Cost (API-equivalent) | Time per call |
|---|---|---|---|---|---|
| Usage test, Sonnet | 50 | 6% → 27% | 16% → 18% | $1.92 | 18.2 s |
| Backfill part 1, Haiku (stopped when the window refused a call) | 605 | 27% → 99% | 18% → 24% | $6.50 | 9.3 s |
| Backfill part 2, Haiku (fresh window) | 288 | | | $3.03 | 9.0 s |
| Backfill part 2, Sonnet rescore of Haiku fit 6+ | 130 | 25 calls returned `allowed_warning` | | $5.20 | 20.4 s |

Derived:
- One Sonnet call is about 0.4% of a window; one Haiku call about 0.12%.
- A window holds roughly 240 Sonnet or 800 Haiku calls.
- A full window is about 8% of the weekly limit, so about 12.5 windows (about 10,000 Haiku calls) a week is the ceiling for everything, including the candidate's own Claude use.
- Usage tracks API-equivalent cost at about 0.1% of a window per cent.
- Claude reports `utilization` only from about 90% of the window (`allowed_warning`, `surpassedThreshold: 0.9`); below that the status is only `allowed`.

Backfill outcomes (893 Haiku-scored roles at the 26 configured companies):
- Statuses: 718 blocked, 128 check, 47 clear.
- 130 rated 6+ by Haiku; Sonnet kept 55 of them at 6+, 36 at 7+.

## 5. Scoring agreement with the hand-scored calibration set (`eval`)

Each role is scored 3 times with its own example left out of the prompt.

| Date | Model, rules | Roles | Fit within 1 | Status | Odds | Action | Timing blockers / timing-changed actions (want 0) | Mean fit spread | Cost |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24 | Haiku, thinking off | 4 | 75% | 75% | 100% | 83% | | 0.5 | $0.18 |
| 2026-10-24 | Haiku, thinking on (8,000) | 4 | 83% | 58% | 92% | 83% | | 1.25 | $0.41 |
| 2026-10-24 | Haiku, timing-neutral and hard-requirement rules | 4 | 92% | 92% | 100% | 92% | 0 / 0 | 0.25 | $0.32 |
| 2026-10-24 | Haiku, 15 new roles added | 19 | 72% | 63% | 72% | 81% | 3 / 2 of 57 | 0.47 | $0.66 |
| 2026-10-24 | Haiku, experienced-hire, domain and graduation-window rules | 19 | 77% | 63% | 70% | 82% | 4 / 2 of 57 | 0.32 | $1.02 |
| 2026-10-24 | Haiku, `min_years` | 19 | 72% | 74% | 79% | 82% | 2 / 1 of 57 | 0.68 | $1.16 |
| 2026-10-24 | Sonnet, `min_years` | 19 | 89% | 82% | 81% | 75% | 0 / 0 | 0.68 | $4.11 |
| 2026-10-24 | **Sonnet, final (rules frozen)** | 19 | **89%** | **86%** | **82%** | **86%** | 0 / 0 | 0.74 | $3.02 |

- Final Sonnet run: no experience blocker without years left in scores (2 produced, moved to gaps by code), and no `watch_for_reopening` needed correcting.
- Stopping rule (fit within 1 ≥ 80%, action ≥ 85%, no experience blockers without years) met, so the rules were frozen.
- Haiku as a backlog filter, same date: of runs on roles the candidate rated 7+, 2 of 30 came out below 6 (both on one role that is blocked anyway); of runs on roles rated 5 or less, 8 of 18 came out at 6+ and would go on to Sonnet.

## 6. Batch title check

| Date | Version | Roles | Calls, cost | Dropped | Of those dropped, fit ≤ 3 | Sonnet 6+ dropped | Sonnet 7+ dropped | Calibration 6+ / 7+ dropped | Drops rejected as ungrounded |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24 | Throwaway script: title, company, locations; free-text reasons | 999 | 10, $0.24 | 49% | 90% | | 0 of 42 | | |
| 2026-10-24 | Built: title, company, department, locations; verbatim profile quote required | 998 + 32 calibration | 11, $0.32 | 43% | 92% | 0 of 63 | 0 of 42 | 0 of 21 / 0 of 18 | 21 |
| 2026-10-25 | Built, departments left out (as in light listings) | 998 + 32 calibration | 11, $0.28 | 31% | 92% | 0 of 63 | 0 of 42 | 0 of 21 / 0 of 18 | 26 |

Keep rates by graduate wording (no departments, 2026-10-25):
- Roles with an entry-level term: 81% kept.
- Roles without one: 65% kept.

Haiku's rate of rating a role 6+, so that it goes on to Sonnet (26 configured companies, 2026-10-25):
- With an entry-level term: 31%.
- Without one: 9%.

- The throwaway version also dropped one role the candidate rated 6, with an invented reason. The quote requirement was added because of that.
- About 0.25% of a window per call of 100 titles.

Rule-based alternative (title must contain an engineering word), measured 2026-10-24 on the 998 scored roles:
- It dropped 39%, with 0 of 42 Sonnet 7+ lost.
- But it would drop 2 calibration roles rated 9 (generic "Graduate Programme" and "Tech Academy" titles), so it was rejected.

## 7. Harvest: generated company list (finished 2026-10-25)

Read 3 Common Crawl crawls (CC-MAIN-2026-43, -39, -34) with no failed index pages:
- 44,252 board accounts found.
- 17,984 new accounts on supported boards checked live, in about 2 hours on the laptop, plus 108 s to finish.
- **10,740 relevant companies**, meaning at least one open role whose location passes the pre-filter.

| Board | Accounts found | Already in companies.toml | Checked | Answering with jobs | Relevant | Open roles | Pass the pre-filter | Pass, posted in the last 28 days | Pass, undated |
|---|---|---|---|---|---|---|---|---|---|
| Greenhouse | 6,111 | 12 | 6,099 | 4,303 | 4,107 | 160,807 | 75,675 | 23,923 | 34 |
| Workday | 6,413 | 3 | 6,410 | 3,239 | 3,155 | 698,322 | 392,001 | 209,945 | 171,998 |
| Ashby | 3,881 | 3 | 3,878 | 3,104 | 2,998 | 66,460 | 24,881 | 7,863 | 0 |
| Lever | 373 | 2 | 371 | 275 | 263 | 27,143 | 11,521 | 5,233 | 0 |
| Recruitee (corrected 2026-10-25, see below) | 1,246 | 1 | 1,245 | 1,040 | 1,008 | 20,276 | 14,164 | 3,861 | 1 |

Accounts found on boards without an adapter (not checkable live):

| Board | Accounts | Board | Accounts |
|---|---|---|---|
| Workable | 5,366 | Teamtailor | 1,242 |
| BambooHR | 4,246 | Softgarden | 777 |
| Personio | 3,445 | Jobvite | 737 |
| Join | 2,753 | Homerun | 689 |
| Breezy | 1,938 | JazzHR | 484 |
| Rippling | 1,481 | Pinpoint | 117 |
| Gem | 1,436 | Factorial | 56 |
| SmartRecruiters | 1,418 | Jobylon | 43 |

Recruitee correction (2026-10-25):
- The first harvest checked all Recruitee subdomains in parallel and 963 of 1,245 checks got HTTP 429. Recruitee rate-limits across subdomains.
- With one shared delay and `harvest --recheck-errors`, 1,147 now answer.
- The figures above are the corrected ones; first run: 223 answering, 217 relevant, 3,808 open, 801 passing in 28 days.
- The same re-check moved Workday slightly: 3,244 answering, 3,160 relevant.
- The generated list now has 11,537 companies.

Workday's pass rate is inflated by non-tech employers (retail, healthcare) whose titles the noise rules do not touch. Its undated roles are "Posted 30+ Days Ago".

## 8. Volume estimate from the harvest (2026-10-25, before UK and US parking; superseded by section 10)

New passing roles a day = passing roles posted in the last 28 days ÷ 28: about 8,850 a day across all boards, about 1,350 without Workday.

Usage per role uses section 4: Haiku 0.12%, Sonnet 0.4% (for the 15% Haiku rates 6+), title check 0.25% per 100 titles keeping 57%. The graduate-wording row uses the 18% share from section 2 (an estimate for unknown companies) and assumes the title check keeps 70% of graduate-worded roles.

| Setup | All boards, windows a week | Without Workday, windows a week |
|---|---|---|
| Haiku on everything, Sonnet for Haiku 6+ | about 110 | about 17 (over the limit) |
| Title check, then Haiku, then Sonnet | about 65 | about 10 (about 78% of a week) |
| Graduate wording required, then title check, Haiku, Sonnet | about 14 | about 2.2 (about 17% of a week) |

Backlog for the generated list, without Workday, graduate wording required:
- 28 days back: about 8.7 windows.
- 7 days back: about 2.2 windows.

## 9. Per-run estimate for the 26 configured companies (2026-10-24)

| Measurement | Value |
|---|---|
| Fetch | about 55 s |
| Scoring | about 1 Sonnet call (20 s) a run on average |
| Setup in Actions (checkout, uv, installing the Claude CLI) | about 1 to 1.5 min (estimate) |
| Actions minutes | about 3 a run, about 1,100 a month at 12 runs a day (limit 3,000) |
| Subscription | about 0.4% of a window a run, about 2.5% of the weekly limit a week |

## 10. Sampled re-check: role-level volumes (2026-10-25)

`jobhunt harvest-sample` re-fetched a random sample of relevant accounts per board (light listings), role by role; `scripts/sample_volumes.py` scales it to every relevant account.
- Sample sizes: 300 accounts per board; all 263 for Lever; Recruitee re-sampled after the rate-limit fix.
- Errors: none, except 2 Workday sites.
- UK and US roles are parked as decided: a role is scored only if a location is outside the UK and US, or unrecognised, or unqualified "Remote".

Roles passing the pre-filter, by country group (open roles, all ages):

| Board | Relevant accounts | Seconds per account (mean, median) | Open roles | Passing | EU/EEA/CH | UK | US | Unknown |
|---|---|---|---|---|---|---|---|---|
| Greenhouse | 4,107 | 0.26, 0.25 | 133,655 | 62,344 | 3,888 | 2,122 | 49,695 | 6,640 |
| Ashby | 2,999 | 0.26, 0.26 | 68,407 | 23,782 | 4,648 | 1,789 | 16,475 | 870 |
| Lever | 263 | 0.62, 0.27 | 27,144 | 11,521 | 1,801 | 692 | 8,686 | 342 |
| Recruitee | 1,008 | 0.27, 0.25 | 22,357 | 14,418 | 12,687 | 131 | 675 | 924 |
| Workday | 3,160 | 10.80, 2.85 | 657,927 | 369,031 | 23,456 | 5,832 | 180,820 | 158,923 |

New roles a day, posted in the last 28 days ÷ 28 (7-day rate in brackets):

| Board | Passing | Passing, UK and US parked | Of those, with an entry-level term |
|---|---|---|---|
| Greenhouse | 826 (1,082) | 164 (211) | 11 (12) |
| Ashby | 304 (516) | 85 (174) | 6 (14) |
| Lever | 187 (514) | 36 (105) | 4 (11) |
| Recruitee | 153 (160) | 145 (148) | 7 (7) |
| **Without Workday** | **1,470 (2,272)** | **430 (638)** | **28 (44)** |
| Workday | 6,756 (11,728) | 3,419 (5,976) | 348 (477) |

Subscription use, UK and US parked, title check then Haiku then Sonnet for Haiku 6+:
- Rates used: title check keep and Sonnet shares from section 6, by entry-level term; Haiku 0.12%, Sonnet 0.4%, title check 0.25% per 100.
- The rates are 28-day; at the 7-day rate, multiply by about 1.5.
- The 12.5-window weekly limit is from section 4.

| Scope | No graduate-wording filter, windows a week | Graduate-wording filter, windows a week | 7-day backlog, no filter / filter (windows, once) |
|---|---|---|---|
| Without Workday | 3.3 (26% of a week) | 0.4 (3%) | 4.9 / 0.6 |
| Workday only | 27.2 (218%) | 4.9 (39%) | 46.7 / 6.7 |
| All boards | 30.5 (244%) | 5.3 (42%) | 51.6 / 7.3 |

The 26 configured companies add about 0.3 windows a week.

Entry-level terms in new scored roles a day (28-day rate), without Workday / Workday:

| Term | Without Workday | Workday |
|---|---|---|
| associate | 9.0 | 166.3 |
| junior | 5.6 | 14.8 |
| intern | 4.3 | 44.7 |
| 2027 | 4.2 | 35.2 |
| stage | 2.6 | 41.3 |
| internship | 2.1 | 20.8 |
| engineer I | 1.0 | 0.4 |
| praktikum | 0.6 | 1.1 |
| early career | 0.4 | 1.1 |
| trainee | 0.3 | 8.3 |
| stagiair | 0.2 | 3.8 |
| praktikant | 0.1 | 0.4 |
| entry level | 0.1 | 3.0 |
| graduate | 0.0 | 15.1 |
| apprentice | 0.0 | 16.7 |
| new grad | 0.0 | 0.8 |
| university | 0.0 | 0.8 |
| campus | 0.0 | 0.8 |
| absolvent, berufseinsteiger, becario, stagista | 0.0 | 0.0 |

A role can match several terms.

GitHub Actions minutes, estimated from the seconds per account above. Generated list without Workday; Recruitee on one shared worker.

- **One pass over the generated list:** about 18 minutes of fetching (Greenhouse 17.8 min, Ashby 13 min, Recruitee 4.5 min and Lever 2.7 min run in parallel).
- **Scoring:** about 7.5 minutes a day for about 430 new roles. That is about 4 title-check calls, about 300 Haiku calls and about 30 Sonnet calls, 8 at a time.

| | Configured companies, every 2 h | Generated list | Monthly harvest (newest crawl, Workday not checked) | Total a month (limit 3,000) |
|---|---|---|---|---|
| Generated list checked once a day | about 1,080 | about 780 | about 75 | **about 1,935 (65%)** |
| Generated list checked twice a day | about 1,080 | about 1,300 | about 75 | **about 2,455 (82%)** |

## 11. App page load times (2026-10-25)

Real state after the generated list's first fetch:
- 127,325 stored roles; the app shows 44,425 of them.
- Left out of the app: 82,892 parked UK/US roles, 140 title-check drops, and 4,534 roles that fail the pre-filter.

Measured through the app (FastAPI test client), median of 3 runs, on a copy of the tracker database with sync switched off, so `.data/` was not changed.

| Page | Before (the whole state in memory, filtered per request) | After (indexed catalogue) |
|---|---|---|
| First page after start or sync | 6,769 ms | 44 ms (the catalogue is built at start: 4,253 ms, and after a sync in the background) |
| Jobs, Scored | 1,457 ms | 21 ms |
| Apply now, fit 7+ | 1,441 ms | 8 ms |
| Waiting to be scored | 1,504 ms | 16 ms |
| Scored, page 5 | 1,385 ms | 25 ms |
| All roles, page 300 | 2,500 ms | 54 ms |
| Search "software engineer" | 1,503 ms | 24 ms |
| Filtered (EU/EEA/CH, fit ≥ 0) | 1,411 ms | 20 ms |
| Applications | 781 ms | 5 ms |
| Job detail | 1,162 ms | 6 ms |

What made the difference:
- An SQLite catalogue with indexes for each view's sort and for the filters, and full-text search.
- Counts per view computed at build time, less the few roles hidden or applied to.
- Applications without a job key turned into job keys once per request (indexed), instead of being compared with every role.

## 12. Data-branch sync and Actions minutes (2026-10-25)

Sync before and after the fix, measured on a copy of the real data branch: 127,325 jobs, about 110 MB of state. Pushes went to a local stand-in for GitHub, so network time is not included.

| Step | Before | After |
|---|---|---|
| Pull, nothing changed | 159 s (149 s reading every file of the remote commit by path, 9 s parsing every job) | 0.2 s |
| A scheduled run's save and push (400 jobs in one slot, 50 scores) | 163 s (the same full read before pushing; the push itself 2 s) | 3.2 s |
| Pull of another writer's run (2 files changed) | about 160 s | 0.7 s |
| One-time switch to the new file layout | | 11 s |
| Shallow checkout of the data branch, as Actions does (local) | | 9.5 s |
| Load the state | | 1.1 s |

The causes:
- **Reading every file by path.** `git cat-file --batch` resolved each `ref:path` separately, at about 128 ms per file (154 s for 1,200 files). Reading the same files by object id takes 0.76 s.
- **Reading and parsing everything** even when nothing changed.

The fixes:
- A pull reads only the files the remote changed since the last sync, by object id.
- Job files are split by origin and by daily slot, so a run rewrites about one generated file.
- Unchanged files are not rewritten.

Actions minutes per scheduled run (estimated from the measurements above; setup is an estimate until the first real runs):

| Part | Time |
|---|---|
| Runner, checkouts, uv, Claude CLI install | about 60 to 75 s, plus 10 to 20 s for the data branch from GitHub |
| Pull and state load | about 2 s |
| Fetching: configured companies and one slot of the generated list (about 700 companies), in parallel | about 90 s |
| Scoring about 36 to 53 new roles: 1 title-check call, about 30 Haiku and 3 to 5 Sonnet calls, 8 at a time | about 70 to 80 s |
| Save and push | about 3 to 5 s |
| **Per run** | **about 4 to 4.7 minutes; billed 5 (GitHub rounds each job up)** |

| Month | Before the fix | After |
|---|---|---|
| 360 scheduled runs | about 3,600 minutes (2 × 2 min 40 s of sync on top of each run) | about 1,800 |
| Monthly harvest | about 90 | about 90 |
| CI on pushes | about 40 | about 40 |
| **Total of 3,000** | **about 3,730 (over the limit)** | **about 1,930 (64%)** |

## 13. Generated-list backlog under the new rule (2026-10-25)

Rule:
- Open and not parked.
- Entry-level wording and posted within 20 days (or undated), or no entry-level wording and posted within 14 days.
- Not already scored.

Estimated with `jobhunt backlog`, using the measured title-check keep rates (81% and 65%) and the share Haiku sends to Sonnet (20.5% of entry-worded generated roles on night 2; 9% of others).

| Part | Roles | Text already fetched | Title check keeps | Haiku calls | Sonnet calls | Windows | Share of a week | Nights at 40% |
|---|---|---|---|---|---|---|---|---|
| Entry-level, posted ≤ 20 days or undated | 485 | 264 | 393 | 393 | 81 | 0.81 | 6% | 2.0 |
| Other, posted ≤ 14 days | 6,365 | 2,684 | 4,137 | 4,137 | 372 | 6.61 | 53% | 16.5 |
| **Total** | **6,850** | | **4,530** | **4,530** | **453** | **7.42** | **59%** | **18.5** |

The "other" part by age: 1,222 roles posted within 3 days, 3,692 within 7, 4,876 within 10, and 6,365 within 14.

Blocked roles among generated roles Haiku scored (141 by 2026-10-25): 117 blocked, 18 check, 6 clear.
- Blocking types: language 59, experience 32, enrolment 28, eligibility 24, other 8, visa 4.
- Of 13 blocked roles at fit 6+, Sonnet rescored all: 1 became clear, 4 became check, 8 stayed blocked.

Applied on 2026-10-25 with the other part at 7 days (`jobhunt backlog --apply --other-days 7`):

| Part | Roles | Text already fetched | Title check keeps | Haiku calls | Sonnet calls | Windows | Share of a week | Nights at 40% |
|---|---|---|---|---|---|---|---|---|
| Entry-level, posted ≤ 20 days or undated | 485 | 264 | 393 | 393 | 81 | 0.81 | 6% | 2.0 |
| Other, posted ≤ 7 days | 3,692 | 1,449 | 2,400 | 2,400 | 216 | 3.84 | 31% | 9.6 |
| **Total** | **4,177** | | **2,793** | **2,793** | **297** | **4.64** | **37%** | **11.6** |

## 14. Slow fetch, call cap, retries and overlap (2026-10-27)

**The 1h48m daily fetch (25 Oct, `run --generated all --budget 0.5`):** generated Greenhouse took 6,526 s against 1,256 s the next day. That run wrote 14,530 posting texts (the next one 1,415) while finding only 29 new Greenhouse roles: roles the backlog rule had skipped at their company's first fetch had no text, and the "no text yet" check fetched it at every later fetch, one request at a time, although they are never scored. Fixed: text is fetched only for roles that can be scored. Scheduled runs also stop starting fetches after 600 s and fetch at most 400 texts.

**Haiku stopping at exactly 100 calls:** `[scoring] max_calls_per_run` (100) capped every phase, also with `--budget` (today: 170 pending, 33% of the 50% budget used). An explicit `--budget` now lifts the cap.

**"Claude is not available: claude exited 1":** single failed calls (exit 1, empty stderr) after 100+ good calls, status allowed. Now retried twice; a run stops after 3 failures in a row.

**The 11 Haiku 6+ roles (25 Oct 11:18 backfill):** all 11 were rescored by Sonnet in the three short runs that followed (11:31, 11:33, 11:35: 4 + 4 + 3), each stopped by the 90% window warning; "0 pending" afterwards was right.

**The odd batch (26 Oct 00:27 backfill):** 285 Haiku scores, 279 blocked, 242 at fit 3 or 4. 245 of them were one Speechify role, "Software Engineer, AI Data", posted once per US city with the city in the title and location "Remote" (246 copies, 27 slightly different texts); the blocker was "N+ years of professional experience" (231 of them). Duplicates were matched on the exact title, so the copies were not merged. Fixed by ignoring a trailing place in the title: Speechify drops to 2 roles, and about 4,700 such copies merge across the list (3,000 at one company).

**Overlap of fetching and scoring:** the 26 Oct daily run took 1,805 s: fetch 1,263 s, then my companies (33 Sonnet, 143 s), the title check (8 calls), Haiku (100 calls, 229 s, 41 of them entry-level) and Sonnet (15, 80 s). Simulated at 1/100 scale with the real code: 26.7 s without overlap, 22.7 s with (about 15%, roughly 1,805 s to 1,530 s). The next real run reports its own figure.

## 15. Usage per call from Claude's own readings (2026-10-30)

Since 2026-10-27 every call reports Claude's five-hour and seven-day utilisation
(`rate_limit.unifiedWindows`), whole percent. The tool's estimate stood at 72% of the week while
Claude reported 33% (your own use included), so the per-call figures of 2026-10-24 were checked
against those readings.

**Method:** for each GitHub run with 10+ calls in one five-hour window, the rise of the five-hour
figure from its first to its last call against the API-equivalent cost of the calls in between
(title checks included). 23 runs, 2026-10-28 to 2026-10-30.

| Runs | Runs counted | Cost | Five-hour rise | Window per dollar | Tool's recorded estimate |
|---|---|---|---|---|---|
| Night (Amsterdam 00:00–07:00, no own use) | 9 | $2.50 | 0.24 | 0.096 | 1.35 windows |
| Day | 14 | $5.16 | 0.54 | 0.105 | 2.13 windows |
| All | 23 | $7.66 | 0.78 | 0.102 | 3.48 windows |

Usage follows cost at **0.10 of a window per dollar**, night and day alike. What had changed is
the cost per call (prompt caching): Haiku $0.00135 (was about $0.011 on 2026-10-24), Sonnet
$0.025 (median $0.017), a 100-title check $0.0038. So per call: Haiku about 0.014% of a window
(configured 0.12%, nine times too high), Sonnet 0.25% (configured 0.4%), title check 0.04%
(configured 0.25%). A Haiku-only run of 155 calls was recorded as 0.20 of a window and used 0.03.

**Windows per week:** overnight, the seven-day figure rose 1% per night for about $1 of calls:
0.008 to 0.01 of the week per dollar, so 10 to 13 windows a week; 12.5 kept. (Summing each
five-hour window's highest reading gives only a lower bound, 5.8: readings exist only while the
tool calls, so most of your own use is unseen.) The tool's calls this week cost $7.35, about 0.75
windows or 6% of the week; the rest of Claude's 33% was your own use.

**Changes:** each call counts at its actual cost (`[usage] window_share_per_usd = 0.10`), after
reserving `share_per_call` (Haiku 0.00015, Sonnet 0.0025, title check 0.0004) before it starts;
past runs are counted from their recorded cost too. As budgets now mean real usage they rarely
stop a scheduled run, so scheduled runs start no call after 14 minutes (`[scoring]
scheduled_max_run_seconds = 840`) to stay within the Actions job's 20.

**A second weekly limit:** on 2026-10-30 calls from the laptop (Haiku and Sonnet alike, Claude
Code 2.1.114 and the newest 2.x) reported a single seven-day reading, 26 to 29% with status
`allowed_warning` and no threshold, resetting 5 November 18:00 UTC, while the weekly limit stood
at 33% resetting 4 November 01:00. Claude does not name it (most likely a per-model weekly
limit). The tool now keeps both (`pipeline.claude_weeks`: a reset a whole number of weeks from the
weekly limit's is the weekly limit, any other is the second), the guard and the backlog hard stop
follow the higher, and a warning stops a run only from the guard's 75%.
