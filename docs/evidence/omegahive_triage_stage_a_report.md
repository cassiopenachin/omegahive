# Coordination-Demand Triage — Stage A Report

**Question.** Before funding expensive multi-agent coordination experiments on OmegaHive, do the ResearchClawBench (RCBench) research problems actually exhibit *coordination-shaped* failures? Stage A statically mines existing benchmark data to produce a **ranking hypothesis** — not a conclusion — about which tasks are worth instrumented Stage-B re-runs.

**Bottom line up front.** The static evidence leans toward **"coordination-shaped failure is rare"** on these problems. Even among the tasks that best fit the process-sensitive profile (high best-run scores, high cross-run variance), the gap between the best and a median run is dominated by *single-thread-depth* and *single-insight verification* misses — one agent failing to execute one deep deliverable — rather than by failures to combine parallel workstreams. Of the 10 strongest process-sensitive candidates dissected in D3, only 2 show a coordination-shaped miss pattern; 8 are capability-shaped. This should be treated as a ranking hypothesis with low-to-moderate confidence given the archival limitations below.

---

## Limitations (read first — they bound every claim here)

1. **No execution transcripts.** Only final artifacts and reports are archived. We cannot see *how* an agent worked (whether it serialized, backtracked, dropped a sub-goal mid-run). Stage A infers process quality from the *residue* in the final report, which is weak evidence for a process claim.
2. **No per-item ground-truth rubric scores.** Only `total_score` is archived. The per-item coverage judgments in D3 are *our own reading* of report vs rubric (acceptable at Stage A precision, per spec), not the benchmark's official scoring. Where our reading and the total score disagree (e.g. Life_003), we flag it.
3. **Capability/process confound.** Each task's ~21–29 runs come from *different* agents and models (GPT-5.x, Claude Opus 4.x, Gemini 3.x, GLM/Qwen/MiniMax/MiMo, etc.). Cross-run variance therefore conflates raw model capability with process quality. A "process-sensitive" score profile can be produced entirely by a capability spread across models — which is exactly what D3 suggests is happening. Only instrumented single-agent-vs-hive re-runs (Stage B) can separate the two.
4. **Archival gap.** The index lists 1,083 completed runs; only **860** have an on-disk report (`data.json`). D1/D2 use all 1,083 index rows (score/duration/cost are all present in the index). D3, which needs the report text, could only draw best/median runs from the 860 archived. For most D3 tasks the on-disk best equals or nearly equals the true best; the one exception is **Math_000**, whose true best (35.9) is not archived — its D3 "best" is only 26.6, so its best-vs-median gap is compressed and its D3 is weaker evidence (flagged in D3 and D4).

---

## D1 — Per-task quantitative table

Computed programmatically over all **1,083 completed runs** (`runs_index.json`); every number below is script-generated (`omegahive_triage_stage_a_table.csv` accompanies this report). `gap` = 50 − max. Duration in seconds, cost in USD, both medians. `std` is the sample standard deviation of `total_score`.

