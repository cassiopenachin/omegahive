# Grading rubric — instrument-teeth — the predictions gate becomes a tool, not a reading

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **one-parser** — DoD (c): the launch-side gate and the scorer are demonstrably ONE parser — a single input yields the same verdict from both callers, proven by a case, not by two implementations that currently agree.
- **refuse-unparsed-not-absent** — DoD (a)/(b): an order whose Predictions section parses zero of three fields is refused BEFORE any spine write, with an override flag; an order with no Predictions section at all, and one carrying the declared-unpredicted disposition, both launch with no refusal; a partial section warns and launches.
- **absent-vs-unparsed-distinct** — DoD (d): coverage records section-absent and section-present-but-unparsed as two different strings.
- **review-vocabulary** — DoD (e): the review field accepts only the three values, case-insensitively, stored lowercase; anything else is refused naming them.
- **close-ordering** — DoD (f): the close emits first; a regeneration failure afterwards is loud and non-fatal and never makes a successful close look failed.
- **refuse-not-rebase** — DoD (g): on push failure the metrics commit helper refuses — commits locally, prints the manual step, exits non-zero — and does not rebase.
- **named-risks-unscored** — The fourth prediction bullet stays required-but-unscored and is DOCUMENTED as unscored. Quietly starting to score it is a stop-line.
- **drill-hygiene-reported** — DoD (h): the bounded two-class drill-hygiene sweep is reported — files read, instances found, fixes made, or 'none found' stated.
- **shellcheck** (checked mechanically) — shellcheck -x clean on the loop scripts, DoD (i)
- **metrics-drill** (checked mechanically) — the hermetic metrics/scoring drill, green including the new cases

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-prediction-field-changes** — No prediction fields are added, removed, or newly scored.
- **no-event-model** — No event-model, board-guard, adopt-logic, gateway, notifier or web-UI changes.
- **never-edit-orders** — Tooling never edits an order or report body; the launch check reads and refuses and writes nothing back.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- `scripts/hive-tooling-drill.sh` green, including this task's new cases. (The loop drill emits scratch spine events, which this instrument's own scope forbids, and it drives tmux. At this task's baseline the drill predates the tmux isolation that the task itself introduces, so running it necessarily creates sessions on the operator's live tmux server — the server holding every worker pane. An offline evaluation instrument must not carry that blast radius. The operator runs the drill against a green cell.)
- The order cites `projects/omegahive/orders/2026-07-28-cli-qol.md`, which the attempt did not have. Judge the work against the order's own text.
- The order cites `projects/omegahive/reports/2026-07-28-cli-qol-result.md`, which the attempt did not have. Judge the work against the order's own text.

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
