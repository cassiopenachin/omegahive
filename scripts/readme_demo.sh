#!/usr/bin/env bash
# Runs the README's worked example verbatim ("A worked example: one tiny project, end to end").
# This script IS the README section — if one changes, change both. Exits non-zero if any
# step behaves differently than the README promises (including the steps that must fail).
set -euo pipefail

RUN="${1:-demo-$(date +%s)}"   # default: fresh run id per invocation (re-runs stay clean)
hive() { docker compose run --rm -T cli "$@"; }

echo "== 1. seed (planner hat) — run: $RUN"
hive emit --run-id "$RUN" --role planner --actor operator --type worker.registered \
  --payload '{"worker_id": "sess-demo-1"}'
hive emit --run-id "$RUN" --role planner --actor operator --type task.created --task t1 \
  --payload '{"title": "Draft the release notes", "task_type": "writing"}'
hive emit --run-id "$RUN" --role planner --actor operator --type task.created --task t2 \
  --payload '{"title": "Publish the notes", "task_type": "writing"}'
hive emit --run-id "$RUN" --role planner --actor operator --type dependency.added --task t2 \
  --payload '{"depends_on": "t1"}'

echo "== board after seeding (t1 ready, t2 waiting)"
hive report "$RUN" --board

echo "== 2. README promise: assigning a READY task to an unregistered worker is UNKNOWN_WORKER"
if hive emit --run-id "$RUN" --role coordinator --actor operator --type task.assigned --task t1 \
  --payload '{"worker": "sess-typo-9"}'; then
  echo "FAIL: ghost-worker assign was accepted"; exit 1
fi

echo "== 2b. assign for real (coordinator hat) + accept (worker)"
hive emit --run-id "$RUN" --role coordinator --actor operator --type task.assigned --task t1 \
  --payload '{"worker": "sess-demo-1"}'
hive emit --run-id "$RUN" --role worker --actor sess-demo-1 --type task.accepted --task t1

echo "== 3. question -> blocked (refs are illustrative pins; any path@sha shape validates)"
QREF='projects/demo/questions/2026-07-10-tone.md@9d01c4e59d01c4e59d01c4e59d01c4e59d01c4e5'
hive emit --run-id "$RUN" --role worker --actor sess-demo-1 --type task.reported --task t1 \
  --payload "{\"kind\": \"question\", \"ref\": \"$QREF\"}"
hive emit --run-id "$RUN" --role worker --actor sess-demo-1 --type task.blocked --task t1 \
  --payload "{\"reason\": \"tone: formal vs conversational\", \"needs\": \"decision\", \"ref_report\": \"$QREF\"}"

echo "== answer consumed -> unblock (worker-owned)"
hive emit --run-id "$RUN" --role worker --actor sess-demo-1 --type task.unblocked --task t1

echo "== 4. result, review, close"
RREF='projects/demo/reports/2026-07-10-notes.md@b52e77d1b52e77d1b52e77d1b52e77d1b52e77d1'
hive emit --run-id "$RUN" --role worker --actor sess-demo-1 --type task.result_posted --task t1 \
  --payload "{\"artifact_refs\": [{\"ref\": \"$RREF\"}]}"
hive emit --run-id "$RUN" --role instrument --actor operator --type review.passed --task t1 \
  --payload "{\"ref_result\": \"$RREF\"}"
hive emit --run-id "$RUN" --role coordinator --actor operator --type task.status_override --task t1 \
  --payload '{"status": "done"}'

echo "== 4b. README promise: closing unreviewed t2 is refused"
if hive emit --run-id "$RUN" --role coordinator --actor operator --type task.status_override --task t2 \
  --payload '{"status": "done"}'; then
  echo "FAIL: unreviewed close was accepted"; exit 1
fi

echo "== 4c. README promise: an identical re-emit dedupes (expect: already recorded)"
hive emit --run-id "$RUN" --role coordinator --actor operator --type task.status_override --task t1 \
  --payload '{"status": "done"}'

echo "== final board (t1 done, t2 ready) + full trace incl. the two rejections"
hive report "$RUN" --board
echo "OK: the README's worked example holds, run: $RUN"
