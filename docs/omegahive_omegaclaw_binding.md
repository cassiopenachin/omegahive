# OmegaHive ↔ OmegaClaw Binding — Q1 Design Options

**Status:** Design comparison for the coordinator↔board binding. The decision that matters; concrete options with pseudo-code, a recommendation, and the deciding argument.

## Fixed going in

- **Board = the external Python/Postgres substrate (decision A).** Coordinators bind via one thin Python port; the substrate stays fixed so we can **swap only the coordinator** and compare **greedy → vanilla-LLM chief-of-staff → OmegaClaw** on the same board. OmegaClaw must beat the *plain-LLM* coordinator — that gap is H2 (does NAL/PLN + the MeTTa substrate earn its keep).
- **Q2 (errors):** op-rejection (`TransitionRejected`) → operational feedback; world-failure (escalation/exhaustion) → the board view the coordinator reasons over.
- **Q3:** one fused chief-of-staff to start.

## The crisp reframing: two orthogonal axes

"Is the hive a channel?" conflated two independent questions:

- **READ axis** — how board state reaches the coordinator's reasoning.
- **OP axis** — how the coordinator emits board changes.

"Channel" was a candidate answer on *both* axes simultaneously. Separating them is what makes this decision clean.

| Axis | Mechanism options |
|---|---|
| READ | **R-chan**: board arrives via `(receive)` — board *is* the `commchannel`. · **R-ctx**: board projection injected into `getContext`, like memory/history. |
| OP | **O-send**: ops are `(send "assign t1 w2")` strings the adapter re-parses. · **O-skill**: ops are distinct action skills `(assign t1 w2)` → py-call the port. |

## The shared port (what every coordinator plugs into)

Holds the substrate fixed; serves the three-way comparison.

```python
class HiveCoordinatorPort:           # one Python adapter over the OmegaHive gateway
    def view(self) -> str:           # curated board projection (promotion/H3-filtered, bounded)
    def assign(self, task, worker) -> OpResult     # OpResult = OK | "REJECTED: <reason>"
    def reassign(self, task, worker) -> OpResult
    def escalate(self, task, reason) -> OpResult
    def close(self, task) -> OpResult              # gateway still enforces the review/done gate
    def reopen(self, task) -> OpResult
    # planning ops only if the chief-of-staff also plans: create_task, add_dep
```

- **greedy** calls these from `decide()` (effectively already does).
- **vanilla-LLM control**: `while: v=view(); ops=llm(render(v)); for op in ops: apply(op)`.
- **OmegaClaw**: `view()` feeds context; each op is a skill that py-calls the port.

`"REJECTED: …"` is the gateway's `TransitionRejected` surfaced as a value (Q2 operational path). Escalations/exhaustion live inside `view()` (Q2 situation path).

## The three coherent designs

### D1 — Channel-everything  (R-chan + O-send)
```
 board ──getLastMessage()──▶ (receive) ─────────────▶ prompt
 LLM ──"assign t1 w2\nescalate t3"──▶ (send) ──send_message()──▶ adapter re-parses ──▶ gateway
```
Board *is* the `commchannel`. One `send_message(str)` carries every op as a mini command-language the adapter must re-parse.

### D2 — Context + Action-skills  (R-ctx + O-skill)  ◀ recommended
```
 board ──port.view()──▶ getContext("BOARD: …") ──────▶ prompt
 LLM ──(assign t1 w2)──▶ eval skill ──py-call port.assign()──▶ gateway ──OpResult──▶ &lastresults
```
Board state is *situation* (context); ops are *actions* (skills). Mirrors the vanilla control one-to-one.

