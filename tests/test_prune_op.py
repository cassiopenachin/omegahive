"""Prune op-vocabulary parity (stage 2 V2b Phase 0): the catalog head, the port Op, and
the Emit<->Op mapping all know about `task.pruned`. Pure — no DB."""

from __future__ import annotations

from uuid import uuid4

from qual.loader import QUAL_ROOT, load_catalog
from qual.schema import KNOWN_HEADS, Scenario

from omegahive.port import PruneOp
from omegahive.sim.engine.protocol import Emit
from omegahive.sim.reference_client import emit_to_op

VALID_SCENARIO = {
    "id": "X", "description": "x", "persona": "p.txt",
    "skills_catalog": "catalogs/board-ops-v2.yaml", "board_fixture": "f.json",
    "turns": [{"inject": "hi"}], "op_vocabulary": ["prune"],
    "budget": {"usd": 0.5, "max_turns": 8},
}


def test_prune_op_renders_task_pruned():
    op = PruneOp(task_id="A", reason="doomed")
    assert op.to_emit() == ("task.pruned", {"reason": "doomed"}, "A")
    assert PruneOp(task_id="A").to_emit() == ("task.pruned", {"reason": None}, "A")


def test_emit_to_op_maps_task_pruned_to_prune_op():
    cause = uuid4()
    op = emit_to_op(Emit("task.pruned", {"reason": "abandon"}, task_id="A", causation_id=cause))
    assert isinstance(op, PruneOp)
    assert op.task_id == "A" and op.reason == "abandon" and op.causation_id == cause


def test_prune_is_a_known_head_and_in_the_catalog():
    assert "prune" in KNOWN_HEADS
    catalog = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v2.yaml")
    assert "prune" in catalog.heads
    (entry,) = [e for e in catalog.entries if e.head == "prune"]
    assert entry.arity == 1 and entry.port_op == "PruneOp"


def test_scenario_accepts_prune_in_op_vocabulary():
    Scenario.model_validate(VALID_SCENARIO)   # no ValidationError -> prune is an accepted head
