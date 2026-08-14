"""A tiny real HTTP server (stdlib only) standing in for Beastie's JSON API in the
stdio protocol tests. A `MockTransport` only works in-process; the protocol tests
spawn a real `omegahive-mcp serve` subprocess, so the fake upstream must be a real
socket that subprocess can actually connect to.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEALTH = {
    "status": "ok",
    "schema_version": "1",
    "observed_at": "2026-08-14T12:00:00Z",
    "database": "ok",
}

PORTFOLIO = {
    "meta": {
        "schema_version": "1",
        "observed_at": "2026-08-14T12:00:00Z",
        "window_description": "open work plus closes within 7d",
        "window_days": 7,
    },
    "runs": [
        {
            "run": {"run_id": "r1", "cursor": 4, "generation": 1},
            "task_counts": {"total": 1, "active": 0, "attention": 1, "completed": 0},
            "tasks": [
                {
                    "task_id": "T1",
                    "status": "blocked",
                    "owner": "w1",
                    "title": "Freeze the grid",
                    "task_type": None,
                    "priority": "normal",
                    "depends_on": [],
                    "review": None,
                    "escalated": False,
                    "blocker_reason": "image digest missing",
                    "blocker_needs": "operator ack",
                    "last_status_change_logical_ts": 4,
                    "status_changed_at": "2026-08-14T11:30:00Z",
                    "elapsed_seconds": 1800.0,
                    "clock_kind": "wall",
                }
            ],
        }
    ],
    "hidden_run_count": 0,
}

TASK_DETAIL = {
    "meta": {
        "schema_version": "1",
        "observed_at": "2026-08-14T12:00:00Z",
        "window_description": "full history",
        "window_days": None,
    },
    "run": {"run_id": "r1", "cursor": 4, "generation": 1},
    "task": {
        "task_id": "T1",
        "status": "blocked",
        "owner": "w1",
        "title": "Freeze the grid",
        "task_type": None,
        "priority": "normal",
        "depends_on": [],
        "tried_by": ["w1"],
        "ready_when": None,
        "join_unsatisfiable": False,
        "pruned": False,
        "escalated": False,
        "review": None,
        "blocker_reason": "image digest missing",
        "blocker_needs": "operator ack",
        "last_result_ref": None,
        "last_causing_seq": 4,
        "last_status_change_logical_ts": 4,
        "status_changed_at": "2026-08-14T11:30:00Z",
        "elapsed_seconds": 1800.0,
        "clock_kind": "wall",
    },
    "events": [],
    "events_truncated": False,
    "events_returned": 0,
    "events_available": 0,
}

NOT_FOUND = {"error": "unknown_task", "detail": "no such task"}


class FakeUpstream:
    """A real, local HTTP server on `127.0.0.1`. `origin` is what a test writes into
    the MCP process's config. Swap `routes` to change what the next requests see."""

    def __init__(self) -> None:
        upstream = self
        routes = {
            "/api/v1/health": (200, HEALTH),
            "/api/v1/portfolio": (200, PORTFOLIO),
            "/api/v1/runs/r1/tasks/T1": (200, TASK_DETAIL),
            "/api/v1/runs/r1/tasks/nope": (404, NOT_FOUND),
        }
        self.routes = routes

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:  # silence stdlib access logging
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib method name
                status, body = upstream.routes.get(
                    self.path, (404, {"error": "unknown_run", "detail": "no route"})
                )
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> FakeUpstream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
