"""The op-reference sheet = R1's system prompt (stage 2 V2b Phase 2 / §5.1 symmetry).
Pure — no DB."""

from __future__ import annotations

from ladder.opsheet import op_reference_sheet
from qual.loader import QUAL_ROOT, load_catalog

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v1.yaml")


def test_sheet_op_lines_are_exactly_the_catalog_texts_in_order():
    sheet = op_reference_sheet(CATALOG)
    op_lines = [ln[2:] for ln in sheet.splitlines() if ln.startswith("- ")]
    assert op_lines == [e.text for e in CATALOG.entries]     # structural symmetry, nothing extra
    assert any(line.startswith("Abandon a doomed") for line in op_lines)  # prune present


def test_sheet_carries_no_strategy_or_norms():
    # §5.1: syntax/semantics/legality only — strategy (when to prune, evidence thresholds)
    # lives in the KB (V3), never in the shared op sheet.
    low = op_reference_sheet(CATALOG).lower()
    for word in ("evidence", "threshold", "recommend", "consecutive", "should prune",
                 "strategy"):
        assert word not in low
