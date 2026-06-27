# OmegaHive M4 — Closed-loop / Stochastic Stubs Spec

**Status:** Build spec, ready to scaffold on top of M3. **Implements:** the deferred Track-A piece — workers whose outcome is drawn from a seeded distribution, so a scenario runs as a **distribution over seeds**, not a single scripted trace. This turns the M1–M3 mechanics into the **Track-A baseline instrument** (H1/H3/H6 + cost as distributions, greedy coordinator as the control).

**Builds on:** M1–M3 (gateway, reducer, DES engine, reactors, metrics, promotion). **Additive — no events-table migration, no engine change.** The seed flows `scenario → assembly → worker`; the RNG seam (`engine/rng.py`) is finally consumed; a multi-seed harness and a pure aggregation projection are new.

**Design stance (read this first):** we are deliberately taking the **simplest version that still produces interpretable, reproducible signal**, and running it before adding anything. The dumb coordinator is one instance of that rule; the single stochastic knob below is the same rule applied to the workers. Every richer model I can think of (learning curves, latent task hardness, mixed failure modes, latency/cost noise, a family of coordinators) is **deferred to §9 as a diagnostic** — a hypothesis the first sweep tests for free, pulled in only when the data asks for it. The simple baseline is self-diagnosing.

**Decisions baked in (flag any to red-team):**

- **One stochastic primitive: a flat, memoryless `p_success` per attempt.** On an assignment the worker succeeds (quality `ok` → passes review) with probability `p_success`, else produces a review-failing result (→ reopen → retry). `p_success` does **not** vary with attempt. Everything else — latencies, cost, the M2 failure scripts — stays the deterministic point value it is today.
- **Stochastic is opt-in; scalars are point masses.** A worker with no `outcome` block builds exactly today's deterministic behavior, so all M0–M3 scenarios and the 102 existing tests stay byte-identical. The new behavior appears only when a scenario asks for it.
- **Keep the attempt-keyed RNG — it's floor, not complication.** `rng_for(seed, agent, task, attempt)` gives each assignment an independent draw, so a re-attempt can come out differently. Without it, a re-assigned task replays the identical outcome → retries deterministically re-fail → the sweep shows 100% escalation with zero recovery variance (a degenerate experiment, not a simpler one). Because `p_success` is flat, the per-task-vs-per-worker attempt-scope question is **moot** — we don't decide it.
- **Single failure mode behind an *open* field.** Failure is "fails review," produced via `quality_on_fail` (default `missing_sources`). It's a field, not a hardcoded branch, so a categorical over the M2 failure modes drops in later without reshaping anything.
- **Only the world is stochastic; the coordinator stays dumb and deterministic.** Stochasticity lives in the workers (the environment). The greedy coordinator is the deliberate control for the eventual H2 comparison and is untouched — we vary the world and hold the policy fixed.
- **Determinism is per-`(scenario, seed)`.** Byte-identity holds for a fixed seed (re-run into a clean table); across seeds logs differ (distinct `run_id` → distinct `event_id`s, and different draws change the emit count). The multi-seed aggregate is deterministic (fixed seed set) and history-independent (metrics read per-`run_id`, use `logical_ts` deltas and counts, never absolute `seq`).
- **The test oracle is invariants, not golden numbers.** We assert what must hold for *any* valid run (false-completion is always 0; completion_rate rises with `p_success`; adding a worker never lowers it; escalation_incidence rises as `p_success` falls) and *report* the observed bands rather than gating on them. Seed count is a placeholder set from the first sweep's variance, not a principled constant.

---

## 1. Scope

**What the first experiments are for:** show the substrate produces a **stable, reproducible distribution** of coordination dynamics under a fixed policy — that recovery-by-retry and escalation both occur across seeds, that the metrics aggregate sanely, and that determinism survives the move from script to draw. Nothing in M4 tries to be a faithful model of research difficulty; it is the minimal stochastic environment that lets the M3 instruments be read as distributions.

**New in M4:** consumption of the **`rng_for` seam** (attempt nonce); the **`p_success` / `quality_on_fail`** worker knob; **seed wiring** scenario→assembly→worker; a **multi-seed harness** (`engine/simulate.py`) and **distribution aggregation** (`metrics/distribution.py`); the scenario-format extension; the CLI surface; plus the two folded-in M3 review nits (§7).

**Deferred (see §9):** everything richer than one flat knob.

## 2. The stochastic knob (minimal change to `WorkerStub`)

No new abstraction — `WorkerStub` is extended in place (a behavior-policy *interface* is itself deferred to §9, to land with the second stochastic primitive). The worker gains `seed`, `p_success: float | None`, `quality_on_fail: str`, and a per-task `_attempts` counter. On an assignment targeting it:

```
attempt = self._attempts[tid] = self._attempts.get(tid, 0) + 1
if self.p_success is None:                      # today's deterministic path, unchanged
    quality = self.quality
else:
    rng = rng_for(self.seed, self.agent_id, tid, attempt)
    quality = "ok" if rng.random() < self.p_success else self.quality_on_fail
# ...then schedule accept → progress → result exactly as today, from `quality`
```

The M2 failure scripts (silent/rejects/fails_at/blocks) are unchanged and remain deterministic. `seed` arrives via `_worker(wid, pol, seed=scenario.seed)` in assembly — **no engine change**.

## 3. Determinism (`engine/rng.py`)

The seam gains the attempt nonce:

```python
def rng_for(seed, agent_id, task_id, attempt) -> Random:
    key = f"{seed}:{agent_id}:{task_id or ''}:{attempt}".encode()
    return Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))
```

