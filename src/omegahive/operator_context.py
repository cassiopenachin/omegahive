"""Bounded, read-only evidence for one task: what the operator needs before deciding.

The provider reads the task's own spine events through the port and, for report
content, the workspace hub (a bare repository, mounted read-only). Run and task come
from the request; the hub path is a deployment fact from the environment. It accepts
no ref, path, pane or process from a client: every ref it reads is one the task's own
events already carry.

Evidence the page cannot see yet is reported as unavailable with its reason, never as
an empty value: the worker's pane needs the host operation socket, and independent
reviews are files on the host, not spine events.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .api.service import UnknownRun, UnknownTask
from .report.reader import PortFactory, read_view

CONTEXT_SCHEMA_VERSION = "operator-context.v2"
MAX_ARTIFACTS = 5
MAX_ARTIFACT_BYTES = 32 * 1024
MAX_HISTORY_ITEMS = 100
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}

WORKER_OUTPUT_UNAVAILABLE = (
    "the worker's pane is read through the host operation socket, which is not "
    "installed yet (salvage step S3)"
)
INDEPENDENT_REVIEW_UNAVAILABLE = (
    "independent reviews are recorded as files in the run's reviews directory on the "
    "host, not as spine events; no independent review event exists for this task"
)


def configured_workspace_hub() -> Path | None:
    """The workspace hub mount, or None when this deployment has not configured one."""
    value = os.environ.get("OMEGAHIVE_WORKSPACE_HUB", "").strip()
    return Path(value) if value else None


def _seq(event: Any) -> int:
    value = getattr(event, "seq", None)
    return value if isinstance(value, int) else -1


def _payload(event: Any | None) -> Mapping[str, Any]:
    return event.payload if event is not None and isinstance(event.payload, dict) else {}


def _last(events: Iterable[Any], *event_types: str) -> Any | None:
    selected = [event for event in events if event.event_type in event_types]
    return max(selected, key=_seq) if selected else None


def _unavailable(unavailable_reason: str, **fields: object) -> dict[str, object]:
    return {"available": False, **fields, "unavailable_reason": unavailable_reason}


def _available(**fields: object) -> dict[str, object]:
    return {"available": True, **fields, "unavailable_reason": None}


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class OperatorContextProvider:
    """Build one snapshot of one task from its events and the hub."""

    def __init__(
        self,
        port_factory: PortFactory,
        now_factory: Callable[[], datetime],
        workspace_hub: Path | None,
    ) -> None:
        self._port_factory = port_factory
        self._now_factory = now_factory
        self._hub = workspace_hub

    def __call__(self, run_id: str, task_id: str) -> dict[str, object]:
        now = self._now_factory()
        view = read_view(self._port_factory, run_id, None, None)
        board = view.board
        if board is None or not board.tasks:
            raise UnknownRun(run_id)
        task = board.tasks.get(task_id)
        if task is None:
            raise UnknownTask(task_id)

        events = sorted((e for e in view.events if e.task_id == task_id), key=_seq)
        question_event = _last(events, "question.asked")
        question_report = _last(
            (
                e
                for e in events
                if e.event_type == "task.reported" and _payload(e).get("kind") == "question"
            ),
            "task.reported",
        )
        blocked_event = _last(events, "task.blocked")
        result_event = _last(events, "task.result_posted")

        question_ref = self._question_ref(events, question_event, question_report)
        question_text = _payload(question_event).get("text")
        if not isinstance(question_text, str) or not question_text:
            question_text = None

        result_refs = self._result_refs(result_event)
        review_events = [e for e in events if e.event_type in {"review.passed", "review.failed"}]
        refs = self._artifact_refs(result_refs, review_events)
        artifacts = [self._artifact(ref) for ref in refs[:MAX_ARTIFACTS]]
        if question_text is None and question_ref is not None:
            question_artifact = self._artifact(question_ref)
            if question_artifact["available"] is True and isinstance(
                question_artifact.get("content"), str
            ):
                question_text = question_artifact["content"]

        question_seq = max(
            (_seq(e) for e in (question_event, question_report) if e is not None),
            default=None,
        )
        question = (
            _available(text=question_text, ref=question_ref, event_seq=question_seq)
            if question_text is not None
            else _unavailable(
                "no recorded question text is available",
                text=None,
                ref=question_ref,
                event_seq=question_seq,
            )
        )

        if task.status == "blocked":
            blocker = _available(
                reason=task.blocker_reason,
                needs=task.blocker_needs,
                ref=_payload(blocked_event).get("ref_report"),
                event_seq=_seq(blocked_event) if blocked_event is not None else None,
            )
        else:
            blocker = _unavailable(
                "task is not currently blocked", reason=None, needs=None, ref=None, event_seq=None
            )

        result = (
            _available(
                artifact_refs=result_refs,
                cost=_payload(result_event).get("cost"),
                event_seq=_seq(result_event),
            )
            if result_event is not None
            else _unavailable(
                "no result has been posted", artifact_refs=[], cost=None, event_seq=None
            )
        )

        independent = [e for e in review_events if self._review_kind(e) == "independent"]
        unclassified = [e for e in review_events if self._review_kind(e) is None]
        current_ref = result_refs[0]["ref"] if result_refs else None

        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": task_id,
            "observation_basis": {
                "event_cursor": view.cursor,
                "generation": view.generation,
                "observed_at": _iso(now),
            },
            "task_evidence": {
                "status": task.status,
                "title": task.title,
                "owner": task.owner,
                "question": question,
                "blocker": blocker,
                "result": result,
                "independent_review": (
                    _available(rounds=self._rounds(independent))
                    if independent
                    else _unavailable(INDEPENDENT_REVIEW_UNAVAILABLE, rounds=[])
                ),
                "operator_acceptance": self._operator_acceptance(events, current_ref),
                "unclassified_reviews": self._rounds(unclassified),
                "cancellation": self._cancellation(events, task.status),
                "effort": self._effort(events),
            },
            "operation_history": self._operation_history(events),
            "pinned_artifacts": artifacts,
            "worker_output": _unavailable(WORKER_OUTPUT_UNAVAILABLE, lines=[]),
        }

    @staticmethod
    def _question_ref(
        events: list[Any],
        question_event: Any | None,
        question_report: Any | None,
    ) -> str | None:
        reported_ref = _payload(question_report).get("ref")
        if isinstance(reported_ref, str):
            return reported_ref
        if question_event is None:
            ref = _payload(_last(events, "task.blocked")).get("ref_report")
            return ref if isinstance(ref, str) else None
        question_seq = _seq(question_event)
        next_unblocked_seq = min(
            (
                _seq(e)
                for e in events
                if e.event_type == "task.unblocked" and _seq(e) > question_seq
            ),
            default=None,
        )
        matching_blocks = [
            e
            for e in events
            if e.event_type == "task.blocked"
            and _seq(e) > question_seq
            and (next_unblocked_seq is None or _seq(e) < next_unblocked_seq)
        ]
        blocked = max(matching_blocks, key=_seq) if matching_blocks else None
        ref = _payload(blocked).get("ref_report")
        return ref if isinstance(ref, str) else None

    @staticmethod
    def _result_refs(result_event: Any | None) -> list[dict[str, object]]:
        refs = _payload(result_event).get("artifact_refs")
        if not isinstance(refs, list):
            return []
        return [
            {"ref": item.get("ref"), "quality": item.get("quality")}
            for item in refs
            if isinstance(item, dict) and isinstance(item.get("ref"), str)
        ]

    @staticmethod
    def _artifact_refs(result_refs: list[dict[str, object]], review_events: list[Any]) -> list[str]:
        candidates: list[object] = [item.get("ref") for item in result_refs]
        candidates.extend(_payload(e).get("ref_result") for e in review_events)
        unique: list[str] = []
        for candidate in candidates:
            if isinstance(candidate, str) and candidate not in unique:
                unique.append(candidate)
        return unique

    @staticmethod
    def _review_kind(event: Any) -> str | None:
        declared = _payload(event).get("review_kind")
        return declared if declared in {"independent", "operator_acceptance"} else None

    @staticmethod
    def _rounds(events: list[Any]) -> list[dict[str, object]]:
        return [
            {
                "event_seq": _seq(e),
                "verdict": "passed" if e.event_type == "review.passed" else "failed",
                "actor_id": e.actor.id,
                "ref_result": _payload(e).get("ref_result"),
                "reason": _payload(e).get("reason"),
            }
            for e in events
        ]

    @staticmethod
    def _operator_acceptance(events: list[Any], current_ref: object) -> dict[str, object]:
        """The latest `review.passed` that `hive-close` records, with the close's reason."""
        accepted = _last(
            (
                e
                for e in events
                if e.event_type == "review.passed"
                and OperatorContextProvider._review_kind(e) == "operator_acceptance"
            ),
            "review.passed",
        )
        if accepted is None:
            return _unavailable(
                "the operator has not accepted a result for this task",
                actor_id=None,
                ref_result=None,
                current=False,
                reason=None,
                event_seq=None,
            )
        closed = _last(
            (
                e
                for e in events
                if e.event_type == "task.status_override"
                and _payload(e).get("status") == "done"
                and _seq(e) > _seq(accepted)
            ),
            "task.status_override",
        )
        ref_result = _payload(accepted).get("ref_result")
        return _available(
            actor_id=accepted.actor.id,
            ref_result=ref_result,
            current=ref_result == current_ref,
            reason=_payload(closed).get("reason"),
            event_seq=_seq(accepted),
        )

    @staticmethod
    def _cancellation(events: list[Any], status: str) -> dict[str, object]:
        override = _last(
            (
                e
                for e in events
                if e.event_type == "task.status_override"
                and _payload(e).get("status") == "cancelled"
            ),
            "task.status_override",
        )
        if status != "cancelled" or override is None:
            return _unavailable(
                "task is not cancelled",
                reason=None,
                actor_id=None,
                decision_ref=None,
                event_seq=None,
            )
        payload = _payload(override)
        return _available(
            reason=payload.get("reason"),
            actor_id=override.actor.id,
            decision_ref=payload.get("decision_ref"),
            event_seq=_seq(override),
        )

    @staticmethod
    def _operation_history(events: list[Any]) -> dict[str, object]:
        """Who did what to this task, newest first — the spine, not a local store."""
        items = []
        for e in sorted(events, key=_seq, reverse=True)[:MAX_HISTORY_ITEMS]:
            payload = _payload(e)
            items.append(
                {
                    "event_seq": _seq(e),
                    "event_type": e.event_type,
                    "actor_role": e.actor.role,
                    "actor_id": e.actor.id,
                    "wall_ts": _iso(e.wall_ts) if isinstance(e.wall_ts, datetime) else None,
                    "status": payload.get("status"),
                    "reason": payload.get("reason"),
                    "decision_ref": payload.get("decision_ref"),
                    "executed_by": payload.get("executed_by"),
                }
            )
        return {"items": items, "truncated": len(events) > MAX_HISTORY_ITEMS}

    @staticmethod
    def _effort(events: list[Any]) -> dict[str, object]:
        finished = _last(
            (
                e
                for e in events
                if e.event_type == "execution.finished" and _payload(e).get("purpose") == "work"
            ),
            "execution.finished",
        )
        usage = _payload(finished).get("usage") if finished is not None else None
        empty = {"tokens": None, "cost_usd": None, "elapsed_seconds": None}
        if not isinstance(usage, dict) or usage.get("status") != "reported":
            return _unavailable("no durable consumption evidence was recorded", **empty)
        keys = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
        counts = {key: usage.get(key) for key in keys}
        if not all(isinstance(value, int) for value in counts.values()):
            return _unavailable("durable consumption evidence is incomplete", **empty)
        tokens = sum(cast(int, counts[key]) for key in keys)
        cost = OperatorContextProvider._cost(counts, _payload(finished).get("price_basis"))
        elapsed = OperatorContextProvider._elapsed(events, finished)
        return _available(tokens=tokens, cost_usd=cost, elapsed_seconds=elapsed)

    @staticmethod
    def _cost(counts: Mapping[str, object], price_basis: object) -> float | None:
        if not isinstance(price_basis, dict):
            return None
        total = 0.0
        for key in counts:
            count = counts[key]
            rate = price_basis.get("per_mtok_" + key.removesuffix("_tokens"))
            if not isinstance(count, int) or (count and not isinstance(rate, (int, float))):
                return None
            total += count * float(rate or 0.0) / 1_000_000
        return total

    @staticmethod
    def _elapsed(events: list[Any], finished: Any) -> float | None:
        execution_id = _payload(finished).get("execution_id")
        started = _last(
            (
                e
                for e in events
                if e.event_type == "execution.started"
                and _payload(e).get("execution_id") == execution_id
            ),
            "execution.started",
        )
        start_text = _payload(started).get("started_at")
        finish_text = _payload(finished).get("finished_at")
        if not isinstance(start_text, str) or not isinstance(finish_text, str):
            return None
        try:
            start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            finish = datetime.fromisoformat(finish_text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0.0, (finish - start).total_seconds())

    def _artifact(self, ref: str) -> dict[str, object]:
        """Read one `path@sha` ref from the hub, bounded. The ref comes from an event."""
        base = {
            "ref": ref,
            "media_type": "text/markdown" if ref.split("@", 1)[0].endswith(".md") else "text/plain",
            "content": None,
            "truncated": False,
        }
        if self._hub is None:
            return _unavailable(
                "the workspace hub is not configured (OMEGAHIVE_WORKSPACE_HUB)", **base
            )
        relative, sep, commit = ref.rpartition("@")
        if not sep:
            return _unavailable("artifact ref is not path@sha", **base)
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not _FULL_SHA.fullmatch(commit):
            return _unavailable("artifact ref is not a normalized full-SHA ref", **base)
        hub = str(self._hub)
        try:
            size_result = subprocess.run(  # noqa: S603 - fixed executable, event-sourced ref
                ["/usr/bin/git", "-C", hub, "cat-file", "-s", f"{commit}:{relative}"],
                env=_GIT_ENV,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return _unavailable("artifact metadata is unavailable", **base)
        if size_result.returncode != 0:
            return _unavailable("artifact cannot be read at its pinned ref", **base)
        try:
            size = int(size_result.stdout.strip())
        except ValueError:
            return _unavailable("artifact size is unavailable", **base)
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed executable, event-sourced ref
                ["/usr/bin/git", "-C", hub, "cat-file", "blob", f"{commit}:{relative}"],
                env=_GIT_ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert process.stdout is not None
            raw = process.stdout.read(MAX_ARTIFACT_BYTES)
            if size > MAX_ARTIFACT_BYTES:
                process.terminate()
            returncode = process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            return _unavailable("artifact cannot be read at its pinned ref", **base)
        if returncode != 0 and size <= MAX_ARTIFACT_BYTES:
            return _unavailable("artifact cannot be read at its pinned ref", **base)
        truncated = size > MAX_ARTIFACT_BYTES
        content = _decode(raw, truncated)
        if content is None:
            return _unavailable("artifact is not UTF-8 text", **base)
        return _available(**{**base, "content": content, "truncated": truncated})


def _decode(raw: bytes, truncated: bool) -> str | None:
    """Strict UTF-8, except that a cut at the byte bound may split one character."""
    for cut in range(4 if truncated else 1):
        try:
            return raw[: len(raw) - cut].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
    return None
