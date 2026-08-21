"""The one record: the operator-owned deployment route catalog.

There used to be two — a catalog and a committed per-order *launch binding* that named
a route, pinned an order, and carried a signed token prediction. The binding is gone.
The accepted runner doctrine (2026-08-20) says configuration is authorization: a route
present in this deployment's catalog with `enabled: true` is blessed for ordinary work,
and an operator-entered `--route` override is blessed for one launch. There is no second
approval file, no digest to paste, no promotion state, and no per-order prerequisite.

What a catalog entry is, and what it is deliberately not:

  * It is a DEPLOYMENT fact — what this host can currently run, under which runner
    invocation, billing against which pool, at what list-price basis. It lives outside
    every project and is never committed to one. Its content digest is recorded at
    launch so a later reader can tell whether the catalog has since moved.

  * It is NOT a safety certificate. The `runner` block below says which executable this
    deployment runs and with which static arguments; Hive records that resolved shape
    and does not claim to know what is behind it. An operator-supplied wrapper is just
    an executable plus arguments.

Two things the catalog may never carry, and both refuse by name rather than being
dropped by a lenient parser: a credential VALUE of any kind (the runner block names
environment *variables*, never their contents), and the name of a Hive authority
credential — the database, gateway and reserved-role DSNs. Provider credentials are
deployment posture and an operator may inherit one by name; Hive's own authority is not
inheritable by a worker under any configuration.
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
# v1 carried per-route permission-boundary pins (`binding_id`, `binding_digest`) and a
# `credential_mode` gate; v2 replaces all three with the `runner` block. v2 itself was
# trimmed by the `worker-turns` cutover — the `runner.worker_io` choice and the retired
# `hive-worker` Codex permission arguments are gone — without a version bump, because
# `extra="forbid"` makes a catalog still carrying them refuse with the field named, and
# `hive-routes migrate` drops them in place. `hive-routes migrate` is the production path
# for both steps.
CATALOG_SCHEMA_VERSION = 2
CATALOG_SCHEMA_VERSION_V1 = 1

_NAME_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ENV_NAME_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

# Hive's OWN authority credentials, by exact environment-variable name. These are the
# database and gateway DSNs from `secrets-manifest.yaml` plus the notifier's bot token —
# every one of them a capability over Hive's durable record or Hive's outbound identity,
# not a provider account an operator might legitimately want a worker to use.
#
# The old rule banned any name CONTAINING `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD` or
# `CREDENTIAL`. That is retired: it also banned `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`,
# which are provider access — deployment posture the operator owns — and the doctrine's
# whole point is that Hive does not decide those. What Hive still decides is its own.
HIVE_AUTHORITY_ENV_NAMES = frozenset(
    {
        "OMEGAHIVE_DATABASE_URL",
        "OMEGAHIVE_GATEWAY_DATABASE_URL",
        "OMEGAHIVE_OWNER_DATABASE_URL",
        "OMEGAHIVE_TEST_DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "TELEGRAM_BOT_TOKEN",
    }
)

# The shape rule behind the list, so a future DSN added to the deployment is refused on
# the day it is added rather than on the day someone remembers to extend the set above.
_HIVE_AUTHORITY_SHAPE = re.compile(r"^OMEGAHIVE_[A-Z0-9_]*DATABASE_URL$")


def is_hive_authority_env(name: str) -> bool:
    """True when `name` is a Hive authority credential and never inheritable."""
    return name in HIVE_AUTHORITY_ENV_NAMES or bool(_HIVE_AUTHORITY_SHAPE.match(name))


class RefusalError(Exception):
    """A refusal with a machine code — the one failure type this module raises.

    Codes are part of the operator-facing contract (`hive-launch --check` prints them),
    so they are stable strings, not prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RunnerSpec(BaseModel):
    """How this deployment actually starts the harness. Argv, never a shell string.

    Three fields and no fourth, because every additional field is a place a shell could
    re-enter. `executable` plus `args` is the whole command; an operator who wants a
    container, a VM, a sandbox wrapper or a bare binary writes exactly that here and
    Hive runs it. The adapter appends the dynamic elements it knows the harness needs —
    the model, the session id, the task-root paths, and the kickoff.

    `inherit_env` names variables this route needs from the launching environment. Names
    only: no value ever appears in this file, in a plan, in a log line, in the preflight
    or on the spine. A provider credential name may be listed deliberately; a Hive
    authority credential name refuses.

    There is deliberately no `worker_io` field any more. It used to choose between the
    worker performing its own spine writes and git publication (`direct`) and a
    privileged resident mediator performing them on its behalf (`supervised`). The
    mediator is retired: under the accepted runner doctrine a configured full-worker
    runner must itself supply ordinary worker function — read/edit/test/commit, governed
    emit, workspace sync and publication, code push and PR — and a runner that withholds
    them is a runner the operator must change, not a hole for Hive to keep a trusted
    process patching. A route that cannot emit, sync or publish now produces an honest
    block from the worker rather than a second transport.
    """

    model_config = ConfigDict(extra="forbid")

    executable: str
    args: list[str] = Field(default_factory=list)
    inherit_env: list[str] = Field(default_factory=list)

    @field_validator("executable")
    @classmethod
    def _executable_present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("executable must not be empty")
        if any(c in v for c in "\n\r\x00"):
            raise ValueError("executable must not contain a newline or NUL")
        return v

    @field_validator("args")
    @classmethod
    def _args_are_argv(cls, v: list[str]) -> list[str]:
        for a in v:
            if "\x00" in a:
                raise ValueError("an argument must not contain a NUL byte")
        return v

    @field_validator("inherit_env")
    @classmethod
    def _env_names_only(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _ENV_NAME_SHAPE.fullmatch(name):
                raise ValueError(
                    f"inherit_env carries {name!r}, which is not an environment "
                    "variable NAME; this field never holds a value"
                )
            if is_hive_authority_env(name):
                raise ValueError(
                    f"inherit_env names {name!r}, a Hive authority credential. Provider "
                    "access is deployment posture and may be inherited by name; Hive's "
                    "own database, gateway and reserved-role credentials may not"
                )
        return v

    def fingerprint(self) -> str:
        """`sha256:<hex>` over the resolved, non-secret runner configuration.

        This is the provenance the doctrine asks for and deliberately not a posture
        verdict: it answers "was the runner configuration the same as last time", which
        a reader can check, and says nothing about whether that configuration is safe,
        which nobody here can check. Environment NAMES are inside the fingerprint;
        values were never available to it.

        The `worker-turns` cutover removed `worker_io` from the canonical form, so a
        route's fingerprint changes across that boundary even when the operator changed
        nothing. That is correct and intended: the resolved runner configuration really
        did change shape, and a fingerprint that pretended otherwise would answer its one
        question wrongly. Historical events keep the value they were emitted with; no
        event is rewritten.
        """
        canonical = json.dumps(
            {
                "executable": self.executable,
                "args": list(self.args),
                "inherit_env": sorted(self.inherit_env),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RouteEntry(BaseModel):
    """One thing this deployment can currently run."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model_vendor: str
    provider: str
    model: str
    harness: str
    adapter: str
    billing_market: Literal["subscription", "api"]
    credential_pool: str      # opaque label — never a key, an account id, or a host path
    runner: RunnerSpec
    # A route present in the catalog but switched off — kept visible (and therefore
    # auditable) rather than deleted, so "we turned this off" and "this never existed"
    # stay distinguishable.
    enabled: bool = True
    # Absent for subscription routes, which have no per-token list price. Absence is
    # recorded as absence; it never becomes a zero that reads as free.
    price_basis: PriceBasis | None = None
    note: str | None = None

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not _NAME_SHAPE.fullmatch(v):
            raise ValueError(f"must match [A-Za-z0-9._-]{{1,64}}, got {v!r}")
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


class CatalogDefaults(BaseModel):
    """Which route an ordinary launch uses when the operator names none.

    Exactly one, and required. A catalog with several plausible workers and no stated
    default makes `hive-launch <order>` a coin toss over billing markets — so the
    absence refuses at preflight rather than resolving to whichever entry came first.
    """

    model_config = ConfigDict(extra="forbid")

    worker: str

    @field_validator("worker")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not _NAME_SHAPE.fullmatch(v):
            raise ValueError(f"must match [A-Za-z0-9._-]{{1,64}}, got {v!r}")
        return v


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
    defaults: CatalogDefaults
    routes: list[RouteEntry] = Field(default_factory=list)
    note: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"catalog schema_version {v} is not supported by this build "
                f"(expected {CATALOG_SCHEMA_VERSION}). A v1 catalog is migrated with "
                "`hive-routes migrate`, which backs up the original first"
            )
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
    # Named before the generic parse so an operator running a v1 catalog is told to
    # migrate rather than reading a field-level complaint about `defaults`.
    if data.get("schema_version") == CATALOG_SCHEMA_VERSION_V1:
        raise RefusalError(
            "CATALOG_V1",
            "this is a schema_version 1 route catalog. Migrate it with `hive-routes "
            "migrate` (it writes a timestamped backup beside the original first); v1 "
            "pinned a permission-boundary descriptor per route, which this build no "
            "longer reads",
        )
    legacy = _legacy_fields(data)
    if legacy:
        raise RefusalError(
            "CATALOG_LEGACY_FIELDS",
            "this catalog still carries fields the `worker-turns` cutover retired: "
            + ", ".join(legacy)
            + ". Run `hive-routes migrate` (it writes a timestamped backup beside the "
            "original first, and preserves every operator-authored runner argument and "
            "route identity exactly). The refusal is by name rather than a silent drop "
            "because a dropped `worker_io` would change how a route is launched without "
            "the operator ever being told",
        )
    try:
        return RouteCatalog(**data)
    except ValidationError as exc:
        raise RefusalError("CATALOG_MALFORMED", _first_error(exc)) from exc


# Fields a pre-cutover v2 catalog may still carry, and where each one used to live. They
# are named rather than pattern-matched so the refusal can tell an operator exactly what
# `hive-routes migrate` will do to their file.
LEGACY_RUNNER_FIELDS = ("worker_io",)


def _legacy_fields(data: Any) -> list[str]:
    """Retired field paths present in a raw catalog document, in stable order."""
    found: list[str] = []
    routes = data.get("routes")
    if not isinstance(routes, list):
        return found
    for i, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        runner = route.get("runner")
        if not isinstance(runner, dict):
            continue
        name = route.get("name") if isinstance(route.get("name"), str) else f"#{i}"
        for field_name in LEGACY_RUNNER_FIELDS:
            if field_name in runner:
                found.append(f"routes[{name}].runner.{field_name}")
    return found


def resolve_route(catalog: RouteCatalog, name: str | None) -> tuple[RouteEntry, str]:
    """Resolve a route NAME (or the catalog default) to EXACTLY ONE enabled entry.

    Returns the entry and its provenance — `"override"` when the operator named it on
    the command line, `"default"` when it came from `defaults.worker`. The provenance is
    recorded on the launch, because "the operator chose this route for this task" and
    "this is what the catalog does by default" are different facts about the same run.
    """
    source = "override"
    if name is None:
        source = "default"
        name = catalog.defaults.worker
        if not any(r.name == name for r in catalog.routes):
            known = ", ".join(sorted(r.name for r in catalog.routes)) or "<none>"
            raise RefusalError(
                "DEFAULT_ROUTE_UNKNOWN",
                f"the catalog's defaults.worker names {name!r} and no such route "
                f"exists; known: {known}",
            )

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
            "ROUTE_DISABLED",
            f"route {name!r} is present but disabled in the catalog. Catalog presence "
            "plus enabled:true is what authorizes a runner; editing that field is the "
            "authorization act",
        )
    return entry, source
