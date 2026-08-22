#!/usr/bin/env python3
"""Assert a textual property of one file in a tree — a witness, never a candidate grader.

Usage:
  text_property.py --file PATH (--present RE | --absent RE)
                   [--section-from RE --section-to RE] [--why TEXT]

This exists for the reviewer corpus's gold, where both ends are FIXED historical commits.
That is the whole reason a text-level assertion is legitimate here and would not be
legitimate in a worker grader: nothing is being compared against a solution a model might
have written differently — the question is whether one named property of one named file
really differs between two commits, which is what makes a must-find a fact rather than a
recollection.

Exit 0 when the property holds, 1 when it does not, 2 on a usage or environment error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True)
    ap.add_argument("--present")
    ap.add_argument("--absent")
    ap.add_argument("--section-from")
    ap.add_argument("--section-to")
    ap.add_argument("--why", default="")
    args = ap.parse_args()

    if bool(args.present) == bool(args.absent):
        print("usage: exactly one of --present / --absent")
        return 2

    path = Path(args.file)
    if not path.is_file():
        print(f"absent-file: {args.file} does not exist in this tree")
        return 1
    text = path.read_text(errors="replace")

    if args.section_from:
        start = re.search(args.section_from, text, re.M)
        if not start:
            print(f"no-section: {args.section_from!r} does not match in {args.file}")
            return 1
        rest = text[start.end():]
        end = re.search(args.section_to, rest, re.M) if args.section_to else None
        text = rest[: end.start()] if end else rest

    if args.present:
        hit = re.search(args.present, text, re.M)
        print(
            f"{'holds' if hit else 'FAILS'}: {args.file} "
            f"{'contains' if hit else 'does not contain'} {args.present!r}"
            + (f" — {args.why}" if args.why else "")
        )
        return 0 if hit else 1

    hit = re.search(args.absent, text, re.M)
    print(
        f"{'FAILS' if hit else 'holds'}: {args.file} "
        f"{'contains' if hit else 'does not contain'} {args.absent!r}"
        + (f" — {args.why}" if args.why else "")
    )
    return 1 if hit else 0


if __name__ == "__main__":
    raise SystemExit(main())
