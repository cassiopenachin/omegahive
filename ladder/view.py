"""Compatibility shim. The board-view renderer moved to `omegahive.port.render` so both the R1
vanilla harness and the OmegaClaw fork's board adapter share one implementation (identical views
for the ladder comparison). Import from `omegahive.port.render` in new code.
"""

from __future__ import annotations

from omegahive.port.render import is_coordinator_rejection, render_view

__all__ = ["is_coordinator_rejection", "render_view"]
