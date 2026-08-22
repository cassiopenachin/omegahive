# Grading rubric — cli-qol-2 — one close, one verdict, one row; committed answers without the CLI-text bottleneck

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **runbook-updates** — RUNBOOK names the new close syntax, --no-score, the one-row re-score semantics, and the long-form answer flow.
- **drill-cases-read-as-code** — The focused drill cases are real cases a reader can follow — each names the input it sets up and the outcome it requires — rather than a smoke that would pass over the bug.
- **nudge-after-verify** — A verified --sha goes on to perform the same safe resume the text form performs, without rewriting or recommitting the file. The fixture proves the verification and the no-write stop-line; whether the resume itself fires needs a live worker's turn state, so judge it from the code and the drill rather than from a verifier log.
- **restore-commands** — The result report's To operate carries the exact two operator commands that restore the two cross-project calibration rows the context names, and does not run them.
- **result-report** — A result report exists under the project's reports directory carrying What shipped / How verified / To operate / Reflection, and its claims match what the attempt actually did.
- **loop-behaviour** (checked mechanically) — the order's definition-of-done cases (a) through (f), driven against the candidate's own scripts in a disposable fixture: a workspace with its own hub, a project, two orders, a calibration file already carrying the duplicated rows the order names, a recording stub for the hive CLI and a recording stub for tmux. It checks what the scripts DO — refuse before writing, close, score, replace one row, carry a verdict forward, refuse a false clean, and accept or refuse a pushed answer commit — never how they are written
- **shellcheck** (checked mechanically) — shellcheck -x clean on the scripts this order touches. This is a no-regression gate rather than a discriminator: it is clean at the pre-task baseline too

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-new-authority** — No new event type, calibration field, review value, or review authority. The operator still makes the verdict and still signs the answer commit.
- **sha-never-writes** — --sha verifies an already-pushed commit; it never stages, commits, rebases, or pushes, and never accepts an answer embedded in a mixed commit.
- **no-general-drill-rewrite** — No general drill rewrite. The order refuses the fired drill-hygiene trigger to its own separate pass.
- **no-prediction-parsing-change** — No scoring of Named risks, no change to prediction parsing, and no change to the task-versus-final-artefact semantics already decided.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- Run the two operator commands that restore the ptc-revalidate and pw-libpln-slice calibration rows. (A write to the live workspace on another project, which the order itself assigns to the operator rather than to the task. The report must still CARRY both commands, and that is graded.)
- Announce the result on the spine per WORKER.md. (A spine emit, which this instrument's own scope forbids. The report the order asks for IS written and graded — only the announcement is out of reach.)

## Documents the attempt could not open

The order cites these and this replay did not supply them. An attempt that works around the
gap honestly — naming what it could not check — is doing the right thing; an attempt that
cites one as if it had read it is not.

- `projects/omegahive/reports/2026-07-28-cli-qol-result.md` — The direct predecessor's result report, and a controlling ref of this order. It is withheld because `cli-qol` is held out of corpus v0.1, which is still in use: shipping its result report here would spend that reservation by citation, for a task this instrument may still want as a cold regression probe. The scripts the predecessor produced ARE in the code baseline, so what is lost is its narrative, not its work.

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
