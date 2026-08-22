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

## Dry-run the preconditions without spending anything

```bash
uv run --frozen taskbench preflight --config <a generated config> \
  --work-root /tmp/taskbench-dry --out /tmp/taskbench-dry-records
```

Use this, **not** the launcher, when you only want to know whether the environment agrees.
The launcher is the batch: it runs preflight and then immediately starts calling models. (I
learned that the expensive way — running the launcher "to check preflight" burned a partial
candidate session before it was killed.)

## Run the approved batch

One command, no arguments, from the worker's clone. Running it **is** the approval — it never
asks again, and it starts spending as soon as preflight agrees:

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

---

# Qualification: running the five candidate bundles (HIP-1 M1b part two)

Everything above describes running *one* bundle against the corpus. This part describes the
qualification study: the incumbent plus five candidate bundles, three of which reach their model
through OpenRouter and are therefore scored on the gateway's own accounting rather than on
anything a harness reports about itself.

**Nothing here qualifies anything.** These commands produce records; the M1c designation is an
operator act in a committed disposition, after the checkpoint at the end of the result report.

## The one credential, and where it lives

`OPENROUTER_API_KEY` is the single operator-owned secret for all three direct-cost arms. It
reaches every process in this study **through the environment and through nothing else**:
taskbench never reads it from a file, never writes it into a generated config, never puts it in
argv, and never prints it — not its value, not its length, not a prefix.

```bash
export OPENROUTER_API_KEY=...        # from your own secret surface
```

Two places derive a differently-named variable from it, both because a harness demands it and
both under a stated condition:

- `cell-claude-openrouter.sh` exports `ANTHROPIC_API_KEY` **inside the cell process**. Nothing
  is persisted.
- `cell-reasonix.sh` writes it into a mode-0600 `.env` inside that cell's private, disposable
  `REASONIX_HOME`, because Reasonix v1.25.2 refuses inherited provider-key environment
  variables and reads only that file. It is created under `umask 077` and removed on EXIT, INT,
  TERM and HUP. Neither wrapper may use `exec`: `exec` replaces the shell's process image and
  discards the EXIT trap, which is how an earlier revision left that file on disk in every cell
  root.

## Step 1 — prove the routes, before anything spends

```bash
taskbench/launch/qualify-setup.sh
```

No arguments. Free local checks first — the scored instrument against the pinned revision, the
corpus by content hash, the incumbent record, every harness build — then the gateway: both
presets, both endpoint capability sets, and a **live validation of the receipt recorder**. That
last part makes four sixteen-token calls (one direct and one through the recorder, per preset)
and reconciles both against `/generation`, because the order requires the recorder to be proved
against a real gateway response before any scored call and that is not a claim code can make
about itself.

It writes `~/work/taskbench/qualify-preflight/qualify-preflight.json`, recording what was
**observed** — resolved models, upstreams, endpoint capabilities, the canonical preset config
that was hashed, each generation receipt — not merely pass or fail.

**If it refuses, no batch may run.** Read the disagreeing checks with their observations. If the
block is a missing usage or receipt surface, the remedy is to stop and ask: introducing a
measurement proxy after scored calls begin is the failure this whole preflight exists to
prevent, and a bundle that cannot prove its own accounting is recorded `unreachable` rather than
rerouted through another provider or harness.

**A hash disagreement with every field check green is probably not a moved preset.** The two
pinned config hashes were computed by an earlier session under a canonicalization rule this code
does not share. The check records the canonical bytes it hashed beside the result — compare
them, and settle it with a decision rather than re-pinning to whatever today's fetch returned.

## Step 2 — one signed batch per bundle

Running one **is** the approval for that batch's spend. It never asks again.

```bash
taskbench/launch/wave-1-haiku-claude-code.sh
taskbench/launch/wave-2-luna-codex.sh
taskbench/launch/wave-3-deepseek-paired.sh     # BOTH DeepSeek arms, one signature
taskbench/launch/wave-4-muse-claude-code.sh
```

Each one, in order: prints its resolved non-secret config; re-checks its preset and endpoint
(a preset is editable from a web page, so a check from an hour ago proves nothing); runs a
**smoke** — one disposable read/edit/test loop using the exact argv the batch will use, with the
five-minute diagnostic pulse — and stops the batch if it is not green; then runs preflight and
the five cells, leading with `docs-triage`.

