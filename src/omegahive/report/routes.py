"""The refusal report: every catalog route as `launchable` or `refused`, with reasons.

One redacted, side-effect-free command answers the question an operator otherwise
answers by attempting a launch and reading a traceback: which routes can carry a worker
right now, under which permission boundary, with which classes probed, and — for the
ones that cannot — exactly why.

Three properties are load-bearing:

  * **It never upgrades a route.** This module only reads. There is no path here that
    materializes a file, writes a catalog, marks a descriptor proven, or flips a
    credential mode. A report that could fix what it reports would eventually be run
    for the fixing.

  * **It makes no network or model call.** The harness version is not probed and no
    provider is contacted, so this is safe to run on a laptop, in a cron, or into a
    paste. What it cannot see, it says it cannot see.

  * **It is redacted by construction, not by filtering.** The only strings it can print
    come from the catalog (route identity and an opaque credential-pool label), the
    descriptor (rule patterns, which are policy), and refusal messages. It never
    receives an environment, a settings file's values, or a credential, so there is
    nothing here for a redaction pass to miss.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from omegahive.harness.adapters import LaunchContext, get_adapter
from omegahive.harness.bindings import materialize, run_local_probes
from omegahive.harness.plan import _credential_gate, resolve_harness_binding
from omegahive.harness.records import RefusalError, RouteEntry, load_catalog

# A deterministic stand-in for the launch context. The report builds each route's argv
# through the REAL adapter — an argv-flag probe over a hand-written vector would check
# the report's idea of the boundary rather than the launcher's — but it must do so
# without a task, an order, or a worker, so these placeholders stand in for the parts
# that cannot affect a boundary check.
_STUB_SESSION = "00000000-0000-4000-a000-000000000000"


def _row(route: RouteEntry, **kw: Any) -> dict[str, Any]:
    base = {
        "route": route.name,
        "harness": route.harness,
        "adapter": route.adapter,
        "model": route.model,
        "provider": route.provider,
        "billing_market": route.billing_market,
        "credential_mode": route.credential_mode,
        "credential_pool": route.credential_pool,
        "enabled": route.enabled,
        "binding_id": route.binding_id,
        "binding_digest": route.binding_digest,
        "state": "refused",
        "refusal_code": None,
        "reason": None,
        "binding_status": None,
        "command_mode": None,
        "classes": {},
        "probes": {},
        "residuals": {},
    }
    base.update(kw)
    return base


def evaluate_routes(
    *,
    catalog_raw: bytes,
    descriptors_raw: Mapping[str, bytes],
    parent_env: Mapping[str, str] | None = None,
    present_paths: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """One row per catalog route, in catalog order.

    Catalog order rather than sorted: the operator edits that file by hand, and a report
    whose order matches the file is a report they can read against it.
    """
    catalog = load_catalog(catalog_raw)
    env = dict(parent_env or {})
    rows: list[dict[str, Any]] = []
    for route in catalog.routes:
        if not route.enabled:
            rows.append(
                _row(
                    route,
                    refusal_code="ROUTE_DISABLED",
                    reason="present in the catalog and switched off; kept visible so "
                    "'we turned this off' and 'this never existed' stay distinguishable",
                )
            )
            continue
        try:
            binding = resolve_harness_binding(route, descriptors_raw)
        except RefusalError as exc:
            rows.append(_row(route, refusal_code=exc.code, reason=exc.message))
            continue
        try:
            adapter = get_adapter(route.adapter)
            ctx = LaunchContext(
                kickoff="",
                cwd="",
                execution_id="report",
                session_id=_STUB_SESSION,
                parent_env=env,
                code_root="",
            )
            launch = adapter.build(route, ctx, binding)
            mat = materialize(binding, extra_dirs=[])
            probes = run_local_probes(
                binding,
                materialized=mat,
                argv=launch.argv,
                env=launch.env,
                present_paths=present_paths,
            )
        except RefusalError as exc:
            rows.append(
                _row(
                    route,
                    refusal_code=exc.code,
                    reason=exc.message,
                    binding_status=binding.status,
                )
            )
            continue

        failed = [p for p in probes if p.state == "fail"]
        common = {
            "binding_status": binding.status,
            "command_mode": binding.command_mode,
            "classes": binding.mechanism_summary(),
            "probes": {p.probe_id: p.state for p in probes},
            "residuals": {c.policy_class: c.residual for c in binding.classes if c.residual},
        }
        if failed:
            rows.append(
                _row(
                    route,
                    refusal_code="BINDING_PROBE_FAILED",
                    reason="; ".join(
                        f"{p.policy_class}/{p.probe_id}: {p.detail}" for p in failed
                    ),
                    **common,
                )
            )
            continue
        gate = _credential_gate(route)
        if gate is not None:
            rows.append(_row(route, refusal_code=gate[0], reason=gate[1], **common))
            continue
        rows.append(_row(route, state="launchable", **common))
    return rows


def routes_to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2, sort_keys=True)


def routes_to_text(rows: list[dict[str, Any]]) -> str:
    """The human form. Every row states its verdict first, on one line."""
    if not rows:
        return "no routes in the catalog."
    out: list[str] = []
    launchable = sum(1 for r in rows if r["state"] == "launchable")
    out.append(f"{len(rows)} route(s): {launchable} launchable, {len(rows) - launchable} refused")
    out.append("")
    for r in rows:
        verdict = "LAUNCHABLE" if r["state"] == "launchable" else f"REFUSED  [{r['refusal_code']}]"
        out.append(f"{verdict}  {r['route']}")
        out.append(
            f"    {r['model']} @ {r['harness']} "
            f"({r['billing_market']}/{r['credential_mode']}, pool {r['credential_pool']})"
        )
        out.append(f"    boundary: {r['binding_id']} {r['binding_digest'][:19]}… "
                   f"status {r['binding_status'] or 'n/a'}  mode {r['command_mode'] or 'n/a'}")
        for pc in ("P1", "P2", "P3", "P4"):
            mech = ", ".join(r["classes"].get(pc, [])) or "—"
            out.append(f"      {pc}: {mech}")
        deferred = [k for k, v in r["probes"].items() if v == "deferred"]
        failed = [k for k, v in r["probes"].items() if v == "fail"]
        passed = [k for k, v in r["probes"].items() if v == "pass"]
        if r["probes"]:
            line = f"      probes: {len(passed)} pass"
            if deferred:
                line += f", {len(deferred)} deferred ({', '.join(sorted(deferred))})"
            if failed:
                line += f", {len(failed)} FAIL ({', '.join(sorted(failed))})"
            out.append(line)
        for pc, residual in sorted(r["residuals"].items()):
            if residual:
                out.append(f"      {pc} residual: {residual}")
        if r["reason"]:
            out.append(f"    reason: {r['reason']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
