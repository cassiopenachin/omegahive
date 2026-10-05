"""The task page's provider reads one task's own events, and the hub only at their refs."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from omegahive.api.models import OperatorContextResponse
from omegahive.api.service import UnknownTask
from omegahive.board import fold
from omegahive.events.envelope import Actor, Event
from omegahive.operator_context import MAX_ARTIFACT_BYTES, OperatorContextProvider
from omegahive.port import PortView
from omegahive.ui.demo import DEMO_RUN_ID, DemoPort

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "operator_context_v2.json"
SHA_A = "a" * 40
SHA_B = "b" * 40
RESULT = f"r/result.md@{SHA_A}"
ELSEWHERE = f"r/elsewhere.md@{SHA_B}"
WORKER = Actor(role="worker", id="w1")
OPERATOR = Actor(role="human", id="cassio")
_COORDINATOR = Actor(role="coordinator", id="coordinator")


def _event(seq: int, event_type: str, payload: dict, task_id: str = "t", actor: Actor = WORKER):
    return Event(
        event_id=uuid5(NAMESPACE_URL, f"operator-context-test:{seq}"),
        run_id="r",
        logical_ts=seq,
        wall_ts=NOW,
        actor=actor,
        event_type=event_type,
        task_id=task_id,
        payload=payload,
        seq=seq,
    )


class _ListPort:
    def __init__(self, events: list[Event]) -> None:
        self._events = events

    def read(self, cursor: int | None = None) -> PortView:
        return PortView(
            cursor=len(self._events),
            generation=1,
            events=self._events,
            board=fold(self._events),
            changed=True,
        )


def _provider(events: list[Event], hub: Path | None = None) -> OperatorContextProvider:
    return OperatorContextProvider(lambda run_id, generation: _ListPort(events), lambda: NOW, hub)


def _closed_task() -> list[Event]:
    """A task that asked, was answered, posted, was reviewed three ways and was closed."""
    coordinator = _COORDINATOR
    return [
        _event(1, "task.created", {"title": "Port the page"}, actor=coordinator),
        _event(2, "task.created", {"title": "Another task"}, task_id="other", actor=coordinator),
        _event(3, "task.assigned", {"worker": "w1"}, actor=coordinator),
        _event(4, "task.accepted", {}),
        _event(5, "question.asked", {"text": "Which base branch?"}),
        _event(6, "task.blocked", {"reason": "needs answer", "ref_report": f"q/1.md@{SHA_A}"}),
        _event(7, "task.unblocked", {}, actor=OPERATOR),
        _event(8, "task.result_posted", {"artifact_refs": [{"ref": RESULT, "quality": "ok"}]}),
        _event(9, "task.result_posted", {"artifact_refs": [{"ref": ELSEWHERE, "quality": "ok"}]},
               task_id="other"),
        _event(10, "review.failed", {"ref_result": RESULT, "reason": "untagged"},
               actor=Actor(role="instrument", id="legacy")),
        _event(11, "review.passed", {"ref_result": RESULT, "review_kind": "independent"},
               actor=Actor(role="instrument", id="sol")),
        _event(12, "review.passed", {"ref_result": RESULT, "review_kind": "operator_acceptance"},
               actor=Actor(role="instrument", id="cassio")),
        _event(13, "task.status_override", {"status": "done", "reason": "clean"}, actor=OPERATOR),
    ]  # fmt: skip


def test_demo_blocked_task_validates_and_says_why_each_unavailable_card_is_empty():
    provider = OperatorContextProvider(
        lambda run_id, generation: DemoPort(run_id, generation), lambda: NOW, None
    )

    context = OperatorContextResponse.model_validate(provider(DEMO_RUN_ID, "T2"))

    evidence = context.task_evidence
    assert evidence["status"] == "blocked"
    assert evidence["blocker"]["available"] is True
    assert evidence["blocker"]["reason"] == "the fork image is not available"
    for card in (context.worker_output, evidence["independent_review"]):
        assert card["available"] is False
        assert card["unavailable_reason"]


def test_golden_context_is_the_clients_stub():
    """The fixture is what a client (the page, later Slack) can build against. When the
    shape changes on purpose, regenerate it from `_closed_task()` and bump the version."""
    golden = json.loads(FIXTURE.read_text())

    assert OperatorContextResponse.model_validate(golden).schema_version == "operator-context.v2"
    assert _provider(_closed_task())("r", "t") == golden


def test_an_older_reported_question_ref_does_not_attach_to_a_newer_question():
    events = _closed_task()[:4] + [
        _event(5, "task.reported", {"kind": "question", "ref": f"q/old.md@{SHA_A}"}),
        _event(6, "question.asked", {"text": "New question?"}),
        _event(7, "task.blocked", {"reason": "needs answer", "ref_report": f"q/new.md@{SHA_B}"}),
    ]

    question = _provider(events)("r", "t")["task_evidence"]["question"]

    assert (question["text"], question["ref"]) == ("New question?", f"q/new.md@{SHA_B}")


def test_unknown_task_is_refused_not_partially_shown():
    with pytest.raises(UnknownTask):
        _provider(_closed_task())("r", "nope")


def test_acceptance_independent_and_unclassified_reviews_never_share_a_card():
    evidence = _provider(_closed_task())("r", "t")["task_evidence"]

    acceptance = evidence["operator_acceptance"]
    assert acceptance["available"] is True
    assert (acceptance["actor_id"], acceptance["reason"], acceptance["current"]) == (
        "cassio",
        "clean",
        True,
    )
    assert [r["actor_id"] for r in evidence["independent_review"]["rounds"]] == ["sol"]
    assert [r["actor_id"] for r in evidence["unclassified_reviews"]] == ["legacy"]


def test_an_untagged_review_alone_leaves_both_review_cards_unavailable():
    events = _closed_task()[:8] + [
        _event(9, "review.passed", {"ref_result": RESULT}),
    ]

    evidence = _provider(events)("r", "t")["task_evidence"]

    assert evidence["independent_review"]["available"] is False
    assert evidence["operator_acceptance"]["available"] is False
    assert len(evidence["unclassified_reviews"]) == 1


def test_only_refs_from_this_tasks_events_are_read():
    context = _provider(_closed_task())("r", "t")

    refs = [artifact["ref"] for artifact in context["pinned_artifacts"]]
    assert refs == [RESULT]
    assert all("elsewhere" not in ref for ref in refs)


def test_history_is_the_tasks_spine_newest_first_with_actors():
    history = _provider(_closed_task())("r", "t")["operation_history"]

    seqs = [item["event_seq"] for item in history["items"]]
    assert seqs == sorted(seqs, reverse=True)
    assert 9 not in seqs  # another task's event
    newest = history["items"][0]
    assert (newest["event_type"], newest["actor_id"], newest["status"]) == (
        "task.status_override",
        "cassio",
        "done",
    )


def test_cancelled_task_shows_reason_deciding_actor_and_decision_ref():
    events = _closed_task()[:4] + [
        _event(
            5,
            "task.status_override",
            {"status": "cancelled", "reason": "worker died", "decision_ref": "slack:123"},
            actor=OPERATOR,
        ),
    ]

    context = _provider(events)("r", "t")["task_evidence"]

    assert context["status"] == "cancelled"
    assert context["cancellation"] == {
        "available": True,
        "reason": "worker died",
        "actor_id": "cassio",
        "decision_ref": "slack:123",
        "event_seq": 5,
        "unavailable_reason": None,
    }


def test_effort_cost_needs_a_price_basis():
    usage = {
        "status": "reported",
        "input_tokens": 1_000_000,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
    }
    finished = {"purpose": "work", "execution_id": "e", "finished_at": "2026-10-05T12:10:00Z"}
    events = _closed_task()[:4] + [
        _event(5, "execution.started", {"execution_id": "e", "started_at": "2026-10-05T12:00:00Z"}),
        _event(6, "execution.finished", {**finished, "usage": usage}),
    ]
    price = {"per_mtok_input": 3.0}
    priced = events[:-1] + [
        _event(6, "execution.finished", {**finished, "usage": usage, "price_basis": price}),
    ]

    unpriced_effort = _provider(events)("r", "t")["task_evidence"]["effort"]
    priced_effort = _provider(priced)("r", "t")["task_evidence"]["effort"]

    assert unpriced_effort["tokens"] == 1_000_000 and unpriced_effort["cost_usd"] is None
    assert unpriced_effort["elapsed_seconds"] == 600.0
    assert priced_effort["cost_usd"] == 3.0


def test_without_a_hub_report_content_is_unavailable_with_the_reason():
    artifact = _provider(_closed_task(), hub=None)("r", "t")["pinned_artifacts"][0]

    assert artifact["available"] is False
    assert "OMEGAHIVE_WORKSPACE_HUB" in artifact["unavailable_reason"]


@pytest.fixture
def hub(tmp_path: Path) -> tuple[Path, str]:
    """A real repository with a short and an over-bound report, committed once."""
    if not Path("/usr/bin/git").exists():
        pytest.skip("git is not installed at /usr/bin/git")
    repo = tmp_path / "hub"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "r").mkdir()
    (repo / "r" / "result.md").write_text("# Result\n\n<b>done</b>\n")
    (repo / "r" / "big.md").write_text("é" * MAX_ARTIFACT_BYTES)  # two bytes each
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "reports"], check=True)
    sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def test_report_content_is_read_from_the_hub_at_the_events_ref(hub):
    repo, sha = hub
    provider = _provider([], hub=repo)

    content = provider._artifact(f"r/result.md@{sha}")
    big = provider._artifact(f"r/big.md@{sha}")

    assert content["available"] is True and content["content"].startswith("# Result")
    assert big["available"] is True and big["truncated"] is True
    assert len(big["content"].encode()) <= MAX_ARTIFACT_BYTES


@pytest.mark.parametrize(
    "ref",
    ["r/result.md", "r/result.md@abc1234", "../outside.md@" + SHA_A, "/etc/passwd@" + SHA_A],
)
def test_malformed_or_escaping_refs_are_refused_before_git_runs(hub, ref):
    repo, _ = hub

    artifact = _provider([], hub=repo)._artifact(ref)

    assert artifact["available"] is False
    assert artifact["unavailable_reason"]
