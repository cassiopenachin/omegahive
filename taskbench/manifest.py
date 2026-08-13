"""Task manifests and the corpus catalog — the frozen description of corpus v0.

A **manifest** is everything needed to rebuild one historical task's starting world and to
grade an attempt at it, with the eventual solution deliberately absent. A **catalog** marks
which manifests are held-in (qualification) and which are held-out (regression only, never
executed, never used to tune anything).

Three files exist per task and they are kept apart on purpose:

  ``tasks/<id>.yaml``     the manifest — pins, labels, verifiers. Operator-visible.
  ``rubrics/<id>.md``     the sanitized grading rubric. The ONLY corpus file the blinded
                          reviewer ever sees. Must not name the historical solution.
  ``grading/<id>.yaml``   grader-only acceptance facts derived from the closed review.
                          Never enters a candidate root and never enters a review packet.

Everything is content-hashed together (`corpus_content_hash`). A record pins that hash, so
editing a manifest after a cell has run is detectable and, per the order's stop-line,
requires a corpus-version increment that invalidates every earlier cell.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from omegahive.port.keys import canonical_payload

# --- vocabularies -------------------------------------------------------------

#: HIP-1's three order classes. Only `bounded` may be seeded into this corpus.
TaskClass = Literal["bounded", "standard", "judgment"]

#: Coarse shape of the work, so a later report can expose the corpus's composition
#: instead of flattening eight heterogeneous tasks into one pass rate.
WorkShape = Literal[
    "shell-tooling",
    "python-service",
    "docs-reorg",
    "external-verification",
]

#: What a candidate is expected to leave behind. Used by the artefact-existence leg.
OutputKind = Literal["code_patch", "tests", "doc_update", "verdict_doc", "repro_script"]

Applicability = Literal["offline", "operator"]


class GitPin(BaseModel):
    """A repository plus the exact commit a replay starts from."""

    model_config = {"extra": "forbid"}

    repo: str
    #: The commit the historical branch was cut from — the candidate's single baseline.
    pre_task_base_sha: str
    #: Local path hint for the materializer's object source. Deployment fact, overridable.
    local_path: str | None = None

    @field_validator("pre_task_base_sha")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if len(v) != 40 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"pre_task_base_sha must be a full 40-hex sha, got {v!r}")
        return v


class WorkspaceInput(BaseModel):
    """One workspace file the candidate is given, pinned at its launch-era revision.

    The candidate never receives a workspace clone: it receives exactly these paths at
    exactly these shas. That is what keeps later workspace state — including this corpus's
    own held-out manifests — out of the candidate root.
    """

    model_config = {"extra": "forbid"}

    path: str
    sha: str
    #: `order` is the task's own order file; `context` is a doc the order cites.
    role: Literal["order", "context"] = "context"

    @field_validator("sha")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        if len(v) != 40 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"workspace input sha must be a full 40-hex sha, got {v!r}")
        return v


class WithheldInput(BaseModel):
    """A document the historical worker could open and this replay's candidate cannot.

    Declared, never silent: a citation the candidate cannot follow is a real difference from
    the historical launch, and a reader comparing outcomes has to know about it.
    """

    model_config = {"extra": "forbid"}

    path: str
    reason: str


class DependencySnapshot(BaseModel):
    """An out-of-repo dependency the task needs, pinned and exportable offline.

    `kind=git_bundle` is exported from a local clone into the candidate root; `kind=image`
    is asserted present in local container storage (never pulled by the runner);
    `kind=volume` names a warm cache the operator must have.
    """

    model_config = {"extra": "forbid"}

    name: str
    kind: Literal["git_bundle", "image", "volume"]
    ref: str
    sha: str | None = None
    local_path: str | None = None
    note: str | None = None


class Verifier(BaseModel):
    """One deterministic check, invoked as argv — never a shell-evaluated string.

    `applicable=operator` marks a leg that only a human with live infrastructure can run.
    Those legs are recorded, reported, and excluded from the cell verdict; see
    `TaskManifest.non_replayable_legs`.
    """

    model_config = {"extra": "forbid"}

    id: str
    argv: list[str]
    #: Relative to the cell root. `code` is the materialized single-baseline repo.
    cwd: str = "code"
    expect_exit: int = 0
    applicable: Applicability = "offline"
    timeout_s: int = 1800
    #: Substrings that must appear in combined output for the check to pass.
    expect_output_contains: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("argv")
    @classmethod
    def _argv_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("verifier argv must be non-empty")
        return v


class ChecklistItem(BaseModel):
    """A DoD leg that is judged, not executed. Enters the reviewer's rubric verbatim."""

    model_config = {"extra": "forbid"}

    id: str
    text: str
    applicable: Applicability = "offline"


