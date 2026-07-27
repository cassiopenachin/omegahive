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
# The gateway coalesces a burst into ONE event carrying coalesced_count; counting
# rows would say 1 where the worker really hit the gate 3 times.
check "beta 3 rejections (coalesced)"   "[ \"\$(col beta rejections)\" = 3 ]"
check "beta 3 worker rejections"        "[ \"\$(col beta rejections_worker)\" = 3 ]"
check "beta 1 reassignment"             "[ \"\$(col beta reassignments)\" = 1 ]"

check "gamma shape=retired"             "[ \"\$(col gamma shape)\" = retired ]"
check "gamma has no accept duration"    "[ -z \"\$(col gamma accepted_to_result_s)\" ]"

# epsilon: closed by task.failed, not by status_override. Failed work is the
# outcome the improver most needs counted, so it must be measured, not dropped.
check "epsilon measured (closed by task.failed)" "[ \"\$(col epsilon shape)\" = worked ]"
check "epsilon final_status=failed"     "[ \"\$(col epsilon final_status)\" = failed ]"
check "epsilon not listed as open"      "! grep -q '\`epsilon\`' '$MD'"
# Its one block opens AFTER the last result and never closes: charging it back to
# the result would be negative, so it charges forward to the terminal event.
check "epsilon post-result block = 500s" "[ \"\$(col epsilon blocked_s)\" = 500 ]"
check "epsilon net <= gross"            "[ \"\$(col epsilon accepted_to_result_net_s)\" -le \"\$(col epsilon accepted_to_result_s)\" ]"

# zeta: closed, reopened out of review, closed again. Only the final close counts.
check "zeta measured once"              "[ \"\$(grep -c '^| zeta |' '$MD')\" = 3 ]"
check "zeta closed at the second close" "[ \"\$(col zeta closed)\" = 2026-07-21T05:50:00Z ]"
check "zeta final_status=done"          "[ \"\$(col zeta final_status)\" = done ]"
# zeta's 3599s span sits in the window where an independently-rounded remainder
# renders an impossible clock. No cell may ever read Xh60m or a bare 60m.
check "zeta 3599s renders as 1h0m"      "grep -qF '| 1h0m |' '$MD'"
check "no impossible clock strings"     "! grep -qE '[0-9]+h60m|\\| 60m \\|' '$MD'"

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
"$M" "$PROJECT" --upto "$((BETA_CLOSE_SEQ - 1))" >/dev/null
check "--upto before beta close drops beta" "[ \"\$(col beta shape)\" = '«missing»' ]"
check "--upto before beta close keeps alpha" "[ \"\$(col alpha shape)\" = worked ]"
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
check "alpha predicted effort quoted" "entry alpha | grep -q '1 worker-hour'"
check "alpha actual effort scored"    "entry alpha | grep -q '1.0h'"
# All three verdict branches are exercised across alpha/epsilon/zeta, so an
# inverted or short-circuited comparison cannot pass this drill.
check "alpha effort verdict = hit"    "[ \"\$(verdict alpha effort)\" = hit ]"
check "alpha questions verdict = hit" "[ \"\$(verdict alpha questions)\" = hit ]"
check "alpha review left unscored"    "verdict alpha 'review outcome' | grep -q '^unscored'"
check "alpha coverage full"           "entry alpha | grep -q '^- coverage: full$'"

expect_fail_msg "re-scoring refused"  "already scored" "$S" alpha
"$S" alpha --again >/dev/null
check "--again re-scores"             "[ \"\$(grep -c '^### alpha — ' '$CAL')\" = 2 ]"

"$S" epsilon >/dev/null
check "epsilon effort verdict = over" "[ \"\$(verdict epsilon effort)\" = over ]"
check "epsilon scorable (task.failed closed it)" "entry epsilon | grep -q 'final status .failed.'"

"$S" zeta >/dev/null
check "zeta effort verdict = under"      "[ \"\$(verdict zeta effort)\" = under ]"
check "zeta questions verdict = under"   "[ \"\$(verdict zeta questions)\" = under ]"
check "zeta predicted range quoted"      "entry zeta | grep -q '1-2'"

"$S" beta >/dev/null
check "beta recorded as partial"      "entry beta | grep -q '^- coverage: partial'"
check "beta effort prediction quoted" "entry beta | grep -q '4 worker-hours'"
check "beta absent fields marked"     "entry beta | grep -q 'not predicted'"
check "beta absent field unpredicted" "[ \"\$(verdict beta questions)\" = unpredicted ]"

"$S" gamma >/dev/null
check "gamma recorded as unpredicted" "entry gamma | grep -q '^- coverage: unpredicted'"
check "gamma effort unpredicted"      "[ \"\$(verdict gamma effort)\" = unpredicted ]"

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
check "entries cite the conf's run"                  "entry alpha | grep -qF 'run \`metrics-drill\`'"
# ...and HIVE_RUN_ID / --run still win over the conf (the documented escape hatch).
expect_fail_msg "--run overrides the conf" "no events for run 'other-run'" \
  env -u HIVE_SPINE_JSON "$M" "$PROJECT" --run other-run
# A project with no committed conf has no well-defined run to measure.
mkdir -p "$WS/projects/confless/orders"
expect_fail_msg "project without a conf refused" "no project.conf for project 'confless'" \
  "$M" confless

# --- 5. the stop-line: read-only, no emits ------------------------------------
echo
echo "metrics drill: stop-lines"

check "fixture untouched" "[ \"\$(sha256sum '$FIXTURE' | cut -d' ' -f1)\" = '$FIXTURE_SUM' ]"
# Strip comments and blanks first: these greps look for *code* that writes, and a
# prose mention of committing in a comment is not a write path.
code() { grep -vE '^[[:space:]]*(#|$)' "$1"; }
check "hive-metrics contains no emit" "! code '$M' | grep -Eq '(^|[^-[:alnum:]_])emit[[:space:]]'"
check "hive-score contains no emit"   "! code '$S' | grep -Eq '(^|[^-[:alnum:]_])emit[[:space:]]'"
check "neither tool commits to git"   "! { code '$M'; code '$S'; } | grep -Eq 'git .*(commit|push)'"
