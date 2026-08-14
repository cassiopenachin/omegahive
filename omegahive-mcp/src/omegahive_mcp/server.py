"""The stdio MCP server: exactly two structured-output tools, `hive_portfolio` and
`hive_task` (hive-mcp order, scope item 5). No prompts, resources, sampling,
elicitation, write tool, or generic HTTP tool is registered — `build_server` never
calls `add_resource`/`add_prompt`, and the two `@server.tool()` calls below are the
only capability surface this module adds to a fresh `MCPServer`.

`server.run("stdio")` (called from `cli.py`, never here) makes stdout MCP-protocol-
only by construction — the SDK owns stdout for the JSON-RPC stream. Every log
statement in this module goes through `logging`, configured by the CLI entry point
to write to stderr, so a diagnostic line can never land on stdout and corrupt a
client's framing.
"""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .client import HiveApiClient, UpstreamError
from .schemas import PortfolioResponse, TaskDetailResponse

logger = logging.getLogger(__name__)

# Every tool this server registers is a GET, has no side effect, and reads a live,
# operator-owned system it does not control — the honest hint set for all three
# flags, not a per-tool judgment call.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

INSTRUCTIONS = (
    "Read-only observation of one live omegahive hive over the operator's tailnet. "
    "hive_portfolio lists active runs and tasks; hive_task returns one task's full "
    "detail and recent event timeline. Neither tool can assign, close, emit, or "
    "otherwise change hive state — there is no write path here."
)


def build_server(client: HiveApiClient) -> MCPServer:
    """Build the server around an already-configured `HiveApiClient` — the caller
    (`cli.py`) owns loading config and constructing that client exactly once per
    process, so a restart (scope item 9's "restart recovery" check) always starts
    from a fresh client with no state carried over."""
    server = MCPServer(name="omegahive-hive", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        name="hive_portfolio",
        description=(
            "The live hive portfolio: every active run and its active tasks, under "
            "the same active-window/exclusion rules the operator's board applies. "
            "Every response carries an observed-at time and, per run, a cursor and "
            "generation — treat two calls a minute apart as two independent "
            "snapshots, not a subscription."
        ),
        annotations=_READ_ONLY,
    )
    def hive_portfolio() -> PortfolioResponse:
        try:
            return client.portfolio()
        except UpstreamError as exc:
            logger.warning("hive_portfolio: %s", exc.code)
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    @server.tool(
        name="hive_task",
        description=(
            "One task's full detail: status, owner, blocker context, result "
            "provenance, and a bounded, newest-first event timeline. run_id and "
            "task_id come from hive_portfolio's output."
        ),
        annotations=_READ_ONLY,
    )
    def hive_task(run_id: str, task_id: str) -> TaskDetailResponse:
        try:
            return client.task(run_id, task_id)
        except UpstreamError as exc:
            logger.warning("hive_task(%s, %s): %s", run_id, task_id, exc.code)
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    return server
