"""The op-reference sheet = R1's system prompt (stage 2 V2b Phase 2 / §5.1 symmetry).
Pure — no DB."""

from __future__ import annotations

from ladder.opsheet import op_reference_sheet
from qual.loader import QUAL_ROOT, load_catalog

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v2.yaml")

_HEADS = {"assign", "reassign", "escalate", "close", "reopen", "prune"}


def test_sheet_op_lines_are_exactly_the_catalog_texts_in_order():
    sheet = op_reference_sheet(CATALOG)
    op_lines = [ln[2:] for ln in sheet.splitlines() if ln.startswith("- ")]
    assert op_lines == [e.text for e in CATALOG.entries]     # structural symmetry, nothing extra


def test_sheet_has_all_six_heads_including_prune():
    # structural, not phrasing: the sheet must name every op by head, never by wording that
    # could leak strategy (e.g. "doomed") into the catalog text.
    heads = {e.head for e in CATALOG.entries}
    assert heads == _HEADS


def test_sheet_carries_no_strategy_or_norms():
    # §5.1: syntax/semantics/legality only — strategy (when to prune, evidence thresholds)
    # lives in the KB (V3), never in the shared op sheet. "doomed" is pinned here because it
    # was the v1 catalog's leaked strategy word (B1/B2) — a regression must not reintroduce it.
    low = op_reference_sheet(CATALOG).lower()
    for word in ("evidence", "threshold", "recommend", "consecutive", "should prune",
                 "strategy", "doomed"):
        assert word not in low
