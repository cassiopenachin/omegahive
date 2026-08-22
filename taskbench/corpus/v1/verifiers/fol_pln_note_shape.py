#!/usr/bin/env python3
"""fol-pln-mapping: does the design note answer everything a converter order must ask it?

Usage: fol_pln_note_shape.py <repo-root>

The order's deliverable is a document "that a FOLIO converter order can be written from",
and it enumerates exactly what that means: a measured construct inventory, a decision or a
declared gap for every construct above a stated frequency, the two-arm reporting
recommendation with a realization path, and the run protocol. Those are structural
questions, and structure is what this checks.

It deliberately does NOT judge whether an encoding is SOUND, whether a gap's recommended
treatment is the right one, or whether the arithmetic in the protocol holds. Those are the
substance of the task and they are the rubric's, stated there as such. What a script can
tell is whether the document leaves a hole where the converter order needs an answer —
which is the failure mode a research note actually has.

Every construct and section below is named by the order itself. Nothing is read off the
accepted document.

Exit 0 when the note answers everything, 1 otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

NOTE = "docs/fol-pln-mapping.md"
REPORT = "benchmarks/folio/REPORT.html"
REPROS = "tests/repros/fol"

#: The construct families the order's scope 1 lists. Each needs a decision or a declared
#: gap; a note that never mentions one has left the converter order to guess.
CONSTRUCTS = {
    "universal quantification": (r"∀", r"\bforall\b", r"\buniversal"),
    "existential quantification": (r"∃", r"\bexists\b", r"\bexistential"),
    "quantifier nesting": (r"nest", r"nested quantifi"),
    "conjunction": (r"∧", r"\bconjunction", r"\band-antecedent"),
    "disjunction": (r"∨", r"\bdisjunction"),
    "implication": (r"→", r"\bimplication"),
    "biconditional": (r"↔", r"\bbiconditional", r"\bif and only if"),
    "negation, including its position": (r"¬", r"\bnegation"),
    "predicate arity": (r"\barit(y|ies)", r"\bn-ary\b", r"\bbinary predicate"),
    "function symbols": (r"function symbol", r"\bfunction terms?\b", r"\bskolem"),
}

#: The order's scope 3 and 4 — the two decisions a converter order cannot proceed without.
SECTIONS = {
    "a deductive-subset reporting arm": (r"deduct", ),
    "a full reporting arm with confidence calibration": (r"calibrat", r"confidence"),
    "how the chosen arm is realized without editing the vendored runtime": (
        r"vendor", r"derived rule", r"regenerat", r"base[- ]rate",
    ),
    "slice versus full-dataset scale, costed": (r"\bslice\b", r"\bfull\b.{0,40}\b1,?4[0-9]{2}\b",
                                                r"CPU[- ]hour"),
    "a budget policy": (r"budget", ),
    "a per-question timeout": (r"timeout", ),
    "liveness canaries": (r"canar", r"known-derivable"),
}


def read(root: Path, rel: str) -> str | None:
    p = root / rel
    return p.read_text(errors="replace") if p.is_file() else None


def changed_files(root: Path) -> list[str]:
    """Paths the attempt changed, relative to the cell's single baseline commit."""
    base = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    if not base:
        return []
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, check=False)
    out = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", base[-1], "--", "."],
        capture_output=True, text=True, check=False,
    )
    return [f for f in out.stdout.splitlines() if f]


def _cited(note: str, name: str, stem: str) -> str | None:
    """Is this micro-example pointed at from the note?

    A note may cite a repro by filename, by stem, or — as a numbered set naturally does —
    by its number in a table cell or beside the word "repro". All three are the reader
    finding the file, which is what the requirement is for.
    """
    if name in note or stem in note:
        return name
    prefix = re.match(r"\d+", stem)
    if not prefix:
        return None
    n = prefix.group(0)
    if re.search(rf"repros?\s*[`'\"(]?{n}\b", note, re.I) or re.search(rf"`{n}`", note):
        return name
    return None


def has_frequency_table(text: str) -> bool:
    """A markdown table with at least four data rows carrying a count or a percentage."""
    rows = 0
    for line in text.splitlines():
        if line.count("|") >= 3 and re.search(r"\d[\d,]*(\.\d+)?\s*%?", line):
            if re.search(r"\|\s*\d[\d,]*(\.\d+)?\s*%?\s*\|", line):
                rows += 1
    return rows >= 4


