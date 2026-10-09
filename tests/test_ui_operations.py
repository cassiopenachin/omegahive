"""The page's write side (S3 part C): forms post to the operation service and show its
receipt or refusal; writes need the Tailscale identity, reads do not; the board offers
published orders that have no task."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient

from omegahive.board import fold
from omegahive.events.envelope import Actor, Event
from omegahive.port import PortView
from omegahive.ui.app import create_app

RUN = "prun"
NOW = datetime(2026, 10, 7, tzinfo=UTC)
LOGIN = "me@example.com"
WORKER = Actor(role="worker", id="w1")
HUMAN = Actor(role="human", id="operator")
RESULT = "projects/p/reports/t1-result.md@" + "a" * 40
HEAD = "c" * 40


def ev(seq: int, event_type: str, payload: dict, task: str | None = "t1",
       actor: Actor = WORKER) -> Event:
    return Event(event_id=uuid5(NAMESPACE_URL, f"ui-ops:{seq}"), run_id=RUN, logical_ts=seq,
                 wall_ts=NOW, actor=actor, event_type=event_type, task_id=task,
                 payload=payload, seq=seq)


BLOCKED = [
    ev(1, "task.created", {"title": "First", "task_type": "task"}, actor=HUMAN),
    ev(2, "worker.registered", {"worker_id": "w1"}, None, HUMAN),
    ev(3, "task.assigned", {"worker": "w1"}, actor=Actor(role="coordinator", id="operator")),
    ev(4, "task.accepted", {}),
    ev(5, "question.asked", {"text": "Which base?"}),
    ev(6, "task.blocked", {"reason": "needs an answer"}),
]
IN_REVIEW = [
    *BLOCKED,
    ev(7, "task.reported", {"ref": "o.md@" + "b" * 40, "kind": "answer", "question_seq": 5,
                            "executed_by": "operation-service", "decision_ref": "op-77"},
       actor=HUMAN),
    ev(8, "task.unblocked", {}),
    ev(9, "task.result_posted", {"artifact_refs": [{"ref": RESULT, "quality": "ok"}]}),
]


class ListPort:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    def read(self, cursor: int | None = None) -> PortView:
        return PortView(cursor=len(self.events), generation=1, events=self.events,
                        board=fold(self.events), changed=True)


class FakeOps:
    """Stands in for the service: records each call and checks the login as it does."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reply: dict = {"status": "done", "message": "done"}

    def operate(self, operation, operation_id, params, *, surface="cli", login=None):
        self.calls.append((operation, operation_id, params, surface, login))
        if login != LOGIN:
            return {"status": "forbidden", "message": "this web identity may not operate"}
        return {"operation_id": operation_id, "notes": [], **self.reply}

    def tail(self, task):
        return ["$ working"]

    def merge_candidates(self, run, task):
        return {"candidates": [{"number": 12, "url": "https://github.com/o/r/pull/12",
                                "state": "OPEN", "head": HEAD, "checks": "green",
                                "error": None}], "error": None}

    def routes(self):
        return ["claude-opus", "codex-sol"]


@pytest.fixture
def hub(tmp_path):
    seed, hub = tmp_path / "seed", tmp_path / "hub.git"
    orders = seed / "projects" / "p" / "orders"
    orders.mkdir(parents=True)
    (seed / "projects" / "p" / "project.conf").write_text(f"RUN_ID={RUN}\nCODE_REPO=x\n")
    (orders / "2026-10-01-t1.md").write_text("# Order: First\n")
    (orders / "2026-10-02-fresh.md").write_text(
        "# Order: A fresh one\n\n**Harness / model:** codex-sol / GPT\n")
    (orders / "2026-10-03-typo.md").write_text(
        "# Order: Typo\n\n**Harness / model:** or-mimo-pro / x\n")
    other = seed / "projects" / "q" / "orders"
    other.mkdir(parents=True)
    (seed / "projects" / "q" / "project.conf").write_text("RUN_ID=elsewhere\nCODE_REPO=x\n")
    (other / "2026-10-04-not-this-run.md").write_text("# Order: other run\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(seed)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(hub)], check=True)
    return hub


def client(events: list[Event], ops: FakeOps | None, hub: Path) -> TestClient:
    app = create_app(port_factory=lambda run, gen: ListPort(events),
                     runs_factory=lambda: [], base_path="", workspace_hub=hub,
                     ops_client=ops)  # type: ignore[arg-type]
    return TestClient(app)


def form(page: str, action: str) -> dict[str, str]:
    """The hidden fields of the form posting to `action`, as the page rendered them."""
    import re

    block = page.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]
    return dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', block))


ME = {"Tailscale-User-Login": LOGIN}


# --- the forms post what C1 says and show what the service says -------------------------

