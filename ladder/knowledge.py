"""Load the pinned coordination KB and persona role-blocks into R1's system prompt (V4).

The KB (`qual/kb/<name>/kb.md`) and the persona (`qual/personas/coordinator-v2/`) are
hash-pinned authoring artifacts (each dir carries a `HASHES` manifest). Every load verifies
the file's SHA-256 against that manifest, so a run can never silently use a tampered or
re-hashed artifact — the frozen run-config (V4) pins these same hashes.

The persona's `r1-system.txt` bundles the role blocks with a fork-format op reference
(`board "assign …"`); the ladder speaks its own `assign A w1` format via `ladder/opsheet.py`,
so `persona_blocks()` returns only the role blocks (up to `[MECHANICS]`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from qual.loader import QUAL_ROOT

_PERSONA_DIR = QUAL_ROOT / "personas" / "coordinator-v2"
_MECHANICS_MARKER = "[MECHANICS]"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pinned_hash(path: Path) -> str:
    """The SHA-256 recorded for `path` in the sibling `HASHES` manifest."""
    for line in (path.parent / "HASHES").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == path.name:
            return parts[0]
    raise ValueError(f"{path.name} is not listed in {path.parent / 'HASHES'}")


def _verified_text(path: Path) -> str:
    digest = sha256_of(path)
    pinned = _pinned_hash(path)
    if digest != pinned:
        raise ValueError(f"{path} SHA-256 {digest} != pinned {pinned}")
    return path.read_text()


def kb_path(name: str) -> Path:
    return QUAL_ROOT / "kb" / name / "kb.md"


def load_kb(name: str) -> str:
    """The coordination KB text (hash-verified) — rides an L3 cell's system prompt verbatim."""
    return _verified_text(kb_path(name))


def persona_blocks() -> str:
    """The coordinator-v2 persona role blocks (IDENTITY/OBJECTIVE/ENVIRONMENT/FEEDBACK),
    hash-verified, with the fork-format [MECHANICS]+op-reference tail dropped."""
    text = _verified_text(_PERSONA_DIR / "r1-system.txt")
    idx = text.find(_MECHANICS_MARKER)
    return (text[:idx] if idx != -1 else text).rstrip()
