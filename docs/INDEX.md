# docs/ Index — one line per doc, authority scope, status (a map, not a status board)

## docs/ (current — living architecture and operating specs)
- omegahive_design_1_1.md — the implementation design against Ben's OmegaHive 1.1 spec; authoritative architecture, rev 6, current
- omegahive_deployment_spec.md — deployment topology, fork program, operating procedures; v2, current (§7 Beastie bring-up is a completed milestone, labeled in place)
- omegahive_test_plan.md — gates and experiments for every build stage; normative companion to the design doc, rev 6, current
- omegahive_hive_native_ops.md — how the OmegaHive project itself becomes the hive's first workload (tiers 1–3 of agent capability); current, follows `reference/omegahive_stage2_verdict.md`
- omegahive_session_agents.md — worker-side binding story for CLI coding agents (wake patterns, hooks→MCP instrumentation); design doc v1, current
- omegahive_ui_spec.md — operator-facing web UI spec; v0.1, shipped (`src/omegahive/ui/` implements it)
- omegahive_recovery_runbook.md — human-only recovery path: backup, restore, drain-before-migrate; v1, current
- omegahive_worker_harness.md — how a launch chooses a model and how that choice is recorded: route catalog vs launch binding, adapters, the supervisor, the execution lifecycle facts and their query; v1, current (HIP-1 M2 `worker-harness-core`)

## docs/reference/ (normative-or-historical sources; consult, don't start here — see reference/README.md)
- omegahive_spec_1_1.md — upstream OmegaHive 1.1 spec (Ben Goertzel), text-faithful PDF conversion; reference source
- omegahive_port_spec.md — the port milestone build spec (`HiveCoordinatorPort`); shipped, semantics normative
- omegahive_stage2_verdict.md — closes the stage-2 coordinator ladder, states what stages 3+ assume; decision record, governs
- omegahive_c2_battery_spec.md — build spec for the `qual` CLI (model-qualification battery); shipped, still normative, cited by `qual/__init__.py`
- omegahive_o2_fork_patches.md — OmegaClaw fork patch operational decisions (channels, skills, config, Landlock); shipped, normative binding-story facts
- omegahive_v0b_order.md — hive-image board-binding qualification gate; not yet executed (no `qual/records/*-v0b-*`), cited by `hive_native_ops.md`'s Tier-3 definition
- omegahive_taskbench_guide.md — operating guide for the `taskbench` CLI (task-replay benchmark, HIP-1 M1b): corpus v0 and its held-out reservation, how to materialize a cell, run an approved batch, validate and read a record; current, cited by `taskbench/__init__.py`

## docs/deployments/ (deployment records)
- TEMPLATE.md — the host-facts + lockfile template a new deployment record starts from; **every host fact belongs in a record here, not in the general docs** — that is what this directory is for
- deployment-0-beastie.md — Beastie (deployment #0) acceptance record; checks 1–5 green, Jul 7 2026
- omegaclaw-fork.md — OmegaClaw fork base-image record; boot-smoked on Beastie, Jul 7 2026
- omegahive_remote_access_spec.md — Beastie remote access (Tailscale) for a 5-week unattended absence; v1, deployment-#0 practice record

## docs/archive/ (superseded and closed; retained for history — see archive/README.md)
21 files, one line each in their own Status header, not restated here: 13 pre-design-doc plans/specs/architecture from before the Jul 6 design-doc rewrite (architecture, docs_audit, m0–m5 specs, overview, plan, repo_hygiene_spec, rp3_spec, v0_spec), plus this triage's additions — `omegahive_hive_user_migration.md` (executed procedure — the `hive` unix account exists on the host), `omegahive_omegaclaw_binding.md` (decided Q1 deliberation, superseded by design §3.4), `omegahive_stage2_spec.md` (closed ladder, companion to the verdict), and five closed work/fix orders now that their deliverables have shipped or merged (`omegahive_c2_v0a_v2_order.md`, `omegahive_port_fixes_D1-D5.md`, `omegahive_stage2_fixes_A1-A4.md`, `omegahive_v3_fixes_B1-B6.md`, `omegahive_v3_kb_persona_brief.md`).

## docs/evidence/ (frozen experiment records; not reorganized)
11 files, one line each in their own text: dated experiment/analysis records (baseline_experiment, c2_v0a_r1, competitive_evaluation, deferred_capability_coordination, interop_durability_notes, researchclawbench_deepdive, shell_drill_audit_2026_08_13, triage_stage_a report/spec/table, vs_qwestor_cascade). Frozen by convention — `docs/omegahive_deployment_spec.md` §2 names this directory alongside `docs/` and `docs/archive/` as one of the three homes for documentation.
