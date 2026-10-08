"""The operation service (S3 part B): preconditions, replay, the launch cap, the wrapper's
environment and the receipt, against a fake port, a fake wrapper that records its argv and
environment, a fake seat registry and real git fixtures for the hub and the workspace."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from omegahive.board import fold
from omegahive.events.envelope import Actor, Event
from omegahive.operations import (
    EXECUTED_BY,
    NUDGE_NOTE,
    UNCONFIRMED_NOTE,
    Operations,
    Refused,
)
from omegahive.port import PortView

RUN = "prun"
NOW = datetime(2026, 10, 7, tzinfo=UTC)
WORKER = Actor(role="worker", id="w1")
HUMAN = Actor(role="human", id="operator")
REPO = "cassiopenachin/code"
HEAD = "c" * 40


def ev(seq: int, event_type: str, payload: dict, task: str = "t1",
       actor: Actor = WORKER) -> Event:
    return Event(event_id=uuid5(NAMESPACE_URL, f"ops-test:{seq}:{task}"), run_id=RUN,
                 logical_ts=seq, wall_ts=NOW, actor=actor, event_type=event_type,
                 task_id=task, payload=payload, seq=seq)


def created(task: str, seq: int) -> list[Event]:
    return [ev(seq, "task.created", {"title": task, "task_type": "task"}, task, HUMAN),
            ev(seq + 1, "worker.registered", {"worker_id": f"w-{task}"}, None, HUMAN),  # type: ignore[arg-type]
            ev(seq + 2, "task.assigned", {"worker": f"w-{task}"}, task,
               Actor(role="coordinator", id="operator"))]


class ListPort:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    def read(self, cursor: int | None = None) -> PortView:
        return PortView(cursor=len(self.events), generation=1, events=self.events,
                        board=fold(self.events), changed=True)


class FakeSeats:
    def __init__(self) -> None:
        self.live: set[str] = set()
        self.windows: dict[str, str] = {}   # task -> "dead" | "live"
        self.cleared: list[str] = []

    def live_workers(self, tasks):
        return sorted(t for t in self.live if t in set(tasks))

    def clear_dead_seat(self, task):
        state = self.windows.get(task)
        if state is None:
            return "none"
        if state == "live":
            return "occupied"
        self.cleared.append(task)
        del self.windows[task]
        return "cleared"

    def tail(self, task):
        return ["last line"] if task in self.live else None


@pytest.fixture
def rig(tmp_path):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    hub, seed = tmp_path / "hub.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
    (seed / "projects" / "p" / "orders").mkdir(parents=True)
    (seed / "projects" / "p" / "reports").mkdir()
    (seed / "projects" / "p" / "project.conf").write_text(
        f'RUN_ID={RUN}\nCODE_REPO="git@github.com:{REPO}.git"\n')
    (seed / "projects" / "p" / "orders" / "2026-10-01-t1.md").write_text("# Order: t1\n")
    (seed / "projects" / "p" / "orders" / "2026-10-02-new.md").write_text(
        "# Order: new\n\n**Project:** p · **Harness / model:** codex-sol / GPT-6 Sol\n")
    (seed / "projects" / "p" / "orders" / "2026-10-03-other.md").write_text("# Order: other\n")
    (seed / "projects" / "p" / "reports" / "t1-result.md").write_text(
        f"PR: https://github.com/{REPO}/pull/12 (and old https://github.com/{REPO}/pull/3)\n"
        "Not ours: https://github.com/someone/else/pull/99\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    subprocess.run([*git, "-C", str(seed), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(hub), "main"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    result = f"projects/p/reports/t1-result.md@{sha}"

    events: list[Event] = [*created("t1", 1),
                           ev(10, "task.accepted", {}),
                           ev(11, "question.asked", {"text": "which?"}),
                           ev(12, "task.blocked", {"reason": "needs answer"})]
    env_file = tmp_path / "launch.env"
    env_file.write_text('# credentials\nexport OPENROUTER_API_KEY="k-123"\nOTHER=x\n')
    deployment = {
        "scripts_root": "/opt/hive/scripts", "workspace_hub": str(hub),
        "operator_workspace": str(seed), "tmux_session": "hive",
        "receipts_dir": str(tmp_path / "receipts"), "env_file": str(env_file),
        "route_catalog": str(tmp_path / "routes.json"), "operator_actor": "operator",
        "authority": {"web_login": "me@example.com"},
        "bounds": {"operation_timeout_s": 60, "max_concurrent": None},
    }
    calls: list[tuple[list[str], dict[str, str]]] = []
    gh = {"state": "OPEN", "headRefOid": HEAD, "number": 12, "url": "u",
          "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]}
    seats = FakeSeats()

    lands = {"merge": True}     # whether GitHub reports the PR merged after `gh pr merge`

    def runner(argv, env, timeout):
        calls.append((argv, dict(env)))
        if argv[:3] == ["gh", "pr", "merge"] and lands["merge"]:
            gh["state"] = "MERGED"
        if argv[:3] == ["gh", "pr", "view"]:
            return {"argv": argv, "exit_status": 0, "timed_out": False,
                    "stdout": json.dumps(gh), "stderr": ""}
        if "hive-launch" in " ".join(argv):
            time.sleep(0.2)
            seats.live.add(Path(argv[-1] if "--route" not in argv else argv[-3]).stem[11:])
        return {"argv": argv, "exit_status": 0, "timed_out": False, "stdout": "ok", "stderr": ""}

    environ = {"PATH": "/usr/bin", "HOME": "/home/x", "OMEGAHIVE_DATABASE_URL": "secret-dsn",
               "HIVE_CLI_CMD": "nope", "XDG_RUNTIME_DIR": "/run/user/1000"}
    ops = Operations(deployment, lambda run, gen: ListPort(events), runner=runner,
                     seats=seats, environ=environ)  # type: ignore[arg-type]

    class Rig:
        pass

    r = Rig()
    r.ops, r.events, r.calls, r.gh, r.seats, r.result, r.deployment, r.lands = (
        ops, events, calls, gh, seats, result, deployment, lands)
    return r


def post_result(rig) -> None:
    rig.events += [ev(13, "task.unblocked", {}),
                   ev(14, "task.result_posted",
                      {"artifact_refs": [{"ref": rig.result, "quality": "ok"}]})]


def argvs(rig) -> list[list[str]]:
    return [argv for argv, _ in rig.calls]


# --- answer ---------------------------------------------------------------------------

def test_an_answer_to_a_stale_question_is_refused(rig):
    receipt = rig.ops.execute("answer", "op-a1", "cli",
                              {"run": RUN, "task": "t1", "question_seq": 4, "text": "x"})
    assert receipt["status"] == "refused"
    assert "latest question is 11" in receipt["message"]
    assert rig.calls == []


def test_an_answer_to_the_latest_question_runs_the_script(rig):
    receipt = rig.ops.execute("answer", "op-a2", "cli",
                              {"run": RUN, "task": "t1", "question_seq": 11, "text": "use X"})
    assert receipt["status"] == "done", receipt
    assert argvs(rig) == [["/opt/hive/scripts/hive-answer", "t1", "use X"]]


def test_an_answer_whose_nudge_is_unconfirmed_is_done_with_the_warning_as_a_note(rig):
    runner = rig.ops.runner

    def unconfirmed(argv, env, timeout):
        result = runner(argv, env, timeout)
        if argv[0].endswith("hive-answer"):
            return {**result, "exit_status": 3, "stderr": "could not confirm the nudge\n"}
        return result

    rig.ops.runner = unconfirmed
    receipt = rig.ops.execute("answer", "op-a4", "cli",
                              {"run": RUN, "task": "t1", "question_seq": 11, "text": "use X"})
    assert receipt["status"] == "done", receipt
    assert receipt["notes"] == [NUDGE_NOTE]


def test_exit_3_from_any_other_script_is_still_a_failure(rig):
    post_result(rig)
    runner = rig.ops.runner
    rig.ops.runner = lambda argv, env, timeout: {**runner(argv, env, timeout),
                                                 "exit_status": 3, "stderr": "boom"}
    receipt = rig.ops.execute("close", "op-c9", "cli",
                              {"run": RUN, "task": "t1", "result_ref": rig.result,
                               "verdict": "clean"})
    assert receipt["status"] == "failed", receipt


def test_a_multiline_answer_is_refused_and_names_the_long_form(rig):
    receipt = rig.ops.execute("answer", "op-a3", "cli",
                              {"run": RUN, "task": "t1", "question_seq": 11, "text": "a\nb"})
    assert receipt["status"] == "refused"
    assert "--sha" in receipt["message"]


# --- resume ---------------------------------------------------------------------------

def resume(rig, op_id: str, reason: str = "your login was refreshed; continue"):
    return rig.ops.execute("resume", op_id, "cli", {"run": RUN, "task": "t1", "reason": reason})


def test_a_resume_of_a_live_worker_nudges_it_with_the_reason(rig):
    rig.seats.live.add("t1")
    receipt = resume(rig, "op-r1")
    assert receipt["status"] == "done", receipt
    assert argvs(rig) == [["/opt/hive/scripts/hive-answer", "t1", "--resume-only",
                           "your login was refreshed; continue"]]


def test_a_resume_with_no_live_worker_is_refused(rig):
    receipt = resume(rig, "op-r2")
    assert receipt["status"] == "refused" and "no live worker" in receipt["message"]
    assert rig.calls == []


def test_a_resume_of_a_task_in_review_is_refused(rig):
    rig.seats.live.add("t1")
    post_result(rig)
    receipt = resume(rig, "op-r3")
    assert receipt["status"] == "refused" and "in_review" in receipt["message"]
    assert rig.calls == []


def test_a_resume_reason_is_one_line(rig):
    rig.seats.live.add("t1")
    receipt = resume(rig, "op-r4", "a\nb")
    assert receipt["status"] == "refused" and "one line" in receipt["message"]


def test_a_resume_whose_nudge_is_unconfirmed_is_done_with_the_note(rig):
    rig.seats.live.add("t1")
    runner = rig.ops.runner
    rig.ops.runner = lambda argv, env, timeout: {**runner(argv, env, timeout),
                                                 "exit_status": 3, "stderr": "unconfirmed"}
    receipt = resume(rig, "op-r5")
    assert receipt["status"] == "done", receipt
    assert receipt["notes"] == [UNCONFIRMED_NOTE]


# --- close ----------------------------------------------------------------------------

def test_a_close_of_a_result_that_is_not_the_latest_is_refused(rig):
    post_result(rig)
    rig.events.append(ev(15, "task.result_posted",
                         {"artifact_refs": [{"ref": "newer.md@" + "d" * 40, "quality": "ok"}]}))
    receipt = rig.ops.execute("close", "op-c1", "cli", {
        "run": RUN, "task": "t1", "result_ref": rig.result, "verdict": "clean", "reason": ""})
    assert receipt["status"] == "refused"
    assert "newer.md@" in receipt["message"]


def test_a_close_of_the_latest_result_runs_the_scored_close(rig):
    post_result(rig)
    receipt = rig.ops.execute("close", "op-c2", "cli", {
        "run": RUN, "task": "t1", "result_ref": rig.result, "verdict": "minor rework",
        "reason": "fine"})
    assert receipt["status"] == "done", receipt
    assert argvs(rig) == [["/opt/hive/scripts/hive-close", "t1", "--review", "minor rework",
                           "--reason", "fine"]]


# --- merge ----------------------------------------------------------------------------

def merge(rig, op_id: str, pr: int = 12, head: str = HEAD):
    return rig.ops.execute("merge", op_id, "cli",
                           {"run": RUN, "task": "t1", "pr": pr, "head_sha": head})


def test_a_merge_whose_head_moved_is_refused(rig):
    post_result(rig)
    rig.gh["headRefOid"] = "e" * 40
    receipt = merge(rig, "op-m1")
    assert receipt["status"] == "refused"
    assert "head moved" in receipt["message"]
    assert not any(a[:3] == ["gh", "pr", "merge"] for a in argvs(rig))


def test_a_merge_of_a_pr_the_report_does_not_name_is_refused(rig):
    post_result(rig)
    assert merge(rig, "op-m2", pr=99)["status"] == "refused"


def test_a_merge_with_red_checks_is_refused(rig):
    post_result(rig)
    rig.gh["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
    receipt = merge(rig, "op-m3")
    assert receipt["status"] == "refused" and "red" in receipt["message"]


def test_a_merge_of_the_named_green_unmoved_pr_runs_and_records_the_outcome(rig):
    post_result(rig)
    receipt = merge(rig, "op-m4")
    assert receipt["status"] == "done", receipt
    merges = [a for a in argvs(rig) if a[:3] == ["gh", "pr", "merge"]]
    assert merges == [["gh", "pr", "merge", "12", "--repo", REPO, "--squash",
                       "--match-head-commit", HEAD]]
    assert argvs(rig)[-1][:3] == ["gh", "pr", "view"]   # what GitHub says afterwards
    assert receipt["merged"] is True


def test_a_merge_that_github_does_not_report_merged_is_a_failure(rig):
    # gh's exit status is not the outcome: a PR GitHub still calls OPEN was not merged.
    post_result(rig)
    rig.lands["merge"] = False
    receipt = merge(rig, "op-m5")
    assert receipt["status"] == "failed", receipt
    assert receipt["merged"] is False
    assert "OPEN" in receipt["message"]


def test_merge_candidates_are_the_repos_prs_the_report_names(rig):
    post_result(rig)
    candidates = rig.ops.merge_candidates(RUN, "t1")
    assert [c["number"] for c in candidates] == [3, 12]
    assert candidates[0]["head"] == HEAD and candidates[0]["checks"] == "green"


# --- abandon --------------------------------------------------------------------------

def test_an_abandon_of_a_task_that_is_not_abandonable_is_refused(rig):
    post_result(rig)
    rig.events += [ev(15, "review.passed", {"ref_result": rig.result}, actor=HUMAN),
                   ev(16, "task.status_override", {"status": "done"}, actor=HUMAN)]
    receipt = rig.ops.execute("abandon", "op-x1", "cli", {"run": RUN, "task": "t1",
                                                          "reason": "r"})
    assert receipt["status"] == "refused"


def test_an_abandon_with_a_live_worker_is_refused(rig):
    rig.seats.windows["t1"] = "live"
    receipt = rig.ops.execute("abandon", "op-x2", "cli", {"run": RUN, "task": "t1",
                                                          "reason": "r"})
    assert receipt["status"] == "refused" and "running worker" in receipt["message"]


def test_an_abandon_of_a_dead_worker_clears_its_seat_then_abandons(rig):
    rig.seats.windows["t1"] = "dead"
    receipt = rig.ops.execute("abandon", "op-x3", "cli", {"run": RUN, "task": "t1",
                                                          "reason": "died"})
    assert receipt["status"] == "done", receipt
    assert rig.seats.cleared == ["t1"]
    assert "seat cleared" in receipt["notes"][0]
    assert argvs(rig) == [["/opt/hive/scripts/hive-abandon", "t1", "--reason", "died"]]


# --- launch ---------------------------------------------------------------------------

def launch(rig, op_id: str, order: str = "projects/p/orders/2026-10-02-new.md",
           route: str = ""):
    return rig.ops.execute("launch", op_id, "cli", {"order_path": order, "route": route})


def test_a_launch_of_an_order_whose_task_exists_is_refused(rig):
    receipt = launch(rig, "op-l1", "projects/p/orders/2026-10-01-t1.md")
    assert receipt["status"] == "refused" and "already exists" in receipt["message"]


def test_a_launch_of_an_unpublished_order_is_refused(rig):
    receipt = launch(rig, "op-l2", "projects/p/orders/2026-10-09-draft.md")
    assert receipt["status"] == "refused" and "not published" in receipt["message"]


def test_a_launch_takes_the_orders_route_and_runs_in_its_own_scope(rig):
    receipt = launch(rig, "op-l3")
    assert receipt["status"] == "done", receipt
    assert argvs(rig)[-1] == ["systemd-run", "--user", "--scope", "--quiet", "--",
                              "/opt/hive/scripts/hive-launch",
                              "projects/p/orders/2026-10-02-new.md", "--route", "codex-sol"]


def test_a_route_named_in_the_request_wins(rig):
    launch(rig, "op-l4", route="claude-opus")
    assert argvs(rig)[-1][-2:] == ["--route", "claude-opus"]


def test_two_launches_under_a_cap_of_one_yield_one_launch_and_one_refusal(rig):
    rig.deployment["bounds"]["max_concurrent"] = 1
    results: dict[str, dict] = {}

    def go(op_id: str, order: str) -> None:
        results[op_id] = launch(rig, op_id, order)

    threads = [threading.Thread(target=go, args=("op-p1", "projects/p/orders/2026-10-02-new.md")),
               threading.Thread(target=go, args=("op-p2", "projects/p/orders/2026-10-03-other.md"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(r["status"] for r in results.values())
    assert statuses == ["done", "refused"]
    refused = next(r for r in results.values() if r["status"] == "refused")
    assert "allows 1 at once" in refused["message"]


# --- replay, environment, receipt, identity --------------------------------------------

ANSWER = {"run": RUN, "task": "t1", "question_seq": 11, "text": "use X"}


def test_a_repeated_id_with_identical_content_returns_the_first_receipt_and_runs_nothing(rig):
    first = rig.ops.execute("answer", "op-r1", "cli", ANSWER)
    calls = len(rig.calls)
    again = rig.ops.execute("answer", "op-r1", "cli", ANSWER)
    assert len(rig.calls) == calls
    assert again["replayed"] is True
    assert {k: v for k, v in again.items() if k != "replayed"} == first


def test_a_reused_id_with_different_content_is_refused(rig):
    rig.ops.execute("answer", "op-r2", "cli", ANSWER)
    calls = len(rig.calls)
    with pytest.raises(Refused, match="different request"):
        rig.ops.execute("answer", "op-r2", "cli", {**ANSWER, "text": "use Y"})
    assert len(rig.calls) == calls


def test_the_wrapper_sees_the_decision_and_only_the_allow_listed_environment(rig):
    rig.ops.execute("answer", "op-e1", "cli", ANSWER)
    (_, env), = rig.calls
    assert env["HIVE_ACTOR"] == "operator"
    assert env["HIVE_EXECUTED_BY"] == EXECUTED_BY
    assert env["HIVE_DECISION_REF"] == "op-e1"
    assert env["OPENROUTER_API_KEY"] == "k-123"            # the policy's env_file
    assert set(env) == {"PATH", "HOME", "XDG_RUNTIME_DIR", "OPENROUTER_API_KEY", "OTHER",
                        "HIVE_ACTOR", "HIVE_EXECUTED_BY", "HIVE_DECISION_REF"}


def test_the_receipt_holds_what_was_run_and_the_status_either_side(rig, tmp_path):
    rig.ops.execute("answer", "op-e2", "cli", ANSWER)
    receipt = json.loads((tmp_path / "receipts" / "op-e2.json").read_text())
    (command,) = receipt["commands"]
    assert set(command) >= {"argv", "exit_status", "stdout", "stderr"}
    assert receipt["status_before"] == "blocked"
    assert "status_after" in receipt and receipt["status"] == "done"


def test_a_failing_wrapper_is_reported_with_its_own_words(rig):
    def failing(argv, env, timeout):
        return {"argv": argv, "exit_status": 1, "timed_out": False, "stdout": "",
                "stderr": "hive: refusing to launch 'new' — 3 task(s) awaiting review\n"}
    rig.ops.runner = failing
    receipt = launch(rig, "op-f1")
    assert receipt["status"] == "failed"
    assert receipt["message"].startswith("hive: refusing to launch")


@pytest.mark.parametrize("login", [None, "someone@else.com"])
def test_a_web_request_needs_the_policys_login(rig, login):
    with pytest.raises(Refused, match="web_login"):
        rig.ops.execute("answer", "op-w1", "web", ANSWER, login=login)
    assert rig.calls == []


def test_the_policys_login_may_operate_from_the_web(rig):
    receipt = rig.ops.execute("answer", "op-w2", "web", ANSWER, login="me@example.com")
    assert receipt["status"] == "done"


def test_the_tail_of_an_unknown_task_is_no_window(rig):
    assert rig.ops.tail("nope") is None


# --- the socket -------------------------------------------------------------------------

@pytest.fixture
def served(rig, tmp_path):
    import uvicorn

    from omegahive.ops_service import OpsClient, build_app, listen

    socket = tmp_path / "ops.sock"
    bound = listen(socket)                    # as serve() does: bound here, not by uvicorn
    server = uvicorn.Server(uvicorn.Config(build_app(rig.ops), fd=bound.fileno(),
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield OpsClient(str(socket))
    server.should_exit = True
    thread.join(timeout=10)
    bound.close()


def test_the_socket_stays_0600_while_the_service_serves(served, tmp_path):
    import stat

    assert served.operate("answer", "op-s0", ANSWER)["status"] == "done"
    assert stat.S_IMODE((tmp_path / "ops.sock").stat().st_mode) == 0o600


def test_an_operation_over_the_socket_returns_its_receipt(served, rig):
    receipt = served.operate("answer", "op-s1", ANSWER)
    assert receipt["status"] == "done"
    assert receipt["actor"] == "operator"


def test_a_refusal_over_the_socket_is_its_message(served):
    body = served.operate("answer", "op-s2", ANSWER, surface="web", login="intruder@x")
    assert body == {"status": "forbidden",
                    "message": "this web identity may not operate the hive (policy web_login)"}


def test_the_tail_over_the_socket(served, rig):
    rig.seats.live.add("t1")
    assert served.tail("t1") == ["last line"]
    assert served.tail("nope") is None


def test_an_absent_service_is_named(tmp_path):
    from omegahive.ops_service import OpsClient, OpsUnavailable

    with pytest.raises(OpsUnavailable, match="not answering"):
        OpsClient(str(tmp_path / "absent.sock")).tail("t1")
