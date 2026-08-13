# Grading rubric — CLI QoL sweep — four small pains from waves 1 and 2

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **derivation** — DoD (a): a project whose directory name differs from its repo basename launches with no environment override, and the override still wins.
- **title-fallback** — DoD (b): a heading-less order launches, titled by task id.
- **session-guard** — DoD (c): an unsafe session name is refused before any spine write.
- **close-score-commit** — DoD (d): close then score then commit lands in the sandbox workspace repo, and an injected scoring failure leaves the close intact with a loud complaint.
- **autonomous-default** — DoD (e): the issued worker invocation carries the autonomous flag by default and respects the override.
- **shellcheck** (checked mechanically) — shellcheck -x clean

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-event-model** — No event-model, board-guard, adopt-logic, gateway, notifier or web-UI changes.
- **generators-write-metrics-only** — The generators write only their own files under the project's metrics directory — never order or report bodies, never spine emits.
- **score-never-fails-close** — A scoring failure may never make a close look failed.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- `scripts/hive-tooling-drill.sh` green, including this task's new cases. (The loop drill emits scratch spine events, which this instrument's own scope forbids, and it drives tmux. At this task's baseline the drill predates the tmux isolation that the task itself introduces, so running it necessarily creates sessions on the operator's live tmux server — the server holding every worker pane. An offline evaluation instrument must not carry that blast radius. The operator runs the drill against a green cell.)

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
