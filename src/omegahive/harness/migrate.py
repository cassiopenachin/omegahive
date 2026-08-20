"""v1 route catalog -> v2, as a pure function. The production path off the old schema.

A v1 catalog pinned every route to a permission-boundary descriptor by id and digest,
and gated `api`-market routes behind a credential mode. All three are retired. What a
v2 route needs instead is a `runner` block — the executable this deployment actually
runs, its static arguments, the environment variable NAMES it needs, and whether the
worker can perform its own spine writes and publication or needs the supervisor to
bridge them.

Nothing here enables or disables a route, edits an identity, invents a price, or drops
a note. The operator's catalog comes out the other side saying the same things about
the same routes, in the schema this build reads. The one thing the migration may have to
ask for is the worker default, because v1 had no such field and a catalog with several
enabled routes cannot have one inferred.

The known-harness translations below are the invocations this repository was actually
launching on 2026-08-20. A harness the table does not know is migrated to a runner that
names it and nothing more, which is a configuration an operator can read and correct —
never a guess dressed as a fact.
"""

from __future__ import annotations

import json
from typing import Any

from omegahive.harness.records import (
    CATALOG_SCHEMA_VERSION,
    CATALOG_SCHEMA_VERSION_V1,
    RefusalError,
    RouteCatalog,
)

# Fields a v1 route carried that v2 has no place for. Dropped rather than preserved
# under another name: a `binding_digest` copied forward is a pin to a descriptor this
# build no longer reads, and a reader would be right to think it still meant something.
V1_ONLY_KEYS = ("binding_id", "binding_digest", "credential_mode")

# The Codex native-sandbox settings the operator measured on codex-cli 0.147.0
# (boundary evidence, 2026-08-20), expressed as ordinary route arguments.
#
# Three deliberate absences, each with a reason the operator already ruled on:
#
#   * NO `--enable network_proxy`. Egress stays OFF. The measurements established that
#     the managed proxy works and that, with it on, a one-token obfuscation of a
#     forbidden fetch reaches an allowlisted host — so enabling it answers a policy
#     question that is the operator's, not this migration's. A supervised route does not
#     need egress for the worker protocol: sync, emit and publication are bridged.
#   * NO `--sandbox <mode>`. The mode follows from the permissions profile; passing the
#     flag OVERRIDES the profile, which was measured to defeat the deny table.
#   * NO execpolicy command-prefix rules. Those live in `$CODEX_HOME/*.rules`, are a
#     deployment file rather than an argument, and command spelling is retired as a
#     boundary (permissions.md, P4). A route that inherits `CODEX_HOME` keeps whatever
#     the operator has there.
#
# The writable roots are absent on purpose too: the adapter merges the task root and
# both clones' `.git` directories into this table at launch, because only the launch
# knows them.
_CODEX_DENIES = [
    "~/.ssh",
    "~/.aws",
    "~/.netrc",
    "~/.git-credentials",
    "~/.config/gh",
    "~/.config/omegahive",
    "~/.local/share/omegahive",
    "~/repos",
    "~/src/SNET/omegahive",
]


def _codex_filesystem_table() -> str:
    entries = {"/": "read"}
    for path in _CODEX_DENIES:
        entries[path] = "deny"
    body = ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in entries.items())
    return "permissions.hive-worker.filesystem={" + body + "}"


KNOWN_RUNNERS: dict[str, dict[str, Any]] = {
    "claude-code": {
        "executable": "claude",
        # The permission mode the launcher has always passed. A launched pane that waits
        # on interactive permission prompts is not launched, so this is a function
        # setting rather than a safety one; the operator's own deny settings are a
        # deployment example and stay in the operator's own configuration.
        "args": ["--permission-mode", "auto"],
        "inherit_env": ["CLAUDE_CONFIG_DIR"],
        "worker_io": "direct",
    },
    "codex": {
        "executable": "codex",
        "args": [
            "exec",
            "-c", 'default_permissions="hive-worker"',
            "-c", _codex_filesystem_table(),
        ],
        # CODEX_HOME carries the operator's own Codex state: the ChatGPT login the
        # subscription route bills against, and any execpolicy rules the operator keeps.
        "inherit_env": ["CODEX_HOME"],
        # The measured reason, not a preference: inside this sandbox the emit wrapper's
        # podman transport cannot initialize, the workspace hub is outside every
        # writable root, and no forge credential is reachable. Those are worker-protocol
        # outcomes the supervisor supplies from outside the boundary.
        "worker_io": "supervised",
    },
    "fake": {
        "executable": "hive-fake-harness",
        "args": [],
        "inherit_env": ["HIVE_FAKE_BEHAVIOUR", "HIVE_FAKE_USAGE_FILE"],
        "worker_io": "direct",
    },
}


