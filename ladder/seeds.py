"""The pre-registered seed generator for the fork board (stage 2 §6).

Each seed draws the *environment* deterministically and identically across cells:
branch A's fate (doomed, or recovers only after a long tail), branch B's
attempts-to-success (m_B), and the evidence threshold at which pruning A becomes
justified. The board topology (ladder/board.py) is fixed; only these draws vary.

An "attempt" on a branch is the number of *distinct* workers tried on it — which the
greedy coordinator grows by one per reassignment (assign → fail → reopen → reassign to
an untried worker). A worker decides success from `len(task.tried_by)` at its turn.

These constants ARE the pre-registration (design §9.1, set at board-authoring time,
reviewed by the adjudicator, frozen before any LLM run — §8).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from random import Random

N_SEEDS = 20
ROSTER_SIZE = 9                               # worker pool; bounds A's attempts before escalation
RECOVER_SEEDS = frozenset({2, 6, 9, 13, 17})  # 5 of 20: A recovers after a long tail
A_RECOVER_ATTEMPT_RANGE = (6, 9)              # attempt a recovering A finally succeeds
B_SUCCESS_ATTEMPT_RANGE = (2, 4)              # m_B: the attempt B succeeds
EVIDENCE_K = 3                                # A's kth consecutive failure ⇒ prune justified

BRANCH_TASKS = ("A", "B")                         # the fork branches; other tasks always succeed


@dataclass(frozen=True)
class SeedSchedule:
    seed: int
    a_recovers: bool
    a_success_attempt: int | None   # 1-based attempt A succeeds (recover seeds); None = doomed
    b_success_attempt: int          # m_B
    evidence_k: int
    roster: tuple[str, ...]

    def succeeds(self, branch: str, attempt: int) -> bool:
        """Does branch A/B's `attempt`-th distinct worker produce an ok result?
        Non-branch tasks (the join, the tail) are not this generator's concern and
        always succeed — the worker handles them directly."""
        if branch == "A":
            return self.a_recovers and attempt == self.a_success_attempt
        if branch == "B":
            return attempt == self.b_success_attempt
        return True


def _rng(seed: int) -> Random:
    digest = hashlib.sha256(f"ladder-fork:{seed}".encode()).digest()[:8]
    return Random(int.from_bytes(digest, "big"))


def schedule_for(seed: int) -> SeedSchedule:
    r = _rng(seed)
    # Draw both attempt counts in a fixed order regardless of a_recovers, so the RNG
    # stream — and thus every other draw — is stable no matter the recover-set membership.
    a_attempt = r.randint(*A_RECOVER_ATTEMPT_RANGE)
    b_attempt = r.randint(*B_SUCCESS_ATTEMPT_RANGE)
    a_recovers = seed in RECOVER_SEEDS
    return SeedSchedule(
        seed=seed,
        a_recovers=a_recovers,
        a_success_attempt=a_attempt if a_recovers else None,
        b_success_attempt=b_attempt,
        evidence_k=EVIDENCE_K,
        roster=tuple(f"w{i}" for i in range(1, ROSTER_SIZE + 1)),
    )


def all_schedules() -> list[SeedSchedule]:
    return [schedule_for(s) for s in range(N_SEEDS)]
