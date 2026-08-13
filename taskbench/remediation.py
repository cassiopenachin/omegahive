"""The one review-and-repair cycle — the pipeline this benchmark actually evaluates.

v0 scored a single-shot attempt against an outcome that was produced by a worker *plus* an
independent review and a repair. `run-registration` showed what that costs: the incumbent
shipped the same defect the historical worker shipped, and was marked red for lacking the
review round the accepted outcome had.

So a cell is one bounded pipeline: attempt → blinded review → **at most one** remediation →
every deterministic gate again → blinded final confirmation. Both verdicts are recorded. A
model that routinely needs rescue must not read as a clean generator, which is why the
first-pass verdict is never rewritten.

What the remediation may see is deliberately no more than an ordinary worker gets: its own
order, the review's findings, and its own verifier output. Never the historical patch, the
grader-only facts, held-out material, or anything about which candidate produced the work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .grade import DeterministicLeg, ReviewLeg

REMEDIATION_BRIEF = """\
# Rework — {title}

Your work on this order was reviewed and did not pass. You have **one** opportunity to fix
it. Work in the same tree you already have; everything you wrote is still there.

The order remains the authority for scope: `workspace/{order_path}`

## What the review found

{findings}

## What the checks reported

{verifier}

## How this is judged

Fix the findings above and nothing else — widening scope is its own defect. Leave your work
committed. Every deterministic check runs again, and the result is reviewed again; there is
no second opportunity after this one.
"""

NO_FINDINGS = (
    "_The review recorded no written finding; the deterministic checks below are what\n"
    "failed._"
)


@dataclass
class CycleRecord:
    """Both halves of the cycle, side by side and never collapsed."""

    first_deterministic: DeterministicLeg
    first_review: ReviewLeg
    first_passed: bool
    remediated: bool
    remediation_reason: str = ""
    final_deterministic: DeterministicLeg | None = None
    final_review: ReviewLeg | None = None

    def to_json(self) -> dict[str, Any]:
        from dataclasses import asdict

        return {
            "first_pass": {
                "passed": self.first_passed,
                "deterministic": asdict(self.first_deterministic),
                "review": asdict(self.first_review),
            },
            "remediated": self.remediated,
            "remediation_reason": self.remediation_reason,
            "final": (
                {
                    "deterministic": asdict(self.final_deterministic),
                    "review": asdict(self.final_review),
                }
                if self.final_deterministic is not None and self.final_review is not None
                else None
            ),
        }


def findings_text(review: ReviewLeg) -> str:
    """The review's findings, rendered for the worker — verbatim, and nothing else from it."""
    verdict = review.verdict or {}
    defects = verdict.get("would_have_shipped_defects") or []
    if not defects:
        return NO_FINDINGS
    out: list[str] = []
    for i, d in enumerate(defects, 1):
        out.append(f"{i}. **{d.get('summary', '(no summary)')}**")
        if d.get("why_blocking"):
            out.append(f"   - Why it blocks: {d['why_blocking']}")
        if d.get("evidence"):
            out.append(f"   - Evidence: {d['evidence']}")
    for leg in verdict.get("dod_legs") or []:
        if str(leg.get("met", "")).lower() == "no":
            out.append(f"- Leg `{leg.get('leg')}` not met: {leg.get('note', '')}")
    return "\n".join(out)


def verifier_text(det: DeterministicLeg) -> str:
    """Only what an ordinary worker sees: the output of its own checks."""
    lines: list[str] = []
    for check in det.checks:
        state = "passed" if check.passed else "FAILED"
        lines.append(f"### `{check.id}` — {state} ({check.detail})")
        tail = (check.output_tail or "").strip()
        if tail and not check.passed:
            lines += ["", "```", tail[-4000:], "```", ""]
    for violation in det.stop_line_violations:
        lines.append(f"- Stop-line crossed: {violation}")
    for missing in det.missing_artefacts:
        lines.append(f"- Required artefact missing: {missing}")
    for untouched in det.untouched_required_changes:
        lines.append(f"- The order names this deliverable and it was never touched: {untouched}")
    return "\n".join(lines) or "_Every deterministic check passed._"


def build_brief(*, title: str, order_path: str, det: DeterministicLeg, review: ReviewLeg) -> str:
    return REMEDIATION_BRIEF.format(
        title=title,
        order_path=order_path,
        findings=findings_text(review),
        verifier=verifier_text(det),
    )


def should_remediate(first_passed: bool, review: ReviewLeg) -> tuple[bool, str]:
    """One repair opportunity, and only when there is something correctable to hand over.

    A review that never ran — a failed cold-reader probe, a reviewer that timed out — is a
    broken leg, not a finding. Handing a worker no findings and calling the result a repaired
    artefact would launder an instrument failure into a model result.
    """
    if first_passed:
        return False, "first pass was green; no repair opportunity was used"
    if not review.ran:
        return False, f"no remediation: the review leg did not run ({review.reason})"
    return True, "first pass was red and the review produced findings to work from"


def write_brief(cell_root: str | Path, brief: str) -> Path:
    path = Path(cell_root) / "REWORK.md"
    path.write_text(brief)
    return path


def spend_by_leg(
    *,
    attempt: dict[str, Any],
    remediation: dict[str, Any] | None,
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derived list-price spend and consumption, per leg, from what each harness reported.

    Reported, never estimated: a leg whose harness said nothing contributes `null`, not zero,
    because a silent zero would understate the pipeline's cost exactly where it matters.
    """

    def one(u: dict[str, Any] | None) -> dict[str, Any]:
        if not u or not u.get("available"):
            return {"available": False, "missing_surface": (u or {}).get("missing_surface")}
        reported = u.get("reported") or u.get("usage") or {}
        return {
            "available": True,
            "usd": u.get("total_cost_usd"),
            "output_tokens": reported.get("output_tokens"),
            "input_tokens": reported.get("input_tokens"),
            "cache_read_input_tokens": reported.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": reported.get("cache_creation_input_tokens"),
        }

    attempt_leg = one(attempt)
    remediation_leg = one(remediation) if remediation else None
    review_legs = [one(r) for r in reviews]
    known = [
        leg["usd"]
        for leg in [attempt_leg, remediation_leg, *review_legs]
        if leg and leg.get("available") and leg.get("usd") is not None
    ]
    legs: dict[str, Any] = {
        "candidate_attempt": attempt_leg,
        "candidate_remediation": remediation_leg,
        "review": review_legs,
    }
    legs["total_usd_reported"] = round(sum(known), 4) if known else None
    legs["note"] = (
        "Per-leg list-price spend as each harness reported it. A leg reporting nothing is "
        "null, never zero. The generation/review split is what the all-in economics "
        "comparison needs; it is not computed here."
    )
    return legs


def summary_line(cycle: CycleRecord) -> str:
    """One sentence separating first-shot generation quality from pipeline quality."""
    if not cycle.remediated:
        state = "green first shot" if cycle.first_passed else "red, and not remediable"
        return f"{state} ({cycle.remediation_reason})"
    final_green = bool(
        cycle.final_deterministic
        and cycle.final_deterministic.passed
        and cycle.final_review
        and cycle.final_review.passed
    )
    return (
        "red on the first shot, "
        + ("green after one repair cycle" if final_green else "still red after its one repair")
    )


def load_cycle(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
