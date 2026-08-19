"""Harness binding descriptors: the worker permission boundary, made checkable.

`permissions.md` states four policy classes (P1 access layer, P2 secrets, P3 durable
stack, P4 raw fetch) and then leaves one question open — who *enforces* them for a
worker on a harness the hive does not control. The answer this module implements:

    the repository carries an auditable binding DESCRIPTOR; the launcher
    MATERIALIZES the harness-native configuration into the isolated worker root
    and, before the execution starts, VERIFIES that the bytes on disk and the flags
    in the argv are the ones that were approved.

Read that verification claim precisely, because the looser reading is the thing this
module exists to prevent. The launch-time check answers "is the configuration the child
will read the configuration the operator approved". Whether the harness HONOURS it is a
different question with a different mechanism — `scripts/hive-binding-probe`, at a
different time, in a different root, for money — and a probe that needs it is recorded as
`deferred`, never folded into a pass.

The descriptor is the auditable half. It maps every policy class to one or more
*enforceable native mechanisms* and to focused probes, and it refuses rather than
degrading: a blank class, a class held up by prose alone, an unknown descriptor
version, an unrecognized command mode, or a descriptor whose bytes no longer match
what the operator pinned in the catalog all stop the launch at preflight.

Three distinctions carry the design, and collapsing any of them is the defect:

  * **A mechanism is enforceable or it is not.** `instruction` is a real mechanism —
    P2's "do not print resolved configuration" genuinely binds through worker
    instructions and review, because no deny list can express it — but a class whose
    ONLY mechanism is an instruction is not bound, and saying so is the whole point.
    Such a class must name its `residual` and still carry an enforceable mechanism
    for the parts that can be enforced.

  * **Declared is not proven.** A descriptor written from a vendor's documentation
    records an intent. Until its probes have actually been run against the installed
    harness on this deployment, `status` stays `declared` and every route using it
    refuses. That is what keeps "we wrote a config" from reading as "the boundary
    holds".

  * **The catalog pins the descriptor by digest.** The descriptor travels with the
    launcher (code), but which descriptor a route runs under is an operator decision
    (deployment). A route names `binding_id` and `binding_digest`; a descriptor whose
    bytes have moved since the operator pinned them refuses, so a boundary change
    cannot ride in on a code deploy.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from omegahive.harness.records import RefusalError, _first_error, _parse_json

BINDING_DESCRIPTOR_SCHEMA_VERSION = 1

# The four classes of `permissions.md`. Coverage is checked against this exact set:
# a descriptor missing one refuses, and a descriptor inventing a fifth refuses too,
# because a policy class that exists in a binding and not in the policy is a boundary
# nobody approved.
POLICY_CLASSES: tuple[str, ...] = ("P1", "P2", "P3", "P4")

# Mechanism kinds that actually stop something. Everything not in this set is a
# statement of intent, and a class held up only by statements of intent is unbound.
ENFORCEABLE_MECHANISMS = frozenset(
    {
        "settings-deny",          # deny rules in the harness's own permission engine
        "settings-allow",         # the positive half: development tools stay usable
        "setting-source-gating",  # which config files the child is allowed to load
        "launch-flag",            # a flag on the child's own command line
        "sandbox-flag",           # an OS-level confinement mode the harness provides
        "env-allowlist",          # the child's environment is constructed, not inherited
        # An OS-level READ denial on named paths or globs, expressed in the harness's
        # own filesystem-permission table. Kept apart from `settings-deny` because the
        # two answer different questions and, on Codex, render into different files:
        # `settings-deny` gates COMMANDS before they run, this gates PATHS at the
        # syscall. A harness that has one and not the other is a real difference, and
        # collapsing them would let a command matcher stand in for a read boundary.
        "filesystem-deny",
        # The launcher hands the child a GENERATED harness-state directory by
        # environment variable, so the operator's own harness state — prior threads,
        # memory, plugins, personal configuration — is absent by construction rather
        # than disabled by a flag whose meaning can change under a version bump.
        "generated-home",
    }
)
MECHANISM_KINDS = ENFORCEABLE_MECHANISMS | {"instruction"}

# Kinds whose whole content IS their rules. A mechanism of one of these kinds with an
# empty rule list denies nothing while satisfying every structural check, so it is
# refused at parse time rather than caught later by something that does not look inside.
_RULE_BEARING_MECHANISMS = frozenset({"settings-deny", "settings-allow", "filesystem-deny"})

# The rule-bearing kinds that express a DENIAL. `check_coverage` requires each of these
# to be named by a `rule-present` probe, so that deleting the mechanism is caught by
# something rather than by nobody.
_DENY_BEARING_MECHANISMS = frozenset({"settings-deny", "filesystem-deny"})

# Probe kinds. `local` probes are deterministic and run at every preflight; `harness`
# probes need the installed harness and a model call, so they run in the probe runner
# (`scripts/hive-binding-probe`) and their result is recorded in the descriptor.
LOCAL_PROBE_KINDS = frozenset(
    {"rule-present", "argv-flag", "env-absent", "config-absent", "env-present"}
)
HARNESS_PROBE_KINDS = frozenset({"deny-enforced", "allow-executes", "source-gated"})
PROBE_KINDS = LOCAL_PROBE_KINDS | HARNESS_PROBE_KINDS

_ID_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DIGEST_SHAPE = re.compile(r"sha256:[0-9a-f]{64}")

# Names whose presence in a worker environment is a finding regardless of value.
CREDENTIAL_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")


class Mechanism(BaseModel):
    """One native control, named by kind so its enforceability is a fact not a claim."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    # For `settings-deny` / `settings-allow`: the exact rule strings that go into the
    # materialized config. For `launch-flag` / `sandbox-flag`: the argv tokens. For
    # `env-allowlist` / `instruction`: empty, and `detail` carries the meaning.
    rules: list[str] = Field(default_factory=list)
    detail: str

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in MECHANISM_KINDS:
            raise ValueError(f"unknown mechanism kind {v!r}; known: {sorted(MECHANISM_KINDS)}")
        return v

    @model_validator(mode="after")
    def _rule_bearing_kinds_carry_rules(self) -> Mechanism:
        """A rule-bearing mechanism with no rules is an empty boundary that reads as full.

        Every other check in this module tests the SHAPE of the descriptor — that a class
        names a mechanism, that the mechanism is enforceable, that a probe exists. None of
        them looks inside. Without this, a descriptor whose four `settings-deny`
        mechanisms all carry `rules: []` passes coverage, materializes
        `{"permissions": {}}`, and reports four green classes with *passing* `rule-present`
        probes — because "all zero of my rules are present" is vacuously true. The catalog
        digest pin does not help: it answers "are these the bytes the operator approved",
        not "do these bytes contain a boundary".
        """
        if self.kind in _RULE_BEARING_MECHANISMS and not self.rules:
            raise ValueError(
                f"a {self.kind!r} mechanism with no rules denies nothing while reading as "
                "a bound class; give it rules or remove it"
            )
        return self

    @property
    def enforceable(self) -> bool:
        return self.kind in ENFORCEABLE_MECHANISMS


