"""PromotionEvaluator — an instrument reactor that surfaces the human-relevant subset.

Applies the deterministic ruleset (promotion/rules.py) over the stream and emits
promotion.created{ref_event, rule_id} for the first occurrence of each
(task-or-correlation, rule_id) situation. Severity is derived at view time, never
stamped. The promotion is a child of its source (causation_id = source event id) so
it inherits the source's correlation thread and the human view can walk caused_by
back to the raw trace.

Deterministic + converges: seq-order consumption, fixed ruleset, monotone _promoted
dedup set, never schedules wakes, never re-promotes.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from ..board.reducer import Board
from ..engine.protocol import Emit, ReactResult
from ..events.envelope import Event
from ..promotion.config import PromotionConfig
from ..promotion.rules import RuleContext, board_rules, evaluate


class PromotionEvaluator:
    role = "instrument"

    def __init__(
        self, agent_id: str = "promotion", *, config: PromotionConfig | None = None
    ) -> None:
        self.agent_id = agent_id
        self.config = config or PromotionConfig()
        self._seen: list[Event] = []
        self._promoted: set[tuple[str | None, str]] = set()

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        self._seen.extend(new_events)
        thread_len: Counter[UUID] = Counter(
            e.correlation_id for e in self._seen if e.correlation_id is not None
        )
        ctx = RuleContext(thread_len=thread_len, config=self.config)
        res = ReactResult()

        for ev in new_events:  # seq order (engine guarantee)
            rule_id = evaluate(ev, ctx)
            if rule_id is None:
                continue
            key = (
                (str(ev.correlation_id), rule_id)
                if rule_id == "thread_too_long"
                else (ev.task_id, rule_id)
            )
            if key in self._promoted:
                continue
            self._promoted.add(key)
            res.immediate.append(
                Emit("promotion.created",
                     {"ref_event": str(ev.event_id), "rule_id": rule_id},
                     task_id=ev.task_id, causation_id=ev.event_id)
            )

        for tid, rule_id, ref_id in board_rules(board, now, self.config):
            if ref_id is None or (tid, rule_id) in self._promoted:
                continue
            self._promoted.add((tid, rule_id))
            res.immediate.append(
                Emit("promotion.created",
                     {"ref_event": str(ref_id), "rule_id": rule_id},
                     task_id=tid, causation_id=ref_id)
            )

        return res
