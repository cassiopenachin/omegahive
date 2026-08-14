"""The receipt recorder, against a fake OpenRouter.

What these tests are actually for: the recorder sits on the credentialed path between a
harness and the gateway, and the whole study's cost and upstream numbers come out of it. So
the tests check the three things that would quietly ruin a batch — that it forwards bytes
unchanged (a harness that behaves differently through the proxy is measuring something else),
that no credential reaches the JSONL, and that a missing receipt stays missing instead of
becoming a zero.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from taskbench.receipts import (
    ObservedCall,
    ReceiptRecorder,
    fetch_generation,
    load_calls,
    reconcile,
)

MESSAGE_BODY = {
    "id": "gen-abc123",
    "type": "message",
    "role": "assistant",
    "model": "deepseek/deepseek-v4-flash-20260731",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    },
    # OpenRouter's extension, which Claude Code discards before anything can read it.
    "openrouter": {"cost": 0.000123, "provider": "GMICloud"},
}

SSE_STREAM = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"gen-stream1",'
    b'"model":"meta/muse-spark-1.2-20260805",'
    b'"usage":{"input_tokens":100,"cache_read_input_tokens":40,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","usage":{"output_tokens":250}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


class _FakeUpstream(BaseHTTPRequestHandler):
    """Stands in for OpenRouter. Records what it was sent so transparency can be asserted."""

    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def log_message(self, *args, **kwargs):  # noqa: A003, ANN002, ANN003
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        if self.path.endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in SSE_STREAM.split(b"\n\n"):
                if not part.strip():
                    continue
                chunk = part + b"\n\n"
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        payload = json.dumps(MESSAGE_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream():
    _FakeUpstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", _FakeUpstream
    server.shutdown()
    server.server_close()


@pytest.fixture
def recorder(upstream, tmp_path):
    base, _ = upstream
    rec = ReceiptRecorder(tmp_path / "receipts.jsonl", upstream=base).start()
    yield rec
    rec.stop()


def test_non_streaming_response_is_forwarded_byte_identical(recorder):
    """The harness must see exactly what the gateway sent. Anything else measures the proxy."""
    resp = httpx.post(
        f"{recorder.base_url}/v1/messages",
        json={"model": "deepseek/deepseek-v4-flash-0731@preset/p", "messages": []},
        headers={"Authorization": "Bearer sk-or-SECRET", "anthropic-version": "2023-06-01"},
        timeout=30,
    )
    assert resp.status_code == 200
    assert resp.json() == MESSAGE_BODY


def test_non_streaming_call_is_recorded_with_identity_and_usage(recorder):
    httpx.post(
        f"{recorder.base_url}/v1/messages",
        json={"model": "deepseek/deepseek-v4-flash-0731@preset/p", "messages": []},
        headers={"Authorization": "Bearer sk-or-SECRET"},
        timeout=30,
    )
    (call,) = recorder.calls
    assert call.generation_id == "gen-abc123"
    assert call.requested_model == "deepseek/deepseek-v4-flash-0731@preset/p"
    assert call.response_model == "deepseek/deepseek-v4-flash-20260731"
    assert call.usage["input_tokens"] == 11
    assert call.usage["cache_read_input_tokens"] == 5
    # The extension is the whole reason this proxy exists: Claude Code drops it.
    assert call.extensions["openrouter"] == {"cost": 0.000123, "provider": "GMICloud"}
    assert call.status == 200


def test_the_upstream_receives_the_original_credential(recorder, upstream):
    """Forwarded verbatim — the proxy is transparent, not a credential broker."""
    _, fake = upstream
    httpx.post(
        f"{recorder.base_url}/v1/messages",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer sk-or-SECRET"},
        timeout=30,
    )
    assert fake.seen[0]["headers"]["authorization"] == "Bearer sk-or-SECRET"
    # The path is forwarded verbatim, `/api` and all. Against the live gateway this is exactly
    # what keeps `https://openrouter.ai` + `/api/v1/messages` from becoming `/api/api/v1/…`,
    # which would have 404'd every scored call in the study.
    assert fake.seen[0]["path"] == "/api/v1/messages"


def test_no_credential_reaches_the_record(recorder, tmp_path):
    """An allowlist, so a header this module has never heard of cannot leak by default."""
    httpx.post(
        f"{recorder.base_url}/v1/messages",
        json={"model": "m", "messages": []},
        headers={
            "Authorization": "Bearer sk-or-SECRET",
            "x-api-key": "sk-ant-ALSOSECRET",
            "x-some-future-harness-token": "sk-FUTURE",
        },
        timeout=30,
    )
    assert recorder.drain(timeout=30), "the call was still being written down"
    written = (tmp_path / "receipts.jsonl").read_text()
    assert "SECRET" not in written
    assert "sk-FUTURE" not in written
    assert "authorization" not in written.lower()
    assert "gen-abc123" in written


def test_streaming_is_passed_through_and_usage_is_merged_across_events(recorder):
    """`message_start` has the input counts, `message_delta` the output. Neither alone is it."""
    chunks: list[bytes] = []
    with httpx.stream(
        "POST",
        f"{recorder.base_url}/v1/messages/stream",
        json={"model": "meta/muse-spark-1.2@preset/p", "messages": [], "stream": True},
        timeout=30,
    ) as resp:
        assert resp.status_code == 200
        for chunk in resp.iter_raw():
            chunks.append(chunk)
    assert b"".join(chunks) == SSE_STREAM

    (call,) = recorder.calls
    assert call.streamed is True
    assert call.generation_id == "gen-stream1"
    assert call.response_model == "meta/muse-spark-1.2-20260805"
    assert call.usage == {
        "input_tokens": 100,
        "cache_read_input_tokens": 40,
        "output_tokens": 250,  # the delta's value wins; the placeholder 1 does not survive
    }


def test_an_unreachable_upstream_is_recorded_not_swallowed(tmp_path):
    rec = ReceiptRecorder(tmp_path / "r.jsonl", upstream="http://127.0.0.1:1").start()
    try:
        resp = httpx.post(
            f"{rec.base_url}/v1/messages", json={"model": "m"}, timeout=30
        )
        assert resp.status_code == 502
    finally:
        rec.stop()
    (call,) = rec.calls
    assert call.proxy_error
    assert call.generation_id is None
    assert load_calls(tmp_path / "r.jsonl")[0].proxy_error == call.proxy_error


# --- /generation ---------------------------------------------------------------------------

RECEIPT = {
    "id": "gen-abc123",
    "model": "deepseek/deepseek-v4-flash-20260731",
    "provider_name": "GMICloud",
    "preset_id": "omegahive-deepseek-v4-flash-0731",
    "total_cost": 0.000123,
    "native_tokens_prompt": 11,
    "native_tokens_completion": 7,
    "native_tokens_cached": 5,
    "native_tokens_reasoning": 0,
}


def _generation_transport(*, misses: int = 0, receipt: dict | None = None) -> httpx.Client:
    """A /generation that 404s `misses` times first — the real one is written asynchronously."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        state["n"] += 1
        if state["n"] <= misses:
            return httpx.Response(404, json={"error": "not found yet"})
        return httpx.Response(200, json={"data": receipt or RECEIPT})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_generation_is_retried_until_the_record_exists():
    with _generation_transport(misses=2) as client:
        got = fetch_generation("gen-abc123", "sk-or-x", client=client, first_delay_s=0.001)
    assert got["available"] is True
    assert got["attempts"] == 3
    assert got["receipt"]["provider_name"] == "GMICloud"


