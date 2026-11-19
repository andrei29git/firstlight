# Scoring rules

<!-- note: Generic template, sent to the AI with every scoring call together with profile.md and the calibration examples. Candidate facts are not written here: placeholders in double curly braces are filled from candidate.toml, and rendering fails on any placeholder without a value. The block between the timing markers is included only when consider_start_date = true in settings.toml, and the block between the no-timing markers only when it is false. Comments starting with "note:" are stripped before sending. -->

<!-- no-timing:start -->
## Timing is ignored

Start dates, "start now" hiring and intake timing do not count for this candidate. They never change fit, blockers or action: there are no `timing` blockers, and a role is never skipped, or given a lower fit, because of when it starts. Still extract `start_date` if the posting states one. A stated graduation window ("must graduate between December 2026 and August 2027") is an `eligibility` requirement, not timing. A required start date before the candidate graduates is not a reason to skip: judge the role on its other requirements.
<!-- no-timing:end -->

## Fit (0 to 10)

How good the role is for {{candidate.name}}: stack, domain, level, the experience it asks for, and the languages it requires. A stated experience requirement the candidate does not meet lowers fit, and so does a required language they do not speak.

A role written for experienced hires loses about 2 fit points, even when no number of years is stated. Apply this only when the posting shows positive experienced-hire signals **and** has no graduate, junior, intern, early-career or new-grad wording. Experienced-hire signals: senior titles hired through the same posting ("Java Engineers and Senior Java Engineers"), requirements phrased as past production work ("you have deployed high-throughput services"), or expectations of owning or leading systems. A posting with no level signals either way gets no penalty.

Fit does **not** change for visa, start date, enrolment rules or grade cut-offs. Those go in `blockers`, next to the fit and not subtracted from it. Fit is the sort key, so a strong role with a visa or eligibility problem must still show a high fit.

## Status and blockers

Blockers are only **hard requirements** the candidate may not meet: eligibility (for example right to work, degree or graduation window), a stated minimum number of years of experience, a required language, enrolment, visa, and grade cut-offs, plus timing when the Availability section is present. Skills, tools, frameworks, domain knowledge and portfolio items (open-source contributions, a GitHub profile, publications) are never blockers: list the missing ones in `gaps`, and let them lower fit. Preferences ("ideally", "a plus", "preferred degree") are never blockers. Domain knowledge (finance, trading, a given industry) is a gap, never an `experience` blocker.

Each blocker has a `severity`: `blocking` (realistically rules the role out) or `check` (the candidate needs to look at it or ask). Code sets the status from the blockers: any `blocking` means blocked, otherwise any `check` means check, otherwise clear.

Whether a posting is closed is decided by the tool (the posting has gone from its board), not by the AI.

## Odds (low, medium, high)

{{odds.rubric}}

## Rules

- **Wording:** prefer postings that say graduate, new grad, student, intern, early career, trainee, or name a graduating year. This is a level signal and counts toward fit.
- **Experience:** a stated minimum of 2+ years is `experience` blocking. A stated 1+ year of a *specific* skill the candidate lacks (for example production ML) is also blocking. Experience-heavy wording with no number ("experience designing scalable systems") is not a blocker: it lowers fit (see Fit). Unmet experience requirements also lower fit. Every `experience` blocker sets `min_years` to the minimum the posting states; with no stated number it is not a blocker, so put it in `gaps` instead (the tool moves any experience blocker without `min_years` to `gaps`). `min_years` is null for every other blocker type.
- **Enrolment:** internships that require study remaining *after* the internship are `enrolment` blocking unless the candidate continues to further study. {{enrolment.master_decision}} "Penultimate or final year" internships are a check for the same reason.
- **Visa:** visa never changes fit. The candidate is a {{visa.citizenship}}.
  - {{visa.clear_regions}}
  - **{{visa.sponsorship_countries}}:** {{visa.policy}} The detail must say what the posting states: sponsorship offered, not mentioned, or refused ("posting says no sponsorship").
{{visa.country_notes}}
  - Any other country is removed by the pre-filter and never reaches scoring.
- **Language:** {{languages.summary}} Required fluency in any language other than {{languages.other_than}} is `language` blocking and lowers fit. "Nice to have" is fine.
- **Grade:** {{grade.summary}} {{grade.blocking_rule}}
- **Stack:** strong in {{stack.strong}}. {{stack.poor_fit}} are poor fits (low fit).
- **Portfolio:** {{portfolio.summary}} A required GitHub profile or open-source experience goes in `gaps`, never a blocker on its own.

<!-- timing:start -->
## Availability (only when consider_start_date = true)

- Full time from **{{availability.full_time_from}}**. {{availability.current_commitments}}
- Full-time starts before {{availability.full_time_from}} are `timing` blocking.
- A role with no graduate wording and no start date is usually a "start now" hire, which the candidate cannot take: `timing` check, or blocking if the posting asks for an immediate start.
- Short events or assessment days on weekdays are a `timing` check.
- When timing is the only problem with a strong role, prefer the action `contact_recruiter_about_later_intake` or `apply_state_availability` over `skip`.
- `apply_state_availability`: apply, stating when the candidate is available full time ({{availability.full_time_from}}).
- `contact_recruiter_about_later_intake`: strong role, but this intake does not work; ask about the next one.
<!-- timing:end -->

## Actions

- `apply_now`: worth applying to now.
- `watch_for_reopening`: only for a role with fit 7 or more that is blocked only because its graduation window ended before the candidate graduates: next year's cohort will fit. Every other block is `skip`: any other eligibility rule, a grade cut-off, enrolment, language or experience, and a graduation window that starts after the candidate graduates.
- `skip`: not worth it. Mention in the explanation if a pending decision (such as further study) would change this.
