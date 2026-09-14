"""The close-time usage harvest: which surface belongs to which execution.

The extraction rules live in `harness.usage` and are tested there. What is tested here
is the question that comes first and is easier to get wrong: given a finished task root,
WHICH file recorded the work and WHICH recorded a review. Both purposes bill differently,
often to different accounts, and a file attributed to the wrong one is a number that
looks right and is not.

Three properties carry the module:

* Evidence is COPIED, not referenced. The reviewer's transcript lives inside a sandbox
  VM and `sbx prune` deletes every stopped one. A harvest that only recorded a path
  would hold a citation to a file the next cleanup destroys.

* An unattributable file is recorded and NOT counted. The review sidecars name the
  transcripts each round wrote; a transcript no sidecar names, on a route whose worker
  does not write transcripts, is a review nobody can place. It goes into the evidence
  directory and stays out of every total, because a total containing an unknown is worse
  than a total that says it is missing.

* A harness with no usage surface says so with a named reason. `antigravity` reports
  nothing, and `unavailable` carrying that reason is the truthful record — a zero would
  read as a free execution.
"""

from __future__ import annotations

import json
from pathlib import Path

from omegahive.harness.harvest import (
    HarvestRequest,
    finished_payload,
    harvest,
)

IDENTITY = {
    "route": "or-glm-5.3", "model_vendor": "z-ai", "provider": "openrouter",
    "model": "z-ai/glm-5.3", "harness": "opencode", "billing_market": "api",
    "credential_pool": "openrouter-primary", "adapter": "generic",
}
REVIEWER_IDENTITY = {**IDENTITY, "route": "claude-opus", "model_vendor": "anthropic",
                     "provider": "anthropic", "model": "claude-opus-5",
                     "harness": "claude-code", "billing_market": "subscription",
                     "credential_pool": "anthropic-subscription-primary",
                     "adapter": "claude-code"}


# --- builders ------------------------------------------------------------------------

def transcript(path: Path, *, msg: str = "m1", out: int = 100, cache_read: int = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "assistant", "isSidechain": False,
        "message": {"id": msg, "model": "claude-opus-5", "usage": {
            "input_tokens": 1, "output_tokens": out,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": 2}},
    }) + "\n")
    return path


def opencode_export(path: Path, *, out: int = 500) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": "msg_1", "role": "assistant", "modelID": "z-ai/glm-5.3", "cost": 0.5,
        "tokens": {"input": 10, "output": out, "reasoning": 1,
                   "cache": {"read": 900, "write": 0}},
    }) + "\n")
    return path


def task_root(tmp_path: Path, *, reviews: dict[str, list[Path]] | None = None) -> Path:
    """A task root with a `run/reviews/` holding the named rounds and their sidecars."""
    root = tmp_path / "work" / "sess-t-0914"
    (root / "run" / "reviews" / "meta").mkdir(parents=True)
    for name, transcripts in (reviews or {}).items():
        (root / "run" / "reviews" / name).write_text("VERDICT: PASS\n")
        (root / "run" / "reviews" / "meta" / f"{name}.transcripts").write_text(
            "".join(f"{p}\n" for p in transcripts))
    return root


def run(root: Path, **over) -> object:
    req = HarvestRequest(
        task="t", task_root=root, worker_id="sess-t-0914",
        work_identity=over.pop("work_identity", IDENTITY),
        reviewer_identity=over.pop("reviewer_identity", REVIEWER_IDENTITY),
        execution_id=over.pop("execution_id", "t-a1-abc"),
        attempt=1, order_ref="orders/t.md@deadbeef",
        host_sources=over.pop("host_sources", []),
        sandbox_sources=over.pop("sandbox_sources", []),
        **over,
    )
    return harvest(req)


# --- attribution -----------------------------------------------------------------------

def test_the_workers_own_surface_becomes_the_work_execution(tmp_path):
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[("opencode-messages", export)])
    assert res.work.usage.status == "reported"
    assert res.work.usage.output_tokens == 500


def test_a_transcript_a_review_sidecar_names_is_billed_to_the_review(tmp_path):
    reviewed = transcript(tmp_path / "vm" / "claude" / "r1.jsonl", out=70)
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[("opencode-messages", export),
                                     ("claude-code-transcript", reviewed)])
    assert res.review.usage.output_tokens == 70
    assert res.work.usage.output_tokens == 500, "the review must not leak into the work total"


def test_two_rounds_are_summed_into_one_review_execution(tmp_path):
    r1 = transcript(tmp_path / "vm" / "c" / "r1.jsonl", msg="a", out=70)
    r2 = transcript(tmp_path / "vm" / "c" / "r2.jsonl", msg="b", out=30)
    root = task_root(tmp_path, reviews={"review-1.txt": [r1], "review-2.txt": [r2]})
    res = run(root, sandbox_sources=[("claude-code-transcript", r1),
                                     ("claude-code-transcript", r2)],
              work_identity=None)
    assert res.review.usage.output_tokens == 100
    assert res.review.usage.evidence_records == 2


def test_a_round_naming_two_transcripts_refuses_rather_than_guessing(tmp_path):
    """Claude Code writes one session file per `-p` invocation, so two is anomalous — most
    likely the worker's own session landed in the window. Summing it would bill the whole
    worker run to the review."""
    a = transcript(tmp_path / "vm" / "c" / "a.jsonl", msg="a")
    b = transcript(tmp_path / "vm" / "c" / "b.jsonl", msg="b")
    root = task_root(tmp_path, reviews={"review-1.txt": [a, b]})
    res = run(root, sandbox_sources=[("claude-code-transcript", a),
                                     ("claude-code-transcript", b)])
    assert res.review.usage.status == "unavailable"
    assert "review-1.txt" in (res.review.usage.reason or "")
    assert res.review.usage.output_tokens is None


