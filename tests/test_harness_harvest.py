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
    Source,
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
    res = run(root, sandbox_sources=[Source("opencode-export", export)])
    assert res.work.usage.status == "reported"
    assert res.work.usage.output_tokens == 500


def test_a_transcript_a_review_sidecar_names_is_billed_to_the_review(tmp_path):
    reviewed = transcript(tmp_path / "vm" / "claude" / "r1.jsonl", out=70)
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[Source("opencode-export", export),
                                     Source("claude-session", reviewed)])
    assert res.review.usage.output_tokens == 70
    assert res.work.usage.output_tokens == 500, "the review must not leak into the work total"


def test_two_rounds_are_summed_into_one_review_execution(tmp_path):
    r1 = transcript(tmp_path / "vm" / "c" / "r1.jsonl", msg="a", out=70)
    r2 = transcript(tmp_path / "vm" / "c" / "r2.jsonl", msg="b", out=30)
    root = task_root(tmp_path, reviews={"review-1.txt": [r1], "review-2.txt": [r2]})
    res = run(root, sandbox_sources=[Source("claude-session", r1),
                                     Source("claude-session", r2)],
              work_identity=None)
    assert res.review.usage.output_tokens == 100
    assert res.review.usage.evidence_records == 2


def test_a_round_naming_two_transcripts_refuses_rather_than_guessing(tmp_path):
    """Claude Code writes one session file per `-p` invocation, so two is anomalous — most
    likely the worker's own session landed in the window. On a Claude route, where the
    worker writes transcripts too, summing them would bill a whole worker run to a
    review."""
    a = transcript(tmp_path / "vm" / "c" / "a.jsonl", msg="a")
    b = transcript(tmp_path / "vm" / "c" / "b.jsonl", msg="b")
    root = task_root(tmp_path, reviews={"review-1.txt": [a, b]})
    res = run(root, work_identity={**IDENTITY, "harness": "claude-code"},
              sandbox_sources=[Source("claude-session", a),
                               Source("claude-session", b)])
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
    res = run(root, sandbox_sources=[Source("opencode-export", export),
                                     Source("claude-session", reviewed)])
    kept = sorted(p.name for p in (root / "run" / "usage" / "raw").rglob("*.jsonl"))
    assert len(kept) == 2
    for p in (root / "run" / "usage" / "raw").rglob("*.jsonl"):
        assert p.stat().st_size > 0
    assert res.manifest_path.exists()


def test_the_evidence_ref_points_at_a_file_that_re_derives_the_total(tmp_path):
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[Source("opencode-export", export)])
    ref = Path(res.work.usage.evidence_ref)
    assert ref.exists()
    rows = json.loads(ref.read_text())["rows"]
    assert sum(r["output_tokens"] for r in rows) == res.work.usage.output_tokens


def test_no_row_carries_message_text(tmp_path):
    """The evidence must re-derive a total and reconstruct nothing."""
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    res = run(root, sandbox_sources=[Source("claude-session", reviewed)])
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
    res = run(root, sandbox_sources=[Source("opencode-export", export)])
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
    res = run(root, sandbox_sources=[Source("claude-session", reviewed)])
    payload = finished_payload(
        execution_id="t-a1-rev", purpose="review", attempt=1,
        identity=REVIEWER_IDENTITY, evidence=res.review,
    )
    assert payload["identity"]["model"] == "claude-opus-5"
    assert payload["identity"]["billing_market"] == "subscription"


def test_a_model_the_evidence_names_is_recorded_as_harness_reported(tmp_path):
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    res = run(root, sandbox_sources=[Source("claude-session", reviewed)])
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
    res = run(root, work_identity=None, sandbox_sources=[Source("claude-session", stray)])
    assert res.work.usage.status == "unavailable"
    assert "no recorded route" in (res.work.usage.reason or "")
    assert res.work.usage.output_tokens is None
    assert "stray.jsonl" in res.unattributed
    assert (root / "run" / "usage" / "raw" / "vm" / "stray.jsonl").exists(), (
        "an unattributable file is still kept"
    )


