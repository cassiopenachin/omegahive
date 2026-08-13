"""docs-triage: every file under docs/ is accounted for in docs/INDEX.md, exactly once.

The order's definition of done is a counting claim, so this checks it by counting. It never
looks at *where* a document was moved to — that is the triage verdict's business and the
reviewer's — only that the index is a complete, non-duplicating map of the tree.

Three things it deliberately does not do, all learned by running it against the accepted
outcome before trusting it — a gate that fails the outcome the operator accepted is a
broken grader, and would misreport a packaging defect as a model result:

* It counts **entries**, not occurrences. An index line for one document routinely cites
  another; a substring count reads those citations as duplicate entries and fails a correct
  index.
* It does not check that every filename mentioned in the index exists. Index prose cites
  documents by shortened names, and a naive existence check turns that into a false defect.
  Dangling *links* are the real risk and `link_integrity.py` owns them.
* It does **not** demand a line per file. The order permits a single entry for a directory
  as a whole, so a subtree accounted for by one line is accounted for. That is what makes
  completeness a reachable bar rather than a clerical one: the one subtree the historical
  outcome missed needs one line, not nine.

A directory may be covered file-by-file or by one entry for the directory as a whole, which
is what the order permits for the archive and evidence trees.

Usage: python3 docs_index_complete.py <repo-root>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: A list item whose text begins with the filename — an index *entry*, as opposed to a
#: mention of the same file inside some other entry's description.
ENTRY = re.compile(r"^\s*[-*|]\s*`?([^\s`|]+)")


def _entry_targets(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = ENTRY.match(line)
        if m:
            out.append(m.group(1).rstrip("`,:;"))
    return out


def _covered_dirs(text: str, docs: Path) -> set[str]:
    """Directories the index accounts for as a whole, named in a heading or an entry."""
    covered = set()
    for d in sorted(docs.rglob("*")):
        if not d.is_dir():
            continue
        rel = str(d.relative_to(docs))
        if re.search(rf"(?<!\w){re.escape(rel)}/", text):
            covered.add(rel)
    return covered


def main(root: Path) -> int:
    docs = root / "docs"
    index = docs / "INDEX.md"
    if not index.is_file():
        print("FAIL docs/INDEX.md is missing")
        return 1

    text = index.read_text(errors="replace")
    entries = _entry_targets(text)
    problems: list[str] = []

    files = [
        p for p in sorted(docs.rglob("*"))
        if p.is_file() and p.name != "INDEX.md" and ".git" not in p.parts
    ]
    if not files:
        print("FAIL docs/ holds no files")
        return 1

    covered = _covered_dirs(text, docs)

    # Several directories legitimately hold a file of the same name (three README.md files
    # already do). Matching an entry by basename alone would then count one entry against
    # every namesake and report each of them as a duplicate. A basename only identifies a
    # file when it is unique in the tree; otherwise the entry must carry a path.
    basename_counts: dict[str, int] = {}
    for f in files:
        basename_counts[f.name] = basename_counts.get(f.name, 0) + 1

    gaps: list[str] = []
    for p in files:
        rel = str(p.relative_to(docs))
        parent = str(p.parent.relative_to(docs))
        unique_name = basename_counts[p.name] == 1
        listed = sum(
            1 for e in entries
            if e == rel or e.endswith("/" + rel) or (unique_name and e == p.name)
        )
        if listed > 1:
            problems.append(f"has {listed} index entries: docs/{rel}")
            continue
        if listed == 1:
            continue
        if parent != "." and any(parent == c or parent.startswith(c + "/") for c in covered):
            continue
        gaps.append(f"docs/{rel}")

    for gap in gaps:
        print(f"FAIL not accounted for in the index: {gap}")
    for problem in problems:
        print(f"FAIL {problem}")
    if gaps or problems:
        print(
            f"\n{len(gaps)} unaccounted file(s) and {len(problems)} duplicate entr(ies); "
            f"{len(files)} file(s) under docs/. A directory covered by one index line counts "
            "as covered — an unaccounted subtree needs one line, not one per file."
        )
        return 1
    print(f"ok — {len(files)} file(s) under docs/, each accounted for exactly once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
