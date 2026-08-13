#!/usr/bin/env bash
# hive-metrics-drill.sh — exercises hive-metrics / hive-score against a frozen
# spine fixture and a throwaway workspace.
#
# Hermetic by construction: the fixture is a JSON file in the shape
# `omegahive report <run> --json` returns, fed in via HIVE_SPINE_JSON, so the
# drill needs neither the stack nor the database — and, crucially, **issues no
# events at all**. Both tools are read-only over the spine (order stop-line);
# the drill asserts that structurally (no emit in either script, and OMEGA_DIR
# pointed at an empty dir so any stray CLI call would fail loudly) as well as
# behaviourally (the fixture is byte-identical afterwards).
#
# Usage: scripts/hive-metrics-drill.sh   (run from anywhere; needs jq)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/hive-metrics-drill.XXXXXX")"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [cond: $2]"; fi; }
# expect_fail_msg <desc> <needle> <cmd...>: passes iff the command fails AND its
# combined output contains <needle> — the refusal's reason is asserted, not just
# its exit code.
expect_fail_msg(){
  local d="$1" needle="$2"; shift 2; local out
  if out=$("$@" 2>&1); then bad "$d (expected refusal, got success)"
  # `grep -q` would do here: printf's whole payload is a single write() of a
  # value already fully in memory. But it is the SAME shape as the SIGPIPE trap
  # below (an early -q match closing the reader before the writer is done), so
  # it gets the same fix on the drill-hygiene sweep (instrument-teeth item 7) —
  # the two are indistinguishable once the captured output is long enough not to
  # fit one pipe write.
  elif printf '%s' "$out" | grep -F -- "$needle" >/dev/null; then ok "$d"
  else bad "$d (refused, but message missing '$needle'); got: $out"; fi
}

# log_has <repo> <needle> [pathspec ...] — is <needle> a commit subject in <repo>?
#
# Two traps, both of which produced a green-looking false FAIL here:
#   * `git log | grep -q` — `-q` exits at the FIRST match, and the SIGPIPE that
#     sends `git log` fails the whole pipeline under `set -o pipefail`. It bites
#     precisely when the needle matches an EARLY line, i.e. the passing case.
#     Plain `grep` reads to EOF, so git is never signalled.
#   * a bare `git log` in a bare repo whose HEAD is an unborn `master` while every
#     ref lives on `main` — `--all` reads refs, not HEAD.
log_has() {
  local repo="$1" needle="$2"; shift 2
  git -C "$repo" log --all --format=%s -- "$@" | grep -F -- "$needle" >/dev/null
}

cleanup() {
  rm -rf "$SANDBOX"
  echo
  echo "metrics drill: PASS=$PASS  FAIL=$FAIL  (no events issued; no run touched)"
  [ "$FAIL" -eq 0 ] || echo "metrics drill: FAILURES PRESENT"
}
trap cleanup EXIT

echo "metrics drill: sandbox=$SANDBOX"

WS="$SANDBOX/ws"
HUB="$SANDBOX/hub.git"          # push target, so the tools' commit path runs for real
EMPTY="$SANDBOX/empty"          # stands in for OMEGA_DIR: no compose file, so any
mkdir -p "$EMPTY"               # accidental `hive ...` call fails instead of running
PROJECT=drill
ORDERS="$WS/projects/$PROJECT/orders"
METRICS="$WS/projects/$PROJECT/metrics"
mkdir -p "$ORDERS"

# The workspace is a real clone with a real upstream, because both tools now
# COMMIT and PUSH what they regenerate: a regenerated artifact that stops at the
# working tree is invisible to the improver seat, the only seat that reads it.
# Driving them against a bare directory would exercise a path that no longer ships.
git init --quiet --bare "$HUB"
git init --quiet "$WS"
# Local branch `main`, matching the upstream it will track — set via symbolic-ref
# rather than `git init -b` so this does not depend on the host's git version or
# its init.defaultBranch. A local/upstream name mismatch makes a bare `git push`
# refuse, which would test the tools' error path instead of their happy one.
git -C "$WS" symbolic-ref HEAD refs/heads/main
git -C "$WS" config user.email drill@example.invalid
git -C "$WS" config user.name  drill
git -C "$WS" remote add origin "$HUB"

# The scratch project's committed identity. Both tools resolve the run through
# load_project_conf, so the project name is NOT assumed to be the run id — this
# conf deliberately names a run that differs from the project name, which is what
# makes the drill prove the conf is consulted rather than guessed at.
cat > "$WS/projects/$PROJECT/project.conf" <<'EOF'
RUN_ID=metrics-drill
CODE_REPO=git@example.invalid:drill/drill.git
EOF

# --- 1. the frozen spine fixture ----------------------------------------------
# One scratch run covering every shape the tools must survive:
#   alpha  — a clean cycle: create → launch → accept → result → review → close
#   beta   — the messy cycle: a question, a block/unblock, a rejection, a reassign
#   gamma  — closed with no work cycle at all (debris retired by decision)
#   delta  — still in flight; must not appear in the closed table
#   probe  — a report against a task id that was never created (unattributed)
# Timestamps are epoch seconds, which is what the spine's logical_ts is under
# server time — the fixture therefore exercises the same arithmetic as real data.
FIXTURE="$SANDBOX/spine.json"
python3 - "$FIXTURE" <<'PY'
import json, sys

B = 1784600000
rows, seq = [], 0

def ev(dt, role, actor, etype, task=None, payload=None):
    global seq
    seq += 1
    rows.append({
        "seq": seq,
        "event_id": f"00000000-0000-0000-0000-{seq:012d}",
        "run_id": "metrics-drill",
        "logical_ts": B + dt,
        "actor": {"role": role, "id": actor},
        "event_type": etype,
        "task_id": task,
        "payload": payload or {},
        "causation_id": None,
        "correlation_id": None,
    })

def created(dt, task):
    ev(dt, "human", "operator", "task.created",
       task, {"title": task, "task_type": "code", "acceptance": None,
              "required_artifacts": [], "ready_when": None})

def closed(dt, task, reason="drill"):
    ev(dt, "human", "operator", "task.status_override", task,
       {"status": "done", "reason": reason, "decision_ref": None})

