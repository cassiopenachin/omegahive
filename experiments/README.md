# OmegaHive Experiments

Regime-A baseline experiments. Each subfolder is one experiment: `spec.md` (design + what it tests) and `results.md` (the measured distribution + findings). The scenario YAMLs live in [`../scenarios/`](../scenarios) and are referenced by path from each spec.

**Convention.** One project, run at three **happiness settings** — `clean` / `wobbly` / `messy` — so the chaos level is the single independent variable. Each setting is swept over 50 seeds:

```
omegahive simulate scenarios/<scenario>.yaml --replications 50
```

Calibration is anchored to the RCBench deep-dive (OpenClaw-solo mean **16.6/100**, ≈⅓ of tasks failed outright); `p_success` runs 0.9 / 0.5 / 0.3 across the three settings. The **greedy coordinator is the deliberate control** throughout (the H2 baseline) — kept dumb on purpose.

| Experiment | DAG | What it tests | Verdict |
|---|---|---|---|
| [rp1_linear](rp1_linear/) | linear 4-task chain | H1 coherence, H3 legibility, H6 detectors, cost floor | done — the linear chain *serializes* failures |
| [rp2_reproduction](rp2_reproduction/) | reproduction DAG, two diamonds, 3 parallel experiments | concurrency + simultaneous (execution) failure | done — the diamond delivers; per-task difficulty is now the binding constraint |

**Next:** RP3 — *decision forking* (try N implementations, expect some to fail, pick the winner). That's where failure becomes *planning* failure and a real planner/coordinator first beats greedy. Gated on the M5 substrate refinements (per-task difficulty, capability matching, worker capacity).
