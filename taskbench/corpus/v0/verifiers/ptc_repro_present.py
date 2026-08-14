"""ptc-revalidate: a committed, runnable repro script exists and the verdict points at it.

The order requires a script "committed and re-runnable from a clean clone". Its path is the
candidate's to choose, so this looks for a newly-added script that the verdict document
itself references — the pairing is what makes the rerunnability claim checkable without
assuming any particular layout.

The script is not executed here: running it needs a container build and the pinned upstream
snapshots, which is the cell's environment, not this check's.

Usage: python3 ptc_repro_present.py <repo-root>
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _added_since_baseline(root: Path) -> list[str]:
    base = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    if not base:
        return []
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, check=False)
    out = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=A", base[-1]],
        capture_output=True, text=True, check=False,
    )
    return [f for f in out.stdout.splitlines() if f]


def main(root: Path) -> int:
    doc = root / "docs" / "ptc-verdict.md"
    if not doc.is_file():
        print("FAIL docs/ptc-verdict.md is missing, so nothing can point at a repro script")
        return 1
    verdict_text = doc.read_text(errors="replace")

    added = _added_since_baseline(root)
    scripts = [f for f in added if f.endswith((".sh", ".bash", ".py"))]
    if not scripts:
        print("FAIL no script was added; the order requires a committed repro script")
        return 1

    referenced = [s for s in scripts if Path(s).name in verdict_text or s in verdict_text]
    if not referenced:
        print(
            "FAIL the verdict document references none of the added scripts "
            f"({', '.join(scripts[:8])}) — the rerunnability claim has no named subject"
        )
        return 1

    non_empty = [s for s in referenced if (root / s).is_file() and (root / s).stat().st_size > 200]
    if not non_empty:
        print(f"FAIL the referenced script(s) {referenced} are empty or missing on disk")
        return 1

    print(f"ok — repro script(s) committed and referenced by the verdict: {', '.join(non_empty)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
