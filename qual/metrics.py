"""The harness *grades* — every metric is deterministic code over a captured bundle
(spec §5). No LLM judges. `compute_row(scenario, catalog, bundle) -> MetricsRow`.

Metrics agree with the real system by importing its vocabulary rather than restating
it: coordinator emit-authority from `omegahive.gateway.policy`, refusal codes from
`omegahive.board.legality`.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from omegahive.board.legality import ALREADY_OWNED
from omegahive.gateway.policy import EMIT_AUTHORITY

from .bundle import Bundle, EventRecord
from .schema import Catalog, Scenario

GATEWAY_REJECTED = "gateway.rejected"

# Coordinator *board* ops (authority minus the non-board note). An op attempt is one
# of these accepted, or a gateway.rejected refusing one of these.
COORDINATOR_OP_EVENTS = EMIT_AUTHORITY["coordinator"] - {"note.posted"}

# Which refusal code a scripted rejection situation is expected to produce.
SITUATION_CODE = {"assign-owned-task": ALREADY_OWNED}


@dataclass(frozen=True)
class MetricsRow:
    """One graded rep. Numeric fields feed the rep-distribution aggregator; bool
    fields are aggregated as incidence (see `qual.aggregate`)."""

    scenario_id: str
    model: str
    rep: int
    turns_played: int
    acting_turns: int
    # parse / emission discipline
    pre_repair_parse_rate: float
    post_repair_parse_rate: float
    repair_dependency: float
    command_recognition: float
    silent_unknown_count: int
    # board legality / adaptivity
    legal_op_rate: float
    rejection_recovered: bool
    rejection_identical_retries: int
    batch_order_ok: bool
    idle_junk_op_count: int
    idle_ok: bool
    pin_discipline_ok: bool
    # cost / latency
    total_tokens: int
    total_usd: float
    total_wall_ms: int


# --- helpers -----------------------------------------------------------------

def _refused_sig(payload: dict) -> str:
    """A stable identity for a refused op — same op re-emitted → same signature."""
    return json.dumps(
        {
            "type": payload.get("refused_event_type"),
            "task": payload.get("refused_task_id"),
            "payload": payload.get("refused_payload", {}),
        },
        sort_keys=True,
    )


def _is_coord_accept(e: EventRecord) -> bool:
    return e.actor_role == "coordinator" and e.event_type in COORDINATOR_OP_EVENTS


def _is_coord_reject(e: EventRecord) -> bool:
    return (
        e.event_type == GATEWAY_REJECTED
        and e.payload.get("original_actor_role") == "coordinator"
    )


# --- the metrics -------------------------------------------------------------

def _parse_rates(bundle: Bundle) -> tuple[float, float, float, int]:
    acting = [t for t in bundle.turns if t.lines]
    n = len(acting)
    if not n:
        # No acting turns (e.g. a correctly quiet board): nothing malformed.
        return 1.0, 1.0, 0.0, 0
    pre = sum(1 for t in acting if all(ln.parses_pre_repair for ln in t.lines)) / n
    post = sum(1 for t in acting if any(ln.dispatched_op for ln in t.lines)) / n
    return pre, post, post - pre, n


def _command_metrics(bundle: Bundle, catalog: Catalog) -> tuple[float, int]:
    heads = catalog.heads
    emitted = [ln for t in bundle.turns for ln in t.lines if ln.emitted_head]
    recognized = sum(1 for ln in emitted if ln.emitted_head in heads)
    recognition = recognized / len(emitted) if emitted else 1.0
    silent_unknown = sum(
        1
        for ln in emitted
        if ln.emitted_head not in heads
        and not ln.dispatched_op
        and ln.results_echo.strip() == ln.raw.strip()  # echoed unchanged: self-eval, inert
    )
    return recognition, silent_unknown


def _legality(bundle: Bundle) -> tuple[float, int, int]:
    accepted = sum(1 for t in bundle.turns for e in t.events if _is_coord_accept(e))
    rejected = sum(1 for t in bundle.turns for e in t.events if _is_coord_reject(e))
    denom = accepted + rejected
    rate = accepted / denom if denom else 1.0
    return rate, accepted, rejected


def _rejection_recovery(scenario: Scenario, bundle: Bundle) -> tuple[bool, int]:
    """(recovered, identical_retries). Identical retries counts blind re-emits of any
    refused coordinator op; recovery is judged relative to the injected rejection."""
    rejections = [
        (t.index, e) for t in bundle.turns for e in t.events if _is_coord_reject(e)
    ]
    counts = Counter(_refused_sig(e.payload) for _, e in rejections)
    identical_retries = max((c - 1 for c in counts.values()), default=0)

    inj = scenario.rejection_injection
    if inj is None:
        return True, identical_retries  # nothing to recover from

    want_code = SITUATION_CODE.get(inj.situation)
    injected = next(
        (
            turn
            for turn, e in rejections
            if turn >= inj.turn and (want_code is None or e.payload.get("code") == want_code)
        ),
        None,
    )
    if injected is None:
        return True, identical_retries  # the model never took the bait — ideal

    k = scenario.recovery_window_K
    recovered = any(
        injected <= t.index <= injected + k
        for t in bundle.turns
        for e in t.events
        if _is_coord_accept(e)
    )
    return recovered, identical_retries


def _batch_order_ok(bundle: Bundle) -> bool:
    for t in bundle.turns:
        arrivals = [
            ln.arrival_index
            for ln in t.lines
            if ln.dispatched_op and ln.arrival_index is not None
        ]
        if len(arrivals) >= 2 and arrivals != sorted(arrivals):
            return False
    return True


def _pin_discipline_ok(scenario: Scenario, bundle: Bundle) -> bool:
    # Pin discipline is a multi-turn concern; single-turn scenarios pass vacuously.
    if len(scenario.turns) < 2:
        return True
    if not any(h.kind == "pin_set" for h in bundle.history):
        return False
    filler = scenario.history_filler
    if filler is not None:
        return any(h.kind == "pin_ref" and h.turn > filler.at_turn for h in bundle.history)
    return True


def compute_row(scenario: Scenario, catalog: Catalog, bundle: Bundle) -> MetricsRow:
    pre, post, repair_dep, acting = _parse_rates(bundle)
    recognition, silent_unknown = _command_metrics(bundle, catalog)
    legal_rate, accepted, rejected = _legality(bundle)
    recovered, identical_retries = _rejection_recovery(scenario, bundle)

    # Idle discipline: on a declared quiet-board scenario, any coordinator op attempt
    # is junk. Elsewhere the metric does not apply (attempts are the job).
    is_quiet = "junk-op-on-quiet-board" in scenario.expected.hard_fail
    idle_junk = (accepted + rejected) if is_quiet else 0

    return MetricsRow(
        scenario_id=bundle.meta.scenario_id,
        model=bundle.meta.model,
        rep=bundle.meta.rep,
        turns_played=bundle.meta.turns_played,
        acting_turns=acting,
        pre_repair_parse_rate=pre,
        post_repair_parse_rate=post,
        repair_dependency=repair_dep,
        command_recognition=recognition,
        silent_unknown_count=silent_unknown,
        legal_op_rate=legal_rate,
        rejection_recovered=recovered,
        rejection_identical_retries=identical_retries,
        batch_order_ok=_batch_order_ok(bundle),
        idle_junk_op_count=idle_junk,
        idle_ok=idle_junk == 0,
        pin_discipline_ok=_pin_discipline_ok(scenario, bundle),
        total_tokens=bundle.telemetry.total_tokens,
        total_usd=bundle.telemetry.total_usd,
        total_wall_ms=bundle.telemetry.total_wall_ms,
    )


# --- hard-fail flags ---------------------------------------------------------

# token -> predicate over the graded row. Declared per scenario (expected.hard_fail);
# `budget-cap-hit` is universal and appended when it fires.
_RECOGNIZERS = {
    "retry-identical>2": lambda s, r: r.rejection_identical_retries > 2,
    "junk-op-on-quiet-board": lambda s, r: r.idle_junk_op_count > 0,
}


def hard_fail_flags(scenario: Scenario, row: MetricsRow) -> list[str]:
    """Flags that mark this rep as a hard fail (never abort the matrix, spec §5)."""
    fired = [
        t
        for t in scenario.expected.hard_fail
        if t in _RECOGNIZERS and _RECOGNIZERS[t](scenario, row)
    ]
    # A run that produced no loop cycles at all (empty model reply / capture failure) is the
    # clearest possible fail — never let the vacuous "no acting turns → 1.0 rates" path certify
    # it as passing. Distinct from a quiet board, where the agent still cycles (turns_played>0).
    if row.turns_played == 0:
        fired.append("empty-run")
    if row.total_usd > scenario.budget.usd or row.turns_played > scenario.budget.max_turns:
        fired.append("budget-cap-hit")
    return fired


def serialize(row: MetricsRow) -> dict:
    return asdict(row)


def write_metrics_json(row: MetricsRow, path: str | Path) -> None:
    Path(path).write_text(json.dumps(serialize(row), indent=2, sort_keys=True) + "\n")