**`docs-triage` first is a pause point.** When it finishes, stop and look. It is never permission
to score a partial bundle as adequate.

Wave 3 signs both arms at once deliberately: they are one experiment, and signing them separately
would let one arm run under conditions the other did not. It writes its schedule — adjacent
matched pairs, alternating lead by task — to the work root *before* the first call, so a
reordering after the fact is visible to an audit.

## Step 3 — read the money

```bash
uv run --frozen taskbench gateway-totals <record>     # per-cell receipts, rolled up
uv run --frozen taskbench matrix \
  --bundle incumbent=taskbench/records/2026-08-13-incumbent-fidelity-v0-1-2 \
  --bundle haiku-claude-code=<record> \
  --bundle luna-codex="UNREACHABLE:<why>" \
  --out qualification-matrix.md
```

`gateway-totals` reads the **per-cell** receipt files rather than the record-level one, because a
record can be assembled over several sittings — the paired wave runs one task at a time to keep
its arms adjacent, and a resumed batch carries conclusive cells forward verbatim. Each sitting's
recorder only ever saw its own calls.

An `UNREACHABLE:` bundle is carried into the matrix with its reason rather than omitted. A bundle
that could not run is a result of this study, not an absence from it.

## What the numbers do and do not mean

- **Neither Claude Code's `total_cost_usd` nor Reasonix's metrics is gateway spend.** The first
  is a harness-local price-table figure labelled `firstParty`, which describes Claude Code's
  request path rather than GMICloud or Meta; the second does not exist. For the three OpenRouter
  arms, spend comes from the receipts and from nowhere else.
- **Codex reports no server-resolved model id.** Luna's cells carry the requested string and an
  explicit note that no resolved identity was available. The launch alias is not promoted into
  one.
- **Haiku and Luna run on subscriptions**, so there is no per-run price for them at all. It is
  reported absent, never as zero and never as a price-table estimate.
- **A total is reported with its coverage.** A field missing from any one receipt makes that
  total unknown rather than a sum over the subset that carried it, and a capture the recorder
  could not drain is labelled a floor.
- **One task is twenty percentage points.** No figure is quoted finer, no population or
  general-coding claim is made, and ties are left tied.

---

# The middle-tier study: corpus v1 and the reviewer corpus (HIP-1)

Everything above is corpus v0.1 — eight small, bounded orders, on which the incumbent and a
cheap candidate both finished 5/5. That instrument saturated. It can qualify a cheap route
for bounded low-complexity hive work; it cannot say whether a middle tier can carry ordinary
tenant work, and it says nothing at all about review.

This part describes the two instruments that ask those questions. They are **separate
five-case instruments**, not a grid: five worker cells and five reviewer cells, run in one
batch and read apart.

## What is in them, and what they are not

**Worker corpus v1** — five larger orders, none of them bounded-class:

| task | project | shape | what makes it hard to replay |
|---|---|---|---|
| `cli-qol-2` | omegahive | shell tooling | multi-script operator-loop behaviour and a git-history verifier |
| `hive-mcp` | omegahive | API + service | an API/service/package boundary and an external protocol client |
| `sole-write-path` | omegahive | python service | database roles, deployment consumers, a credential scan |
| `fol-pln-mapping` | pln-benchmarks | research + design | niche-language design plus executable micro-verifications |
| `pw-writeup` | pln-benchmarks | technical writing | source-grounded synthesis for a reader outside the project |

Reserved and never executed: `worker-turns`, `pw-d5-comparable`, `result-revision`.

**Three of five are hive infrastructure and two are tenant research and writing.** Five
tasks give 20-point resolution. Do not read an aggregate over them as a claim about software
work in general, and do not read the two tenant tasks as a claim about applied PLN research.

**Two of the three reservations are PARTIAL, and `taskbench corpus` says so.**
`pw-d5-comparable`'s accepted merge *is* `pw-writeup`'s pre-task base, and `result-revision`
merged before `hive-mcp` branched — so each of those reserved tasks' accepted code is
already in a held-in task's baseline. The reservation still keeps them out of every
execution, launch packet, canary choice and tuning pass. It no longer makes them a cold read
for a model that has run the contaminating cell. That is a fact about the history, not a
choice made here, and it is recorded rather than relied on quietly.

