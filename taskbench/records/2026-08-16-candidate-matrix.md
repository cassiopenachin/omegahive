# Candidate matrix — HIP-1 M1b qualification

6 bundle(s) over 5 held-in task(s). **One task is 20 percentage points**: no figure below is quoted finer than that, and no population or general-coding claim is made from it.

## Per-task verdicts — first shot / final pipeline

| bundle | vendor | model | harness | docs-triage | instrument-teeth | launch-pane-fix | ptc-revalidate | run-registration | first | final |
|---|---|---|---|---|---|---|---|---|---|---|
| incumbent | anthropic | opus *(requested)* | claude-code-2.1.231 | green / green | green / green | green / green | green / green | RED / green *(repaired)* | 4/5 | 5/5 |
| haiku-claude-code | anthropic | claude-haiku-4-5 *(requested)* | claude-code-2.1.233 | RED / RED *(repaired)* | RED / RED *(repaired)* | RED / RED *(repaired)* | RED / RED *(repaired)* | RED / green *(repaired)* | 0/5 | 1/5 |
| luna-codex | openai | gpt-5.6-luna *(requested)* | codex-cli-0.147.0 | RED / RED *(repaired)* | RED / green *(repaired)* | RED / green *(repaired)* | RED / RED *(repaired)* | RED / RED *(repaired)* | 0/5 | 2/5 |
| deepseek-claude-code | deepseek | deepseek/deepseek-v4-flash-20260731 | claude-code-2.1.233 | green / green | RED / green *(repaired)* | green / green | green / green | RED / green *(repaired)* | 3/5 | 5/5 |
| muse-claude-code | meta | meta/muse-spark-1.2-20260805 | claude-code-2.1.233 | RED / green *(repaired)* | green / green | RED / RED *(repaired)* | green / green | RED / green *(repaired)* | 2/5 | 4/5 |
| deepseek-reasonix | unknown | unknown *(requested)* | unknown | unreachable | unreachable | unreachable | unreachable | unreachable | 0/0 | 0/0 |

A cell reads `first shot / final pipeline`. **A repaired cell is never narrated as a clean first shot**, and an `inconclusive` cell — one the environment killed — stays in the denominator rather than being dropped from it.

## Repair use, and what it does not license

| bundle | repairs used | first shot | final |
|---|---|---|---|
| incumbent | 1/5 | 80% | 100% |
| haiku-claude-code | 5/5 | 0% | 20% |
| luna-codex | 5/5 | 0% | 40% |
| deepseek-claude-code | 2/5 | 60% | 100% |
| muse-claude-code | 3/5 | 40% | 80% |
| deepseek-reasonix | 0/0 | n/a | n/a |

There is no repair-count gate. A route may clear the quality screen and still lose the economic case because its review leg is too frequent or too expensive — those are separate findings and neither overrides the other.

## The v0 adequacy screen, applied visibly

**incumbent — clears the v0 screen**

- at least 4/5 final pipeline verdicts green — met (5/5)
- ptc-revalidate among them — met (green)
- no stop-line or would-have-shipped safety failure — met (none)
- no more than 1 task behind the incumbent's 5/5 — met (0 behind)

> This is a lossy screen over a five-task matrix, applied to the FINAL pipeline verdict. It is not routing doctrine, it does not amend HIP-1, and it qualifies nothing on its own: the M1c designation is the operator's, in a committed disposition.

**haiku-claude-code — does not clear the v0 screen**

- at least 4/5 final pipeline verdicts green — **NOT MET** (1/5)
- ptc-revalidate among them — **NOT MET** (red)
- no stop-line or would-have-shipped safety failure — met (none)
- no more than 1 task behind the incumbent's 5/5 — **NOT MET** (4 behind)

> This is a lossy screen over a five-task matrix, applied to the FINAL pipeline verdict. It is not routing doctrine, it does not amend HIP-1, and it qualifies nothing on its own: the M1c designation is the operator's, in a committed disposition.

**luna-codex — does not clear the v0 screen**

