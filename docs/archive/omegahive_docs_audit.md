# OmegaHive / OmegaClaw — Docs Audit (Jun 29 2026)

Status check on every doc against where we've landed: the **pivot** from simulation-elaboration to real-agent integration, **decision A** (board stays external), the **coordinator ladder** (greedy → vanilla-LLM chief-of-staff → OmegaClaw), **RP3 shelved**, and the **verified OmegaClaw-Core architecture**.

## Progress (Jun 29, evening)

**Reorganized + canonical docs pulled in.** The cowork folder is now structured: `omegahive/` (our 19 prototype docs — intra-links verified intact), `omegaclaw/` (workstream docs + the new reference + `core/`), `metta/` (the skill). The **47 canonical `docs`-branch docs are copied to `omegaclaw/core/`** as our editable working copy. Keystone written: **`omegaclaw/omegaclaw-core-architecture.md`** — the verified-against-code reference (supersedes the stale `core/omegaclaw_architecture_analysis.md` / `component_overview.md` where they conflict; complements the `reference-*`/`tutorial-*` user docs).

### Canonical-docs update plan — the `omegaclaw_*` analytical set ("by me/Codex long ago")
These ~18 are the update targets; the `reference-*` / `tutorial-*` / `introduction` framework docs read accurate (partly verified) and need only spot-checks. Reconcile each against the verified reference + current code, priority order:

- **High** (architecture-describing, most drift): `omegaclaw_architecture_analysis.md`, `omegaclaw_component_overview.md`, `omegaclaw_reasoning_deep_dive.md`, `omegaclaw_memory_design_note.md`, `omegaclaw_memory_log_analysis_spec.md`.
- **Medium** (hardening/security — may cite shipped or dropped items): `omegaclaw_concrete_hardening_proposal.md`, `omegaclaw_hardening_backlog.md`, `omegaclaw_hardening_validation_note.md`, `omegaclaw_security_hardening_roadmap.md`, `omegaclaw_tools_skills_extension_note.md`.
- **Lower** (narrower scope): `omegaclaw_prompt_persistence_note.md`, `omegaclaw_prompt_vs_helpers_{intro,memory,reasoning,skills}_note.md`, `omegaclaw_doc_validation_note.md`.

The pass should be **per-doc and reviewed** (read against the reference + code, fix drift) — not a blind mass-edit. Several of these cover memory / policy / NAL-PLN, so I should finish the `[pending code-verify]` reads (`src/memory.metta`, `profile/policy`, the reasoning libs) first.

**Workstream note:** `omegaclaw/omegaclaw-readme-draft.md` is stale — its `./docs/*` links use the old tutorial numbering (the `docs` branch renumbered them; tutorial-04 is now "writing-a-custom-skill"). Low priority — flagged, not fixed.

---

_The original audit (still valid for the OmegaHive-side docs) follows._

## Already updated (safe, no judgment needed)

- **`omegahive_rp3_spec.md`** — prepended a **SHELVED** banner (superseded by the pivot; records why: over-scoped, the `escalated`-flag bug, and "can't test H2 on stubs"). Original draft kept below it.
- **`omegahive_plan.md`** — added a "partially superseded / revision pending" banner (Track A done, RP3 shelved, Track B now active). Body not rewritten — see "needs your input" below.
- **`omegahive_architecture.md`** — added a "§6 re-framing pending" banner pointing at the binding doc.
- **`omegahive_deferred_capability_coordination.md`** — retargeted the trigger from "RP3/Track B" to "when a real coordinator needs routing."

## Audit table

