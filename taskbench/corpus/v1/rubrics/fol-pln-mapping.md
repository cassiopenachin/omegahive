# Grading rubric — fol-pln-mapping — FOLIO's FOL inventory vs lib_pln, encodings decided or gaps declared

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **encodings-verified-not-argued** — Every encoding decision at or above the order's frequency threshold rests on a runnable micro-example whose expected value was captured from the runtime before being asserted, not on an argument about what the rule set ought to do.
- **gaps-compared** — Every declared gap compares the candidate treatments the order names — a rewrite, abstaining on the item, excluding with a count — and recommends one, with the reason.
- **semantics-not-score** — No encoding is argued for on the grounds that it scores better. The order disqualifies that argument by construction because this benchmark family rewards weakening the reasoner; reporting the effect is fine, reasoning from it is not.
- **errata-recorded-not-fixed** — Dataset annotation quirks encountered are recorded with a count and examples, and are not repaired.
- **arithmetic-checked** — The run protocol's cost arithmetic is checked against the dataset's actual size rather than carried over from the previous benchmark, and the note says which figures it re-derived.
- **escalation-honoured** — If a construct above the order's escalation threshold has no sound encoding candidate at all, that is raised as a question rather than resolved unilaterally.
- **result-report** — A result report exists under the project's reports directory carrying What shipped / How verified / To operate / Reflection, and its claims match what the attempt actually did.
- **note-answers-the-converter-order** (checked mechanically) — the holes a converter order would fall into, checked against what this order enumerates: a decision or declared gap for every FOL construct it lists, a measured frequency table, both reporting arms with a realization path that does not edit the vendored runtime, a run protocol with scale, budget, timeout and liveness canaries, micro-examples that assert rather than merely run, and a seeded report section carrying the two published baselines the order quotes with at least one source
- **pytest-offline** (checked mechanically) — the repository's own offline suite green — the no-regressions bar. The runtime-marked tests are excluded here because they shell out to the pinned runtime and are the micro-examples' own leg, judged in review from the evidence the attempt records

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-converter-no-scale-runs** — No converter implementation and no dataset-scale runs — micro-examples only.
- **no-vendor-edits** — No edits under the vendored runtime. A derived rule file, if recommended, is specified in the note with its provenance and regeneration, never built here.
- **semantics-only** — Encoding choices are justified by FOL semantics and micro-verification only.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- Re-verify the published neurosymbolic comparison figures at their sources. (Needs fresh retrieval of public papers, which this offline instrument does not supply — and unlike the dataset, their launch-era digests were never recorded, so there is nothing to pin a snapshot against. The two figures the ORDER itself states are required and checked; the rest of section 2 is graded for shape and for honest marking, never for having been verified. A future corpus version can close this by pinning a source packet at first fetch.)
- Record the two-arm recommendation in the workspace decisions log for operator ratification. (A write to the live hive workspace. The instrument never touches it, and the recommendation itself IS in the note and is graded.)
- Post the prepared upstream filings. (The order already leaves posting to the operator. What the attempt PREPARED — repro plus suggested text — is in the diff and is graded.)
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
