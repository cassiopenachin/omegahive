# Grading rubric — sole-write-path — the gateway becomes the only writer, and the manifest gets teeth

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **rollback-rehearsed** — A scripted rollback exists beside the forward migration and the report shows it was run — revoking, dropping both roles and un-recording the migration — and re-applied cleanly afterwards. The order refuses a durable cutover until the rollback has been rehearsed.
- **scan-asymmetry** — The credential-scope check is asymmetric as the order specifies — a key present in a service and absent from its manifest row exits non-zero naming the service and the key name; a declared key the service lacks is reported and does not fail — and it gained no exception list.
- **scan-prints-no-values** — The scan reads, prints, logs and diffs key NAMES only. The report shows its full output and that output is safe to paste into a shared terminal.
- **consumers-re-pointed** — Write paths connect as the writing role, read paths as the reading role, and the report demonstrates it rather than asserting it from a code read.
- **deploy-checks-disposition** — The conditional deploy-checks trigger the order names is executed and stated — either the scan is wired there and the named Postgres-bounce defect is fixed in the same change, or the scan landed elsewhere and the report says the trigger did not fire. Silence is the one unavailable outcome.
- **docs-marked** — RUNBOOK is updated where the operator surface changed, and the operations document's Phase 2 gate item is marked with this task's evidence.
- **cutover-in-to-operate** — The result report's To operate carries the durable cutover procedure and its rollback as steps the operator can run without reconstructing anything from the session.
- **result-report** — A result report exists under the project's reports directory carrying What shipped / How verified / To operate / Reflection, and its claims match what the attempt actually did.
- **two-role-property** (checked mechanically) — the order's definition-of-done (b), asked of the database rather than of the diff: the candidate's own migrations are applied to a scratch database, then a direct INSERT as the read role must fail with insufficient_privilege, an append as the gateway role must succeed, the read role must still read, and the gateway role must be unable to delete an event, rewrite its actor, type or run, or do DDL
- **pytest** (checked mechanically) — the suite green — the order's no-regressions bar, minus three modules that have since stopped being replayable. Those three assert against a live wall clock and fixture data that has now aged out of the window they check, which a later task fixed by injecting a clock; at THIS baseline there is no clock to inject, so they fail at the pre-task state and at the accepted outcome alike. Excluding them is named here rather than discovered mid-batch, and it tells a candidate nothing: the exclusion is about the calendar, not about the work
- **ruff** (checked mechanically) — lint clean
- **mypy** (checked mechanically) — types clean
- **shellcheck** (checked mechanically) — shellcheck -x clean on the deploy-path scripts. A no-regression gate: it is clean at the pre-task baseline too

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **gate-item-one-only** — This is the first gate mechanism only. No per-seat credentials, no credential-to-identity derivation, no role-from-secret, and no unix-user cutover.
- **no-durable-cutover** — The worker never mutates the durable stack. Everything is proved against an isolated test database and a transient branch stack, and the state found is restored.
- **no-scan-exceptions** — The scan gains no exception list. A legitimately undeclared platform variable is fixed by adding the manifest row.
- **no-review-authority-change** — Review and instrument authority are untouched; nothing changes who may emit review.passed.
- **no-event-model-change** — No event-model, board-guard, or adopt-logic changes.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- Apply the durable cutover on the running deployment. (The order itself reserves this to the operator and forbids the worker from doing it. An offline replay could not do it and must not.)
- Run the credential-scope scan against the live stack's running containers. (Requires the running deployment, which this instrument never touches. The scan's SEMANTICS are still graded — the order states them as an asymmetry, an injected over-scope key and a never-print-values constraint — from the candidate's own tests and evidence, as a rubric item rather than as a verifier.)
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