def test_a_sandbox_source_is_matched_by_its_in_VM_path(tmp_path):
    """The review sidecar was written INSIDE the VM and names `/home/agent/...`. The file
    the harvest holds is a copy on the host. Matching on the copy's path would leave every
    sandboxed review — which is every review this deployment runs — unattributed."""
    pulled = transcript(tmp_path / "pulled" / "9f7606ac.jsonl", out=42)
    in_vm = "/home/agent/.claude/projects/-x/9f7606ac.jsonl"
    root = task_root(tmp_path, reviews={"review-1.txt": [Path(in_vm)]})
    res = run(root, sandbox_sources=[Source("claude-session", pulled, origin=in_vm)])
    assert res.review.usage.status == "reported"
    assert res.review.usage.output_tokens == 42
    assert res.unattributed == []


# --- finding the sources at all -------------------------------------------------------
#
# A locator that finds nothing does not fail: it produces `unavailable`, which reads as
# "this harness reports no usage" and is indistinguishable from the truth. So the globs
# get tests of their own, including the near-miss that would quietly widen them.

def test_the_host_locator_finds_the_task_roots_own_claude_sessions(tmp_path):
    from omegahive.harness.harvest import locate_host_sources
    root = tmp_path / "work" / "sess-t-0914"
    root.mkdir(parents=True)
    home = tmp_path / "home"
    slug = str(root).replace("/", "-")
    mine = home / ".claude" / "projects" / f"{slug}-hive"
    mine.mkdir(parents=True)
    (mine / "a.jsonl").write_text("{}\n")
    other = home / ".claude" / "projects" / "-home-cassio-src-something"
    other.mkdir(parents=True)
    (other / "b.jsonl").write_text("{}\n")

    found = locate_host_sources(task_root=root, home=home, codex_home=tmp_path / "nope")
    assert [s.path.name for s in found] == ["a.jsonl"]
    assert found[0].surface == "claude-session"


def test_the_host_locator_does_not_claim_a_neighbouring_task_root(tmp_path):
    """`sess-t-0914` must not match `sess-t-0914b`. A prefix glob would, and the tokens of
    a different task would land on this one's bill."""
    from omegahive.harness.harvest import locate_host_sources
    root = tmp_path / "work" / "sess-t-0914"
    root.mkdir(parents=True)
    home = tmp_path / "home"
    neighbour = home / ".claude" / "projects" / (str(root).replace("/", "-") + "b-hive")
    neighbour.mkdir(parents=True)
    (neighbour / "theirs.jsonl").write_text("{}\n")
    assert locate_host_sources(task_root=root, home=home, codex_home=tmp_path / "n") == []


def test_the_host_locator_finds_codex_rollouts_by_the_directory_they_ran_in(tmp_path):
    from omegahive.harness.harvest import locate_host_sources
    root = tmp_path / "work" / "sess-t-0914"
    root.mkdir(parents=True)
    codex = tmp_path / "codex" / "sessions" / "2026" / "09" / "14"
    codex.mkdir(parents=True)
    mine = codex / "rollout-2026-09-14T10-00-00-abc.jsonl"
    mine.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": f"{root}/repo"}}) + "\n")
    theirs = codex / "rollout-2026-09-14T11-00-00-def.jsonl"
    theirs.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/elsewhere"}}) + "\n")

    found = locate_host_sources(task_root=root, home=tmp_path / "h", codex_home=tmp_path / "codex")
    assert [s.path.name for s in found] == [mine.name]
    assert found[0].surface == "codex-rollout"


