"""The daily-heartbeat accumulator — what the notifier remembers between heartbeats.

The heartbeat is a once-a-day liveness message derived **only** from the notifier's own
cursor stream and this state (no board fold, no new read scope). This module holds the
between-heartbeat tally and open-blocks set, folded over the same event stream the pager
reads, and persisted in the existing cursor state file alongside the read cursor.

What it tracks:
  - `counts`: attention events observed since the last heartbeat, per type (reset each
    heartbeat). Head delta is analogous — both are "since the previous heartbeat".
  - `open_blocks`: `task.blocked` seen without a subsequent `task.unblocked`, per task id,
    with a first-seen timestamp (the event's wall time if the envelope carries one, else
    the tick time). Task ids only — no refs, no content, no titles.
  - `last_date` / `last_hour`: when the last heartbeat went out, so a restart never
    double-sends (the day is the idempotence key).
  - `head`: the spine head recorded at the last heartbeat, for the +N/24h delta.

Serialization is `.get`-based and additive: an old cursor-only state file (no heartbeat
block) loads into a clean default, and unknown keys are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..events.envelope import Event

# the four attention types the heartbeat tallies, in message order.
_COUNT_KEYS = ("question", "blocked", "escalated", "result")


def _empty_counts() -> dict[str, int]:
    return {k: 0 for k in _COUNT_KEYS}


@dataclass
class HeartbeatState:
    """Mutable between-heartbeat accumulator. Persisted as one JSON object."""

    last_date: str | None = None
    last_hour: int | None = None
    head: int | None = None
    counts: dict[str, int] = field(default_factory=_empty_counts)
    open_blocks: dict[str, str] = field(default_factory=dict)  # task_id -> first-seen ISO

    def observe(self, event: Event, now: datetime) -> None:
        """Fold one event into the tally. Called once per event as the read cursor passes
        it (so a held-cursor retry never double-counts). Non-attention events are ignored
        except `task.unblocked`, which clears an open block."""
        et = event.event_type
        if et == "task.reported":
            if event.payload.get("kind") == "question":
                self.counts["question"] = self.counts.get("question", 0) + 1
        elif et == "task.blocked":
            self.counts["blocked"] = self.counts.get("blocked", 0) + 1
            tid = event.task_id or "—"
            if tid not in self.open_blocks:
                seen = event.wall_ts if event.wall_ts is not None else now
                self.open_blocks[tid] = seen.isoformat()
        elif et == "task.unblocked":
            if event.task_id is not None:
                self.open_blocks.pop(event.task_id, None)
        elif et == "task.escalated":
            self.counts["escalated"] = self.counts.get("escalated", 0) + 1
        elif et == "task.result_posted":
            self.counts["result"] = self.counts.get("result", 0) + 1

    def roll(self, date: str, hour: int, head: int) -> None:
        """A heartbeat just went out: record when + the current head, and reset the tally.
        Open blocks are NOT reset — a block stays open until its `task.unblocked` arrives."""
        self.last_date = date
        self.last_hour = hour
        self.head = head
        self.counts = _empty_counts()

    def open_block_ages(self, now: datetime) -> list[tuple[str, int]]:
        """(task_id, age_in_hours) for each open block, oldest first."""
        out: list[tuple[str, datetime]] = []
        for tid, seen_iso in self.open_blocks.items():
            try:
                seen = datetime.fromisoformat(seen_iso)
            except ValueError:
                seen = now
            # tolerate a naive stored time by assuming it shares 'now's tzinfo.
            if seen.tzinfo is None and now.tzinfo is not None:
                seen = seen.replace(tzinfo=now.tzinfo)
            out.append((tid, seen))
        out.sort(key=lambda p: p[1])
        return [(tid, max(0, int((now - seen).total_seconds() // 3600))) for tid, seen in out]

    def to_dict(self) -> dict:
        return {
            "last_date": self.last_date,
            "last_hour": self.last_hour,
            "head": self.head,
            "counts": dict(self.counts),
            "open_blocks": dict(self.open_blocks),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> HeartbeatState:
        if not data:
            return cls()
        counts = _empty_counts()
        raw = data.get("counts")
        if isinstance(raw, dict):
            for k in _COUNT_KEYS:
                v = raw.get(k)
                if isinstance(v, int):
                    counts[k] = v
        blocks = data.get("open_blocks")
        open_blocks = (
            {str(k): str(v) for k, v in blocks.items()} if isinstance(blocks, dict) else {}
        )
        return cls(
            last_date=data.get("last_date"),
            last_hour=data.get("last_hour"),
            head=data.get("head"),
            counts=counts,
            open_blocks=open_blocks,
        )
