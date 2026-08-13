# Task-replay aggregate — incumbent-fidelity (2026-08-13)

Corpus `v0` (`sha256:b9f14e1be7ff…`) · candidate **anthropic/claude-opus-5** on `claude-code-2.1.231` · reviewer **opus**.

Requested as `--model opus`; the identifier above is what each run's own report said it resolved to. An alias is a request, not an identity.

**3/5 task-level verdicts green.**

| task | project | work shape | deterministic | review | verdict | because |
|---|---|---|---|---|---|---|
| docs-triage | omegahive | docs-reorg | pass | pass | green | all legs green |
| instrument-teeth | omegahive | shell-tooling | pass | pass | green | all legs green |
| launch-pane-fix | omegahive | shell-tooling | pass | FAIL | RED | review leg: 1 would-have-shipped defect(s) |
| ptc-revalidate | pln-benchmarks | external-verification | pass | pass | green | all legs green |
| run-registration | omegahive | python-service | FAIL | FAIL | RED | deterministic checks failed: registration-property; review leg: 2 would-have-shipped defect(s) |

## Per-task caveats

**docs-triage** — The order supplies the verdict for every surveyed document, the four destination categories with worked examples, and a conservative-on-doubt rule for anything the doc's own content contradicts. Done is checkable by counting: every file in the docs tree appears exactly once in the index, and every cross-reference resolves. No content is rewritten, so there is no authoring judgment in scope.
  - `docs-index-complete`: pass — ok
  - `link-integrity`: pass — ok
  - `nothing-deleted`: pass — ok

**instrument-teeth** — Six numbered scope items, each with its own lettered acceptance case in the DoD, and two pre-decided folds. Every case is a drill assertion; the order even fixes the vocabulary the validator must accept and the two Watch folds' dispositions. Nothing is left for the worker to design: the one wording gap it contains (the phrase that spells the declared-unpredicted disposition) is a naming choice inside a stated case, not a design decision.
  - Excluded leg (`operator`): `scripts/hive-tooling-drill.sh` green, including this task's new cases. — The loop drill emits scratch spine events, which this instrument's own scope forbids, and it drives tmux. At this task's baseline the drill predates the tmux isolation that the task itself introduces, so running it necessarily creates sessions on the operator's live tmux server — the server holding every worker pane. An offline evaluation instrument must not carry that blast radius. The operator runs the drill against a green cell.
  - `shellcheck`: pass — ok
  - `metrics-drill`: pass — ok

**launch-pane-fix** — The order names one defect with a reproduction, one allocation requirement, one ordering requirement, and a drill that must stay green plus two named new cases. The definition of done is a drill run and a doc section, not a design choice: the one seam the order leaves open (whether to reorder seeding before pane creation) it delegates explicitly and asks the worker to state, which is a bounded election between two written options rather than an open design.
  - Excluded leg (`operator`): `scripts/hive-tooling-drill.sh` green, including this task's new cases. — The loop drill emits scratch spine events, which this instrument's own scope forbids, and it drives tmux. At this task's baseline the drill predates the tmux isolation that the task itself introduces, so running it necessarily creates sessions on the operator's live tmux server — the server holding every worker pane. An offline evaluation instrument must not carry that blast radius. The operator runs the drill against a green cell.
  - `bash-syntax`: pass — ok
  - `shellcheck`: pass — ok

**ptc-revalidate** — A verification task with the subjects, the shas, the probe and the output path all fixed in advance: build the pinned environment, run the upstream suites, reproduce one named example, and write the verdict to a named file with a rerunnable script. The worker chooses no design; the answer is whatever the runs say, and the order states in advance that a failed environment build IS the finding.
  - Excluded leg (`not-executed`): Close the stall-ledger row against the report ref. — A write to the live hive workspace. The instrument never touches it.
  - `verdict-shape`: pass — ok
  - `repro-script-present`: pass — ok
  - `subjects-unpatched`: pass — ok

**run-registration** — One behavioural change on one write path, stated as a property (every run touched by an emit carries a generation token from its first event), with the four test cases the order itself enumerates and two named documentation edits. The stop-lines fix the blast radius precisely: no generation semantics, no cursor logic, no schema change beyond what the existing helper does.
  - Excluded leg (`operator`): The `omegahive` row demonstrably exists in `runs`, with query evidence. — Requires a live-spine mutation or a merged-and-deployed image. Both are operator acts that post-date any worker's result, as the closed task's own report records; an offline replay cannot produce them.
  - `pytest`: pass — ok
  - `ruff`: pass — ok
  - `mypy`: pass — ok
  - `registration-property`: FAIL — exit 1 (expected 0)

## What this table is not

5 tasks give task-grain resolution of 20 points and a fidelity check. They do not measure a stable population, and this corpus is hive-infrastructure-heavy: the work-shape column is there so a later reader can see that rather than read one pass rate.