def _fake_sbx(tmp_path: Path, *, listing: str = "", cp_ok: bool = True,
              export: str | None = None, list_status: int = 0):
    """A stand-in for `sbx`, recording what it was asked and answering as configured."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[0] == "exec" and "base64 -d | python3 -" in argv[-1]:
            return (0, export) if export is not None else (3, "")
        if argv[0] == "exec":
            return list_status, listing
        if argv[0] == "cp":
            src = argv[1].split(":", 1)[1]
            if cp_ok:
                dest = Path(argv[2]) / Path(src).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text('{"type":"assistant"}\n')
                return 0, ""
            return 1, "cp failed"
        return 1, "unknown"

    return runner, calls


def test_the_pull_keeps_the_in_VM_path_as_the_origin(tmp_path):
    from omegahive.harness.harvest import pull_sandbox_sources
    in_vm = "/home/agent/.claude/projects/-x/abc.jsonl"
    runner, _ = _fake_sbx(tmp_path, listing=f"{in_vm}\n")
    sources, notes = pull_sandbox_sources(
        sandbox="hive-t", staging=tmp_path / "stage", runner=runner)
    claude = [s for s in sources if s.surface == "claude-session"]
    assert len(claude) == 1
    assert claude[0].origin == in_vm
    assert claude[0].path.is_file()
    assert notes == []


def test_the_opencode_export_runs_inside_the_VM_not_against_a_copied_file(tmp_path):
    """SQLite's write-ahead log is a separate file, and on a session that just ended most
    of the data is still in it. Copying `opencode.db` alone loses that silently."""
    from omegahive.harness.harvest import pull_sandbox_sources
    row = json.dumps({"id": "m", "role": "assistant", "modelID": "z-ai/glm-5.3",
                      "cost": 0.5, "tokens": {"input": 1, "output": 2, "reasoning": 0,
                                              "cache": {"read": 0, "write": 0}}})
    runner, calls = _fake_sbx(tmp_path, export=row + "\n")
    sources, notes = pull_sandbox_sources(
        sandbox="hive-t", staging=tmp_path / "stage", runner=runner)
    assert any(s.surface == "opencode-export" for s in sources)
    assert not any(a[0] == "cp" and "opencode.db" in a[1] for a in calls), (
        "the database itself must never be copied out"
    )


def test_an_unreachable_sandbox_is_a_note_and_not_an_exception(tmp_path):
    """Harvesting an old task whose VM was pruned is ordinary, and must not cost the
    surfaces that are still readable."""
    from omegahive.harness.harvest import pull_sandbox_sources
    runner, _ = _fake_sbx(tmp_path, list_status=1, listing="no such sandbox")
    sources, notes = pull_sandbox_sources(
        sandbox="hive-gone", staging=tmp_path / "stage", runner=runner)
    assert sources == []
    assert notes and "hive-gone" in notes[0]


def test_a_failed_copy_is_named_and_does_not_lose_the_others(tmp_path):
    from omegahive.harness.harvest import pull_sandbox_sources
    runner, _ = _fake_sbx(tmp_path, listing="/home/agent/.claude/projects/-x/a.jsonl\n",
                          cp_ok=False)
    sources, notes = pull_sandbox_sources(
        sandbox="hive-t", staging=tmp_path / "stage", runner=runner)
    assert sources == []
    assert any("a.jsonl" in n for n in notes)


def test_a_sandbox_without_an_opencode_store_is_silent_about_it(tmp_path):
    """Every claude and antigravity route is this case. A note here would appear on most
    harvests and train the operator to skim past the ones that matter."""
    from omegahive.harness.harvest import pull_sandbox_sources
    runner, _ = _fake_sbx(tmp_path, listing="/home/agent/.claude/projects/-x/a.jsonl\n")
    _, notes = pull_sandbox_sources(sandbox="hive-t", staging=tmp_path / "s", runner=runner)
    assert notes == []


def test_finished_at_is_derived_from_the_evidence_and_not_from_the_clock(tmp_path):
    """Two harvests of one finished task must produce the SAME payload, or the gateway's
    content-addressed idempotency cannot collapse them and one execution acquires two
    terminal facts. `datetime.now()` would guarantee that."""
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    first = run(root, sandbox_sources=[Source("opencode-export", export)])
    second = run(root, sandbox_sources=[Source("opencode-export", export)])
    a = finished_payload(execution_id="e", purpose="work", attempt=1,
                         identity=IDENTITY, evidence=first.work,
                         finished_at=first.finished_at)
    b = finished_payload(execution_id="e", purpose="work", attempt=1,
                         identity=IDENTITY, evidence=second.work,
                         finished_at=second.finished_at)
    assert a["finished_at"] == b["finished_at"]
    assert a == b


def test_finished_at_is_when_the_evidence_was_last_written(tmp_path):
    import os
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    os.utime(export, (1_789_000_000, 1_789_000_000))
    res = run(root, sandbox_sources=[Source("opencode-export", export)])
    assert res.finished_at.startswith("2026-"), res.finished_at


# --- what the route entails -----------------------------------------------------------
#
# The sidecars are new, so every review already on disk names no transcript. Refusing all
# of them would be needlessly strict on the routes where the answer is forced: nothing in
# an opencode or antigravity sandbox runs Claude Code EXCEPT the reviewer, so a Claude
# transcript there is a review by entailment from the route, not by inference from a
# timestamp. Where the worker IS Claude Code the entailment does not hold, and there the
# sidecar is the only thing that separates the two.

def test_an_unsidecarred_transcript_is_a_review_when_the_worker_is_not_claude(tmp_path):
    orphan = transcript(tmp_path / "vm" / "c" / "o.jsonl", out=60)
    root = task_root(tmp_path, reviews={"review-1.txt": []})
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    res = run(root, sandbox_sources=[Source("opencode-export", export),
                                     Source("claude-session", orphan)])
    assert res.review.usage.status == "reported"
    assert res.review.usage.output_tokens == 60
    assert res.work.usage.output_tokens == 500
    assert res.unattributed == []


def test_an_unsidecarred_transcript_stays_the_workers_when_the_worker_is_claude(tmp_path):
    """Here the entailment fails: both the worker and the reviewer write Claude
    transcripts into the same home, and only the sidecar tells them apart."""
    mine = transcript(tmp_path / "host" / "w.jsonl", out=400)
    root = task_root(tmp_path)
    res = run(root, host_sources=[Source("claude-session", mine)],
              work_identity={**IDENTITY, "harness": "claude-code"})
    assert res.work.usage.output_tokens == 400
    assert res.review.usage.status == "unavailable"


def test_a_claude_worker_with_an_unsidecarred_round_refuses_the_round_by_name(tmp_path):
    """A pre-sidecar review on a Claude route is genuinely unattributable, and its
    transcript is indistinguishable from the worker's. Saying which round is missing is
    the difference between a gap an operator can close and one they cannot see."""
    root = task_root(tmp_path, reviews={"review-1.txt": []})
    mine = transcript(tmp_path / "host" / "w.jsonl")
    res = run(root, host_sources=[Source("claude-session", mine)],
              work_identity={**IDENTITY, "harness": "claude-code"})
    assert res.review.usage.status == "unavailable"
    assert "review-1.txt" in (res.review.usage.reason or "")


def test_a_reviewer_on_another_harness_is_entailed_even_on_a_claude_route(tmp_path):
    """The claude-opus route runs a CODEX reviewer. Its rollout is a shape the Claude
    worker never writes, so there is nothing to confuse it with — the ambiguity is
    per-surface, not per-route, and gating it on the route left every codex review of a
    Claude worker uncounted."""
    rollout = tmp_path / "host" / "rollout-x.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1100, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 0, "output_tokens": 55,
            "reasoning_output_tokens": 0}}}}) + "\n")
    mine = transcript(tmp_path / "host" / "w.jsonl", out=400)
    root = task_root(tmp_path, reviews={"review-1.txt": []})
    res = run(root, work_identity={**IDENTITY, "harness": "claude-code"},
              host_sources=[Source("claude-session", mine), Source("codex-rollout", rollout)])
    assert res.review.usage.status == "reported"
    assert res.review.usage.output_tokens == 55
    assert res.work.usage.output_tokens == 400, "the worker's own transcript stays its own"
    assert res.unattributed == []


def test_a_route_with_no_sandbox_at_all_is_silent_about_it(tmp_path):
    """Every host route is this case — `claude-opus` never had a VM. A note here would
    appear on most harvests and train the operator past the ones that mean something."""
    from omegahive.harness.harvest import pull_sandbox_sources

    def runner(argv: list[str]) -> tuple[int, str]:
        return 1, "ERROR: no sandbox named 'hive-t'\n\nTo create one:\n  sbx create ..."

    sources, notes = pull_sandbox_sources(
        sandbox="hive-t", staging=tmp_path / "s", runner=runner)
    assert sources == []
    assert notes == []


def test_finished_at_is_the_utc_shape_the_gateway_accepts(tmp_path):
    """`finished_at must be ISO-8601 UTC ('...Z')` — the gateway refuses `+00:00`, and a
    payload refused at emit time is a harvest that ran and recorded nothing."""
    import os
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    os.utime(export, (1_789_000_000, 1_789_000_000))
    res = run(root, sandbox_sources=[Source("opencode-export", export)])
    assert res.finished_at.endswith("Z"), res.finished_at
    assert "+00:00" not in res.finished_at


def test_a_generated_export_is_stamped_from_its_contents_not_from_now(tmp_path):
    """The opencode export does not exist until the harvest writes it, so its mtime is
    always "now" — and `finished_at` derived from that changes on every run, which defeats
    the content-addressed idempotency that makes re-harvesting safe."""
    from omegahive.harness.harvest import pull_sandbox_sources
    rows = "\n".join(json.dumps({
        "id": f"m{i}", "role": "assistant", "modelID": "x", "cost": 0.0,
        "time_completed": 1_789_000_000_000 + i * 1000,
        "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }) for i in range(3))
    runner, _ = _fake_sbx(tmp_path, export=rows + "\n")
    sources, _ = pull_sandbox_sources(
        sandbox="hive-t", staging=tmp_path / "s", runner=runner)
    export = next(s for s in sources if s.surface == "opencode-export")
    assert export.path.stat().st_mtime == 1_789_000_002.0


def test_harvesting_twice_does_not_count_the_evidence_twice(tmp_path):
    """The whole point of a re-runnable harvest. Keeping the first copy AND a suffixed
    second one would double every total on the second run — silently, and in the
    direction that makes everything look twice as expensive."""
    root = task_root(tmp_path)
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    first = run(root, sandbox_sources=[Source("opencode-export", export)])
    second = run(root, sandbox_sources=[Source("opencode-export", export)])
    assert second.work.usage.output_tokens == first.work.usage.output_tokens
    kept = list((root / "run" / "usage" / "raw").rglob("*.jsonl"))
    assert len(kept) == 1, [p.name for p in kept]


def test_two_sources_sharing_a_name_in_one_harvest_are_both_kept(tmp_path):
    """Within one run a collision is two different files, and losing one silently is the
    failure this module exists to stop. Across runs it is the same file again."""
    a = transcript(tmp_path / "one" / "same.jsonl", msg="a", out=10)
    b = transcript(tmp_path / "two" / "same.jsonl", msg="b", out=20)
    root = task_root(tmp_path)
    res = run(root, work_identity={**IDENTITY, "harness": "claude-code"},
              host_sources=[Source("claude-session", a), Source("claude-session", b)])
    kept = sorted(p.name for p in (root / "run" / "usage" / "raw").rglob("*.jsonl"))
    assert len(kept) == 2, kept
    assert res.work.usage.output_tokens == 30


# --- which model actually ran ----------------------------------------------------------
#
# The gateway refuses a `success` whose resolved model does not match the pinned one, so
# this choice decides whether the fact lands at all. A session routinely names more than
# one model — the quota and title calls run on a cheaper one — and taking whichever came
# first is a coin toss that loses.

def _evidence(models: list[str]):
    from omegahive.events.types import ExecutionUsage
    from omegahive.harness.usage import UsageEvidence
    return UsageEvidence(
        usage=ExecutionUsage(status="reported", source="s", input_tokens=1,
                             cache_read_tokens=0, cache_write_tokens=0, output_tokens=1),
        rows=[], main_chain_models=models,
    )


def test_the_pinned_model_wins_when_the_evidence_names_it():
    """The harness DID report running it; the auxiliary traffic beside it is not the
    execution's model."""
    p = finished_payload(execution_id="e", purpose="work", attempt=1, identity=IDENTITY,
                         evidence=_evidence(["claude-haiku-4-5", "z-ai/glm-5.3"]))
    assert p["model_resolved"] == "z-ai/glm-5.3"
    assert p["model_evidence"] == "harness-reported"