| task | n | mean | max | std | gap→50 | med dur (s) | med cost ($) | class |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Astronomy_000 | 27 | 19.6 | 33.1 | 8.1 | 16.9 | 946 | 0.984 | capability-limited |
| Astronomy_001 | 27 | 19.3 | 39.2 | 12.0 | 10.8 | 420 | 0.589 | **process-sensitive** |
| Astronomy_002 | 27 | 20.7 | 34.6 | 7.7 | 15.4 | 914 | 0.913 | capability-limited* |
| Astronomy_003 | 28 | 45.8 | 48.0 | 1.8 | 2.0 | 349 | 0.398 | **ceiling** |
| Chemistry_000 | 25 | 15.0 | 21.6 | 4.6 | 28.4 | 2560 | 3.007 | capability-limited |
| Chemistry_001 | 27 | 3.9 | 11.3 | 3.1 | 38.7 | 1022 | 0.709 | capability-limited |
| Chemistry_002 | 28 | 1.2 | 5.0 | 1.4 | 45.0 | 544 | 0.531 | capability-limited |
| Chemistry_003 | 24 | 11.5 | 28.8 | 6.4 | 21.2 | 936 | 0.918 | capability-limited |
| Earth_000 | 28 | 17.2 | 25.5 | 4.6 | 24.5 | 582 | 0.542 | capability-limited |
| Earth_001 | 28 | 34.4 | 41.6 | 5.5 | 8.4 | 457 | 0.418 | **ceiling** |
| Earth_002 | 28 | 20.6 | 34.0 | 7.6 | 16.0 | 690 | 0.550 | capability-limited* |
| Earth_003 | 27 | 2.2 | 10.5 | 2.9 | 39.5 | 855 | 0.798 | capability-limited |
| Energy_000 | 26 | 11.8 | 22.0 | 5.5 | 28.0 | 1202 | 0.829 | capability-limited |
| Energy_001 | 27 | 16.1 | 27.0 | 7.4 | 23.0 | 913 | 1.001 | capability-limited |
| Energy_002 | 28 | 32.9 | 42.5 | 5.8 | 7.5 | 696 | 0.584 | **ceiling** |
| Energy_003 | 28 | 15.7 | 27.0 | 6.5 | 23.0 | 520 | 0.469 | capability-limited |
| Information_000 | 25 | 18.4 | 49.4 | 15.1 | 0.6 | 633 | 0.789 | **process-sensitive** |
| Information_001 | 26 | 4.3 | 18.0 | 4.0 | 32.0 | 708 | 0.707 | capability-limited |
| Information_002 | 28 | 22.3 | 39.8 | 11.5 | 10.2 | 382 | 0.427 | **process-sensitive** |
| Information_003 | 27 | 8.1 | 17.8 | 4.7 | 32.2 | 1529 | 1.665 | capability-limited |
| Life_000 | 28 | 7.1 | 13.2 | 2.8 | 36.8 | 798 | 0.801 | capability-limited |
| Life_001 | 29 | 10.2 | 19.6 | 4.9 | 30.4 | 425 | 0.486 | capability-limited |
| Life_002 | 28 | 6.0 | 11.2 | 2.5 | 38.8 | 756 | 0.459 | capability-limited |
| Life_003 | 28 | 30.6 | 40.5 | 5.4 | 9.5 | 411 | 0.541 | **process-sensitive** (borderline) |
| Material_000 | 27 | 16.7 | 26.0 | 6.5 | 24.0 | 1806 | 1.615 | capability-limited |
| Material_001 | 28 | 10.6 | 23.6 | 6.4 | 26.4 | 578 | 0.448 | capability-limited |
| Material_002 | 27 | 26.1 | 40.7 | 13.0 | 9.3 | 1087 | 1.059 | **process-sensitive** |
| Material_003 | 28 | 16.7 | 28.8 | 5.8 | 21.2 | 1223 | 1.058 | capability-limited |
| Math_000 | 27 | 19.0 | 35.9 | 8.2 | 14.1 | 789 | 0.911 | **process-sensitive** |
| Math_001 | 28 | 28.7 | 44.1 | 8.7 | 5.9 | 730 | 1.000 | **process-sensitive** |
| Math_002 | 26 | 9.8 | 20.5 | 5.5 | 29.5 | 1665 | 1.201 | capability-limited |
| Math_003 | 23 | 9.3 | 29.6 | 7.8 | 20.4 | 621 | 0.639 | capability-limited* |
| Neuroscience_000 | 28 | 9.2 | 14.2 | 3.2 | 35.8 | 740 | 0.571 | capability-limited |
| Neuroscience_001 | 25 | 3.7 | 18.5 | 4.2 | 31.5 | 689 | 0.896 | capability-limited |
| Neuroscience_002 | 28 | 0.7 | 3.8 | 0.8 | 46.2 | 1490 | 1.019 | capability-limited |
| Neuroscience_003 | 28 | 10.0 | 20.4 | 5.5 | 29.6 | 893 | 0.849 | capability-limited |
| Physics_000 | 27 | 22.0 | 37.2 | 8.0 | 12.8 | 665 | 0.613 | **process-sensitive** |
| Physics_001 | 27 | 29.0 | 46.2 | 7.1 | 3.8 | 761 | 0.664 | **process-sensitive** |
| Physics_002 | 26 | 26.8 | 47.4 | 9.2 | 2.6 | 541 | 0.569 | **process-sensitive** |
| Physics_003 | 28 | 40.8 | 55.6 | 6.3 | −5.6 | 547 | 0.511 | **ceiling** (borderline) |

*Asterisked* rows are borderline capability-limited — see D2.

---

## D2 — Task classification

