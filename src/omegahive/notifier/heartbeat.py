"""The daily-heartbeat accumulator — what the notifier remembers between heartbeats.

The heartbeat is a once-a-day liveness message derived **only** from the notifier's own
cursor stream and this state (no board fold, no new read scope). One notifier watches the
whole spine, so the state is two layers: a per-run tally (`RunHeartbeat`) folded over that
run's event stream, and one portfolio wrapper (`PortfolioHeartbeat`) holding the runs plus
the single send schedule — **one heartbeat total**, not one per run.

What a run's tally tracks:
  - `counts`: attention events observed since the last heartbeat, per type (reset each
    heartbeat). Head delta is analogous — both are "since the previous heartbeat".
  - `open_blocks`: `task.blocked` seen without a subsequent `task.unblocked`, per task id,
    with a first-seen timestamp (the event's wall time if the envelope carries one, else
    the tick time). Task ids only — no refs, no content, no titles.
  - `head`: the run's spine head recorded at the last heartbeat, for the +N/24h delta.

What the portfolio wrapper adds:
  - `last_date` / `last_hour`: when the last heartbeat went out, so a restart never
    double-sends (the day is the idempotence key) — one schedule for all runs.
  - `runs`: the per-run tallies, pruned as runs leave the active portfolio.

Serialization is `.get`-based and additive. A **legacy** single-run state file (the
pre-portfolio notifier's flat `head`/`counts`/`open_blocks`) loads with its schedule kept —
so a cutover on a day whose heartbeat already went out does not send a second one — and its
tally dropped, because the portfolio re-arms every run at its current head (cursor.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..events.envelope import Event

# the four attention types the heartbeat tallies, in message order.
_COUNT_KEYS = ("question", "blocked", "escalated", "result")


def _empty_counts() -> dict[str, int]:
    return {k: 0 for k in _COUNT_KEYS}


@dataclass
class RunHeartbeat:
    """One run's between-heartbeat accumulator."""

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

    def roll(self, head: int | None) -> None:
        """A heartbeat just went out: record this run's current head and reset the tally.
        Open blocks are NOT reset — a block stays open until its `task.unblocked` arrives."""
        if head is not None:
            self.head = head
        self.counts = _empty_counts()

    def quiet(self) -> bool:
        """No attention at all in the window — the shape a stalled run makes."""
        return not any(self.counts.get(k, 0) for k in _COUNT_KEYS)

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
            "head": self.head,
            "counts": dict(self.counts),
            "open_blocks": dict(self.open_blocks),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> RunHeartbeat:
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
        return cls(head=data.get("head"), counts=counts, open_blocks=open_blocks)


@dataclass
class PortfolioHeartbeat:
    """Every followed run's tally plus the one send schedule they share."""

    last_date: str | None = None
    last_hour: int | None = None
    runs: dict[str, RunHeartbeat] = field(default_factory=dict)

    def for_run(self, run_id: str) -> RunHeartbeat:
        """This run's tally, created on first sight."""
        return self.runs.setdefault(run_id, RunHeartbeat())

    def roll(self, date: str, hour: int, heads: dict[str, int | None]) -> None:
        """A heartbeat just went out: record when, and roll every run's tally onto the head
        the message reported for it. A run with no head this cycle keeps its previous one."""
        self.last_date = date
        self.last_hour = hour
        for run_id, head in heads.items():
            self.for_run(run_id).roll(head)

    def prune(self, run_ids: Iterable[str]) -> bool:
        """Drop the tallies of runs no longer in the active portfolio. Returns True if
        anything was dropped. A run that comes back is a first sight again — it re-arms at
        head rather than replaying a dormancy's worth of history as fresh pages."""
        keep = set(run_ids)
        gone = [r for r in self.runs if r not in keep]
        for run_id in gone:
            del self.runs[run_id]
        return bool(gone)

    def open_block_ages(self, now: datetime) -> list[tuple[str, str, int]]:
        """(run_id, task_id, age_in_hours) across every followed run, oldest first."""
        out = [
            (run_id, tid, age)
            for run_id, hb in self.runs.items()
            for tid, age in hb.open_block_ages(now)
        ]
        out.sort(key=lambda row: (-row[2], row[0], row[1]))
        return out

    def to_dict(self) -> dict:
        return {
            "last_date": self.last_date,
            "last_hour": self.last_hour,
            "runs": {run_id: hb.to_dict() for run_id, hb in self.runs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> PortfolioHeartbeat:
        """Load the portfolio state. A legacy single-run file (no `runs` key) keeps only its
        send schedule: the tally belonged to one run under a cursor the portfolio does not
        adopt, so carrying it would print a delta against a head nothing will match."""
        if not data:
            return cls()
        raw = data.get("runs")
        runs = (
            {str(k): RunHeartbeat.from_dict(v) for k, v in raw.items()}
            if isinstance(raw, dict)
            else {}
        )
        return cls(
            last_date=data.get("last_date"),
            last_hour=data.get("last_hour"),
            runs=runs,
        )


@dataclass(frozen=True)
class RunDelta:
    """One run's line in the heartbeat: how far it moved, how far behind we are, what
    landed. The render takes these already computed, so the message is a pure function of
    what the service observed."""

    run_id: str
    head: int
    delta: int
    lag: int
    counts: dict[str, int]

    @property
    def quiet(self) -> bool:
        return not any(self.counts.get(k, 0) for k in _COUNT_KEYS)
