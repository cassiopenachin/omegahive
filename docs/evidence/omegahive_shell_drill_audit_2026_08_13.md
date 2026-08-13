# Shell drill audit — 2026-08-13

Basis: the `worker-harness-core` order's Scope 8, which folds the fired "drills are code"
hygiene trigger into the next `scripts/`-touching order. Every shell file in the repository
was read in full: 20 shell files, 5,006 lines, plus `.github/workflows/ci.yml`.

This file is the **inventory**. What was fixed, and what was deliberately left as a reported
design class rather than repaired in passing, is recorded in the order's result report.

## Files read

| Path | Lines | `set -euo pipefail` |
|---|---|---|
| `scripts/hive-tooling-drill.sh` | 1339 | yes, L28 |
| `scripts/hive-metrics-drill.sh` | 701 | yes, L15 |
| `scripts/hive-metrics` | 529 | no — inherited from sourced `hive-common.sh:26` |
| `scripts/hive-common.sh` | 524 | yes, L26 |
| `scripts/hive-score` | 485 | no — inherited |
| `scripts/hive-bringup-drill.sh` | 451 | yes, L48 |
| `scripts/hive-launch` | 396 | no — inherited (the `set -euo pipefail` at L255 is inside the generated-wrapper heredoc) |
| `deploy/hive-user/restore_rehearsal.sh` | 327 | yes, L42 |
| `scripts/deploy_checks.sh` | 264 | yes, L15 |
| `scripts/hive-init-workspace` | 235 | yes, L42 |
| `deploy/hive-user/precheck.sh` | 205 | `set -uo pipefail` — `-e` deliberately off (documented L23) |
| `scripts/hive-answer` | 155 | no — inherited |
| `scripts/hive-close` | 154 | no — inherited |
| `scripts/credential_scope_scan.sh` | 111 | yes, L26 |
| `scripts/hive-init-secrets` | 85 | yes, L25 |
| `deploy/phantom_ahead.sh` | 62 | `set -eu` (sh) — no pipefail |
| `deploy/git_bundle.sh` | 56 | `set -eu` |
| `scripts/roles_rollback.sh` | 51 | `set -eu` |
| `scripts/pg_backup.sh` | 31 | `set -eu` |
| `scripts/pg_restore_check.sh` | 25 | `set -eu` |

`.github/workflows/ci.yml` (48 lines) runs `uv sync`, `ruff`, `mypy`, `pytest` — **no shell
script, no drill, and no `shellcheck`. Nothing in this audit is exercised by CI.** Numerous
`# shellcheck disable=` waivers imply shellcheck was run by hand at some point; nothing
enforces it.

## Class A — a green report over a broken system

The top-severity class: the drill says PASS (or exits 0) while the property it checks is false.

**A1 — `hive-metrics-drill.sh` always exits 0.** L702 is the last statement, a `check` call.
`check`/`bad` both return 0, and there is no terminal `[ "$FAIL" -eq 0 ]` — unlike
`hive-tooling-drill.sh:1339`, `deploy_checks.sh:264`, and `hive-bringup-drill.sh:157`. The
EXIT trap prints `FAILURES PRESENT` but never changes the status. Any caller doing
`hive-metrics-drill.sh && record_green` records a passing drill over failing checks.

**A2 — `credential_scope_scan.sh` cannot produce its own "could not run" code.** Its header
(L22-25) defines exit 2 as "the scan COULD NOT RUN", but L74 (`IDS="$(... ps -q)"`) aborts
under `set -e` with compose's status 1, and the terminating pipeline at L108-111 also yields
1. `deploy_checks.sh:245-261` then treats non-2 as "findings" and, with `SCAN_FATAL=0`
(default), prints `[WARN] ... not failing this run` and exits 0. A dead container engine or a
renamed manifest is recorded as a passed credential scan.

**A3 — live-port guards silently skipped (`printf | grep -q` under `pipefail`).**
`hive-bringup-drill.sh:271` and `restore_rehearsal.sh:221`. `grep -q` exits at the first
match, `printf` takes EPIPE, `pipefail` makes the pipeline non-zero, and the `if` is false
**precisely when the pattern matches**. `$cfg` is a resolved `compose config` of a 13-service
stack, routinely past the 64 KiB pipe buffer. Verified empirically. In the bringup drill this
turns a scratch stack that publishes the live 5432 into `[PASS] scratch config publishes no
live port`, immediately before `dc up -d postgres`. `restore_rehearsal.sh:221` is additionally
narrower — it matches only `published: "5432"`, missing an unquoted rendering entirely.