def test_a_single_reported_model_is_used_even_when_it_is_not_the_pinned_one():
    """A genuine mismatch is a fact worth recording, not one to round away — the gateway
    will refuse it, and being refused is the correct outcome for an execution that ran
    something other than what was approved."""
    p = finished_payload(execution_id="e", purpose="work", attempt=1, identity=IDENTITY,
                         evidence=_evidence(["some-other-model"]))
    assert p["model_resolved"] == "some-other-model"


def test_several_models_none_of_them_the_pinned_one_resolves_to_nothing():
    """Picking one would be a guess, and a guess here is either a false mismatch or a
    false confirmation."""
    p = finished_payload(execution_id="e", purpose="work", attempt=1, identity=IDENTITY,
                         evidence=_evidence(["a", "b"]))
    assert p["model_resolved"] is None
    assert p["model_evidence"] == "none"


def test_each_purpose_is_stamped_from_its_own_evidence(tmp_path):
    """A task-wide timestamp would say the review finished when the worker last wrote,
    which on a task whose reviews ran hours later is simply false."""
    import os
    export = opencode_export(tmp_path / "vm" / "opencode-messages.jsonl")
    os.utime(export, (1_789_000_000, 1_789_000_000))
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    os.utime(reviewed, (1_789_090_000, 1_789_090_000))
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    res = run(root, sandbox_sources=[Source("opencode-export", export),
                                     Source("claude-session", reviewed)])
    assert res.work.finished_at != res.review.finished_at
    assert res.work.finished_at < res.review.finished_at


