"""A bounded HTTP client fixed to one operator-configured origin (scope items 6–7).

Every request is built from `origin + one of three fixed path templates`. `run_id`/
`task_id` are percent-encoded into their own path segment (`quote(..., safe="")`) so
neither can smuggle a `/`, a scheme, or a host into the request — the one place a
tool argument ever reaches the network. There is no `base_url` join anywhere in this
module on purpose: httpx's URL-joining rules would silently drop a path prefix like
Beastie's `/omegahive` if a request path were ever built as an absolute `/api/...`
string against a `base_url` that itself carries a path — building the full URL by
hand every time removes that whole class of mistake.

No redirect is ever followed, TLS verification is never disabled, and both a
connect and a read timeout are always in force — none of the four is a per-call
option; they are constructed once, in `HiveApiClient.__init__`, and nothing in this
module exposes a way to change them per request.
"""

from __future__ import annotations

from typing import Literal, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from .schemas import ErrorResponse, HealthResponse, PortfolioResponse, TaskDetailResponse

CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0

ErrorCode = Literal[
    "unreachable",
    "timeout",
    "invalid_tls",
    "malformed_json",
    "schema_mismatch",
    "unknown_run",
    "unknown_task",
    "database_unavailable",
    "invalid_request",
    "upstream_error",
]

_M = TypeVar("_M", bound=BaseModel)


class UpstreamError(Exception):
    """One of the distinct, safe error codes scope item 6 requires. `code` is what a
    tool call turns into a structured MCP tool error; `message` is for a human."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code: ErrorCode = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _classify_transport_error(exc: httpx.TransportError) -> UpstreamError:
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError("timeout", str(exc))
    cause = exc.__cause__
    if cause is not None and "ssl" in type(cause).__module__.lower():
        return UpstreamError("invalid_tls", str(exc))
    return UpstreamError("unreachable", str(exc))


class HiveApiClient:
    """Talks to exactly one fixed origin's `/api/v1/*` routes and validates every
    response against the schema the tool promised to return (scope item 4: "validate
    the Mac client's upstream response before returning it to a model")."""

    def __init__(self, origin: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._origin = origin.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=READ_TIMEOUT_SECONDS,
                pool=READ_TIMEOUT_SECONDS,
            ),
            follow_redirects=False,
            verify=True,
            transport=transport,
            headers={"Cache-Control": "no-cache"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HiveApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def health(self) -> HealthResponse:
        return self._get("/api/v1/health", HealthResponse)

    def portfolio(self) -> PortfolioResponse:
        return self._get("/api/v1/portfolio", PortfolioResponse)

    def task(self, run_id: str, task_id: str) -> TaskDetailResponse:
        path = f"/api/v1/runs/{quote(run_id, safe='')}/tasks/{quote(task_id, safe='')}"
        return self._get(path, TaskDetailResponse)

    def _get(self, path: str, model: type[_M]) -> _M:
        url = self._origin + path
        try:
            response = self._client.get(url)
        except httpx.TransportError as exc:
            raise _classify_transport_error(exc) from exc

        if response.is_redirect:
            raise UpstreamError(
                "unreachable", f"upstream tried to redirect ({response.status_code}); refused"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError("malformed_json", f"{url}: {exc}") from exc

        if response.is_success:
            try:
                return model.model_validate(body)
            except ValidationError as exc:
                raise UpstreamError("schema_mismatch", f"{url}: {exc}") from exc

        try:
            error = ErrorResponse.model_validate(body)
        except ValidationError as exc:
            raise UpstreamError(
                "upstream_error", f"{url}: HTTP {response.status_code}, unparseable body: {exc}"
            ) from exc
        raise UpstreamError(error.error, error.detail)
