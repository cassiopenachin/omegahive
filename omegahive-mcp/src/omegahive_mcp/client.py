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

import ssl
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


def _carries_ssl_error(exc: BaseException, *, _depth: int = 0) -> bool:
    """Whether `exc`, or anything reachable from it, is an `ssl.SSLError`.

    httpx wraps a real TLS failure two levels deep and does not put it in
    `__cause__` at the level that matters: `httpx.ConnectError.__cause__` is an
    `httpcore.ConnectError` whose OWN `__cause__` is `None` — the `ssl.SSLError` is
    one of *its* `.args` instead (verified against the installed httpx/httpcore:
    `except httpx.ConnectError as e: e.__cause__.args == (SSLCertVerificationError(...),)`).
    A single `type(exc.__cause__).__module__` check therefore never matches a real
    cert failure. Walking both `__cause__` and `.args` (bounded, so a malformed or
    self-referential chain cannot recurse forever) is what actually finds it.
    """
    if isinstance(exc, ssl.SSLError):
        return True
    if _depth > 4:
        return False
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, BaseException) and _carries_ssl_error(arg, _depth=_depth + 1):
            return True
    cause = exc.__cause__
    return cause is not None and _carries_ssl_error(cause, _depth=_depth + 1)


def _classify_transport_error(exc: httpx.HTTPError) -> UpstreamError:
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError("timeout", str(exc))
    if _carries_ssl_error(exc):
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
        except httpx.InvalidURL as exc:
            # Not an httpx.HTTPError subclass at all (it is a bare Exception), so it
            # would otherwise escape uncaught past every caller of this method.
            raise UpstreamError("upstream_error", f"{url}: invalid URL ({exc})") from exc
        except httpx.DecodingError as exc:
            # The connection succeeded but the response body could not be decoded
            # (e.g. a bad Content-Encoding) — a malformed response, not a transport
            # fault, so it gets the same code a corrupt JSON body would.
            raise UpstreamError("malformed_json", f"{url}: {exc}") from exc
        except httpx.HTTPError as exc:
            # Every other httpx fault (ConnectError, ReadError, TimeoutException,
            # TooManyRedirects, ...) — classified below rather than left to escape.
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
