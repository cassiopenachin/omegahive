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


def _cause(board: Board, tid: str) -> UUID | None:
    ts = board.tasks.get(tid)
    return ts.last_causing_event_id if ts is not None else None


def _to_emit(head: str, args: list[str], board: Board) -> Emit | None:
    tid = args[0]
    cause = _cause(board, tid)
    if head == "assign":
        return Emit("task.assigned", {"worker": args[1]}, task_id=tid, causation_id=cause)
    if head == "reassign":
        owner = board.tasks[tid].owner if tid in board.tasks else None
        if owner is None:
            return None   # nothing to reassign from
        return Emit("task.reassigned", {"from": owner, "to": args[1], "reason": None},
                    task_id=tid, causation_id=cause)
    if head == "escalate":
        return Emit("task.escalated", {"reason": "coordinator escalation"},
                    task_id=tid, causation_id=cause)
    if head == "close":
        return Emit("task.status_override", {"status": "done"}, task_id=tid, causation_id=cause)
    if head == "reopen":
        return Emit("task.status_override", {"status": "reopened"}, task_id=tid, causation_id=cause)
    if head == "prune":
        return Emit("task.pruned", {"reason": None}, task_id=tid, causation_id=cause)
    return None


def parse_commands(text: str, board: Board, catalog: Catalog) -> ParseResult:
    arity = {e.head: e.arity for e in catalog.entries}
    emits: list[Emit] = []
    skipped: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()   # tolerate list-marker prefixes
        if not line or line[0] in "#;":
            continue
        toks = line.split()
        head = toks[0].lower()
        args = toks[1:]
        if head not in arity:
            skipped.append((raw, f"unknown head {head!r}"))
            continue
        if len(args) != arity[head]:
            skipped.append((raw, f"{head} expects {arity[head]} arg(s), got {len(args)}"))
            continue
        emit = _to_emit(head, args, board)
        if emit is None:
            skipped.append((raw, f"could not build emit for {head!r} on {args[0]!r}"))
            continue
        emits.append(emit)
    return ParseResult(emits=emits, skipped=skipped)
