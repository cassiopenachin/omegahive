"""Score one reviewer verdict against hidden gold, and decide the instrument's fidelity.

Two things are scored, and they are kept apart because they fail for different reasons:

* **disposition** — did the reviewer answer the question? A packet that needs a change and
  is passed is a false negative; a clean packet that is failed is a false positive. Both
  are one bit and both are reported as one bit.
* **must-find coverage** — did it name the defects that decide whether this work ships? A
  reviewer that returns `required_change` for a reason that is not the reason has answered
  correctly by accident, and the two numbers separate that from real detection.

What never counts against a reviewer: an extra finding. The order is explicit that a valid
alternative finding may score even where the historical reviewer missed it, so extra
findings are reported and not penalised — with exactly one exception, the clean packet,
where an unsupported finding IS the failure mode being measured. There the bar is
`acceptable_optional`: a finding that matches one of those is a fair reading of real code,
and anything else on a packet that shipped unchanged is a false positive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .reviewbench import PacketGold


@dataclass
class MustFindResult:
    id: str
    severity: str
    found: bool
    matched_finding: str = ""


@dataclass
class PacketScore:
    packet_id: str
    blind_id: str
    expected_disposition: str
    reported_disposition: str
    disposition_correct: bool
    must_find: list[MustFindResult] = field(default_factory=list)
    #: Findings that matched neither a must-find nor an acceptable-optional entry.
    unmatched_findings: list[dict] = field(default_factory=list)
    #: On a clean packet only: unmatched findings at high or critical severity.
    unsupported_high_severity: list[dict] = field(default_factory=list)
    #: The cell said nothing usable. Never scored as a reviewer opinion.
    inconclusive: bool = False
    because: str = ""

    @property
    def blocking_found(self) -> bool:
        return all(m.found for m in self.must_find if m.severity in ("critical", "approach"))

    def to_json(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items()},
            "blocking_found": self.blocking_found,
        }


def score_packet(
    gold: PacketGold, verdict: dict[str, Any] | None, *, blind_id: str = ""
) -> PacketScore:
    """Turn one reviewer's JSON into a score. An absent or malformed verdict is inconclusive."""
    if verdict is None:
        return PacketScore(
            packet_id=gold.packet_id, blind_id=blind_id,
            expected_disposition=gold.expected_disposition, reported_disposition="",
            disposition_correct=False, inconclusive=True,
            because="the reviewer wrote no verdict; this cell is not a reviewer result",
        )

    reported = str(verdict.get("disposition", "")).strip().lower()
    findings = verdict.get("findings")
    if not isinstance(findings, list):
        return PacketScore(
            packet_id=gold.packet_id, blind_id=blind_id,
            expected_disposition=gold.expected_disposition, reported_disposition=reported,
            disposition_correct=False, inconclusive=True,
            because="`findings` is not a list, so nothing in this verdict can be read",
        )
    if reported not in ("required_change", "no_required_change"):
        return PacketScore(
            packet_id=gold.packet_id, blind_id=blind_id,
            expected_disposition=gold.expected_disposition, reported_disposition=reported,
            disposition_correct=False, inconclusive=True,
            because=f"disposition {reported!r} is neither of the two answers the brief allows",
        )
    if (reported == "no_required_change") != (len(findings) == 0):
        return PacketScore(
            packet_id=gold.packet_id, blind_id=blind_id,
            expected_disposition=gold.expected_disposition, reported_disposition=reported,
            disposition_correct=False, inconclusive=True,
            because=(
                f"the reviewer said {reported} with {len(findings)} finding(s) — "
                "self-inconsistent, so neither half can be taken at face value"
            ),
        )

    results: list[MustFindResult] = []
    claimed: set[int] = set()
    for m in gold.must_find:
        hit = next(
            ((i, f) for i, f in enumerate(findings) if isinstance(f, dict) and m.matches(f)),
            None,
        )
        if hit is None:
            results.append(MustFindResult(m.id, m.severity, False))
        else:
            i, f = hit
            claimed.add(i)
            results.append(
                MustFindResult(m.id, m.severity, True, str(f.get("summary", ""))[:200])
            )

    unmatched: list[dict] = []
    for i, f in enumerate(findings):
        if i in claimed or not isinstance(f, dict):
            continue
        if any(o.matches(f) for o in gold.acceptable_optional):
            continue
        unmatched.append(f)

    unsupported: list[dict] = []
    if gold.expected_disposition == "no_required_change":
        unsupported = [
            f for f in unmatched
            if str(f.get("severity", "")).strip().lower() in ("critical", "high", "approach")
        ]

    correct = reported == gold.expected_disposition
    missed = [m.id for m in results if not m.found]
    reasons = []
    if not correct:
        reasons.append(f"disposition {reported} where the gold says {gold.expected_disposition}")
    if missed:
        reasons.append(f"missed: {', '.join(missed)}")
    if unsupported:
        reasons.append(f"{len(unsupported)} unsupported high-severity finding(s) on a clean packet")
    return PacketScore(
        packet_id=gold.packet_id,
        blind_id=blind_id,
        expected_disposition=gold.expected_disposition,
        reported_disposition=reported,
        disposition_correct=correct,
        must_find=results,
        unmatched_findings=unmatched,
        unsupported_high_severity=unsupported,
        because="; ".join(reasons) if reasons else "disposition correct, every must-find named",
    )


@dataclass
class ReviewerFidelity:
    """The instrument-level verdict, by the rule the order fixes before any cell runs."""

    green: bool
    blocking_all_found: bool
    dispositions_correct: int
    dispositions_scored: int
    clean_packet_unsupported: int
    inconclusive: list[str] = field(default_factory=list)
    because: str = ""

    def to_json(self) -> dict:
        return asdict(self)


#: The order's rule, written once, here, so no later reading of a red can soften it.
MIN_DISPOSITIONS_CORRECT = 4


def reviewer_fidelity(scores: list[PacketScore]) -> ReviewerFidelity:
    """Green only when every blocking defect was found, at least four of five dispositions
    are right, and the clean packet drew no unsupported high-severity finding."""
    inconclusive = [s.packet_id for s in scores if s.inconclusive]
    scored = [s for s in scores if not s.inconclusive]
    blocking_all = all(s.blocking_found for s in scored)
    correct = sum(1 for s in scored if s.disposition_correct)
    unsupported = sum(len(s.unsupported_high_severity) for s in scored)

    reasons: list[str] = []
    if inconclusive:
        reasons.append(
            f"{len(inconclusive)} cell(s) produced no usable verdict ({', '.join(inconclusive)}); "
            "an instrument that cannot read its own cells is not green"
        )
    if not blocking_all:
        missed = [
            f"{s.packet_id}/{m.id}"
            for s in scored for m in s.must_find
            if not m.found and m.severity in ("critical", "approach")
        ]
        reasons.append(f"blocking defects not found: {', '.join(missed)}")
    if correct < MIN_DISPOSITIONS_CORRECT:
        reasons.append(
            f"{correct} of {len(scored)} dispositions correct; the rule is "
            f"at least {MIN_DISPOSITIONS_CORRECT}"
        )
    if unsupported:
        reasons.append(
            f"{unsupported} unsupported high-severity finding(s) on the packet that shipped "
            "unchanged"
        )
    return ReviewerFidelity(
        green=not reasons,
        blocking_all_found=blocking_all,
        dispositions_correct=correct,
        dispositions_scored=len(scored),
        clean_packet_unsupported=unsupported,
        inconclusive=inconclusive,
        because="; ".join(reasons) if reasons else "every blocking defect found, dispositions "
        "within the rule, no unsupported finding on the clean packet",
    )
