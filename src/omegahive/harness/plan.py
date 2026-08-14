"""Binding + catalog + adapter -> one resolved, executable plan (or one named refusal).

This is the whole approval path in one pure function. It takes bytes and context, and
returns either a `ResolvedPlan` or raises `RefusalError` with a stable code. It touches
no files, no clock, no network and no database, which is what makes the operator's
`--check` path genuinely free of side effects: the same code that launches also
answers "what would happen", because there is nothing else for it to do.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omegahive.events.types import ExecutionIdentity, PriceBasis
from omegahive.harness.adapters import LaunchContext, LaunchPlan, get_adapter
from omegahive.harness.records import (
    LaunchBinding,
    RefusalError,
    RouteEntry,
    catalog_digest,
    load_binding,
    load_catalog,
    resolve_route,
)

# Billing markets a worker may actually be launched on in this order. API routes
# validate structurally — the catalog can describe them and `--check` will resolve
# them — but launching one needs credential delivery and permission controls that
# `worker-harness-bindings` owns. Until then a launch attempt refuses rather than
# putting a key anywhere near a worker process.
LAUNCHABLE_MARKETS = frozenset({"subscription"})


@dataclass(frozen=True)
class ResolvedPlan:
    binding: LaunchBinding
    route: RouteEntry
    identity: ExecutionIdentity
    price_basis: PriceBasis | None
    catalog_digest: str
    execution_id: str
    binding_ref: str
    launch: LaunchPlan
    launchable: bool
    unlaunchable_reason: str | None


def execution_id_for(binding: LaunchBinding, binding_ref: str) -> str:
    """A stable id for (this binding, this attempt, this purpose).

    Derived rather than random so that re-running the very same approved launch — a
    retried emit, a resumed supervisor, a `--check` followed by the real thing — names
    the SAME execution. A different attempt is a different execution by construction,
    which is what makes attempt-numbering meaningful instead of decorative.
    """
    seed = f"{binding_ref}|{binding.task}|{binding.purpose}|{binding.attempt}"
    short = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    # 128-char ceiling from the payload model; the task is the part worth truncating
    # because the suffix is what guarantees uniqueness.
    task = binding.task[:100]
    return f"{task}-a{binding.attempt}-{short}"


def resolve(
    *,
    binding_raw: bytes,
    catalog_raw: bytes,
    binding_ref: str,
    expected_task: str | None,
    expected_order_ref: str | None,
    kickoff: str,
    cwd: str,
    session_id: str,
    parent_env: Mapping[str, str],
) -> ResolvedPlan:
    """Resolve, verify agreement, and build the plan. Raises `RefusalError` to refuse."""
    binding = load_binding(binding_raw)
    catalog = load_catalog(catalog_raw)

    # Agreement checks come before route resolution: a binding pointing at the wrong
    # task is wrong regardless of whether its route happens to exist, and the operator
    # should be told the specific disagreement rather than a downstream symptom.
    if expected_task is not None and binding.task != expected_task:
        raise RefusalError(
            "BINDING_TASK_MISMATCH",
            f"binding names task {binding.task!r} but this launch is for "
            f"{expected_task!r} — a binding is signed for one task",
        )
    if expected_order_ref is not None and binding.order_ref != expected_order_ref:
        raise RefusalError(
            "BINDING_ORDER_MISMATCH",
            f"binding pins order {binding.order_ref!r} but the launcher resolved "
            f"{expected_order_ref!r} — the order moved since the binding was signed; "
            "re-sign the binding against the current pin",
        )

    route = resolve_route(catalog, binding.route)
    adapter = get_adapter(route.adapter)

    ctx = LaunchContext(
        kickoff=kickoff,
        cwd=cwd,
        execution_id=execution_id_for(binding, binding_ref),
        session_id=session_id,
        parent_env=parent_env,
    )
    launch = adapter.build(route, ctx)

    launchable = route.billing_market in LAUNCHABLE_MARKETS
    unlaunchable_reason = None
    if not launchable:
        unlaunchable_reason = (
            f"route {route.name!r} bills against the {route.billing_market!r} market; "
            "direct-API routes are not launchable until `worker-harness-bindings` "
            "supplies credential delivery and permission controls"
        )

    return ResolvedPlan(
        binding=binding,
        route=route,
        identity=route.identity(),
        price_basis=route.price_basis,
        catalog_digest=catalog_digest(catalog_raw),
        execution_id=ctx.execution_id,
        binding_ref=binding_ref,
        launch=launch,
        launchable=launchable,
        unlaunchable_reason=unlaunchable_reason,
    )


def _redact_argv(argv: list[str], kickoff: str, env: Mapping[str, str]) -> list[str]:
    """Prepare an argv for display: elide the kickoff, and never print an env VALUE.

    The kickoff is elided not because it is secret — it is not — but because it is a
    multi-hundred-character multi-line block, and a preflight an operator will not read
    is a preflight that does not check anything.

    The env-value substitution is the load-bearing half. `preflight_text` promises that
    environment values never appear, and it enforces that for the `env` block by
    printing names only — but an adapter may legitimately place an env value INTO the
    argv (the fake adapter puts `HIVE_FAKE_HARNESS` at argv[0], and a future adapter
    could pass a config path the same way). Redacting per-adapter would leave the
    invariant one new adapter away from being false, so it is enforced here instead:
    any argv element equal to an env value is shown as `<env:NAME>`, whatever produced
    it. Short values are left alone — a one- or two-character env value collides with
    ordinary flags by accident, and `<env:X>` in place of `auto` would be noise, not
    redaction.
    """
    by_value = {v: k for k, v in sorted(env.items()) if len(v) > 3}
    out = []
    for a in argv:
        if kickoff and a == kickoff:
            out.append(f"<kickoff: {len(kickoff)} chars, {kickoff.count(chr(10)) + 1} lines>")
        elif a in by_value:
            out.append(f"<env:{by_value[a]}>")
        else:
            out.append(a)
    return out


def to_json(plan: ResolvedPlan, *, kickoff: str) -> dict[str, Any]:
    """The machine form the launcher and supervisor consume.

    `env` is carried in full because the supervisor must actually set it; `env_names`
    beside it is what the preflight renders. The separation is deliberate — one
    consumer needs the values, the other must never print them.
    """
    return {
        "ok": True,
        "execution_id": plan.execution_id,
        "task": plan.binding.task,
        "purpose": plan.binding.purpose,
        "attempt": plan.binding.attempt,
        "binding_ref": plan.binding_ref,
        "order_ref": plan.binding.order_ref,
        "catalog_digest": plan.catalog_digest,
        "predicted_total_tokens": plan.binding.predicted_total_tokens,
        "identity": plan.identity.model_dump(mode="json"),
        "price_basis": plan.price_basis.model_dump(mode="json") if plan.price_basis else None,
        "argv": plan.launch.argv,
        "argv_redacted": _redact_argv(plan.launch.argv, kickoff, plan.launch.env),
        "env": plan.launch.env,
        "env_names": sorted(plan.launch.env),
        "version_argv": plan.launch.version_argv,
        "model_requested": plan.launch.model_requested,
        "usage_extractor": plan.launch.usage_extractor,
        "usage_hint": plan.launch.usage_hint,
        "proves_model": plan.launch.proves_model,
        "proves_usage": plan.launch.proves_usage,
        "unproven_reason": plan.launch.unproven_reason,
        "launchable": plan.launchable,
        "unlaunchable_reason": plan.unlaunchable_reason,
    }


def preflight_text(doc: dict[str, Any]) -> str:
    """The redacted operator-facing preflight.

    Environment VALUES never appear here — only names. That rule is what lets this be
    printed into a terminal, a log, or a report without a second thought, and it is the
    same posture that made `compose config` a recorded incident twice.
    """
    ident = doc["identity"]
    lines = [
        f"execution:   {doc['execution_id']}  (purpose {doc['purpose']}, attempt {doc['attempt']})",
        f"task:        {doc['task']}",
        f"binding:     {doc['binding_ref']}",
        f"order:       {doc['order_ref']}",
        f"catalog:     {doc['catalog_digest']}",
        f"route:       {ident['route']}",
        f"  vendor:    {ident['model_vendor']}   provider: {ident['provider']}",
        f"  model:     {ident['model']}   (exact, from the catalog)",
        f"  harness:   {ident['harness']}   adapter: {ident['adapter']}",
        f"  billing:   {ident['billing_market']}   credential pool: {ident['credential_pool']}",
        f"predicted:   {doc['predicted_total_tokens']} total tokens",
    ]
    pb = doc.get("price_basis")
    if pb:
        lines.append(
            f"price basis: {pb['currency']} in/cr/cw/out per Mtok = "
            f"{pb['per_mtok_input']}/{pb['per_mtok_cache_read']}/"
            f"{pb['per_mtok_cache_write']}/{pb['per_mtok_output']}  "
            f"({pb['source']}, captured {pb['captured_at']})"
        )
    else:
        lines.append("price basis: none on this route (subscription-billed; cost is window weight)")
    lines += [
        f"argv:        {doc['argv_redacted']}",
        f"env names:   {' '.join(doc['env_names']) or '<empty>'}   (values never printed)",
        f"version cmd: {' '.join(doc['version_argv'])}",
        f"usage:       extractor {doc['usage_extractor']}"
        f"   proves model: {doc['proves_model']}   proves usage: {doc['proves_usage']}",
    ]
    if doc.get("unproven_reason"):
        lines.append(f"  caveat:    {doc['unproven_reason']}")
    if not doc["launchable"]:
        lines.append(f"NOT LAUNCHABLE: {doc['unlaunchable_reason']}")
    return "\n".join(lines)
