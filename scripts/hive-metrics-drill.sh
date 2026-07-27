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
  elif printf '%s' "$out" | grep -qF -- "$needle"; then ok "$d"
  else bad "$d (refused, but message missing '$needle'); got: $out"; fi
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
EMPTY="$SANDBOX/empty"          # stands in for OMEGA_DIR: no compose file, so any
mkdir -p "$EMPTY"               # accidental `hive ...` call fails instead of running
PROJECT=drill
ORDERS="$WS/projects/$PROJECT/orders"
METRICS="$WS/projects/$PROJECT/metrics"
mkdir -p "$ORDERS"

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
ev(2200, "gateway", "gateway", "gateway.rejected", "beta",
   {"refused_event_type": "task.accepted", "refused_task_id": "beta",
    "refused_payload": {}, "code": "ILLEGAL_TRANSITION", "reason": "already owned",
    "original_actor_role": "worker", "original_actor_id": "w-beta1",
    "coalesced_count": 1})
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

# probe — a question report against a task id that has no task.created
ev(1600, "worker", "w-beta1", "task.reported", "probe",
   {"kind": "question", "ref": "projects/drill/questions/p.md@abc1234"})

json.dump(rows, open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
FIXTURE_SUM="$(sha256sum "$FIXTURE" | cut -d' ' -f1)"

# --- 2. the order files -------------------------------------------------------
# alpha: a complete Predictions section. beta: partial (effort only — no question
# count, no review outcome). gamma: no Predictions section at all. delta: complete,
# but the task is not closed, so scoring it must be refused.
cat > "$ORDERS/2026-07-20-alpha.md" <<'EOF'
# Order: alpha

## Scope
Do the alpha thing.

## Predictions

- Expected effort: 2 worker-hours (base rate). Expected questions: 0. Expected review outcome: clean.
- Named risk: none worth naming.
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

export HIVE_SPINE_JSON="$FIXTURE"
export OPS_WS="$WS"
export OMEGA_DIR="$EMPTY"
export HIVE_PROJECT="$PROJECT"

M="$SCRIPT_DIR/hive-metrics"
S="$SCRIPT_DIR/hive-score"

# --- 3. hive-metrics ----------------------------------------------------------
echo
echo "metrics drill: hive-metrics"

"$M" "$PROJECT" --run metrics-drill >/dev/null
check "tasks.md written"  "[ -s '$METRICS/tasks.md' ]"
check "tasks.csv written" "[ -s '$METRICS/tasks.csv' ]"

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
check "alpha accept->result 3600s"      "[ \"\$(col alpha accepted_to_result_s)\" = 3600 ]"
check "alpha result->review 180s"       "[ \"\$(col alpha result_to_review_s)\" = 180 ]"
check "alpha result->close 240s"        "[ \"\$(col alpha result_to_close_s)\" = 240 ]"
check "alpha 0 questions"               "[ \"\$(col alpha questions)\" = 0 ]"
check "alpha shape=worked"              "[ \"\$(col alpha shape)\" = worked ]"

check "beta blocked 600s"               "[ \"\$(col beta blocked_s)\" = 600 ]"
check "beta accept->result 3600s"       "[ \"\$(col beta accepted_to_result_s)\" = 3600 ]"
check "beta net of blocked 3000s"       "[ \"\$(col beta accepted_to_result_net_s)\" = 3000 ]"
check "beta 1 question"                 "[ \"\$(col beta questions)\" = 1 ]"
check "beta 1 rejection"                "[ \"\$(col beta rejections)\" = 1 ]"
check "beta 1 reassignment"             "[ \"\$(col beta reassignments)\" = 1 ]"

check "gamma shape=retired"             "[ \"\$(col gamma shape)\" = retired ]"
check "gamma has no accept duration"    "[ -z \"\$(col gamma accepted_to_result_s)\" ]"
check "unattributed probe surfaced"     "grep -q 'probe' '$MD'"

# Deterministic regeneration: same spine in, byte-identical artifact out.
cp "$MD" "$SANDBOX/first.md"; cp "$CSV" "$SANDBOX/first.csv"
"$M" "$PROJECT" --run metrics-drill >/dev/null
check "tasks.md regeneration is byte-identical"  "cmp -s '$SANDBOX/first.md' '$MD'"
check "tasks.csv regeneration is byte-identical" "cmp -s '$SANDBOX/first.csv' '$CSV'"

# --upto <head seq> must reproduce the same artifact (the pin the artifact records).
HEAD_SEQ=$(jq -r 'map(.seq) | max' "$FIXTURE")
check "artifact records its head seq" "grep -q 'seq $HEAD_SEQ' '$MD'"
"$M" "$PROJECT" --run metrics-drill --upto "$HEAD_SEQ" >/dev/null
check "--upto head reproduces the artifact" "cmp -s '$SANDBOX/first.md' '$MD'"

# Truncating before beta's close must drop beta from the closed set — the property
# that makes a historical regeneration meaningful.
BETA_CLOSE_SEQ=$(jq -r '[.[] | select(.task_id=="beta" and .event_type=="task.status_override") | .seq] | first' "$FIXTURE")
"$M" "$PROJECT" --run metrics-drill --upto "$((BETA_CLOSE_SEQ - 1))" >/dev/null
check "--upto before beta close drops beta" "[ \"\$(col beta shape)\" = '«missing»' ]"
check "--upto before beta close keeps alpha" "[ \"\$(col alpha shape)\" = worked ]"
"$M" "$PROJECT" --run metrics-drill >/dev/null   # restore the full artifact

# Without the fixture the tool goes to the real read path — which, with OMEGA_DIR
# pointed at an empty dir, cannot reach any stack. The refusal must name the run,
# not blow up. (env -u drops the fixture for this one call only.)
expect_fail_msg "unreachable/empty run refused" "no events for run" \
  env -u HIVE_SPINE_JSON "$M" "$PROJECT" --run nope

# --- 4. hive-score ------------------------------------------------------------
echo
echo "metrics drill: hive-score"

CAL="$METRICS/calibration.md"

"$S" alpha --run metrics-drill >/dev/null
check "calibration.md written"        "[ -s '$CAL' ]"
check "alpha entry recorded"          "grep -q 'alpha' '$CAL'"
check "alpha predicted effort quoted" "grep -q '2 worker-hours' '$CAL'"
check "alpha actual effort scored"    "grep -q '1.0h' '$CAL'"
check "alpha question prediction hit" "grep -qi 'hit' '$CAL'"

expect_fail_msg "re-scoring refused"  "already scored" "$S" alpha --run metrics-drill
"$S" alpha --run metrics-drill --again >/dev/null
check "--again re-scores"             "[ \"\$(grep -c '^### alpha' '$CAL')\" = 2 ]"

"$S" beta --run metrics-drill >/dev/null
check "beta recorded as partial"      "grep -q 'partial' '$CAL'"
check "beta effort prediction quoted" "grep -q '4 worker-hours' '$CAL'"
check "beta absent fields marked"     "grep -q 'not predicted' '$CAL'"

"$S" gamma --run metrics-drill >/dev/null
check "gamma recorded as unpredicted" "grep -q 'unpredicted' '$CAL'"

expect_fail_msg "scoring an open task refused" "not closed" "$S" delta --run metrics-drill
expect_fail_msg "scoring an unknown task refused" "not on the board" "$S" nosuch --run metrics-drill

# --- 5. the stop-line: read-only, no emits ------------------------------------
echo
echo "metrics drill: stop-lines"

check "fixture untouched" "[ \"\$(sha256sum '$FIXTURE' | cut -d' ' -f1)\" = '$FIXTURE_SUM' ]"
check "hive-metrics contains no emit" "! grep -Eq '(^|[^-[:alnum:]_])emit[[:space:]]' '$M'"
check "hive-score contains no emit"   "! grep -Eq '(^|[^-[:alnum:]_])emit[[:space:]]' '$S'"
check "neither tool commits to git"   "! grep -Eq 'git .*(commit|push)' '$M' '$S'"
