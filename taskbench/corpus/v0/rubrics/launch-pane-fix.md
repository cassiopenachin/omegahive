# Grading rubric — launch pane fix — tmux window by name, fail-safe half-launch

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **repro-evidence** — The result names the collision it reproduced and the state that collision stranded, and shows the allocation working afterwards.
- **runbook-half-launch** — Dead-worker recovery documentation gains a half-launched-task case: how to recognise it, why re-running the launcher is wrong, and how to finish by hand.
- **name-collision-refusal** — A window already named for this task is refused with a message naming the recovery path — never a second window.
- **bash-syntax** (checked mechanically) — the launcher parses
- **shellcheck** (checked mechanically) — shellcheck -x clean on the loop scripts, per the DoD

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-event-model** — No event-model, board-guard, or adopt-logic changes.
- **no-registry-file** — No registry file — the pane name stays the registry.
- **no-tmux-conf-assumptions** — No .tmux.conf assumptions (base-index, renumber-windows); name-only allocation must hold under any config.

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
