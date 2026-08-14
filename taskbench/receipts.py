"""Gateway receipts: what OpenRouter says a call cost, captured before a harness discards it.

Both DeepSeek arms and the Muse arm reach OpenRouter through its Anthropic Messages skin, and
**neither harness preserves the fields the study is scored on**. Reasonix keeps exact
token/cache totals but drops the generation id, the resolved upstream and the server cost.
Claude Code keeps token/cache totals but reports a *harness-local* cost computed from its own
price table and the protocol label `firstParty` — which describes Claude Code's request path,
not GMICloud or Meta. A price table is not a receipt, and a transcript-derived count is not a
receipt either.

So this module sits in the one place where the fact still exists: **between the harness and
the gateway**. It is a transparent reverse proxy. It forwards every byte of every request and
response unchanged — streaming included, so a harness sees exactly the latency and the token
stream it would have seen — and keeps a copy of what went past. Afterwards it asks OpenRouter's
`/generation` endpoint for the authoritative record of each call it saw.

Two rules shape it, and both exist because the alternative is a number nobody can trust:

**It never invents.** A field OpenRouter did not return is absent, never zero and never
derived. `reconcile()` reports how many of the calls it observed got an authoritative receipt,
so a reader can see the coverage rather than assume it.

**It never holds a credential.** The client's `Authorization` / `x-api-key` headers are
forwarded verbatim and are neither logged nor stored — every recorded header set is redacted by
an allowlist, not a denylist, so a header added by a future harness cannot leak by default. The
key the proxy uses for its own `/generation` calls comes from its own environment and is never
written down.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

#: Everything here is keyed off the *origin*, never a base that already carries `/api`.
#: The proxy forwards `self.path` verbatim, and the path a harness sends already begins
#: `/api/v1/...` because `base_url` ends in `/api` — so an upstream that also ended in `/api`
#: would request `/api/api/v1/messages` and 404 every call in the study.
OPENROUTER_ORIGIN = "https://openrouter.ai"
GENERATION_PATH = "/api/v1/generation"

#: Request headers worth keeping for the record. An allowlist, because the failure mode of a
#: denylist here is a leaked credential: a header this module has never heard of is dropped.
SAFE_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "anthropic-beta",
        "anthropic-version",
        "content-type",
        "http-referer",
        "user-agent",
        "x-title",
        "x-openrouter-preset",
    }
)

#: Response headers OpenRouter uses to describe routing. Same allowlist rule.
SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-encoding",
        "content-type",
        "openrouter-model",
        "openrouter-provider",
        "x-openrouter-model",
        "x-openrouter-provider",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-request-id",
    }
)

#: Hop-by-hop headers: forwarding these breaks the connection semantics of the proxied hop.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


def _decoded(body: bytes, encoding: str | None) -> bytes:
    """The body as text, whatever the wire did to it. Undecodable stays undecoded.

    The proxy forwards the raw bytes untouched; this copy exists only to be parsed. An encoding
    we cannot undo returns the original rather than raising, so the call is still recorded — with
    no generation id, which `reconcile` then reports honestly as a missing receipt.
    """
    if not encoding or encoding.lower() in ("identity", ""):
        return body
    import gzip
    import zlib

    try:
        if encoding.lower() == "gzip":
            return gzip.decompress(body)
        if encoding.lower() in ("deflate", "zlib"):
            return zlib.decompress(body)
    except (OSError, zlib.error, EOFError):
        return body
    return body


def _redact(headers: Any, allowed: frozenset[str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() in allowed}


@dataclass
class ObservedCall:
    """One request the harness made, as the wire saw it.

    Every field is either something OpenRouter returned or something the proxy timed itself.
    Nothing here is inferred from the harness's own accounting.
    """

    seq: int
    started_utc: str
    finished_utc: str
    duration_ms: int
    method: str
    path: str
    status: int
    streamed: bool
    #: The model string the harness asked for — the study's identity check reads this.
    requested_model: str | None = None
    #: `id` from the response body. For OpenRouter this is the generation id `/generation` takes.
    generation_id: str | None = None
    #: `model` as the response reported it, which can differ from what was requested.
    response_model: str | None = None
    #: The `usage` block verbatim, plus any OpenRouter cost extension riding alongside it.
    usage: dict[str, Any] | None = None
    #: Anything in the body OpenRouter added beyond the Anthropic schema, kept whole.
    extensions: dict[str, Any] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    #: Set when the proxy itself failed, so a missing call is never silently absent.
    proxy_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _extract_from_json_body(body: bytes) -> dict[str, Any]:
    """Pull identity and usage out of a non-streaming Messages response."""
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(doc.get("id"), str):
        out["generation_id"] = doc["id"]
    if isinstance(doc.get("model"), str):
        out["response_model"] = doc["model"]
    if isinstance(doc.get("usage"), dict):
        out["usage"] = doc["usage"]
    # OpenRouter rides its own fields alongside the Anthropic schema. Keep every one we do not
    # recognise as Anthropic's, whole and unedited: this is the surface the order calls the
    # response extension, and guessing its names would be how we lose it.
    anthropic_keys = {
        "id", "type", "role", "content", "model", "stop_reason",
        "stop_sequence", "usage", "container", "context_management",
    }
    extras = {k: v for k, v in doc.items() if k not in anthropic_keys}
    if extras:
        out["extensions"] = extras
    return out


def _extract_from_sse(body: bytes) -> dict[str, Any]:
    """Same, from a server-sent-event stream.

    Anthropic splits the accounting: `message_start` carries the id and the input/cache
    counts, `message_delta` carries the final output count. Both are needed and neither is
    complete on its own, so they are merged rather than one overwriting the other.
    """
    out: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    for line in body.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:"):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("id"), str):
                    out["generation_id"] = message["id"]
                if isinstance(message.get("model"), str):
                    out["response_model"] = message["model"]
                if isinstance(message.get("usage"), dict):
                    usage.update(message["usage"])
        elif kind in ("message_delta", "message_stop"):
            if isinstance(event.get("usage"), dict):
                usage.update(event["usage"])
        # An event type outside Anthropic's set is an extension; keep the last of each kind.
        if kind and kind not in (
            "message_start", "message_delta", "message_stop", "ping",
            "content_block_start", "content_block_delta", "content_block_stop",
        ):
            extensions[str(kind)] = event
    if usage:
        out["usage"] = usage
    if extensions:
        out["extensions"] = extensions
    return out


class _Handler(BaseHTTPRequestHandler):
    """One proxied hop. Streams both ways; keeps a copy; edits nothing."""

    protocol_version = "HTTP/1.1"
    recorder: ReceiptRecorder  # set on the server instance, read via self.server

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # The default handler logs the request line to stderr. A request line is harmless, but
        # this process sits on a credentialed path and its stderr may be captured into a cell
        # record — so it says nothing at all, and the JSONL is the only output.
        return

    def _proxy(self, method: str) -> None:
        recorder: ReceiptRecorder = self.server.recorder  # type: ignore[attr-defined]
        seq = recorder.next_seq()
        try:
            self._proxy_inner(method, seq)
        except BaseException:
            # `next_seq` registered this call as in flight; if we leave without recording it,
            # `drain()` waits forever and every batch reports a possibly-incomplete capture.
            # This is reachable in normal operation: `run_cell` SIGKILLs the harness's process
            # group on timeout, which resets the connection while the body is still being read.
            recorder.abandon(seq, f"the proxy did not complete this call: {method} {self.path}")
            raise

    def _proxy_inner(self, method: str, seq: int) -> None:
        recorder: ReceiptRecorder = self.server.recorder  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        requested_model = None
        if body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and isinstance(parsed.get("model"), str):
                    requested_model = parsed["model"]
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        forward_headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
        }
        # Ask upstream not to compress. `iter_raw()` yields the body exactly as it arrives, so a
        # gzipped response parses to nothing: the generation id and usage vanish and every call
        # is recorded `available: false` with no error anywhere — the whole gateway accounting
        # silently degrading to "unavailable". Node/undici harnesses send `gzip, deflate` by
        # default, so this is the normal case, not an edge one. Content encoding is transport,
        # not semantics: the harness sees the same bytes either way. The decoder below is the
        # belt to this braces, for an upstream that compresses regardless.
        forward_headers = {
            k: v for k, v in forward_headers.items() if k.lower() != "accept-encoding"
        }
        forward_headers["Accept-Encoding"] = "identity"
        url = recorder.upstream.rstrip("/") + self.path
        started = time.time()
        captured = bytearray()
        call = ObservedCall(
            seq=seq,
            started_utc=_utc(started),
            finished_utc=_utc(started),
            duration_ms=0,
            method=method,
            path=self.path,
            status=0,
            streamed=False,
            requested_model=requested_model,
            request_headers=_redact(self.headers, SAFE_REQUEST_HEADERS),
        )

        try:
            with recorder.client.stream(
                method, url, headers=forward_headers, content=body
            ) as upstream:
                call.status = upstream.status_code
                content_type = upstream.headers.get("content-type", "")
                call.streamed = "event-stream" in content_type
                call.response_headers = _redact(upstream.headers, SAFE_RESPONSE_HEADERS)

                self.send_response(upstream.status_code)
                for key, value in upstream.headers.items():
                    if key.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(key, value)
                # Length is unknown until the stream ends, and buffering to learn it would
                # turn a streaming harness into a hanging one. Chunked keeps the hop honest.
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                for chunk in upstream.iter_raw():
                    if not chunk:
                        continue
                    captured.extend(chunk)
                    self.wfile.write(b"%x\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except Exception as exc:  # noqa: BLE001 — any failure is recorded, never swallowed
            call.proxy_error = f"{type(exc).__name__}: {exc}"
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                payload = json.dumps(
                    {"error": {"type": "proxy_error", "message": call.proxy_error}}
                ).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:  # noqa: BLE001 — the client is already gone
                pass

        finished = time.time()
        call.finished_utc = _utc(finished)
        call.duration_ms = int((finished - started) * 1000)

        raw = _decoded(bytes(captured), call.response_headers.get("content-encoding"))
        extracted = _extract_from_sse(raw) if call.streamed else _extract_from_json_body(raw)
        call.generation_id = extracted.get("generation_id")
        call.response_model = extracted.get("response_model")
        call.usage = extracted.get("usage")
        call.extensions = extracted.get("extensions") or {}
        recorder.record(call)

    def do_POST(self) -> None:  # noqa: N802
        self._proxy("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._proxy("GET")


class ReceiptRecorder:
    """A running proxy plus the JSONL it writes. Start it, point a harness at `base_url`.

    Deliberately not a context manager only: a cell's harness is launched by the runner, so the
    recorder has to outlive one function call and be stopped explicitly.
    """

    def __init__(
        self,
        out_path: str | Path,
        *,
        upstream: str = OPENROUTER_ORIGIN,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.upstream = upstream
        self.calls: list[ObservedCall] = []
        self._seq = 0
        self._lock = threading.Lock()
        # A call is recorded *after* its response has been fully streamed back, because the
        # usage totals only arrive at the end. So between a harness receiving its last byte and
        # that call reaching `calls`, there is a window — and a reconciliation that ran inside
        # it would silently drop the final call from every total. `drain()` closes the window.
        self._inflight = 0
        self._idle = threading.Condition(self._lock)
        # No global timeout on the read: an agent turn can think for minutes and a timeout here
        # would truncate a real response into a proxy error.
        self.client = httpx.Client(timeout=httpx.Timeout(30.0, read=None))
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.recorder = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """What a harness's `ANTHROPIC_BASE_URL` must be set to."""
        # `server_address` is typed loosely enough to be bytes; this is an AF_INET socket, so
        # spell the coercion rather than interpolate whatever it happens to be.
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host!s}:{int(port)}/api"

    def start(self) -> ReceiptRecorder:
        self._thread.start()
        return self

    def drain(self, timeout: float = 60.0) -> bool:
        """Wait until every call the proxy has begun has been written down.

        Returns False on timeout rather than raising, so a caller can record an incomplete
        capture *as* incomplete — the one thing that must never happen is a total that silently
        omits a call the proxy was still writing.
        """
        with self._idle:
            return self._idle.wait_for(lambda: self._inflight == 0, timeout=timeout)

    def stop(self, *, drain_timeout: float = 60.0) -> bool:
        drained = self.drain(drain_timeout)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        self.client.close()
        return drained

    def next_seq(self) -> int:
        with self._idle:
            self._seq += 1
            self._inflight += 1
            return self._seq

    def record(self, call: ObservedCall) -> None:
        with self._idle:
            self.calls.append(call)
            with open(self.out_path, "a") as fh:
                fh.write(json.dumps(call.to_json(), sort_keys=True) + "\n")
            self._inflight -= 1
            self._idle.notify_all()

    def abandon(self, seq: int, why: str) -> None:
        """A call the proxy began and could not finish. Recorded as such, never just dropped.

        Dropping it would leave `drain()` blocked on a call that will never arrive AND leave the
        record silently one call short — a total that looks complete and is not.
        """
        self.record(
            ObservedCall(
                seq=seq, started_utc=_utc(time.time()), finished_utc=_utc(time.time()),
                duration_ms=0, method="?", path="?", status=0, streamed=False,
                proxy_error=why,
            )
        )


