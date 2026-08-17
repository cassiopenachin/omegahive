"""The two records: a deployment route catalog and a committed launch binding.

These are deliberately two files with two owners, because they are two different kinds
of fact and collapsing them is the defect this module exists to prevent:

  * A **route catalog entry** is a DEPLOYMENT fact — what this host can currently run
    and what list-price basis applies. It carries no project judgment, it lives outside
    the workspace, and it is never committed to a project. Its content digest is
    recorded at launch so a later reader can tell whether the catalog has since moved.

  * A **launch binding** is a signed DECISION — use one named route, for one pinned
    task and order, with one token prediction. It is committed in the workspace and the
    launcher merely materializes it. The coordinator may recommend it; invoking the
    pinned binding is the operator's approval.

The asymmetry that follows, and the reason `extra="forbid"` is on every model here: a
binding may SELECT a route by name and may not DESCRIBE one. Identity comes from the
catalog or it does not come at all. A binding carrying `model`, `argv`, or `env` is not
a binding with a typo — it is an attempt to launch something the operator did not sign,
and it refuses by name rather than being silently dropped by a lenient parser.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omegahive.events.types import ExecutionIdentity, PriceBasis

# Bumped only on a breaking change. A record whose version this build does not know
# refuses rather than being read on a guess — a misread route is a misbilled launch.
CATALOG_SCHEMA_VERSION = 1
BINDING_SCHEMA_VERSION = 1

_NAME_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_REF_SHAPE = re.compile(r".+@[0-9a-f]{7,40}")
_DIGEST_SHAPE = re.compile(r"sha256:[0-9a-f]{64}")

# Keys a binding may never carry. Every one of them is an identity or execution field
# that belongs to the catalog (deployment) or to the adapter (code). `extra="forbid"`
# already refuses them; this list exists so the refusal can NAME the attempted override
# instead of reporting a generic unknown-field error, because the two have very
# different remedies.
BINDING_FORBIDDEN_KEYS = frozenset(
    {
        "adapter", "argv", "billing_market", "binding_digest", "binding_id", "cmd",
        "command", "credential_mode", "credential_pool", "entrypoint", "env",
        "environment", "exec", "executable", "harness", "model", "model_vendor",
        "permissions", "price_basis", "provider", "sandbox", "shell",
    }
)


class RefusalError(Exception):
    """A refusal with a machine code — the one failure type this module raises.

    Codes are part of the operator-facing contract (`hive-launch --check` prints them),
    so they are stable strings, not prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RouteEntry(BaseModel):
    """One thing this deployment can currently run."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model_vendor: str
    provider: str
    model: str
    harness: str
    billing_market: Literal["subscription", "api"]
    credential_pool: str
    adapter: str
    # Which permission-boundary descriptor this route runs under, and the exact bytes
    # of it the operator approved. Both are REQUIRED: a route with no named boundary is
    # the empty harness-bindings row `permissions.md` says is a launch that does not
    # happen, and a named boundary with no digest cannot fail closed when the
    # descriptor moves. The digest is re-pinned deliberately when the boundary changes,
    # which is what keeps a boundary change from riding in on a code deploy.
    binding_id: str
    binding_digest: str
    # How the provider credential reaches the execution. `harness-native` means the
    # harness's own already-authenticated account is used and nothing is handed to the
    # worker; `broker` means an operator-owned broker issues a scoped, expiring
    # capability. There is deliberately no third value: a raw provider key in a route,
    # a binding, or a worker environment is not a mode, it is the thing this field
    # exists to make unrepresentable.
    credential_mode: Literal["harness-native", "broker"] = "harness-native"
    # A route present in the catalog but switched off — kept visible (and therefore
    # auditable) rather than deleted, so "we turned this off" and "this never existed"
    # stay distinguishable.
    enabled: bool = True
    # Absent for subscription routes, which have no per-token list price. Absence is
    # recorded as absence; it never becomes a zero that reads as free.
    price_basis: PriceBasis | None = None
    note: str | None = None

    @field_validator("name", "binding_id")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not _NAME_SHAPE.fullmatch(v):
            raise ValueError(f"must match [A-Za-z0-9._-]{{1,64}}, got {v!r}")
        return v

    @field_validator("binding_digest")
    @classmethod
    def _digest_shape(cls, v: str) -> str:
        if not _DIGEST_SHAPE.fullmatch(v):
            raise ValueError(f"binding_digest must be 'sha256:<64 hex>', got {v!r}")
        return v

    def identity(self) -> ExecutionIdentity:
        """The normalized identity block that goes on every lifecycle fact."""
        return ExecutionIdentity(
            route=self.name,
            model_vendor=self.model_vendor,
            provider=self.provider,
            model=self.model,
            harness=self.harness,
            billing_market=self.billing_market,
            credential_pool=self.credential_pool,
            adapter=self.adapter,
        )


class RouteCatalog(BaseModel):
    """The deployment's route list.

    `routes` is a LIST, not a name-keyed object, on purpose: a JSON object with a
    repeated key is collapsed silently by every parser we use, so a hand-edited catalog
    holding the same route name twice would resolve to whichever copy happened to win.
    A list makes the duplicate visible, and `resolve_route` refuses it as ambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    captured_at: str
    routes: list[RouteEntry] = Field(default_factory=list)
    note: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"catalog schema_version {v} is not supported by this build "
                f"(expected {CATALOG_SCHEMA_VERSION})"
            )
        return v


