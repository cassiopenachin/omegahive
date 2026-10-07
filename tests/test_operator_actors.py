"""Who decided and who executed: `HIVE_ACTOR`, `HIVE_EXECUTED_BY` and `HIVE_DECISION_REF`
on the operator scripts' human-originated emits (salvage plan D4, S3 part A).

The stack is faked at its seams as in test_hive_abandon.py: HIVE_CLI_CMD answers the
board and report reads and records each emit; a `tmux` shim says no window is open; the
instruments a close or an abandon calls afterwards are stand-ins beside copies of the
scripts. Nothing touches a spine, a container or a tmux server.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FIXTURE = Path(__file__).parent / "fixtures" / "operator_emits_before_s3.json"
RESULT_REF = "projects/p/reports/t1-result.md@" + "a" * 40


@pytest.fixture
def rig(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("hive-close", "hive-abandon", "hive-common.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    log = tmp_path / "emits.jsonl"
    for name in ("hive-usage", "hive-metrics", "hive-score"):
        (scripts / name).write_text("#!/bin/sh\nexit 0\n")
        (scripts / name).chmod(0o755)

    board = tmp_path / "board.json"
    report = tmp_path / "report.json"
    report.write_text(json.dumps([
        {"seq": 1, "event_type": "task.assigned", "task_id": "t1", "payload": {"worker": "w1"}},
        {"seq": 2, "event_type": "task.result_posted", "task_id": "t1",
         "payload": {"artifact_refs": [{"ref": RESULT_REF, "quality": "ok"}]}},
    ]))
    # The emit records role, actor, type and the exact payload bytes the script sent.
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, sys\n"
        "a = sys.argv[1:]\n"
        "def opt(n):\n"
        "    return a[a.index(n) + 1] if n in a else None\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'role': opt('--role'), 'actor': opt('--actor'),\n"
        "        'type': opt('--type'), 'task': opt('--task'),\n"
        "        'payload': opt('--payload')}) + '\\n')\n"
    )
    cli = tmp_path / "cli.sh"
    python = shlex.quote(os.environ.get("PYTHON", "python3"))
    cli.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  board-view) cat "{board}" ;;\n'
        f'  emit) {python} "{recorder}" "$@"; echo accepted; exit "${{FAKE_EMIT_RC:-0}}" ;;\n'
        f'  report) cat "{report}" ;;\n'
        "  *) exit 64 ;;\n"
        "esac\n")
    cli.chmod(0o755)
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "tmux").write_text('#!/bin/sh\nexit 0\n')
    (shims / "tmux").chmod(0o755)

    hub, ws = tmp_path / "hub.git", tmp_path / "ws"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
    subprocess.run(["git", "clone", "-q", str(hub), str(ws)], check=True, capture_output=True)
    (ws / "projects" / "p" / "orders").mkdir(parents=True)
    (ws / "projects" / "p" / "project.conf").write_text("RUN_ID=prun\nCODE_REPO=/nowhere/code\n")
    (ws / "projects" / "p" / "orders" / "2026-01-01-t1.md").write_text("# Order: t1\n")
    git = ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "checkout", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "ws"], check=True)
    subprocess.run([*git, "push", "-q", "-u", "origin", "main"], check=True, capture_output=True)

    env = {**os.environ, "HIVE_CLI_CMD": str(cli), "OPS_WS": str(ws),
           "HIVE_TMUX_SESSION": "hive", "PATH": f"{shims}:{os.environ['PATH']}"}
    for name in ("HIVE_RUN_ID", "HIVE_ACTOR", "HIVE_EXECUTED_BY", "HIVE_DECISION_REF"):
        env.pop(name, None)

    def run(script: str, status: str, *args: str, **extra: str):
        board.write_text(json.dumps([{"task": "t1", "status": status, "owner": "w1",
                                      "depends_on": []}]))
        if log.exists():
            log.unlink()
        proc = subprocess.run(["bash", str(scripts / script), *args],
                              capture_output=True, text=True, env={**env, **extra})
        emits = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return proc, emits

    return run


CLOSE = ("hive-close", "in_review", "t1", "--review", "clean", "--reason", "looks right")
ABANDON = ("hive-abandon", "in_progress", "t1", "--reason", "worker died")
DECIDED = {"HIVE_ACTOR": "cassio", "HIVE_EXECUTED_BY": "operation-service",
           "HIVE_DECISION_REF": "op-1"}


def test_unset_variables_emit_exactly_what_the_scripts_emitted_before(rig):
    expected = json.loads(FIXTURE.read_text())
    for name, call in (("close", CLOSE), ("abandon", ABANDON)):
        proc, emits = rig(*call)
        assert proc.returncode == 0, proc.stderr
        assert emits == expected[name], name


@pytest.mark.parametrize("call", [CLOSE, ABANDON], ids=["close", "abandon"])
def test_a_decided_operation_carries_the_actor_and_both_fields_on_every_emit(rig, call):
    proc, emits = rig(*call, **DECIDED)
    assert proc.returncode == 0, proc.stderr
    assert emits
    for emit in emits:
        assert emit["actor"] == "cassio"
        payload = json.loads(emit["payload"])
        assert payload["executed_by"] == "operation-service"
        assert payload["decision_ref"] == "op-1"


def test_the_role_is_not_overridable(rig):
    _, plain = rig(*CLOSE)
    _, decided = rig(*CLOSE, **DECIDED)
    assert [e["role"] for e in decided] == [e["role"] for e in plain]


@pytest.mark.parametrize("bad", ["cas sio", "x;rm", "$(id)", "a/b", "human:cassio"])
def test_an_actor_outside_the_charset_is_refused_before_any_emit(rig, bad):
    proc, emits = rig(*CLOSE, HIVE_ACTOR=bad)
    assert proc.returncode != 0
    assert "HIVE_ACTOR" in proc.stderr
    assert emits == []


def test_an_empty_actor_means_the_default(rig):
    proc, emits = rig(*ABANDON, HIVE_ACTOR="")
    assert proc.returncode == 0, proc.stderr
    assert {e["actor"] for e in emits} == {"operator"}


# --- the models carry the two fields; launch passes every operator emit through `decided` ---

DECIDED_TYPES = {
    "task.status_override": {"status": "done"},
    "review.passed": {"ref_result": RESULT_REF},
    "task.created": {"title": "t", "task_type": "task"},
    "worker.registered": {"worker_id": "w1"},
    "task.assigned": {"worker": "w1"},
    "task.reassigned": {"from": "w1", "to": "w2"},
    "task.reported": {"ref": RESULT_REF, "kind": "answer", "question_seq": 7},
}


@pytest.mark.parametrize("event_type", sorted(DECIDED_TYPES))
def test_each_human_originated_type_keeps_both_fields(event_type):
    from omegahive.events.types import PAYLOADS

    payload = {**DECIDED_TYPES[event_type], "executed_by": "operation-service",
               "decision_ref": "op-1"}
    dumped = PAYLOADS[event_type](**payload).model_dump(mode="json")
    assert dumped["executed_by"] == "operation-service"
    assert dumped["decision_ref"] == "op-1"


def test_the_route_approval_keeps_both_fields():
    from omegahive.events.types import ExecutionRouteApproved

    route = ExecutionRouteApproved(
        execution_id="t1-a1-0123456789", purpose="work", attempt=1,
        catalog_digest="sha256:" + "0" * 64,
        identity={"route": "r", "model_vendor": "anthropic", "provider": "anthropic",
                  "model": "m", "harness": "claude-code", "billing_market": "subscription",
                  "credential_pool": "p", "adapter": "claude"},
        executed_by="operation-service", decision_ref="op-1")
    assert (route.executed_by, route.decision_ref) == ("operation-service", "op-1")


def test_an_answer_report_carries_its_question_and_an_unknown_kind_still_fails():
    from pydantic import ValidationError

    from omegahive.events.types import TaskReported

    assert TaskReported(ref=RESULT_REF, kind="answer", question_seq=7).question_seq == 7
    with pytest.raises(ValidationError):
        TaskReported(ref=RESULT_REF, kind="verdict")


def test_every_operator_emit_in_the_launcher_passes_through_decided():
    import re

    source = (SCRIPTS / "hive-launch").read_text()
    emits = re.findall(r'emit \w+ "\$OPERATOR_ACTOR"(?:[^\n]*\\\n)?[^\n]*', source)
    assert len(emits) == 8
    for emit in emits:
        assert '--payload "$(decided "$' in emit, emit


# --- the machinist role: exactly the standing-authority acts of D7 ---

MACHINIST = {
    "task.assigned",  # the launch mechanics, on a human's request, and a retried launch
    "note.posted",    # summaries and proposals
}


def test_the_machinist_may_emit_exactly_its_standing_authority_set():
    from omegahive.gateway.policy import EMIT_AUTHORITY, Policy

    assert EMIT_AUTHORITY["machinist"] == MACHINIST
    policy = Policy()
    for event_type in MACHINIST:
        assert policy.may_emit("machinist", event_type)
    # D12: an abandon is always a human decision. Signing for spend is the human's alone.
    # D10: --reassign stays a human act.
    for event_type in ("task.status_override", "execution.route_approved", "task.reassigned",
                       "task.created", "review.passed"):
        assert not policy.may_emit("machinist", event_type)


def test_the_human_set_is_unchanged():
    from omegahive.gateway.policy import EMIT_AUTHORITY

    assert EMIT_AUTHORITY["human"] == {
        "task.reported", "task.created", "task.escalated", "task.status_override",
        "worker.registered", "execution.route_approved",
    }


def test_the_machinist_is_an_actor_role():
    from omegahive.events.envelope import Actor

    assert Actor(role="machinist", id="machinist").role == "machinist"


# --- A4: the answer leaves a spine event ---

@pytest.fixture
def answer_rig(rig, tmp_path):
    """The close/abandon rig plus hive-answer and a tmux that holds a live harness."""
    scripts = tmp_path / "scripts"
    shutil.copy2(SCRIPTS / "hive-answer", scripts / "hive-answer")
    shims = tmp_path / "shims"
    (shims / "tmux").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list-windows) echo t1 ;;\n"
        '  display-message) case "$*" in\n'
        "    *pane_current_command*) echo claude ;;\n"
        "    *) echo 0 ;;\n"
        "  esac ;;\n"
        "esac\n"
        "exit 0\n")
    (shims / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (shims / "sleep").chmod(0o755)
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        subprocess.run(["git", "-C", str(tmp_path / "ws"), "config", key, value], check=True)
    report = tmp_path / "report.json"
    report.write_text(json.dumps([
        {"seq": 3, "event_type": "question.asked", "task_id": "t1", "payload": {"text": "old"}},
        {"seq": 9, "event_type": "question.asked", "task_id": "other", "payload": {"text": "x"}},
        {"seq": 5, "event_type": "question.asked", "task_id": "t1", "payload": {"text": "new"}},
    ]))
    return rig


def _answer_head(tmp_path: Path) -> str:
    return subprocess.run(["git", "-C", str(tmp_path / "ws"), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_an_answer_is_reported_against_the_latest_question(answer_rig, tmp_path):
    proc, emits = answer_rig("hive-answer", "blocked", "t1", "use", "event", "time")
    assert proc.returncode == 0, proc.stderr
    (emit,) = emits
    assert (emit["role"], emit["actor"], emit["type"], emit["task"]) == (
        "human", "operator", "task.reported", "t1")
    payload = json.loads(emit["payload"])
    assert payload == {"ref": f"projects/p/orders/2026-01-01-t1.md@{_answer_head(tmp_path)}",
                       "kind": "answer", "question_seq": 5}


def test_a_decided_answer_carries_both_fields(answer_rig):
    proc, emits = answer_rig("hive-answer", "blocked", "t1", "yes", **DECIDED)
    assert proc.returncode == 0, proc.stderr
    (emit,) = emits
    payload = json.loads(emit["payload"])
    assert emit["actor"] == "cassio"
    assert (payload["executed_by"], payload["decision_ref"]) == ("operation-service", "op-1")


def test_a_bare_nudge_reports_nothing(answer_rig):
    proc, emits = answer_rig("hive-answer", "blocked", "t1", "--resume-only", "poke")
    assert proc.returncode == 0, proc.stderr
    assert emits == []


def test_a_failed_record_warns_and_still_nudges(answer_rig):
    proc, _ = answer_rig("hive-answer", "blocked", "t1", "yes", FAKE_EMIT_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "recording it on the spine" in proc.stderr
    assert "--type task.reported --task t1" in proc.stderr
    assert "hive-answer: nudged" in proc.stdout


# --- A5: a result's refs must exist on the hub before the worker's emit leaves ---

@pytest.fixture
def worker_emit(tmp_path):
    """The emit wrapper `issue_worker_interface` writes, pointed at a fixture hub and a
    recording CLI. Returns (call, hub_sha)."""
    hub, clone = tmp_path / "hub.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
    subprocess.run(["git", "clone", "-q", str(hub), str(clone)], check=True, capture_output=True)
    (clone / "reports").mkdir()
    (clone / "reports" / "r.md").write_text("result\n")
    git = ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "checkout", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "r"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True, capture_output=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()

    run_dir = tmp_path / "run"
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{SCRIPTS / "hive-common.sh"}"; '
         'issue_worker_interface "$1" "$2" "$3" "$4" "$5" "$6"',
         "bash", str(run_dir), str(clone), str(clone), "worker/x", "run1", "w1"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "OMEGA_DIR": str(REPO), "WS_HUB": str(hub),
             "OMEGAHIVE_COMPOSE": "true compose"})
    assert proc.returncode == 0, proc.stderr
    log = tmp_path / "argv.log"
    cli = tmp_path / "cli.sh"
    cli.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n')
    cli.chmod(0o755)

    def call(*args: str, hub_path: Path = hub):
        if log.exists():
            log.unlink()
        if hub_path != hub:
            hub.rename(hub_path)
        try:
            proc = subprocess.run([str(run_dir / "emit"), *args], capture_output=True,
                                  text=True, env={**os.environ, "HIVE_CLI_CMD": str(cli)},
                                  check=False)
        finally:
            if hub_path != hub:
                hub_path.rename(hub)
        sent = log.read_text().splitlines() if log.exists() else None
        return proc, sent

    return call, sha


def _result(ref: str) -> str:
    return json.dumps({"artifact_refs": [{"ref": ref, "quality": "ok"}]})


def test_a_result_whose_refs_exist_passes_unchanged(worker_emit):
    call, sha = worker_emit
    payload = _result(f"reports/r.md@{sha}")
    proc, sent = call("--type", "task.result_posted", "--task", "t", "--payload", payload)
    assert proc.returncode == 0, proc.stderr
    assert sent == ["emit", "--run-id", "run1", "--role", "worker", "--actor", "w1",
                    "--type", "task.result_posted", "--task", "t", "--payload", payload]


def test_a_sha_absent_from_the_hub_is_refused_naming_the_sha(worker_emit):
    call, _ = worker_emit
    absent = "f" * 40
    proc, sent = call("--type", "task.result_posted", "--task", "t",
                      "--payload", _result(f"reports/r.md@{absent}"))
    assert proc.returncode != 0
    assert sent is None
    assert absent in proc.stderr and "publish workspace" in proc.stderr


def test_a_path_absent_at_a_present_sha_is_refused_naming_the_path(worker_emit):
    call, sha = worker_emit
    proc, sent = call("--type", "task.result_posted", "--task", "t",
                      "--payload", _result(f"reports/missing.md@{sha}"))
    assert proc.returncode != 0
    assert sent is None
    assert "reports/missing.md" in proc.stderr


def test_a_ref_that_is_not_path_at_sha_is_refused(worker_emit):
    call, _ = worker_emit
    proc, sent = call("--type", "task.result_posted", "--payload", _result("reports/r.md"))
    assert proc.returncode != 0
    assert sent is None


def test_every_other_type_passes_unchanged(worker_emit):
    call, _ = worker_emit
    payload = json.dumps({"ref": "reports/nowhere.md@" + "f" * 40, "kind": "result"})
    proc, sent = call("--type", "task.reported", "--task", "t", "--payload", payload)
    assert proc.returncode == 0, proc.stderr
    assert sent[-6:] == ["--type", "task.reported", "--task", "t", "--payload", payload]


def test_an_unreachable_hub_refuses_nothing_and_says_so(worker_emit, tmp_path):
    call, sha = worker_emit
    proc, sent = call("--type", "task.result_posted",
                      "--payload", _result(f"reports/r.md@{'f' * 40}"),
                      hub_path=tmp_path / "moved.git")
    assert proc.returncode == 0, proc.stderr
    assert sent is not None
    assert "not checked" in proc.stderr
