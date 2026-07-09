"""The R1 board view (stage 2 §4.1): a deterministic S-expression the LLM coordinator
reads each turn — tasks/states/dependencies, per-branch attempt outcomes, and *this
actor's* rejections since the last view. Rejections are not board state; they are
`gateway.rejected` events in the log, so the renderer folds them in from the event delta
(that is how a refusal "outlives the turn"). Ordering is fully sorted for determinism.

The `(workers …)` section (§6) is sourced from the fold's roster (`board.roster`), never
from an environment-supplied list — the roster is board state now (`worker.registered`
events), so a coordinator that sees only the view can still name every valid assign
target. Emitted on every view, fresh boards included.
"""

from __future__ import annotations

from omegahive.board.state import Board
from omegahive.events.envelope import Event

# statuses in which a worker's ownership counts as "busy" for the workers section.
_BUSY_STATUSES = {"assigned", "in_progress", "blocked", "in_review"}

# refused event_type -> the command head the coordinator emitted (for echoing rejections
# back in the vocabulary the agent speaks). status_override splits on its status.
_HEAD_FOR_EVENT = {
    "task.assigned": "assign",
    "task.reassigned": "reassign",
    "task.escalated": "escalate",
    "task.pruned": "prune",
}


def _refused_head(event_type: str, payload: dict) -> str:
    if event_type == "task.status_override":
        return "reopen" if payload.get("status") == "reopened" else "close"
    return _HEAD_FOR_EVENT.get(event_type, event_type)


def is_coordinator_rejection(event: Event, actor_id: str) -> bool:
    """A gateway.rejected event recording a refusal of *this* coordinator's op. Single source
    of truth shared by the view (what to echo) and the delta gate (when to re-provoke)."""
    p = event.payload
    return (event.event_type == "gateway.rejected"
            and p.get("original_actor_role") == "coordinator"
            and p.get("original_actor_id") == actor_id)


def _my_rejections(events: list[Event], actor_id: str) -> list[str]:
    out: list[str] = []
    for e in events:
        if not is_coordinator_rejection(e, actor_id):
            continue
        p = e.payload
        head = _refused_head(p.get("refused_event_type", "?"), p.get("refused_payload") or {})
        task = p.get("refused_task_id") or "-"
        out.append(f"  (rejected (op {head} {task}) :code {p.get('code', '?')})")
    return out


def _workers_section(board: Board) -> str:
    busy: dict[str, str] = {}   # worker id -> the task it owns (first found, sorted below)
    for tid in sorted(board.tasks):
        ts = board.tasks[tid]
        if ts.owner and ts.status in _BUSY_STATUSES and ts.owner not in busy:
            busy[ts.owner] = tid
    entries = " ".join(
        f"({wid} :busy {busy[wid]})" if wid in busy else f"({wid} :idle)"
        for wid in sorted(board.roster)
    )
    return f"  (workers {entries})"


def render_view(board: Board, events: list[Event], *, actor_id: str = "coordinator",
                notes: list[str] | None = None) -> str:
    """Render the board (+ this actor's rejections in `events`, + any `notes`) as an
    S-expression. `notes` carries feedback the log cannot — e.g. lines the parser dropped
    last turn — so a malformed op gets the same corrective echo a gateway refusal does."""
    lines = ["(board", _workers_section(board)]
    for tid in sorted(board.tasks):
        ts = board.tasks[tid]
        deps = " ".join(sorted(ts.depends_on)) or "-"
        tried = " ".join(sorted(ts.tried_by)) or "-"
        lines.append(
            f"  (task {tid} :status {ts.status} :owner {ts.owner or 'none'} "
            f":deps ({deps}) :ready-when {ts.ready_when if ts.ready_when else 'all'} "
            f":review {ts.latest_review or 'none'} :pruned {'yes' if ts.pruned else 'no'} "
            f":tried ({tried}))"
        )
    lines.extend(_my_rejections(events, actor_id))
    lines.extend(notes or [])
    lines.append(")")
    return "\n".join(lines)
