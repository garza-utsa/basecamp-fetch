# Input format: Basecamp completed-task markdown reports

These files are produced by `basecamp_completed.py` and live at
`user-reporting/<person-slug>/<person-slug>-<season>-<year>.md` — one file per
reporting period (typically a season/quarter). A person may have anywhere from
one file (a new report, or a new direct report you just started tracking) up
to many, accumulated over time. Always read **all** files for the person, not
just the most recent one.

## Frontmatter

```yaml
---
type: reference
date: 2026-09-04              # when the export was generated (not the work itself)
person: Pamela Dyer           # display name — use this for the report title
range: 2025-09-01 to 2025-12-31        # the reporting window queried
completed_span: 2025-12-18 -> 2025-09-02  # actual first/last completion inside it
tags: [...]
---
```

Use `range` (or `completed_span`) from each file to figure out the total
period the person's combined files cover, and to sort files chronologically
before parsing — filenames are a reasonable proxy but the frontmatter is the
source of truth.

## Body structure

```markdown
# Completed Basecamp Tasks — <Person Name>

N completed tasks (<range>). Latest completion: <date>; earliest: <date>.

## <Project Area>

### <Ticket / initiative title>
- [x] [<checklist item text>](<basecamp url>) — completed <YYYY-MM-DD> (due <YYYY-MM-DD>)
- [x] [<checklist item text>](<basecamp url>) — completed <YYYY-MM-DD>
```

- **H2 (`##`) = the Basecamp project/bucket** — the team or initiative the
  work belongs to (e.g. "Student Success", "Academic Innovation", "Cascade
  Rollback remediation"). This is the right grouping for a "work by area"
  breakdown.
- **H3 (`###`) = one ticket** (a Basecamp to-do list / card), which can
  contain one or several checklist items.
- Each `- [x]` line is one completed checklist item: a linked title, a
  completion date, and an *optional* due date. Items with no due date are
  common (ad hoc requests, meetings) — don't treat their absence as a data
  error.

## Important parsing gotcha

**The same H2 heading can appear more than once, non-contiguously**, when a
person did work for the same project area in two unrelated tickets that
happen to be interleaved chronologically with other projects (the source data
is ordered by completion date, not grouped by project). For example, "Global
Initiatives" or "Faculty Success" may each appear as an `## ` heading twice in
the same file, several sections apart. When tallying totals by project area,
**aggregate by heading text across the whole file**, not by treating each
heading occurrence as a separate bucket — otherwise per-area totals will be
wrong and won't reconcile with the file's own reported item count in the
summary line ("N completed tasks").

A good sanity check after parsing: sum of all checklist items you counted
should equal the N stated in the file's summary line. If it doesn't, you
missed or double-counted something.
