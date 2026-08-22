"""The second instrument: five frozen historical states, and what a reviewer must find in them.

`taskbench` asks whether a model can CLOSE a written order. This asks whether a model can
REVIEW one — because if a middle tier can review, the strong-review cost floor moves, and
that is a separate question with a separate answer.

Why historical states rather than patches this study generates: grading reviewers on
generated work would either cross every reviewer with every worker cell, or pick five cases
after seeing how good they turned out. A frozen set of five real states — one accepted and
clean, four caught by a real review — is cheaper and does not let the selection be made
after the fact.

The shape, and the reason for each piece:

  ``packets/<id>.yaml``  what the reviewer is given: the launch-visible order, the diff as
                         it stood at that moment, whole artefacts the reviewer must read,
                         the source set it needs to check claims against, and the output of
                         checks run live against that state. Operator-visible.
  ``gold/<id>.yaml``     GRADER-ONLY. The expected disposition, the defects a reviewer must
                         find, the findings that are legitimate but score nothing, and — for
                         every must-find fact — a witness at both ends: a check that fails at
                         the packet's state and passes at the accepted outcome.
  ``brief.md``           the instructions and verdict schema every reviewer receives.

Three rules this module enforces rather than trusts:

* **a packet never contains its own answer.** No gold file, no historical review, no repair
  diff, no expected disposition, and nothing from the repository after the packet's head.
  `build_packet` resolves what it writes from the manifest and scans the result.
* **a must-find fact is evidenced, not asserted.** A historical review statement that the
  order, the diff and the repair do not support is not gold. Every must-find carries a
  witness pair, and `validate_review_corpus` refuses one that does not.
* **matching a reported finding to a must-find is mechanical.** A finding counts when it
  cites a file the defect lives in AND matches one of the defect's identifying patterns.
  A reviewer that describes a real defect in its own words scores; one that gestures at the
  right file scores nothing.

This is benchmark-local scoring. It adds no live review event and no rejection vocabulary,
and nothing here may be read as a hive review outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from omegahive.port.keys import canonical_payload

Disposition = Literal["required_change", "no_required_change"]
#: `approach` is not a bigger `high`: it is a defect about who the work is FOR or how it is
#: framed, which no line-level reading finds. The rejected writing attempt is entirely that.
Severity = Literal["critical", "high", "medium", "approach"]


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


class GitState(BaseModel):
    """The two commits that bracket exactly what the reviewer is shown."""

    model_config = {"extra": "forbid"}

    repo: str
    #: The commit the branch diverged from — NOT main at merge time. A packet must show the
    #: work as it stood, not the work plus whatever main did meanwhile.
    base_sha: str
    #: The state under review.
    head_sha: str
    local_path: str | None = None

    @field_validator("base_sha", "head_sha")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if len(v) != 40 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"expected a full 40-hex sha, got {v!r}")
        return v


class SourceInput(BaseModel):
    """One document the reviewer receives so it can check a claim at its source.

    The reason this list is long rather than short: an audit bounded by a hand-picked set
    accused a truthful writer of inventing four things it had simply not been shown
    (2026-08-01). A reviewer that cannot follow a citation must be able to say so, and to be
    right when it does.
    """

    model_config = {"extra": "forbid"}

    path: str
    sha: str
    #: `order` is the launch-visible order; `source` is something the work cites.
    role: Literal["order", "source"] = "source"

    @field_validator("sha")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if len(v) != 40 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"expected a full 40-hex sha, got {v!r}")
        return v


class PacketCheck(BaseModel):
    """A check run against the packet's own state, whose output goes INTO the packet.

    This is the work's declared verification, reconstructed rather than transcribed: it is
    what a reviewer would have been handed. It is deliberately not the check that exposes
    the defect — a green suite over a real defect is the situation being reproduced.
    """

    model_config = {"extra": "forbid"}

    id: str
    argv: list[str]
    cwd: str = "code"
    timeout_s: int = 1800
    description: str = ""


class ReviewPacket(BaseModel):
    """One frozen historical state and everything a reviewer is given about it."""

    model_config = {"extra": "forbid"}

    id: str
    project: str
    title: str
    #: What the reviewer is told about WHEN this state is: enough to review it, and nothing
    #: about what happened next.
    state_note: str
    code: GitState
    workspace_repo: str
    inputs: list[SourceInput]
    #: Files at `head_sha` the reviewer must read whole rather than as a diff hunk — a
    #: rendered document, a generated schema. A diff of a rewritten document is unreadable.
    whole_artefacts: list[str] = Field(default_factory=list)
    checks: list[PacketCheck] = Field(default_factory=list)
    #: What this review is FOR, in the reviewer's own terms. Never names a defect.
    review_focus: str

    @model_validator(mode="after")
    def _one_order(self) -> ReviewPacket:
        orders = [i for i in self.inputs if i.role == "order"]
        if len(orders) != 1:
            raise ValueError(f"{self.id}: exactly one input must have role=order")
        return self

    @property
    def order_input(self) -> SourceInput:
        return next(i for i in self.inputs if i.role == "order")


class Witness(BaseModel):
    """Proof that a must-find fact is a real defect and not a reviewer's opinion.

    `bad_end` is a check that must FAIL at the packet's state; `accepted_end` is the same
    check passing at the accepted outcome. A must-find with no witness pair is a claim about
    history, and the order is explicit that a historical review statement the order, the diff
    and the repair do not support is not gold.
    """

    model_config = {"extra": "forbid"}

    #: Run in a tree at the packet head (bad end) and at `accepted_sha` (accepted end).
    argv: list[str]
    cwd: str = "code"
    bad_end_exit: int = 1
    accepted_end_exit: int = 0
    timeout_s: int = 1800
    #: For a defect no script can decide — an approach-level one — the witness is the change
    #: itself: paths whose content between the two states is the repair. Stated, not run.
    documentary: str = ""

    @model_validator(mode="after")
    def _executable_or_documentary(self) -> Witness:
        if not self.argv and not self.documentary.strip():
            raise ValueError("a witness needs either an argv to run or a documentary basis")
        return self


class MustFind(BaseModel):
    """A defect the reviewer has to name for the packet to count as reviewed."""

    model_config = {"extra": "forbid"}

    id: str
    severity: Severity
    #: What a correct finding says, in outcome terms. Never quoted to the reviewer.
    summary: str
    #: A reported finding matches when it cites one of these files AND matches one of the
    #: patterns. Both, deliberately: gesturing at the right file is not finding the defect,
    #: and naming the mechanism against the wrong file is not either.
    files: list[str]
    patterns: list[str]
    witness: Witness
    #: Where the gold comes from. `contemporaneous-review` alone is never enough.
    basis: list[Literal["order", "diff", "repair", "contemporaneous-review", "strong-audit"]]

    @model_validator(mode="after")
    def _basis_is_more_than_hearsay(self) -> MustFind:
        if not self.files or not self.patterns:
            raise ValueError(f"{self.id}: a must-find needs both files and patterns to match on")
        if set(self.basis) <= {"contemporaneous-review"}:
            raise ValueError(
                f"{self.id}: the only basis is the contemporaneous review. A review statement "
                "the order, the diff or the repair does not support is not gold."
            )
        return self

    def matches(self, finding: dict) -> bool:
        """Does this reported finding name this defect?"""
        blob = " ".join(
            str(finding.get(k, "")) for k in ("summary", "why_blocking", "evidence", "detail")
        )
        if not any(f.lower() in blob.lower() for f in self.files):
            return False
        return any(re.search(p, blob, re.I) for p in self.patterns)


class OptionalFinding(BaseModel):
    """A finding that is legitimate here and scores nothing either way.

    Two kinds live here: something the contemporaneous review raised that the repair did not
    act on, and something a fresh reviewer may reasonably see that the historical one missed.
    The order is explicit that a valid alternative finding may score even where the historical
    reviewer missed it, so these are never counted against a reviewer — including on the
    clean packet, where anything NOT listed here is an unsupported claim.
    """

    model_config = {"extra": "forbid"}

    id: str
    summary: str
    files: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)

    def matches(self, finding: dict) -> bool:
        blob = " ".join(
            str(finding.get(k, "")) for k in ("summary", "why_blocking", "evidence", "detail")
        )
        if self.files and not any(f.lower() in blob.lower() for f in self.files):
            return False
        if not self.patterns:
            return bool(self.files)
        return any(re.search(p, blob, re.I) for p in self.patterns)


class PacketGold(BaseModel):
    """GRADER-ONLY hidden truth for one packet. Never enters a packet directory."""

    model_config = {"extra": "forbid"}

    packet_id: str
    expected_disposition: Disposition
    #: The state the repair reached. Used only to run the accepted-end witness.
    accepted_sha: str
    must_find: list[MustFind] = Field(default_factory=list)
    acceptable_optional: list[OptionalFinding] = Field(default_factory=list)
    #: How this gold was arrived at, in the order's four terms.
    adjudication: str
    source_refs: list[str] = Field(default_factory=list)

    @field_validator("accepted_sha")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if len(v) != 40 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"expected a full 40-hex sha, got {v!r}")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> PacketGold:
        if self.expected_disposition == "no_required_change" and self.must_find:
            raise ValueError(
                f"{self.packet_id}: a packet expected to need no change cannot have must-find "
                "defects"
            )
        if self.expected_disposition == "required_change" and not self.must_find:
            raise ValueError(
                f"{self.packet_id}: a packet expected to need a change must say what has to "
                "be found, or the reviewer is being graded on a guess"
            )
        ids = [m.id for m in self.must_find]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.packet_id}: duplicate must-find ids")
        return self

    @property
    def blocking(self) -> list[MustFind]:
        """The defects that decide fidelity: critical and approach-level."""
        return [m for m in self.must_find if m.severity in ("critical", "approach")]


class ReviewCatalog(BaseModel):
    model_config = {"extra": "forbid"}

    corpus_version: str
    frozen_on: str
    description: str
    packets: list[str]

    @model_validator(mode="after")
    def _nonempty(self) -> ReviewCatalog:
        if not self.packets:
            raise ValueError("a reviewer corpus with no packets grades nothing")
        if len(set(self.packets)) != len(self.packets):
            raise ValueError("duplicate packet ids")
        return self


class LoadedReviewCorpus(BaseModel):
    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    root: Path
    catalog: ReviewCatalog
    packets: dict[str, ReviewPacket]
    content_hash: str
    file_hashes: dict[str, str]

    def gold(self, packet_id: str) -> PacketGold:
        data = yaml.safe_load((self.root / "gold" / f"{packet_id}.yaml").read_text())
        return PacketGold.model_validate(data)

    def brief(self) -> str:
        return (self.root / "brief.md").read_text()


def _hash_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "HASHES":
            out[str(p.relative_to(root))] = sha256_bytes(p.read_bytes())
    return out


def load_review_corpus(root: str | Path) -> LoadedReviewCorpus:
    """Load and cross-validate a reviewer corpus directory."""
    root = Path(root).resolve()
    catalog = ReviewCatalog.model_validate(yaml.safe_load((root / "corpus.yaml").read_text()))

    packets: dict[str, ReviewPacket] = {}
    for path in sorted((root / "packets").glob("*.yaml")):
        p = ReviewPacket.model_validate(yaml.safe_load(path.read_text()))
        if p.id != path.stem:
            raise ValueError(f"{path}: packet id {p.id!r} does not match filename")
        packets[p.id] = p

    listed, present = set(catalog.packets), set(packets)
    if listed != present:
        raise ValueError(
            f"catalog/packet mismatch — only in catalog: {sorted(listed - present)}; "
            f"only on disk: {sorted(present - listed)}"
        )
    if not (root / "brief.md").is_file():
        raise ValueError("the reviewer corpus has no brief.md")

    for pid in sorted(packets):
        gold_path = root / "gold" / f"{pid}.yaml"
        if not gold_path.is_file():
            raise ValueError(f"{pid}: no gold file")
        gold = PacketGold.model_validate(yaml.safe_load(gold_path.read_text()))
        if gold.packet_id != pid:
            raise ValueError(f"{gold_path}: packet_id {gold.packet_id!r} does not match {pid!r}")

    file_hashes = _hash_tree(root)
    content_hash = sha256_bytes(
        canonical_payload({"corpus": catalog.corpus_version, "files": file_hashes}).encode()
    )
    return LoadedReviewCorpus(
        root=root, catalog=catalog, packets=packets,
        content_hash=content_hash, file_hashes=file_hashes,
    )
