"""The R1 board view (stage 2 §4.1): a deterministic S-expression the LLM coordinator
reads each turn — tasks/states/dependencies, per-branch attempt outcomes, and *this
actor's* rejections since the last view. Rejections are not board state; they are
`gateway.rejected` events in the log, so the renderer folds them in from the event delta
(that is how a refusal "outlives the turn"). Ordering is fully sorted for determinism.
"""

from __future__ import annotations

from omegahive.board.state import Board
from omegahive.events.envelope import Event

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


def _my_rejections(events: list[Event], actor_id: str) -> list[str]:
    out: list[str] = []
    for e in events:
        if e.event_type != "gateway.rejected":
            continue
        p = e.payload
        if p.get("original_actor_role") != "coordinator" or p.get("original_actor_id") != actor_id:
            continue
        head = _refused_head(p.get("refused_event_type", "?"), p.get("refused_payload") or {})
        task = p.get("refused_task_id") or "-"
        out.append(f"  (rejected (op {head} {task}) :code {p.get('code', '?')})")
    return out


def render_view(board: Board, events: list[Event], *, actor_id: str = "coordinator") -> str:
    """Render the board (+ this actor's rejections in `events`) as an S-expression."""
    lines = ["(board"]
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
    lines.append(")")
    return "\n".join(lines)
