"""Harness binding descriptors: the worker permission boundary, made checkable.

`permissions.md` states four policy classes (P1 access layer, P2 secrets, P3 durable
stack, P4 raw fetch) and then leaves one question open — who *enforces* them for a
worker on a harness the hive does not control. The answer this module implements:

    the repository carries an auditable binding DESCRIPTOR; the launcher
    MATERIALIZES the harness-native configuration into the isolated worker root
    and VERIFIES what the child will actually honor before the execution starts.

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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    }
)
MECHANISM_KINDS = ENFORCEABLE_MECHANISMS | {"instruction"}

# Probe kinds. `local` probes are deterministic and run at every preflight; `harness`
# probes need the installed harness and a model call, so they run in the probe runner
# (`scripts/hive-binding-probe`) and their result is recorded in the descriptor.
LOCAL_PROBE_KINDS = frozenset({"rule-present", "argv-flag", "env-absent", "config-absent"})
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
    path: str | None = None
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


class HarnessBinding(BaseModel):
    """The versioned, source-controlled descriptor for one launchable harness."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    binding_id: str
    harness: str
    captured_at: str
    # Where the materialized configuration is written, relative to the worker root.
    # `null` means this harness takes its whole boundary from launch flags.
    config_path: str | None
    config_format: Literal["claude-code-settings", "codex-toml", "none"]
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
    for token in binding.forbidden_argv_tokens:
        if token in argv:
            raise RefusalError(
                "HARNESS_MODE_UNSAFE",
                f"{binding.binding_id}: the built argv carries {token!r}, which this "
                "descriptor forbids outright",
            )
    for pair in binding.required_flags:
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


class Materialized(BaseModel):
    """What the launcher wrote, and the digest the supervisor re-checks."""

    model_config = ConfigDict(extra="forbid")

    path: str | None            # worker-root-relative; None when the boundary is argv-only
    content: str                # exact bytes to write (empty when path is None)
    digest: str                 # sha256 over `content`
    rules: list[str]            # every enforceable rule the file carries, flattened


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


def materialize(binding: HarnessBinding, *, extra_dirs: list[str]) -> Materialized:
    """Render the harness-native configuration for one isolated worker root."""
    if binding.config_format == "none" or binding.config_path is None:
        return Materialized(path=None, content="", digest=binding_digest(b""), rules=[])
    if binding.config_format == "claude-code-settings":
        doc = _claude_code_settings(binding, extra_dirs)
        content = json.dumps(doc, indent=2, sort_keys=False) + "\n"
        perms = doc["permissions"]
        rules = list(perms.get("deny", [])) + list(perms.get("allow", []))
        return Materialized(
            path=binding.config_path,
            content=content,
            digest=binding_digest(content.encode("utf-8")),
            rules=rules,
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
    present_paths: frozenset[str] = frozenset(),
) -> list[ProbeResult]:
    """Run every deterministic probe the descriptor declares.

    `deferred` is a first-class state and is not a pass: it means "this probe needs the
    installed harness and a model call, so it ran in the probe runner and its outcome
    lives in the descriptor's verification record". Rendering it as a pass here would
    turn the expensive half of the evidence into a thing nobody notices is missing.
    """
    results: list[ProbeResult] = []
    file_text = materialized.content
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
            state, detail = _run_local_probe(p, file_text, argv, env, present_paths)
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
    file_text: str,
    argv: list[str],
    env: dict[str, str],
    present_paths: frozenset[str],
) -> tuple[Literal["pass", "fail"], str]:
    if p.kind == "rule-present":
        missing = [r for r in p.rules if json.dumps(r)[1:-1] not in file_text]
        if missing:
            return "fail", f"materialized config is missing {missing}"
        return "pass", f"{len(p.rules)} rule(s) present in the materialized config"
    if p.kind == "argv-flag":
        joined = "\x00".join(argv)
        want = "\x00".join(p.argv)
        if want not in joined:
            return "fail", f"argv does not carry {p.argv}"
        return "pass", f"argv carries {p.argv}"
    if p.kind == "env-absent":
        leaked = sorted(
            k for k in env if any(m in k.upper() for m in CREDENTIAL_MARKERS)
        )
        if p.sentinel_name and p.sentinel_name in env:
            leaked.append(p.sentinel_name)
        if leaked:
            return "fail", f"constructed environment carries {leaked}"
        return "pass", f"no credential-shaped name among {len(env)} constructed variable(s)"
    if p.kind == "config-absent":
        assert p.path is not None
        if p.path in present_paths:
            return "fail", f"{p.path} exists and would override the materialized boundary"
        return "pass", f"{p.path} absent"
    return "fail", f"no local implementation for probe kind {p.kind!r}"
