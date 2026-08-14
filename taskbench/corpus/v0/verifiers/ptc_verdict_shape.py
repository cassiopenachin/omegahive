"""ptc-revalidate: the verdict document exists, is pinned, and answers both modes.

Shape and pins only. What the verdict *says* is a discovered fact, not a target — a
different honest per-mode status at these shas is a fidelity signal about the environment,
not a wrong answer, and scoring it here would turn the benchmark into a memory test.

Usage: python3 ptc_verdict_shape.py <repo-root>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The two shas the order pins. A verdict that does not name them is not about them.
REQUIRED_PINS = ("02a85c6", "43705f5")


def main(root: Path) -> int:
    doc = root / "docs" / "ptc-verdict.md"
    if not doc.is_file():
        print("FAIL docs/ptc-verdict.md is missing — the order names that exact path")
        return 1
    text = doc.read_text(errors="replace")
    low = text.lower()
    problems: list[str] = []

    if len(text.split()) < 120:
        problems.append(f"the verdict is {len(text.split())} words — too thin to be a verdict")

    for pin in REQUIRED_PINS:
        if pin not in text:
            problems.append(f"the verdict never names the pinned sha {pin}")

    for mode in ("forward", "backward"):
        if not re.search(rf"\b{mode}[- ]?chain", low):
            problems.append(f"the verdict states no status for {mode} chaining")

    if not re.search(r"\b(pass|fail|passed|failed|broken|works?|working)\b", low):
        problems.append("the verdict states no per-mode status in words a reader can act on")

    if "test" not in low:
        problems.append("the verdict records no test-suite results")

    for p in problems:
        print(f"FAIL {p}")
    if problems:
        return 1
    print("ok — verdict present at the named path, both modes addressed, both shas named")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