def test_a_worker_that_writes_nothing_still_gets_its_reviews_attributed(tmp_path):
    """The antigravity routes. Gating the entailment on the worker having a READABLE
    surface disabled it precisely where it is strongest: a harness that writes no usage
    at all cannot be the author of a Claude transcript in its own sandbox, so every one
    of them is a review. Knowing the route is what the entailment needs — not the route
    happening to be measurable."""
    r1 = transcript(tmp_path / "vm" / "c" / "r1.jsonl", msg="a", out=70)
    r2 = transcript(tmp_path / "vm" / "c" / "r2.jsonl", msg="b", out=30)
    root = task_root(tmp_path, reviews={"review-1.txt": [], "review-2.txt": []})
    res = run(root, work_identity={**IDENTITY, "harness": "antigravity"},
              sandbox_sources=[Source("claude-session", r1), Source("claude-session", r2)])
    assert res.work.usage.status == "unavailable"
    assert "antigravity" in (res.work.usage.reason or "")
    assert res.review.usage.status == "reported"
    assert res.review.usage.output_tokens == 100
    assert res.unattributed == []


def test_only_review_files_count_as_rounds(tmp_path):
    """`run/reviews/` also holds the worker's `disposition.md`. Counting it as a round
    made it an unattributable one, which turned a clean harvest into a partial and put a
    refusal about a file that is not a review in front of the operator. Same rule the
    wrapper's own round counter uses: the name has to contain `review`."""
    reviewed = transcript(tmp_path / "vm" / "c" / "r1.jsonl")
    root = task_root(tmp_path, reviews={"review-1.txt": [reviewed]})
    (root / "run" / "reviews" / "disposition.md").write_text("B1: rebutted\n")
    res = run(root, work_identity={**IDENTITY, "harness": "antigravity"},
              sandbox_sources=[Source("claude-session", reviewed)])
    assert res.review.usage.status == "reported"
    manifest = json.loads(res.manifest_path.read_text())
    assert [r["round"] for r in manifest["review_rounds"]] == ["review-1.txt"]
