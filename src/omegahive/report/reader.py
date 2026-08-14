"""The shared read-service seam (hive-mcp order, scope item 1).

One place that turns `(factory, run_id, cursor, generation)` into a `PortView`,
safely closing whatever connection the port holds — extracted from the HTML UI's
`ui.app` so the UI and the new versioned JSON API call the same function rather
than each owning a copy of "read a port and close its connection after."

This module owns no fold, active-window, run-discovery, visibility, cursor, or
restore semantics of its own — it only wires the existing `HiveCoordinatorPort` and
`read_run_summaries` to a connection lifecycle. Those semantics stay exactly where
they already lived (`board.reducer`, `report.portfolio`, `port.port`,
`gateway.policy`, `events.log`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..db import connect
from ..events.envelope import Actor
from ..events.log import read_run_summaries
from ..port import HiveCoordinatorPort, PortView


class ReadPort(Protocol):
    def read(self, cursor: int | None = None) -> PortView: ...


PortFactory = Callable[[str, int | None], ReadPort]
RunsFactory = Callable[[], list[dict]]


def database_port(actor: Actor) -> PortFactory:
    """A `PortFactory` bound to one reader actor identity, over a fresh short-lived
    connection per call — the production factory every real (non-demo) caller uses."""

    def factory(run_id: str, generation: int | None) -> ReadPort:
        conn = connect()
        try:
            return HiveCoordinatorPort(actor, run_id, conn, generation=generation)
        except Exception:
            conn.close()
            raise

    return factory


def database_runs() -> list[dict]:
    """The spine's run registry — which runs exist at all. Discovery is a listing,
    not a fold: every board a caller renders is still read through the port, one per
    run, so the process keeps exactly one fold site (the port's)."""
    conn = connect()
    try:
        return read_run_summaries(conn)
    finally:
        conn.close()


def database_healthcheck() -> None:
    """Raise if the read credential cannot reach the database. The JSON API's
    `/api/v1/health` route is the one caller; it does not fold or query a run, so a
    healthy-but-empty spine still reports `ok`."""
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def read_view(
    factory: PortFactory, run_id: str, cursor: int | None, generation: int | None
) -> PortView:
    port = factory(run_id, generation)
    try:
        return port.read(cursor)
    finally:
        # DemoPort deliberately has no connection. The real port keeps its connection
        # private on `_conn`; closing it here (rather than inside the port) is what
        # lets one short-lived connection serve exactly one read.
        conn = getattr(port, "_conn", None)
        if conn is not None:
            conn.close()
