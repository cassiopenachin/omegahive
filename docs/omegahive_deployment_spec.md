# OmegaHive — Deployment & Operations Spec

**Status:** v2 (Jul 7 2026). Covers repository hygiene, the OmegaClaw fork program, deployment topology and provenance, operating procedures, and the bring-up of **Beastie (deployment #0)**. Standalone: everything needed to execute is in this document; companions are cited for depth, not required — [omegahive_port_spec.md](omegahive_port_spec.md) (the substrate being deployed), [omegahive_design_1_1.md](omegahive_design_1_1.md) (the architecture), [omegahive_c2_battery_spec.md](omegahive_c2_battery_spec.md) (the qualification battery that runs on these deployments). Revision record: §9.

## 0. Execution model — what depends on what

This spec describes **three workstreams that run in parallel**, not a sequence:

- **A. Repository hygiene + the port build** (§2) — in the omegahive repo; independent of everything below.
- **B. The OmegaClaw fork program** (§3) — independent of A and C until its *image* is consumed.
- **C. Beastie bring-up** (§7) — preparation steps start anytime; execution steps wait on exactly one artifact from A.

Only two cross-dependencies exist:

1. **The tagged compose profile** (the port's environment slice) gates Beastie's acceptance run (§7 steps 1–5).
2. **The fork image** (§3) gates adding an agent container to any deployment (stage 2 of the build plan) and the battery's container runs. It gates nothing else — in particular, *not* Beastie's bring-up.

Anything not on those two edges can start immediately.

## 1. Principle, and the failure it prevents

The first live deployment (Ben's laptop, predating this spec) demonstrated the anti-pattern this spec exists to prevent: load-bearing code changes living as unversioned edits — inside a nested checkout, and in one case inside an installed npm bundle — unreviewable, unreproducible, destroyed by any upgrade or one `rm -rf`. The rules: **everything a deployment runs is pinned** (commits, images, models); **every live change is a reviewed commit, a documented discard-and-redo, or an explicit config entry**; **nothing boots by fetching moving branches**; **every deployment can be rebuilt from its lockfile plus its backups**.

**Hard constraint:** the OmegaClaw core team ships a release in mid-July. Nothing here touches upstream OmegaClaw-Core — no PRs, no issues, no pushes — before that release. All work happens on the fork and in hive-owned repos (pulling *from* upstream is fine).

## 2. Repository hygiene floor

- **omegahive repo:** CI runs the full test suite + ruff + mypy on every PR. The substrate directories (`events/`, `gateway/`, `board/`, `migrations/`) are frozen: changes land only via reviewed PR; other directories may move faster. Tag any commit a deployment consumes. Experiment records and specs live in-repo beside the code they describe.
- **Single documentation home:** every spec, design document, and experiment record has exactly one editable copy, in-repo under `docs/` (current), `docs/evidence/` (experiment records and analyses), or `docs/archive/` (superseded, retained for history). Copies elsewhere are pointer stubs, never edited. Rationale: build agents read the docs from the repo; a second editable home guarantees drift.
- **Other hive-owned repos** join under the same floor: CI on PRs, pinned consumption, no unversioned deployment edits.
- **External repos** (benchmarks and the like) are consumed at pinned commits recorded in experiment records, never edited in place.

## 3. The OmegaClaw fork program

**Base and evolution.** The hive fork is cut from the **current upstream `main` HEAD** (re-pin the exact SHA at fork creation and record it in the deployments record). It evolves with exactly two kinds of change:

1. **OmegaHive-only patches** (below), each a named, reviewed commit with a test;
2. **periodic upstream pulls** — on upstream releases, or weekly — merged only when fork CI (including the boot smoke test) is green. This keeps divergence small and future upstream PRs cheap.

**Why upstream HEAD is the right base.** Upstream's image already carries what a hive resident needs: the **container security stack** — an env-scrubbing entrypoint (provider keys never reach the agent process), an nginx key-isolation proxy (all provider/channel credentials live gateway-side), a Landlock filesystem policy, and pre-baked offline embeddings — plus Telegram allowlisting, send-continuation handling, and raw-LLM-reply stdout logging (`[LLM_RAW]`), which the qualification battery requires. Forking HEAD inherits all of it.

**The hive patch set** (each a reviewed commit with a test):

- **`SAFE_VARS` addition** so the hive's Postgres DSN survives the entrypoint's environment scrubbing (verified: the agent process sees the DSN).
- **Board-op skill registration** per the binding's single-string-payload rule (one skill, one string argument, parsed in the skill body), registered across all three catalog sites (skill equation, catalog text, parser command table) with a consistency test asserting the three agree and a round-trip test that an emitted board op survives the output parser.
- **History bounding for residents:** rotation for `memory/history.metta`, or a bound on the `episodes` skill (which scans and RAM-buffers the entire history file per call; the file otherwise grows without limit on the container's only writable volume). A weeks-long resident needs one or the other.
- **Reproducible boot:** enumerate every boot-time `git-import!` target; vendor each at a recorded SHA and switch to local imports (check first how much upstream's recent knowledge-import default change already covers); resolve or stub the known dangling `./src/context` import. A boot smoke test (mock-channel startup) in fork CI makes a missing vendored module fail the *build*, not a deployment.

**The image.** A Dockerfile building from the fork at a pinned commit, embedding model baked in (never downloaded at first boot), policy file included. Network posture, pinned in compose: outbound-only to the named provider/channel endpoints, no inbound ports (all channels poll), resolver readable under the filesystem policy. There is no code-level egress or DNS restriction — the compose profile is where the posture is enforced.

**Deployment #1 inputs (the live laptop): describe-and-redo, not capture-and-port.** The first live deployment carries changes that exist in no repository. At the architecture level this description already exists — the OmegaHive 1.1 spec is the operator's intent statement for what his deployment taught him, and the design doc's component map is its disposition record. What remains is the concrete-code residue: the operator provides a **description of each remaining change's intent**; each item is then reimplemented against the fork under normal discipline (reviewed commit, test), absorbed as a requirement into an existing design, or discarded with the reason recorded in the deployments record. Raw diffs are never merged and no forensic archive of the live tree is kept. Expected dispositions for the known items: loop idle-gating + `continue-thinking` skill → already absorbed as requirements in the resident wake-mechanism design; the OpenClaw LLM-provider + subprocess bridge → superseded by the port binding's client model (redo fresh only if deployment #1 keeps the LLM-via-OpenClaw-gateway topology); installed-bundle hotpatches (e.g. a Telegram media fix inside OpenClaw's npm `dist/`) are **never reimplemented as patches** — check whether the current upstream version already fixes the issue, otherwise file an upstream issue.
Two safeguards: (1) a five-minute read-only **inventory** — `git status --porcelain`, `git diff --stat`, a list of locally patched installed packages; names and sizes only, not content — enumerates what exists so nothing is silently forgotten; (2) any behavior the operator misses after migrating to the profile is filed as an ordinary fork or operator-pack backlog item, never restored from the old tree.

## 4. Deployments, lockfiles, and the deployments record

A deployment is one instance of the generic compose profile: `postgres:16` (pinned digest) + a migrations job, the `omegahive` image (built from the repo at a pinned tag; one image serves library, tools, and tests, all run as one-shot compose services — **a host needs an OCI runtime and compose, nothing else**; no host-side language runtimes, ever), and — from stage 2 onward — the pinned fork image and the OpenClaw gateway image (loopback-bound). Backups run containerized too: the scheduled job invokes `pg_dump` via a one-shot service from the postgres image, triggered by a host systemd timer. Secrets via env/env-file only; named volumes for Postgres data and agent memory; no absolute host paths; no privileged containers.

**A deployment's identity is its lockfile:** image digests, fork SHA, gateway-library tag, model hashes, policy-file hash, compose-profile version — committed to the **deployments record** (in-repo), together with the host-facts table (§7). The record covers *code provenance*, not just workspace state. **Beastie is deployment #0** (ours, §7); the first live deployment becomes **deployment #1** of the profile once its §3 capture completes.

## 5. Operating procedures

**Upgrade / migration.** Before any component upgrade: diff and capture installed-bundle patches (§1's npm-bundle lesson — an upgrade silently destroys them). Migrations run only with coordinators drained: documented stop → migrate → restart ordering; there is no online-migration story and none is promised. The gateway library is pinned per run; never hot-swap it under a live run. **Named open issue:** a weeks-long residency run cannot be "restarted" without a story for its board state — resolution belongs to the run-mapping decision (design doc §10.3: one-run-per-project keeps runs bounded and the pin rule intact; a long-lived hive-run requires run segmentation). This spec records the constraint; that decision resolves it.

**Backup.** Scheduled `pg_dump` of the log store (the source of truth for all coordination history); agent-memory volumes snapshotted on the same schedule; the artifact store joins when it exists (stage 5). Backups are *drilled*, not assumed — a restore drill is part of every deployment's acceptance and recurs per release.

**Restore.** Restore-from-dump rewinds the log and **invalidates every live client cursor** (sequence values are reused past the restore point). Procedure: stop coordinators → restore → **bump the log-generation token** (port spec §2) → restart; clients holding a stale generation receive a distinguishable mismatch signal, drop cursors, and re-snapshot. Until the generation token is deployed, the interim floor is procedural: restore ⇒ restart every client, no exceptions.

**Recovery.** A human-only, agent-free out-of-band path (plain SSH) exists at all times; no self-managing component can lock humans out; recovery credentials never appear in any agent container. The recovery runbook lives in-repo beside this spec and is exercised per release.

**Environment change** (new host, network change, cloud migration): re-run the structural security checks (tier-routing fact, credential-scope scan) and the injection-relevant governance drills before agents resume. These are harness scripts precisely so this checklist is executable, not aspirational.

## 6. Lessons from the first live deployment

Distilled from the deployment's recovery snapshot and operating records; each lesson is already encoded in a rule above.

- **Coordination-side failures** (the "before" evidence for the board-vs-chat comparison): a 998k-token session blow-up; history-pollution crash cascades; lost cross-session context; role boundaries as prose; no audit trail.
- **Provenance failures:** unversioned nested-checkout patches and installed-bundle edits (→ §1, §3, §5-upgrade); boot-time fetches from moving branches (→ §3 reproducible boot); in-process embedded-runtime instability under large calls (→ the subprocess bridge; the port serves both short-lived and long-lived clients).
- **Restore-critical operational state that lived in nobody's records:** the cron inventory *is* the worker schedule (staggered per-project lanes, timezone-pinned, posting to specific channel ids); semantic-search state lives in a local index plus cached models that must be rebuilt after restore; remote access conventions (SSH keys only). All of this belongs in the deployments record (§4).
- **Gaps now closed by rule:** backup existed as a one-time snapshot with no cadence and no drill (→ §5); missing host toolchain (`docker`, `pytest`) repeatedly degraded background workers (→ pin dev dependencies in images).
- **Worth keeping:** Telegram as a human surface only (an adapter, never the coordination medium); allowlist-based channel trust; a loopback-only gateway; approval-backed exec allowlists; secret scans before pushes.

## 7. Beastie bring-up (deployment #0)

Goal: the port's acceptance run and the core deployment checks on hive-owned hardware — proving the profile on a machine nobody hand-built, before any agent lands on it.

**Preparation (start anytime; no dependencies):**
1. Fill the **host-facts table** and commit it to the deployments record: OS + kernel (Landlock enforcement needs ≥ 5.13 — below that the policy is a silent no-op), **container runtime + compose route** (either Docker, or — recommended on Fedora-family hosts — rootless Podman with its Docker-compatible API socket enabled, `DOCKER_HOST` pointed at it, and the genuine compose v2 binary; avoid podman-compose, whose `depends_on: service_healthy` support is unreliable and our migrations ordering depends on it), CPU/RAM/disk (the full-hive reference sizing is 16 vCPU / 64 GB / 1 TB NVMe; the port slice needs far less), network position (who can reach it; Postgres loopback-only), team SSH access — which *is* the out-of-band recovery path (§5), so it's set up first, not last.
2. Prepare the backup job and its storage target (§5) — scheduled from Beastie's first hour, not retrofitted.
3. **SELinux-enforcing hosts (Fedora/RHEL family):** bind-mounted volumes in the compose file need `:z`/`:Z` labels (or a per-service label option) regardless of runtime — without them, container writes fail with permission errors that masquerade as volume-ownership bugs.
4. **Rootless-runtime note for later stages:** when the agent container joins (stage 2), verify the runtime's seccomp default permits the `landlock_*` syscalls (the agent applies its filesystem sandbox in-process) — a two-line container check, run once and recorded. Rootless operation is preferred where available: it removes the root-daemon/socket escalation class entirely, which matters on hosts whose agents' only containment boundary is the container.

**Execution (waits on exactly one artifact: the tagged compose profile from the port's environment slice):**
5. Clone the omegahive repo at the tag; `compose up` — Postgres + migrations + gateway library. **The agent container is not part of deployment #0**; it joins at stage 2 with the fork image (§0 dependency 2).
6. Run the acceptance sequence (port spec §9): seed the demo plan → run the reference coordinator through the port → the board view shows the expected terminal state — every step a one-shot compose service (no host Python), executed from the README by someone who didn't build the profile.
7. Run the deployment checks: acceptance (above), migration idempotence on a second run, snapshot + restore → identical replayed board state, the tier-routing structural fact, the credential-scope scan.
8. Commit deployment #0's **lockfile + host-facts** to the deployments record; schedule the backup job; run the restore drill once.
9. Afterwards, as later stages land: stage 2 adds the pinned fork image (re-run the checks); Beastie then hosts the coordinator spike and the qualification battery's model runs.

**Non-goals for #0:** no outbound capability, no credentials beyond the database role, no chat adapters, no resident agents — each arrives with its stage and its gate.

## 8. Acceptance checklist

- [ ] Hive fork cut from upstream `main` HEAD; SHA in the deployments record; upstream-pull cadence + CI merge gate live
- [ ] Hive patch set landed as reviewed commits with tests (`SAFE_VARS` verified; board-op three-site consistency + parser round-trip; history bounding; vendored boot + smoke test; dangling import resolved)
- [ ] Fork image builds reproducibly (pinned commit, baked embeddings, policy file); network posture pinned in compose
- [ ] Deployment-#1 dispositions filed: operator's intent descriptions + the five-minute inventory; every item redone-under-discipline, absorbed-as-requirement, or discarded-with-reason (§3)
- [ ] omegahive CI live; substrate-directory PR rule documented in-repo
- [ ] Deployments record exists; **deployment #0 (Beastie): lockfile + host-facts committed; acceptance + deployment checks green; backup scheduled; restore drill executed once**
- [ ] Recovery runbook in-repo; out-of-band path verified from outside the hive
- [ ] Zero upstream contact with OmegaClaw-Core before the mid-July release

## 9. Revision record

**v1 (Jul 6):** initial version, absorbing the former repo-hygiene spec and adding topology, procedures, and the Beastie plan.
**v2 (Jul 7):** standalone rewrite. Removed conversational correction narratives and out-of-scope material; added §0 (execution model / dependency map — the fork program and Beastie preparation are parallel workstreams, not sequence steps); restructured §7 into preparation-vs-execution with the single gating artifact named; moved fork-base rationale and the history-bounding patch into §3 as plain requirements; recast §6 as lessons-with-rules rather than survey findings. No substantive policy changes from v1.
**v2.3 (Jul 7):** the `omegahive` image becomes part of deployment #0 — all tooling (migrations, seed, coordinator, board-view, tests, backup pg_dump) runs as one-shot compose services; hosts carry no language runtimes; lockfiles pin the image digest. Closes the unpinned host-`uv` execution surface.
**v2.2 (Jul 7):** runtime-neutral: Docker or rootless Podman (recommended on Fedora-family hosts — no root daemon/socket escalation class; quadlet/systemd fits the ops floor), with the compose v2 binary via Podman's Docker-compatible socket rather than podman-compose; SELinux volume-label and seccomp/Landlock verification items added to §7; port spec §9 acceptance wording aligned. Escape hatch recorded: if the Podman route costs more than a day, install docker-ce and record the choice in host-facts — deployment #0 proves the profile, not a runtime.
**v2.1 (Jul 7):** deployment-#1 policy changed from capture-and-port to **describe-and-redo** — intent descriptions from the operator, each item redone under discipline / absorbed as a requirement / discarded with reason; a five-minute names-and-sizes inventory as the only forensic step; installed-bundle hotpatches never reimplemented (upstream check or upstream issue instead).