def main(root_str: str) -> int:
    root = Path(root_str).resolve()
    findings: list[str] = []

    note = read(root, NOTE)
    if note is None:
        print(f"FAIL missing: {NOTE} is the order's deliverable and does not exist")
        return 1
    print(f"pass  {NOTE} exists ({len(note.splitlines())} lines)")
    low = note.lower()

    for label, patterns in CONSTRUCTS.items():
        if any(re.search(p, note, re.I) for p in patterns):
            print(f"pass  the note reaches {label}")
        else:
            findings.append(
                f"FAIL inventory: the note never mentions {label}. The order asks for a "
                "decision or a declared gap for every construct in the gold annotations; a "
                "construct the note is silent about is one the converter order has to guess at."
            )

    if has_frequency_table(note):
        print("pass  the note carries a measured frequency table")
    else:
        findings.append(
            "FAIL inventory: the note carries no frequency table with counts. The order asks "
            "for the inventory measured, not assumed, and the frequency is what decides which "
            "constructs need a decision at all."
        )

    for label, patterns in SECTIONS.items():
        if any(re.search(p, note, re.I) for p in patterns):
            print(f"pass  the note settles {label}")
        else:
            findings.append(f"FAIL protocol: the note does not settle {label}")

    repro_dir = root / REPROS
    repros = sorted(p for p in repro_dir.glob("*") if p.is_file() and p.suffix in
                    (".metta", ".py", ".sh")) if repro_dir.is_dir() else []
    if not repros:
        findings.append(
            f"FAIL repros: no micro-examples under {REPROS}. The order requires every "
            "encoding decision to rest on a runnable example rather than on an argument."
        )
    else:
        print(f"pass  {len(repros)} micro-example(s) under {REPROS}")
        unverifying = [
            p.name for p in repros
            if p.suffix == ".metta" and not re.search(r"assert", p.read_text(errors="replace"), re.I)
        ]
        if unverifying:
            findings.append(
                "FAIL repros: these micro-examples assert nothing, so running them cannot "
                f"fail: {', '.join(sorted(unverifying))}. The order requires them to be "
                "self-verifying at the pins."
            )
        else:
            print("pass  every micro-example asserts an expected value")
        cited = sum(1 for p in repros if _cited(note, p.name, p.stem))
        if cited >= 3:
            print(f"pass  the note points at {cited} of its micro-examples by name")
        else:
            findings.append(
                f"FAIL repros: the note names only {cited} micro-example(s). An encoding "
                "decision whose repro a reader cannot find is an argument, not a verification."
            )

    report = read(root, REPORT)
    if report is None:
        findings.append(f"FAIL report: {REPORT} was never seeded; the order asks for sections 1-2")
    else:
        print(f"pass  {REPORT} exists")
        for label, pattern in (
            ("a worked example", r"example"),
            ("the published few-shot baseline the order quotes", r"64\.2"),
            ("the published chain-of-thought baseline the order quotes", r"69\.5"),
            ("at least one source citation", r"https?://|arxiv|doi"),
        ):
            if re.search(pattern, report, re.I):
                print(f"pass  the seeded report carries {label}")
            else:
                findings.append(
                    f"FAIL report: the seeded report carries no {label}. Section 2 is the "
                    "known-results frame and it is only useful with its sources."
                )

    touched_vendor = [f for f in changed_files(root) if f.startswith("vendor/")]
    if touched_vendor:
        findings.append(
            "FAIL stop-line: the attempt edited the vendored runtime "
            f"({', '.join(touched_vendor[:5])}). A derived rule file is specified in the note, "
            "never built by editing vendor/."
        )
    else:
        print("pass  the vendored runtime is untouched")

    if re.search(r"scores? (better|higher)|improves? (the )?(score|accuracy)", low):
        print(
            "NOTE the note contains score-improvement language. The order disqualifies "
            "'this choice scores better' as an argument for an encoding, but a note may "
            "legitimately REPORT the effect while refusing it as a reason — whether this "
            "instance argues from it is a rubric question, not a script's."
        )

    for f in findings:
        print(f)
    if findings:
        print(f"\n{len(findings)} gap(s) a converter order would fall into")
        return 1
    print("\nok: the note answers every question the order says a converter order will ask")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
