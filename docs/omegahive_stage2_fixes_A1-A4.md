# Stage-2 V1/V2a — post-audit fixes A1–A4 (self-contained work order)

**Context (all you need):** the k-joins/prune substrate (PR #1) and ladder harness deterministic half (PR #2) were audited against `docs/omegahive_stage2_spec.md` (now v2.2 — §3 was corrected as part of this audit; read it first). Four fixes. File:line references as of merge `3d6e165`; anchor by function if drifted. A1 is the substantive one. Fold/guard unit tests run without Postgres; anything touching the DB paths needs `docker-compose up` + `OMEGAHIVE_TEST_DATABASE_URL`.

---

## A1 — Prune guard: enforce ≥k non-pruned per dependent join (spec was corrected; code now follows)

**Where:** `src/omegahive/board/legality.py` `_g_prune` (~122–145); `src/omegahive/board/reducer.py` (~51).
**Problem:** the guard implements the spec's old k=1-shaped invariant — it rejects a prune only when a dependent join would be left with *zero* live dependencies. For a k-of-n join with k>1 (e.g. k=2 over `{a,b,c}`), pruning `a` then `b` are both accepted; the reducer's `effective = min(required, len(active))` then silently reinterprets the join as 1-of-1 and it fires on `c` alone — **a completion on less redundancy than the plan declared, with no signal anywhere.** Spec v2.2 §3 now states the correct invariant.
**Fix:**
1. In `_g_prune`, for every dependent join of the prune target: compute `k = dependent.ready_when if dependent.ready_when else len(dependent.depends_on)`; reject with `ILLEGAL_TRANSITION` if `len(live_deps_after_prune) < k`.
2. In the reducer, remove the silent downward clamp: `min(required, len(active))` must not mask a violated invariant. Keep it only as defense-in-depth **with an assertion** (the guard now makes the state unreachable; if it's ever reached, that's a bug to surface, not absorb).
3. Update the guard docstring and reducer comment (both still reason in k=1 terms).
**Tests (required, all pure-fold/no-DB):** (a) k=2 over 3 deps: first prune accepted, second prune rejected `ILLEGAL_TRANSITION`; (b) same board: prune one, complete the remaining two → join fires correctly; (c) regression: k=1 fork behavior unchanged (existing tests must stay green); (d) gate/fold-agreement meta-test still passes with the new guard.

## A2 — k-bounds validation on the wire event, not just the sim Plan

**Where:** `src/omegahive/events/types.py` `TaskCreated` (~27–28); sim-side validation exists at `sim/scenario/schema.py:44-54` but does not protect the real emit path.
**Problem:** an emitted `task.created` with `ready_when=0`, negative, or > dependency count bypasses all validation — an LLM coordinator can create a malformed join with one op.
**Fix:** a `model_validator` on `TaskCreated` (or a gate-side check in the legality row for `task.created`): `1 ≤ ready_when ≤ len(distinct dependencies)` when present; violation → the event is refused (`ILLEGAL_TRANSITION` or schema rejection — match how other payload validation is surfaced).
**Test:** emit with `ready_when=0` and `ready_when=n+1` → both refused, recorded; valid k accepted.

## A3 — Loss-bucket taxonomy (owed before any R1+ cell runs; fine to land now)

**Where:** `ladder/metrics.py` (~75).
**Problem:** every non-completion collapses to the single string `"incomplete"`. Spec §7 pre-registers a diagnostic taxonomy (premise-formulation / orchestration / op-ceiling / latency / reasoning) plus cap-exhaustion attribution — "losing informatively is a design goal," and R0 never loses informatively, so this is invisible today and load-bearing the day R1 runs.
**Fix:** record *mechanical* buckets now, from data the runner already has: `cap_ops_exhausted`, `cap_timeout`, `board_stalled` (no legal progress event within the deadline window), `run_error`. The cognitive buckets (premise/orchestration/reasoning) are assigned at analysis time per §7 — the runner's job is to preserve enough evidence (final board state, last N coordinator ops, which cap tripped) for that assignment. Add those fields to the per-seed record.
**Test:** force each mechanical bucket in a scripted run (tiny cap; frozen worker) → correct bucket + evidence fields present.

## A4 — Equivalence coverage for `ready_when`

**Where:** the equivalence scenario set (sim ↔ port identical-logs test).
**Problem:** the loader/schema thread `ready_when` through, and both paths share one fold — but no equivalence scenario *exercises* a k<n join, so a future regression in either path's handling wouldn't trip the keystone test.
**Fix:** add one k=1-of-2 fork scenario (with a prune emitted mid-run by the scripted driver) to the equivalence suite. Pure test addition; no production code.

## Definition of done

A1–A2 regressions green (pure-fold tests without DB; full suite on live Postgres before merge); A1's demonstration case (k=2, prune two) fails on `3d6e165` and passes post-fix; A3's evidence fields visible in a per-seed record; A4 scenario in the equivalence suite; spec v2.2 §3 and the code agree — no comment or docstring still describing the ≥1 rule.
