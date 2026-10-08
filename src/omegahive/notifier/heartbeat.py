"""The daily-heartbeat accumulator — what the notifier remembers between heartbeats.

The heartbeat is a once-a-day liveness message. Its counts come from the notifier's own
cursor stream and this state; its open blocks come from each run's board, read when the
heartbeat goes out (service.py), because only the board knows which tasks are blocked now.
One notifier watches the
whole spine, so the state is two layers: a per-run tally (`RunHeartbeat`) folded over that
run's event stream, and one portfolio wrapper (`PortfolioHeartbeat`) holding the runs plus
the single send schedule — **one heartbeat total**, not one per run.

What a run's tally tracks:
  - `counts`: attention events observed since the last heartbeat, per type (reset each
    heartbeat). Head delta is analogous — both are "since the previous heartbeat". A
    pre-`worker-turns` state file has no `exit` key and loads with it at zero; `.get`
    everywhere is what makes that additive rather than a migration.
  - `head`: the run's spine head recorded at the last heartbeat, for the +N/24h delta.

What the portfolio wrapper adds:
  - `last_date` / `last_hour`: when the last heartbeat went out, so a restart never
    double-sends (the day is the idempotence key) — one schedule for all runs.
  - `runs`: the per-run tallies. A run's tally is **kept** when it leaves the active
    window — a departed run's cursor already sits at its head, so resuming from it replays
    nothing, while forgetting it would swallow the first attention event of its return.
    The heartbeat scopes what it *shows* to the runs currently followed instead.

Serialization is `.get`-based and additive. A **legacy** single-run state file (the
pre-portfolio notifier's flat `head`/`counts`) loads with its schedule kept —
so a cutover on a day whose heartbeat already went out does not send a second one — and its
tally dropped, because the portfolio re-arms every run at its current head (cursor.py). An
`open_blocks` map that an older notifier saved is ignored on load and dropped on the next
save.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..events.envelope import Event
from .events import ATTENTION_CLASSIFICATIONS

# the attention types the heartbeat tallies, in message order. `exit` was added by
# `worker-turns`: a turn that ended with no worker terminal event. It is counted here for
# the same reason it is notified — a run whose workers keep dying unrecorded looks quiet
# on every other counter, which is the exact shape a stalled run makes.
_COUNT_KEYS = ("question", "blocked", "escalated", "result", "exit")


def _empty_counts() -> dict[str, int]:
    return {k: 0 for k in _COUNT_KEYS}


@dataclass
class RunHeartbeat:
    """One run's between-heartbeat accumulator."""

    head: int | None = None
    counts: dict[str, int] = field(default_factory=_empty_counts)

    def observe(self, event: Event, now: datetime) -> None:
        """Fold one event into the tally. Called once per event as the read cursor passes
        it (so a held-cursor retry never double-counts). Non-attention events are ignored."""
        et = event.event_type
        if et == "task.reported":
            if event.payload.get("kind") == "question":
                self.counts["question"] = self.counts.get("question", 0) + 1
        elif et == "task.blocked":
            self.counts["blocked"] = self.counts.get("blocked", 0) + 1
        elif et == "task.escalated":
            self.counts["escalated"] = self.counts.get("escalated", 0) + 1
        elif et == "task.result_posted":
            self.counts["result"] = self.counts.get("result", 0) + 1
        elif et == "execution.finished":
            # Gated on the same classifications the ping is gated on, so the tally and
            # the channel can never disagree about what counted as attention.
            if event.payload.get("classification") in ATTENTION_CLASSIFICATIONS:
                self.counts["exit"] = self.counts.get("exit", 0) + 1

    def roll(self, head: int | None) -> None:
        """A heartbeat just went out: record this run's current head and reset the tally."""
        if head is not None:
            self.head = head
        self.counts = _empty_counts()

    def quiet(self) -> bool:
        """No attention at all in the window — the shape a stalled run makes."""
        return not any(self.counts.get(k, 0) for k in _COUNT_KEYS)

    def to_dict(self) -> dict:
        return {
            "head": self.head,
            "counts": dict(self.counts),
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
        return cls(head=data.get("head"), counts=counts)


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