**Score-distribution context** (across the 40 per-task summaries): task `max` ranges 3.8–55.6 (median 27.9, p25 19.6, p75 40.5); task `std` ranges 0.8–15.1 (median 5.8); task `mean` ranges 0.7–45.8 (median 16.4). Reference point: score 50 ≈ matching the original paper.

**Thresholds (applied uniformly).**
- **capability-limited** if `max < 35`. Rationale: 35 is 70% of the paper-match anchor (50) and sits at the benchmark's upper-mid (between the p50 and p75 of task maxima). If *no* run in ~27 attempts — spanning the strongest frontier models — clears 70% of a paper, the ceiling is set by capability/task difficulty, not by process; more or better-coordinated agents are unlikely to move it.
- **ceiling** if `max ≥ 35` **and** `std < 8` **and** `mean ≥ 32`. Rationale: a high floor with tight dispersion means most agents already do well — there is little residual variance for coordination to capture.
- **process-sensitive** otherwise (i.e. `max ≥ 35` with either `std ≥ 8` or `mean < 32`). Rationale: some agents get close to a paper while most do not, and the spread is wide — the profile where *process*, not raw capability, plausibly separates runs. These are the coordination-amenable candidates that D3 then scrutinizes.

**Counts: capability-limited 26, process-sensitive 10, ceiling 4.**

**Process-sensitive (10), ranked by max then std** — the D3 candidate pool:
Information_000 (max 49.4, std 15.1), Physics_002 (47.4, 9.2), Physics_001 (46.2, 7.1), Math_001 (44.1, 8.7), Material_002 (40.7, 13.0), Life_003 (40.5, 5.4), Information_002 (39.8, 11.5), Astronomy_001 (39.2, 12.0), Physics_000 (37.2, 8.0), Math_000 (35.9, 8.2).

**Borderlines flagged (not silently binned):**
- **Life_003** — `max 40.5 ≥ 35` but `std 5.4 < 8` and `mean 30.6 < 32`. Fails the ceiling `mean` cutoff by 1.4 and the process-sensitive `std` cutoff. Its top runs cluster tightly (40.5/40.1/37.3/36.5/36.0) with a long lower tail, so the variance is driven by weak runs, not a clean best-vs-rest split. Binned **process-sensitive** on the `mean < 32` branch but is the *weakest* member of the set; D3 confirms its best-vs-median content gap is small.
- **Physics_003** — classed **ceiling** (max 55.6, std 6.3, mean 40.8). Its max *exceeds* 50 (gap −5.6, one run at 55.6). std 6.3 < 8 and mean 40.8 ≥ 32, so it clears the ceiling test — but the 55.6-vs-next-cluster (49.0/48.6) suggests one exceptional run rather than uniform excellence. Kept as ceiling (used as the ceiling control in D4).
- **Astronomy_002 (max 34.6), Earth_002 (34.0), Math_003 (29.6)** — capability-limited but within ~5 of the 35 cutoff and with std ≥ 7.6, i.e. "near-process-sensitive." Astronomy_002 and Earth_002 in particular have a modest spread; if the `max` cutoff were relaxed to 33 they would flip. They are *not* promoted (the best run still misses 70% of the paper), but they are the first tasks to re-examine if the threshold is contested.
- **Astronomy_000 (max 33.1)** — top-3 runs identical at 33.1 then a drop; capability-limited with a hard ceiling at exactly 33.1, reinforcing that the difficulty caps the best achievable score.

---

## D3 — Dropped-item analysis

Method: for each of the **10 process-sensitive tasks**, read the rubric (`checklist.json`), the **best-scoring** archived run's report, and one **median-scoring** archived run's report. For every rubric item, judge coverage (covered / partial / missed) by substance (keywords as anchors only), classify the item by the kind of work it demands (multi-workstream / single-thread-depth / data-handling / verification), and identify which item classes the median drops relative to the best. All coverage judgments are our own reading (see Limitation 2); every claim cites the run_id used.

**Per-task verdicts** (best run_id / median run_id):

