# OmegaHive — task-replay benchmark: operator guide

**Status:** reference — operating guide for the `taskbench` CLI, corpus v0 (2026-08-12).
Companion to HIP-1 milestone M1b. Distinct from
[`omegahive_c2_battery_spec.md`](omegahive_c2_battery_spec.md), which specifies the `qual`
coordinator battery: `qual` measures whether a model can drive the board loop; `taskbench`
measures whether a model can close a written order. They share record idioms and nothing
else, and neither is a relabelling of the other.

## What this is for

Replay a closed, bounded order against a candidate agent in a fresh world that does not
contain the answer, then grade the attempt on two independent legs: deterministic checks,
and one blinded review by a strong model that never learns which candidate produced the
work. The instrument is **evaluation-only** — it emits nothing to the spine, never points an
agent at the live workspace or the canonical checkout, and handles no credentials.

Corpus v0 holds **five held-in** tasks and **three held-out**. The held-out three are
reserved: they may not be executed, entered into a qualification launch packet, used to
choose a canary, or used to tune a prompt or a rubric. Every qualification path refuses
them by id. A task once used to tune can never be held out again.

## Before you launch

```bash
cd ~/src/SNET/omegahive
uv run --frozen taskbench corpus            # what is in v0, with the reserved set marked
uv run --frozen taskbench validate-corpus   # manifests, rubrics, grading files, frozen hashes
```

`validate-corpus` must print `valid` before a batch. A hash drift means the corpus moved
after it was frozen; per the stop-line that increments the corpus version and invalidates
every earlier cell, so investigate rather than re-freeze.

**Environment the five held-in cells need on this host.** Missing any of these does not
produce a wrong answer — it produces a red cell whose diagnosis is the environment, which is
a fidelity failure and stops the milestone at diagnosis.

| Need | Used by |
|---|---|
| `shellcheck` | `launch-pane-fix`, `instrument-teeth` |
| a reachable Postgres for the test suite | `run-registration` — the suite makes its own per-run scratch database |
| `uv`, with a warm cache or network | `run-registration` |
| `podman`, with `docker.io/library/swipl:9.3.33` already in local storage | `ptc-revalidate` — the runner never pulls |
| local clones of `PeTTaChainer` and `PeTTa` | `ptc-revalidate` — snapshotted into the cell as git bundles |
| `bwrap` | every cell — the cold-reader sandbox |

No cell needs the stack, the durable Postgres, or tmux. `scripts/hive-tooling-drill.sh` is
deliberately **not** an offline verifier: it emits scratch spine events, which this
instrument's scope forbids, and at `launch-pane-fix`'s baseline it predates the tmux
isolation that very task introduces, so running it creates sessions on the server holding
every live worker pane. It is an operator leg — run it yourself against a green cell.

## Materialize one cell (dry run, no model)

```bash
uv run --frozen taskbench materialize docs-triage \
  --root /tmp/taskbench-check/docs-triage \
  --workspace-repo ~/workspaces/hive
```

Prints the baseline, the count of workspace inputs and dependency snapshots, and
`leakage scan clean`. A `LEAK` line is a hard stop: the candidate root can see the future
and the cell must not run. Look at `<root>/TASK.md` to see exactly what a candidate is
told, and `<root>/code` to confirm it holds one commit and no remote.

## Run the approved batch

One command, no arguments, from the worker's clone. Running it **is** the approval — it never
asks again:

```bash
taskbench/launch/incumbent-fidelity.sh
```

It reads `claude --version` at launch rather than trusting a remembered value (the binary is
a symlink that moves), picks a fresh record id so nobody can overwrite history, writes the
runner config itself, and hands both to `taskbench run`. **Nothing calls a model until
preflight agrees**: corpus hash against a literal, held-in set, clean non-canonical checkout,
every tool a cell's verifiers need, every pinned sha resolvable, the swipl image present,
fresh writable destinations, argv with no shell metacharacters, and a TCP+TLS probe proving
the reviewer's sandbox can actually reach the API. A refusal lists everything wrong at once
and writes nothing.

Three outcomes, and the script says which one you got:

| Exit | Meaning |
|---|---|
| 0 | all five cells ran and the record validates — read `aggregate.md` |
| 3 | preflight refused. No model was called, nothing written, nothing to clean up |
| other | the batch stopped. Every completed cell and raw log is kept; nothing was overwritten |

Interrupting it (Ctrl-C) prints the same guarantee: the partial record and its cells stay
exactly as they are, and re-running opens a **new** record that supersedes them.

