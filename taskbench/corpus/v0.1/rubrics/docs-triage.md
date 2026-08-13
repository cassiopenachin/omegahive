# Grading rubric — docs triage — current, labeled, or archived; nothing ambiguous

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **status-headers** — Moved documents carry a prominent Status line naming their new home and why; kept-current documents have stale Status headers refreshed.
- **conservative-on-doubt** — A triage verdict the document's own content contradicts was left in place and the reason stated, rather than applied.
- **convention-readmes** — Two-line READMEs exist in the archive and reference directories explaining what lands in each.
- **result-report** — A result report exists under the project's reports directory carrying What shipped / How verified / To operate / Reflection, and its claims match what the attempt actually did.
- **docs-index-complete** (checked mechanically) — docs/INDEX.md exists and no document has two entries; every file it does not account for is listed as a NOTE in the log, for you to weigh against the order
- **link-integrity** (checked mechanically) — every relative markdown link in docs/ and README.md resolves
- **nothing-deleted** (checked mechanically) — archive means archive — no doc that existed at the baseline is gone

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **moves-and-labels-only** — Moves and labels only — no content rewrites beyond Status headers and link fixes.
- **nothing-deleted** — Nothing is deleted: archive means archive.
- **evidence-untouched** — docs/evidence/ stays where it is; label it in the index, do not reorganize it.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- Announce the result on the spine per WORKER.md. (A spine emit, which this instrument's own scope forbids. The report the order asks for IS written and graded — only the announcement is out of reach.)

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
