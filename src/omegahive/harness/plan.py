"""Catalog + route + adapter -> one resolved, executable plan (or one named refusal).

This is the whole launch decision in one pure function. It takes bytes and context, and
returns either a `ResolvedPlan` or raises `RefusalError` with a stable code. It touches
no files, no clock, no network and no database, which is what makes the operator's
`--check` path genuinely free of side effects: the same code that launches also answers
"what would happen", because there is nothing else for it to do.

What it deliberately does NOT do, per the accepted runner doctrine (2026-08-20): call a
model, run a probe, consult a permission-boundary descriptor, check a promotion state,
or read a per-order approval file. Catalog presence plus `enabled: true` is the
authorization. The refusals left here are the cheap deterministic ones — malformed
configuration, an absent or ambiguous default or route, an unknown adapter name — plus
the one credential rule that is Hive's own to keep. Whether the named executable exists
is the launcher's check, because a pure function cannot stat anything; both `--check`
and the real launch make it through the same shell helper.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omegahive.events.types import ExecutionIdentity, PriceBasis
from omegahive.harness.adapters import LaunchContext, LaunchPlan, get_adapter
from omegahive.harness.records import (
    RouteEntry,
    catalog_digest,
    load_catalog,
    resolve_route,
)


@dataclass(frozen=True)
class ResolvedPlan:
    route: RouteEntry
    route_source: str            # "default" | "override" — provenance, not a judgment
    identity: ExecutionIdentity
    price_basis: PriceBasis | None
    catalog_digest: str
    runner_fingerprint: str
    execution_id: str
    task: str
    order_ref: str
    purpose: str
    attempt: int
    launch: LaunchPlan
    task_root: str
    run_dir: str
    cwd: str
    code_root: str


def execution_id_for(*, task: str, order_ref: str, purpose: str, attempt: int) -> str:
    """A stable id for (this task, this pinned order, this purpose, this attempt).

    Derived rather than random so that re-running the very same launch — a retried emit,
    a resumed supervisor, a `--check` followed by the real thing — names the SAME
    execution. A different attempt is a different execution by construction, which is
    what makes attempt-numbering meaningful instead of decorative.

    The old seed was the committed launch binding's ref. There is no binding any more,
    so the pinned order takes its place: the human `task.created` with its acceptance
    pin remains the approval act, and this id is derived from the same thing that act
    named.
    """
    seed = f"{order_ref}|{task}|{purpose}|{attempt}"
    short = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    # 128-char ceiling from the payload model; the task is the part worth truncating
    # because the suffix is what guarantees uniqueness.
    return f"{task[:100]}-a{attempt}-{short}"


def resolve(
    *,
    catalog_raw: bytes,
    route_name: str | None,
    task: str,
    order_ref: str,
    purpose: str = "work",
    attempt: int = 1,
    kickoff: str,
    task_root: str,
    cwd: str,
    code_root: str = "",
    run_dir: str = "",
    session_id: str,
    parent_env: Mapping[str, str],
) -> ResolvedPlan:
    """Resolve the catalog and build the plan. Raises `RefusalError` to refuse."""
    catalog = load_catalog(catalog_raw)
    route, route_source = resolve_route(catalog, route_name)
    adapter = get_adapter(route.adapter)

    ctx = LaunchContext(
        kickoff=kickoff,
        cwd=cwd,
        task_root=task_root,
        execution_id=execution_id_for(
            task=task, order_ref=order_ref, purpose=purpose, attempt=attempt
        ),
        session_id=session_id,
        parent_env=parent_env,
        code_root=code_root,
        run_dir=run_dir,
    )
    launch = adapter.build(route, ctx)

    return ResolvedPlan(
        route=route,
        route_source=route_source,
        identity=route.identity(),
        price_basis=route.price_basis,
        catalog_digest=catalog_digest(catalog_raw),
        runner_fingerprint=route.runner.fingerprint(),
        execution_id=ctx.execution_id,
        task=task,
        order_ref=order_ref,
        purpose=purpose,
        attempt=attempt,
        launch=launch,
        task_root=task_root,
        run_dir=run_dir,
        cwd=cwd,
        code_root=code_root,
    )


def _redact_argv(argv: list[str], kickoff: str, env: Mapping[str, str]) -> list[str]:
    """Prepare an argv for display: elide the kickoff, and never print an env VALUE.

    The kickoff is elided not because it is secret — it is not — but because it is a
    multi-hundred-character multi-line block, and a preflight an operator will not read
    is a preflight that does not check anything.

    The env-value substitution is the load-bearing half. `preflight_text` promises that
    environment values never appear, and it enforces that for the `env` block by
    printing names only — but a route's own argv may legitimately contain a value that
    also lives in the environment (a config home, a wrapper path). Redacting
    per-adapter would leave the invariant one new adapter away from being false, so it
    is enforced here instead: any argv element equal to an env value is shown as
    `<env:NAME>`, whatever produced it. Short values are left alone — a one- or
    two-character env value collides with ordinary flags by accident, and `<env:X>` in
    place of `auto` would be noise, not redaction.
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
        "task": plan.task,
        "purpose": plan.purpose,
        "attempt": plan.attempt,
        "order_ref": plan.order_ref,
        "catalog_digest": plan.catalog_digest,
        "identity": plan.identity.model_dump(mode="json"),
        "route_source": plan.route_source,
        "runner_fingerprint": plan.runner_fingerprint,
        "worker_io": plan.route.runner.worker_io,
        "executable": plan.route.runner.executable,
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
        "model_identity_evidence": plan.launch.model_identity_evidence,
        "usage_evidence": plan.launch.usage_evidence,
        "unproven_reason": plan.launch.unproven_reason,
        "task_root": plan.task_root,
        "run_dir": plan.run_dir,
        "cwd": plan.cwd,
        "code_root": plan.code_root,
    }


