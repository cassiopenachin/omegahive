"""Freeze-checklist invariants (v3-fixes B5, non-gating hygiene): assertions the
persona/KB freeze checklist requires but that need no board parameters and no DB — a
pure filesystem scan, so they run everywhere the rest of the suite does.

"No `prompt_<provider>.txt` in any volume template" (stage-2 spec §5.5): a stray
per-provider override resolves first and silently replaces the pinned, hash-frozen
persona for whichever provider it names — breaking cross-cell persona identity exactly
where cheap and strong cells differ (different providers). No volume templates exist in
this repo yet (they land in V4/O4); this test is a placeholder in the sense that it
currently guards an empty set, but the assertion is real and becomes load-bearing the
day the first template is added — it is not deferred or skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that can never legitimately hold a persona override — pruned during the
# walk (not filtered after) so the scan never descends into .venv's tens of thousands of
# files.
_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}


def _iter_repo_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def test_no_provider_override_prompt_file_exists_anywhere():
    offenders = [p for p in _iter_repo_files() if p.name.startswith("prompt_") and
                p.suffix == ".txt"]
    assert not offenders, (
        f"found prompt_<provider>.txt override(s) that would silently replace the frozen "
        f"persona per-provider: {[str(p.relative_to(REPO_ROOT)) for p in offenders]}"
    )
