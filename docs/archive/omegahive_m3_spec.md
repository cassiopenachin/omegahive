# OmegaHive M3 — Promotion & Legibility Spec

**Status:** Build spec, ready to scaffold on top of M2. **Implements:** H3 (deterministic promotion makes a human view legible without flooding it) and H6 (metrics detect unproductive dynamics — stalls, loops, retries, cost, aging).
**Builds on:** M2 (gateway, reducer, DES engine with the bare-wake affordance, reactors, metrics). **Additive — no events-table migration:** `promotion.created` / `promotion.suppressed` and `metric.threshold_crossed` already have payload models (M1 completed `PAYLOADS`); M3 adds an evaluator, detectors, the human-view projection, and a tuning harness.

**Decisions baked in (flag any to red-team):**

- Promotion is **deterministic rules**, never agent self-reporting; **severity is *derived* in the evaluator**, never stamped on the event (the M1 envelope principle, and Ben's "mechanical promotion over self-reporting").
- The **human view is a projection** of `promotion.created`; **one-tier vs two-tier is a config flag** — that *is* the H3 experiment, not two transports.
- **Thresholds are outputs of tuning** (labeled scenarios → precision/recall), not numbers we guess.
- **Digests are deterministic *references*** (thread id + event count + the triggering events), **not generated summaries** — real summarization needs an LLM and is Regime B.
- H6 **time-based detectors reuse M2's wake**; event-driven detectors are pure projections over the log.
- Promotion **labels are our (scenario-authored) judgment** of human-relevance. So the H3 metric measures the ruleset against *our* notion of relevance — honest about that; it's a tuning target, not ground truth.

---

## 1. Scope

**What M3 proves:** that the full operational trace can be made **legible to a human** — the critical events surface, the routine noise is suppressed, and unproductive dynamics are flagged — by deterministic rules, measurably (precision/recall).

**New in M3:** the **promotion evaluator** (instrument), the **human-view projection** (one-/two-tier), the **H6 detectors** (the metrics runner emitting `metric.threshold_crossed` for stall / aging / retry-loop / loop / cost-spike / activity-vs-progress), the **tuning harness** (labeled scenarios → precision/recall), and the scenario labels + CLI views.

**Deferred:** real summarization for digests, a Slack renderer, and a live human (all Regime B); promotion against *real* human-relevance labels (we use our own); RCBench-derived scenarios (the parallel scenario-sourcing milestone feeds these); per-agent policy.

## 2. Components overview

| Component | Kind | M3 responsibility |
|---|---|---|
| Promotion evaluator | instrument reactor | apply the deterministic ruleset over the stream → emit `promotion.created {ref_event, rule_id}`; derive severity here |
| Human view | projection | the promoted subset (+ digest references), rendered with links back to source; `tiers: 1` = all, `tiers: 2` = promoted-only |
| H6 detectors | metrics runner (extends M2) | emit `metric.threshold_crossed {metric, value, threshold}` for stall / aging / retry-loop / loop / cost-spike / activity-vs-progress |
| Tuning harness | offline over labeled runs | score the ruleset's precision/recall vs labels; sweep thresholds to targets |

All emits still go through the gateway (instrument authority + structural validation); detectors and the evaluator **read** the full stream (the §7 instrument projection — instruments see everything).

## 3. Promotion evaluator

An instrument reactor (`agent_id: "promotion"`). On each settle it consumes new events (and maintains light windowed state via the board + correlation threads) and applies a ruleset of **pure predicates** `(event, context) -> promote? {rule_id}`. Promoted → emit `promotion.created {ref_event, rule_id}`. Not promoted → nothing (absence = suppressed; `promotion.suppressed` stays available for an explicit "considered and dropped" audit but M3 doesn't emit it by default).

**Severity is derived here**, never read off the event — the evaluator decides importance from `event_type` + context + windowed state. This is the line that keeps H3 honest (mechanical promotion, not self-report).

**Starter ruleset** (predicates fixed; *thresholds tuned in §6):

- `task.escalated` → promote
- `review.failed` → promote
- `blocked` longer than `T_block` (in `logical_ts`) → promote
- correlation-thread length > `N_thread` → promote a **digest reference** (the thread id, its event count, span, and the first/last events — not a summary)
- `metric.threshold_crossed` (any H6 detector) → promote
- cumulative `cost` over `C_spike` in a window → promote
- routine `task.progress` / `task.accepted` / `task.assigned` → **suppress** (never promote)

**Dedup:** to avoid double-surfacing one situation, the evaluator promotes at most once per `(task_id or correlation, rule_id)` within a window — so a stale task that the coordinator escalates *and* the metrics runner ages doesn't generate two near-identical human items.

## 4. Human view

A projection over `promotion.created`: the promoted events, each carrying its `ref_event` (and the `caused_by` chain) so a human can follow a promoted item back to the raw trace — plus digest references for long threads.

- `config.tiers: 1` → the human view is the *full* event stream (no curation).
- `config.tiers: 2` → the human view is the *promoted subset* only.

Swapping that flag, on the same run, **is** the H3 experiment (does curation cut noise without dropping the critical signal). The CLI gains `report --human` to render the human view (promoted items + digests, with `caused_by`/source links).

**Reconstructability (a structural proxy for the human rubric).** Since there's no live human in Regime A, we measure legibility structurally: for each *critical situation* in a scenario (a labeled task failure / escalation / stall), at least one promoted event must reference it and its key source events must be reachable via the `caused_by`/correlation links from the promoted view. A scenario passes reconstructability if every critical situation is so covered.

## 5. H6 detectors (the metrics runner, extended)

The M2 metrics runner gains *detectors* that emit `metric.threshold_crossed {metric, value, threshold}` when a dynamic looks unproductive. Promotion then surfaces them (§3). (Whether the *coordinator* also reacts to these — e.g. abandoning a looping task — is a later policy refinement, out of scope for M3, whose job is legibility, not new control.) Two families:

**Event-driven** (pure projections over the log):

- **retry-loop** — a task cycles `assigned → reject/reopen → assigned` ≥ `K_retry` times.
- **loop / circular-handoff** — a cycle in the `causation` graph, or a repeated `(event_type, task)` pattern along one correlation thread.
- **cost-spike** — cumulative or per-window `cost` over `C_spike`.
- **activity-vs-progress** — the H6 core: `events_per_completed_task` or progress-events-without-a-result over a window exceeds `A_thresh` (busy but not progressing).

**Time-based** (reuse M2's bare wake — the detector schedules a wake to re-check, no new mechanism):

- **stall** — no task changed status anywhere for > `T_stall` ticks.
- **aging** — a task open (not `done`/`failed`/`cancelled`) for > `T_age` ticks.

Detectors fire **once per situation** (same dedup discipline as promotion), so they don't wake-storm. These are *observability* signals (hive-level / cross-task), distinct from M2's coordinator escalation (task-level operational reaction) — they complement, and dedup keeps the human view from showing both for one situation.

## 6. Tuning harness (precision/recall)

Promotion thresholds are **fit, not guessed.** A scenario's `expected` block labels each promotable situation as human-relevant (critical) or not (routine). The harness runs the ruleset over the labeled scenarios and computes:

- **recall** of critical events (did we promote what we should), **precision** (of what we promoted, how much was critical), **routine-suppression rate**.

It sweeps the thresholds (`T_block`, `N_thread`, `C_spike`, the H6 thresholds) to hit the starter targets (do not overfit): **critical-event recall ≥ 0.90, routine suppression ≥ 0.70.** The fitted thresholds become the v1 promotion config.

## 7. Scenario format extension

```yaml
config:
  tiers: 2                       # 1 = human sees all; 2 = promoted only (the H3 knob)
labels:                          # ground-truth human-relevance for tuning
  critical: [review.failed, task.escalated, "metric:stall"]   # event types / situations that SHOULD promote
  routine:  [task.progress, task.accepted]                    # that should NOT
expected:
  promotions:   { recall_critical: ">= 0.9", suppression_routine: ">= 0.7" }
  h6_detected:  [retry_loop, cost_spike]        # detectors that should fire
  reconstructable: true                          # every critical situation reachable from the human view
```

## 8. Metrics (H3/H6 measurement)

Added to the M1/M2 core, all deterministic projections:

- **Promotion (H3):** precision, recall, promotions-per-task, promotions-per-hour (in `logical_ts`), routine-suppression-rate, reconstructability (pass/fail, §4).
- **H6 detection:** counts of each detector firing, and (against labels) detection precision/recall.

## 9. CLI

- `report --human` — the human view (`tiers`-respecting): promoted items + digest references, each with its source link.
- `report --promotions` — the promotion scoreboard (precision/recall/suppression vs the scenario's labels, if present).
- existing `--board` / `--metrics` unchanged.

## 10. Tests & M3 definition-of-done

- **test_promotion_rules** — each rule fires (escalation / review.failed / blocked-over-threshold / thread-length-digest / cost-spike / `metric.threshold_crossed` → promoted) and routine progress is suppressed; severity is derived, not read from the event; dedup holds.
- **test_human_view** — `tiers:1` shows all, `tiers:2` shows only promoted; promoted items carry working source links; digests are references (no summary text).
- **test_h6_detectors** — retry-loop / loop / cost-spike / activity-vs-progress / stall / aging each fire on a scenario engineered to trigger it, and *don't* on one that shouldn't; stall/aging use the wake and still reach quiescence.
- **test_promotion_tuning** — on a labeled noisy scenario, the fitted ruleset hits recall ≥ 0.9 / suppression ≥ 0.7; reconstructability passes.
- **test_two_tier** — a noisy failure scenario: two-tier surfaces every critical situation while cutting routine volume; one-tier surfaces all.
- **test_determinism** (extended) — promotion + detectors reproduce identically across runs.

**M3 is done when:** a noisy failure scenario, run with `tiers:2`, produces a human view that contains every critical situation (escalations, review-fails, stalls/loops) and suppresses routine progress at the target rates, with reconstructability passing; the H6 detectors fire on the engineered cases; and all tests + ruff + mypy + CI stay green.

## 11. Deferred to Regime B / later

Real (LLM) digest summarization; a Slack renderer and a live human in the loop; promotion measured against *real* human-relevance labels rather than ours; RCBench-derived scenarios (fed by the parallel scenario-sourcing milestone); the real OmegaClaw coordinator reacting to promotions; per-agent policy. None requires an events-table migration or an engine change — M3 is new instrument logic, projections, and a tuning pass over the same spine.
