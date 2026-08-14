#!/usr/bin/env bash
# fake_harness.sh — a deterministic stand-in for a real agent harness.
#
# It exists so the full supervisor path (version probe, started, child, usage
# extraction, terminal fact) can be exercised end to end with no paid model call, no
# network, and no vendor CLI installed. Behaviour is chosen by HIVE_FAKE_BEHAVIOUR:
#
#   success      write a usage file, exit 0                       (the happy path)
#   failure      write a usage file, exit 3                       (non-zero terminal)
#   interrupted  sleep until signalled, exit 130                  (drives the trap)
#   nousage      write NOTHING, exit 0                            (unavailable surface)
#   wrongmodel   write a usage file naming a DIFFERENT model      (the mismatch stop-line)
#   crash        exit 1 before writing anything                   (early death)
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
    *) : ;;   # the kickoff prompt and anything else: accepted and ignored
  esac
  shift
done

BEHAVIOUR="${HIVE_FAKE_BEHAVIOUR:-success}"
USAGE_FILE="${HIVE_FAKE_USAGE_FILE:-}"

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
  success)     write_usage "$MODEL"; echo "fake harness ran model=$MODEL session=$SESSION"; exit 0 ;;
  failure)     write_usage "$MODEL"; echo "fake harness failing on purpose" >&2; exit 3 ;;
  wrongmodel)  write_usage "some-other-model-9"; echo "fake harness ran a different model" >&2; exit 0 ;;
  nousage)     echo "fake harness wrote no usage surface"; exit 0 ;;
  crash)       echo "fake harness crashed before doing anything" >&2; exit 1 ;;
  interrupted)
    # Wait to be signalled. The trap makes the exit status the conventional 128+SIGTERM
    # so the supervisor sees a non-zero code alongside its own interrupted flag.
    trap 'exit 130' TERM INT
    write_usage "$MODEL"
    # A bounded wait, so a wedged test fails in seconds rather than hanging a suite.
    for _ in $(seq 1 100); do sleep 0.1; done
    exit 0 ;;
  *) echo "unknown HIVE_FAKE_BEHAVIOUR: $BEHAVIOUR" >&2; exit 64 ;;
esac