def route_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """The subset of the resolved runner that goes on the spine.

    Deliberately not the runner block itself: a launch fact is a durable public record,
    and what a capacity or audit reader needs is which route ran, whether the operator
    chose it or the catalog defaulted to it, and whether the runner configuration is the
    same one as last time. The fingerprint answers the third without publishing an
    operator's argv, and it is provenance rather than a posture verdict — nothing here
    claims the configuration is safe.
    """
    return {
        "route_source": doc["route_source"],
        "runner_fingerprint": doc["runner_fingerprint"],
        "worker_io": doc["worker_io"],
        "model_identity_evidence": doc["model_identity_evidence"],
        "usage_evidence": doc["usage_evidence"],
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
        f"order:       {doc['order_ref']}",
        f"catalog:     {doc['catalog_digest']}",
        f"route:       {ident['route']}   ({doc['route_source']})",
        f"  vendor:    {ident['model_vendor']}   provider: {ident['provider']}",
        f"  model:     {ident['model']}   (exact, from the catalog)",
        f"  harness:   {ident['harness']}   adapter: {ident['adapter']}",
        f"  billing:   {ident['billing_market']}   credential pool: {ident['credential_pool']}",
        f"runner:      {doc['executable']}   worker I/O: {doc['worker_io']}",
        f"  fingerprint {doc['runner_fingerprint']}",
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
        f"task root:   {doc['task_root']}",
        f"  code       {doc['code_root'] or '<none>'}",
        f"  workspace  {doc['cwd']}",
        f"  run-local  {doc['run_dir'] or '<none>'}",
        f"argv:        {doc['argv_redacted']}",
        f"env names:   {' '.join(doc['env_names']) or '<empty>'}   (values never printed)",
        f"version cmd: {' '.join(doc['version_argv'])}",
        f"model id:    {doc['model_identity_evidence']}"
        f"   usage: {doc['usage_evidence']}   extractor {doc['usage_extractor']}",
    ]
    if doc.get("unproven_reason"):
        lines.append(f"  caveat:    {doc['unproven_reason']}")
    return "\n".join(lines)
