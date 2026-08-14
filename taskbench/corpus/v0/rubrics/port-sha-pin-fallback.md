# Grading rubric — port_library_sha pin — resolver chain instead of git dependency

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **compose-passthrough** — The environment variable is passed through on the shared compose service anchor with an empty default — recorded, not required.
- **validation-not-weakened** — Record validation is neither weakened nor duplicated: empty is still refused.
- **pytest** (checked mechanically) — the host suite green
- **resolver-property** (checked mechanically) — the order's property against the candidate's own code: the pin resolves non-empty with a repo present, with only the environment variable, and with neither; and an empty pin still fails validation

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-git-in-image** — No git binary added to the image; no .git copied into it.
- **no-weakened-validation** — Empty stays refused.
- **nothing-beyond-the-pin** — Nothing beyond this pin.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- `docker compose run --rm test` fully green on the deployment host. (Needs a built image and a running compose stack for the candidate's own tree. The runner does not provision infrastructure for a candidate; the host suite is run instead and the in-container leg is the operator's.)
- The advertised in-container health check passes in a fresh deployment.

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
