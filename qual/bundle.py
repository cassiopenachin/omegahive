"""The captured-artifacts *bundle* — the interface between capture (slice 2) and
grading (slice 1).

One bundle = the artifacts of a single (scenario × model × rep) run. The metrics
module (`qual.metrics`) is a pure function of a bundle plus its scenario/catalog.

Provenance split (important):
  - `turns[*].events` and telemetry are produced by the in-repo event log — the
    formats here mirror `omegahive.events` and are concrete today.
  - `turns[*].lines` (the parse trace) and `history` are **fork-runtime** artifacts.
    In slice 2 the runner fills the parse trace by replaying each `[LLM_RAW]` line
    through the fork's *own* `sread` (pre-repair) and `helper.balance_parentheses`
    (post-repair) inside the container, and normalizes `memory/history.metta` into
    `history`. Slice 1 never runs a model; it validates this schema and grades
    hand-authored canned instances.

The bundle is organized by **turn** (one coordinator cycle). Within a turn, `lines`
are in emission order and `events` in board-arrival order — the two orders are what
the batch-order-sanity metric compares.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ParseLine(BaseModel):
    """One command line the model emitted this turn, with the outcomes the fork's
    parser/repair layer produced for it."""

    raw: str
    parses_pre_repair: bool           # sread alone accepts it (no balance_parentheses)
    parses_post_repair: bool          # accepted after balance_parentheses repair
    emitted_head: str = ""            # parsed command head; "" if unparseable
    emitted_args: list[str] = Field(default_factory=list)
    dispatched_op: bool = False       # produced a port op emission (accepted OR rejected)
    arrival_index: int | None = None  # order this line's op reached the board (batch-order)
    results_echo: str = ""            # the LAST_SKILL_USE_RESULTS echo for this line


class EventRecord(BaseModel):
    """One event this turn produced — mirrors `omegahive.events.envelope.Event`
    (flattened actor). A refused coordinator op appears as a `gateway.rejected`
    event whose payload is the `GatewayRejected` shape (code, refused_event_type,
    original_actor_role, …)."""

    event_type: str
    actor_role: str
    actor_id: str
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)


class TurnCapture(BaseModel):
    index: int                                     # 1-based turn number in the script
    lines: list[ParseLine] = Field(default_factory=list)   # emission order
    events: list[EventRecord] = Field(default_factory=list)  # board-arrival order


class HistoryEntry(BaseModel):
    """A normalized `history.metta` entry. `kind` distinguishes the loop's memory
    operations relevant to pin discipline: a `pin_set` records the objective, a
    `pin_ref` re-references it. (The exact on-disk pin S-expression convention is
    finalized in slice 2's history adapter; this is the normalized view metrics read.)"""

    turn: int
    kind: str = "episode"   # one of: pin_set | pin_ref | episode | recall
    text: str = ""


class TurnTelemetry(BaseModel):
    turn: int
    tokens: int = 0
    usd: float = 0.0
    wall_ms: int = 0


class Telemetry(BaseModel):
    per_turn: list[TurnTelemetry] = Field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens for t in self.per_turn)

    @property
    def total_usd(self) -> float:
        return sum(t.usd for t in self.per_turn)

    @property
    def total_wall_ms(self) -> int:
        return sum(t.wall_ms for t in self.per_turn)


class BundleMeta(BaseModel):
    scenario_id: str
    model: str
    rep: int
    turns_played: int


class Bundle(BaseModel):
    meta: BundleMeta
    turns: list[TurnCapture] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    telemetry: Telemetry = Field(default_factory=Telemetry)
