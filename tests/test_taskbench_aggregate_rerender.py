"""What the aggregate's headline claims ran, and on what evidence.

This line is the first thing a reader of a record sees, and every downstream comparison
assumes it. The three tiers below are ordered by how much they can be trusted, and the test
that matters most is the third: a harness echoing back the alias it was given is not a
resolution, and printing it as one turns a request into a claimed identity.
"""

from __future__ import annotations

import json

from taskbench.record import _headline_identity, rehydrate_config_from_cells

REQUESTED = "meta/muse-spark-1.2@preset/omegahive-muse-spark-1-2"


def _cell(root, name, task, *, first_green, remediated, model_key="resolved_model"):
    cell = root / "cells" / name
    cell.mkdir(parents=True)
    (cell / "task.txt").write_text(f"{task}\nomegahive\ndocs-reorg\n")
    (cell / "run.json").write_text(json.dumps(
        {"resolved_identity": {model_key: "claude-haiku-4-5"}}
    ))
    leg = {"passed": first_green}
    (cell / "cycle.json").write_text(json.dumps(
        {"first_pass": {"deterministic": leg, "review": leg}, "remediated": remediated}
    ))
    (cell / "spend.json").write_text(json.dumps({
        "candidate_attempt": {"available": True, "usd": 1.5},
        "candidate_remediation": {"available": True, "usd": 0.5} if remediated else None,
        "review": [{"available": True, "usd": 2.0}],
    }))
    return cell


def test_a_re_render_recovers_every_render_only_key(tmp_path):
    """`resolved_models`, `cycles` and `spend_by_leg` are built in memory during a batch and
    never written to config.json. A re-render that silently drops them does not fail — it
    produces a weaker document that still looks complete, losing the first-shot column, the
    after-one-repair column and the whole spend table."""
    _cell(tmp_path, "cell-a", "docs-triage", first_green=False, remediated=True)
    _cell(tmp_path, "cell-b", "instrument-teeth", first_green=True, remediated=False)

    out = rehydrate_config_from_cells(tmp_path, {"record_id": "r"})

    assert out["resolved_models"] == ["claude-haiku-4-5"]
    assert out["cycles"] == {
        "docs-triage": {"first_passed": False, "remediated": True},
        "instrument-teeth": {"first_passed": True, "remediated": False},
    }
    assert out["spend_by_leg"] == {
        "candidate_attempt": 3.0, "candidate_remediation": 0.5, "review": 4.0,
    }
    assert "record_id" in out, "the pins it was handed survive"


def test_rehydration_reads_the_field_every_harness_fills(tmp_path):
    """Codex writes `resolved_model` and no `canonical_model`. Reading the Claude-Code-shaped
    key alone drops that arm's identity and downgrades its aggregate to "none reported"."""
    _cell(tmp_path, "cell-a", "docs-triage", first_green=True, remediated=False,
          model_key="resolved_model")
    assert rehydrate_config_from_cells(tmp_path, {})["resolved_models"] == ["claude-haiku-4-5"]


def test_rehydration_never_overwrites_what_the_batch_already_knows(tmp_path):
    _cell(tmp_path, "cell-a", "docs-triage", first_green=True, remediated=False)
    out = rehydrate_config_from_cells(tmp_path, {"resolved_models": ["from-the-batch"]})
    assert out["resolved_models"] == ["from-the-batch"]


def test_a_gateway_receipt_outranks_whatever_the_harness_says():
    identity = _headline_identity(
        {
            "resolved_models": [REQUESTED],
            "gateway_resolved": {
                "models": ["meta/muse-spark-1.2-20260805"],
                "upstreams": ["Meta"],
            },
        },
        {"vendor": "meta", "model": REQUESTED},
    )
    assert identity.name == "meta/muse-spark-1.2-20260805"
    assert "served by Meta" in identity.provenance
    assert "gateway's own per-generation receipts" in identity.provenance


def test_a_harness_echoing_the_alias_back_says_it_cannot_identify_the_server():
    """Claude Code's usage map is keyed by the alias it was handed, preset suffix and all. It
    is still evidence the launch was configured as intended — but not evidence of what served
    the calls, and the headline must not let those read the same."""
    identity = _headline_identity(
        {"resolved_models": [REQUESTED]},
        {"vendor": "meta", "model": REQUESTED},
    )
    assert identity.name == REQUESTED
    assert "reported back exactly the alias it was given" in identity.provenance
    assert "NOT evidence of which model served the calls" in identity.provenance


def test_a_usage_map_keyed_by_a_real_model_name_stays_ordinary_evidence():
    """Waves 1 and 2: a silent fallback to another model would show as a different key, so
    this is a real observation and must not be downgraded."""
    identity = _headline_identity(
        {"resolved_models": ["claude-haiku-4-5"]},
        {"vendor": "anthropic", "model": "claude-haiku-4-5"},
    )
    assert identity.name == "anthropic/claude-haiku-4-5"
    assert "what each run's own report said it resolved to" in identity.provenance
    assert "reported back exactly the alias" in identity.provenance, (
        "requested and resolved are equal here too, and saying so is the honest reading"
    )


def test_a_resolution_that_differs_from_the_request_carries_no_caveat():
    identity = _headline_identity(
        {"resolved_models": ["claude-haiku-4-5-20251001"]},
        {"vendor": "anthropic", "model": "claude-haiku-4-5"},
    )
    assert identity.name == "anthropic/claude-haiku-4-5-20251001"
    assert "reported back exactly the alias" not in identity.provenance


def test_the_vendor_prefix_is_not_doubled_onto_a_qualified_id():
    """`meta` + `meta/muse-spark-1.2` read as `meta/meta/muse-spark-1.2` in wave 4."""
    identity = _headline_identity(
        {"resolved_models": ["meta/muse-spark-1.2-20260805"]},
        {"vendor": "meta", "model": REQUESTED},
    )
    assert identity.name == "meta/muse-spark-1.2-20260805"


def test_no_resolution_at_all_says_so():
    identity = _headline_identity({}, {"vendor": "openai", "model": "gpt-5.6-luna"})
    assert identity.name == "openai/gpt-5.6-luna"
    assert "No resolved model id was reported" in identity.provenance
