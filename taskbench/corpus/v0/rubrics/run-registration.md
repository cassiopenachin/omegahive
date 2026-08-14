# Grading rubric — register the durable run — make the generation token real

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **runbook-caveats** — Both recovery-runbook restore steps — the one in the code repo and the one in the workspace — no longer carry the inert-bump caveat.
- **pytest** (checked mechanically) — the whole suite green — the order's no-regressions bar
- **ruff** (checked mechanically) — lint clean
- **mypy** (checked mechanically) — types clean
- **registration-property** (checked mechanically) — the order's property, tested against the candidate's own code rather than against any particular implementation of it: a first emit on a fresh run registers it, a second is a no-op, a REPLAYED emit on a run with events but no registry row still registers it, and a generation bump then succeeds

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-generation-semantics** — No changes to generation semantics, mismatch handling, or cursor logic.
- **never-bump-live** — Never bump the live run's generation; bump evidence comes from a scratch run.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- The `omegahive` row demonstrably exists in `runs`, with query evidence. (Requires a live-spine mutation or a merged-and-deployed image. Both are operator acts that post-date any worker's result, as the closed task's own report records; an offline replay cannot produce them.)
- The durable run's registry row exists, with read-only query evidence.

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
