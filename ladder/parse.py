"""Parse an R1 LLM completion (command lines) into engine `Emit`s (stage 2 §5.1). One op
per line, positional args; a line with an unknown head, wrong arity, or an unbuildable op
is *skipped* (surfaced in `ParseResult.skipped`), never raised — a malformed line must not
crash the run. Emits (not `wire.Op`) because the ladder's `drive` wraps each Emit in
`_RawOp`; each carries the board's `last_causing_event_id` for the task as its causation.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from qual.schema import Catalog

from omegahive.board.state import Board
from omegahive.sim.engine.protocol import Emit


@dataclass(frozen=True)
class ParseResult:
    emits: list[Emit]
    skipped: list[tuple[str, str]]   # (raw line, reason) — surfaced, never fatal


# markdown / punctuation a model may wrap tokens in (bold, code, list markers, trailing
# stops) — stripped from each token edge so `**assign A w1**` / `- assign A w1.` still parse.
# Task/worker ids are alphanumeric, so stripping these edge chars never corrupts an id.
_MARKUP = "`*_-.,;:!?()[]{}\"'"


def _cause(board: Board, tid: str) -> UUID | None:
    ts = board.tasks.get(tid)
    return ts.last_causing_event_id if ts is not None else None


def _to_emit(head: str, args: list[str], board: Board,
             roster: frozenset[str] | None) -> tuple[Emit | None, str]:
    """Return (emit, reason). emit is None when the op cannot be built; reason explains why
    (surfaced back to the model)."""
    tid = args[0]
    cause = _cause(board, tid)
    if head in ("assign", "reassign") and roster is not None and args[1] not in roster:
        return None, f"worker {args[1]!r} is not in the roster {sorted(roster)}"
    if head == "assign":
        return Emit("task.assigned", {"worker": args[1]}, task_id=tid, causation_id=cause), ""
    if head == "reassign":
        owner = board.tasks[tid].owner if tid in board.tasks else None
        if owner is None:
            return None, f"{tid!r} has no current owner to reassign from"
        return Emit("task.reassigned", {"from": owner, "to": args[1], "reason": None},
                    task_id=tid, causation_id=cause), ""
    if head == "escalate":
        return Emit("task.escalated", {"reason": "coordinator escalation"},
                    task_id=tid, causation_id=cause), ""
    if head == "close":
        return Emit("task.status_override", {"status": "done"}, task_id=tid, causation_id=cause), ""
    if head == "reopen":
        return Emit("task.status_override", {"status": "reopened"},
                    task_id=tid, causation_id=cause), ""
    if head == "prune":
        return Emit("task.pruned", {"reason": None}, task_id=tid, causation_id=cause), ""
    return None, f"no emit for head {head!r}"


def parse_commands(text: str, board: Board, catalog: Catalog,
                   roster: frozenset[str] | None = None) -> ParseResult:
    arity = {e.head: e.arity for e in catalog.entries}
    emits: list[Emit] = []
    skipped: list[tuple[str, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped[0] in "#;":   # blank / comment line
            continue
        toks = [t for t in (tok.strip(_MARKUP) for tok in stripped.split()) if t]
        if not toks:
            continue
        head = toks[0].lower()
        args = toks[1:]
        if head not in arity:
            skipped.append((raw, f"unknown head {head!r}"))
            continue
        if len(args) != arity[head]:
            skipped.append((raw, f"{head} expects {arity[head]} arg(s), got {len(args)}"))
            continue
        emit, reason = _to_emit(head, args, board, roster)
        if emit is None:
            skipped.append((raw, reason))
            continue
        emits.append(emit)
    return ParseResult(emits=emits, skipped=skipped)
