"""Dated price table for the frozen grid run (§7). USD is computed **post-hoc** from the
recorded token counts against a table pinned at freeze — not from litellm's live map — so a
model litellm doesn't price (the V2b silent-$0 bug) can never leave a cell's cost unrecorded.

Table (JSON): `{"date": "2026-07-09", "models": {"<model_id>": {"input_usd_per_mtok": float,
"output_usd_per_mtok": float}}}`. Every cell model MUST have a row (the freeze validator
enforces it); `price()` raises on a missing model rather than silently returning $0.
"""

from __future__ import annotations

import json
from pathlib import Path


class UnpricedModel(KeyError):
    """A model with no row in the pinned price table — a hard error, never a silent $0."""


def load_table(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def priced_models(table: dict) -> set[str]:
    return set(table.get("models", {}))


def price(table: dict, model: str, tokens_in: int, tokens_out: int) -> float:
    row = table.get("models", {}).get(model)
    if row is None:
        raise UnpricedModel(
            f"{model!r} has no row in the price table dated {table.get('date')!r}")
    return (tokens_in / 1_000_000) * row["input_usd_per_mtok"] + \
           (tokens_out / 1_000_000) * row["output_usd_per_mtok"]
