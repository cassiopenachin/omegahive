"""`validate_receipt_recorder` end to end, against a fake OpenRouter.

This function is the first thing the operator runs and the last gate before any gateway arm
spends — and it had **no test at all**, because every other test reached the pieces underneath
it directly. That gap cost the operator a live run: `reconcile(..., origin=…)` funnelled an
unknown keyword into `fetch_generation` and raised `TypeError` after the four probe calls had
already been made and paid for.

So this exercises the whole function against a local gateway: the direct call, the proxied call,
the reconciliation and the agreement check, with no network and no credential. The one thing it
cannot cover is OpenRouter's real response shape — which is exactly what the live run is for.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from taskbench.openrouter import DEEPSEEK_PIN
from taskbench.qualify import validate_receipt_recorder

GEN_ID = "gen-preflight-1"

MESSAGE = {
    "id": GEN_ID,
    "type": "message",
    "role": "assistant",
    "model": "deepseek/deepseek-v4-flash-20260731",
    "content": [{"type": "text", "text": "ready"}],
    "usage": {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 0},
}

RECEIPT = {
    "id": GEN_ID,
    "model": "deepseek/deepseek-v4-flash-20260731",
    "provider_name": "GMICloud",
    "preset_id": "omegahive-deepseek-v4-flash-0731",
    "total_cost": 0.0000412,
    "native_tokens_prompt": 12,
    "native_tokens_completion": 3,
    "native_tokens_cached": 0,
    "native_tokens_reasoning": 0,
}


class _Gateway(BaseHTTPRequestHandler):
    """Answers `/api/v1/messages` and `/api/v1/generation`, like the real one."""

    protocol_version = "HTTP/1.1"
    #: Overridden per test to make the gateway misbehave in a specific way.
    message = MESSAGE
    receipt = RECEIPT
    generation_status = 200
    calls: list[str] = []

    def log_message(self, *a, **k):  # noqa: A003, ANN002, ANN003
        return

    def _send(self, status: int, doc: dict) -> None:
        payload = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).calls.append(f"POST {self.path.split('?')[0]}")
        self._send(200, type(self).message)

    def do_GET(self):  # noqa: N802
        type(self).calls.append(f"GET {self.path.split('?')[0]}")
        if type(self).generation_status != 200:
            self._send(type(self).generation_status, {"error": "not yet"})
            return
        self._send(200, {"data": type(self).receipt})


@pytest.fixture
def gateway():
    _Gateway.message = MESSAGE
    _Gateway.receipt = RECEIPT
    _Gateway.generation_status = 200
    _Gateway.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", _Gateway
    server.shutdown()
    server.server_close()


def _run(gateway, tmp_path, **kw):
    origin, _ = gateway
    return {
        c.name.rsplit("/", 1)[-1]: c
        for c in validate_receipt_recorder(
            DEEPSEEK_PIN, "sk-or-test", out_dir=tmp_path, origin=origin,
            # The live defaults wait ~4 minutes for a record OpenRouter writes asynchronously.
            # This gateway answers immediately or not at all, so waiting proves nothing.
            fetch_attempts=2, fetch_first_delay_s=0.001, **kw
        )
    }


def test_a_healthy_gateway_passes_every_stage(gateway, tmp_path):
    """The regression: this raised TypeError before reaching `generation`, after the probe calls
    had already been made and paid for."""
    checks = _run(gateway, tmp_path)
    assert set(checks) >= {"direct", "transparent", "captured", "generation", "agrees"}
    for name, check in checks.items():
        assert check.ok, f"{name}: {check.detail}"


def test_it_makes_exactly_two_model_calls(gateway, tmp_path):
    """One direct, one proxied. A preflight that costs real money is a preflight people skip."""
    _, fake = gateway
    _run(gateway, tmp_path)
    assert fake.calls.count("POST /api/v1/messages") == 2


def test_the_generation_receipt_is_reported_with_its_resolved_upstream(gateway, tmp_path):
    checks = _run(gateway, tmp_path)
    assert "GMICloud" in checks["generation"].detail
    assert checks["generation"].observed["receipt"]["provider_name"] == "GMICloud"
    assert checks["generation"].observed["receipt"]["total_cost"] == RECEIPT["total_cost"]


def test_the_capture_check_names_what_the_recorder_actually_saw(gateway, tmp_path):
    checks = _run(gateway, tmp_path)
    assert checks["captured"].observed["generation_id"] == GEN_ID
    assert checks["captured"].observed["usage"]["input_tokens"] == 12


def test_a_gateway_resolving_the_wrong_model_fails_identity(gateway, tmp_path):
    """The check that the whole matched-pair claim reduces to."""
    _, fake = gateway
    fake.receipt = {**RECEIPT, "model": "deepseek/some-other-model"}
    checks = _run(gateway, tmp_path)
    assert not checks["generation"].ok
    assert "not among" in checks["generation"].detail


def test_a_fallback_upstream_fails_identity_even_with_the_right_model(gateway, tmp_path):
    _, fake = gateway
    fake.receipt = {**RECEIPT, "provider_name": "DeepInfra"}
    checks = _run(gateway, tmp_path)
    assert not checks["generation"].ok
    assert "fallback the pin exists to prevent" in checks["generation"].detail


def test_a_generation_endpoint_that_never_answers_fails_rather_than_hangs(gateway, tmp_path):
    _, fake = gateway
    fake.generation_status = 404
    checks = _run(gateway, tmp_path)
    assert not checks["generation"].ok
    assert "not recoverable from the harness" in checks["generation"].detail.lower()


def test_a_response_with_no_generation_id_stops_before_the_proxied_call(gateway, tmp_path):
    """With nothing to ask `/generation` about, there is nothing to validate."""
    _, fake = gateway
    fake.message = {k: v for k, v in MESSAGE.items() if k != "id"}
    checks = _run(gateway, tmp_path)
    assert not checks["direct"].ok
    assert "no generation id" in checks["direct"].detail
    assert "transparent" not in checks


def test_the_preflight_receipts_are_written_where_the_record_can_read_them(gateway, tmp_path):
    _run(gateway, tmp_path)
    written = (tmp_path / "preflight-receipts.jsonl").read_text()
    assert GEN_ID in written
    assert "sk-or-test" not in written, "the probe credential must not reach the record"


def test_skipping_the_recorder_still_runs_the_preset_and_endpoint_checks(gateway, tmp_path):
    """`--skip-recorder` is for re-checking a route without paying for the probe."""
    from taskbench.qualify import run_gateway_preflight

    origin, fake = gateway
    checks = run_gateway_preflight(
        "sk-or-test", out_dir=tmp_path, pins=(DEEPSEEK_PIN,),
        validate_recorder=False, origin=origin,
        fetch_attempts=2, fetch_first_delay_s=0.001,
    )
    assert not any(c.name.startswith("receipt/") for c in checks)
    assert fake.calls.count("POST /api/v1/messages") == 0
