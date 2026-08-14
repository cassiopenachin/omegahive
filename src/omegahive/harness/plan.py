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
from omegahive.harness.bindings import (
    HarnessBinding,
    Materialized,
    ProbeResult,
    check_argv,
    check_command_mode,
    check_coverage,
    check_digest,
    check_status,
    load_binding_descriptor,
    materialize,
    run_local_probes,
)
from omegahive.harness.records import (
    LaunchBinding,
    RefusalError,
    RouteEntry,
    catalog_digest,
    load_binding,
    load_catalog,
    resolve_route,
)

# Billing markets a worker may be launched on with the harness's own already-
# authenticated account and nothing handed to the worker. An `api` route bills against
# a long-lived provider secret, and the only shape in which such a route may launch is
# through an operator-owned broker that issues a scoped, expiring capability — see
# `BROKER_IMPLEMENTED` below. This set is NOT the api gate; the credential gate is,
# and keeping them separate is deliberate: "which markets exist" and "how a credential
# reaches an execution" are different questions and were conflated once already.
LAUNCHABLE_MARKETS = frozenset({"subscription"})

# The conditional broker slice of the `worker-harness-bindings` order did NOT activate.
# Its activation condition is a committed HIP-1 M1b/M1c disposition naming a direct-API
# bundle as a live qualifier; at kickoff (2026-08-14) `hip1-bench-seed` had landed with
# fidelity green and NO cheap-candidate qualification had run (`hip1-bench-qualify` is
# unworked, M1c is sequenced out of wave 4), so no direct-API route has earned live
# worker status. This constant is therefore False and the denial seam below is the whole
# credential story: broker status is `not implemented`, said plainly, rather than a stub
# that reads as a control. Flipping it is not a one-line change — it is the order that
# builds the broker.
BROKER_IMPLEMENTED = False


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
    # The permission boundary, resolved: which descriptor, its exact bytes' digest,
    # what it materialized, and how every declared probe stands right now.
    harness_binding: HarnessBinding
    materialized: Materialized
    probes: list[ProbeResult]
    # A stable code for the unlaunchable case, so a reader can branch on the reason
    # rather than matching prose. None when the route is launchable.
    refusal_code: str | None = None


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


def resolve_harness_binding(
    route: RouteEntry, descriptors_raw: Mapping[str, bytes]
) -> HarnessBinding:
    """Route -> the one descriptor it is pinned to, fully checked, or a refusal.

    Every check here fails CLOSED, and the order matters: identify the descriptor,
    prove its bytes are the approved ones, prove it is about this harness, prove it
    binds all four classes with something enforceable, and only then ask whether its
    probes have actually been run. Asking "is it proven" before "is it the right file"
    would let a proven descriptor vouch for a route it does not describe.
    """
    raw = descriptors_raw.get(route.binding_id)
    if raw is None:
        known = ", ".join(sorted(descriptors_raw)) or "<none>"
        raise RefusalError(
            "HARNESS_UNBOUND",
            f"route {route.name!r} names permission binding {route.binding_id!r} and no "
            f"such descriptor ships with this launcher (known: {known}). An unbound "
            "harness is not a warning — it is a launch that does not happen "
            "(permissions.md, 'an empty row is a launch that does not happen')",
        )
    binding = load_binding_descriptor(raw)
    check_digest(binding, raw, route.binding_digest)
    if binding.harness != route.harness:
        raise RefusalError(
            "HARNESS_BINDING_MISMATCH",
            f"route {route.name!r} runs harness {route.harness!r} but descriptor "
            f"{binding.binding_id!r} binds {binding.harness!r}; a boundary written for "
            "one harness says nothing about another",
        )
    check_coverage(binding)
    check_command_mode(binding, binding.command_mode)
    check_status(binding)
    return binding


def _credential_gate(route: RouteEntry) -> tuple[str, str] | None:
    """The credential seam. Returns `(code, reason)` when the route may not launch.

    Subscription routes inherit only the harness's own authenticated channel — the
    adapter's environment allowlist is what makes "inherit" mean one named channel
    rather than the parent shell — and hand the worker nothing. An `api` route bills
    against a long-lived provider secret, which must never be in the binding, the
    worker environment, the tmux command, the generated config, a transcript, an event,
    or a result artifact. There is exactly one shape in which such a route could launch,
    and it does not exist yet.
    """
    if route.billing_market == "api":
        if route.credential_mode != "broker":
            return (
                "ROUTE_CREDENTIAL_MODE",
                f"route {route.name!r} bills against the api market with "
                f"credential_mode={route.credential_mode!r}. A direct-API route is not "
                "launchable on the harness's own credentials: the long-lived provider "
                "secret must stay outside the worker, which needs credential_mode="
                "'broker' and a live broker",
            )
        if not BROKER_IMPLEMENTED:
            return (
                "BROKER_NOT_IMPLEMENTED",
                f"route {route.name!r} requests credential_mode='broker' and no broker "
                "is implemented on this deployment. The broker slice is conditional on a "
                "committed HIP-1 qualification naming a direct-API bundle as a live "
                "qualifier; none exists, so the honest status is 'not implemented' and "
                "the route refuses rather than falling back to a raw key",
            )
    elif route.credential_mode == "broker":
        return (
            "ROUTE_CREDENTIAL_MODE",
            f"route {route.name!r} bills against the subscription market and asks for a "
            "broker. A subscription route uses the harness's existing authenticated "
            "account and has no credential for a broker to scope; this combination is a "
            "catalog mistake, not a configuration",
        )
    if route.billing_market not in LAUNCHABLE_MARKETS:
        return (
            "MARKET_UNLAUNCHABLE",
            f"route {route.name!r} bills against the {route.billing_market!r} market, "
            "which no launch path on this deployment supports",
        )
    return None