**Reviewer corpus `review-v1`** — five frozen historical states: one that shipped unchanged,
and four that a real review sent back. The clean one is the only measurement of false
positives the instrument has; without it, a reviewer that flags everything scores well.

A reviewer packet contains the launch-visible order, the diff as it stood at that moment,
whole artefacts, the complete source set the work cites, and the output of checks run
against that state. It never contains the review that happened, the repair, the expected
disposition, or anything the repository did afterwards.

## Before you launch

```bash
cd ~/src/SNET/omegahive
uv run --frozen taskbench corpus --corpus taskbench/corpus/v1
uv run --frozen taskbench validate-corpus --corpus taskbench/corpus/v1
uv run --frozen taskbench review-corpus
uv run --frozen taskbench validate-review-corpus
```

`validate-review-corpus` runs **every must-find's witness at both ends**: the check must fail
at the packet's own state and pass at the state the repair reached. Gold that cannot do that
is a recollection, and this refuses it. It needs both source repositories and a database.

To prove the worker graders the same way — each gate red at its task's pre-task baseline and
green at its accepted outcome:

```bash
uv run --frozen taskbench endpoint-witness --corpus taskbench/corpus/v1 \
  --out /tmp/endpoint-witness.json
```

A gate that is green at both ends is reported as a no-regression bar rather than a
discriminator; a task where *nothing* discriminates is a refusal.

## The one command that cannot spend

Use this when you want to know whether the environment agrees and nothing else. It cannot
call a model: no code path in it launches anything.

```bash
cd ~/src/SNET/omegahive && uv run --frozen taskbench middle-preflight \
  --config       ~/work/taskbench/<record>/reviewer-runner-config.yaml \
  --worker-config ~/work/taskbench/<record>/worker-runner-config.yaml \
  --work-root    ~/work/taskbench/<record> \
  --out          taskbench/records \
  --expect-worker-hash sha256:319c051127ee469318f109812479c6bc2280cd38c445330d81b21a69070ab43d \
  --expect-review-hash sha256:94cd90ecea2f50d4e5b8a4a2a99d2f2aadb70f79c33a1a25af0861c5e471e6ab
```

The two configs are written by the launcher below; run it once to generate them, or write
them by hand from `taskbench schema`. Exit 3 means it refused, and it lists every
disagreement at once.

## The one command that runs the study

One command, no arguments, from the worker's clone. **Running it is the approval** — it never
asks again, and it starts spending as soon as preflight agrees.

```bash
taskbench/launch/middle-seed-fidelity.sh
```

Four stages, in this order:

| stage | what it does | what stops it |
|---|---|---|
| 1 | preflight for both instruments | any disagreement — exit 3, nothing written, nothing called |
| 2 | one fresh audit of each reviewer packet's proposed must-find set | a dispute — exit 4, nothing scored |
| 3 | five worker cells on corpus v1 | recorded; a red here does not stop stage 4 |
| 4 | five reviewer cells on the frozen packets | recorded |

**Stage 2 is the unusual one.** A must-find set assembled by whoever built the corpus is one
reading of history, so before any scored cell a fresh strong-model pass is shown each packet
and the proposed answer key, and asked only whether the order, the diff and the repair
support it. It **never rewrites gold**: the corpus is frozen, and a corpus that can be
adjusted after seeing an objection measures the adjuster. A dispute is a decision — either
the evidence supports the must-find and the audit is wrong, or it does not and the corpus
needs a new version.

**Every session this batch opens is the incumbent.** It calls no candidate model at all.

## What green means, for each instrument

**Worker fidelity is green only at 5/5 FINAL cells.** First-shot and after-repair are
recorded separately and both are reported: a model that needs rescue must not read as a clean
generator. A cell the environment killed is `inconclusive`, not red — it is not a model
result, and re-running the launcher carries the conclusive cells forward.

**Reviewer fidelity is green only when all three hold:**