`attempt` is run-local (the worker's own assignment count for that task), so each seed reproduces independently of whatever else is in the `events` table, and re-attempts are independent draws. Fixed draw order (one `random()` per assignment in v1) keeps the stream stable. Same `(scenario, seed, run_id)` into a clean table → byte-identical log.

## 4. Multi-seed harness + aggregation

`simulate(scenario, seeds)` (`engine/simulate.py`) runs the scenario once per seed, each in its own `run_id` (`{scenario_id}-s{seed}`) — a clean deterministic run — collects the per-run `Metrics` (and `PromotionScore` when labels are present), and aggregates (`metrics/distribution.py`):

```python
@dataclass(frozen=True)
class Summary:  n: int; mean: float; sd: float; p50: float; min: float; max: float

@dataclass(frozen=True)
class MetricsDistribution:
    n_runs: int
    completion_rate: float          # mean(tasks_completed / tasks_total) across runs
    escalation_incidence: float     # fraction of runs with >= 1 escalation
    false_completion_rate: float    # must be 0.0 across the whole sweep — the gate holds
    summaries: dict[str, Summary]   # per numeric Metrics field
```

Rates summarize across runs; numeric metrics get mean/sd/quantiles. Deterministic given the fixed seed set, so aggregate assertions are exact.

## 5. Scenario format extension

```yaml
run:
  max_logical_ts: 1000
  replications: 50            # placeholder — the seed count; simulate uses seeds 0..N-1.
                              # a single `run` still uses Scenario.seed. Set N from observed variance.
workers:
  w1:
    latency: { accept: 0, progress: 2, result: 4 }   # scalars — unchanged, deterministic
    cost: 5                                            # scalar — unchanged
    outcome: { p_success: 0.4, quality_on_fail: missing_sources }   # the only new, opt-in block
expected:
  invariants:                 # assert what must hold for ANY valid run, not magic numbers
    false_completion_rate: 0          # always
    completion_rate_monotonic_in_p: true
    completion_rate_nondecreasing_in_workers: true
    escalation_falls_as_p_rises: true
```

**Backward-compat is the test:** a worker with no `outcome` block is today's `PointPolicy` behaviour, byte-for-byte. Latency and cost stay scalars in v1 (no jitter — see §9).

## 6. CLI

- `simulate <scenario> [--replications N | --seeds 0,1,2]` — run the sweep, print the distribution table (and promotion distribution if labels are present).
- `report --distribution <run-prefix>` — re-render an existing sweep's aggregate.
- existing `run` / `report` unchanged (a single seeded run stays the default unit).

## 7. Folded-in carry-overs from the M3 review (non-blocking)

- Rename `promotions_per_hour` → **`promotions_per_tick`** in `metrics/promotion.py` (it is per logical tick; the field comment already admits it). One scoreboard label changes.
- A sentence in the scenario-format notes that **label-critical (should-surface) and derived-severity (how-loud) are orthogonal** — e.g. `aging` is label-critical yet severity-`warning`. No code change.
- `activity_vs_progress` staying silent on a fully-stalled 0-completion run (churn under threshold) is a Track-A tuning observation the sweep will surface — revisit the threshold against the observed churn distribution, not now.

## 8. Tests & M4 definition-of-done

- **test_point_policy_unchanged** — every M0–M3 scenario and the determinism fingerprint are **byte-identical** to `main` under the new path (stochastic off ⇒ nothing moved).
- **test_stochastic_determinism** — a `p_success` scenario, same seed, run twice into a clean table → identical logs (event_id included); two different seeds → different logs.
- **test_attempt_independence** — a worker re-assigned the same task can draw a different outcome on attempt 2 than attempt 1, and the draw is stable under replay.
- **test_distribution_invariants** — over a fixed seed set: `false_completion_rate == 0`; `completion_rate` non-decreasing in `p_success` (sweep p ∈ {0.2, 0.5, 0.8}); non-decreasing when a second worker is added; `escalation_incidence` non-increasing in `p_success`.
- **test_aggregation** — `MetricsDistribution` is exact and reproducible over the fixed seed set.
- **test_simulate_cli** — `simulate --replications N` runs N seeded runs and renders the distribution; `report --distribution` re-renders it.

**M4 is done when:** a flaky-worker scenario (a single `p_success`) run with `simulate --replications 50` produces a **deterministic distribution** in which some seeds recover via retry and others escalate, `false_completion_rate` is 0 across all 50, the §8 invariants hold, every existing scenario/test is byte-identical to `main`, and ruff + mypy + CI stay green.

## 9. Deferred — watch the first sweep for these

Each is a complication we are *not* building, paired with the observation that would pull it in:

- **Latent per-task hardness** (correlated failures, genuinely unrecoverable tasks) — add if recovery looks implausibly easy / H6 sees no real retry-loops.
- **Categorical failure mix** over the M2 modes (silent/hard-fail/block, not just review-fail) — add if the sweep only ever fires `retry_loop` and leaves stall/aging/block under-exercised. The `quality_on_fail` field is the seam.
- **Latency / cost jitter** — add if we want to study timing-noise effects; for now all timing/cost variance comes from the number of attempts.
- **A learning curve** (`p_success` rising with attempt) — add if we want to model re-spec improvement; reintroduces the attempt-scope decision when it lands.
- **A `BehaviorPolicy` interface** — extract from `WorkerStub` when a second stochastic primitive actually arrives (jitter or categorical), not before.
- **A documented family of dumb coordinators** (rotate / retry-same / escalate-fast) — add to defend the H2 floor as a *range* of controls; cheap, stays dumb, but not needed for the first runs.
- **More seeds** — set N from the first sweep's variance if the CI is too wide to resolve the Track-B effect.

None requires a migration or an engine change — each lands behind an existing seam on the same spine.
