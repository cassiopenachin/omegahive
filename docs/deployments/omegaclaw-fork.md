# OmegaClaw fork — base image provenance

The code-provenance record for the **OmegaClaw hive fork** and its **base image**
(deployment spec §3). The base image is the first of the two-image sequence:
`fork at its pinned base SHA + vendored boot + baked embedding model + policy file —
no hive patches`. It boots a self-contained agent container with no board integration,
unblocking the qualification battery's plumbing. The **hive image** (base + patch set +
port client) is recorded in the *Hive image* section below.

**Status:** base image built and boot-smoked on Beastie (rootless Podman), Jul 7 2026.
Registry RepoDigest deferred until the image is pushed (same precedent as deployment #0's
omegahive image).

## Fork

| Fact | Value |
|---|---|
| Fork repo | `cassiopenachin/OmegaClaw-Core` (`origin`); upstream `asi-alliance/OmegaClaw-Core` |
| Base SHA | `1b089bf1e0cf633c999e56284316797f5b952073` — **equals upstream `main` HEAD at cut** |
| Base-image commit | `0bb6217`, merged to `main` as `38ebd6d` and tagged **`omegaclaw-base-v1`** (pushed to `origin`) |
| Evolution rule | OmegaHive-only patches (named, reviewed, tested) + periodic upstream pulls merged only when fork CI (incl. boot smoke) is green |

## Base image lockfile (pinned build inputs)

| Component | Pin |
|---|---|
| Base image (`omegaclaw-base`) | local build, config id `sha256:6932bc4fe5334b7b5352085fd07c215180427d9d1314f6844f7a98485b884e0f` — reproducible from `Dockerfile` + the pins below. RepoDigest recorded once pushed to a registry. |
| swipl base | `docker.io/library/swipl:10.0.2@sha256:f801ce1773c0b909e7ccf48aef979bf4aeab591d43ccaca68014925b904ac237` (multi-arch OCI index digest) |
| PeTTa | commit `d8d46920269ced70cd6236a5182d4d2409c1e12b` (was branch `main`) |
| FAISS | release tag `v1.8.0` (immutable) |
| chromadb (`petta_lib_chromadb`) | commit `456385457e4e99ee049c2c0966988a6cd7ff3705` (was branch `master`) |
| Embedding model | `intfloat/e5-large-v2` @ HF revision `f169b11e22de13617baa190a028a32f3493550b6`. Baked offline: build writes `refs/main` → this revision so the revision-less runtime load resolves the pinned snapshot, and the runtime sets `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` so a networked host never re-resolves `main` and silently ignores the pin. |
| Policy file | `profile/policy.yaml` → `sha256:e69b5549f683ba31a185856af5da7222b5bcaf905eacc734c5336e8a6c033b17` |

## In the base image / not in it

- **In:** vendored boot (no boot-time git fetch — see below), baked `e5-large-v2` offline
  embeddings, the Landlock filesystem policy file, and the inherited upstream security
  stack (env-scrubbing entrypoint, nginx key-isolation proxy).
- **Not in** (these are the *hive image*, applied on top later): `SAFE_VARS` Postgres-DSN
  addition, board-op skill registration, board-channel adapter, LLM-usage logging, history
  bounding, and the omegahive port client + Postgres driver.

## Reproducible boot

Boot-time imports otherwise fetch at container start, so an image without vendored boot is
not actually pinned (§3/§42). The fork change:

- `run.metta` / `lib_omegaclaw.metta`: the two boot-time `git-import!` calls (OmegaClaw-Core
  self-import and `petta_lib_chromadb`) → local `library_path` registration
  (`assertaPredicate (Predicate (library_path "./repos/<name>"))`). Both repos are already
  vendored under `/PeTTa/repos` (fork `COPY`'d, chromadb cloned at the pinned SHA at build
  time), so boot loads them locally with no network.
- `lib_omegaclaw.metta`: removed the long-dangling `./src/context` import (no such module
  exists or was ever tracked; symbol referenced nowhere else).
- **Vendoring is now mandatory** (no boot-time fetch, no clone fallback — a fallback would
  reintroduce the moving-branch fetch this eliminates). Each registration is guarded: if the
  vendored entry module is absent (`access_file … exist` on `lib_omegaclaw.metta` /
  `lib_chromadb.metta` — checking the module rather than just the directory also catches a
  partial checkout), boot prints a clear `FATAL: … not vendored …` message and halts
  (exit 1) instead of failing silently later. In the image both repos always exist, so the
  guard is a no-op there; it protects non-image runs.

**Boot smoke test:** fork CI (`.github/workflows/common.yml`) starts the container on the
mock channel (`-p Test -t test`) and fails the run unless the agent reaches ready
(`CHARS_SENT: [0-9]+`) within 90s. A missing load-bearing vendored module prevents ready,
so this gate catches boot regressions. (Enable GitHub Actions on the fork before relying on
it — forks start with Actions disabled.)

## Verification