class Probe(BaseModel):
    """One focused check with a stated expectation.

    A probe that cannot fail proves nothing, so every probe names both what it does
    and what outcome counts as bound.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    # `deny-enforced` / `allow-executes`: the shell command the child is asked to run.
    command: str | None = None
    # `rule-present`: rules that must appear in the materialized config.
    rules: list[str] = Field(default_factory=list)
    # `argv-flag`: tokens that must appear in the child's argv, in order.
    argv: list[str] = Field(default_factory=list)
    # `env-absent`: a sentinel planted in the PARENT environment which must not reach
    # the child. Credential-shaped names are always checked, sentinel or not.
    sentinel_name: str | None = None
    # `config-absent`: a path that must not exist, because its presence would override
    # the materialized boundary (an admin/managed policy file).
    # `env-present`: the suffix the named variable's value must end with, so a harness
    # whose whole boundary is reached through a generated directory can assert that the
    # child is actually pointed at THAT directory rather than at the operator's.
    path: str | None = None
    # `allow-executes` / `source-gated`: the text that PROVES the command ran. Declared
    # rather than guessed. The runner first inferred it from the command's last token,
    # which is right for an `echo <canary>` and silently wrong for `git --version`
    # (whose output says "git version", not "--version") — and the failure mode of a
    # wrong guess here is a control that cannot pass, or worse, one that passes on prose.
    expect_output: str | None = None
    expect: Literal["denied", "executed", "absent", "present"]
    note: str | None = None

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _ID_SHAPE.fullmatch(v):
            raise ValueError(f"probe id must match [A-Za-z0-9._-]{{1,64}}, got {v!r}")
        return v

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in PROBE_KINDS:
            raise ValueError(f"unknown probe kind {v!r}; known: {sorted(PROBE_KINDS)}")
        return v

    @model_validator(mode="after")
    def _kind_carries_its_own_subject(self) -> Probe:
        """A probe with no subject cannot fail, and one of them could not even say so.

        `config-absent` used to reach a bare `assert p.path is not None` in the runner —
        an AssertionError rather than a named refusal, and under `python -O` no assertion
        at all, at which point `None in present_paths` is False and the probe reports
        PASS. Requiring the subject at parse time makes the defect a refusal with a code,
        which is what every other malformed-descriptor case gets.
        """
        required = {
            "rule-present": ("rules", self.rules),
            "argv-flag": ("argv", self.argv),
            "config-absent": ("path", self.path),
            "deny-enforced": ("command", self.command),
            "allow-executes": ("command", self.command),
            "source-gated": ("command", self.command),
            # Without a variable name this probe would assert over nothing at all —
            # the same vacuous-pass shape every other entry here exists to refuse.
            "env-present": ("sentinel_name", self.sentinel_name),
        }.get(self.kind)
        if required is not None and not required[1]:
            raise ValueError(
                f"a {self.kind!r} probe needs a non-empty {required[0]!r}; without it the "
                "probe cannot fail and reports a pass over nothing"
            )
        # A probe whose expectation is EXECUTION must say what execution looks like.
        # Without it the runner has to guess, and a control that is scored on an absence
        # is the failure this whole probe design exists to avoid.
        if self.expect == "executed" and not self.expect_output:
            raise ValueError(
                f"probe {self.id!r} expects the command to execute and does not say what "
                "output proves it; scoring execution on an absence is how a control "
                "passes over a model that simply declined"
            )
        return self

    @property
    def local(self) -> bool:
        return self.kind in LOCAL_PROBE_KINDS


class ClassBinding(BaseModel):
    """How one policy class binds on one harness."""

    model_config = ConfigDict(extra="forbid")

    policy_class: Literal["P1", "P2", "P3", "P4"]
    title: str
    mechanisms: list[Mechanism] = Field(default_factory=list)
    probes: list[Probe] = Field(default_factory=list)
    # What this binding does NOT close. Required whenever an `instruction` mechanism is
    # present, because that is exactly the case where the enforceable part is partial.
    residual: str | None = None

    @property
    def enforceable_mechanisms(self) -> list[Mechanism]:
        return [m for m in self.mechanisms if m.enforceable]


class BindingVerification(BaseModel):
    """Evidence that the harness probes were actually run, and where to read it."""

    model_config = ConfigDict(extra="forbid")

    deployment: str
    harness_version: str
    ran_at: str
    probe_record: str      # repository-relative path to the recorded probe run
    outcome: Literal["pass", "fail"]
    # The digest of the materialized configuration the probes actually ran against.
    # Without it, `status: proven` survives an edit to the rules it was proven over: the
    # catalog digest pin then forces the operator to paste a new number, which does not
    # ask whether anything was re-probed. With it, a rule change invalidates its own
    # evidence and says so.
    config_digest: str

    @field_validator("config_digest")
    @classmethod
    def _digest_shape(cls, v: str) -> str:
        if not _DIGEST_SHAPE.fullmatch(v):
            raise ValueError(f"config_digest must be 'sha256:<64 hex>', got {v!r}")
        return v


class HarnessBinding(BaseModel):
    """The versioned, source-controlled descriptor for one launchable harness."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    binding_id: str
    harness: str
    captured_at: str
    # Where the materialized configuration is written, relative to the root named by
    # `config_root`. `null` means this harness takes its whole boundary from launch
    # flags. For a directory-shaped boundary (`codex-home`) this is the directory, and
    # the rendered files are relative to it.
    config_path: str | None
    config_format: Literal["claude-code-settings", "codex-home", "none"]
    # Which root `config_path` hangs off. `worker` is the worker's own clone, which is
    # right for a boundary the harness reads out of the project it is working in.
    # `run` is the supervisor's private run-dir, which is right for a boundary that
    # doubles as the harness's STATE directory: such a directory holds the ephemeral
    # credential copy and the session record, so it must sit outside every root the
    # model can write and outside any git tree, and it must be removable on every
    # terminal path without touching the worker's work.
    config_root: Literal["worker", "run"] = "worker"
    # Tokens that come immediately after the executable, before any flag — a harness
    # subcommand. Declared here rather than spelled in adapter code so that the argv an
    # adapter builds, the argv `check_argv` verifies, and the argv a probe runner drives
    # are one specification. Its absence was a real defect: the Codex binding required
    # `--ignore-user-config`, which exits 2 at the top level and is only valid under
    # `codex exec`, so the binding as written could not have started the harness at all.
    subcommand: list[str] = Field(default_factory=list)
    # Flags the child's argv MUST carry, as (flag, value) pairs. The adapter emits
    # these verbatim and `check_argv` verifies the argv it actually built — so the
    # descriptor is the single source and an adapter that drifts from it is caught,
    # rather than the two agreeing only in a comment.
    required_flags: list[list[str]] = Field(default_factory=list)
    # Which of those flags names the harness's command mode, so the mode can be checked
    # against known/safe sets without this module knowing any vendor's flag spelling.
    command_mode_flag: str | None = None
    # Command-line tokens that must NEVER appear. An unsafe mode is not a warning.
    forbidden_argv_tokens: list[str] = Field(default_factory=list)
    # Command modes this descriptor recognizes. A mode outside this set refuses rather
    # than being passed through on the assumption that the harness will do something
    # sensible with it.
    known_command_modes: list[str] = Field(default_factory=list)
    safe_command_modes: list[str] = Field(default_factory=list)
    classes: list[ClassBinding] = Field(default_factory=list)
    status: Literal["proven", "declared"]
    verification: BindingVerification | None = None
    note: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != BINDING_DESCRIPTOR_SCHEMA_VERSION:
            raise ValueError(
                f"harness binding schema_version {v} is not supported by this build "
                f"(expected {BINDING_DESCRIPTOR_SCHEMA_VERSION})"
            )
        return v

    @field_validator("binding_id", "harness")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _ID_SHAPE.fullmatch(v):
            raise ValueError(f"must match [A-Za-z0-9._-]{{1,64}}, got {v!r}")
        return v

    def klass(self, policy_class: str) -> ClassBinding | None:
        for c in self.classes:
            if c.policy_class == policy_class:
                return c
        return None

    @property
    def argv_prefix(self) -> list[str]:
        """Subcommand then required flags — everything the boundary contributes."""
        return [*self.subcommand, *(t for pair in self.required_flags for t in pair)]

    @property
    def command_mode(self) -> str | None:
        """The mode this descriptor pins, read out of its own required flags."""
        if self.command_mode_flag is None:
            return None
        for pair in self.required_flags:
            if len(pair) == 2 and pair[0] == self.command_mode_flag:
                return pair[1]
        return None

    def mechanism_summary(self) -> dict[str, list[str]]:
        """Per class, the enforceable mechanism kinds — the row of the binding matrix."""
        return {
            c.policy_class: sorted({m.kind for m in c.enforceable_mechanisms})
            for c in self.classes
        }