def result(dt, task, worker):
    ev(dt, "worker", worker, "task.result_posted", task,
       {"artifact_refs": [{"ref": f"projects/drill/reports/{task}.md@abc1234",
                           "quality": "ok"}], "cost": None})

# alpha — launch 60s, pickup 60s, accept->result 3600s, result->review 180s,
#         review->close 60s (result->close 240s), no questions/blocks/rejections
created(0, "alpha")
ev(10,  "human", "operator", "worker.registered", None, {"worker_id": "w-alpha"})
ev(60,  "coordinator", "operator", "task.assigned", "alpha", {"worker": "w-alpha"})
ev(120, "worker", "w-alpha", "task.accepted", "alpha", {})
result(3720, "alpha", "w-alpha")
ev(3900, "instrument", "operator", "review.passed", "alpha",
   {"ref_result": "projects/drill/reports/alpha.md@abc1234"})
closed(3960, "alpha")

# beta — launch 100s, pickup 100s, accept->result 3600s of which 600s blocked
#        (net 3000s), 1 question, 1 rejection, 1 reassignment
created(1000, "beta")
ev(1005, "human", "operator", "worker.registered", None, {"worker_id": "w-beta1"})
ev(1006, "human", "operator", "worker.registered", None, {"worker_id": "w-beta2"})
ev(1100, "coordinator", "operator", "task.assigned", "beta", {"worker": "w-beta1"})
ev(1200, "worker", "w-beta1", "task.accepted", "beta", {})
ev(1500, "worker", "w-beta1", "task.reported", "beta",
   {"kind": "question", "ref": "projects/drill/questions/q.md@abc1234"})
ev(1510, "worker", "w-beta1", "task.blocked", "beta",
   {"reason": "needs a decision", "needs": "decision",
    "ref_report": "projects/drill/questions/q.md@abc1234"})
ev(2110, "worker", "w-beta1", "task.unblocked", "beta", {})
# coalesced_count 3: the gateway folds a burst of identical refusals into ONE
# event, so counting rows would report 1 where the worker really hit the gate 3x.
ev(2200, "gateway", "gateway", "gateway.rejected", "beta",
   {"refused_event_type": "task.accepted", "refused_task_id": "beta",
    "refused_payload": {}, "code": "ILLEGAL_TRANSITION", "reason": "already owned",
    "original_actor_role": "worker", "original_actor_id": "w-beta1",
    "coalesced_count": 3})
ev(2300, "coordinator", "operator", "task.reassigned", "beta",
   {"from": "w-beta1", "to": "w-beta2", "reason": "worker died"})
result(4800, "beta", "w-beta2")
ev(5000, "instrument", "operator", "review.passed", "beta",
   {"ref_result": "projects/drill/reports/beta.md@abc1234"})
closed(5100, "beta")

# gamma — retired debris: created, then closed with no accept and no result
created(2000, "gamma")
ev(6000, "instrument", "operator", "review.passed", "gamma",
   {"ref_result": "projects/drill/decisions.md@abc1234"})
closed(6010, "gamma", "retired per decision")

# delta — still in flight at the head of the fixture
created(7000, "delta")
ev(7005, "human", "operator", "worker.registered", None, {"worker_id": "w-delta"})
ev(7100, "coordinator", "operator", "task.assigned", "delta", {"worker": "w-delta"})
ev(7200, "worker", "w-delta", "task.accepted", "delta", {})

# epsilon — closed by task.failed, NOT by status_override: the terminal event a
#           worker emits when it cannot finish. Also the effort "over" case:
#           predicted 1 worker-hour, actual 3h. And its one block opens AFTER the
#           last result (a post-result question closed out by hand), which must
#           charge to the close rather than backwards to the result.
created(8000, "epsilon")
ev(8005, "human", "operator", "worker.registered", None, {"worker_id": "w-eps"})
ev(8100, "coordinator", "operator", "task.assigned", "epsilon", {"worker": "w-eps"})
ev(8200, "worker", "w-eps", "task.accepted", "epsilon", {})
result(19000, "epsilon", "w-eps")          # accept->result 10800s = 3h
ev(19500, "worker", "w-eps", "task.blocked", "epsilon",
   {"reason": "post-result question", "needs": "decision", "ref_report": None})
ev(20000, "worker", "w-eps", "task.failed", "epsilon", {"reason": "cannot finish"})

# zeta — closed, then reopened out of review, then closed again. Only the final
#        close counts, and a reopen must not leave it looking terminal in between.
created(9000, "zeta")
ev(9005, "human", "operator", "worker.registered", None, {"worker_id": "w-zeta"})
ev(9100, "coordinator", "operator", "task.assigned", "zeta", {"worker": "w-zeta"})
ev(9200, "worker", "w-zeta", "task.accepted", "zeta", {})
result(9800, "zeta", "w-zeta")
ev(9900, "instrument", "operator", "review.passed", "zeta",
   {"ref_result": "projects/drill/reports/zeta.md@abc1234"})
closed(9950, "zeta")
ev(10000, "human", "operator", "task.status_override", "zeta",
   {"status": "reopened", "reason": "found a defect", "decision_ref": None})
#        3599s is deliberate: it is the window where rounding the minutes
#        remainder independently of the hour floor prints "60m" / "1h60m".
result(12799, "zeta", "w-zeta")            # second cycle: accept->result 3599s
ev(12900, "instrument", "operator", "review.passed", "zeta",
   {"ref_result": "projects/drill/reports/zeta.md@abc1234"})
closed(13000, "zeta")

# probe — a question report against a task id that has no task.created. The id
# also carries a pipe, which must not open a phantom markdown column.
ev(1600, "worker", "w-beta1", "task.reported", "probe|x",
   {"kind": "question", "ref": "projects/drill/questions/p.md@abc1234"})

# eta — closed debris (mirrors gamma's shape); its order carries a `##
# Predictions` heading in the D1 dialect (mislabeled bullets, 0 of 3 fields
# parse). Coverage must read "section present, 0 of 3 fields parsed" — a
# DIFFERENT string from gamma's "no section" — even though both currently
# score unpredicted (instrument-teeth item 3 / DoD (d)).
created(14000, "eta")
ev(14010, "instrument", "operator", "review.passed", "eta",
   {"ref_result": "projects/drill/decisions.md@abc1234"})
