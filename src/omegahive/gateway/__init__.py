"""The gateway: the agent's sole, governed route to the log.

Policy in the gateway, structure in the store. The gateway sits above both the
store and the board; dependencies flow one way (gateway -> {events, board}).
"""

from .gateway import Gateway, GatewayHandle
from .policy import EMIT_AUTHORITY, EmitDenied, Policy, TransitionRejected

__all__ = [
    "Gateway",
    "GatewayHandle",
    "Policy",
    "EMIT_AUTHORITY",
    "EmitDenied",
    "TransitionRejected",
]