The model is requested by alias (`--model opus`), never by a guessed identifier. Each run's
own end-of-run report supplies the **resolved** model id, provider and token counts, and
that is what the record pins — an alias is a request, and the two can differ without anyone
noticing.

## Re-run one cell after a package repair

Records are immutable. A re-run is a **new** record that names the one it supersedes, so the
failed cell and its diagnosis both survive:

```bash
uv run --frozen taskbench run \
  --config taskbench/configs/incumbent-fidelity.yaml \
  --record-id incumbent-fidelity-2 \
  --tasks run-registration \
  --supersedes 2026-08-12-incumbent-fidelity
```

The rerun's `RERUN.md` asks for the package defect that made it necessary. A re-run that
cannot name one is a model result being re-rolled.

## Validate and aggregate

```bash
uv run --frozen taskbench validate-record taskbench/records/2026-08-12-incumbent-fidelity
uv run --frozen taskbench aggregate       taskbench/records/2026-08-12-incumbent-fidelity
```

`validate-record` checks the config pins and every cell's artefacts, and fails a record that
holds a held-out task — the reservation being broken is a record-level defect, not a note.

## Reading a record

```
config.json     corpus version + content hash, harness sha, both label sets, host
cells.json      cell id → task id and labels: the de-blinding map, operator-side only
cells/<cell>/   run.json  — labels, exit, wall time, progress facts, reported tokens
                verdict.json — the two legs, separately, and one line saying what decided it
                candidate.patch, verifier/*.log
                review/packet-manifest.json — exactly what the reviewer was shown
                review/probe.json — whether the cold-reader boundary held for this cell
aggregate.md    per-task table plus per-task caveats
```

Read `review/probe.json` first on any cell you intend to trust. `ok: true` means the review
process could read every declared input and could **not** read the parent-workspace canary
or the operator-only copy of the historical solution. If the probe failed the reviewer was
never launched, and the cell is red for a missing leg rather than for a model's opinion.

`run.json`'s progress facts are honest about their gaps: a fact the harness does not expose
stays `unknown` and `missing_surfaces` names the surface that would have carried it. Treat
an `unknown` as a note about the harness, never as a zero.

## What a red cell means

| Symptom | Read it as |
|---|---|
| deterministic leg red, review green | the attempt missed a checkable requirement |
| deterministic green, review red | a would-have-shipped defect the checks do not cover; read `review/verdict.json` for the evidence |
| review leg `not run` | the cold-reader probe failed. Fix the sandbox binds — do not widen them until the probe passes by seeing more |
| a verifier that could not execute | environment, not model. `verdict.json` carries the error verbatim |
| `terminal_error` set on the run | the session died for a reason that is not judgment (credit, auth, rate limit, context). Diagnose; do not score |

## Limits of corpus v0, stated once

- **Five tasks give 20-point resolution and a fidelity check.** They do not measure a stable
  population and no ranking may be built on them.
- **The corpus is hive-infrastructure-heavy**: seven of eight tasks are hive tooling, one is
  tenant work. The tenant task is a cross-project sentinel, not evidence that the corpus
  represents applied PLN research.
- **Outward-facing legs are staged, not excluded.** `ptc-revalidate` asks for an upstream
  issue filing, and what makes a filing good is what it says — so the candidate really runs
  `gh`, a recording stub ahead of the real tool captures argv and body verbatim, and the
  blinded reviewer grades the content from `artefacts/outward/`. Nothing leaves the machine.
  The runner refuses the cell if the command name does not resolve to the stub. This is an
  evaluation stub, not a network jail: it shadows a command name and claims nothing more.
- **One leg genuinely stays excluded.** `notifier-deep-links`' phone round-trip is a *deploy*
  check — everything the candidate wrote is already covered deterministically by the order's
  own Tests section, and a stub would stand in for the deployment, not for any of its code.
  It is declared with that reasoning, and the declaration is inside the content hash, so it
  cannot be edited after a cell has run without incrementing the corpus version.
- **`launch-pane-fix` carries the weakest deterministic leg** of the five: shell syntax,
  lint, and the requirement that the order's named deliverables were actually changed. Its
  real check — the loop drill — is an operator leg for the reasons above, so the blinded
  review carries most of the weight on that cell. Read its `review/verdict.json`, and run
  the drill yourself before merging anything this cell blesses.
- **Existence is not evidence of work.** Several tasks' deliverables are already in their
  baseline, so each manifest also names `required_changes`: globs the attempt's diff must
  touch. Every one is a path the order itself names; none is read off the historical patch,
  which would be the exact-diff scoring the stop-lines forbid.