closed(14020, "eta", "retired per decision")

# iota — the result-revision scenario proper (decisions.md 2026-08-01): ONE
# accept, TWO result_posted on the SAME cycle (no reopen in between, unlike
# zeta) — a worker posts a result, gets a review question during the wait, then
# re-fires result_posted with a corrected ref. accept->first result = 3600s
# (1.0h) and never moves; accept->last result = 10800s (3.0h) once the revision
# and its 3600s of review-wait land. Predicted effort is 1 worker-hour: under the
# OLD single-span (last-based, net-of-all-blocked) scheme this would have scored
# (10800-3600)/3600 = 2.0h -> "over" the moment the revision fired — exactly the
# silent-inflation bug decisions.md 2026-08-01 names. Under the new scheme the
# production span is untouched by the revision and scores "hit".
created(30000, "iota")
ev(30005, "human", "operator", "worker.registered", None, {"worker_id": "w-iota"})
ev(30100, "coordinator", "operator", "task.assigned", "iota", {"worker": "w-iota"})
ev(30200, "worker", "w-iota", "task.accepted", "iota", {})
result(33800, "iota", "w-iota")                         # first result: accept->first = 3600s
ev(34000, "worker", "w-iota", "task.blocked", "iota",
   {"reason": "review question on the first result", "needs": "decision",
    "ref_report": "projects/drill/questions/iota.md@abc1234"})
ev(37600, "worker", "w-iota", "task.unblocked", "iota", {})   # 3600s blocked, AFTER the first result
result(41000, "iota", "w-iota")                         # revision: accept->last = 10800s
ev(41100, "instrument", "operator", "review.passed", "iota",
   {"ref_result": "projects/drill/reports/iota.md@abc1234"})
closed(41200, "iota")