def binding_digest(raw: bytes) -> str:
    """`sha256:<hex>` over the descriptor's EXACT bytes.

    Same reasoning as the catalog digest: the question a later reader asks is "is the
    boundary the operator pinned the boundary that is there now", and a re-serialized
    structure answers a weaker question.
    """
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_binding_descriptor(raw: bytes) -> HarnessBinding:
    """Parse and structurally validate one descriptor, or refuse by name."""
    data = _parse_json(raw, "harness binding descriptor", "HARNESS_BINDING_MALFORMED")
    if not isinstance(data, dict):
        raise RefusalError(
            "HARNESS_BINDING_MALFORMED", "a harness binding descriptor must be a JSON object"
        )
    if "schema_version" not in data:
        raise RefusalError(
            "HARNESS_BINDING_VERSION",
            "descriptor carries no schema_version; an unversioned boundary is not a "
            "boundary this build will read",
        )
    try:
        return HarnessBinding(**data)
    except ValidationError as exc:
        first = _first_error(exc)
        code = (
            "HARNESS_BINDING_VERSION"
            if first.startswith("schema_version")
            else "HARNESS_BINDING_MALFORMED"
        )
        raise RefusalError(code, first) from exc


def check_coverage(binding: HarnessBinding) -> None:
    """Refuse a descriptor that does not actually bind all four classes.

    This is the "fail closed on missing coverage" half of the order, and it is written
    as a sequence of separate refusals rather than one generic "invalid" because the
    remedies differ: a missing class needs authoring, an unbound class needs a
    mechanism, an unprobed class needs a probe, and a prose-only class needs both a
    mechanism and an honest residual.
    """
    present = [c.policy_class for c in binding.classes]
    if len(present) != len(set(present)):
        dupes = sorted({p for p in present if present.count(p) > 1})
        raise RefusalError(
            "POLICY_CLASS_DUPLICATED",
            f"descriptor {binding.binding_id!r} binds {dupes} more than once; a policy "
            "class must resolve to exactly one binding",
        )
    missing = [p for p in POLICY_CLASSES if p not in present]
    if missing:
        raise RefusalError(
            "POLICY_CLASS_MISSING",
            f"descriptor {binding.binding_id!r} does not bind {missing}; an unbound "
            "class is a launch that does not happen (permissions.md)",
        )
    extra = [p for p in present if p not in POLICY_CLASSES]
    if extra:
        raise RefusalError(
            "POLICY_CLASS_UNKNOWN",
            f"descriptor {binding.binding_id!r} binds {extra}, which the approved "
            "policy does not define; a binding may not invent a class",
        )

    for c in binding.classes:
        if not c.mechanisms:
            raise RefusalError(
                "POLICY_CLASS_UNBOUND",
                f"{binding.binding_id}: policy class {c.policy_class} names no "
                "mechanism at all — a blank class refuses rather than defaulting open",
            )
        if not c.enforceable_mechanisms:
            kinds = sorted({m.kind for m in c.mechanisms})
            raise RefusalError(
                "POLICY_CLASS_UNBOUND",
                f"{binding.binding_id}: policy class {c.policy_class} is held up only "
                f"by {kinds} — a prose-only promise is not enforcement. Bind the part "
                "that can be enforced and name the rest as a residual",
            )
        if not c.probes:
            raise RefusalError(
                "POLICY_CLASS_UNPROBED",
                f"{binding.binding_id}: policy class {c.policy_class} carries no probe; "
                "a mechanism nobody checks is a claim, not a control",
            )
        # Only for a descriptor that actually renders a file. One whose format is `none`
        # declares its rules for a renderer that does not exist yet, and `materialize`
        # refuses it by name (HARNESS_BINDING_UNRENDERABLE) rather than asking it to
        # probe a configuration nothing writes.
        declared = (
            {
                r
                for m in c.mechanisms
                if m.kind in _DENY_BEARING_MECHANISMS
                for r in m.rules
            }
            if binding.config_format != "none"
            else set()
        )
        if declared:
            named = {r for p in c.probes if p.kind == "rule-present" for r in p.rules}
            if not declared & named:
                raise RefusalError(
                    "POLICY_CLASS_UNPROBED",
                    f"{binding.binding_id}: policy class {c.policy_class} declares "
                    f"{len(declared)} deny rule(s) and no rule-present probe names any of "
                    "them. Deleting the mechanism would then be caught by nothing — the "
                    "guarantee has to be as strong as the mechanisms the class claims, "
                    "not as strong as the probes its author happened to write",
                )
        if any(m.kind == "instruction" for m in c.mechanisms) and not c.residual:
            raise RefusalError(
                "POLICY_CLASS_RESIDUAL_UNSTATED",
                f"{binding.binding_id}: policy class {c.policy_class} relies partly on "
                "instructions and states no residual. Where the policy deliberately "
                "leans on instructions plus review, the descriptor must say what is "
                "NOT contained rather than letting the mechanism list imply it is",
            )


