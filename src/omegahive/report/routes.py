"""The catalog check: every route as `resolvable` or `refused`, with the reason.

One redacted, side-effect-free command answers the question an operator otherwise
answers by attempting a launch and reading a traceback: what can this deployment run
right now, which route is the worker default, whether each named executable is present,
and — for the ones that cannot resolve — exactly why.

This used to be a qualification report: boundary status per route, mechanism per policy
class, probe tallies, residual prose. That product is retired (runner-trust doctrine,
2026-08-20). Configuration is authorization, so there is no state here to promote and
nothing to certify; what is left is a catalog and command check, which is a smaller and
more honest thing.

Three properties are load-bearing:

  * **It never changes a route.** This module only reads. There is no path here that
    writes a catalog, enables an entry, or records anything. A report that could fix
    what it reports would eventually be run for the fixing.

  * **It makes no network or model call.** No provider is contacted and no harness is
    started. Executable presence is answered by the HOST and passed in, because a pure
    function cannot stat anything and the CLI runs in a container where the operator's
    harnesses are not installed.

  * **It is redacted by construction, not by filtering.** The only strings it can print
    come from the catalog — route identity, an opaque credential-pool label, and the
    runner's executable and argument vector — plus refusal messages. It never receives
    an environment or a credential, so there is nothing here for a redaction pass to
    miss. The runner's `inherit_env` is printed as NAMES, which is all it ever holds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from omegahive.harness.adapters import get_adapter
from omegahive.harness.records import RefusalError, RouteEntry, load_catalog


def _row(route: RouteEntry, *, default: bool, present: bool | None,
         state: str = "resolvable", refusal_code: str | None = None,
         reason: str | None = None) -> dict[str, Any]:
    return {
        "route": route.name,
        "state": state,
        "refusal_code": refusal_code,
        "reason": reason,
        "is_worker_default": default,
        "enabled": route.enabled,
        "model_vendor": route.model_vendor,
        "provider": route.provider,
        "model": route.model,
        "harness": route.harness,
        "adapter": route.adapter,
        "billing_market": route.billing_market,
        "credential_pool": route.credential_pool,
        "executable": route.runner.executable,
        "executable_present": present,
        "runner_args": list(route.runner.args),
        "inherit_env": list(route.runner.inherit_env),
        "runner_fingerprint": route.runner.fingerprint(),
        # Resolved through the adapter, not guessed: `codex exec resume` refuses several
        # options `codex exec` accepts, so "can this route be resumed" is a property of
        # the operator's own argument vector and has to be computed from it.
        "resume": _resume_status(route),
        "has_price_basis": route.price_basis is not None,
        "note": route.note,
    }


def evaluate_routes(
    *,
    catalog_raw: bytes,
    present_executables: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Every catalog route, in catalog order, with the one verdict a check can give.

    `present_executables` maps an executable name to whether the host can find it. An
    absent entry is reported as unknown rather than as missing: not having asked and
    having asked and been told no are different facts, and only one of them is a reason
    to stop.
    """
    catalog = load_catalog(catalog_raw)
    present_executables = present_executables or {}
    rows: list[dict[str, Any]] = []
    default_name = catalog.defaults.worker
    seen: dict[str, int] = {}
    for route in catalog.routes:
        seen[route.name] = seen.get(route.name, 0) + 1

    for route in catalog.routes:
        present = present_executables.get(route.runner.executable)
        is_default = route.name == default_name
        if seen[route.name] > 1:
            rows.append(_row(route, default=is_default, present=present, state="refused",
                             refusal_code="ROUTE_AMBIGUOUS",
                             reason=f"route name {route.name!r} appears "
                                    f"{seen[route.name]} times in the catalog"))
            continue
        if not route.enabled:
            rows.append(_row(route, default=is_default, present=present, state="refused",
                             refusal_code="ROUTE_DISABLED",
                             reason="present but disabled; enabling it is the "
                                    "authorization act"))
            continue
        try:
            get_adapter(route.adapter)
        except RefusalError as exc:
            rows.append(_row(route, default=is_default, present=present, state="refused",
                             refusal_code=exc.code, reason=exc.message))
            continue
        if present is False:
            rows.append(_row(route, default=is_default, present=present, state="refused",
                             refusal_code="EXECUTABLE_MISSING",
                             reason=f"the host cannot find {route.runner.executable!r} "
                                    "on PATH"))
            continue
        rows.append(_row(route, default=is_default, present=present))

    if default_name not in {r["route"] for r in rows}:
        known = ", ".join(sorted(r["route"] for r in rows)) or "<none>"
        raise RefusalError(
            "DEFAULT_ROUTE_UNKNOWN",
            f"the catalog's defaults.worker names {default_name!r} and no such route "
            f"exists; known: {known}",
        )
    return rows


def _resume_status(route) -> str:
    """`supported`, or the exact reason this route cannot be woken.

    Built by asking the adapter, with a placeholder session id, so the answer is the same
    one `hive-answer` will get. A route that cannot even build an initial turn (malformed
    runner args) reports that here too — it is the same class of fact and an operator
    reading a catalog check wants both.
    """
    from omegahive.harness.adapters import LaunchContext, get_adapter
    from omegahive.harness.records import RefusalError

    ctx = LaunchContext(
        kickoff="", cwd="", task_root="", execution_id="", session_id="probe",
        resume_session_id="probe",
    )
    try:
        plan = get_adapter(route.adapter).build_resume(route, ctx)
    except RefusalError as exc:
        return f"REFUSED [{exc.code}] {exc.message}"
    return "supported" if plan.resumable else (plan.resume_unsupported_reason or "supported")


def routes_to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2, sort_keys=True)


def routes_to_text(rows: list[dict[str, Any]]) -> str:
    """The human form. Every row states its verdict first, on one line."""
    if not rows:
        return "no routes in the catalog.\n"
    out: list[str] = []
    ok = sum(1 for r in rows if r["state"] == "resolvable")
    out.append(f"{len(rows)} route(s): {ok} resolvable, {len(rows) - ok} refused")
    out.append("")
    for r in rows:
        verdict = "OK      " if r["state"] == "resolvable" else f"REFUSED [{r['refusal_code']}]"
        marker = "  <- worker default" if r["is_worker_default"] else ""
        out.append(f"{verdict}  {r['route']}{marker}")
        out.append(
            f"    {r['model']} @ {r['harness']} "
            f"({r['billing_market']}, pool {r['credential_pool']}, adapter {r['adapter']})"
        )
        if r["executable_present"] is None:
            found = "presence not checked"
        else:
            found = "found on PATH" if r["executable_present"] else "NOT FOUND on PATH"
        out.append(f"    runner: {r['executable']}  ({found})")
        # Whether this route can be WOKEN, answered here rather than at answer time. An
        # operator who discovers a route is unresumable when a blocked worker needs an
        # answer has discovered it too late, and the remedy — rewriting the route's args —
        # is a catalog edit they would rather make before a launch.
        out.append(f"      resume: {r['resume']}")
        if r["runner_args"]:
            out.append(f"      args: {r['runner_args']}")
        if r["inherit_env"]:
            out.append(f"      inherits (names only): {' '.join(r['inherit_env'])}")
        out.append(f"      fingerprint: {r['runner_fingerprint']}")
        if r["reason"]:
            out.append(f"    reason: {r['reason']}")
        if r["note"]:
            out.append(f"    note: {r['note']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
