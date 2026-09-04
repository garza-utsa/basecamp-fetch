---
name: direct-report-overview
description: Turn a direct report's completed-Basecamp-task markdown exports (in user-reporting/<name>/*.md) into a self-contained HTML work overview for performance-evaluation prep. Use this whenever the user asks to summarize, review, or build an overview/report of a direct report's completed work, tasks, or Basecamp activity for an evaluation, review, or 1:1 — even if they just name a person and say something like "what has X been working on" or "pull together X's work for their review." Also use it if they ask to update/regenerate an existing person's overview after new reporting periods are added.
---

# Direct-report work overview

Reads one person's completed-Basecamp-task markdown reports and produces a
single HTML file summarizing their work — built to help write a clear,
specific, evidence-based performance evaluation, not just to restate the
task list.

## Why this exists

A raw pile of completed checklist items is hard to evaluate someone from.
The value of this skill is turning ~50-90 individually mundane line items
into the handful of things that actually matter for a review: how broad the
person's reach is, what they built, whether they handled anything unusual
well, and what a fair reviewer should and shouldn't conclude from ticket
data alone. Every direct report should get a report that's structured the
same way and held to the same evidentiary bar, so reviews are comparable
across people.

## Workflow

### 1. Find the input files

Input lives at `user-reporting/<slug>/<slug>-<period>.md` — one file per
reporting period (see `references/input-format.md` for the exact structure
and a parsing gotcha worth reading before you start counting). A person may
have one file or several; always read **all** of them, not just the latest.

If the user names a person and the directory name isn't an obvious match
(nicknames, last-name-only, etc.), glob `user-reporting/*/*.md` and check
each file's `person:` frontmatter field rather than guessing at a directory
name.

### 2. Parse and compute

For the combined set of files, work out:
- Total completed checklist items, and total distinct tickets (H3 headings — a ticket with 3 checklist items is 1 ticket, not 3)
- Distinct project areas (H2 headings, aggregated by text — see the parsing gotcha)
- A per-month completion count across the whole span
- A per-project-area total, and per-period breakdown if there's more than one input file
- Sanity check: your total item count must match the "N completed tasks" line in each file's summary — if it doesn't, re-check your H2 aggregation

For a batch this size, don't hand-tally by reading the markdown — write a
small script (Python's fine) that parses the checklist lines, H2s, and H3s
and prints the numbers. It's faster and it's the only reliable way to catch
an H2-aggregation slip before it silently propagates into the report.

#### Computing the five stat tiles

The executive summary always shows exactly these five tiles, in this order.
Keeping the set and the order fixed is the point — it's what makes reports
across different direct reports comparable at a glance, so don't add,
drop, or reorder them per person the way the rest of the report adapts.

1. **Completed items** — total `- [x]` lines across all files for this person.
2. **Project areas touched** — count of distinct H2 heading *text* (aggregate repeats, per the gotcha above).
3. **Distinct tickets / initiatives** — count of H3 headings (every occurrence counts, even ones that happen to share a title like "Web update: Web update" under different areas — each is a separate ticket).
4. **Avg. completions / month** — total items ÷ number of calendar months from the first to the last month with any file's `range` in it, inclusive (e.g. Sep 2025 through Aug 2026 = 12 months) — not just months that happen to have activity. Excluding quiet months would inflate the average and misrepresent pace. One decimal place (`7.3`), unless the input is a single ~3-4 month period where a whole number reads more naturally (`17`).
5. **Completed on/before due date** — of the items that *have* a stated due date (many don't — ad hoc requests and meetings often lack one), what percentage were completed on or before it. **Exclude items with no due date from both the numerator and denominator** — don't count them as on-time by default, and don't count them as missing/late either; they're simply not measurable on this axis. Round to a whole percent. Because the denominator is a subset of the total, say so explicitly in the executive-summary prose (e.g. "68% of the 60 dated items were completed on or before their due date") so the tile's percentage isn't misread against the full item count.

If a burst of late-but-recovered work (see the burst/anomaly pattern above)
drags this percentage down, say that plainly in the prose near the tile —
a reader skimming a bare percentage could otherwise misread a rollback
recovery as a timeliness problem, which is the opposite of what happened.

Then look for what's actually notable in *this specific person's* data —
don't assume every report needs the same narrative beats:
- **New builds/launches** — tickets about building or launching a page/site, not just editing one
- **Accessibility/compliance work** — explicit accessibility-check or WCAG/contrast-remediation tickets
- **Bursts or anomalies** — a cluster of completions concentrated in a short window, especially when several items' due dates sit months before their completion dates. That pattern usually means backlog recovery (an outage, a CMS rollback, a return from leave, a reprioritization) rather than the person suddenly working faster — worth a spotlight, but say plainly that the cause is *inferred from ticket timing*, not confirmed, unless the source data says otherwise
- **Governance/cleanup initiatives** — a multi-ticket audit or cleanup effort that isn't tied to a single stakeholder request
- **Training/mentoring** — an explicit ticket about teaching or documenting a process for someone else

If a person's data doesn't support one of these patterns, leave that section
out entirely rather than stretching thin evidence to fill it. A report with
4 solid sections beats one with 8 where half are padding — and a padded
"strength" that doesn't survive a follow-up question undermines the whole
document's credibility in an evaluation setting.

Conversely, when a section *does* apply and has more qualifying items than
are worth reading one by one (e.g. 20+ accessibility checks), don't dump the
full raw list — that just recreates the wall-of-text problem this skill
exists to solve. List a representative sample (roughly 8-12) and close with
one summary line covering the rest (date range, areas touched, total count).

### 3. Build the HTML

Copy `assets/template.html` to the output path and fill it in — don't
design a new layout from scratch. The template's CSS is a validated,
light/dark-aware design system; keep it as-is so every direct report's
report looks like part of the same set. It uses a single accent color for
all bars because these are magnitude-by-category/time displays, not
multi-series charts that need to distinguish overlapping identities — if a
future report genuinely needs a multi-series chart, stop and consult the
`dataviz` skill for a proper categorical palette rather than picking colors
by eye.

The template marks each section `ALWAYS INCLUDE` or `CONDITIONAL` — follow
that. Replace `{{PLACEHOLDER}}` tokens with real content and delete
comments as you go; delete any conditional `<section>` that doesn't apply.

**Naming the report**: don't call it an "annual" overview unless the
combined data actually spans close to a year — a person with one quarter of
data should get a report whose title/subtitle honestly reflects that (state
the real date range in the subtitle instead of a time-word). Save the
output as `user-reporting/<slug>/<slug>-work-overview.html`, overwriting on
regeneration so there's one current file per person.

### 4. Handle it as sensitive personnel data

This output describes a named employee's work for use in a performance
evaluation. Keep it as a local file only. Do **not** publish it via the
Artifact tool (or any other sharing mechanism) automatically — that would
turn private personnel prep material into a shareable link without the
user deciding to do that first. Only publish or share it if the user
explicitly asks.

### 5. Say what you found, briefly

After writing the file, summarize in a few sentences what's in it — the
headline numbers, and whichever conditional sections you included — rather
than just announcing that a file was created. The user is about to use this
to write an evaluation; a one-line "done" doesn't help them decide whether
it's usable yet.