def check_command_mode(binding: HarnessBinding, mode: str | None) -> None:
    """Refuse an unrecognized or unsafe command mode before anything is created."""
    if mode is None:
        # A descriptor that names a mode flag and never emits it would skip this whole
        # check silently, which is the quiet way to end up with an unchecked mode. The
        # two coherent states are "no mode flag at all" and "a mode flag with a value";
        # anything between them is a descriptor defect, not a default.
        if binding.command_mode_flag is not None:
            raise RefusalError(
                "HARNESS_MODE_UNKNOWN",
                f"{binding.binding_id}: declares command_mode_flag "
                f"{binding.command_mode_flag!r} and no required flag supplies its value, "
                "so the mode this launch would run under is unknown and unchecked",
            )
        return
    if mode not in binding.known_command_modes:
        raise RefusalError(
            "HARNESS_MODE_UNKNOWN",
            f"{binding.binding_id}: command mode {mode!r} is not one this descriptor "
            f"recognizes ({sorted(binding.known_command_modes)}); an unrecognized mode "
            "refuses rather than being passed through on trust",
        )
    if mode not in binding.safe_command_modes:
        raise RefusalError(
            "HARNESS_MODE_UNSAFE",
            f"{binding.binding_id}: command mode {mode!r} is recognized and unsafe — it "
            "bypasses the very engine every class binds through",
        )


def check_argv(binding: HarnessBinding, argv: list[str]) -> None:
    """Verify the argv the adapter ACTUALLY built, not the one it described.

    An adapter that forgets a flag, or a future adapter edit that drops one, must be
    caught here — the descriptor is the contract and the argv is the delivery.
    """
    if binding.subcommand:
        want = binding.subcommand
        if argv[1 : 1 + len(want)] != want:
            raise RefusalError(
                "HARNESS_FLAG_MISSING",
                f"{binding.binding_id}: the built argv does not begin with subcommand "
                f"{want} after the executable. A flag valid only under a subcommand is "
                "an argv that cannot start the harness",
            )
    for token in binding.forbidden_argv_tokens:
        if token in argv:
            raise RefusalError(
                "HARNESS_MODE_UNSAFE",
                f"{binding.binding_id}: the built argv carries {token!r}, which this "
                "descriptor forbids outright",
            )
    for pair in binding.required_flags:
        if not pair or len(pair) > 2:
            raise RefusalError(
                "HARNESS_BINDING_MALFORMED",
                f"{binding.binding_id}: required_flags entries are [flag] or "
                f"[flag, value]; got {pair!r}",
            )
        if len(pair) == 1:
            if pair[0] not in argv:
                raise RefusalError(
                    "HARNESS_FLAG_MISSING",
                    f"{binding.binding_id}: the built argv is missing required flag "
                    f"{pair[0]!r}",
                )
            continue
        flag, value = pair[0], pair[1]
        found = any(
            argv[i] == flag and i + 1 < len(argv) and argv[i + 1] == value
            for i in range(len(argv))
        )
        if not found:
            raise RefusalError(
                "HARNESS_FLAG_MISSING",
                f"{binding.binding_id}: the built argv does not carry {flag} {value!r}. "
                "This flag is what makes the materialized boundary the one the child "
                "honors; without it the descriptor describes a file nobody reads",
            )