def test_a_receipt_that_never_arrives_is_named_not_guessed():
    with _generation_transport(misses=99) as client:
        got = fetch_generation(
            "gen-x", "sk-or-x", client=client, attempts=3, first_delay_s=0.001
        )
    assert got["available"] is False
    assert "not recoverable from the harness" in got["missing_surface"].lower()


def test_drain_waits_for_a_call_that_is_still_being_written(upstream, tmp_path):
    """A call is recorded only after its response has fully streamed, because the usage totals
    arrive last. Reconciling inside that window would drop the final call from every total."""
    base, _ = upstream
    rec = ReceiptRecorder(tmp_path / "d.jsonl", upstream=base).start()
    try:
        for _ in range(5):
            httpx.post(
                f"{rec.base_url}/v1/messages", json={"model": "m", "messages": []}, timeout=30
            )
        assert rec.drain(timeout=30)
        assert len(rec.calls) == 5
        assert len(load_calls(tmp_path / "d.jsonl")) == 5
    finally:
        rec.stop()


def test_stop_reports_whether_it_managed_to_drain(upstream, tmp_path):
    """False rather than an exception, so an incomplete capture can be recorded AS incomplete
    instead of becoming a total that quietly omits a call."""
    base, _ = upstream
    rec = ReceiptRecorder(tmp_path / "s.jsonl", upstream=base).start()
    httpx.post(f"{rec.base_url}/v1/messages", json={"model": "m"}, timeout=30)
    assert rec.stop(drain_timeout=30) is True


def test_reconcile_totals_only_the_calls_that_have_receipts():
    calls = [
        ObservedCall(
            seq=1, started_utc="t", finished_utc="t", duration_ms=1, method="POST",
            path="/v1/messages", status=200, streamed=False, generation_id="gen-abc123",
        ),
        ObservedCall(  # no id: nothing to ask about
            seq=2, started_utc="t", finished_utc="t", duration_ms=1, method="POST",
            path="/v1/messages", status=500, streamed=False,
        ),
    ]
    with _generation_transport() as client:
        out = reconcile(calls, "sk-or-x", client=client, first_delay_s=0.001)
    totals = out["totals"]
    assert totals["calls_observed"] == 2
    assert totals["calls_with_receipt"] == 1
    assert totals["gateway_cost_usd"] == pytest.approx(0.000123)
    assert totals["resolved_upstreams"] == ["GMICloud"]
    assert totals["preset_ids"] == ["omegahive-deepseek-v4-flash-0731"]
    assert "floor, not a total" in totals["incomplete"]


def test_a_field_missing_from_any_receipt_makes_its_total_unknown_not_partial():
    """A sum over the subset that happened to carry the field reads as a total for all of them."""
    calls = [
        ObservedCall(
            seq=n, started_utc="t", finished_utc="t", duration_ms=1, method="POST",
            path="/v1/messages", status=200, streamed=False, generation_id=f"gen-{n}",
        )
        for n in (1, 2)
    ]
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        doc = dict(RECEIPT)
        if state["n"] == 2:
            del doc["total_cost"]  # one receipt is missing the priced field
        return httpx.Response(200, json={"data": doc})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = reconcile(calls, "sk-or-x", client=client, first_delay_s=0.001)
    assert out["totals"]["calls_with_receipt"] == 2
    assert out["totals"]["gateway_cost_usd"] is None
    assert out["totals"]["native_tokens_prompt"] == 22