def fetch_generation(
    generation_id: str,
    api_key: str,
    *,
    upstream: str = OPENROUTER_ORIGIN,
    attempts: int = 8,
    first_delay_s: float = 2.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """The authoritative record of one call, or an honest account of why there is none.

    OpenRouter writes the generation record asynchronously, so an immediate read 404s on a call
    that completed perfectly well. The setup preflight saw exactly that: absent through a
    twelve-second poll, present later. Hence the backoff — eight attempts doubling from two
    seconds is a little over four minutes, which is longer than any delay observed so far, and
    a bounded wait is the point.
    """
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    url = upstream.rstrip("/") + GENERATION_PATH
    delay = first_delay_s
    last: str = "no attempt was made"
    try:
        for attempt in range(1, attempts + 1):
            try:
                resp = http.get(
                    url,
                    params={"id": generation_id},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    doc = resp.json()
                    # The documented envelope wraps the record in `data`; accept a bare record
                    # too rather than returning nothing if that ever changes.
                    record = doc.get("data") if isinstance(doc, dict) else None
                    return {
                        "available": True,
                        "attempts": attempt,
                        "receipt": record if isinstance(record, dict) else doc,
                    }
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in (404, 408, 425, 429, 500, 502, 503, 504):
                    break
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
        return {
            "available": False,
            "attempts": attempts,
            "missing_surface": (
                f"OpenRouter /generation did not return a record for {generation_id} "
                f"(last: {last}). The gateway cost and resolved upstream for this call are "
                "unknown; they are NOT recoverable from the harness's own accounting."
            ),
        }
    finally:
        if owned:
            http.close()


#: Fields the study is scored on. Named explicitly so a reader can see what was taken from the
#: receipt and what was left behind, rather than trusting a dict passed through whole.
RECEIPT_FIELDS = (
    "id",
    "model",
    "provider_name",
    "preset_id",
    "total_cost",
    "upstream_inference_cost",
    "cache_discount",
    "native_tokens_prompt",
    "native_tokens_completion",
    "native_tokens_reasoning",
    "native_tokens_cached",
    "tokens_prompt",
    "tokens_completion",
    "latency",
    "generation_time",
    "finish_reason",
    "streamed",
    "cancelled",
    "is_byok",
)


def reconcile(
    calls: list[ObservedCall],
    api_key: str,
    *,
    upstream: str = OPENROUTER_ORIGIN,
    client: httpx.Client | None = None,
    budget_s: float = 900.0,
    **fetch_kwargs: Any,
) -> dict[str, Any]:
    """Pair every observed call with its gateway receipt and total what is actually known.

    The totals cover only the calls that produced a receipt, and `coverage` says how many that
    was. A partial total presented as a whole one is the failure this whole module exists to
    prevent, so the two numbers travel together and neither is reported alone.
    """
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    deadline = time.monotonic() + budget_s
    try:
        rows: list[dict[str, Any]] = []
        for call in calls:
            row: dict[str, Any] = {
                "seq": call.seq,
                "path": call.path,
                "status": call.status,
                "streamed": call.streamed,
                "requested_model": call.requested_model,
                "response_model": call.response_model,
                "generation_id": call.generation_id,
                "harness_visible_usage": call.usage,
                "duration_ms": call.duration_ms,
            }
            if call.proxy_error:
                row["proxy_error"] = call.proxy_error
            if not call.generation_id:
                row["receipt"] = {
                    "available": False,
                    "missing_surface": (
                        "the response carried no id, so there is nothing to ask "
                        "/generation about"
                    ),
                }
            elif time.monotonic() > deadline:
                # The per-call backoff runs to about four minutes, and an agent batch makes
                # hundreds of calls. A systematic receipt gap would otherwise turn
                # reconciliation into hours of sleeping. Past the budget we stop waiting and
                # say so — an unfetched receipt is reported as unfetched, never as absent.
                row["receipt"] = {
                    "available": False,
                    "missing_surface": (
                        f"the reconciliation budget of {budget_s:.0f}s was exhausted before "
                        "this call was fetched; the receipt may well exist. Re-run "
                        "`taskbench gateway-totals` later rather than reading this as a "
                        "missing receipt."
                    ),
                }
            else:
                fetched = fetch_generation(
                    call.generation_id, api_key, upstream=upstream, client=http, **fetch_kwargs
                )
                if fetched.get("available"):
                    full = fetched["receipt"]
                    row["receipt"] = {
                        "available": True,
                        "attempts": fetched["attempts"],
                        **{k: full.get(k) for k in RECEIPT_FIELDS if k in full},
                    }
                    row["receipt_raw"] = full
                else:
                    row["receipt"] = fetched
            rows.append(row)

        receipted = [r for r in rows if r["receipt"].get("available")]
        totals: dict[str, Any] = {
            "calls_observed": len(rows),
            "calls_with_receipt": len(receipted),
        }
        if receipted:
            def total(key: str) -> Any:
                values = [r["receipt"].get(key) for r in receipted]
                present = [v for v in values if isinstance(v, (int, float))]
                # All-or-nothing: a sum over the subset that happened to carry the field would
                # read as a total for every call. If any receipt lacks it, the total is unknown.
                if len(present) != len(receipted):
                    return None
                return sum(present)

            totals["gateway_cost_usd"] = total("total_cost")
            totals["native_tokens_prompt"] = total("native_tokens_prompt")
            totals["native_tokens_completion"] = total("native_tokens_completion")
            totals["native_tokens_reasoning"] = total("native_tokens_reasoning")
            totals["native_tokens_cached"] = total("native_tokens_cached")
            providers = sorted({str(r["receipt"].get("provider_name")) for r in receipted})
            totals["resolved_upstreams"] = providers
            totals["resolved_models"] = sorted(
                {str(r["receipt"].get("model")) for r in receipted}
            )
            totals["preset_ids"] = sorted(
                {str(r["receipt"].get("preset_id")) for r in receipted}
            )
        missing = len(rows) - len(receipted)
        if missing:
            totals["incomplete"] = (
                f"{missing} of {len(rows)} observed call(s) have no gateway receipt. Every "
                "total above covers only the receipted calls and is a floor, not a total."
            )
        return {"calls": rows, "totals": totals}
    finally:
        if owned:
            http.close()


def load_calls(path: str | Path) -> list[ObservedCall]:
    """Read back a recorder's JSONL. Used by reconciliation after a cell has finished."""
    out: list[ObservedCall] = []
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        known = {k: v for k, v in doc.items() if k in ObservedCall.__dataclass_fields__}
        out.append(ObservedCall(**known))
    return out


def api_key_from_env(var: str = "OPENROUTER_API_KEY") -> str:
    """The one credential this module touches, and it never writes it anywhere."""
    key = os.environ.get(var)
    if not key:
        raise RuntimeError(
            f"{var} is not set. It is the operator's secret and reaches this process through "
            "the environment only — taskbench never reads it from a file, stores it, or puts "
            "it in a generated config."
        )
    return key