def check_harness_version(binding: HarnessBinding, probed: str) -> str | None:
    """Compare the installed harness to the one the evidence was taken against.

    Returns None when they agree closely enough, or a refusal message when they do not.
    Not raised from `resolve`, because `resolve` is a pure function of bytes and cannot
    ask the host what is installed — the supervisor probes the version and calls this
    immediately before the child exists, which is the same place the config digest is
    re-checked.

    The rule is major.minor, deliberately. Refusing on any difference would brick every
    launch the moment a harness auto-updates a patch release, which is a worse failure
    than the one it prevents; ignoring the difference entirely would leave a `proven`
    record standing over a binary whose matcher semantics may have changed. A patch
    difference is recorded and announced; a minor difference stops.
    """
    if binding.verification is None:
        return None
    proven = binding.verification.harness_version

    def series(v: str) -> tuple[str, ...]:
        return tuple(v.split(".")[:2])

    if series(proven) != series(probed):
        return (
            f"{binding.binding_id} was proven against harness {proven} and this host has "
            f"{probed}. A boundary's evidence is a point measurement against one build; a "
            "different series may match commands differently. Re-run "
            "scripts/hive-binding-probe and update the verification block"
        )
    return None


def check_status(binding: HarnessBinding) -> None:
    """A declared boundary is not a proven one, and only proven ones launch."""
    if binding.status != "proven":
        raise RefusalError(
            "HARNESS_BINDING_UNPROVEN",
            f"descriptor {binding.binding_id!r} is {binding.status!r}: its mechanisms "
            "are written from documentation and have not been probed against the "
            "installed harness on this deployment. Run scripts/hive-binding-probe and "
            "record the result before a worker runs here",
        )
    if binding.verification is None or binding.verification.outcome != "pass":
        raise RefusalError(
            "HARNESS_BINDING_UNPROVEN",
            f"descriptor {binding.binding_id!r} claims status 'proven' with no passing "
            "verification record; the claim and its evidence must travel together",
        )
    # And the evidence must be evidence about THESE rules. A descriptor whose rules were
    # edited after the probe run keeps a passing record that no longer describes it; the
    # catalog re-pin the edit forces is an operator pasting a number, not an operator
    # re-proving a boundary.
    proved = binding.verification.config_digest
    current = materialize(binding, extra_dirs=[]).digest
    if proved != current:
        raise RefusalError(
            "HARNESS_BINDING_UNPROVEN",
            f"descriptor {binding.binding_id!r} was proven against a configuration "
            f"hashing to {proved}, and now renders {current}. The rules changed after the "
            "probe run, so the record is evidence about a boundary that is no longer this "
            "one. Re-run scripts/hive-binding-probe and update the verification block",
        )


def check_digest(binding: HarnessBinding, raw: bytes, expected: str) -> None:
    """Fail closed on descriptor drift.

    The operator pins a digest in the deployment catalog. If the repository's
    descriptor has moved since — a strengthened rule, a weakened one, a typo — the
    launch refuses and the operator re-pins deliberately. A boundary change is an
    approved act, never a side effect of pulling code.
    """
    if not _DIGEST_SHAPE.fullmatch(expected):
        raise RefusalError(
            "CATALOG_MALFORMED",
            f"binding_digest must be 'sha256:<64 hex>', got {expected!r}",
        )
    actual = binding_digest(raw)
    if actual != expected:
        raise RefusalError(
            "BINDING_DIGEST_MISMATCH",
            f"route pins descriptor {binding.binding_id!r} at {expected}, but the "
            f"descriptor in this build hashes to {actual}. The boundary moved since the "
            "operator approved it — re-pin the catalog deliberately, or check out the "
            "build the pin was taken against",
        )


# --------------------------------------------------------------------------------------
# Materialization: descriptor -> the smallest harness-native configuration
# --------------------------------------------------------------------------------------


class MaterializeContext(BaseModel):
    """The per-execution facts a renderer needs, and nothing else.

    Every field defaults to empty, and the empty context is the CANONICAL rendering —
    the one `verification.config_digest` pins and `check_status` re-derives. A launch
    renders the same rules with this execution's paths filled in, so the two digests
    differ by construction and each answers its own question: the canonical one asks
    "are these the rules that were proved", the launch one asks "are these the bytes
    the child read".
    """

    model_config = ConfigDict(extra="forbid")

    # Writable roots outside the worker root — on Claude Code the `additionalDirectories`
    # entry, on Codex a second `write` row in the filesystem table.
    extra_dirs: list[str] = Field(default_factory=list)
    # The worker's own clone. Claude Code scopes to the cwd implicitly and needs no
    # entry; a harness with an explicit filesystem table must name it or the worker
    # cannot write its own workspace.
    worker_root: str = ""
    # The supervisor's private run-dir. Never writable, and DENIED outright where the
    # harness can express that: it holds the plan that anchors the boundary check and,
    # for a generated-home harness, the ephemeral credential copy.
    run_dir: str = ""


class MaterializedFile(BaseModel):
    """One file the launcher writes, relative to the boundary's own root."""

    model_config = ConfigDict(extra="forbid")

    path: str        # relative to `Materialized.path`'s root; never absolute, never `..`
    content: str
    digest: str      # sha256 over `content`


