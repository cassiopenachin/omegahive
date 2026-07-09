# V3 — KB, op sheet, persona (self-contained work order)

**Supersession (v2 artifacts replace all prior drafts):** the pre-existing flat `qual/personas/coordinator-v1.txt` (a 5-op draft predating `prune`) and `qual/catalogs/board-ops-v1.yaml` are superseded — your deliverables are **`coordinator-v2/`** and **`qual/catalogs/board-ops-v2.yaml`** (same 6-op set as v1's catalog, descriptions aligned to the §2 table). `git rm` the stale flat persona draft in the same PR (git history preserves it); leave `board-ops-v1.yaml` in place until its referencing scenarios migrate.

**Context (all you need):** you are authoring the three text artifacts for the stage-2 coordinator ladder ([omegahive_stage2_spec.md](omegahive_stage2_spec.md) — read §5.1, §5.4, §5.5 first; they are the contract). Deliverables: `persona-coordinator-v1` (one `prompt.txt` + the R1 system-prompt variant), the **op-reference sheet**, and `coordination-kb-v1` (one markdown file, 3–5 pages). All three are hash-pinned experiment inputs.

**Authoring discipline (hard rule):** work from this brief and the spec sections named above **only**. Do **not** read `ladder/` code, `seeds.py`, board fixtures, or any run configuration — the KB must be authored blind to board parameters (the leakage check compares afterward). If you need a fact about the runtime or ops, it is in this brief or the spec; if it isn't, stop and ask rather than reading the ladder.

## 1. Persona

Blocks and exact constraints per spec §5.5. Deliver as: `qual/personas/coordinator-v2/prompt.txt` (R2: the five blocks — IDENTITY, OBJECTIVE, ENVIRONMENT, FEEDBACK, MEMORY — nothing else) and `qual/personas/coordinator-v2/r1-system.txt` (R1: same four shared blocks, no MEMORY, plus a [MECHANICS] block stating the harness op-output format — one op per line, exact shapes from the op sheet). Wording rules: strategically inert (objectives yes, policies no — when in doubt, the sentence goes to the KB or dies); no contractions or quoted examples; [MEMORY] must use exactly the spec's sentence naming the `query` skill; [ENVIRONMENT] must say "most recent" view and mark earlier history views stale.

## 2. Op-reference sheet

Syntax, semantics, and legality of every op — **no strategy, no norms**. The legality below is **authoritative** (extracted from the substrate's legality spec, Jul 9; the fresh-eyes reviewer re-verifies it against the merged substrate at freeze — you do not read the code):

| Op | Legal when | Rejection codes you can see |
|---|---|---|
| `assign <task> <worker>` | task exists, status `ready`, no owner; worker is in the view's `(workers …)` section | `UNKNOWN_TASK` · `ALREADY_OWNED` · `NOT_READY` · `UNKNOWN_WORKER` |
| `reassign <task> <worker>` | task exists, status `assigned`, `blocked`, or `in_progress`; worker registered | `UNKNOWN_TASK` · `ILLEGAL_TRANSITION` (wrong status) · `UNKNOWN_WORKER` |
| `escalate <task>` | task exists — **any status**; escalation is always available as a flag for attention | `UNKNOWN_TASK` |
| `close <task>` | task's **latest review is `passed`** (review happens via worker/review events, not your ops) | `ILLEGAL_TRANSITION` (incl. unknown task — reported as the review precondition failing) |
| `reopen <task>` | task status is **`in_review` only** | `UNKNOWN_TASK` · `ILLEGAL_TRANSITION` |
| `prune <task>` | task exists, not `done`/`cancelled`, not already pruned, and pruning would not take a currently-satisfiable dependent join below its `ready_when` k surviving dependencies | `UNKNOWN_TASK` · `ILLEGAL_TRANSITION` |

Any op from an actor without coordinator authority returns `NOT_AUTHORIZED` (not relevant to your text — the coordinator has authority; omit or mention once). State in the sheet: worker ids come from the view's `(workers …)` section, task ids from `(tasks …)`; a rejection always carries a reason string alongside the code. Keep every example in bare `board "verb args"` shape; no quotes/apostrophes inside payloads. One file, used verbatim in two places (R1 system prompt; the `board` skill catalog entry) — you deliver the single source file; wiring is not your job.

## 3. Knowledge base (`qual/kb/coordination-kb-v1/kb.md`)

One markdown file, 3–5 pages, chunk-friendly headings. Content list (general coordination knowledge — this is where ALL strategy lives): evidence norms for pruning (what repeated failure does and doesn't imply; weighing evidence before abandoning an approach; the cost of pruning too early vs too late — **in general terms, no numeric thresholds**); escalation norms (when a blocked situation warrants escalation vs patience); allocation under contention (idle workers, competing ready tasks); k-of-n fork semantics and what redundancy is for; rejection-recovery patterns (what each code suggests doing next); the value of re-reading the board before repeating an op. **Leakage rules (pre-registered, checked by a fresh-eyes reviewer):** no numeric threshold that could coincide with any run-config value; no worked example isomorphic to a two-branch fork with a doomed branch; reference the op sheet, never restate it.

## 4. Freeze checklist (you complete, the reviewer verifies)

- [ ] Three artifacts committed; SHA-256 of each recorded in `qual/personas/coordinator-v2/HASHES` and `qual/kb/coordination-kb-v1/HASHES`
- [ ] No `prompt_<provider>.txt` exists in any volume template (assert absence — a stray override silently replaces the persona per-provider)
- [ ] Persona sentences each classifiable as identity/objective/environment/feedback/mechanics — zero policy sentences (reviewer runs this check)
- [ ] KB leakage check passed against the frozen board parameters (reviewer runs it — you never see those parameters)
- [ ] R1 system prompt and the op sheet source are byte-identical in their shared content

**Review protocol:** submit the three artifacts; the fresh-eyes reviewer (not you) runs the strategy-inertness pass on the persona and the leakage check on the KB. Findings come back as edits, not debate — the artifacts are experiment inputs, not prose to defend.