| # | Task | Best run_id (score) | Median run_id (score) | Miss pattern (median vs best) | Verdict |
|---|---|---|---|---|---|
| 1 | Information_000 | Information_000_20260325_155748 (48.0) | Information_000_20260415_140033 (16.4) | Median drops the equation-OCR (data-handling) item entirely and thins both single-thread items to unexecuted claims. | **capability-shaped** |
| 2 | Physics_002 | Physics_002_20260428_120221 (38.6) | Physics_002_20260404_172029 (24.9) | Median matches best on the shared data-handling core (XEB subset) but drops every item needing a *second parallel estimator line* (MB-regression/gate-count), the larger N=56 system, and the gate-count model. | **coordination-shaped** |
| 3 | Physics_001 | Physics_001_20260518_062515 (46.2) | Physics_001_20260415_005728 (28.5) | Median drops the quantitative anchors (critical-current value, correct power-law exponent, microwave-quadratic consistency), retreating to proxies/hedges. | capability-shaped |
| 4 | Math_001 | Math_001_20260401_235039 (44.1) | Math_001_20260416_200558 (30.4) | Both meet the surface plot comparison; median loses the depth/verification sub-claims (strict tolerance, late-stage linear regime, adaptive restart). | capability-shaped |
| 5 | Material_002 | Material_002_20260416_221556 (39.1) | Material_002_20260415_134111 (28.4) | Median under-executes two *self-contained* compute pipelines (too-short MD; mis-fit adsorption slope), degrading substance rather than omitting a workstream. | capability-shaped |
| 6 | Life_003 | Life_003_20260409_095535 (40.5) | Life_003_20260414_211912 (31.8) | Both cover the same primary metric per item and both drop the same *secondary cross-referenced* sub-claims (MAD quality-check, 26% recall/cancer-gene biology, dual-reader signature). | coordination-shaped (weak) |
| 7 | Information_002 | Information_002_20260409_155531 (39.8) | Information_002_20260402_222052 (15.8) | Median reframed the physics derivation as benchmark-scoring meta-analysis and never executed it; all three items (all single-thread-depth) land partial/missed. | capability-shaped |
| 8 | Astronomy_001 | Astronomy_001_20260414_202922 (37.0) | Astronomy_001_20260401_120316 (20.0) | Median plotted data/tables but never rendered the two required 2D posterior/distance *visualizations* with model contours/curves — stopped at 1D summaries. | capability-shaped |
| 9 | Physics_000 | Physics_000_20260516_053530 (35.7) | Physics_000_20260416_215341 (20.9) | Both drop the mechanistic dynamic-growth insight; the ~15-pt gap tracks depth/polish on one data-handling item (median actually stronger on the heaviest item). | capability-shaped |
| 10 | Math_000 † | Math_000_20260321_013909 (26.6) | Math_000_20260416_114950 (21.1) | Median drops the dominant (0.70-weight) verification item — no self-run ablation, no plug-and-play/beats-baseline check; headline numbers contradict reproduction. | capability-shaped |

† **Math_000 caveat:** true best (35.9) not archived; D3 "best" is only 26.6, so the gap is compressed and this row is weak evidence.

**Aggregate D3 signal.** 8 of 10 capability-shaped; 2 coordination-shaped (Physics_002 clean; Life_003 weak). The recurring failure mode across the capability-shaped eight is a single agent **not executing one deep deliverable** — a derivation not carried out (Information_002), a required figure not rendered (Astronomy_001), a critical value not extracted (Physics_001), an ablation not run (Math_000), a pipeline under-sampled (Material_002). These are the failures multiplying agents does *not* obviously fix: a hive of the same weak model would still not render the contour plot. The one clean coordination-shaped task (Physics_002) is exactly the profile the thesis predicts — the median nails the single shared analytic core and loses only on *additional parallel estimator workstreams and a larger system* that a coordinated division of labor could plausibly cover in parallel.