def is_v2(data: dict[str, Any]) -> bool:
    return data.get("schema_version") == CATALOG_SCHEMA_VERSION


def migrate_catalog(
    data: dict[str, Any], *, default_worker: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Return `(v2 catalog, notes)`. Raises `RefusalError` on anything it may not decide.

    `notes` are operator-facing lines about what the migration did and what it could not
    know — a harness with no known invocation, for instance. They are advisory; the
    catalog they accompany is complete and valid.
    """
    if not isinstance(data, dict):
        raise RefusalError("CATALOG_MALFORMED", "route catalog must be a JSON object")
    version = data.get("schema_version")
    if version == CATALOG_SCHEMA_VERSION:
        # Idempotent by design: running the migration on a v2 catalog is a no-op, so an
        # operator who is unsure whether they already ran it can simply run it again.
        return data, ["already schema_version 2; nothing to migrate"]
    if version != CATALOG_SCHEMA_VERSION_V1:
        raise RefusalError(
            "CATALOG_MALFORMED",
            f"cannot migrate schema_version {version!r}; this migration reads "
            f"version {CATALOG_SCHEMA_VERSION_V1}",
        )

    notes: list[str] = []
    routes_in = data.get("routes")
    if not isinstance(routes_in, list):
        raise RefusalError("CATALOG_MALFORMED", "routes must be a list")

    routes_out: list[dict[str, Any]] = []
    for raw in routes_in:
        if not isinstance(raw, dict):
            raise RefusalError("CATALOG_MALFORMED", "every route must be a JSON object")
        route = {k: v for k, v in raw.items() if k not in V1_ONLY_KEYS}
        harness = route.get("harness", "")
        known = KNOWN_RUNNERS.get(harness)
        if known is None:
            route["runner"] = {
                "executable": harness or route.get("adapter", "") or "UNSET",
                "args": [],
                "inherit_env": [],
                "worker_io": "direct",
            }
            notes.append(
                f"route {route.get('name')!r}: harness {harness!r} has no known "
                "invocation in this build, so its runner names the harness and nothing "
                "more. Set executable/args before launching it."
            )
        else:
            route["runner"] = {
                "executable": known["executable"],
                "args": list(known["args"]),
                "inherit_env": list(known["inherit_env"]),
                "worker_io": known["worker_io"],
            }
        routes_out.append(route)

    enabled = [r["name"] for r in routes_out if r.get("enabled", True)]
    if default_worker is None:
        if len(enabled) == 1:
            default_worker = enabled[0]
            notes.append(f"worker default set to the only enabled route: {default_worker}")
        else:
            raise RefusalError(
                "DEFAULT_ROUTE_REQUIRED",
                f"v2 needs exactly one worker default and this catalog has "
                f"{len(enabled)} enabled route(s): {', '.join(enabled) or '<none>'}. "
                "Re-run with --default <name>",
            )
    elif default_worker not in [r["name"] for r in routes_out]:
        raise RefusalError(
            "DEFAULT_ROUTE_UNKNOWN",
            f"--default names {default_worker!r} and no such route is in the catalog",
        )
    elif default_worker not in enabled:
        raise RefusalError(
            "DEFAULT_ROUTE_DISABLED",
            f"--default names {default_worker!r}, which is present but disabled. "
            "Migration never enables or disables a route; enable it first, or pick "
            "another default",
        )

    out: dict[str, Any] = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "captured_at": data.get("captured_at", ""),
        "defaults": {"worker": default_worker},
        "routes": routes_out,
    }
    if data.get("note") is not None:
        out["note"] = data["note"]

    # Validate before anyone writes it. A migration that emits a file the launcher then
    # refuses is worse than one that refuses itself, because the operator has already
    # replaced their catalog by then.
    RouteCatalog(**out)
    return out, notes
