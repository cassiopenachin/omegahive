"""Emit results — recorded values, never exceptions (§5).

The gateway returns one of these for every emit. Policy/legality refusals are
`Rejected` values carrying a machine code, a human reason, and the id of the
persisted `gateway.rejected` event — never a raise across the process boundary.
(Infrastructure faults still raise; those are not policy.)
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..events.envelope import Event


@dataclass(frozen=True)
class Accepted:
    event: Event


@dataclass(frozen=True)
class Rejected:
    code: str
    reason: str
    rejection_event_id: UUID


EmitResult = Accepted | Rejected


def unwrap(result: EmitResult) -> Event:
    """The accepted event, or raise if refused. For internal call sites that emit a
    known-legal event (plan emission, test setup) where a Rejected is a bug, not a
    control-flow value."""
    if isinstance(result, Rejected):
        raise RuntimeError(f"emit unexpectedly rejected: {result.code} {result.reason}")
    return result.event
