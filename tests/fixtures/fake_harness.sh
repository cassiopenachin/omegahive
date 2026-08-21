#!/usr/bin/env bash
# fake_harness.sh — a deterministic stand-in for a real agent harness.
#
# It exists so the full turn path (version probe, cursor, started, child, structured
# stream, scan, classification, usage extraction, terminal fact, summary) can be
# exercised end to end with no paid model call, no network, and no vendor CLI installed.
#
# Like the two shipped harnesses it writes a STRUCTURED STREAM on stdout, one JSON object
# per line, and it honours both `--session-id <uuid>` (an initial turn pins one) and
# `--resume <id>` (a resume turn wakes one). Echoing the resumed id back is what lets a
# test assert continuity: a fake that invented a fresh id on resume would pass a test the
# real harnesses would fail.
#
# Behaviour is chosen by HIVE_FAKE_BEHAVIOUR:
#
#   success      structured stream ending `completed`, exit 0     (the happy path)
#   failure      structured stream ending `error`, exit 3         (non-zero terminal)
#   budget       structured stream ending `budget`, exit 0        (the budget row)
#   interrupted  sleep until signalled, exit 130                  (no terminal record)
#   nousage      a clean stream, but NO usage file                (unavailable surface)
#   wrongmodel   a usage file naming a DIFFERENT model            (the mismatch stop-line)
#   crash        exit 1 having written nothing                    (missing evidence)
#   malformed    a stream with an unparseable line in the middle  (preserved, not repaired)
#   truncated    a stream cut off mid-line, exit 137              (a killed harness)
#   protocol     a full stream, then run $HIVE_FAKE_SCRIPT        (the worker protocol)
#
# `protocol` is what makes the lifecycle test end-to-end rather than a mock: the script it
# runs is an ordinary worker session's commands — the issued emit wrapper, the issued
# sync/publish wrapper, git in its own clones — executed as a child of the real turn
# runner, in the real task root, with the real constructed environment.
#
# The usage file it writes is deliberately a different SHAPE from Claude Code's
# transcript — one JSON object per message, no per-content-block repetition — so a test
# that passes here is not accidentally re-testing the transcript parser.

set -euo pipefail

MODEL=""
SESSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) echo "fake-harness 9.9.9"; exit 0 ;;
    --model)   shift; MODEL="${1:-}" ;;
    --session-id) shift; SESSION="${1:-}" ;;
    --resume)  shift; SESSION="${1:-}" ;;
    *) : ;;   # the prompt and anything else: accepted and ignored
  esac
  shift
done

BEHAVIOUR="${HIVE_FAKE_BEHAVIOUR:-success}"
USAGE_FILE="${HIVE_FAKE_USAGE_FILE:-}"

emit_session() {
  printf '{"type":"session","session_id":"%s","model":"%s"}\n' "$SESSION" "$MODEL"
}

emit_result() {  # emit_result <status>
  printf '{"type":"result","status":"%s"}\n' "$1"
}

write_usage() {  # write_usage <model>
  [ -n "$USAGE_FILE" ] || return 0
  # Two messages, and the first is written TWICE with the same message_id — the
  # deduplication the extractor must perform. A parser that sums rows instead of
  # deduplicating them reports 300 output tokens here; the correct answer is 200.
  {
    printf '{"message_id":"m1","model":"%s","input_tokens":10,"cache_read_tokens":1000,"cache_write_tokens":50,"output_tokens":100}\n' "$1"
    printf '{"message_id":"m1","model":"%s","input_tokens":10,"cache_read_tokens":1000,"cache_write_tokens":50,"output_tokens":100}\n' "$1"
    printf '{"message_id":"m2","model":"%s","input_tokens":20,"cache_read_tokens":2000,"cache_write_tokens":0,"output_tokens":100}\n' "$1"
  } > "$USAGE_FILE"
}

case "$BEHAVIOUR" in
  success)
    emit_session; write_usage "$MODEL"; emit_result completed; exit 0 ;;
  failure)
    emit_session; write_usage "$MODEL"
    echo "fake harness failing on purpose" >&2
    emit_result error; exit 3 ;;
  budget)
    emit_session; write_usage "$MODEL"; emit_result budget; exit 0 ;;
  wrongmodel)
    emit_session; write_usage "some-other-model-9"; emit_result completed; exit 0 ;;
  nousage)
    emit_session; emit_result completed; exit 0 ;;
  crash)
    echo "fake harness crashed before doing anything" >&2; exit 1 ;;
  malformed)
    # A bad line in the MIDDLE. The scan must count it, keep the surrounding records, and
    # never try to repair it — and the renderer must survive it.
    emit_session
    printf 'this is not json at all\n'
    write_usage "$MODEL"
    emit_result completed; exit 0 ;;
  truncated)
    # A stream cut off mid-line with no trailing newline, and a signal-shaped exit code:
    # what a killed harness actually leaves behind.
    emit_session
    printf '{"type":"result","status":"comp'
    exit 137 ;;
  protocol)
    emit_session
    write_usage "$MODEL"
    [ -n "${HIVE_FAKE_SCRIPT:-}" ] || { echo "protocol behaviour needs HIVE_FAKE_SCRIPT" >&2; exit 64; }
    rc=0
    bash "$HIVE_FAKE_SCRIPT" || rc=$?
    if [ "$rc" -eq 0 ]; then emit_result completed; else emit_result error; fi
    exit "$rc" ;;
  interrupted)
    # Wait to be signalled. The trap makes the exit status the conventional 128+SIGTERM
    # so the runner sees a non-zero code, and the stream carries no terminal record —
    # which is exactly the `missing` evidence shape.
    trap 'exit 130' TERM INT
    emit_session
    write_usage "$MODEL"
    # A bounded wait, so a wedged test fails in seconds rather than hanging a suite.
    for _ in $(seq 1 100); do sleep 0.1; done
    exit 0 ;;
  *) echo "unknown HIVE_FAKE_BEHAVIOUR: $BEHAVIOUR" >&2; exit 64 ;;
esac
