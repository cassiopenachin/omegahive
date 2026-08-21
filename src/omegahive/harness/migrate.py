"""Bringing an operator's route catalog to the shape this build reads. A pure function.

Two migrations live here and both run on every call, oldest first:

  **v1 -> v2.** A v1 catalog pinned every route to a permission-boundary descriptor by id
  and digest, and gated `api`-market routes behind a credential mode. All three are
  retired. What a v2 route needs instead is a `runner` block — the executable this
  deployment actually runs, its static arguments, and the environment variable NAMES it
  needs.

  **the `worker-turns` cutover, within v2.** Two things a pre-cutover v2 catalog can
  still carry are dropped: `runner.worker_io`, which chose between a worker doing its own
  spine writes and a privileged resident mediator doing them for it, and the retired
  `hive-worker` Codex permission profile arguments, which were Hive's own specification
  of an operator's sandbox. There is no schema bump, because `extra="forbid"` already
  makes a catalog carrying either refuse by name, which is the loud failure a silent drop
  would not be.

Nothing here enables or disables a route, edits an identity, invents a price, or drops a
note. **Every operator-authored runner argument survives verbatim**: the cutover removes
only the two argument pairs that name the retired `hive-worker` profile literally, and
anything else the operator wrote — a sandbox mode, a wrapper, a feature flag — comes out
exactly as it went in. The one thing the migration may have to ask for is the worker
default, because v1 had no such field and a catalog with several enabled routes cannot
have one inferred.

The known-harness translations below are the invocations this repository launches. A
harness the table does not know is migrated to a runner that names it and nothing more,
which is a configuration an operator can read and correct — never a guess dressed as a
fact.
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

# Arguments the `worker-turns` cutover retires from a pre-cutover v2 catalog, and the
# rule that recognizes them.
#
# Exactly two shapes are removed, and both name the retired `hive-worker` permission
# profile LITERALLY: `-c default_permissions="hive-worker"` and any
# `-c permissions.hive-worker.<key>=...`. That profile was Hive's own specification of
# an operator's Codex sandbox, written into catalogs by this repository's own migration
# and widened at launch by the adapter. Under the accepted runner doctrine the runner's
# reach is the operator's to configure and Hive's to record, so Hive stops shipping one.
#
# Nothing else is touched. A route that also carries `-s workspace-write`, a wrapper, a
# feature flag or a differently-named permissions profile keeps every one of them
# verbatim: those are operator-authored, opaque to Hive, and preserving them exactly is
# the difference between a migration and an opinion.
_RETIRED_CODEX_PROFILE = "hive-worker"
_CONFIG_FLAGS = ("-c", "--config")


def _is_retired_codex_config(value: str) -> bool:
    key = value.split("=", 1)[0].strip()
    if key == "default_permissions":
        rhs = value.split("=", 1)[1].strip() if "=" in value else ""
        return rhs.strip('"\'') == _RETIRED_CODEX_PROFILE
    return key.startswith(f"permissions.{_RETIRED_CODEX_PROFILE}.")


def strip_retired_codex_args(args: list[str]) -> tuple[list[str], list[str]]:
    """Return `(kept args, removed args)` for one route's static argument vector."""
    kept: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in _CONFIG_FLAGS and nxt is not None and _is_retired_codex_config(nxt):
            removed += [a, nxt]
            i += 2
            continue
        kept.append(a)
        i += 1
    return kept, removed


def cutover_route(route: dict[str, Any]) -> list[str]:
    """Apply the `worker-turns` cutover to ONE route in place; return operator notes."""
    notes: list[str] = []
    runner = route.get("runner")
    if not isinstance(runner, dict):
        return notes
    name = route.get("name", "<unnamed>")
    if "worker_io" in runner:
        was = runner.pop("worker_io")
        notes.append(
            f"route {name!r}: dropped runner.worker_io={was!r}. The supervised transport "
            "is retired; this route's worker now emits, syncs and publishes directly, and "
            "must be configured to be able to. If it cannot, the worker will block and "
            "say so rather than being bridged"
        )
    args = runner.get("args")
    if isinstance(args, list) and all(isinstance(a, str) for a in args):
        kept, removed = strip_retired_codex_args(args)
        if removed:
            runner["args"] = kept
            notes.append(
                f"route {name!r}: dropped the retired hive-worker Codex permission "
                f"arguments ({len(removed)} element(s)). Codex now runs under whatever "
                "sandbox YOUR configuration selects; if you want the old table back, "
                "re-add it under a profile name of your own choosing — it is your "
                "deployment posture, not Hive's"
            )
    return notes


KNOWN_RUNNERS: dict[str, dict[str, Any]] = {
    "claude-code": {
        "executable": "claude",
        # The permission mode the launcher has always passed. A launched pane that waits
        # on interactive permission prompts is not launched, so this is a function
        # setting rather than a safety one; the operator's own deny settings are a
        # deployment example and stay in the operator's own configuration.
        "args": ["--permission-mode", "auto"],
        "inherit_env": ["CLAUDE_CONFIG_DIR"],
    },
    "codex": {
        # `exec` and nothing else. The adapter appends `--json`, the model and the
        # prompt, and inserts `resume <thread-id>` here on a resume turn. Any sandbox,
        # permission profile or feature flag beyond this is the OPERATOR's to add: Hive
        # migrated one of its own into every catalog once and then had to widen it from
        # inside the launcher to make a worker able to commit, which is precisely the
        # arrangement the runner-trust doctrine retires.
        "executable": "codex",
        "args": ["exec"],
        # CODEX_HOME carries the operator's own Codex state: the ChatGPT login the
        # subscription route bills against, and any execpolicy rules the operator keeps.
        "inherit_env": ["CODEX_HOME"],
    },
    "fake": {
        "executable": "hive-fake-harness",
        "args": [],
        "inherit_env": ["HIVE_FAKE_BEHAVIOUR", "HIVE_FAKE_USAGE_FILE"],
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
        # Already v2 — but a pre-cutover v2 can still carry retired fields, so the
        # cutover half runs anyway. Idempotent by design: a catalog with nothing left to
        # drop comes back byte-equal with a note saying so, which is what makes it safe
        # for an operator who is unsure whether they already ran it.
        out = json.loads(json.dumps(data))
        notes = []
        for route in out.get("routes", []):
            if isinstance(route, dict):
                notes += cutover_route(route)
        if not notes:
            notes = ["already schema_version 2 and post-cutover; nothing to migrate"]
        RouteCatalog(**out)
        return out, notes
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
            }
        notes += cutover_route(route)
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