def test_the_answer_form_posts_the_question_it_was_rendered_with(hub):
    ops = FakeOps()
    c = client(BLOCKED, ops, hub)
    page = c.get(f"/run/{RUN}/task/t1").text
    hidden = form(page, f"/run/{RUN}/task/t1/op/answer")
    assert hidden["question_seq"] == "5" and len(hidden["operation_id"]) == 32
    r = c.post(f"/run/{RUN}/task/t1/op/answer", headers=ME,
               data={**hidden, "text": "use main"})
    assert r.status_code == 200
    (operation, op_id, params, surface, login), = ops.calls
    assert (operation, op_id, surface, login) == ("answer", hidden["operation_id"], "web", LOGIN)
    assert params == {"run": RUN, "task": "t1", "question_seq": 5, "text": "use main"}
    assert "✓ Done" in r.text


def test_close_and_merge_post_the_result_and_the_pr_head_shown(hub):
    ops = FakeOps()
    c = client(IN_REVIEW, ops, hub)
    page = c.get(f"/run/{RUN}/task/t1").text
    close = form(page, f"/run/{RUN}/task/t1/op/close")
    merge = form(page, f"/run/{RUN}/task/t1/op/merge")
    assert close["result_ref"] == RESULT
    assert (merge["pr"], merge["head_sha"]) == ("12", HEAD)
    c.post(f"/run/{RUN}/task/t1/op/close", headers=ME,
           data={**close, "verdict": "minor rework", "reason": "ok"})
    c.post(f"/run/{RUN}/task/t1/op/merge", headers=ME, data=merge)
    assert [call[2] for call in ops.calls] == [
        {"run": RUN, "task": "t1", "result_ref": RESULT, "verdict": "minor rework",
         "reason": "ok"},
        {"run": RUN, "task": "t1", "pr": 12, "head_sha": HEAD}]


def test_abandon_posts_its_reason(hub):
    ops = FakeOps()
    c = client(BLOCKED, ops, hub)
    hidden = form(c.get(f"/run/{RUN}/task/t1").text, f"/run/{RUN}/task/t1/op/abandon")
    c.post(f"/run/{RUN}/task/t1/op/abandon", headers=ME, data={**hidden, "reason": "dead"})
    assert ops.calls[0][2] == {"run": RUN, "task": "t1", "reason": "dead"}


def test_resume_posts_its_reason_while_the_worker_has_a_window(hub):
    ops = FakeOps()
    c = client(BLOCKED, ops, hub)
    hidden = form(c.get(f"/run/{RUN}/task/t1").text, f"/run/{RUN}/task/t1/op/resume")
    assert len(hidden["operation_id"]) == 32
    c.post(f"/run/{RUN}/task/t1/op/resume", headers=ME,
           data={**hidden, "reason": "login refreshed"})
    (operation, _, params, _, _), = ops.calls
    assert (operation, params) == ("resume", {"run": RUN, "task": "t1",
                                              "reason": "login refreshed"})


def test_resume_is_not_offered_once_a_result_is_in_review(hub):
    page = client(IN_REVIEW, FakeOps(), hub).get(f"/run/{RUN}/task/t1").text
    assert "op/resume" not in page


def test_resume_is_not_offered_without_a_worker_window(hub):
    ops = FakeOps()
    ops.tail = lambda task: None            # type: ignore[method-assign]
    page = client(BLOCKED, ops, hub).get(f"/run/{RUN}/task/t1").text
    assert "op/resume" not in page


def test_a_refusal_is_shown_verbatim(hub):
    ops = FakeOps()
    ops.reply = {"status": "refused", "message": "task is in_review; this needs blocked"}
    c = client(BLOCKED, ops, hub)
    r = c.post(f"/run/{RUN}/task/t1/op/answer", headers=ME,
               data={"operation_id": "op-1", "question_seq": "5", "text": "x"})
    assert "✕ Refused" in r.text
    assert "task is in_review; this needs blocked" in r.text


def test_a_repeated_submit_says_already_done(hub):
    ops = FakeOps()
    ops.reply = {"status": "done", "replayed": True}
    c = client(BLOCKED, ops, hub)
    r = c.post(f"/run/{RUN}/task/t1/op/answer", headers=ME,
               data={"operation_id": "op-1", "question_seq": "5", "text": "x"})
    assert "↺ Already done" in r.text


def test_the_history_card_shows_who_executed_and_which_decision(hub):
    page = client(IN_REVIEW, FakeOps(), hub).get(f"/run/{RUN}/task/t1").text
    assert "executed by operation-service" in page
    assert "decision op-77" in page


def test_the_worker_output_card_shows_the_tail(hub):
    assert "$ working" in client(BLOCKED, FakeOps(), hub).get(f"/run/{RUN}/task/t1").text


def test_without_the_service_the_page_says_so_and_offers_no_form(hub):
    page = client(BLOCKED, None, hub).get(f"/run/{RUN}/task/t1").text
    assert "Operations unavailable" in page and "op/answer" not in page


# --- identity: writes need the 8444 login, reads do not ---------------------------------

WRITES = [f"/run/{RUN}/task/t1/op/answer", f"/run/{RUN}/task/t1/op/close",
          f"/run/{RUN}/task/t1/op/merge", f"/run/{RUN}/task/t1/op/abandon",
          f"/run/{RUN}/task/t1/op/resume",
          f"/run/{RUN}/launch"]
READS = [f"/run/{RUN}/board", f"/run/{RUN}/task/t1", f"/run/{RUN}/events",
         f"/run/{RUN}/metrics"]


