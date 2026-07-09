"""Pure unit tests for the ladder runner's loss-attribution helper (no DB, no processes)."""

from __future__ import annotations

from ladder.runner import _make_coordinator, _stop_reason


def test_stop_reason_maps_coordinator_signal():
    assert _stop_reason("cap_ops", False) == "cap_ops_exhausted"
    assert _stop_reason("cap_timeout", False) == "cap_timeout"
    assert _stop_reason("terminal", False) is None          # completed => no bucket


def test_stop_reason_no_clean_report_is_error_or_timeout():
    assert _stop_reason(None, True) == "run_error"           # coordinator crashed (non-zero exit)
    assert _stop_reason(None, False) == "cap_timeout"        # coordinator killed/hung


def test_stop_reason_is_coordinator_attributed_not_ancillary():
    """The coordinator's cap report is authoritative even if an ancillary worker/review child
    crashed — its error is subsumed, not promoted over the coordinator's mechanical stop."""
    # coord_errored reflects the COORDINATOR only; a worker crash never reaches this call,
    # so a genuine cap_ops stays cap_ops rather than being relabeled run_error.
    assert _stop_reason("cap_ops", False) == "cap_ops_exhausted"


def test_vanilla_coordinator_loads_the_v2_catalog_not_v1():
    # B1: the grid must run against board-ops-v2 (v1's prune line leaks the strategy word
    # "doomed" into the coordinator's system prompt). Constructing the LLMClient makes no
    # network call — only `.complete()` would.
    coord = _make_coordinator("L1", ("w1",), model="fake-model", max_llm_calls=1)
    assert "doomed" not in coord._system.lower()
    assert "Abandon a not-done task" in coord._system
