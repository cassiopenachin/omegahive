"""Port wire types (§2): the closed Op union the client sequences, and the PortView
read envelope. Op is pydantic (parsed from the binding client's canonicalized decision);
each op knows how to render itself as a (event_type, payload, task_id) emit. Board and
Event stay dataclasses/models — rendering (S-expression, prose) happens in the binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..board.state import Board
from ..events.envelope import Event


class _Op(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    task_id: str
    # provenance: the event this decision cites (the board's last_causing_event_id for the
    # task). Metadata, not part of the idempotency key (which keys only op_type + payload).
    causation_id: UUID | None = None

    def to_emit(self) -> tuple[str, dict, str | None]:
        raise NotImplementedError


class AssignOp(_Op):
    op: Literal["assign"] = "assign"
    worker: str

    def to_emit(self) -> tuple[str, dict, str | None]:
        return "task.assigned", {"worker": self.worker}, self.task_id


class ReassignOp(_Op):
    op: Literal["reassign"] = "reassign"
    from_: str = Field(alias="from")
    to: str
    reason: str | None = None

    def to_emit(self) -> tuple[str, dict, str | None]:
        return "task.reassigned", {"from": self.from_, "to": self.to, "reason": self.reason}, \
            self.task_id


class EscalateOp(_Op):
    op: Literal["escalate"] = "escalate"
    reason: str

    def to_emit(self) -> tuple[str, dict, str | None]:
        return "task.escalated", {"reason": self.reason}, self.task_id


class CloseOp(_Op):
    op: Literal["close"] = "close"
    reason: str | None = None

    def to_emit(self) -> tuple[str, dict, str | None]:
        return "task.status_override", {"status": "done", "reason": self.reason}, self.task_id


class ReopenOp(_Op):
    op: Literal["reopen"] = "reopen"
    reason: str | None = None

    def to_emit(self) -> tuple[str, dict, str | None]:
        return "task.status_override", {"status": "reopened", "reason": self.reason}, self.task_id


class PruneOp(_Op):
    op: Literal["prune"] = "prune"
    reason: str | None = None

    def to_emit(self) -> tuple[str, dict, str | None]:
        return "task.pruned", {"reason": self.reason}, self.task_id


# The closed op vocabulary (plan ops stay behind a flag until needed — §2).
Op = Annotated[
    AssignOp | ReassignOp | EscalateOp | CloseOp | ReopenOp | PruneOp,
    Field(discriminator="op"),
]


class BatchOp(BaseModel):
    """An ordered list of ops the client sequences explicitly — for agents whose
    runtimes don't guarantee evaluation order of a turn's calls. Each member is keyed
    independently; occ disambiguates identical members within the batch (§3a)."""
    ops: list[Op]


@dataclass
class PortView:
    """Board + events + cursor + generation, all anchored to one log point S (§2).
    board is the server's fold of the full run prefix up to S; events is (cursor, S].
    A no-change read returns changed=False with board=None and no fold performed."""
    cursor: int
    generation: int | None
    events: list[Event]
    board: Board | None
    changed: bool = True
    generation_mismatch: bool = False
