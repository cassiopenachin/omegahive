"""taskbench — the HIP-1 M1b task-replay benchmark.

An **evaluation-only** instrument: it replays closed, bounded hive and tenant orders
against a candidate agent and grades the result. It is deliberately a separate namespace
from `qual/` (the C2 coordinator battery). The two share record *idioms* — canonical-JSON
hashing, atomic writes, config pins, a shared validator — and nothing else. `qual`
measures whether a model can drive the OmegaClaw loop against a board; taskbench measures
whether a model can close a written order. Do not relabel either as the other.

What this package never does: emit to the spine, point an agent at the live workspace or
the canonical code checkout, handle API credentials (agent and reviewer commands arrive as
argv from the operator's launch), or execute a held-out task.

Operating guide: `docs/reference/omegahive_taskbench_guide.md`.

Controlling refs: HIP-1 M1b + amendments 5/7
(`projects/omegahive/hips/2026-08-11-model-routing.md` in the hive workspace); the order
`projects/omegahive/orders/2026-08-12-hip1-bench-seed.md`. Record discipline is modelled
on `docs/reference/omegahive_c2_battery_spec.md` §8.
"""

from __future__ import annotations

__all__ = ["CORPUS_ROOT", "TASKBENCH_ROOT"]

from pathlib import Path

TASKBENCH_ROOT = Path(__file__).resolve().parent
CORPUS_ROOT = TASKBENCH_ROOT / "corpus"
