# V3 review — fixes B1–B6 (self-contained work order)

**Context (all you need):** PR #8's three artifacts (persona v2, op-reference sheet, KB v1) passed fresh-eyes review clean — strategy-inert, leak-free, symmetric. The blockers are in the surrounding code: the grid's execution path still consumes the superseded catalog, and the op sheet documents a rejection code the substrate does not implement. These land as a follow-up PR on the V track; **B1–B4 gate the grid freeze** (any L-cell run before they land is contaminated or mis-documented). File:line refs as of `29b499d`.

## B1 — BLOCKER: the grid runs against the contaminated v1 catalog

`ladder/runner.py:59` hardcodes `board-ops-v1.yaml`. Repoint to `qual/catalogs/board-ops-v2.yaml`. Until then every L1–L3 cell reads v1 — whose prune description (line 28, "Abandon a **doomed** not-done task…") is both a strategy sentence (policy in a legality surface) and a vocabulary leak isomorphic to the seed design. One-line fix + a test asserting the runner loads v2.

## B2 — BLOCKER: CI pins the leak

`ladder/opsheet.py` docstring and `tests/test_ladder_opsheet.py:16` assert the presence of the exact contaminated string ("Abandon a doomed"). Repoint both to v2 content; the assertion should check *structural* properties (six ops present, codes listed), never phrasing.

## B3 — BLOCKER: the ghost-worker fix was implemented as a silent client-side drop, not the specced roster

The substrate has **no worker roster and no `UNKNOWN_WORKER`** (nothing in `board/state.py` or `board/legality.py`); instead `ladder/parse.py:42-43` silently drops ops naming unknown workers before they reach the board. That is the original stall bug *hidden* rather than fixed: the model gets no feedback, no recorded rejection, no next-view surfacing — the exact silent-failure class the stage-2 spec v2.3 roster design (§6) exists to kill, and the op sheet + `board-ops-v2.yaml` currently promise `UNKNOWN_WORKER` semantics the code lacks. Implement per spec §6: `worker.registered` events at run-seed (whitelist row), fold tracks the roster, renderer emits the `(workers …)` section on every view, `assign`/`reassign` guards reject unregistered targets with `UNKNOWN_WORKER` (recorded, surfaced in the next view). **Delete the silent drop in `parse.py`** — parse passes the op through; the gateway answers. Frozen-seam discipline: pinned regressions, meta-tests (coverage + gate/fold agreement) extended to the new event type and code.

## B4 — BLOCKER (consequence of B3): closed-loop system test 2 must assert the real behavior

Spec §2's suite requires hallucinated ids to yield **recorded rejections that appear in the next view**. A silent client drop cannot produce that; whatever the suite currently asserts for this case is either missing or wrong. After B3, make test 2 assert four things: (1) every unknown-worker assign yields `gateway.rejected(UNKNOWN_WORKER)` in the log; (2) each rejection is present in the next delivered view; (3) the run terminates `cap_ops_exhausted` — this is the correct mechanical bucket, because the test board is completable and `board_stalled` stays reserved for its offline-decidable meaning (unsatisfiable join); do **not** add a live no-progress detector, which would be a threshold heuristic the bucket taxonomy deliberately keeps out of the runner; (4) the per-seed evidence record shows zero accepted coordinator ops (terminal rejection streak preserved), which is what lets analysis assign the cognitive bucket. The property under test is "failure is visible and attributable, never silent" — those four assertions express it fully. This test green is B3's definition of done.

## B5 — Hygiene (non-gating)

Add the placeholder test for the freeze assertion "no `prompt_<provider>.txt` in any volume template" so it exists when templates land.

## B6 — Migrate v1 consumers, then retire v1 (PR #8's declared follow-up)

**What:** repoint the battery scenarios S1/S3/S8, `tests/test_qual_schema.py`, and the battery spec's S3 example from `qual/personas/coordinator-v1.txt` → `qual/personas/coordinator-v2/prompt.txt`; then `git rm` the v1 persona **and** `board-ops-v1.yaml` (git history preserves both).
**Sequencing:** does not gate the grid freeze (B1–B4 do), but it **must land before the first v0a battery run** — otherwise v0a qualifies emission discipline against a superseded 5-op persona and the "doomed"-contaminated catalog, and the results would need re-running against the final artifacts anyway. If the migration cannot land in time, the stopgap is a one-line in-place fix: strike "doomed" from v1's prune description so v0a at least does not inherit the leaked vocabulary — but the migration is the real fix; the stopgap does not close B6.
**Watch during migration:** v1 predates `prune` (5-op era). Any scenario expectation, schema assertion, or fixture that enumerates ops or persona sections will need updating for the 6-op sheet and the five-block v2 shape — treat expectation diffs as expected, but review each one rather than bulk-accepting. The correct v2 file for battery Mode A (base image, R2 agent) is `prompt.txt`, not `r1-system.txt`.

## Definition of done

B1–B2: runner + tests point at v2; no test asserts catalog phrasing. B3–B4: roster in the fold, `UNKNOWN_WORKER` recorded and view-surfaced, silent drop deleted, closed-loop test 2 green on the real path. B5 as stated. B6: no reference to `coordinator-v1.txt` or `board-ops-v1.yaml` remains anywhere outside git history and revision records; battery suite green against v2. PR #8 itself merges independently — its artifacts are clean; do not hold it for these.
