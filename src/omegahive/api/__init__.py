"""The versioned, read-only JSON API (§ hive-mcp order, scope item 2).

Same origin as the operator UI, same port read path, same `report.portfolio` cuts —
this package adds a machine-shaped view over facts the UI already renders. No write
route lives here or ever will: every function in this package takes a connection or a
port and returns data, never accepts one.
"""

from __future__ import annotations

from .routes import API_SCHEMA_VERSION, build_api_router

__all__ = ["build_api_router", "API_SCHEMA_VERSION"]