OmegaClaw side, concrete:
```metta
; READ — a new context source spliced into getContext's py-str:
(= (boardView) (py-call (hive.view)))
;   getContext: "... BOARD: " (boardView) " SKILLS: " (getSkills) " OUTPUT_FORMAT: ..."

; OPS — board-op action skills (alongside shell/write-file, NOT alongside send/receive):
(= (assign $t $w) (py-call (hive.assign $t $w)))
(= (escalate $t)  (py-call (hive.escalate $t "coordinator")))
(= (close $t)     (py-call (hive.close $t)))
(= (reopen $t)    (py-call (hive.reopen $t)))
;   getSkills gains:  "- Assign task to worker: assign task worker"  etc.
```
A rejected op returns `"REJECTED: t1 not ready"` → into `LAST_SKILL_USE_RESULTS` (operational, retry-shaped). Escalations/review-fails appear under `BOARD:` next turn (situation, reason-shaped).

**Implementation caveat:** `assign`/`reassign` are *two-arg* skills, like `write-file`. OmegaClaw's parser special-cases two-arg commands (`helper.balance_parentheses` `special_two_arg_cmds`, plus `LLM_COMMANDS`); board ops need entries there or the multi-arg parse breaks. And the `hive` py-module must be importable and able to reach Postgres **under `profile/policy`** (the landlock sandbox — still on my read list; it gates whether either bridge can touch the DB).

### D3 — Hybrid: channel-as-wake + context + skills  (R-chan-signal + R-ctx + O-skill)
D2 plus a *liveness* signal: `getLastMessage()` returns a compact `"board changed: 3 ready, 1 escalated"` only to drive the loop's keep-awake / wake-on-change; the full board still comes from `view()` in context, ops are still skills.
```
 board ──"3 ready, 1 escalated"──▶ (receive) ─────────▶ keeps &loops alive / wakes the loop
 board ──port.view()──▶ getContext(full) ─────────────▶ prompt
 LLM ──(assign …)──▶ skill ──▶ gateway
```

## Compare

| Criterion | D1 channel-all | D2 context+skills | D3 hybrid |
|---|---|---|---|
| Faithful to OmegaClaw grain (chan=transport · ctx=situation · skill=action) | ✗ ops-as-chat | ✓ | ✓ |
| **Parallel to the vanilla-LLM control (clean H2)** | ✗ diverges | **✓✓** | ✓ |
| Structured multi-ops | ✗ one send → command-lang | ✓ | ✓ |
| Coexists with a human channel | ✗ board eats `commchannel` | ✓ | ✗ board eats `commchannel` |
| Error routing (op-reject vs world-fail) | muddled (both via channel) | ✓ clean | ✓ clean |
| Liveness (react to board change) | ✓ channel wakes | ✗ run-continuous or add signal | ✓ |
| Spike complexity | medium | **lowest** | low–medium |

## Recommendation: D2 for the spike, D3 as the liveness refinement

The deciding reason is not taste — it's that **D2 makes OmegaClaw and the vanilla-LLM control structurally identical**: same `view()` render, same op vocabulary, same gateway-apply path. The *only* difference left between them is OmegaClaw's symbolic internals (NAL/PLN, MeTTa memory) — which is exactly the variable decision A isolates for H2. D1 diverges from the control (ops become a chat command-language), muddies the comparison, and eats the single `commchannel`. D3 is just D2 + a wake-signal — adopt it when continuous-running gets wasteful; for a 1–2 task spike, liveness is a non-issue (run the coordinator continuously). The coordinator "reports" to humans through the board's own promotion→human-view, so it doesn't need the chat `commchannel` D1/D3 would consume.

## To pin down with you (small, concrete)

1. **Does the fused chief-of-staff also plan in the spike** (emit create-task/add-dep), or do we hand it a fixed plan and test coordination only first? *Lean: fixed plan first — isolate coordination, add planning once coordination holds.*
2. **Projection format** in `view()`: structured S-expr (MeTTa-native — OmegaClaw can `match` it) vs prose table? *Lean: S-expr for OmegaClaw; the vanilla control takes either.*
3. **Op granularity**: one op/turn or a batch (OmegaClaw emits up to 5)? *Lean: batch — matches OmegaClaw and is more realistic.*

(When this is locked and we spec the spike for build, I'll run that spec through the red-team panel.)
