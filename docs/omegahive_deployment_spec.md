# OmegaHive — Deployment & Operations Spec

**Status:** v1 (Jul 6 2026). The deployment-and-recovery document owed by [omegahive_design_1_1.md](omegahive_design_1_1.md) §10.6. **Absorbs** the former repo-hygiene spec (`omegahive_repo_hygiene_spec.md`, now bannered obsolete) and extends it with deployment topology, upgrade/backup/restore/recovery procedures, and the **Beastie bring-up plan** (§7) — the first hive-owned deployment. Companion: [omegahive_port_spec.md](omegahive_port_spec.md) (rev 3) whose environment slice consumes this doc's fork image and container policy.
**Known gap:** the live-deployment materials (the `zerobot-recovery-main` tree, deploy scripts, runbook — in `~/code/SNET`, not mounted in this workspace) are needed for §3 step 0 and to complete §6; items depending on them are marked ⚠.

## 1. Principle, and the failure it prevents

The live deployment demonstrated the anti-pattern this spec exists to prevent: load-bearing code changes living as unversioned edits inside a nested checkout on one machine — unreviewable, unreproducible, one `rm -rf` from gone. The rules: **everything a deployment runs is pinned** (commits, images, models); **every live change is a reviewed commit, a documented discard-and-redo, or an explicit config entry**; **nothing boots by fetching moving branches**; and **every deployment can be rebuilt from its lockfile plus its backups**.

**Hard constraint (unchanged):** the OmegaClaw core team ships a release in mid-July. Nothing here touches upstream OmegaClaw-Core — no PRs, no issues, no pushes — before that release. All work happens on the fork and in hive-owned repos.

## 2. Repo hygiene floor

- **omegahive repo:** CI (GitHub Actions) runs the full test suite + ruff + mypy on every PR. The substrate directories (`events/`, `gateway/`, `board/`, `migrations/`) are frozen: changes land only via reviewed PR; other directories may move faster. Tag any commit a deployment consumes. Experiment records and specs live in-repo beside the code they describe.
- **Other hive repos** (ThreadKeeper fork work, petta-memory if adopted): same floor as they join — CI on PRs, pinned consumption, no unversioned deployment edits.
- **External repos** (RCBench and friends): consumed at pinned commits recorded in experiment records, never edited in place.

## 3. The OmegaClaw fork program

**Base decision (corrected Jul 6, evening): the hive fork is cut from the current upstream `main` HEAD** (`a56a1a0` at time of writing — re-pin at fork creation), tracked through the mid-July release. It evolves with exactly two kinds of change: **(1) OmegaHive-only patches** as the port/binding work needs them, and **(2) periodic upstream pulls** (cadence: on upstream release, or weekly — merge gated on fork CI incl. the boot smoke test) to avoid divergence and keep future PRs easy. Fork-from-HEAD gets upstream's recent work for free: the container security stack (below), the Telegram allowlist (`f306193`), send-wrapping (`0134368`), `[LLM_RAW]` logging, and #217's knowledge-import default change (verify at fork time whether it already mitigates the boot-import item).

**What the earlier survey actually looked at (correction):** `OmegaClaw-Core-private` is **Jon's exploratory fork** — vibe-coded, idea-rich, and *unrelated to omegahive work*; its merge-back-to-upstream review already exists (`fork-review-and-roadmap-mapping.md`). It is a **cherry-pick menu, not a base**: the typed command-syntax membrane (head commit) is directly relevant to emission discipline (C2), and attachment ingestion is a candidate; both flow through the existing review, on their own track.

**Ben's laptop deployment inputs** (reference material for triage, *not* the fork base): the live checkout still holds two changes on no branch anywhere — loop idle-gating + `continue-thinking`, and the OpenClaw provider + subprocess bridge — plus ⚠ patches inside installed npm bundles (a live Telegram fix in `dist/bot-*.js`, in no repo, lost on upgrade). Capture remains a small read-only ZeroBot task (`git rev-parse HEAD`, `git diff`, untracked inventory, bundle diffs), filed as *deployment #1 reference material*: each item is then **reimplemented as a reviewed fork commit or explicitly discarded** — never merged as a diff.

**Fork creation.** One fork under the org at the captured base SHA. Branch naming: `hive/<topic>`.

**Patch triage.** Every live change classified: *(a)* reviewed, tested commit on the fork; *(b)* documented discard-and-redo; *(c)* explicit deployment-only config entry. Nothing remains an unversioned diff. Known inventory:

| Live change | Likely class |
|---|---|
| Telegram adapter: allowlists (`TG_ALLOWED_USER_ID(S)`, `TG_PRIVATE_ONLY`), offset handling, attachment ingestion + chunking | (a) — general-purpose hardening |
| `loop.metta`: idle gating, `continue-thinking` skill + `&continueRequested` wiring | (a) — core loop semantics: review hardest |
| `helper.py`: send-wrapping of bare replies, no-op suppression | (a)/(b) — judge whether the fix belongs in the provider |
| `lib_llm_ext.py`: OpenClaw provider, subprocess bridge (Janus segfault workaround), per-call sessions, 900s timeouts | (a) — the subprocess bridge remains one first-class client shape (mixed population, design §3.4) |
| Local `policy.local.yaml`, supervisor/wrapper scripts, env-file loading | (c) — deployment config, recorded not forked |