class Materialized(BaseModel):
    """What the launcher wrote, and the digest the supervisor re-checks."""

    model_config = ConfigDict(extra="forbid")

    path: str | None            # root-relative; None when the boundary is argv-only
    # For a single-file boundary this is the file's exact bytes. For a directory-shaped
    # one it is the MANIFEST — one `<digest>  <path>` line per rendered file, sorted —
    # so that `digest` keeps meaning "the one number that pins what was written",
    # whether that is one file or a tree.
    content: str
    digest: str                 # sha256 over `content`
    rules: list[str]            # every enforceable rule the file carries, flattened
    # The actual files. A single-file boundary carries exactly one entry whose `path`
    # equals `Materialized.path`; a directory boundary carries one per file, each
    # relative to that directory.
    files: list[MaterializedFile] = Field(default_factory=list)
    # Whether `path` names a directory to be created rather than a file to be written.
    directory: bool = False
    # Kept apart as well as flattened, because a substring search over the whole file
    # cannot tell a deny from an allow — and a rule MOVED from deny to allow would then
    # still satisfy its own `rule-present` probe over a file that now permits it.
    deny: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)


def _class_rules(binding: HarnessBinding, kind: str) -> list[str]:
    """Every rule of one mechanism kind, in descriptor order, deduplicated."""
    out: list[str] = []
    for c in binding.classes:
        for m in c.mechanisms:
            if m.kind == kind:
                out.extend(m.rules)
    return _dedupe(out)


# A `filesystem-deny` rule may be written relative to every writable root, because the
# path that matters is not a fixed place on the host but "wherever this worker can
# write". Codex's filesystem table takes absolute paths and `~`-rooted ones only — a
# bare `**/*.env` is rejected outright — so the descriptor carries the ROOT-RELATIVE
# form and the renderer expands it once per writable root. In the CANONICAL rendering
# there are no roots, so these rules expand to nothing, which is correct: they describe
# a boundary around this execution's trees and there are none.
_ROOT_TOKEN = "{writable_root}"


def _expand_roots(rules: list[str], roots: list[str]) -> list[str]:
    out: list[str] = []
    for rule in rules:
        if _ROOT_TOKEN not in rule:
            out.append(rule)
            continue
        for root in roots:
            out.append(rule.replace(_ROOT_TOKEN, root.rstrip("/")))
    return _dedupe(out)


def _toml_quote(value: str) -> str:
    r"""A TOML basic string. Refuses rather than escaping anything exotic.

    Every value that reaches here is a filesystem path this launcher chose, so the
    reachable character set is narrow. A path carrying a quote, a backslash or a
    control character is a launcher bug or a hostile workspace name, and silently
    escaping it would write a boundary whose meaning depends on a TOML parser's
    escape handling agreeing with ours. Refusing is the honest answer.
    """
    if any(c in value for c in '"\\\n\r\t') or any(ord(c) < 0x20 for c in value):
        raise RefusalError(
            "HARNESS_BINDING_UNRENDERABLE",
            f"cannot render {value!r} into the boundary: a path carrying a quote, a "
            "backslash or a control character would change what the rendered "
            "configuration means, so it refuses rather than being escaped",
        )
    return f'"{value}"'


def _codex_home(binding: HarnessBinding, ctx: MaterializeContext) -> list[MaterializedFile]:
    r"""Render the descriptor into a generated Codex home directory.

    Two files, because Codex binds commands and paths through two different
    mechanisms and there is no one file that carries both:

      config.toml       a named PERMISSION PROFILE. `default_permissions` selects it,
                        `filesystem` maps paths to read/write/deny. Measured on
                        codex-cli 0.147.0: a `deny` entry beats a containing `write`
                        root, globs work, `~` expands, and a path named by nothing is
                        readable — which is what makes the denies falsifiable.
      rules/hive.rules  the EXECPOLICY rules, `prefix_rule(pattern, decision)` in
                        Starlark. User scope, so they are always loaded; the project
                        scope is gated on a trust entry and is the wrong shape for a
                        launcher.

    Why the argv carries no sandbox flag: `--sandbox workspace-write` OVERRIDES the
    profile. Measured — with the profile denying a planted secret and `-s
    workspace-write` on the command line, the agent read the secret. So the mode is
    expressed here and `--sandbox` is a forbidden argv token, not a required flag.
    `--ignore-user-config` is forbidden for the mirror reason: it suppresses exactly
    this config.toml.
    """
    write_roots = _dedupe([d for d in [ctx.worker_root, *ctx.extra_dirs] if d])
    deny_paths = _expand_roots(_class_rules(binding, "filesystem-deny"), write_roots)
    # The run-dir holds the plan that anchors the boundary check AND the ephemeral
    # credential copy, so it is denied outright rather than merely left unwritable.
    if ctx.run_dir:
        deny_paths.append(ctx.run_dir)

    lines = [
        "# GENERATED by omegahive hive-launch. Do not edit: the supervisor recomputes",
        "# this file's digest against the approved plan immediately before the child",
        f"# exists, and a mismatch is a terminal failure. Descriptor: {binding.binding_id}",
        "",
        f"default_permissions = {_toml_quote(_CODEX_PROFILE)}",
        "",
        f"[permissions.{_CODEX_PROFILE}]",
        "filesystem = {",
        '  "/" = "read",',
    ]
    for root in _dedupe(write_roots):
        lines.append(f"  {_toml_quote(root)} = \"write\",")
    for path in _dedupe(deny_paths):
        lines.append(f"  {_toml_quote(path)} = \"deny\",")
    lines.append("}")
    lines.append("")

    rules = _class_rules(binding, "settings-deny") + _class_rules(binding, "settings-allow")
    rule_lines = [
        "# GENERATED by omegahive hive-launch. Codex execpolicy, user scope.",
        "# `forbidden` wins over `allow` regardless of order; patterns match argv",
        "# tokens positionally, so they cannot be evaded by whitespace and cannot",
        "# over-match a command that merely mentions the token.",
        "",
        *rules,
        "",
    ]
    return [
        _rendered("config.toml", "\n".join(lines)),
        _rendered("rules/hive.rules", "\n".join(rule_lines)),
    ]


# The profile name the renderer writes and `default_permissions` selects. One spelling,
# here, because a name that appeared twice would eventually appear differently twice.
_CODEX_PROFILE = "hive-worker"


