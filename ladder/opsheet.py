"""The op-reference sheet — R1's *entire* system prompt (stage 2 §5.1). It is the op
catalog's syntax/semantics/legality lines and nothing else: no norms, no strategy (those
live only in the KB, V3). Deriving it from the same `board-ops-v2.yaml` the fork `board`
skill is built from makes "identical op sheet across R1 and the skill" a structural fact,
not a copy — so a comparison measures architecture, not documentation access.
"""

from __future__ import annotations

from qual.schema import Catalog

_HEADER = (
    "You coordinate a task board. You are given the board as an S-expression. Reply with "
    "zero or more command lines — one op per line, positional arguments, and nothing else "
    "(no prose, no explanations, no code fences). The ops you may emit:"
)
_FOOTER = (
    "An illegal op is refused; it is echoed back to you as a (rejected (op ...) :code ...) "
    "line in the next board view."
)


def op_reference_sheet(catalog: Catalog) -> str:
    ops = "\n".join(f"- {e.text}" for e in catalog.entries)
    return f"{_HEADER}\n{ops}\n{_FOOTER}"
