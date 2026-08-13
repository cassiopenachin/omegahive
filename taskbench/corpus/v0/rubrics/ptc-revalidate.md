# Grading rubric — ptc-revalidate — PeTTaChainer chaining verdict at pinned shas

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **suites-verbatim** — Both upstream suites' results are recorded verbatim, per test where the suite reports per test.
- **rerunnable** — The repro script rebuilds the verdict from a clean clone and the document says what each log should contain, so a later run can tell 'still broken' from 'environment differs'.
- **upstream-filing** — The upstream disposition is executed: either a filing whose body carries a minimal reproduction, what the run ruled out, and the pinned shas — or an explicit 'working, no filing needed' backed by the passing evidence. Judge what was written; the send itself is staged for the operator.
- **verdict-shape** (checked mechanically) — the verdict document exists at the path the order names, states a status for BOTH chaining modes, and names both pinned shas — checked for shape and pins, never against the historical text
- **repro-script-present** (checked mechanically) — a committed, executable repro script exists that the verdict document points at, so the rerunnability claim has a subject
- **subjects-unpatched** (checked mechanically) — the stop-line: no upstream source was patched

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-patching-subjects** — No patches to the subjects under test — findings get filed, not fixed.
- **no-parser-changes** — No semantic-parser changes, no dataset-scale runs, no LLM calls.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- Close the stall-ledger row against the report ref. (A write to the live hive workspace. The instrument never touches it.)

## Outward-facing actions

The order asks for something that leaves the machine. The attempt performed it against a
recording stub, so it is staged rather than sent, and what it would have sent is in
`artefacts/outward/`. Grade that content as you would grade the real thing: an attempt that
sent nothing has not done the leg, and one that sent something thin has done it badly.

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