json.dump(rows, open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
FIXTURE_SUM="$(sha256sum "$FIXTURE" | cut -d' ' -f1)"

# --- 2. the order files -------------------------------------------------------
# Between them these exercise every verdict branch, so a broken comparison cannot
# pass: alpha effort `hit` / questions `hit`; epsilon effort `over`; zeta effort
# `under` and questions `under` (via a predicted RANGE, so the range parser is
# exercised too). beta is partial (effort only), gamma has no Predictions section,
# delta is complete but not closed, so scoring it must be refused.
cat > "$ORDERS/2026-07-20-alpha.md" <<'EOF'
# Order: alpha

## Scope
Do the alpha thing.

## Predictions

- Expected effort: 1 worker-hour (base rate). Expected questions: 0. Expected review outcome: clean.
- Named risk: none worth naming.
EOF

cat > "$ORDERS/2026-07-20-epsilon.md" <<'EOF'
# Order: epsilon

## Scope
Do the epsilon thing.

## Predictions

- Expected effort: 1 worker-hour. Expected questions: 0. Expected review outcome: clean.
EOF

cat > "$ORDERS/2026-07-20-zeta.md" <<'EOF'
# Order: zeta

## Scope
Do the zeta thing, twice.

## Predictions

- Expected effort: 8 worker-hours. Expected questions: 1-2. Expected review outcome: minor rework.
EOF

cat > "$ORDERS/2026-07-20-beta.md" <<'EOF'
# Order: beta

## Scope
Do the beta thing.

## Predictions

- Expected effort: 4 worker-hours.
EOF

cat > "$ORDERS/2026-07-20-gamma.md" <<'EOF'
# Order: gamma

## Scope
Debris. Retired by decision, never worked.
EOF

cat > "$ORDERS/2026-07-20-delta.md" <<'EOF'
# Order: delta

## Scope
Still in flight.

## Predictions

- Expected effort: 1 worker-hour. Expected questions: 0. Expected review outcome: clean.
EOF

# eta reproduces retro 2026-07-29 D1's dialect verbatim: a `## Predictions`
# heading present, fully "populated" by its own author's lights, and unparsed
# by hive-score because the bullet labels are re-worded rather than copied.
cat > "$ORDERS/2026-07-20-eta.md" <<'EOF'
# Order: eta

## Scope
Debris, D1-dialect. Retired by decision, never worked.

## Predictions

- **Effort:** 2 hours.
- **Questions:** 0.
- **Review outcome:** clean.
EOF

# iota predicts 1 worker-hour — a HIT against the new accept->first-result-net
# figure (3600s) and an OVER against the old accept->last-result-net figure the
# revision would have produced (7200s once its 3600s of post-first-result
# blocked time is netted out of the 10800s gross) — the exact regression this
# order exists to prevent.
cat > "$ORDERS/2026-07-20-iota.md" <<'EOF'
# Order: iota

## Scope
Do the iota thing, then revise the result once under review.

## Predictions

- Expected effort: 1 worker-hour. Expected questions: 0. Expected review outcome: minor rework.
EOF

# Seed the workspace and give `main` an upstream, so the tools' plain `git push`
# has somewhere to go.
git -C "$WS" add -A
git -C "$WS" commit --quiet -m "drill: seed workspace"
git -C "$WS" push --quiet -u origin HEAD:main
git -C "$WS" branch --quiet --set-upstream-to=origin/main 2>/dev/null || true

export HIVE_SPINE_JSON="$FIXTURE"
export OPS_WS="$WS"
export OMEGA_DIR="$EMPTY"

M="$SCRIPT_DIR/hive-metrics"
S="$SCRIPT_DIR/hive-score"

# --- 3. hive-metrics ----------------------------------------------------------
echo
echo "metrics drill: hive-metrics"

"$M" "$PROJECT" >/dev/null
check "tasks.md written"  "[ -s '$METRICS/tasks.md' ]"
check "tasks.csv written" "[ -s '$METRICS/tasks.csv' ]"
# Regenerating and recording are one act — the artifacts are committed and pushed,
# not left for the operator to remember.
check "the artifacts were committed"        "log_has '$WS' 'metrics: refresh $PROJECT on run metrics-drill' 'projects/$PROJECT/metrics'"
check "the artifacts reached the upstream"  "log_has '$HUB' 'metrics: refresh $PROJECT on run metrics-drill'"
check "the workspace was left clean"        "[ -z \"\$(git -C '$WS' status --porcelain -- 'projects/$PROJECT/metrics')\" ]"
# Deterministic regeneration means an unchanged spine prefix has nothing to
# commit — a success, not a failure, and it must not manufacture an empty commit.
NCOMMITS_BEFORE=$(git -C "$WS" rev-list --count HEAD)
# shellcheck disable=SC2034  # consumed by `check`, which evals its condition string
RERUN_OUT="$("$M" "$PROJECT")"
check "a no-change re-run says so"          "printf '%s' \"\$RERUN_OUT\" | grep -F 'no change to commit' >/dev/null"
check "a no-change re-run adds no commit"   "[ \"\$(git -C '$WS' rev-list --count HEAD)\" = '$NCOMMITS_BEFORE' ]"
# --no-commit is the escape hatch: written, deliberately not recorded. Commit a
# deliberately stale artifact first, so a regeneration genuinely has a diff to
# withhold — otherwise determinism would make "nothing to commit" indistinguishable
# from "chose not to commit".
printf 'stale\n' > "$METRICS/tasks.md"
git -C "$WS" commit --quiet -m "drill: stale artifact" -- "projects/$PROJECT/metrics"
"$M" "$PROJECT" --no-commit >/dev/null
check "--no-commit leaves the regenerated artifact uncommitted" \
  "[ -n \"\$(git -C '$WS' status --porcelain -- 'projects/$PROJECT/metrics')\" ]"
"$M" "$PROJECT" >/dev/null   # re-record, so the rest of the drill starts clean
check "the follow-up run recorded it after all" \
  "[ -z \"\$(git -C '$WS' status --porcelain -- 'projects/$PROJECT/metrics')\" ]"

MD="$METRICS/tasks.md"; CSV="$METRICS/tasks.csv"
col() { # col <task> <header>  -> that task's value from the CSV ("«missing»" if no row)
  # The CSV is @csv-quoted; strip one layer of quoting per field. No field in this
  # schema contains a comma or a quote, so a plain split is exact here.
  jq -rn --rawfile c "$CSV" --arg t "$1" --arg h "$2" '
    def unq: map(sub("^\"";"") | sub("\"$";""));
    ($c | rtrimstr("\n") | split("\n") | map(split(",") | unq)) as $rows
    | ($rows[0] | index($h)) as $i
    | ($rows[1:] | map(select(.[0] == $t)) | first) as $row
    | if $row == null then "«missing»" else $row[$i] end'
}

check "generated marker present"      "grep -qi 'GENERATED' '$MD'"
# Both are committed text files; a missing final newline is permanent diff noise.
check "tasks.md ends with a newline"  "[ -z \"\$(tail -c 1 '$MD')\" ]"
check "tasks.csv ends with a newline" "[ -z \"\$(tail -c 1 '$CSV')\" ]"
check "alpha row present"             "grep -q 'alpha' '$MD'"
check "gamma listed as retired"       "grep -q 'gamma' '$MD'"
check "delta (in flight) not measured" "[ \"\$(col delta shape)\" = '«missing»' ]"
check "delta listed as open, not measured" "grep -q 'Open at this window' '$MD' && grep -q 'delta' '$MD'"

check "alpha launch latency 60s"        "[ \"\$(col alpha created_to_assigned_s)\" = 60 ]"
check "alpha pickup 60s"                "[ \"\$(col alpha assigned_to_accepted_s)\" = 60 ]"
# alpha has one result_posted, so first == last on both spans.
check "alpha accept->first result 3600s" "[ \"\$(col alpha accepted_to_first_result_s)\" = 3600 ]"
check "alpha accept->last result 3600s" "[ \"\$(col alpha accepted_to_last_result_s)\" = 3600 ]"
check "alpha result->review 180s"       "[ \"\$(col alpha result_to_review_s)\" = 180 ]"
check "alpha result->close 240s"        "[ \"\$(col alpha result_to_close_s)\" = 240 ]"
check "alpha 0 questions"               "[ \"\$(col alpha questions)\" = 0 ]"
check "alpha shape=worked"              "[ \"\$(col alpha shape)\" = worked ]"

check "beta blocked 600s"               "[ \"\$(col beta blocked_s)\" = 600 ]"
check "beta accept->last result 3600s"  "[ \"\$(col beta accepted_to_last_result_s)\" = 3600 ]"
check "beta accept->first result 3600s" "[ \"\$(col beta accepted_to_first_result_s)\" = 3600 ]"
check "beta blocked before first result 600s" "[ \"\$(col beta blocked_before_first_result_s)\" = 600 ]"
check "beta net of blocked 3000s"       "[ \"\$(col beta accepted_to_first_result_net_s)\" = 3000 ]"
check "beta 1 question"                 "[ \"\$(col beta questions)\" = 1 ]"
# The gateway coalesces a burst into ONE event carrying coalesced_count; counting
# rows would say 1 where the worker really hit the gate 3 times.
check "beta 3 rejections (coalesced)"   "[ \"\$(col beta rejections)\" = 3 ]"
check "beta 3 worker rejections"        "[ \"\$(col beta rejections_worker)\" = 3 ]"
check "beta 1 reassignment"             "[ \"\$(col beta reassignments)\" = 1 ]"

check "gamma shape=retired"             "[ \"\$(col gamma shape)\" = retired ]"
check "gamma has no accept duration"    "[ -z \"\$(col gamma accepted_to_first_result_s)\" ]"

# epsilon: closed by task.failed, not by status_override. Failed work is the
# outcome the improver most needs counted, so it must be measured, not dropped.
check "epsilon measured (closed by task.failed)" "[ \"\$(col epsilon shape)\" = worked ]"
check "epsilon final_status=failed"     "[ \"\$(col epsilon final_status)\" = failed ]"
check "epsilon not listed as open"      "! grep -q '\`epsilon\`' '$MD'"
# Its one block opens AFTER the result and never closes: charging it back to the
# result would be negative, so blocked_s (the full-task figure) charges it forward
# to the terminal event — but blocked_before_first_result_s excludes it entirely,
# since a block that opens after the (only, here also first) result never counts
# against the production span at all.
check "epsilon post-result block = 500s (full-task blocked_s)" "[ \"\$(col epsilon blocked_s)\" = 500 ]"
check "epsilon blocked before first result = 0 (post-result block excluded)" \
  "[ \"\$(col epsilon blocked_before_first_result_s)\" = 0 ]"
check "epsilon first-result net == gross (post-result block never subtracted)" \
  "[ \"\$(col epsilon accepted_to_first_result_net_s)\" = \"\$(col epsilon accepted_to_first_result_s)\" ]"

# zeta: closed, reopened out of review, closed again. Only the final close counts,
# and its single task.accepted (never re-fired after reopen) against TWO
# result_posted events makes it the fixture's other first/last split case, distinct
# from a same-cycle revision (iota, below): here accept->first is the pre-reopen
# result, accept->last is the post-reopen one.
check "zeta measured once"              "[ \"\$(grep -c '^| zeta |' '$MD')\" = 3 ]"
check "zeta closed at the second close" "[ \"\$(col zeta closed)\" = 2026-07-21T05:50:00Z ]"
check "zeta final_status=done"          "[ \"\$(col zeta final_status)\" = done ]"
check "zeta accept->first result 600s (pre-reopen result)" \
  "[ \"\$(col zeta accepted_to_first_result_s)\" = 600 ]"
check "zeta accept->last result 3599s (post-reopen result)" \
  "[ \"\$(col zeta accepted_to_last_result_s)\" = 3599 ]"
# zeta's 3599s span sits in the window where an independently-rounded remainder
# renders an impossible clock. No cell may ever read Xh60m or a bare 60m.
check "zeta 3599s renders as 1h0m"      "grep -qF '| 1h0m |' '$MD'"
check "no impossible clock strings"     "! grep -qE '[0-9]+h60m|\\| 60m \\|' '$MD'"

# iota: the same-cycle revision case (decisions.md 2026-08-01). accept->first is
# fixed at the first firing and never sees the post-first-result block; accept->
# last grows to include the revision and the blocked time along the way.
check "iota accept->first result 3600s (unmoved by the revision)" \
  "[ \"\$(col iota accepted_to_first_result_s)\" = 3600 ]"
check "iota accept->last result 10800s (full cycle, incl. revision + wait)" \
  "[ \"\$(col iota accepted_to_last_result_s)\" = 10800 ]"
check "iota blocked before first result = 0 (the block opens after)" \
  "[ \"\$(col iota blocked_before_first_result_s)\" = 0 ]"
check "iota first-result net == first-result gross (3600s, block excluded)" \
  "[ \"\$(col iota accepted_to_first_result_net_s)\" = 3600 ]"
check "iota full-task blocked_s = 3600 (the review-wait block, counted here)" \
  "[ \"\$(col iota blocked_s)\" = 3600 ]"
# The worker-clock table's iota row must show the FIRST-result net (1h0m), and
# the mixed-clock table's iota row the LAST-result gross (3h0m) — extracted by
# slicing the artifact between its own section headings, since a whole-file grep
# cannot tell which table a "1h0m" belongs to.
iota_section() { awk -v h="$1" 'index($0,"## "h)==1{f=1;next} /^## /{f=0} f' "$MD"; }
check "iota worker-clock table shows the FIRST-result net (1h0m)" \
  "iota_section 'Worker clock' | grep '^| iota ' | grep -qF '| 1h0m |'"
check "iota mixed-clock table shows the LAST-result gross (3h0m)" \
  "iota_section 'Mixed clock' | grep '^| iota ' | grep -qF '| 3h0m |'"

check "unattributed probe surfaced"     "grep -q 'probe' '$MD'"
# The probe id contains a pipe; unescaped it would open a phantom column.
check "pipe in a task id is escaped"    "grep -qF 'probe\\\\|x' '$MD'"
check "no unescaped pipe in probe row"  "! grep -qF '| \`probe|x\`' '$MD'"

# Deterministic regeneration: same spine in, byte-identical artifact out.
cp "$MD" "$SANDBOX/first.md"; cp "$CSV" "$SANDBOX/first.csv"
"$M" "$PROJECT" >/dev/null
check "tasks.md regeneration is byte-identical"  "cmp -s '$SANDBOX/first.md' '$MD'"
check "tasks.csv regeneration is byte-identical" "cmp -s '$SANDBOX/first.csv' '$CSV'"

# --upto <head seq> must reproduce the same artifact (the pin the artifact records).
HEAD_SEQ=$(jq -r 'map(.seq) | max' "$FIXTURE")
check "artifact records its head seq" "grep -q 'seq $HEAD_SEQ' '$MD'"
"$M" "$PROJECT" --upto "$HEAD_SEQ" >/dev/null
check "--upto head reproduces the artifact" "cmp -s '$SANDBOX/first.md' '$MD'"

# Truncating before beta's close must drop beta from the closed set — the property
# that makes a historical regeneration meaningful.
BETA_CLOSE_SEQ=$(jq -r '[.[] | select(.task_id=="beta" and .event_type=="task.status_override") | .seq] | first' "$FIXTURE")
UPTO_COMMITS_BEFORE=$(git -C "$WS" rev-list --count HEAD)
"$M" "$PROJECT" --upto "$((BETA_CLOSE_SEQ - 1))" >/dev/null
check "--upto before beta close drops beta" "[ \"\$(col beta shape)\" = '«missing»' ]"
check "--upto before beta close keeps alpha" "[ \"\$(col alpha shape)\" = worked ]"
# A historical rebuild is an inspection, never the record: committing one would
# rewind the committed instrument to an older spine prefix.
check "--upto never commits the rewound artifact" \
  "[ \"\$(git -C '$WS' rev-list --count HEAD)\" = '$UPTO_COMMITS_BEFORE' ]"
# ...and it must not TELL the operator to commit it either — that advice would undo
# the guard by hand. Assert the words, because a `${VAR:+…}${VAR:-…}` pair renders
# the wrong branch silently and no exit code ever notices.
# shellcheck disable=SC2034  # consumed by `check`, which evals its condition string
UPTO_OUT="$("$M" "$PROJECT" --upto "$((BETA_CLOSE_SEQ - 1))")"
check "--upto says why it did not commit"      "printf '%s' \"\$UPTO_OUT\" | grep -F 'an --upto rebuild is an inspection' >/dev/null"
check "--upto does NOT advise committing it"   "! printf '%s' \"\$UPTO_OUT\" | grep -F 'commit it yourself' >/dev/null"
check "--upto names the restore command"       "printf '%s' \"\$UPTO_OUT\" | grep -F 'hive-metrics $PROJECT' >/dev/null"
check "--upto never leaks the seq into prose"  "! printf '%s' \"\$UPTO_OUT\" | grep -E 'record[0-9]' >/dev/null"
"$M" "$PROJECT" >/dev/null   # restore the full artifact

# Without the fixture the tool goes to the real read path — which, with OMEGA_DIR
# pointed at an empty dir, cannot reach any stack. The refusal must name the run,
# not blow up. (env -u drops the fixture for this one call only.)
expect_fail_msg "unreachable/empty run refused" "no events for run" \
  env -u HIVE_SPINE_JSON "$M" "$PROJECT" --run nope

# An --upto below the run's first seq must refuse, not overwrite the good artifact
# with an empty `seq null` skeleton and exit 0.
expect_fail_msg "--upto below the first seq refused" "selects no events" \
  "$M" "$PROJECT" --upto 0
check "refused --upto left the artifact intact" "cmp -s '$SANDBOX/first.md' '$MD'"

# --- 4. hive-score ------------------------------------------------------------
echo
echo "metrics drill: hive-score"

CAL="$METRICS/calibration.md"

# Every assertion below is scoped to ONE task's entry. A whole-file grep is
# useless here: calibration.md opens with a header that itself contains the words
# "hit" and "unpredicted" (the stated verdict rules), and every later entry adds
# more, so an unanchored needle passes even when the verdict logic is inverted.
entry() {  # entry <task> -> that task's last entry block, header line excluded
  awk -v t="### $1 — " '
    index($0, t) == 1 { buf = ""; inb = 1; next }
    /^### / { inb = 0 }
    inb { buf = buf $0 "\n" }
    END { printf "%s", buf }
  ' "$CAL"
}
row() { entry "$1" | grep -F "| $2 |" | head -1; }   # one field's row from an entry
# verdict <task> <field> -> the last cell of that row (the verdict column)
verdict() { row "$1" "$2" | awk -F'|' '{ gsub(/^ +| +$/, "", $(NF-1)); print $(NF-1) }'; }

"$S" alpha >/dev/null
check "calibration.md written"        "[ -s '$CAL' ]"
check "alpha entry recorded"          "[ -n \"\$(entry alpha)\" ]"
check "alpha predicted effort quoted" "entry alpha | grep '1 worker-hour' >/dev/null"
check "alpha actual effort scored"    "entry alpha | grep '1.0h' >/dev/null"
# All three verdict branches are exercised across alpha/epsilon/zeta, so an
# inverted or short-circuited comparison cannot pass this drill.
check "alpha effort verdict = hit"    "[ \"\$(verdict alpha effort)\" = hit ]"
check "alpha questions verdict = hit" "[ \"\$(verdict alpha questions)\" = hit ]"
check "alpha review left unscored"    "verdict alpha 'review outcome' | grep '^unscored' >/dev/null"
check "alpha coverage full"           "entry alpha | grep '^- coverage: full$' >/dev/null"
# Scoring records the score: the entry is committed and pushed as part of the same
# act, because the improver seat reads committed instruments and nothing else.
check "the score was committed"        "log_has '$WS' 'score: alpha on run metrics-drill' 'projects/$PROJECT/metrics'"
check "the score reached the upstream" "log_has '$HUB' 'score: alpha on run metrics-drill'"
check "scoring left the workspace clean" "[ -z \"\$(git -C '$WS' status --porcelain -- 'projects/$PROJECT/metrics')\" ]"
check "the score commit touched only the metrics dir" \
  "[ -z \"\$(git -C '$WS' show --name-only --format= HEAD | grep -v '^projects/$PROJECT/metrics/')\" ]"
# --no-commit is the escape hatch: written, deliberately not recorded. An explicit
# --review that actually changes the verdict, not a bare --again: with the
# one-row rewrite, a no-op rescore on an unchanged day is legitimately
# byte-identical to what is already there and leaves nothing to be uncommitted —
# same determinism hive-metrics already has. This assertion is about --no-commit,
# not about that determinism, so force a real content change.
SCORE_COMMITS_BEFORE=$(git -C "$WS" rev-list --count HEAD)
"$S" alpha --review "minor rework" --again --no-commit >/dev/null
check "--no-commit writes but does not commit" \
  "[ -n \"\$(git -C '$WS' status --porcelain -- 'projects/$PROJECT/metrics')\" ]"
check "--no-commit adds no commit" \
  "[ \"\$(git -C '$WS' rev-list --count HEAD)\" = '$SCORE_COMMITS_BEFORE' ]"
git -C "$WS" checkout --quiet -- "projects/$PROJECT/metrics"

expect_fail_msg "re-scoring refused"  "already scored" "$S" alpha
"$S" alpha --again >/dev/null
# One row per task (item 3): --again REPLACES the entry in place rather than
# appending a second one, and a bare --again with no prior human verdict on
# record (alpha's first score above passed no --review) carries nothing forward
# — it stays unscored, it does not invent a verdict.
check "--again replaces in place (still one row, not two)" \
  "[ \"\$(grep -c '^### alpha — ' '$CAL')\" = 1 ]"
check "bare --again with no prior human verdict stays unscored" \
  "verdict alpha 'review outcome' | grep '^unscored' >/dev/null"

"$S" epsilon >/dev/null
check "epsilon effort verdict = over" "[ \"\$(verdict epsilon effort)\" = over ]"
check "epsilon scorable (task.failed closed it)" "entry epsilon | grep 'final status .failed.' >/dev/null"

"$S" zeta >/dev/null
check "zeta effort verdict = under"      "[ \"\$(verdict zeta effort)\" = under ]"
check "zeta questions verdict = under"   "[ \"\$(verdict zeta questions)\" = under ]"
check "zeta predicted range quoted"      "entry zeta | grep '1-2' >/dev/null"

# iota: the retro 3 Verdict 4 case. Predicted 1 worker-hour; accept->FIRST result
# net is exactly 3600s (1.0h) -> hit. Had hive-score still scored the OLD
# accept->LAST result net (10800s gross minus its 3600s post-first-result blocked
# time = 7200s = 2.0h), this would read "over" the moment the revision fired —
# the silent-inflation bug decisions.md 2026-08-01 exists to kill. This is the one
# assertion in the corpus that fails if hive-score ever regresses to scoring the
# full-cycle span as effort.
"$S" iota >/dev/null
check "iota effort verdict = hit (scored against FIRST result, not last)" \
  "[ \"\$(verdict iota effort)\" = hit ]"
check "iota actual effort quoted as 1.0h (not 2.0h/3.0h)" "entry iota | grep '1.0h' >/dev/null"
check "iota actual effort names the full-cycle span beside it (revised)" \
  "entry iota | grep 'full cycle, incl. revision: 3.0h' >/dev/null"

echo
echo "metrics drill: --effort-uninterpretable (the one narrow, non-numeric effort override)"
expect_fail_msg "--effort-uninterpretable requires --note" "requires --note" \
  "$S" iota --effort-uninterpretable scope-amendment --again
expect_fail_msg "--effort-uninterpretable rejects an undecided cause class" "must be one of" \
  "$S" iota --effort-uninterpretable weather --note "nope" --again
IOTA_Q_BEFORE="$(verdict iota questions)"
"$S" iota --effort-uninterpretable host-incident --note "beastie tmux kill mid-run, 2026-08-13" --again >/dev/null
check "uninterpretable verdict names the cause class"     "[ \"\$(verdict iota effort)\" = 'uninterpretable (host-incident)' ]"
check "uninterpretable retains the measured span as context" "entry iota | grep '1.0h' >/dev/null"
check "uninterpretable cause note recorded verbatim"      "entry iota | grep -F 'beastie tmux kill mid-run' >/dev/null"
check "uninterpretable never inferred — questions verdict unaffected" \
  "[ \"\$(verdict iota questions)\" = '$IOTA_Q_BEFORE' ]"
check "uninterpretable never inferred — review verdict unaffected (still unscored)" \
  "verdict iota 'review outcome' | grep '^unscored' >/dev/null"
check "still one row (uninterpretable rides --again, not a second entry)" \
  "[ \"\$(grep -c '^### iota — ' '$CAL')\" = 1 ]"
"$S" iota --again >/dev/null   # restore a normal verdict so later corpus-wide checks read cleanly
check "a plain --again afterward returns to the derived verdict (not sticky)" \
  "[ \"\$(verdict iota effort)\" = hit ]"

"$S" beta >/dev/null
check "beta recorded as partial"      "entry beta | grep '^- coverage: partial' >/dev/null"
check "beta effort prediction quoted" "entry beta | grep '4 worker-hours' >/dev/null"
check "beta absent fields marked"     "entry beta | grep 'not predicted' >/dev/null"
check "beta absent field unpredicted" "[ \"\$(verdict beta questions)\" = unpredicted ]"

"$S" gamma >/dev/null
check "gamma recorded as unpredicted" "entry gamma | grep '^- coverage: unpredicted' >/dev/null"
check "gamma effort unpredicted"      "[ \"\$(verdict gamma effort)\" = unpredicted ]"
check "gamma coverage: no section, verbatim" "entry gamma | grep '^- coverage: unpredicted (no \`## Predictions\` section)\$' >/dev/null"

# eta: a `## Predictions` heading present, D1's mislabeled dialect, 0 of 3
# fields parse — absent and unparsed must NOT read the same (D1's whole cost
# was that they did). One parser (predictions_classify, hive-common.sh) is
# what makes this and hive-launch's gate agree on the same input — asserted
# end to end (the gate refusing, and both callers reaching the same verdict on
# a live launch) in hive-tooling-drill.sh, which alone can run hive-launch.
"$S" eta >/dev/null
check "eta recorded as unpredicted"   "entry eta | grep '^- coverage: unpredicted' >/dev/null"
check "eta coverage: section present, unparsed, verbatim" \
  "entry eta | grep '^- coverage: unpredicted (section present, 0 of 3 fields parsed)\$' >/dev/null"
check "absent and unparsed are DIFFERENT strings" \
  "[ \"\$(entry gamma | grep '^- coverage:')\" != \"\$(entry eta | grep '^- coverage:')\" ]"
check "eta effort unpredicted"        "[ \"\$(verdict eta effort)\" = unpredicted ]"

echo
echo "metrics drill: --review is validated against the scored vocabulary and lowercased"
# Wave 2 produced Rework/rework/Clean/clean/minor rework into the same column
# (retro 1's instrument note, unenforced since) — rework and minor rework are
# two distinct severities, so an uncontrolled casing is not just cosmetic.
"$S" alpha --review "Rework" --again --note "review-vocab-check" >/dev/null
check "mixed-case --review is stored lowercase" "[ \"\$(verdict alpha 'review outcome')\" = rework ]"
"$S" alpha --review "Minor Rework" --again --note "case-check" >/dev/null
check "'minor rework' (already valid, mixed case) is accepted and lowercased" \
  "[ \"\$(verdict alpha 'review outcome')\" = 'minor rework' ]"
ALPHA_ENTRIES_BEFORE=$(grep -c '^### alpha — ' "$CAL")
expect_fail_msg "an invalid --review value is refused, not recorded" "must be one of" \
  "$S" alpha --review "bogus" --again
expect_fail_msg "the refusal names the three valid values" "clean | minor rework | rework" \
  "$S" alpha --review "bogus" --again
check "the invalid --review appended no new entry" \
  "[ \"\$(grep -c '^### alpha — ' '$CAL')\" = '$ALPHA_ENTRIES_BEFORE' ]"

expect_fail_msg "scoring an open task refused" "not closed" "$S" delta
# A task with no order file cannot name its project, and guessing one is exactly
# what project_from_order refuses to do — so this asks for --project by name.
expect_fail_msg "un-inferable project refused" "pass --project" "$S" nosuch
# Given the project explicitly, the refusal is the board's, not the resolver's.
expect_fail_msg "scoring an unknown task refused" "not on the board" \
  "$S" nosuch --project "$PROJECT"

# find_order refuses on two different things. "found 0" is legitimate (the
# hand-seeded 2026-07-13 ids do not invert to their filenames) and scores as
# unpredicted; an AMBIGUOUS pair is the shared guard firing, and swallowing it
# would append a permanent entry claiming the operator predicted nothing.
printf '# Order: alpha (duplicate)\n' > "$ORDERS/alpha.md"
expect_fail_msg "ambiguous order refused, not scored as unpredicted" \
  "cannot resolve the order" "$S" alpha --again
rm -f "$ORDERS/alpha.md"

# PROJECT is interpolated into a path under $OPS_WS/projects/.
expect_fail_msg "traversal in --project refused" "bare directory name" \
  "$S" alpha --project ../../escaped
expect_fail_msg "unknown --project refused" "no project.conf for project 'drll'" \
  "$S" alpha --project drll
check "no escaped calibration file written" "[ ! -e '$SANDBOX/../escaped' ]"

# Project identity comes from the committed conf, never from the project name:
# the scratch project is `drill` and its run is `metrics-drill`, so a tool that
# guessed the run from the name would have found no events at all.
check "run resolved from project.conf, not the name" "grep -qF '| Run | \`metrics-drill\`' '$MD'"
check "entries cite the conf's run"                  "entry alpha | grep -F 'run \`metrics-drill\`' >/dev/null"
# ...and HIVE_RUN_ID / --run still win over the conf (the documented escape hatch).
expect_fail_msg "--run overrides the conf" "no events for run 'other-run'" \
  env -u HIVE_SPINE_JSON "$M" "$PROJECT" --run other-run
# A project with no committed conf has no well-defined run to measure.
mkdir -p "$WS/projects/confless/orders"
expect_fail_msg "project without a conf refused" "no project.conf for project 'confless'" \
  "$M" confless

# --- 4.5. commit_metrics refuses rather than rebases on a push failure --------
echo
echo "metrics drill: commit_metrics refuses rather than rebases on push failure"
# The pre-decided successor to the inherited push -> pull --rebase -> push retry
# (retro 2026-07-29 ledger item 13, folded early because item 5 raises this
# path's firing rate to every close): on a push failure, refuse — commit
# locally, print the manual step, exit non-zero — rather than rebase a shared
# metrics path unattended. Proven against a REAL divergence (another writer
# pushed to the hub), not just a downed remote: under the old behaviour, a
# `pull --rebase` here would have SUCCEEDED silently, which is exactly the
# unattended-conflict-resolution risk the fix removes.
DIVERGE="$SANDBOX/diverge-clone"
git clone --quiet "$HUB" "$DIVERGE" 2>/dev/null
# The bare HUB's own HEAD symref was never updated off its init-time default (it
# only ever received pushes to `main`, not a checkout), so a plain clone leaves
# no local branch checked out. Without this, the "divergent" commit below would
# land on an unrelated history and its own push would spuriously fail — track
# `main` explicitly.
git -C "$DIVERGE" checkout --quiet -B main origin/main
git -C "$DIVERGE" config user.email drill@example.invalid
git -C "$DIVERGE" config user.name  drill
echo "divergent write" > "$DIVERGE/DIVERGENT.txt"
git -C "$DIVERGE" add -A
git -C "$DIVERGE" commit --quiet -m "drill: a divergent write from another clone"
git -C "$DIVERGE" push --quiet origin HEAD:main

BEFORE_SHA=$(git -C "$WS" rev-parse HEAD)
DIVERGE_RC=0
# shellcheck disable=SC2034  # consumed by `check`, which evals its condition string
DIVERGE_OUT=$("$S" alpha --again --note "diverge-test" 2>&1) || DIVERGE_RC=$?
check "scoring refuses rather than rebasing when the hub has diverged" "[ '$DIVERGE_RC' -ne 0 ]"
check "the refusal names the commit as local-only, not on the hub" \
  "printf '%s' \"\$DIVERGE_OUT\" | grep -F 'committed LOCALLY' >/dev/null"
check "the refusal states it will not rebase a shared metrics path" \
  "printf '%s' \"\$DIVERGE_OUT\" | grep -F 'Refusing to rebase' >/dev/null"
check "the refusal names the manual resolution step" \
  "printf '%s' \"\$DIVERGE_OUT\" | grep -F 'git pull --rebase && git push' >/dev/null"
check "the score entry was committed locally despite the refusal" \
  "log_has '$WS' 'score: alpha on run metrics-drill' 'projects/$PROJECT/metrics'"
check "the local commit sits on the OLD head, unrebased (single parent = pre-diverge HEAD)" \
  "[ \"\$(git -C '$WS' log -1 --format=%P HEAD)\" = '$BEFORE_SHA' ]"
check "the unpushed commit did NOT reach the hub" "! log_has '$HUB' 'diverge-test'"

# Hand-resolution completes it — the refusal is recoverable, not a dead end.
git -C "$WS" pull --rebase --quiet
git -C "$WS" push --quiet
check "manual pull --rebase + push resolves it" "log_has '$HUB' 'score: alpha on run metrics-drill'"
check "the divergent file made it into the workspace too" "[ -f '$WS/DIVERGENT.txt' ]"

# --- 5. the stop-line: read-only, no emits ------------------------------------
echo
echo "metrics drill: stop-lines"

check "fixture untouched" "[ \"\$(sha256sum '$FIXTURE' | cut -d' ' -f1)\" = '$FIXTURE_SUM' ]"
# Strip comments and blanks first: these greps look for *code* that writes, and a
# prose mention of committing in a comment is not a write path.
code() { grep -vE '^[[:space:]]*(#|$)' "$1"; }
check "hive-metrics contains no emit" "! code '$M' | grep -E '(^|[^-[:alnum:]_])emit[[:space:]]' >/dev/null"
check "hive-score contains no emit"   "! code '$S' | grep -E '(^|[^-[:alnum:]_])emit[[:space:]]' >/dev/null"
check "neither tool commits to git"   "! { code '$M'; code '$S'; } | grep -E 'git .*(commit|push)' >/dev/null"
