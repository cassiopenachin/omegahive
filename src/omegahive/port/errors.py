"""Port infrastructure faults — raised only after §2a same-key retry is exhausted.

Policy/legality refusals are never exceptions (they are Rejected values); a raised
PortInfraError means "retry exhausted", not "unknown outcome".
"""

from __future__ import annotations


class PortInfraError(Exception):
    """An infrastructure fault (connection loss, etc.) that survived bounded retry."""
