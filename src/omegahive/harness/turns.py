"""One turn: what the harness said, what the spine says, and the one classification.

A **turn** is one harness process, from kickoff or resume prompt to process exit. The
process may disappear between turns; the durable native session id, the workspace state,
the report and the spine are the worker. This module owns the two pure decisions that
turn a finished process into a record an operator can act on:

  `scan_stream`  the harness's own structured output -> normalized facts
  `classify`     those facts + the spine after a saved cursor -> one classification

**Two authorities, never one.** The spine owns TASK DISPOSITION: only the worker may say
its task posted, blocked or failed, and only an accepted event counts. The harness's
structured output owns PROCESS TERMINATION: whether the process completed, errored, or
ran out of budget. Neither is allowed to speak for the other. An OS exit code never
becomes `task.failed`; a task event never becomes evidence about the process; and no
classification is ever derived from the assistant's prose, the terminal screen, a branch
name or a report's contents.

**Refusing to guess is part of the result type.** Every exit produces either
`classified(posted|blocked|failed|budget)` or `unclassified(<reason>)`. There is no
third state and no "probably" — an `unclassified` record carries the raw evidence and
says which half of it was missing, which is what makes it actionable rather than a log
warning nobody reads.

The functions here read no file, no clock, no database and no environment. The turn
runner in `hive-launch --turn` owns the side effects: it saves the cursor, runs the
harness, retains the stream, reads the spine and calls these. That separation is what
makes a re-classification of the same saved stream and cursor produce byte-identical
normalized evidence, which the order requires and `tests/test_harness_turns.py` asserts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# The three worker-owned events that dispose of a task. Nothing else on the spine — not
# `task.accepted`, not `task.reported`, not `task.unblocked` — answers "how did this turn
# end for the task", and treating any of them as an answer is how a progress report
# becomes a terminal fact.
TASK_DISPOSITIONS = ("task.result_posted", "task.blocked", "task.failed")

# What a harness's terminal record can be, normalized across vendors. `budget` is only
# ever reached from a MEASURED, allowlisted structured signal; a vendor that exposes none
# returns `unknown` and lands in `failed` or `unclassified`, never a fabricated budget
# pass.
HarnessKind = Literal["completed", "error", "budget", "missing", "malformed"]

Classification = Literal["posted", "blocked", "failed", "budget", "unclassified"]


@dataclass(frozen=True)
class HarnessTerminal:
    """The harness's own last word about its process, normalized.

    `reason` is the vendor's terminal reason verbatim when this build has MEASURED it
    (see each adapter's allowlist), and `unknown` otherwise. Keeping the vendor string
    rather than only the normalized kind is deliberate: the kind is what the classifier
    branches on, and the reason is what an operator reads when the kind is not enough.
    """

    kind: HarnessKind
    reason: str = "unknown"
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class TurnFacts:
    """Everything the retained structured stream establishes about one turn.

    Every field is either read out of a structured record or left absent. Nothing here
    is inferred from prose, and `unavailable_reason` says why when a field is absent, so
    "the harness does not expose this" and "we did not look" stay distinguishable.
    """

    terminal: HarnessTerminal
    session_id: str | None = None
    model_resolved: str | None = None
    harness_version: str | None = None
    usage: dict[str, Any] | None = None
    records: int = 0
    malformed: int = 0
    truncated: bool = False
    digest: str = ""
    unavailable_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "terminal": self.terminal.to_json(),
            "session_id": self.session_id,
            "model_resolved": self.model_resolved,
            "harness_version": self.harness_version,
            "usage": self.usage,
            "records": self.records,
            "malformed": self.malformed,
            "truncated": self.truncated,
            "digest": self.digest,
            "unavailable_reason": self.unavailable_reason,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ExitRecord:
    """The operator-facing classification, plus the evidence it was derived from.

    `classification` is the one answer. `task_disposition` and `harness_terminal_reason`
    are the two authorities that produced it, kept separately so a reader can always see
    which half spoke — and `harness_failed_after_disposition` preserves the secondary
    fact when a harness errors during shutdown after the worker already posted.
    """

    classification: Classification
    reason: str
    task_disposition: str | None
    terminal_event_seq: int | None
    harness_terminal_kind: HarnessKind
    harness_terminal_reason: str
    exit_code: int | None
    spine_cursor: int | None
    spine_basis: Literal["read", "unavailable"]
    harness_failed_after_disposition: bool = False
    considered_events: int = 0

    @property
    def needs_attention(self) -> bool:
        """Whether this exit is one the operator must be told about directly.

        `posted` and `blocked` already produce their own `task.*` notification, so
        re-notifying here would be a second message about one event — which is how a
        notification channel stops being read. Everything else has no task event behind
        it by construction, which is exactly why it needs one.
        """
        return self.classification in ("failed", "budget", "unclassified")

    def to_json(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "classification_reason": self.reason,
            "task_disposition": self.task_disposition,
            "terminal_event_seq": self.terminal_event_seq,
            "harness_terminal_kind": self.harness_terminal_kind,
            "harness_terminal_reason": self.harness_terminal_reason,
            "exit_code": self.exit_code,
            "spine_cursor": self.spine_cursor,
            "spine_basis": self.spine_basis,
            "harness_failed_after_disposition": self.harness_failed_after_disposition,
            "considered_events": self.considered_events,
        }


def stream_digest(raw: bytes) -> str:
    """`sha256:<hex>` over the retained stream's EXACT bytes.

    Over bytes, like the catalog digest and for the same reason: the question a later
    reader asks is "is this the stream that produced that classification", and a digest
    over a re-serialization answers a weaker question. A malformed or truncated stream
    digests exactly as well as a clean one, which is the point — the evidence is
    preserved and fingerprinted whatever shape it arrived in.
    """
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def parse_stream(raw: str) -> tuple[list[dict[str, Any]], int, bool]:
    """Split a JSONL stream into records, counting what would not parse.

    Returns `(records, malformed, truncated)`. A line that is not a JSON object is
    counted as malformed and DROPPED FROM THE RECORDS, never repaired — the raw bytes
    stay on disk and the count travels on the fact. `truncated` is true when the stream's
    final line has no trailing newline and does not parse, which is what a killed
    harness leaves behind; a well-formed last line without a newline is not truncation.
    """
    records: list[dict[str, Any]] = []
    malformed = 0
    truncated = False
    lines = raw.splitlines()
    ends_clean = raw.endswith("\n")
    for i, line in enumerate(lines):
        text = line.strip()
        if not text:
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            malformed += 1
            if i == len(lines) - 1 and not ends_clean:
                truncated = True
            continue
        if isinstance(doc, dict):
            records.append(doc)
        else:
            malformed += 1
    return records, malformed, truncated


def classify(
    *,
    spine_events: Iterable[Mapping[str, Any]],
    facts: TurnFacts,
    exit_code: int | None,
    cursor: int | None,
    run: str,
    task: str,
    worker: str,
    spine_readable: bool = True,
) -> ExitRecord:
    """Derive the one classification from the two authorities. Pure and idempotent.

    `spine_events` is whatever the caller could read; the scoping is done HERE rather
    than trusted from the caller, so a broader query cannot widen the answer. Only
    events on this run, for this task, authored by this worker in the `worker` role, and
    strictly after `cursor` can count. Everything else is another turn's evidence, or
    somebody else's.

    A present event IS an accepted event: the spine stores what it accepted and nothing
    else, so a refused emit leaves no row to find. That is why "only an accepted event
    counts" needs no extra field — it is a property of where we are reading.
    """
    if not spine_readable:
        # The harness evidence is still retained and still reported; what is missing is
        # the half that could name a task disposition, so no classification is possible
        # and none is invented. A later reconciliation reading the same saved cursor and
        # stream recomputes the same record.
        return ExitRecord(
            classification="unclassified",
            reason="spine_unavailable",
            task_disposition=None,
            terminal_event_seq=None,
            harness_terminal_kind=facts.terminal.kind,
            harness_terminal_reason=facts.terminal.reason,
            exit_code=exit_code,
            spine_cursor=cursor,
            spine_basis="unavailable",
        )

    if cursor is None:
        # A READABLE spine and no cursor is the most dangerous combination available here,
        # and it is refused rather than widened. Without the position the turn started
        # from there is nothing to scope to: every event this worker ever emitted for this
        # task looks current, so a turn that said nothing would be confidently classified
        # from a block an hour old. That is the order's named risk exactly, and "read the
        # whole history instead" is not a degraded answer to it — it is a wrong one that
        # looks right.
        #
        # This costs an `unclassified` on the rare turn whose pre-start head read failed.
        # The evidence is intact, the operator is told, and `hive-answer --resume-only`
        # continues the worker. That is the correct trade: a missing cursor is a knowable
        # gap, and a confident classification derived from an unknowable window is not.
        return ExitRecord(
            classification="unclassified",
            reason="cursor_unavailable: the spine head could not be read before this turn "
                   "started, so no event can be placed relative to it",
            task_disposition=None,
            terminal_event_seq=None,
            harness_terminal_kind=facts.terminal.kind,
            harness_terminal_reason=facts.terminal.reason,
            exit_code=exit_code,
            spine_cursor=None,
            spine_basis="read",
        )

    scoped = [
        ev
        for ev in spine_events
        if ev.get("run_id") == run
        and ev.get("task_id") == task
        and _actor_role(ev) == "worker"
        and _actor_id(ev) == worker
        and _seq_after(ev, cursor)
    ]
    dispositions = [ev for ev in scoped if ev.get("event_type") in TASK_DISPOSITIONS]
    kinds = {str(ev.get("event_type")) for ev in dispositions}
    harness_errored = facts.terminal.kind in ("error", "budget", "missing", "malformed")

    if len(kinds) > 1:
        # Two different terminal claims about one task inside one turn. Neither is
        # discardable and neither can be preferred without inventing a rule the worker
        # protocol does not have, so the record says so and keeps both.
        return ExitRecord(
            classification="unclassified",
            reason="conflicting_task_dispositions: " + ", ".join(sorted(kinds)),
            task_disposition=None,
            terminal_event_seq=None,
            harness_terminal_kind=facts.terminal.kind,
            harness_terminal_reason=facts.terminal.reason,
            exit_code=exit_code,
            spine_cursor=cursor,
            spine_basis="read",
            considered_events=len(scoped),
        )

    if kinds:
        # Newest wins, which is what makes a `task.result_posted` REVISION still `posted`
        # rather than a second, competing fact (WORKER.md v2.3: append-only, newest wins).
        newest = max(dispositions, key=lambda e: _seq_of(e) or 0)
        event_type = str(newest.get("event_type"))
        return ExitRecord(
            classification=_CLASSIFICATION_OF[event_type],
            reason=f"spine: {event_type}",
            task_disposition=event_type,
            terminal_event_seq=_seq_of(newest),
            harness_terminal_kind=facts.terminal.kind,
            harness_terminal_reason=facts.terminal.reason,
            exit_code=exit_code,
            spine_cursor=cursor,
            spine_basis="read",
            # A task disposition wins the primary classification even when the harness
            # then dies during shutdown; the harness failure stays on the record as the
            # secondary fact it is.
            harness_failed_after_disposition=harness_errored,
            considered_events=len(scoped),
        )

    # No task disposition. The harness is now the only authority left, and it may only
    # speak about its own process.
    if facts.terminal.kind == "budget":
        return _no_disposition(
            "budget", f"harness: {facts.terminal.reason}", facts, exit_code, cursor, len(scoped)
        )
    if facts.terminal.kind == "error":
        return _no_disposition(
            "failed", f"harness: {facts.terminal.reason}", facts, exit_code, cursor, len(scoped)
        )
    if facts.terminal.kind == "completed":
        return _no_disposition(
            "unclassified",
            "missing_worker_terminal_event",
            facts,
            exit_code,
            cursor,
            len(scoped),
        )
    return _no_disposition(
        "unclassified",
        f"insufficient_harness_evidence: {facts.terminal.kind}",
        facts,
        exit_code,
        cursor,
        len(scoped),
    )


_CLASSIFICATION_OF: dict[str, Classification] = {
    "task.result_posted": "posted",
    "task.blocked": "blocked",
    "task.failed": "failed",
}


def _no_disposition(
    classification: Classification,
    reason: str,
    facts: TurnFacts,
    exit_code: int | None,
    cursor: int | None,
    considered: int,
) -> ExitRecord:
    return ExitRecord(
        classification=classification,
        reason=reason,
        task_disposition=None,
        terminal_event_seq=None,
        harness_terminal_kind=facts.terminal.kind,
        harness_terminal_reason=facts.terminal.reason,
        exit_code=exit_code,
        spine_cursor=cursor,
        spine_basis="read",
        considered_events=considered,
    )


def _actor_role(ev: Mapping[str, Any]) -> str | None:
    actor = ev.get("actor")
    if isinstance(actor, Mapping):
        role = actor.get("role")
        return role if isinstance(role, str) else None
    return None


def _actor_id(ev: Mapping[str, Any]) -> str | None:
    actor = ev.get("actor")
    if isinstance(actor, Mapping):
        ident = actor.get("id")
        return ident if isinstance(ident, str) else None
    return None


def _seq_of(ev: Mapping[str, Any]) -> int | None:
    seq = ev.get("seq")
    return seq if isinstance(seq, int) else None


def _seq_after(ev: Mapping[str, Any], cursor: int | None) -> bool:
    """Strictly after the saved cursor.

    An event with no sequence cannot be placed relative to the cursor at all, so it is
    excluded rather than assumed recent — reading a PRIOR turn's `task.blocked` as this
    turn's exit is a named risk of this order, and an unordered event is exactly the
    shape that risk takes.
    """
    seq = _seq_of(ev)
    if seq is None:
        return False
    if cursor is None:
        # Unreachable from `classify`, which refuses a cursor-less classification outright.
        # Kept as a closed door rather than an open one: a future caller that forgets the
        # guard gets an empty scope, not the entire history.
        return False
    return seq > cursor


def summary_lines(
    *,
    record: ExitRecord,
    facts: TurnFacts,
    task: str,
    worker: str,
    route: str,
    turn_id: str,
    turn_kind: str,
) -> list[str]:
    """The intelligible terminal summary a tmux window keeps after the process exits.

    This exists because a raw JSON stream on a dead pane is retained evidence, not an
    operator interface: the operator who walks past the window ten minutes later must be
    able to read what happened without opening a file. Every line here is derived from
    the record and the facts — nothing is re-read, and nothing is inferred.
    """
    glyph = {
        "posted": "[=]",
        "blocked": "[!]",
        "failed": "[x]",
        "budget": "[~]",
        "unclassified": "[?]",
    }[record.classification]
    lines = [
        "",
        "  ------------------------------------------------------------------",
        f"  {glyph} turn {turn_id} ({turn_kind}) ended: {record.classification.upper()}",
        f"      {record.reason}",
        f"      task {task}   worker {worker}   route {route}",
        f"      harness: {record.harness_terminal_kind} ({record.harness_terminal_reason})"
        f"   exit {record.exit_code if record.exit_code is not None else '?'}",
    ]
    if record.task_disposition:
        lines.append(
            f"      spine:   {record.task_disposition} at seq {record.terminal_event_seq}"
            f"   (cursor {record.spine_cursor})"
        )
    else:
        lines.append(
            f"      spine:   no task disposition after seq {record.spine_cursor}"
            f"   ({record.spine_basis}, {record.considered_events} scoped event(s))"
        )
    if record.harness_failed_after_disposition:
        lines.append(
            "      note:    the harness also failed during shutdown; the task"
            " disposition above is the primary fact"
        )
    if facts.session_id:
        lines.append(f"      session: {facts.session_id}   (resume with hive-answer)")
    else:
        lines.append(
            "      session: NOT RECORDED — this turn cannot be resumed;"
            " see the retained stream"
        )
    lines.append(
        f"      stream:  {facts.records} record(s), {facts.malformed} malformed"
        f"{', TRUNCATED' if facts.truncated else ''}   {facts.digest or '<no digest>'}"
    )
    if record.needs_attention:
        lines.append("      -> this exit has no task event behind it; the operator is notified")
    lines.append("  ------------------------------------------------------------------")
    return lines


def scoped_query(*, run: str, task: str, worker: str, cursor: int | None) -> dict[str, Any]:
    """The scope the classifier will apply, as data — for the record and for `--check`.

    Printed rather than described so that an operator reading an `unclassified` record
    can see exactly which slice of the spine was consulted, without reading this file.
    """
    return {"run": run, "task": task, "worker": worker, "after_seq": cursor}


def normalize_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the fields the classifier reads, so a record of them stays small.

    Payloads are deliberately dropped: nothing in the classification reads one, and an
    evidence file that carried them would put report content into a provenance record.
    """
    out = []
    for ev in rows:
        out.append(
            {
                "seq": _seq_of(ev),
                "run_id": ev.get("run_id"),
                "task_id": ev.get("task_id"),
                "event_type": ev.get("event_type"),
                "actor": {"role": _actor_role(ev), "id": _actor_id(ev)},
            }
        )
    return out
