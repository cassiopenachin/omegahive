# Record 2026-07-09-v0a-r1 — read this first

The C2 battery's first real run. It is a **plumbing smoke test, not a valid measurement** —
`aggregate.md` here is machine-generated and must **not** be used as a model ranking or
stage-2 calibration data.

Known invalidities (full analysis: `../../../docs/evidence/omegahive_c2_v0a_r1.md`):
- the pinned persona (`coordinator-v2/prompt.txt`) never ran — the image's baked prompt did;
- `pre_repair_parse_rate` / `repair_dependency` / `silent_unknown_count` / `pin_discipline_ok`
  are host-side artifacts, not the container's real parse behaviour;
- the probe is trivial, so competent models saturate ("qwen matches opus" is not supported);
- the `minimax-m3` route is unattributed (likely an OpenRouter routing/fallback artifact).

What it *does* prove: the harness boots real providers, drives the mock channel, and
captures/grades end-to-end.