1. every must-find defect at `critical` or `approach` severity was found;
2. at least four of five dispositions are correct;
3. the packet that shipped unchanged drew no unsupported high-severity finding.

Read `aggregate.md` for the split that matters: **disposition** and **must-find coverage**
are separate columns. A reviewer that returns `required_change` for a reason that is not the
reason has answered correctly by accident, and only the second column tells you which
happened. An extra finding is reported and never counted against a reviewer — except on the
clean packet, where an unsupported one is the failure being measured.

Read `cells/<id>/probe.json` before trusting any cell. Its reviewer ran in a **fresh home**
seeded with nothing but the two files its own tool needs to authenticate, and the probe had
to fail to read three things — the work-root canary, that packet's own gold, and the
operator's own configuration — before the reviewer was launched at all. A cell whose probe
failed was never run, and its score is absent rather than red. (Corpus v0.1's reviewer
inherited an operator `$HOME` carrying transcripts of the tasks it was grading. Nothing here
inherits a home.)

**An honest red stops at diagnosis.** Do not weaken a packet, a rubric or a pass rule to make
the incumbent green. That is the one repair this study cannot make.

## Interruption, environment failure, and resuming

- **Ctrl-C** keeps every completed cell and every raw log exactly as they are. Re-running the
  launcher opens **new** records that supersede the partial ones; nothing is overwritten.
- **An environment-killed cell** — a rate limit, an auth failure, a session that died for a
  reason that is not judgment — is recorded `inconclusive`. Re-running the launcher carries
  every conclusive cell forward verbatim and re-runs only those.
- **Only the first two of those may resume.** An honest semantic red — the model produced
  work and the work was wrong — is never re-rolled. Re-running a genuine red for a better
  number is the one thing the resume path refuses.
- **A rerun must be able to name the package defect that made it necessary.** One that
  cannot is a model result being re-rolled.

## The one dependency the runner will not fetch

`fol-pln-mapping` measures a public dataset, and the runner never reaches the network. Place
the two files once, at the digests the corpus pins; preflight verifies both and refuses on a
mismatch:

```bash
mkdir -p ~/work/taskbench/sources/folio-dataset
curl -fsSL -o ~/work/taskbench/sources/folio-dataset/folio-train.jsonl \
  https://raw.githubusercontent.com/Yale-LILY/FOLIO/main/data/v0.0/folio-train.jsonl
curl -fsSL -o ~/work/taskbench/sources/folio-dataset/folio-validation.jsonl \
  https://raw.githubusercontent.com/Yale-LILY/FOLIO/main/data/v0.0/folio-validation.jsonl
sha256sum ~/work/taskbench/sources/folio-dataset/*.jsonl
#   008d34b750d31fa7f014e953228adf4db81ec34bbda9e7f67c96c60438d1e6b2  folio-train.jsonl
#   6922c988ef10987bd6545568ee8e63e897af80994591fa20539767da58f8e3d1  folio-validation.jsonl
```

A digest mismatch means the upstream moved. **Do not re-pin to whatever a fetch returns
today** — that silently changes what the benchmark measured. The digests are the ones the
closed task recorded at launch era and checked on every load.

`TASKBENCH_SOURCE_CACHE` moves the cache if you want it elsewhere.

## Limits of these two instruments, stated once

- **Five cases each.** Twenty-point resolution, no population, no ranking.
- **The worker corpus is three-fifths hive infrastructure.** The two tenant tasks are a
  second repository, a second ecosystem and two work shapes the hive side does not contain.
  They are not evidence about applied research in general.
- **The reviewer corpus grades against four defect classes and one clean case.** A reviewer
  that scores well here has been measured on five historical states, not on review.
- **`fol-pln-mapping` cannot re-verify its published comparison figures**, because those
  sources never had launch-era digests recorded and there is nothing to pin a snapshot
  against. That leg is declared not-executed in the manifest and excluded from the rubric; the
  two figures the order itself states are still checked.
- **`hive-mcp` and `sole-write-path` each leave a leg to the operator** — a desktop-bridge
  round-trip and a scan against live containers. Both are declared, and the rubric tells the
  reviewer not to mark an attempt down for them or credit a claim to have done them.
- **Two of the three worker-corpus reservations are partial**, as above.
