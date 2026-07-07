# Repo Hygiene & OmegaClaw Fork Program — Execution Spec

> **OBSOLETE (Jul 6 2026).** Absorbed into [omegahive_deployment_spec.md](omegahive_deployment_spec.md) (§§2–3), which extends it with deployment topology, upgrade/backup/restore/recovery procedures, and the Beastie bring-up plan. One content change in the move: the two-arg board-op round-trip test became conditional on the stage-2 §3.4 payload decision (single-string default). Retained for history only — do not update.

**Status:** Companion to [omegahive_port_spec.md](omegahive_port_spec.md), split out so it can be executed independently — by Claude Code running at `~/code/SNET` (recommended: it needs real git and `gh`, which the cowork sandbox handles poorly on mounted repos). Covers cross-repo git hygiene and the OmegaClaw hive fork. **Interface to the port milestone:** this track delivers a buildable, pinned fork image + container policy file; the port's environment slice consumes them.

## 1. Principle and the failure it prevents

The live deployment demonstrated the anti-pattern this spec exists to prevent: load-bearing code changes living as unversioned edits inside a nested checkout on one machine — unreviewable, unreproducible, one `rm -rf` from gone. The rules: everything a deployment runs is pinned (commits, images, models); every live change is either a reviewed commit, a documented discard-and-redo, or an explicit config entry; nothing boots by fetching moving branches.

**Hard constraint:** the OmegaClaw core team ships a release in mid-July. Nothing in this spec touches upstream OmegaClaw-Core — no PRs, no issues, no pushes — before that release. All work happens on the fork and in hive-owned repos.

## 2. omegahive repo

- CI (GitHub Actions): full test suite + ruff (+ mypy as already configured) on every PR.
- The substrate directories (`events/`, `gateway/`, `board/`, `migrations/`) are frozen: changes land only via reviewed PR. Other directories may move faster.
- Tag any commit a deployment consumes; a deployment's identity is its lockfile (§5).
- Experiment records and specs live in-repo beside the code they describe (already the practice — keep it).

## 3. The OmegaClaw fork program

**Step 0 — base-pinning (blocks everything else).** Locate the live deployment's actual changes:
1. Check GitHub first: the changes may already be pushed (check the owner's fork/branches). If a pushed copy exists, verify it matches what's running (compare against the RUNBOOK's described patches).
2. Otherwise, capture from the laptop (a small task for ZeroBot, read-only): `git rev-parse HEAD`, `git status --porcelain`, and `git diff` (plus untracked-file inventory) in the nested `OmegaClaw-Core` checkout. Deliverable either way: **the base SHA + one archived working-tree diff**, committed as-is to the deployments record before any triage begins. Without this, the fork's base is guesswork.

**Fork creation.** One fork under the org, created at the captured base SHA. Branch naming: `hive/<topic>`.

**Patch triage.** Every live change is classified: *(a)* reviewed, tested commit on the fork; *(b)* documented discard-and-redo; *(c)* explicit deployment-only config entry. Nothing remains an unversioned diff. Known inventory to triage:

| Live change | Likely class |
|---|---|
| Telegram adapter: allowlists (`TG_ALLOWED_USER_ID(S)`, `TG_PRIVATE_ONLY`), offset handling, attachment ingestion + chunking | (a) — general-purpose hardening |
| `loop.metta`: idle gating (`&loops=0` start, reset on fresh input), `continue-thinking` skill + `&continueRequested` wiring | (a) — but core loop semantics: review hardest |
| `helper.py`: send-wrapping of bare replies, no-op suppression | (a)/(b) — judge whether the fix belongs in the provider instead |
| `lib_llm_ext.py`: OpenClaw provider, subprocess bridge (Janus segfault workaround), per-call sessions, 900s timeouts | (a) — the subprocess bridge pattern is load-bearing for the port binding too |
| Local `policy.local.yaml`, supervisor/wrapper scripts, env-file loading | (c) — deployment config, recorded not forked |

**New fork patches the port track needs** (each a named, reviewed commit with a test):
- `SAFE_VARS` additions so the hive Postgres DSN survives the entrypoint's env scrubbing.
- Container `policy.yaml`: read-only resolver paths (`/etc/resolv.conf`, `/etc/hosts`, `/etc/nsswitch.conf`, `/run/systemd/resolve`) so TCP-to-Postgres-by-hostname resolves under Landlock.
- Board-op skills registered across all three catalog sites (skill equations, `getSkills` text, parser command registry), with a round-trip test that a two-arg `(assign t w)` survives the output parser.

**Reproducible boot.** Enumerate every boot-time `git-import!` target in `run.metta`/`lib_omegaclaw.metta`; vendor each at a recorded SHA into the fork; switch to local imports; resolve or stub the known dangling `./src/context` import. Add a boot smoke test (mock-channel startup) to fork CI so a missing vendored module fails the *build*, not a deployment.

**Image.** A Dockerfile building from the fork at a pinned commit, with the embedding model baked in (or a pre-populated cache volume) — never downloaded at first boot — and the policy file above. This image + policy are the deliverables the port's compose profile consumes.

## 4. Other hive repos

Apply the same floor to hive-owned repos as they join (ThreadKeeper fork work, petta-memory if adopted): CI on PRs, pinned consumption, no unversioned deployment edits. Benchmarks and external repos (RCBench and friends) are consumed at pinned commits recorded in experiment records, never edited in place.

## 5. Deployments

The compose profile pins every image and commit; each deployment commits its lockfile (image digests, fork SHA, model hashes) to a deployments record — extending the existing recovery-snapshot pattern to cover *code provenance*, not just workspace state. The live laptop becomes deployment #1 of the profile when the port's environment slice lands.

## 6. Acceptance

- [ ] Base SHA + archived diff of the live checkout committed (step 0)
- [ ] Fork exists at that base; triage table filled in with a/b/c dispositions, each (a) item a reviewed commit with a test
- [ ] Vendored boot: fork CI green including the boot smoke test
- [ ] Fork image builds reproducibly with policy + baked embeddings; `SAFE_VARS` patch verified (agent process sees the DSN)
- [ ] omegahive CI live; substrate-dir PR rule documented in the repo
- [ ] Deployments record exists with the first lockfile
- [ ] Zero upstream contact with OmegaClaw-Core before the mid-July release
