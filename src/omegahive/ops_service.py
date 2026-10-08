"""The operation service on its Unix socket, and the client the UI and `hive ops` share.

The socket is the policy's `ops_socket`, created mode 0600 by the user who runs the
service (`hive-ops.service`). Nothing listens on a network interface (salvage plan D8).
The UI container reaches the socket through a bind mount; its root maps to this user.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .deployment import resolve
from .events.envelope import Actor
from .operations import Forbidden, Operations, Refused
from .report.reader import database_port


class OperationRequest(BaseModel):
    operation_id: str
    surface: str = "cli"          # "web" when the UI forwards a verified login
    login: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def build_app(ops: Operations) -> FastAPI:
    app = FastAPI(title="omegahive operations", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/operations/{operation}")
    def operate(operation: str, request: OperationRequest) -> JSONResponse:
        try:
            receipt = ops.execute(operation, request.operation_id, request.surface,
                                  request.params, login=request.login)
        except Forbidden as exc:
            return JSONResponse({"status": "forbidden", "message": str(exc)}, status_code=403)
        except Refused as exc:
            return JSONResponse({"status": "refused", "message": str(exc)}, status_code=409)
        return JSONResponse(receipt)

    @app.get("/tail/{task}")
    def tail(task: str) -> dict[str, Any]:
        return {"lines": ops.tail(task)}

    @app.get("/merge-candidates/{run}/{task}")
    def merge_candidates(run: str, task: str) -> dict[str, Any]:
        try:
            return {"candidates": ops.merge_candidates(run, task), "error": None}
        except Refused as exc:
            return {"candidates": [], "error": str(exc)}

    @app.get("/routes")
    def routes() -> dict[str, Any]:
        return {"routes": ops.routes()}

    return app


def listen(path: Path) -> socket.socket:
    """The service's socket, bound and mode 0600: this user, and the container root it maps.

    Bound here rather than by uvicorn's `uds=`, which chmods the socket 0666 after binding.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    return sock


def serve() -> None:
    import uvicorn

    deployment = resolve(os.environ)
    ops = Operations(deployment, database_port(Actor(role="coordinator",
                                                     id="operation-service")))
    sock = listen(Path(deployment["ops_socket"]))
    uvicorn.run(build_app(ops), fd=sock.fileno(), log_level="info")


class OpsClient:
    """The service over its socket. Every call returns the service's JSON, or raises
    OpsUnavailable when nothing answers there."""

    def __init__(self, socket: str, timeout: float = 960.0) -> None:
        self.socket = socket
        self._client = httpx.Client(transport=httpx.HTTPTransport(uds=socket),
                                    base_url="http://ops", timeout=timeout)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise OpsUnavailable(f"the operation service is not answering on {self.socket}: "
                                 f"{exc}") from exc
        body: dict[str, Any] = response.json()
        return body

    def operate(self, operation: str, operation_id: str, params: Mapping[str, Any], *,
                surface: str = "cli", login: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/operations/{operation}", json={
            "operation_id": operation_id, "surface": surface, "login": login,
            "params": dict(params)})

    def tail(self, task: str) -> list[str] | None:
        lines: list[str] | None = self._call("GET", f"/tail/{task}", timeout=15)["lines"]
        return lines

    def merge_candidates(self, run: str, task: str) -> dict[str, Any]:
        return self._call("GET", f"/merge-candidates/{run}/{task}", timeout=90)

    def routes(self) -> list[str]:
        routes: list[str] = self._call("GET", "/routes", timeout=15)["routes"]
        return routes


class OpsUnavailable(RuntimeError):
    pass