**A4 / A5 — `hive-launch` fails open on every spine read.** `board_status` and
`global_in_review` (`hive-common.sh:486,491,521,522`) discard stderr, and `die()` inside a
command substitution exits only the subshell. Any read failure — stack down, wrong
`OMEGA_DIR`, missing `jq` (which `hive-launch`, unlike `hive-metrics:92` and `hive-score:86`,
never checks for) — collapses to the empty string. Consequences: an `in_review` task reads as
absent and `hive-launch:281` re-emits `task.created`, which the script's own comment
(L109-111) says silently regresses it to fresh; and `N_REVIEW=0` disables the review-WIP
throttle. Both fail open, and both fail open exactly when the spine is unhealthy.

**A6 — `hive-score`'s calibration rewrite can no-op and still report success.** L448-458:
`awk ... > "$CAL_TMP" && mv "$CAL_TMP" "$CAL"`. A command that is not final in an `&&` list is
exempt from `set -e`, so a failed `awk` short-circuits `mv`, leaves `calibration.md`
unchanged, leaks the temp file, and falls through to the success print at L463-466 with exit
0. `hive-close:141` then reports `scored`.

**A7 — `precheck.sh` reports scratch ports FREE when `ss` is absent.** L59/L66; `-e` is off by
design, so a missing `ss` (Linux-only) yields an empty pipeline and `pass "scratch port ${p}
is free"` for a port that may be bound.

**A8 — `hive-metrics-drill.sh:701` asserts a false property, vacuously.** `check "neither tool
commits to git" "! { code '$M'; code '$S'; } | grep -E 'git .*(commit|push)'"`. Both tools
*do* commit and push, via `commit_metrics` (`hive-common.sh:444-464`, called at
`hive-metrics:527` and `hive-score:483`) — which the same drill asserts at L353 and L523. The
grep is confined to two files and cannot see the sourced helper.

**A9 — `deploy/phantom_ahead.sh` reports a git failure as a completed analysis, exit 0.** L35:
a command substitution in a `for` word-list is not subject to `set -e`. If both git forms
fail, the list is empty and L44-49 prints "the ENTIRE HEAD history is phantom-ahead" then
exits 0 — a specific-looking verdict from a git command that never ran, in a post-restore
data-safety tool.

**A10 — `restore_rehearsal.sh`'s "no dump found" refusal is dead code.** L146-153:
`DUMP=$(newest ...)` is final in its `||` list, so `set -e` applies and `pipefail` propagates
the empty glob's exit 2. The shell exits 2 silently; L152's `die` is unreachable.

## Class B — false red / misdiagnosis

Not green-over-broken, but they send the operator to the wrong place.

- `hive-answer:82` — `CHANGED_N=$(printf '%s\n' "$CHANGED" | grep -c .)`. With `$CHANGED`
  empty, `grep -c` prints 0 and exits 1; the bare assignment trips `set -e` and the script
  aborts with **no message at all**, bypassing L83-84's intended refusal.
- `hive-close:83-86` — a `report` failure, missing `jq`, or stack outage is reported as
  "the worker's result is malformed". Actively misleading.
- `hive-close:76` — a spine-read failure is reported as "task is not on the board (wrong task
  or project?)".
- `deploy_checks.sh:214` — the restore check runs with no `||`, so a failure aborts the
  harness before check 3 can print `[FAIL]`, with all output discarded.
- `hive-bringup-drill.sh:214-215` — the idempotence re-run has no `||`, so a genuinely
  non-idempotent bootstrap kills the drill instead of reporting the defect it exists to find.
- `hive-bringup-drill.sh:308` — `grep -qi healthy` also matches `unhealthy`.
- `hive-bringup-drill.sh:363`, and the `grep -q` false-red list in class C2 below.
- `restore_rehearsal.sh:236-239` — a bare assignment from `compose ps` inside the
  wait-for-healthy retry loop: a transient failure while postgres is starting (the exact state
  the loop exists to wait through) aborts the whole rehearsal under `set -e`.
- `hive-tooling-drill.sh:248` — `raw_emit ... >/dev/null 2>&1` discards both streams *and* the
  exit code in the fixture seeder; a rejected emit leaves the fixture unseeded and the
  subsequent check reports a product failure.
- `hive-tooling-drill.sh:235-246` — `bstatus`/`btitle`/`bcount_review` swallow read failures,
  so L362's "durable omegahive run untouched by launch" **passes when the stack is down**.

## Class C — cross-cutting patterns

**C1 — negated assertions pass on any error.** `check "..." "! <cmd>"` treats "the command
failed for an unrelated reason" as "the property holds". 20 instances:
`hive-tooling-drill.sh` L589, 595, 605, 629, 736, 756, 757, 860, 908, 994, 1317;
`hive-metrics-drill.sh` L422, 435, 440, 471, 473, 683, 699, 700, 701. Example:
`hive-metrics-drill.sh:435` passes "no impossible clock strings" when `tasks.md` does not
exist at all (grep exits 2, `!` inverts to true). The correct shape is capture → assert
non-empty → assert absence.