class NonReplayableLeg(BaseModel):
    """A DoD leg an offline instrument structurally cannot execute.

    Declared here so the exclusion is a frozen, hashed corpus fact rather than a decision
    someone makes after seeing a red cell. `executed_by` says who really owns it.
    """

    model_config = {"extra": "forbid"}

    leg: str
    reason: str
    executed_by: Literal["operator", "not-executed"]


class StopLine(BaseModel):
    """A stop-line the candidate must not cross, checked mechanically where possible."""

    model_config = {"extra": "forbid"}

    id: str
    text: str
    #: Globs, relative to the candidate's code repo, matched against the files the
    #: candidate CHANGED — never against the files that merely exist at the baseline.
    forbidden_paths: list[str] = Field(default_factory=list)


class TaskManifest(BaseModel):
    """One replayable task. Frozen at corpus-freeze time; hashed into the record."""

    model_config = {"extra": "forbid"}

    id: str
    project: str
    run: str
    title: str

    task_class: TaskClass
    work_shape: WorkShape
    #: Why this task is genuinely bounded — the HIP's predicate, argued, not asserted.
    bounded_because: str

    workspace_repo: str
    workspace_inputs: list[WorkspaceInput]
    #: Documents the historical launch had but this replay withholds, with the reason. The
    #: only legitimate reason is that the document would spend the held-out reservation.
    withheld_inputs: list[WithheldInput] = Field(default_factory=list)
    code: GitPin
    dependency_snapshots: list[DependencySnapshot] = Field(default_factory=list)

    expected_output_kinds: list[OutputKind]
    #: Globs that must EXIST in the candidate's tree when it finishes.
    required_artefacts: list[str] = Field(default_factory=list)
    #: Globs the candidate's diff must TOUCH. Existence is not evidence of work when the
    #: file is already in the baseline: without this, a shell-tooling cell whose drill is an
    #: operator leg passes its whole deterministic leg having done nothing at all. Every
    #: entry must be a path the ORDER names, never one read off the historical patch.
    required_changes: list[str] = Field(default_factory=list)

    verifiers: list[Verifier] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    stop_lines: list[StopLine] = Field(default_factory=list)
    non_replayable_legs: list[NonReplayableLeg] = Field(default_factory=list)

    #: Everything the environment must provide for an offline replay to be honest.
    environment_needs: list[str] = Field(default_factory=list)

    rubric: str
    grading: str

    @model_validator(mode="after")
    def _coherent(self) -> TaskManifest:
        if self.task_class != "bounded":
            raise ValueError(f"{self.id}: corpus v0 seeds bounded-class tasks only")
        orders = [w for w in self.workspace_inputs if w.role == "order"]
        if len(orders) != 1:
            raise ValueError(f"{self.id}: exactly one workspace input must have role=order")
        if not self.verifiers and not self.checklist:
            raise ValueError(f"{self.id}: a task needs a finish-time verifier or a checklist")
        ids = [v.id for v in self.verifiers]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.id}: duplicate verifier ids")
        return self

    @property
    def order_input(self) -> WorkspaceInput:
        return next(w for w in self.workspace_inputs if w.role == "order")

    def offline_verifiers(self) -> list[Verifier]:
        return [v for v in self.verifiers if v.applicable == "offline"]


class AcceptanceFacts(BaseModel):
    """Grader-only facts derived from the closed review.

    These describe *what the closed outcome established*, never *how the historical patch
    was written* — the order forbids exact-diff scoring, and a fact that quotes the patch
    would smuggle it back in. They never enter a candidate root or a review packet; the
    reviewer sees only the sanitized rubric.
    """

    model_config = {"extra": "forbid"}

    task_id: str
    #: The outcome the order actually bought, in outcome terms.
    accepted_outcome: list[str]
    #: Defects the closed review caught — a candidate repeating one is a red flag worth
    #: naming in the rubric in *behavioural* terms, not as a patch hint.
    known_defect_classes: list[str] = Field(default_factory=list)
    #: Where the facts come from, so an audit can check them.
    source_refs: list[str]
    #: The historical merge commit, used ONLY to write the operator-only read-denial canary
    #: and to give the operator a diagnosis aid. Never an expected output: exact-diff
    #: scoring is a stop-line and no code path compares a candidate patch against it.
    historical_solution_sha: str | None = None
    notes: str = ""


