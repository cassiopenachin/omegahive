"""Deterministic per-(seed, agent, task, attempt) RNG.

Uses hashlib (NOT Python's built-in hash(), which is salted per-process via
PYTHONHASHSEED and would break replay) to derive a stable seed. M4 consumes this
seam for the stochastic worker: `attempt` is the worker's own per-task assignment
count, so each assignment is an independent draw and a re-attempt can differ — and
each seed reproduces independently of whatever else is in the events table.
"""

from __future__ import annotations

import hashlib
from random import Random


def rng_for(seed: int, agent_id: str, task_id: str | None, attempt: int) -> Random:
    key = f"{seed}:{agent_id}:{task_id or ''}:{attempt}".encode()
    digest = hashlib.sha256(key).digest()[:8]
    return Random(int.from_bytes(digest, "big"))