Local build + mock-channel boot on Beastie (rootless Podman 5.7.1, Fedora 43,
Landlock-enforcing), Jul 7 2026:

- Image builds reproducibly (all pins baked; verified in-image: PeTTa `d8d4692`, chromadb
  `4563854`, model snapshot `f169b11` with `refs/main`, zero `git-import` in the boot files).
- Container boots on the mock channel (`commchannel=test`) and reaches ready
  (`CHARS_SENT: 3418`) in ~5s, with **zero** import-resolution or Hugging Face connect
  errors. All ~24 vendored MeTTa modules load from the local `/PeTTa/repos` copies; the
  Landlock policy applies ("policy applied"); `initKnowledge` loads the baked `e5-large-v2`
  offline (1024-dim embeddings). This is the same criterion as the fork CI boot gate.

## Hive image

The second image of the sequence: base + the stage-2 board-binding patch set + the pinned
`omegahive` port client + its Postgres driver. It binds the OmegaClaw agent to the OmegaHive
board (read views, emit ops through the port) — the substrate for the stage-2 coordinator
ladder. **Status:** built and DoD-verified on Beastie (rootless Podman), Jul 8 2026; local
build, RepoDigest deferred until pushed. Patch branches are local (pending PRs) — SHAs update
at merge.

### Hive image lockfile

| Component | Pin |
|---|---|
| Hive image (`omegaclaw-hive`) | local build, config id `sha256:74ea5e3c12f462e6ecdfc1316a7301a9c0f821bc9d8dd10586d13c9f72369f38` — reproducible from `Dockerfile.hive` + the base + the pins below |
| Base image it derives from | `omegaclaw-base` @ `omegaclaw-base-v1` (config id `sha256:6932bc4…`) — unmodified |
| omegahive port client | commit `3c4a0fc` (branch `feature/port-render`; adds the shared `omegahive.port.render`), installed as a wheel; production pins the merged tag |
| Postgres driver | `psycopg[binary]` 3.3.4 |
| Python note | base image is Python 3.11; the port surface is 3.11-compatible, installed with `--ignore-requires-python` (omegahive's declared `>=3.12` target is unchanged) |

### The patch set (each a reviewed commit + tests; separate PRs)

| Patch | Branch | Upstreamable |
|---|---|---|
| `SAFE_VARS` hive DSN (`OMEGAHIVE_DATABASE_URL`) | `patch/safe-vars-hive-dsn` | hive-specific |
| LLM-usage logging (`[LLM_USAGE]` beside `[LLM_RAW]`) | `patch/llm-usage-logging` | **yes** (standalone) |
| Board channel adapter (`channels/board.py` + dispatch chains) | `patch/board-channel` | hive-specific |
| Board-op skill (`board`, single-string payload) | `patch/board-skill` (stacks on the adapter) | hive-specific |

Rendering is the shared `omegahive.port.render` (one implementation, consumed by both the fork
adapter and the R1 vanilla harness → identical views). History bounding was deferred.

### Verification (DoD)

`postgres:16` + `db-migrate` + `seed-demo` (the `demo_plan` two-task DAG, run `hive-dod`), then
the hive image on a shared network with `commchannel=board` + the DSN:

- **Startup survives** — policy applied, memory/knowledge init, `initChannels` → the board
  adapter opens the port and starts its 1s poller; the loop reaches `CHARS_SENT`. (The loop's
  LLM call needs a provider/mock harness, out of this gate's scope.)
- **Rendered view delivered** — the adapter reads the board and renders via
  `omegahive.port.render`: `(board (task t1 :status ready …) (task t2 :status created :deps (t1) …))`,
  matching the seeded plan; the basis/cursor/generation store is written under `memory/`.
- **Emit path** — the `board` skill emits through a per-call port client:
  `assign t1 w1 → Accepted task.assigned t1`, `escalate t2 → Accepted`, `prune t2 → Accepted`;
  a malformed op returns an error string, never crashing the loop.
- **Agent-driven closed loop** (`Autotests/test_board_e2e.py`, `RUN_BOARD_E2E=1`) — the *live*
  agent on `commchannel=board` reads the board, its (mock) LLM replies `board "assign t1 w1"`,
  the agent parses and dispatches the skill, the port **accepts**, and `t1` is **assigned in
  Postgres**. `receive(view) → LLM → parse → skill → emit → board change` in one process. An
  illegal reply (`prune t1`) is correctly **rejected** (`ILLEGAL_TRANSITION`, k=1 join) and the
  refusal folds back into the next view — the reject path works too.

This E2E caught a binding bug now fixed: a `configure` (add-atom) equation is invisible to a
`py-call` argument in the same `progn`, so the board channel registration passes argv values via
`argk` directly (`(py-call (board.start_board (argk run_id "") …))`), not `(configure …)`+`(run_id)`.

The full R2 replay-vs-repeat binding smoke (adapter under the real policy, DB fixtures) is the
omegahive-side follow-up that consumes this image.
