"""Scenario / catalog / fixture schema for the qualification battery (spec §4).

Pydantic models mirroring the sim scenario pattern (`omegahive.sim.scenario.schema`).

Catalogs are **self-describing**: a catalog enumerates its own heads, and a scenario's
`op_vocabulary` must be a subset of *its* catalog's heads. Two catalog kinds exist —
the board-op catalog (`assign`/`reassign`/…/`prune`, mapped to port ops for v0b) and the
stock-skill catalog (`send`/`pin`/`query`/`board`/… advertised by the fork image, for the
v0a emission-discipline half). The cross-file `op_vocabulary ⊆ catalog.heads` invariant is
enforced in `qual.loader`, not on any model in isolation.

  - **board-mutation ops** — scripted, harness-side worker/stub actions played *between*
    turns (`complete`, `block`, …); these are not coordinator commands (v0b only).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

# The coordinator board-op heads (the port Op union; omegahive_port_spec.md §2). Reference
# constant for the board-op catalog; catalogs are no longer required to draw from it.
KNOWN_HEADS = frozenset({"assign", "reassign", "escalate", "close", "reopen", "prune"})

# Scripted worker/stub mutations the harness plays between turns. Not coordinator
# commands. The event-type each maps to is a slice-2 concern (the runner emits them
# through the port); slice 1 only validates the op name is known.
BOARD_MUTATION_OPS = frozenset(
    {"accept", "progress", "complete", "reject", "block", "unblock", "fail"}
)


class BoardMutation(BaseModel):
    actor: str
    op: str
    task: str

    @model_validator(mode="after")
    def _check_op(self) -> BoardMutation:
        if self.op not in BOARD_MUTATION_OPS:
            raise ValueError(
                f"unknown board_mutation op {self.op!r} (known: {sorted(BOARD_MUTATION_OPS)})"
            )
        return self


class Turn(BaseModel):
    """One scripted turn: either a channel injection or a between-turn board mutation.
    Exactly one of the two is set (the YAML list item carries a single key)."""

    inject: str | None = None
    board_mutation: BoardMutation | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Turn:
        set_fields = [f for f in (self.inject, self.board_mutation) if f is not None]
        if len(set_fields) != 1:
            raise ValueError("a turn must set exactly one of {inject, board_mutation}")
        return self


class RejectionInjection(BaseModel):
    turn: int
    situation: str


class HistoryFiller(BaseModel):
    chars: int
    at_turn: int


class Expected(BaseModel):
    labels: list[str] = Field(default_factory=list)
    hard_fail: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    usd: float
    max_turns: int


class Scenario(BaseModel):
    id: str
    description: str
    persona: str                    # path to the pinned persona file
    skills_catalog: str             # path to the catalog YAML
    board_fixture: str | None = None  # board seed (v0b); absent for v0a stock probes
    turns: list[Turn]
    rejection_injection: RejectionInjection | None = None
    recovery_window_K: int = 3
    history_filler: HistoryFiller | None = None
    op_vocabulary: list[str] = Field(default_factory=list)
    expected: Expected = Field(default_factory=Expected)
    budget: Budget


class CatalogEntry(BaseModel):
    head: str          # the head the agent emits, e.g. "assign" (board) or "send" (stock)
    text: str          # the catalog line the agent is shown
    arity: int         # number of positional args
    port_op: str = ""  # the omegahive.port Op this maps to (board catalogs only)


class Catalog(BaseModel):
    version: str
    entries: list[CatalogEntry]

    @property
    def heads(self) -> set[str]:
        return {e.head for e in self.entries}

    @model_validator(mode="after")
    def _unique_heads(self) -> Catalog:
        heads = [e.head for e in self.entries]
        if len(heads) != len(set(heads)):
            raise ValueError("catalog has duplicate heads")
        return self


class FixtureEvent(BaseModel):
    """A single board-seeding event. When folded through the reducer these produce the
    scenario's starting board. Slice 1 validates the shape; slice 2 emits them through
    the port as the harness actor."""

    event_type: str
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)


class Fixture(BaseModel):
    """A board fixture: an ordered list of seeding events (spec §4, "seed events via
    port"). `tasks` is an optional human-facing manifest of the task ids it creates."""

    tasks: list[str] = Field(default_factory=list)
    events: list[FixtureEvent]
