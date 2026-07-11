"""The port: one binding surface (read/emit) for every coordinator and executor (§2).

Wire types (Op union, PortView) are here; Accepted/Rejected are reused from the gateway.
The substrate imports none of the sim; the port imports none of the sim either.
"""

from ..gateway import Accepted, Rejected
from .errors import PortInfraError
from .keys import BasisStore, derive_key
from .port import HiveCoordinatorPort, open_run
from .render import is_coordinator_rejection, render_view
from .wire import (
    AssignOp,
    BatchOp,
    CloseOp,
    EscalateOp,
    Op,
    PortView,
    PruneOp,
    RawOp,
    ReassignOp,
    ReopenOp,
)

__all__ = [
    "HiveCoordinatorPort",
    "open_run",
    "PortView",
    "PortInfraError",
    "Accepted",
    "Rejected",
    "Op",
    "AssignOp",
    "ReassignOp",
    "EscalateOp",
    "CloseOp",
    "ReopenOp",
    "PruneOp",
    "RawOp",
    "BatchOp",
    "derive_key",
    "BasisStore",
    "render_view",
    "is_coordinator_rejection",
]
