# Retired harness binding descriptors — historical evidence

These two files were the shipped **permission-boundary descriptors**: one per launchable
harness, mapping each of `permissions.md`'s four policy classes to native mechanisms and
focused probes, pinned by digest from every route in the deployment catalog. Between
2026-08-12 and 2026-08-20 a route could not launch a worker unless its descriptor was
present, matched its pinned bytes, covered all four classes with something enforceable,
and carried a passing verification record.

**That rule is retired** (`2026-08-20-doctrine-runner-trust-v2.md` in the workspace, and
the `worker-transport` cutover in this repository). Nothing reads these files. They are
kept, not deleted, because the probe records beside them in `docs/evidence/` —
`harness_binding_probe_claude_code_2026_08_14.md`,
`harness_binding_probe_codex_2026_08_14.md` — are measurements *of these exact bytes*,
and a measurement whose subject has been deleted is unreadable.

- `claude-code.v1.json` — status `proven`, against Claude Code 2.1.231. The residuals it
  records are the ones the doctrine argued from: a prefix rule is evaded by an absolute
  path or an interpreter, an allowed interpreter reaches any secret, and the file the
  supervisor checked lived at the worker's own user.
- `codex.v1.json` — status `declared`, demoted from `proven` by operator rejection on
  2026-08-20. Its long note is the record of why: a P4 proof that measured a permission
  profile's `network` table while the feature gate that mechanism needs was switched off.

The live operator guide is `docs/omegahive_worker_harness.md`; the retired product's own
guide is `docs/omegahive_worker_boundary.md`, similarly labelled.
