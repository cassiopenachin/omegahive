"""Deterministic per-(seed, agent, task) RNG.

Uses hashlib (NOT Python's built-in hash(), which is salted per-process via
PYTHONHASHSEED and would break replay) to derive a stable seed. M1 happy-path
latencies are fixed by policy so this isn't consumed yet, but the seam exists so
M2 stochastic worker policies drop in without an engine change.
"""

from __future__ import annotations

import hashlib
from random import Random


def rng_for(seed: int, agent_id: str, task_id: str | None) -> Random:
    key = f"{seed}:{agent_id}:{task_id or ''}".encode()
    digest = hashlib.sha256(key).digest()[:8]
    return Random(int.from_bytes(digest, "big"))