| Doc | Status | Update needed |
|---|---|---|
| `omegahive_omegaclaw_binding.md` | **current** | none — just written |
| `omegahive_baseline_experiment.md` (RP1/RP2 design+results) | **current record** | one line: RP3 shelved, sim paused post-pivot. Low priority |
| `omegahive_researchclawbench_deepdive.md` | **current, more central** | RCBench is now *the* real yardstick (OpenClaw-solo / prose-cascade vs OmegaHive-coordinated). Add a note; otherwise solid |
| `omegahive_vs_qwestor_cascade.md` | **current** | qwestor is a named input to the charter; fine. Maybe note Track-C status |
| `omegahive_v0_spec.md` | **accurate, framing dated** | substrate spec still true; add a status note that it's the (paused) Regime-A foundation |
| `omegahive_m0…m5_spec.md` (6 docs) | **accurate historical records** | all built+reviewed green; optional "BUILT ✓ / substrate paused for pivot" header. Low priority |
| `omegahive_competitive_evaluation.md` | **historical, fine** | none (point-in-time June eval) |
| `omegahive_interop_durability_notes.md` | **historical, fine** | none |
| `omegahive_overview.md` | pointer stub | none |
| **`omegahive_plan.md`** | **needs real revision** | the track structure — see below |
| **`omegahive_architecture.md`** | **needs §6 reframe** | the staged path — see below |
| **`omegahive_rp3_spec.md`** | shelved (banner added) | done |

## Two that need your input (I didn't rewrite the strategy unilaterally)

1. **`omegahive_plan.md` — the track structure.** Proposed revision: **Track A = done** (fold in the M5/baseline results); **Track B = now active**, restructured around the OmegaClaw work — decision A, the greedy→vanilla-LLM→OmegaClaw ladder, the binding (D2), and the spike; **RP3/decision-forking moved to a "deferred substrate ideas" appendix**; **Track C (qwestor)** unchanged but noted as gated. The hypothesis→track table needs H2 re-pointed (it's now real-coordinator vs vanilla-LLM, not stub-based). I can draft this for your review.

2. **`omegahive_architecture.md` — §6 staged path.** Proposed reframe: keep §1–5 + §7 (destination + scaling invariants still hold); rewrite §6 so the path is "v0 substrate (done) → **real OmegaClaw coordinator over the external board** (now) → workers/compute/governance," dropping the "many simulation stages first" framing. The board-as-external + the binding become an explicit part of the path. I can draft this too.

## OmegaClaw-workstream docs (different stream — flag for you)

These aren't OmegaHive-prototype docs; the pivot doesn't obsolete them, but you mentioned "obsolete omegaclaw docs":

- `fork-review-and-roadmap-mapping.md` — point-in-time fork review (Jun 12, fork head `4ae8037`). Still valid as history; the roadmap it maps to has since been acted on. No change unless you want a freshness note.
- `omegaclaw-readme-draft.md` — worth diffing against the **current** repo `README.md` (I just read it; it's substantive and current). Possibly stale.
- `omegaclaw-marketing-brief.md`, `omegaclaw-marketing-triage.md`, `omegaclaw-process-proposal.md`, `OmegaClaw_Roadmap_Q2-Q3.docx` — separate (marketing/process/roadmap) streams; out of scope here.

**I did *not* find a dedicated "OmegaClaw architecture" doc in this folder that my deep study obsoletes.** The genuinely-superseded descriptions I saw live in the **OmegaClaw-Core repo's own** `docs/` (`omegaclaw_architecture_analysis.md`, `omegaclaw_concrete_hardening_proposal.md`) — those are the team's, on the repo's `docs` branch. If *those* are what you meant by "update our docs," I can reconcile them against the verified architecture there.

## Two questions for tomorrow

1. **Which doc set is canonical?** You mentioned "the docs local branch of the repo." Confirm: do current OmegaClaw architecture docs live on the **OmegaClaw-Core `docs` branch**, and should my verified-architecture write-up land there (vs. staying in this cowork folder)? I'd treat the repo's `docs` branch as canonical and retire/redirect the obsolete cowork copies.
2. **Want a new "OmegaClaw-Core: verified architecture" doc?** My expert understanding currently lives in `omegahive_omegaclaw_binding.md` + memory. If useful, I'll consolidate it into a standalone reference (the loop, skills, channels, dispatch, errors, single-agent reality, the py-bridge) — the doc the pivot needs and that would replace any obsolete description.

## Suggested order tomorrow

1. Confirm canonical doc location (Q1 above) + which OmegaClaw docs you meant.
2. I draft the `plan.md` track revision + `architecture.md` §6 reframe for your review.
3. (If wanted) the consolidated OmegaClaw-Core architecture reference.
4. Then back to the binding: lock the three small open questions and spec the spike (which I'll red-team-panel).