def test_a_round_with_no_sidecar_at_all_refuses_and_names_the_round(tmp_path):
    """Every round predating the sidecar looks like this, and so does a round whose
    sidecar write failed. Silence would report those reviews as free."""
    root = task_root(tmp_path, reviews={"review-1.txt": []})
    res = run(root)
    assert res.review.usage.status == "unavailable"
    assert "review-1.txt" in (res.review.usage.reason or "")


def test_a_task_with_no_reviews_records_that_rather_than_an_empty_total(tmp_path):
    root = task_root(tmp_path)
    res = run(root)
    assert res.review.usage.status == "unavailable"
    assert "no review" in (res.review.usage.reason or "").lower()


def test_a_harness_with_no_usage_surface_says_which_one(tmp_path):
    root = task_root(tmp_path)
    res = run(root, work_identity={**IDENTITY, "harness": "antigravity"})
    assert res.work.usage.status == "unavailable"
    assert "antigravity" in (res.work.usage.reason or "")
    assert res.work.usage.output_tokens is None


# --- durability --------------------------------------------------------------------

def test_every_source_is_copied_into_the_task_root(tmp_path):
    """`sbx prune` removes every stopped sandbox. A recorded path is not a record."""
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[("opencode-messages", export),
                                     ("claude-code-transcript", reviewed)])
    kept = sorted(p.name for p in (root / "run" / "usage" / "raw").rglob("*.jsonl"))
    assert len(kept) == 2
    for p in (root / "run" / "usage" / "raw").rglob("*.jsonl"):
        assert p.stat().st_size > 0
    assert res.manifest_path.exists()


def test_the_evidence_ref_points_at_a_file_that_re_derives_the_total(tmp_path):
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[("opencode-messages", export)])
    ref = Path(res.work.usage.evidence_ref)
    assert ref.exists()
    rows = json.loads(ref.read_text())["rows"]
    assert sum(r["output_tokens"] for r in rows) == res.work.usage.output_tokens


def test_no_row_carries_message_text(tmp_path):
    """The evidence must re-derive a total and reconstruct nothing."""
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    res = run(root, sandbox_sources=[("claude-code-transcript", reviewed)])
    rows = json.loads(Path(res.review.usage.evidence_ref).read_text())["rows"]
    allowed = {"message_id", "model", "input_tokens", "cache_read_tokens",
               "cache_write_tokens", "output_tokens", "sidechain",
               "reasoning_output_tokens", "reported_cost_usd", "source_file"}
    for row in rows:
        assert set(row) <= allowed, f"unexpected evidence key(s): {set(row) - allowed}"


# --- the payload ---------------------------------------------------------------------

def test_the_finished_payload_validates_and_carries_the_identity(tmp_path):
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[("opencode-messages", export)])
    payload = finished_payload(
        execution_id="t-a1-abc", purpose="work", attempt=1,
        identity=IDENTITY, evidence=res.work,
    )
    assert payload["identity"]["route"] == "or-glm-5.3"
    assert payload["usage"]["status"] == "reported"
    assert payload["outcome_certainty"] == "uncertain", (
        "a harvest finds the process already gone; claiming certainty would be a lie"
    )
    assert payload["price_basis"] is None


def test_a_review_payload_uses_the_reviewers_identity_not_the_workers(tmp_path):
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    res = run(root, sandbox_sources=[("claude-code-transcript", reviewed)])
    payload = finished_payload(
        execution_id="t-a1-rev", purpose="review", attempt=1,
        identity=REVIEWER_IDENTITY, evidence=res.review,
    )
    assert payload["identity"]["model"] == "claude-opus-5"
    assert payload["identity"]["billing_market"] == "subscription"


def test_a_model_the_evidence_names_is_recorded_as_harness_reported(tmp_path):
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    res = run(root, sandbox_sources=[("claude-code-transcript", reviewed)])
    payload = finished_payload(
        execution_id="e", purpose="review", attempt=1,
        identity=REVIEWER_IDENTITY, evidence=res.review,
    )
    assert payload["model_resolved"] == "claude-opus-5"
    assert payload["model_evidence"] == "harness-reported"


def test_an_unavailable_surface_names_no_model_rather_than_the_routes(tmp_path):
    """`model_resolved` answers "what actually ran". With no evidence the honest answer is
    null with `model_evidence: none` — never the model the catalog hoped for."""
    root = task_root(tmp_path)
    res = run(root, work_identity={**IDENTITY, "harness": "antigravity"})
    payload = finished_payload(
        execution_id="e", purpose="work", attempt=1, identity=IDENTITY, evidence=res.work,
    )
    assert payload["model_resolved"] is None
    assert payload["model_evidence"] == "none"


def test_a_task_with_no_recorded_route_attributes_nothing_to_work(tmp_path):
    """No `execution.route_approved` means no identity, and no identity means no idea
    which surface was the worker's. Counting whatever is left over would be a guess with
    a dollar figure attached."""
    stray = transcript(tmp_path / "vm" / "c" / "stray.jsonl", out=999)
    root = task_root(tmp_path)
    res = run(root, work_identity=None, sandbox_sources=[("claude-code-transcript", stray)])
    assert res.work.usage.status == "unavailable"
    assert "no recorded route" in (res.work.usage.reason or "")
    assert res.work.usage.output_tokens is None
    assert "stray.jsonl" in res.unattributed
    assert (root / "run" / "usage" / "raw" / "vm" / "stray.jsonl").exists(), (
        "an unattributable file is still kept"
    )
