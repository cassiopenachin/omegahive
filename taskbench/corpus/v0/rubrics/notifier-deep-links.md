# Grading rubric — notifier deep links — buzz to read without SSH

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **byte-identical-unset** — With the base URL unset, every message renders byte-identically to before — the link is additive, never structural.
- **config-not-secret** — The new variable follows the config-not-secret pattern: compose environment with an empty default and an entry on the allowed list, never in the env file.
- **pytest** (checked mechanically) — the whole suite green
- **ruff** (checked mechanically) — lint clean
- **no-token-in-output** (checked mechanically) — the notifier suite, including the token-absence checks

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **outbound-only** — Outbound only — no inbound surface, no bot commands, no ack path, no new trigger types.
- **no-ui-changes** — No UI changes.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- One real notification on the phone opens the UI page over the tailnet. (Deliberately not mocked. A stub would stand in for the deploy, not for anything the candidate wrote: everything under its control — the link is present and correct when the base URL is set, the render is byte-identical when it is unset, trailing slashes normalize, escaping holds, the token appears nowhere — is already the order's own Tests section and is checked deterministically. What remains is that the deployed service is reachable from a physical device over the tailnet, which is a deploy check the order itself calls the operator's close-time act.)
- One real notification on the phone opens the UI page over the tailnet — the deploy check, not a code check.

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