**Container stack (settled by the Jul 6 source pass):** upstream's image carries the security stack we want — env-scrub entrypoint (keys never reach the agent process), the nginx key-isolation proxy (all provider/channel credentials live gateway-side), Landlock filesystem policy, `HF_HUB_OFFLINE` pre-baked embeddings. (Jon's fork, which the survey initially mistook for the hive fork, has none of it — one more reason it is a cherry-pick source, not a base.) Forking upstream HEAD inherits the stack for free. Network facts to pin (verified: no network/DNS constraint exists anywhere in code): default-bridge outbound-open egress to the named provider/channel endpoints; no inbound ports (all channels poll); resolver readable under the policy (`/etc` is read-only-allowed).

**Fork patches the port track needs** (each a named, reviewed commit with a test):
- `SAFE_VARS` additions so the hive Postgres DSN survives the entrypoint's env scrubbing.
- The `[LLM_RAW]` stdout logging patch (upstream has it; fork branches don't) — required by the C2 battery's pre-repair parse-rate measurement.
- History rotation (or an `episodes`-skill bound) for resident configurations — `episodes` scans and RAM-buffers the whole history file per call and history never rotates (design §10.4a); a weeks-long resident needs one or the other.
- Board-op skills registered per the **stage-2 §3.4 decision**: default is single-string payloads (one catalog line + equation + one `LLM_COMMANDS` entry; round-trip test that `(assign "t1 w2")` survives the output parser); if the fork-extends option is chosen instead, the parser's two-arg table gains board ops and the round-trip test covers bare two-arg emissions. Either way, a fork-side consistency test asserts the three registration sites agree.
- **Reproducible boot:** enumerate every boot-time `git-import!` target in `run.metta`/`lib_omegaclaw.metta`; vendor each at a recorded SHA; switch to local imports; resolve or stub the dangling `./src/context` import. Boot smoke test (mock-channel startup) in fork CI so a missing vendored module fails the *build*, not a deployment.

**Image.** A Dockerfile building from the fork at a pinned commit, with the embedding model baked in (or a pre-populated cache volume) — never downloaded at first boot — and the policy file above. This image + policy are the deliverables the port's environment slice consumes.

## 4. Deployment topology, lockfiles, and the deployments record

A deployment is one instance of the generic compose profile (port spec §9): `postgres:16` (pinned digest) + migrations job, the `omegahive` gateway library image, the pinned OmegaClaw fork image, the pinned `openclaw-gateway` image (loopback-bound). Secrets via env/env-file only; named volumes for Postgres data and agent memory; no absolute host paths; no privileged containers unless demonstrably required.

**A deployment's identity is its lockfile:** image digests, fork SHA, gateway-library SHA/tag, model hashes, policy-file hash, compose-profile version — committed to the **deployments record** (in-repo). The record extends the existing recovery-snapshot pattern to cover *code provenance*, not just workspace state. The live laptop becomes deployment #1 of the profile when its base is captured (§3 step 0); **Beastie is deployment #0**, ours (§7).

## 5. Operating procedures

**Upgrade / migration.** Before any component upgrade: **diff and capture installed-bundle patches** (the live deployment carries fixes as edits inside installed npm bundles — §6; an upgrade silently destroys them). Migrations run only with coordinators drained: documented stop → migrate → restart ordering; there is no online-migration story and none is promised. The gateway library version is pinned per run; never hot-swap the library under a live run. **Long-run collision (named, unresolved):** a weeks-long residency run cannot be "restarted" without a story for its board state — the resolution depends on the run-mapping decision (design §10.3: one-run-per-project keeps runs bounded and the pin rule intact; a long hive-run requires run *segmentation* — a drain emits a run-continuation event, the pin applies per segment). Decide there; this spec records the constraint.

**Backup.** Scheduled `pg_dump` of the log store (it is the source of truth for all coordination history); agent-memory volumes snapshotted on the same schedule; artifact store (stage 5) joins when it exists. Backup restore is *drilled*, not assumed (T1 check 3).

**Restore.** Restore-from-dump rewinds the log and **invalidates every live client cursor** (seq values are reused past the restore point — silent skips otherwise). Procedure: stop coordinators → restore → **bump the log-generation token** (port spec rev 3 §2) → restart; clients holding a stale generation receive `GENERATION_MISMATCH`, drop cursors, re-snapshot. Until the token lands (port slice 3), the interim floor is procedural: restore ⇒ restart every client, no exceptions.

**Recovery.** A human-only, agent-free out-of-band path (plain SSH) exists at all times; no self-managing component can lock humans out; recovery credentials never appear in any agent container (T1 check 5 scans; E2.5 drills with adapters down). The runbook lives beside this spec in-repo and is exercised per release.

**Environment change** (new host, network change, the eventual ASI:cloud move): re-run T1 structural checks (tier routing fact, credential scope scan) and the injection-relevant T4 subset before agents resume. This checklist is the reason those checks are harness scripts, not manual steps.

## 6. Learnings from the live deployment (deployment #1-to-be)

From validated docs plus the recovery snapshot and source (Jul 6 pass):

- **What broke coordination-side** (the "before" evidence, test plan D2): a 998k-token session blow-up; history-pollution crash cascades; lost cross-session context; role boundaries as prose; no audit trail.
- **What broke infrastructure-side:** unversioned nested-checkout patches *and unversioned installed-bundle patches* (a live fix inside OpenClaw's npm `dist/` — in no repo, lost on upgrade; → §1's rules and the §5 upgrade procedure); boot-time `git-import!` drift (→ vendored boot); in-process Janus instability under large calls (→ the subprocess bridge; one shape in the mixed client population); env-file/supervisor sprawl (→ class-(c) config entries).
- **Operational facts that are restore-critical state** (from the recovery snapshot): the cron inventory *is* the worker schedule — staggered 2-hour lanes per project plus a daily-summary cron, all timezone-pinned (America/Vancouver) and posting to specific Telegram group ids; semantic-search state lives in a local sqlite index + cached embedding models that must be rebuilt after restore; remote access via Tailscale hostnames with an SSH-keys-only rule. All of this belongs in a deployment's lockfile-adjacent record, not in anyone's head.
- **Gaps the live deployment demonstrates:** backup was a one-time setup snapshot — no recurring cadence, no restore drill, ever (→ §5 makes both mandatory); host toolchain gaps (`docker`, `pytest` missing from expected paths) repeatedly degraded background workers (→ pin dev deps in images).
- **What worked and is kept:** the recovery-snapshot habit (generalized into lockfiles + scheduled backups); Telegram as the human surface (an adapter, not the coordination medium); allowlist-based channel trust (→ 1.1's group-trust model); loopback-only gateway, approval-backed exec allowlist, secret scans — the reference security posture to carry into hive deployments.

## 7. Beastie bring-up (deployment #0)

Goal: the stage-1 acceptance run and T1 core on hive-owned hardware — proving the generic profile on a machine nobody hand-built, before any resident agent lands on it.

**Host facts to fill in before starting** *(one table, committed to the deployments record)*: OS + kernel (Landlock needs ≥5.13 for the fork's policy to enforce rather than no-op), Docker/compose versions, CPU/RAM/disk (reference sizing: 1.1 §5.1 suggests 16 vCPU/64 GB/1 TB for a full hive; the stage-1 slice needs far less), network position (who can reach it; whether Postgres is loopback-only), team access (SSH keys = the out-of-band recovery path from day one).

**Bring-up sequence:**
1. Clone the omegahive repo at a tag; `compose up` the profile (Postgres + migrations + gateway library image — the OmegaClaw container is *not* needed for stage-1 acceptance and joins at the spike).
2. Run the port acceptance sequence (port spec §9): seed the demo plan → greedy coordinator via the port → expected terminal board state, executed from the README by someone who didn't build the profile.
3. Run T1 core checks 1–5 (acceptance, migration idempotence, snapshot/restore, tier-routing fact, credential scan).
4. Commit deployment #0's lockfile + host-facts table to the deployments record.
5. Schedule the backup job (§5) from day one — Beastie is a real deployment from its first hour.
6. At stage 2, add the pinned fork image and re-run T1; Beastie becomes the spike host, and later the D4/T2 harness host.

**Non-goals for #0:** no outbound capability, no credentials beyond the DB role, no chat adapters, no resident agents — those arrive with their stages and their gates.

## 8. Acceptance checklist

- [ ] Hive fork cut from upstream `main` HEAD (SHA recorded); upstream-pull cadence + CI merge gate in place
- [ ] ⚠ Laptop reference capture: idle-gating/`continue-thinking` + OpenClaw-provider/subprocess-bridge diffs, installed-bundle patches, live base SHA — filed as deployment-#1 reference material; each item reimplemented-or-discarded, never merged as a diff
- [ ] Jon's-fork cherry-pick review connected: membrane + attachment-ingestion candidates dispositioned via the existing merge-back track
- [ ] Fork exists at that base; triage table filled with a/b/c dispositions; each (a) item a reviewed commit with a test
- [ ] Vendored boot: fork CI green including the boot smoke test; dangling import resolved
- [ ] Fork image builds reproducibly (policy + baked embeddings); `SAFE_VARS` patch verified (agent process sees the DSN)
- [ ] Board-op registration matches the stage-2 §3.4 decision, with the three-site consistency test
- [ ] omegahive CI live; substrate-dir PR rule documented in-repo
- [ ] Deployments record exists; **deployment #0 (Beastie) lockfile + host-facts committed; acceptance + T1 core green on Beastie**
- [ ] Backup job scheduled on Beastie; restore drill executed once (with generation-bump or full-client-restart interim procedure)
- [ ] Recovery runbook exists in-repo; out-of-band path verified from outside the hive
- [ ] Zero upstream contact with OmegaClaw-Core before the mid-July release