**C2 — `grep -q` / `head -1` as a pipeline consumer under `pipefail`.** Both drills document
this hazard at length and fix it in `log_has`, then leave it everywhere else.
False-green instances: `hive-bringup-drill.sh:271`, `restore_rehearsal.sh:221`,
`hive-launch:163` (the duplicate-pane guard, whose die text calls the pane "the registry"),
`hive-tooling-drill.sh:605`, `hive-metrics-drill.sh:471,473`.
False-red instances: `hive-bringup-drill.sh:308,363`; `hive-tooling-drill.sh:147,352,357,391,
392,399,404,419,431,444,464,604,606,785,786,820,822,895-903`; `hive-metrics-drill.sh:36`;
`hive-answer:150`.
Benign (status discarded in `$( )`): `hive-metrics-drill.sh:506`; `hive-common.sh:363,366,369`;
`hive-bringup-drill.sh:370`; `restore_rehearsal.sh:148,239`; `hive-tooling-drill.sh:826`.

**C3 — five production scripts have no `set -euo pipefail` of their own.** `hive-launch`,
`hive-close`, `hive-answer`, `hive-metrics`, `hive-score` rely entirely on `hive-common.sh:26`
running during `source`. A refactor that guards or moves that line silently removes `-e`,
`-u`, and `pipefail` from all five at once.

**C4 — four incompatible compose-resolution orders.** `hive-common.sh:113-125` (probe each
candidate by running it: podman → docker → docker-compose); `deploy_checks.sh:35-40` (docker
first, two by presence); `restore_rehearsal.sh:85-91` (presence only, and **never considers
`docker compose`**, the v2 plugin); `precheck.sh:185` (presence of `docker-compose` *or*
`docker`). `hive-common.sh:103-110` records the damage this class already caused once —
"deploy-checks came back green over a dead loop". Only `hive-common.sh` was fixed;
`credential_scope_scan.sh` and `hive-bringup-drill.sh` correctly reuse it.

**C5 — platform-specific syntax.**

| Construct | Location | Impact |
|---|---|---|
| `env -C <dir>` | `hive-launch:258`, inside **every issued worker wrapper** | GNU coreutils ≥ 8.28 only; BSD/macOS `env` has no `-C`, so every wrapper fails at first use |
| `sha256sum` | `hive-metrics-drill.sh:243, 695` | GNU-only; on BSD the drill aborts at L243 before any check runs |
| `stat -c` | `precheck.sh:141,143`; `restore_rehearsal.sh:170` | GNU-only; precheck emits a false FAIL on `.ssh` mode. `hive-bringup-drill.sh:88-90` has the correct `stat -c ... \|\| stat -f ...` form |
| `mktemp -t <template>` | `deploy_checks.sh:70,131`; `credential_scope_scan.sh:77` | GNU: template. **BSD: prefix** — the file does not end in `.yml`, and `deploy_checks.sh:175` feeds it to `compose -f` |
| `ss` | `precheck.sh:59,66` | iproute2/Linux only; absent → false PASS (A7) |

Clean across the repo, checked and confirmed absent: `sed -i`, `readlink -f`, `grep -P`,
`sort -V`, `date -d`/`date -v`, `timeout`, `declare -A`, `mapfile`, `${var^^}`.

**C6 — quoting.** No unquoted `$var` in a `test`/`[` operand or a path was found in any file.
Every deliberate word-split carries a shellcheck waiver. This class is uniformly clean.

**C7 — `local x=$(cmd)` masking exit status.** Zero instances repo-wide; `hive-common.sh:194,
472` and `hive-tooling-drill.sh:131` correctly split declaration from assignment.

**C8 — bare `(( ))` under `set -e`.** Zero instances repo-wide; every arithmetic site uses
`$(( ))` inside an assignment.

## What the audit found done right

Worth recording, because these are the patterns the fixes below copy rather than invent:

- `deploy_checks.sh:93-124` `role_urls` — refuses rather than guessing, and names the
  stale-image cause.
- `deploy_checks.sh:142-153` — `exit` moved out of a command substitution, with the reason
  written down.
- `hive-bringup-drill.sh:88-105` `file_mode`/`check_mode` — the correct GNU/BSD `stat` form,
  distinguishing "unreadable" from "wrong mode".
- `hive-metrics-drill.sh:377-386` `col()` — returns a `«missing»` sentinel so "no row" and
  "empty cell" stay distinguishable. The best assertion helper in the repo.
- `hive-metrics:106-110` and `hive-score:136-143` — swallow the read, then **immediately
  validate** with `jq -e` and die naming both hypotheses. This is the correct handling of the
  pattern A4/A5 get wrong, already present in the same codebase.
- `hive-common.sh:111-126` `resolve_compose` — probes by running, not by presence.
- `git_bundle.sh:41-44` — verifies the artifact it just wrote.
- `roles_rollback.sh:37-43` — `while read` over a here-doc rather than `for db in $DBS`.
- `hive-init-secrets` — zero swallowed errors.
