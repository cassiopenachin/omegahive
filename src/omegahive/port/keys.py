"""Content+basis idempotency keys (§3a) and the durable basis store.

    key = SHA-256(run_id ‖ actor_id ‖ op_type ‖ canonical_payload ‖ basis_seq ‖ occ)

Two emits share a key iff they are the same op decided from the same observed board.
An accidental replay (library retry, crashed-and-redispatched turn with persisted
basis, LLM retry against a stale view) reproduces the key -> dedupes. An intentional
repeat interposes new observed state (a fresh read or the client's own intervening
accepted emit) -> basis_seq moved -> new key. `occ` covers identical ops within one
batch. Keys are derived from the canonicalized parsed Op, never from raw LLM text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

_SEP = "‖"  # ‖ — a separator that cannot occur in the field values


def canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def derive_key(
    run_id: str, actor_id: str, op_type: str, payload: dict, basis_seq: int, occ: int = 0,
) -> str:
    material = _SEP.join(
        [run_id, actor_id, op_type, canonical_payload(payload), str(basis_seq), str(occ)]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class BasisStore:
    """basis_seq persisted per (run, actor) in the binding workdir, surviving subprocess
    exit and crash-redispatch. basis_seq = max(last read cursor seq, last accepted emit
    seq). Atomic write (temp + fsync + replace) so a crash never leaves a torn value."""

    def __init__(self, workdir: str | Path, run_id: str, actor_id: str) -> None:
        self._dir = Path(workdir)
        self._dir.mkdir(parents=True, exist_ok=True)
        safe = f"{run_id}__{actor_id}".replace("/", "_")
        self._path = self._dir / f"basis_{safe}.json"

    def get(self) -> int:
        try:
            return int(json.loads(self._path.read_text())["basis_seq"])
        except (FileNotFoundError, KeyError, ValueError):
            return 0

    def observe(self, seq: int | None) -> None:
        """Advance basis_seq to max(current, seq). No-op for None."""
        if seq is None:
            return
        current = self.get()
        if seq <= current:
            return
        self._atomic_write(seq)

    def _atomic_write(self, seq: int) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"basis_seq": seq}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