class LaunchBinding(BaseModel):
    """The signed decision, committed in the workspace.

    Note what is NOT here: any execution detail. The binding selects; the catalog
    describes; the adapter executes.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    task: str
    order_ref: str                       # workspace path@<git-sha>, pinned
    route: str                           # a NAME resolved against the catalog
    predicted_total_tokens: int = Field(ge=0)
    purpose: Literal["work", "review"] = "work"
    attempt: int = Field(default=1, ge=1)
    note: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != BINDING_SCHEMA_VERSION:
            raise ValueError(
                f"binding schema_version {v} is not supported by this build "
                f"(expected {BINDING_SCHEMA_VERSION})"
            )
        return v

    @field_validator("task", "route")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not _NAME_SHAPE.fullmatch(v):
            raise ValueError(f"must match [A-Za-z0-9._-]{{1,64}}, got {v!r}")
        return v

    @field_validator("order_ref")
    @classmethod
    def _ref_shape(cls, v: str) -> str:
        if not _REF_SHAPE.fullmatch(v):
            raise ValueError(f"order_ref must be 'path@<git-sha>', got {v!r}")
        return v


def catalog_digest(raw: bytes) -> str:
    """`sha256:<hex>` over the catalog's EXACT bytes.

    Over bytes rather than over the parsed structure, deliberately: the question a
    reader asks later is "was the file the operator approved against the same file that
    is there now", and a re-serialized structure answers a different, weaker question.
    """
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _parse_json(raw: bytes, what: str, code: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RefusalError(code, f"{what} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefusalError(code, f"{what} is not valid JSON: {exc}") from exc


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"]) or "<root>"
    return f"{loc}: {err['msg']}"


def load_catalog(raw: bytes) -> RouteCatalog:
    data = _parse_json(raw, "route catalog", "CATALOG_MALFORMED")
    if not isinstance(data, dict):
        raise RefusalError("CATALOG_MALFORMED", "route catalog must be a JSON object")
    try:
        return RouteCatalog(**data)
    except ValidationError as exc:
        raise RefusalError("CATALOG_MALFORMED", _first_error(exc)) from exc


def load_binding(raw: bytes) -> LaunchBinding:
    data = _parse_json(raw, "launch binding", "BINDING_MALFORMED")
    if not isinstance(data, dict):
        raise RefusalError("BINDING_MALFORMED", "launch binding must be a JSON object")

    # Named before the generic parse so the operator is told they tried to override
    # catalog identity, not merely that a field was unrecognized.
    overrides = sorted(BINDING_FORBIDDEN_KEYS & set(data))
    if overrides:
        raise RefusalError(
            "BINDING_OVERRIDES_IDENTITY",
            f"a binding selects a route by name and may not describe one; "
            f"remove {overrides} — execution identity comes from the deployment catalog",
        )
    try:
        return LaunchBinding(**data)
    except ValidationError as exc:
        raise RefusalError("BINDING_MALFORMED", _first_error(exc)) from exc


def resolve_route(catalog: RouteCatalog, name: str) -> RouteEntry:
    """Resolve a route NAME to EXACTLY ONE catalog entry, or refuse."""
    matches = [r for r in catalog.routes if r.name == name]
    if not matches:
        known = ", ".join(sorted(r.name for r in catalog.routes)) or "<none>"
        raise RefusalError(
            "ROUTE_UNKNOWN", f"no route named {name!r} in the catalog; known: {known}"
        )
    if len(matches) > 1:
        raise RefusalError(
            "ROUTE_AMBIGUOUS",
            f"route {name!r} appears {len(matches)} times in the catalog; "
            "a route name must resolve to exactly one entry",
        )
    entry = matches[0]
    if not entry.enabled:
        raise RefusalError(
            "ROUTE_DISABLED", f"route {name!r} is present but disabled in the catalog"
        )
    return entry