@pytest.mark.parametrize("headers", [{}, {"Tailscale-User-Login": "someone@else.com"}],
                         ids=["no-header", "wrong-login"])
def test_every_write_route_is_forbidden_without_the_policys_login(hub, headers):
    c = client(BLOCKED, FakeOps(), hub)
    for route in WRITES:
        r = c.post(route, headers=headers, data={"operation_id": "op-1", "question_seq": "5",
                                                 "pr": "1", "order_path": "x"})
        assert r.status_code == 403, route
        assert "8444" in r.text, route


@pytest.mark.parametrize("headers", [{}, ME], ids=["no-header", "login"])
def test_every_read_route_answers_either_way(hub, headers):
    c = client(BLOCKED, FakeOps(), hub)
    for route in READS:
        assert c.get(route, headers=headers).status_code == 200, route


# --- the board: published, not launched -------------------------------------------------

def test_the_board_offers_published_orders_with_no_task_on_this_run(hub):
    page = client(BLOCKED, FakeOps(), hub).get(f"/run/{RUN}/board").text
    region = page.split("Published, not launched", 1)[1]
    assert "projects/p/orders/2026-10-02-fresh.md" in region
    assert "2026-10-01-t1.md" not in region                 # its task exists
    assert "not-this-run" not in region                      # another project's run
    fresh = region.split("2026-10-02-fresh.md", 1)[1].split("</article>", 1)[0]
    assert '<option value="codex-sol" selected>' in fresh    # the order's route
    typo = region.split("2026-10-03-typo.md", 1)[1].split("</article>", 1)[0]
    assert "names &#39;or-mimo-pro&#39;, which is not an enabled catalog route" in typo
    assert "Pick a route" in typo


def test_a_launch_posts_the_order_and_route_and_a_refusal_shows_in_its_row(hub):
    ops = FakeOps()
    ops.reply = {"status": "failed", "message": "hive: refusing to launch 'fresh' — 3 in review"}
    c = client(BLOCKED, ops, hub)
    hidden = form(c.get(f"/run/{RUN}/board").text, f"/run/{RUN}/launch")
    r = c.post(f"/run/{RUN}/launch", headers=ME, data={**hidden, "route": "codex-sol"})
    (operation, _, params, _, _), = ops.calls
    assert operation == "launch"
    assert params == {"order_path": "projects/p/orders/2026-10-02-fresh.md", "route": "codex-sol"}
    row = r.text.split("Published, not launched", 1)[1].split("2026-10-02-fresh.md", 1)[1]
    assert "refusing to launch &#39;fresh&#39; — 3 in review" in row.split("</article>", 1)[0]


# --- a pressed button says so ------------------------------------------------------------

def _post_forms(page: str) -> list[str]:
    return [block.split(">", 1)[0] for block in page.split('<form ')[1:]
            if 'method="post"' in block.split(">", 1)[0]]


def test_every_operation_form_says_what_it_is_doing_once_pressed(hub):
    c = client(IN_REVIEW, FakeOps(), hub)
    pages = [c.get(f"/run/{RUN}/task/t1").text,
             client(BLOCKED, FakeOps(), hub).get(f"/run/{RUN}/task/t1").text,
             c.get(f"/run/{RUN}/board").text]
    tags = [tag for page in pages for tag in _post_forms(page)]
    assert len(tags) >= 6                           # answer, close, merge, resume, abandon, launch
    assert all('data-pending="' in tag for tag in tags), tags
    launch = next(tag for tag in tags if tag.endswith('/launch"') or '/launch" ' in tag)
    assert "minutes" in launch                      # a sandboxed launch is slow; say so
    assert all("/static/ops.js" in page for page in pages)


def test_the_pending_script_is_served(hub):
    r = client(BLOCKED, FakeOps(), hub).get("/static/ops.js")
    assert r.status_code == 200
    assert "dataset.pending" in r.text and "pageshow" in r.text   # reset on a Back restore


def test_static_links_carry_a_content_version_so_a_deploy_is_not_hidden_by_the_cache(hub):
    import hashlib
    import re

    from omegahive.ui import app as ui_app

    page = client(BLOCKED, FakeOps(), hub).get(f"/run/{RUN}/board").text
    for name in ("ui.css", "live.js", "ops.js"):
        body = (ui_app._ROOT / "static" / name).read_bytes()
        version = hashlib.sha256(body).hexdigest()[:12]
        assert re.search(rf"/static/{re.escape(name)}\?v={version}\"", page), name


def test_a_launch_rows_pending_line_sits_beside_its_form_not_inside_it():
    # Inside the launch row's auto-width form, a long line widened the form's column and
    # squeezed the order's title; beside the form it spans the row like the outcome line.
    # The task page's forms are full width, so there it stays inside, above the divider.
    from omegahive.ui import app as ui_app

    js = (ui_app._ROOT / "static" / "ops.js").read_text()
    css = (ui_app._ROOT / "static" / "ui.css").read_text()
    assert 'form.closest(".launch-row")) form.after(line); else form.append(line)' in js
    assert ".launch-row .op-pending" in css
