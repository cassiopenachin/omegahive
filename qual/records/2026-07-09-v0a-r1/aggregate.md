# Qualification aggregate — v0a-r1 (2026-07-09)

Image `localhost/omegaclaw-hive:0.1` · role **v0a** · R=3 reps.

Emission-discipline subset. Board-op metrics need the hive board channel driven against a real board (v0b) and are omitted here.

## V0A1-stock-send

| model | pre-parse | post-parse | repair-dep | cmd-recog | silent-unk | pin-ok | idle-ok | junk-ops | tokens | wall-ms |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-opus-4-8 | 0.00 | 1.00 | 1.00 | 1.00 [0.96–1.00] | 0.00 [0.00–1.00] | 100% | 100% | 0.00 | 14473.00 [13723.00–14639.00] | 18000.00 [18000.00–20000.00] |
| glm-5.2 | 0.00 | 1.00 | 1.00 | 1.00 | 0.00 | 100% | 100% | 0.00 | 4144.00 [4050.00–4362.00] | 13000.00 [8000.00–14000.00] |
| minimax-m3 | 0.00 [0.00–1.00] | 1.00 [0.00–1.00] | 0.00 [0.00–1.00] | 1.00 [0.00–1.00] | 0.00 [0.00–7.00] | 0% | 100% | 0.00 | 1432.00 [0.00–1488.00] | 0.00 |
| qwen3.6-local | 0.00 | 1.00 | 1.00 | 1.00 | 0.00 | 100% | 100% | 0.00 | 11093.00 [5646.00–11647.00] | 18000.00 [18000.00–20000.00] |

_Batch-order sanity: N/A for the as-shipped one-call-one-emit binding._