def _rendered(path: str, content: str) -> MaterializedFile:
    return MaterializedFile(
        path=path, content=content, digest=binding_digest(content.encode("utf-8"))
    )


def _manifest(files: list[MaterializedFile]) -> str:
    """`<digest>  <path>` per file, sorted by path — the digest subject for a tree.

    Sorted so that reordering the renderer's output cannot change the number, and
    carrying each file's own digest so that a manifest match means every file matches.
    """
    return "".join(f"{f.digest}  {f.path}\n" for f in sorted(files, key=lambda f: f.path))


def _claude_code_settings(binding: HarnessBinding, extra_dirs: list[str]) -> dict[str, Any]:
    """Render the descriptor into Claude Code's own settings schema.

    Deliberately minimal. This file carries the deny rules, the allow rules that keep
    development tools usable, and the additional directory the worker's code clone
    lives in — and nothing else. It never restates the operator's global preferences,
    because the launcher does not own those and rewriting them would be the failure
    `permissions.md` was written to end, one level down.
    """
    deny: list[str] = []
    allow: list[str] = []
    for c in binding.classes:
        for m in c.mechanisms:
            if m.kind == "settings-deny":
                deny.extend(m.rules)
            elif m.kind == "settings-allow":
                allow.extend(m.rules)
    perms: dict[str, Any] = {}
    if allow:
        perms["allow"] = _dedupe(allow)
    if deny:
        perms["deny"] = _dedupe(deny)
    if extra_dirs:
        perms["additionalDirectories"] = list(extra_dirs)
    return {"permissions": perms}


def _dedupe(items: list[str]) -> list[str]:
    """Preserve descriptor order; drop repeats. Order is readable, sets are not."""
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def materialize(
    binding: HarnessBinding,
    *,
    extra_dirs: list[str] | None = None,
    context: MaterializeContext | None = None,
) -> Materialized:
    """Render the harness-native configuration for one isolated worker root.

    `context` carries everything per-execution. `extra_dirs` is the older, narrower
    spelling of the same thing and is kept because it is the whole context a
    single-root harness needs; passing both is a caller bug and refuses.
    """
    if context is not None and extra_dirs is not None:
        raise RefusalError(
            "HARNESS_BINDING_MALFORMED",
            "materialize takes extra_dirs or context, not both — two spellings of the "
            "same per-execution facts is how they drift apart",
        )
    ctx = context or MaterializeContext(extra_dirs=list(extra_dirs or []))
    extra_dirs = list(ctx.extra_dirs)
    if binding.config_format == "none" or binding.config_path is None:
        # Rules with no renderer are rules nobody will write. A descriptor may legitimately
        # take its whole boundary from argv (`config_format: none`), but then it must not
        # also declare rule-bearing mechanisms — otherwise it materializes an empty
        # configuration while its own text reads as a bound set of denials, which is the
        # same lie the empty-rules check refuses one level up. The shipped Codex
        # descriptor is exactly this shape on purpose: its rules are recorded for the
        # worker who will build the renderer, and this refusal is what stops them being
        # mistaken for something in force.
        orphaned = [
            m.kind
            for c in binding.classes
            for m in c.mechanisms
            if m.kind in _RULE_BEARING_MECHANISMS
        ]
        if orphaned:
            raise RefusalError(
                "HARNESS_BINDING_UNRENDERABLE",
                f"{binding.binding_id}: declares {sorted(set(orphaned))} rules and "
                f"config_format {binding.config_format!r}, so nothing writes them. Either "
                "ship a renderer for this format or stop declaring rules this build "
                "cannot materialize",
            )
        return Materialized(path=None, content="", digest=binding_digest(b""), rules=[])
    if binding.config_format == "codex-home":
        files = _codex_home(binding, ctx)
        write_roots = _dedupe([d for d in [ctx.worker_root, *ctx.extra_dirs] if d])
        # `deny` is what the file ACTUALLY says, root-relative rules already expanded,
        # because a `rule-present` probe that matched the descriptor's template rather
        # than the rendered line would go green over a file that says something else.
        deny = _class_rules(binding, "settings-deny") + _expand_roots(
            _class_rules(binding, "filesystem-deny"), write_roots
        )
        allow = _class_rules(binding, "settings-allow")
        if not deny:
            # The same gate the Claude Code branch carries, for the same reason: four
            # classes can each name an enforceable mechanism — a generated home, an env
            # allowlist — and still render a directory that refuses nothing.
            raise RefusalError(
                "HARNESS_BINDING_UNRENDERABLE",
                f"{binding.binding_id}: renders {binding.config_path} with no deny rule "
                "of any kind. A descriptor whose format writes a boundary must write a "
                "boundary into it",
            )
        manifest = _manifest(files)
        return Materialized(
            path=binding.config_path,
            content=manifest,
            digest=binding_digest(manifest.encode("utf-8")),
            rules=deny + allow,
            deny=deny,
            allow=allow,
            files=files,
            directory=True,
        )
    if binding.config_format == "claude-code-settings":
        doc = _claude_code_settings(binding, extra_dirs)
        content = json.dumps(doc, indent=2, sort_keys=False) + "\n"
        perms = doc["permissions"]
        # The last gate, and the one that catches what the per-mechanism check cannot.
        # `_rule_bearing_kinds_carry_rules` refuses an EMPTY settings-deny; it says
        # nothing about a class that names only `launch-flag` or `env-allowlist` — both
        # legitimately carry no rules, both are enforceable, and four such classes pass
        # coverage while rendering `{"permissions": {}}`. A descriptor that renders a
        # file must render a boundary into it; the alternative is `config_format: none`,
        # which is a different and honestly-labelled thing.
        if not perms.get("deny"):
            raise RefusalError(
                "HARNESS_BINDING_UNRENDERABLE",
                f"{binding.binding_id}: renders {binding.config_path} with no deny rules "
                "at all. Four classes can each name an enforceable mechanism and still "
                "produce an empty configuration; a descriptor whose format writes a file "
                "must write a boundary into it",
            )
        rules = list(perms.get("deny", [])) + list(perms.get("allow", []))
        return Materialized(
            path=binding.config_path,
            content=content,
            digest=binding_digest(content.encode("utf-8")),
            rules=rules,
            deny=list(perms.get("deny", [])),
            allow=list(perms.get("allow", [])),
            files=[_rendered(binding.config_path, content)],
        )
    raise RefusalError(
        "HARNESS_BINDING_MALFORMED",
        f"{binding.binding_id}: this build cannot materialize config_format "
        f"{binding.config_format!r}; the renderer and the descriptor must ship together",
    )