def resolve(
    *,
    binding_raw: bytes,
    catalog_raw: bytes,
    descriptors_raw: Mapping[str, bytes],
    binding_ref: str,
    expected_task: str | None,
    expected_order_ref: str | None,
    kickoff: str,
    cwd: str,
    session_id: str,
    code_root: str = "",
    parent_env: Mapping[str, str],
    present_paths: frozenset[str] = frozenset(),
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
    harness_binding = resolve_harness_binding(route, descriptors_raw)

    ctx = LaunchContext(
        kickoff=kickoff,
        cwd=cwd,
        execution_id=execution_id_for(binding, binding_ref),
        session_id=session_id,
        parent_env=parent_env,
        code_root=code_root,
    )
    launch = adapter.build(route, ctx, harness_binding)

    # Materialize BEFORE checking the argv, because on a harness whose boundary is a
    # file the argv's job is to make the child read that file — the two are one control
    # and verifying either alone verifies nothing.
    extra_dirs = [code_root] if code_root else []
    mat = materialize(harness_binding, extra_dirs=extra_dirs)
    check_argv(harness_binding, launch.argv)

    probes = run_local_probes(
        harness_binding,
        materialized=mat,
        argv=launch.argv,
        env=launch.env,
        present_paths=present_paths,
    )
    failed = [p for p in probes if p.state == "fail"]
    if failed:
        detail = "; ".join(f"{p.policy_class}/{p.probe_id}: {p.detail}" for p in failed)
        raise RefusalError(
            "BINDING_PROBE_FAILED",
            f"{harness_binding.binding_id}: {len(failed)} preflight probe(s) failed — "
            f"{detail}. The configuration was materialized and does not hold; a launch "
            "here would be a worker running outside the boundary that was approved",
        )

    gate = _credential_gate(route)
    launchable = gate is None
    refusal_code, unlaunchable_reason = (None, None) if gate is None else gate

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
        harness_binding=harness_binding,
        materialized=mat,
        probes=probes,
        refusal_code=refusal_code,
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
        "refusal_code": plan.refusal_code,
        # The boundary, in the form the launcher writes and the supervisor re-checks.
        # `content` is carried because the launcher must actually write the file;
        # `digest` is what makes a later "is this still the approved boundary" answerable
        # without re-reading it, and it is what the supervisor compares before starting.
        "binding": {
            "binding_id": plan.harness_binding.binding_id,
            "binding_digest": plan.route.binding_digest,
            "harness": plan.harness_binding.harness,
            "status": plan.harness_binding.status,
            "command_mode": plan.harness_binding.command_mode,
            # Carried so the supervisor can re-check the vector it is about to exec
            # against the descriptor's own requirement, rather than against a second
            # copy of that requirement written in shell.
            "required_flags": plan.harness_binding.required_flags,
            "mechanisms": plan.harness_binding.mechanism_summary(),
            "residuals": {
                c.policy_class: c.residual
                for c in plan.harness_binding.classes
                if c.residual
            },
            "config_path": plan.materialized.path,
            "config_content": plan.materialized.content,
            "config_digest": plan.materialized.digest,
            "rules": plan.materialized.rules,
            "probes": [p.model_dump(mode="json") for p in plan.probes],
        },
    }


def binding_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """The subset of the binding that goes on the spine.

    Deliberately NOT the whole block: `config_content` is the materialized file and the
    execution facts are a public record. What a capacity or audit reader needs is which
    boundary ran and whether it held — the id, the two digests, the mechanism per class,
    and the probe verdicts — never the file itself and never a value out of anyone's
    environment or settings.
    """
    b = doc["binding"]
    return {
        "binding_id": b["binding_id"],
        "binding_digest": b["binding_digest"],
        "config_digest": b["config_digest"],
        "command_mode": b["command_mode"],
        "mechanisms": b["mechanisms"],
        "probes": {p["probe_id"]: p["state"] for p in b["probes"]},
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

    b = doc["binding"]
    lines += [
        f"boundary:    {b['binding_id']}  status {b['status']}  mode {b['command_mode']}",
        f"  descriptor {b['binding_digest']}",
        f"  config     {b['config_path'] or '<argv only>'}  {b['config_digest']}",
    ]
    for pc in ("P1", "P2", "P3", "P4"):
        mech = ", ".join(b["mechanisms"].get(pc, [])) or "UNBOUND"
        states = [p["state"] for p in b["probes"] if p["policy_class"] == pc]
        # A `deferred` probe is shown as deferred, never folded into the pass count.
        # The expensive half of the evidence lives in the descriptor's verification
        # record, and a summary that hides its absence is how "we configured it" starts
        # reading as "the boundary holds".
        tally = f"{states.count('pass')} pass"
        if states.count("deferred"):
            tally += f", {states.count('deferred')} deferred to the probe runner"
        if states.count("fail"):
            tally += f", {states.count('fail')} FAIL"
        lines.append(f"  {pc}: {mech}   [{tally}]")
        residual = b["residuals"].get(pc)
        if residual:
            lines.append(f"      residual: {residual[:150]}")
    if not doc["launchable"]:
        lines.append(f"NOT LAUNCHABLE [{doc['refusal_code']}]: {doc['unlaunchable_reason']}")
    return "\n".join(lines)