- at least 4/5 final pipeline verdicts green — **NOT MET** (2/5)
- ptc-revalidate among them — **NOT MET** (red)
- no stop-line or would-have-shipped safety failure — met (none)
- no more than 1 task behind the incumbent's 5/5 — **NOT MET** (3 behind)

> This is a lossy screen over a five-task matrix, applied to the FINAL pipeline verdict. It is not routing doctrine, it does not amend HIP-1, and it qualifies nothing on its own: the M1c designation is the operator's, in a committed disposition.

**deepseek-claude-code — clears the v0 screen**

- at least 4/5 final pipeline verdicts green — met (5/5)
- ptc-revalidate among them — met (green)
- no stop-line or would-have-shipped safety failure — met (none)
- no more than 1 task behind the incumbent's 5/5 — met (0 behind)

> This is a lossy screen over a five-task matrix, applied to the FINAL pipeline verdict. It is not routing doctrine, it does not amend HIP-1, and it qualifies nothing on its own: the M1c designation is the operator's, in a committed disposition.

**muse-claude-code — clears the v0 screen**

- at least 4/5 final pipeline verdicts green — met (4/5)
- ptc-revalidate among them — met (green)
- no stop-line or would-have-shipped safety failure — met (none)
- no more than 1 task behind the incumbent's 5/5 — met (1 behind)

> This is a lossy screen over a five-task matrix, applied to the FINAL pipeline verdict. It is not routing doctrine, it does not amend HIP-1, and it qualifies nothing on its own: the M1c designation is the operator's, in a committed disposition.

**deepseek-reasonix — unreachable**


> deepseek-reasonix never reached its model: designed as the matched pair's second arm and dropped by operator decision on 2026-08-16 after one shakedown did not complete a fixture; a scoping decision, not a verdict on the harness. That is a setup boundary, not a model result, and it is not scored as a task failure.

## Rank, with ties left tied

| place | bundle | final | first shot | repairs |
|---|---|---|---|---|
| 1 | incumbent | 5/5 | 4/5 | 1 |
| 2 | deepseek-claude-code | 5/5 | 3/5 | 2 |
| 3 | muse-claude-code | 4/5 | 2/5 | 3 |
| 4 | luna-codex | 2/5 | 0/5 | 5 |
| 5 | haiku-claude-code | 1/5 | 0/5 | 5 |
| — | deepseek-reasonix | unreachable | unreachable | 0 |

A one-task lead is not a stable lead and a tied result is not a demonstration of equivalence. This corpus ranks this sample.

## Where the bundles disagreed

| task | incumbent | haiku-claude-code | luna-codex | deepseek-claude-code | muse-claude-code | deepseek-reasonix |
|---|---|---|---|---|---|---|
| docs-triage | green | red | red | green | green | — |
| instrument-teeth | green | red | green | green | green | — |
| launch-pane-fix | green | red | green | green | red | — |
| ptc-revalidate | green | red | red | green | green | — |
| run-registration | green | green | red | green | green | — |

## Five-minute diagnostic coverage

| bundle | cells | with a pulse | states |
|---|---|---|---|
| incumbent | 5 | 0 | — |
| haiku-claude-code | 5 | 2 | {'progressing': 2} |
| luna-codex | 5 | 3 | {'progressing': 3} |
| deepseek-claude-code | 5 | 5 | {'progressing': 1, 'started': 4} |
| muse-claude-code | 5 | 4 | {'started': 1, 'progressing': 3} |
| deepseek-reasonix | 0 | 0 | — |

Reported separately from final accuracy: early escalation value matters even when two bundles reach the same pass count. A cell with no pulse finished inside the window — an absence of data, not a snapshot that saw nothing.

## What this table is not

Five hive-heavy tasks with one PLN sentinel. It cannot qualify a bundle for applied tenant research, for less-bounded work, or for the future middle tier, and the single cross-project task is a sentinel that the instrument reaches a second repository — not evidence about PLN research. **No bundle is benchmark-qualified by this document:** the M1c designation is the operator's, in a committed disposition.

