"""docs-triage: every relative markdown link in docs/ and README.md resolves.

The order's guard against the named risk — silent link breakage caused by the moves. Only
relative links are followed; anchors, mailto and absolute URLs are out of scope, as is any
link inside a fenced code block, which is illustration rather than navigation.

Usage: python3 link_integrity.py <repo-root>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")


def _strip_fences(text: str) -> str:
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        out.append("" if fenced else line)
    return "\n".join(out)


def main(root: Path) -> int:
    targets = [p for p in sorted((root / "docs").rglob("*.md"))]
    readme = root / "README.md"
    if readme.is_file():
        targets.append(readme)
    if not targets:
        print("FAIL no markdown found under docs/ and no README.md")
        return 1

    dangling: list[str] = []
    checked = 0
    for path in targets:
        body = _strip_fences(path.read_text(errors="replace"))
        for target in LINK.findall(body):
            if target.startswith(SKIP_PREFIXES):
                continue
            bare = target.split("#", 1)[0]
            if not bare:
                continue
            checked += 1
            # A leading slash means "from the repository root", not "from the filesystem
            # root". Resolving it against the host would call every such link dangling.
            resolved = (
                (root / bare.lstrip("/")) if bare.startswith("/") else (path.parent / bare)
            ).resolve()
            if not resolved.exists():
                dangling.append(f"{path.relative_to(root)} -> {target}")

    for d in dangling:
        print(f"FAIL dangling link: {d}")
    if dangling:
        print(f"\n{len(dangling)} dangling of {checked} relative link(s)")
        return 1
    print(f"ok — {checked} relative link(s) in {len(targets)} file(s), none dangling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