# --------------------------------------------------------------------------------------
# Local probes: deterministic, no model call, run at every preflight
# --------------------------------------------------------------------------------------


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str
    policy_class: str
    kind: str
    state: Literal["pass", "fail", "deferred"]
    detail: str


def run_local_probes(
    binding: HarnessBinding,
    *,
    materialized: Materialized,
    argv: list[str],
    env: dict[str, str],
    parent_env: Mapping[str, str] | None = None,
    present_paths: frozenset[str] = frozenset(),
) -> list[ProbeResult]:
    """Run every deterministic probe the descriptor declares.

    `deferred` is a first-class state and is not a pass: it means "this probe needs the
    installed harness and a model call, so it ran in the probe runner and its outcome
    lives in the descriptor's verification record". Rendering it as a pass here would
    turn the expensive half of the evidence into a thing nobody notices is missing.
    """
    results: list[ProbeResult] = []
    for c in binding.classes:
        for p in c.probes:
            if not p.local:
                results.append(
                    ProbeResult(
                        probe_id=p.id,
                        policy_class=c.policy_class,
                        kind=p.kind,
                        state="deferred",
                        detail="needs the installed harness: see scripts/hive-binding-probe",
                    )
                )
                continue
            state, detail = _run_local_probe(
                p, materialized, argv, env, dict(parent_env or {}), present_paths
            )
            results.append(
                ProbeResult(
                    probe_id=p.id,
                    policy_class=c.policy_class,
                    kind=p.kind,
                    state=state,
                    detail=detail,
                )
            )
    return results


def _run_local_probe(
    p: Probe,
    materialized: Materialized,
    argv: list[str],
    env: dict[str, str],
    parent_env: dict[str, str],
    present_paths: frozenset[str],
) -> tuple[Literal["pass", "fail"], str]:
    if p.kind == "rule-present":
        # Checked against the DENY array, not against the file's text. A substring search
        # over the whole file is satisfied by a rule sitting in the allow list — so
        # moving `Bash(*curl *)` from deny to allow keeps its own probe green over a
        # configuration that now explicitly permits curl. The probe must know which side
        # of the boundary its rule landed on.
        missing = [r for r in p.rules if r not in materialized.deny]
        if missing:
            widened = [r for r in missing if r in materialized.allow]
            if widened:
                return "fail", (
                    f"{widened} appear in the ALLOW list rather than the deny list — the "
                    "materialized config permits what this probe says it refuses"
                )
            return "fail", f"materialized config denies none of {missing}"
        return "pass", f"{len(p.rules)} rule(s) present in the materialized deny list"
    if p.kind == "argv-flag":
        joined = "\x00".join(argv)
        want = "\x00".join(p.argv)
        if want not in joined:
            return "fail", f"argv does not carry {p.argv}"
        return "pass", f"argv carries {p.argv}"
    if p.kind == "env-absent":
        # Measured as a DIFFERENCE, not as an absence. Checking only that the child's
        # environment is clean makes this probe unfalsifiable in the real pipeline: the
        # adapter builds that environment from an allowlist and drops credential-shaped
        # names, so the thing being checked is the thing that just did the checking, and
        # the declared sentinel (whose name contains TOKEN) is doubly unreachable. What
        # has content is: the parent HAD such names, and none of them survived.
        leaked = sorted(k for k in env if any(m in k.upper() for m in CREDENTIAL_MARKERS))
        if p.sentinel_name and p.sentinel_name in env:
            leaked.append(p.sentinel_name)
        if leaked:
            return "fail", f"constructed environment carries {leaked}"
        candidates = sorted(
            k
            for k in parent_env
            if any(m in k.upper() for m in CREDENTIAL_MARKERS)
            or (p.sentinel_name is not None and k == p.sentinel_name)
        )
        if candidates:
            return "pass", (
                f"{len(candidates)} credential-shaped name(s) in the parent environment, "
                f"0 in the {len(env)} constructed variable(s)"
            )
        return "pass", (
            f"no credential-shaped name among {len(env)} constructed variable(s); the "
            "parent carried none either, so nothing was dropped and this run measured a "
            "clean parent rather than a working filter"
        )
    if p.kind == "env-present":
        # The mirror of `env-absent`, and it exists because on some harnesses the
        # load-bearing launch control is an ENVIRONMENT VARIABLE rather than a flag:
        # Codex reads its whole boundary out of `$CODEX_HOME`, so an argv-flag probe
        # would be checking a surface the boundary does not travel on. Asserting the
        # SUFFIX rather than the whole value keeps the check meaningful without
        # putting an absolute host path in a descriptor that ships in git.
        name = p.sentinel_name or ""
        value = env.get(name)
        if not value:
            return "fail", (
                f"the constructed environment carries no {name}, so the child would "
                "fall back to the operator's own harness state instead of the "
                "generated one this boundary is written into"
            )
        if p.path and not value.endswith(p.path):
            return "fail", (
                f"{name} does not end with {p.path!r}; the child is pointed at some "
                "other directory than the one the boundary was materialized into"
            )
        return "pass", f"{name} is set and ends with {p.path or '<any>'}"
    if p.kind == "config-absent":
        # `path` is required for this kind by the model, so it is present here.
        if p.path in present_paths:
            return "fail", f"{p.path} exists and would override the materialized boundary"
        return "pass", f"{p.path} absent"
    return "fail", f"no local implementation for probe kind {p.kind!r}"
