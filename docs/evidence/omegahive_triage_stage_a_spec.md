# Coordination-Demand Triage — Stage A Execution Spec

**Status:** Execution spec for a delegated analysis run. Self-contained: everything needed is in this document and the local data.
**Context:** OmegaHive is a coordination substrate (event log + gateway + task board) for hives of research agents. Before funding expensive multi-agent coordination experiments, we test the assumption under them: *do the benchmark research problems actually exhibit coordination-shaped failures?* If they rarely do, the coordination thesis is overweighted for these problems and project goals get re-examined; if many do, the experiments proceed on a defensible task subset. Stage A is static mining of existing benchmark data — it produces a **ranking hypothesis**, later confirmed or killed by instrumented re-runs (Stage B, out of scope here).

## Data (all local, all read-only)

ResearchClawBench (RCBench): 40 research tasks across 10 scientific domains, each a curated workspace (real datasets from published papers) with an expert-written weighted rubric. Scoring 50 ≈ matching the original paper.

Paths (host form for Read/Grep/Glob → bash form for shell):

- `/Users/cassio/code/SNET/ResearchClawBench` → `/sessions/eager-dazzling-cray/mnt/SNET/ResearchClawBench`
  - `tasks/<TaskId>/task_info.json` — task statement + dataset descriptions
  - `tasks/<TaskId>/target_study/checklist.json` — the rubric: weighted items, each with `content`, `keywords`, `weight`, `type` (text/image)
- `/Users/cassio/code/SNET/ResearchClawBench-Home/data` → `/sessions/eager-dazzling-cray/mnt/SNET/ResearchClawBench-Home/data`
  - `runs_index.json` — 1,083 runs: `run_id`, `task_id`, `agent_name`, `model`, `status`, `duration_seconds`, `cost_usd`, `total_score`
  - `runs/<run_id>/data.json` — includes `report` (the run's full final report, markdown string), score, cost
  - `runs/<run_id>/workspace/` — final artifacts (code, outputs, report dir)

**Known limitations (state them in the report):** no execution transcripts and no per-item rubric scores are archived — only final artifacts and total scores. Each task has ~27 runs from *different* agents/models, so cross-run variance confounds model capability with process quality. That is exactly why Stage A output is a ranking hypothesis, not a conclusion.

## Deliverables

Write the report to `/Users/cassio/Documents/Claude/Projects/OmegaClaw/omegahive/omegahive_triage_stage_a_report.md` (bash: `/sessions/eager-dazzling-cray/mnt/OmegaClaw/omegahive/…`). Optionally a companion CSV of the per-task table next to it.

**D1 — Per-task quantitative table.** For each of the 40 tasks over completed runs: n, mean/max/std of `total_score`, gap-to-50 of the best run, median duration, median cost. Compute programmatically (Python via shell); do not hand-copy numbers.

**D2 — Task classification.** Classify every task as one of:
- `capability-limited` — everyone fails (low max regardless of variance): more/better-coordinated agents are unlikely to help;
- `ceiling` — everyone does well, low variance: nothing to coordinate;
- `process-sensitive` — high variance and a high max (some agents get close to 50, most don't): process, not raw capability, separates agents — the coordination-amenable candidates.

Propose explicit numeric thresholds, justify them against the score distribution, and apply them uniformly. Borderline cases get called out, not silently binned.

**D3 — Dropped-item analysis** on the 8–12 strongest `process-sensitive` candidates. For each task: read the rubric `checklist.json`, then the **best-scoring** run's `report` and one **median-scoring** run's `report` (cite `run_id`s). For every rubric item, judge coverage in each report (covered / partial / missed — use the item's `keywords` as anchors, but judge substance, not keyword string-matching). Classify each item by the kind of work it demands:
- `multi-workstream` — requires combining parallel lines of work (data pipeline + theory + validation + figures) — coordination-shaped;
- `single-thread-depth` — one deep analytic insight — capability-shaped;
- `data-handling` — correct ingestion/processing of provided data;
- `verification` — checking/validating claims against evidence — review-gate-shaped.

Per task, report where the median run loses ground versus the best run: which item classes get dropped. A task whose misses concentrate in `multi-workstream`/`verification` items is coordination-shaped; misses concentrated in `single-thread-depth` are not.

**D4 — Stage-B subset recommendation.** 6–10 tasks stratified across the spectrum (mostly strong process-sensitive candidates, plus one `capability-limited` and one `ceiling` control), with one-line rationale each.

**D5 — Gate readout.** A short, honest section: does the static evidence lean toward "coordination-shaped failure is common" or "rare" on these problems? State the confidence appropriately (this is a ranking hypothesis) and list the top 3 things Stage B must check.

## Method constraints

- **Read-only** on both repos. No file modifications outside the two deliverable paths. No network access, no external APIs, no LLM-judge calls — your own reading of reports vs rubrics *is* the judgment, which is acceptable at Stage A precision.
- Reports can be long; read them selectively but honestly (do not judge an item "missed" from a skim of the abstract — search the report for the item's substance before deciding).
- Every qualitative claim cites `run_id` and rubric item index. Every number comes from a script you actually ran.
- Budget guidance: D1/D2 are cheap and fully programmatic. D3 is the expensive part — bound it to ~10 tasks × 2 runs; if context pressure forces triage, prefer fewer tasks done properly over more done shallowly, and say what was cut.

## Acceptance checklist

- [ ] D1 table complete for all 40 tasks, script-generated
- [ ] D2 thresholds explicit and justified; every task classified; borderlines flagged
- [ ] D3 covers ≥8 process-sensitive tasks, best + median runs each, with run_id citations and per-item-class miss patterns
- [ ] D4 subset of 6–10 with rationale
- [ ] D5 gate readout with stated confidence and Stage-B checklist
- [ ] Limitations section states: no transcripts, no per-item ground-truth scores, capability/process confound
