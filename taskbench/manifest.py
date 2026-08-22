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
from typing import Literal, cast

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
    # v1 adds three shapes the middle-tier corpus needs. A shape is a reporting label,
    # never a grading input: it exists so a reader can see the composition of a corpus
    # instead of a single pass rate over heterogeneous work.
    "api-service",
    "research-design",
    "technical-writing",
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


class SnapshotFile(BaseModel):
    """One file of a pinned public source, named by the URL it came from and its digest."""

    model_config = {"extra": "forbid"}

    name: str
    url: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _hex64(cls, v: str) -> str:
        if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"sha256 must be 64 hex characters, got {v!r}")
        return v


class DependencySnapshot(BaseModel):
    """An out-of-repo dependency the task needs, pinned and exportable offline.

    `kind=git_bundle` is exported from a local clone into the candidate root; `kind=image`
    is asserted present in local container storage (never pulled by the runner);
    `kind=volume` names a warm cache the operator must have.
    """

    model_config = {"extra": "forbid"}

    name: str
    kind: Literal["git_bundle", "image", "volume", "source_snapshot"]
    ref: str
    sha: str | None = None
    local_path: str | None = None
    note: str | None = None
    #: For `kind=source_snapshot`: the launch-era files, each pinned by sha256. The
    #: operator fetches them once into a local cache; the runner NEVER fetches, and a
    #: hash mismatch is a refusal, not a warning. A task that depended on a public
    #: dataset or paper is measured against THIS packet, not against a fresh web
    #: search, and the manifest says so where a reader of the result will see it.
    files: list[SnapshotFile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _snapshot_has_files(self) -> DependencySnapshot:
        if self.kind == "source_snapshot" and not self.files:
            raise ValueError(f"{self.name}: a source_snapshot must pin at least one file")
        if self.kind != "source_snapshot" and self.files:
            raise ValueError(f"{self.name}: only a source_snapshot may pin files")
        return self


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


class MockTool(BaseModel):
    """An outward-facing tool the candidate really invokes, against a recording stub.

    Excluding an outward leg leaves it ungraded; mocking it turns the act into an artefact
    the blinded reviewer can judge. The stub is installed ahead of the real tool on the
    candidate's PATH and writes every invocation, argv and body, to the cell's mock log.

    This is an evaluation stub, not a network jail: a candidate determined to reach the
    network by another route could. The runner proves the stub is what the tool name
    resolves to and records that proof; it claims nothing more.
    """

    model_config = {"extra": "forbid"}

    #: The command name to shadow, e.g. `gh`.
    name: str
    #: Corpus-relative path of the stub script.
    script: str
    #: What the order asks for and why it is staged rather than sent.
    purpose: str


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
    #: Required for a `bounded` task and meaningless for any other, which is why v1's
    #: larger tasks argue `replayable_because` instead.
    bounded_because: str = ""
    #: Why this task can still be replayed years after it closed: the pre-task state is
    #: reconstructable, the launch-era inputs are enough to do the work, the accepted
    #: outcome is recoverable, and the finish-time checks test the ORDER rather than the
    #: historical patch. Required for every task a corpus seeds outside the bounded class,
    #: because that is exactly where "it closed once" stops implying "it can close again".
    replayable_because: str = ""

    workspace_repo: str
    workspace_inputs: list[WorkspaceInput]
    #: Documents the historical launch had but this replay withholds, with the reason. The
    #: only legitimate reason is that the document would spend the held-out reservation.
    withheld_inputs: list[WithheldInput] = Field(default_factory=list)
    #: Workspace paths (globs) the ORDER makes the candidate's to write — a runbook section,
    #: a result report. v0 exported the whole workspace read-only and captured none of it, so
    #: a candidate that did this work had it thrown away before grading and was marked down
    #: for not doing it. Anything matching enters the candidate diff and the review packet.
    writable_workspace_paths: list[str] = Field(default_factory=list)
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

    #: Outward-facing tools the candidate invokes for real, against recording stubs.
    mock_tools: list[MockTool] = Field(default_factory=list)
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
        # Which classes a corpus may seed is a CATALOG fact, checked in `load_corpus`:
        # v0/v0.1 seed bounded work only, v1 deliberately seeds larger orders. What is
        # checked here is that whichever class a manifest claims, it argues its own
        # predicate rather than asserting it.
        if self.task_class == "bounded" and not self.bounded_because.strip():
            raise ValueError(f"{self.id}: a bounded task must argue `bounded_because`")
        if self.task_class != "bounded" and not self.replayable_because.strip():
            raise ValueError(
                f"{self.id}: a {self.task_class}-class task must argue `replayable_because` "
                "— outside the bounded class, replayability is the thing that is not obvious"
            )
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


class ReservedTask(BaseModel):
    """A held-out task, pinned but deliberately not built into a gradeable manifest.

    A reservation only has to survive; it does not have to be executable today. Pinning
    the launch-visible order and both endpoints here is what lets a later corpus version
    build the manifest from the same historical state, without paying now for a grader
    nobody may run.

    `contaminated_by` is the honest half. A held-in task that launched *after* a reserved
    one necessarily carries the reserved task's accepted outcome in its own single-baseline
    export — the code is simply there, in the tree the candidate starts from. That does not
    stop the reservation working against tuning and execution, and it does stop the task
    being a clean cold read for any model that has run the contaminating cell. Declaring it
    turns a quiet weakness into a hashed corpus fact; `load_corpus` refuses a declaration
    with no note.
    """

    model_config = {"extra": "forbid"}

    id: str
    why_reserved: str
    #: `<workspace path>@<sha>` — the order as the historical launch showed it.
    order_ref: str
    code_repo: str
    pre_task_base_sha: str
    accepted_sha: str
    contaminated_by: list[str] = Field(default_factory=list)
    contamination_note: str = ""

    @model_validator(mode="after")
    def _contamination_is_explained(self) -> ReservedTask:
        if self.contaminated_by and not self.contamination_note.strip():
            raise ValueError(
                f"{self.id}: reservation is contaminated by {sorted(self.contaminated_by)} "
                "and carries no note. A partial reservation must say what it still buys."
            )
        return self


class CorpusCatalog(BaseModel):
    """The frozen corpus: which tasks qualify models, which are reserved."""

    model_config = {"extra": "forbid"}

    corpus_version: str
    frozen_on: str
    description: str
    held_in: list[str]
    held_out: list[str]
    #: Which order classes this corpus may seed. v0/v0.1 omit it and get the bounded-only
    #: default they were frozen under; v1 widens it deliberately and visibly.
    allowed_task_classes: list[TaskClass] = Field(
        default_factory=lambda: [cast(TaskClass, "bounded")]
    )
    #: Pins for the held-out set. When present it must cover the held-out set exactly, and
    #: those tasks then need no manifest — see `load_corpus`.
    reserved: list[ReservedTask] = Field(default_factory=list)

    @model_validator(mode="after")
    def _disjoint(self) -> CorpusCatalog:
        overlap = set(self.held_in) & set(self.held_out)
        if overlap:
            raise ValueError(f"held-in and held-out overlap: {sorted(overlap)}")
        if not self.held_in:
            raise ValueError("catalog has no held-in tasks")
        if not self.allowed_task_classes:
            raise ValueError("catalog allows no task class at all")
        if self.reserved:
            declared = sorted(r.id for r in self.reserved)
            if declared != sorted(self.held_out):
                raise ValueError(
                    f"reserved pins {declared} but the held-out set is {sorted(self.held_out)}"
                )
        return self

    @property
    def reserved_by_id(self) -> dict[str, ReservedTask]:
        return {r.id: r for r in self.reserved}

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

    # A held-in task must have a manifest; a RESERVED task may be carried as pins alone
    # (`catalog.reserved`), because building a grader for work nobody may run buys nothing
    # and cannot be validated at either endpoint. Everything on disk must still be listed.
    listed, present = set(catalog.all_ids), set(manifests)
    missing_held_in = sorted(set(catalog.held_in) - present)
    stray = sorted(present - listed)
    missing_held_out = sorted(set(catalog.held_out) - present - set(catalog.reserved_by_id))
    if missing_held_in or stray or missing_held_out:
        raise ValueError(
            f"catalog/manifest mismatch — held-in without a manifest: {missing_held_in}; "
            f"held-out neither manifested nor pinned under `reserved`: {missing_held_out}; "
            f"on disk but not in the catalog: {stray}"
        )

    allowed = set(catalog.allowed_task_classes)
    for task_id, m in sorted(manifests.items()):
        if m.task_class not in allowed:
            raise ValueError(
                f"{task_id}: corpus {catalog.corpus_version} seeds "
                f"{sorted(allowed)} tasks only, and this one is {m.task_class!r}"
            )

    for r in catalog.reserved:
        bad = sorted(set(r.contaminated_by) - set(catalog.held_in))
        if bad:
            raise ValueError(
                f"reserved {r.id}: contaminated_by names {bad}, which are not held-in tasks"
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
        for tool in m.mock_tools:
            if not (root / tool.script).is_file():
                raise ValueError(f"{task_id}: missing mock script {tool.script}")
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
