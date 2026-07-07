"""The gateway: the agent's sole, governed route to the log.

Policy in the gateway, structure in the store. The gateway sits above both the
store and the board; dependencies flow one way (gateway -> {events, board}).
Refusals are recorded values (Accepted | Rejected), never exceptions (§5).
"""

from .gateway import Gateway, GatewayHandle
from .policy import EMIT_AUTHORITY, Policy
from .result import Accepted, EmitResult, Rejected, unwrap

__all__ = [
    "Gateway",
    "GatewayHandle",
    "Policy",
    "EMIT_AUTHORITY",
    "Accepted",
    "Rejected",
    "EmitResult",
    "unwrap",
]
