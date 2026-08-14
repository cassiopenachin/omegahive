"""ptc-revalidate stop-line: the subjects under test were not patched.

The order is explicit — findings get filed, not fixed. A candidate that "fixes" a path
mismatch by editing the subject has changed what it measured. Anything the candidate added
under a vendor tree is a modification of a subject as far as this check is concerned; the
correct place to resolve an environment mismatch is the mount layout or the wrapper.

Usage: python3 ptc_subjects_unpatched.py <repo-root>
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VENDOR_MARKERS = ("vendor/", "third_party/", "PeTTaChainer/", "PeTTa/", "external/")


def main(root: Path) -> int:
    base = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    if not base:
        print("FAIL cannot resolve the baseline commit")
        return 1
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, check=False)
    changed = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", base[-1]],
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()

    touched = [f for f in changed if any(m in f for m in VENDOR_MARKERS)]
    for f in touched:
        print(f"FAIL subject under test modified (tracked): {f}")

    # The vendored subjects are normally fetched into an ignored directory, so a patch there
    # never reaches the candidate repo's own diff. Ask each vendored checkout directly.
    dirty = _dirty_vendored_checkouts(root)
    for line in dirty:
        print(f"FAIL subject under test modified (working tree): {line}")

    if touched or dirty:
        print(
            f"\n{len(touched) + len(dirty)} modification(s) to a subject under test. The "
            "subjects are what the verdict is about; patching one invalidates it."
        )
        return 1
    print(
        f"ok — {len(changed)} changed file(s), none under a vendored subject; "
        f"{_vendor_count(root)} vendored checkout(s) clean"
    )
    return 0


def _vendored_roots(root: Path) -> list[Path]:
    out = []
    for marker in ("vendor", "third_party", "external"):
        base = root / marker
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if (child / ".git").exists():
                out.append(child)
    return out


def _vendor_count(root: Path) -> int:
    return len(_vendored_roots(root))


def _dirty_vendored_checkouts(root: Path) -> list[str]:
    dirty: list[str] = []
    for repo in _vendored_roots(root):
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        ).stdout.splitlines()
        for line in status:
            if line.strip():
                dirty.append(f"{repo.relative_to(root)}: {line.strip()}")
    return dirty


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