class CorpusCatalog(BaseModel):
    """The frozen corpus: which tasks qualify models, which are reserved."""

    model_config = {"extra": "forbid"}

    corpus_version: str
    frozen_on: str
    description: str
    held_in: list[str]
    held_out: list[str]

    @model_validator(mode="after")
    def _disjoint(self) -> CorpusCatalog:
        overlap = set(self.held_in) & set(self.held_out)
        if overlap:
            raise ValueError(f"held-in and held-out overlap: {sorted(overlap)}")
        if not self.held_in:
            raise ValueError("catalog has no held-in tasks")
        return self

    @property
    def all_ids(self) -> list[str]:
        return list(self.held_in) + list(self.held_out)


class HeldOutRefused(Exception):
    """Raised when a qualification path is pointed at a reserved task.

    The held-out set exists to detect regressions from changes to worker instructions,
    order templates, or this benchmark. A task once used to qualify or tune can never be
    held out again, so the refusal is a hard error, not a warning.
    """


# --- loading -------------------------------------------------------------------


class LoadedCorpus(BaseModel):
    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    root: Path
    catalog: CorpusCatalog
    manifests: dict[str, TaskManifest]
    content_hash: str
    file_hashes: dict[str, str]

    def require_held_in(self, task_id: str) -> TaskManifest:
        """Resolve a task for a qualification path, refusing the reserved set."""
        if task_id in self.catalog.held_out:
            raise HeldOutRefused(
                f"{task_id} is held out of corpus {self.catalog.corpus_version}: it may not be "
                "executed, entered into a qualification launch packet, used for canary "
                "selection, or used to tune prompts or rubrics."
            )
        if task_id not in self.manifests:
            raise KeyError(f"unknown task {task_id!r} in corpus {self.catalog.corpus_version}")
        return self.manifests[task_id]

    def acceptance_facts(self, task_id: str) -> AcceptanceFacts:
        m = self.manifests[task_id]
        data = yaml.safe_load((self.root / m.grading).read_text())
        return AcceptanceFacts.model_validate(data)

    def rubric_text(self, task_id: str) -> str:
        return (self.root / self.manifests[task_id].rubric).read_text()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    """Hash every corpus file, path-relative and sorted — the freeze fingerprint."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "HASHES":
            out[str(p.relative_to(root))] = sha256_bytes(p.read_bytes())
    return out


def load_corpus(root: str | Path) -> LoadedCorpus:
    """Load and cross-validate a corpus directory.

    Enforces the invariants a hand-edited YAML tree cannot be trusted to hold: every
    catalog id has a manifest and vice versa, every manifest's rubric and grading file
    exists and parses, and no rubric leaks the grader-only file's contents.
    """
    # Absolute from here on: verifier argv carries `{corpus}` and runs with the CELL as its
    # working directory, so a relative corpus path would expand to a path relative to the
    # wrong tree and every such check would fail as "could not execute".
    root = Path(root).resolve()
    catalog = CorpusCatalog.model_validate(yaml.safe_load((root / "corpus.yaml").read_text()))

    manifests: dict[str, TaskManifest] = {}
    for path in sorted((root / "tasks").glob("*.yaml")):
        m = TaskManifest.model_validate(yaml.safe_load(path.read_text()))
        if m.id != path.stem:
            raise ValueError(f"{path}: manifest id {m.id!r} does not match filename")
        manifests[m.id] = m

    listed, present = set(catalog.all_ids), set(manifests)
    if listed != present:
        raise ValueError(
            f"catalog/manifest mismatch — only in catalog: {sorted(listed - present)}; "
            f"only on disk: {sorted(present - listed)}"
        )

    # The reservation is spent the moment a held-out task's order or result report rides
    # into a qualification launch packet as another task's context. Refuse it structurally
    # rather than trusting an author to notice.
    for task_id in catalog.held_in:
        for item in manifests[task_id].workspace_inputs:
            for reserved in catalog.held_out:
                if reserved in item.path:
                    raise ValueError(
                        f"{task_id}: workspace input {item.path} names held-out task "
                        f"{reserved!r}. Shipping it would spend the reservation; withhold it "
                        "and record the withholding in `withheld_inputs`."
                    )

    for task_id, m in manifests.items():
        for ref in (m.rubric, m.grading):
            if not (root / ref).is_file():
                raise ValueError(f"{task_id}: missing {ref}")
        facts = AcceptanceFacts.model_validate(yaml.safe_load((root / m.grading).read_text()))
        if facts.task_id != task_id:
            raise ValueError(f"{m.grading}: task_id {facts.task_id!r} does not match {task_id!r}")

    file_hashes = _hash_tree(root)
    content_hash = sha256_bytes(
        canonical_payload({"corpus": catalog.corpus_version, "files": file_hashes}).encode()
    )
    return LoadedCorpus(
        root=root,
        catalog=catalog,
        manifests=manifests,
        content_hash=content_hash,
        file_hashes=file_hashes,
    )