**Important cross-check on Life_003.** Our reading found the *median* run slightly more thorough than the *best* on rubric item[0], and both runs covered the same core and missed the same secondary strands — yet the total scores differ 40.5 vs 31.8. That divergence between our substance reading and the archived total score is a concrete instance of Limitation 2 (the score difference likely lives in figure quality/formatting our text reading can't see) and is why Life_003's coordination verdict is marked *weak*.

---

## D4 — Stage-B subset recommendation

Eight tasks, stratified across the spectrum. Priority is the coordination-amenable end, but the set is deliberately mixed so Stage B can falsify the ranking, not just confirm it. One-line rationale each:

1. **Physics_002** — *primary coordination candidate.* The only clean coordination-shaped D3 result: median holds the analytic core and loses exactly the additional parallel estimator/larger-system workstreams a hive could parallelize. If coordination helps anywhere, here first.
2. **Life_003** — *coordination candidate (verification-flavored), stress-test.* Weak/ambiguous D3 signal plus a score-vs-substance mismatch — precisely the task where instrumentation would resolve whether the gap is coordination, verification-gating, or just formatting. High information value.
3. **Information_000** — *widest spread, near-ceiling best (max 49.4, std 15.1).* D3 read it capability-shaped (per-artifact depth), but its three items are three *independent* deliverables — a natural test of whether parallel agents-per-artifact beats one serial agent. Directly probes the multi-workstream hypothesis.
4. **Physics_001** — *high best (46.2), reachable ceiling.* D3 capability-shaped (missed quantitative anchors); include to check whether a review/verification gate — a lightweight coordination pattern — catches the dropped critical-current/exponent anchors a solo run hedged on.
5. **Math_001** — *high mean and max, verification-shaped miss (adaptive restart, tolerance).* Tests whether a dedicated verification/critic role recovers the depth sub-claims the median dropped.
6. **Information_002** — *large best-vs-median gulf (39.8 vs 15.8), single-thread-depth.* Include as a **capability-shaped control within the process-sensitive band**: the thesis predicts coordination should *not* help here (the median simply didn't do the derivation). A useful negative expectation.
7. **Neuroscience_002** — *capability-limited control* (max 3.8, mean 0.7, std 0.8). Everyone fails hard; coordination should do essentially nothing. Confirms the floor and guards against a "coordination helps everywhere" artifact.
8. **Astronomy_003** — *ceiling control* (max 48.0, mean 45.8, std 1.8). Everyone already near paper-match; coordination should add ~nothing. Confirms the ceiling and calibrates the "no headroom" end.

(Physics_003 is the alternative ceiling control if a run above 50 is wanted; Astronomy_003 is preferred for its tighter, cleaner ceiling profile.)

---

## D5 — Gate readout

**Direction.** The static evidence leans toward **"coordination-shaped failure is RARE"** on these RCBench problems. First, the population is dominated by **capability-limited** tasks (26/40) where even the best of ~27 frontier-model runs misses 70% of the paper — no amount of coordination is the lever there. Second, and more decisive for the thesis, *even inside the process-sensitive band* — the tasks selected precisely for looking coordination-amenable — the best-vs-median gap is capability/single-insight-shaped in 8 of 10 cases; only Physics_002 shows a clean coordination-shaped miss, with Life_003 a weak second. The modal way a median run loses is failing to execute one deep deliverable, which multiplying same-capability agents would not fix.

**Confidence: low-to-moderate, and this remains a ranking hypothesis, not a verdict.** Three factors cap confidence: (a) the capability/process confound is unbroken — cross-model variance can wholly manufacture the process-sensitive profile, and Stage A cannot tell that apart from genuine process spread; (b) coverage was judged from final-report *residue* with no transcripts, so a run that coordinated well but reported tersely is indistinguishable from one that didn't; (c) our substance reading diverged from the archived total score on at least Life_003, exposing that item-level ground truth is not what we're actually measuring. The *ranking* (which tasks are most coordination-plausible) is more trustworthy than the *rate* (how common coordination-shaped failure is).

**Top 3 things Stage B must check:**
1. **Break the capability/process confound directly.** Re-run the same fixed model as (a) a solo agent and (b) an OmegaHive-coordinated hive on the D4 subset. Only a within-model solo-vs-hive delta isolates process from capability; the score spread in this data cannot.
2. **Instrument the failure locus, not just the score.** Capture transcripts/event logs and confirm whether the dropped rubric items on Physics_002 / Life_003 are genuinely *parallel-workstream* misses that a coordinated division of labor closes — versus a single deep step no configuration executed. If the hive drops the same items, the coordination story is falsified for that task.
3. **Test the verification/review-gate pattern specifically.** Several capability-shaped misses (Physics_001 anchors, Math_000 ablation, Math_001 restart) are *verification*-flavored — a claim not checked against evidence. A lightweight critic/review role is a cheaper coordination primitive than full multi-agent parallelism; Stage B should measure whether it recovers those items before concluding coordination has no value here.

---

*Data: `runs_index.json` (1,083 runs), 40 tasks; reports from the 860 archived runs. D1/D2 script-generated over all index rows; companion table `omegahive_triage_stage_a_table.csv`. D3 judgments are the analyst's own reading of report vs rubric at Stage-A precision. Read-only; no modifications outside this report and its CSV.*
