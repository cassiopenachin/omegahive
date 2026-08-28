"""End-to-end: one worker's turns, on a whole simulated deployment.

This replaces the supervised-transport suite. There is no mediator left to test; what
there is instead is a LIFECYCLE, and this file drives the real one — `hive-launch --turn`
as a subprocess, the wrappers `issue_worker_interface` actually writes, the real
`harness-resolve` and `harness-turn`, the real gateway, real git repositories, and a real
tmux window where tmux is available.

What each section is for:

  1. the turn        one process from prompt to classified exit, and the evidence it keeps
  2. the classifier  the same matrix as the unit tests, but reached through the shell
  3. resume          `hive-answer` waking a native session, and every refusal it owes
  4. worker function a fixture worker that does the whole WORKER.md finish sequence —
                     accept, external critic, publish, red CI, repair, green CI, one
                     moved-main integration, result, exit `posted`
  5. deletion        no launch path still references the retired product

The negative half matters as much as the positive one. Refusing to guess is the product
here, so most of what is asserted below is a refusal with an exact reason.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
import pytest

import scratch_db
from harness_fixtures import catalog, route, runner

REPO = Path(__file__).resolve().parent.parent
LAUNCH = REPO / "scripts" / "hive-launch"
ANSWER = REPO / "scripts" / "hive-answer"
COMMON = REPO / "scripts" / "hive-common.sh"

TASK = "drill-task"
PROJECT = "omegahive"
ORDER = f"projects/{PROJECT}/orders/2026-08-21-{TASK}.md"
ORDER_REF = f"{ORDER}@" + "a" * 40
CODE_BRANCH = f"worker/{TASK}"


def _db_url() -> str:
    url = os.environ.get(scratch_db.BASE_URL_ENV)
    if not url:
        pytest.skip("no scratch database published for this run")
    return url


def _omegahive_cmd() -> list[str]:
    return ["uv", "run", "--project", str(REPO), "omegahive"]


def git(repo, *args, check=True, env=None):
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "drill", "GIT_AUTHOR_EMAIL": "drill@example.invalid",
        "GIT_COMMITTER_NAME": "drill", "GIT_COMMITTER_EMAIL": "drill@example.invalid",
    })
    e.update(env or {})
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=e, timeout=120)
    if check:
        assert proc.returncode == 0, f"git {args}: {proc.stdout}{proc.stderr}"
    return proc


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tmux_isolation():
    """A private tmux server for this file, and nothing of the operator's.

    Without this the suite runs `hive-answer` against the DEFAULT server and the
    operator's own `hive` session — which is not a hypothetical: an early run of this file
    created a `drill-task` window in the live session and left nine orphaned sessions
    behind it. Two guards, both needed:

      * `TMUX_TMPDIR` moves the socket, so every tmux call — the tests' and
        `hive-answer`'s alike — lands on a server holding only this file's sessions;
      * `TMUX`/`TMUX_PANE` are dropped, so a suite started from inside tmux does not
        inherit a client and resolve targets against it.

    The socket directory is a SHORT path under the system temp dir rather than pytest's
    `tmp_path`: tmux binds `$TMUX_TMPDIR/tmux-<uid>/default` through `sockaddr_un`, whose
    `sun_path` is 104 bytes on macOS and 108 on Linux — a kernel limit, not a tunable —
    and pytest's per-test directory names are long enough to reach it.
    """
    socket_dir = tempfile.mkdtemp(prefix="hts-")
    env = {"TMUX_TMPDIR": socket_dir}
    yield env
    # Guarded, because not every test in this file needs tmux to exist: section 6 runs the
    # launch against a stub and must stay green on a host with no tmux installed. An
    # unguarded teardown would raise FileNotFoundError there and fail tests that never
    # touched a pane.
    if shutil.which("tmux"):
        subprocess.run(["tmux", "kill-server"], capture_output=True,
                       env={**os.environ, **env, "TMUX": "", "TMUX_PANE": ""})
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def deployment(tmp_path, tmux_isolation):
    """A whole simulated deployment: a hub, a code remote, a task root, turn 001."""
    db = _db_url()
    run_id = "drill-" + uuid.uuid4().hex[:8]
    worker = f"sess-{TASK}-0821"

    # --- the workspace hub, with the order the worker was launched on ------------------
    hub_seed = tmp_path / "hub-seed"
    hub_seed.mkdir()
    git(hub_seed, "init", "--quiet", "-b", "main")
    write(hub_seed / ORDER, "# Order: drill\n")
    write(hub_seed / f"projects/{PROJECT}/project.conf",
          f'RUN_ID="{run_id}"\nCODE_REPO="{tmp_path}/code-remote.git"\n')
    git(hub_seed, "add", "-A")
    git(hub_seed, "commit", "--quiet", "-m", "seed")
    hub = tmp_path / "hub.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(hub_seed), str(hub))

    # --- the code remote ---------------------------------------------------------------
    code_seed = tmp_path / "code-seed"
    code_seed.mkdir()
    git(code_seed, "init", "--quiet", "-b", "main")
    write(code_seed / "README.md", "seed\n")
    git(code_seed, "add", "-A")
    git(code_seed, "commit", "--quiet", "-m", "seed")
    code_remote = tmp_path / "code-remote.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(code_seed), str(code_remote))

    # --- the operator's own workspace clone (what hive-answer writes into) -------------
    ops_ws = tmp_path / "ops-hive"
    git(tmp_path, "clone", "--quiet", str(hub), str(ops_ws))

    # --- the worker's ONE task root ----------------------------------------------------
    work_root = tmp_path / "work"
    work_root.mkdir()
    task_root = work_root / worker
    task_root.mkdir()
    ws_root = task_root / "hive"
    code_root = task_root / PROJECT
    run_dir = task_root / "run"
    git(tmp_path, "clone", "--quiet", str(hub), str(ws_root))
    git(tmp_path, "clone", "--quiet", str(code_remote), str(code_root))
    git(code_root, "checkout", "--quiet", "-b", CODE_BRANCH)

    usage_file = tmp_path / "fake-usage.jsonl"
    script = tmp_path / "worker-script.sh"
    # A HOME with no gitconfig, and both other git config layers closed. Without this the
    # suite passes on a developer machine and fails on CI, which is what happened once:
    # the sync wrapper's rebase depended on an ambient committer identity, and where
    # there was none it left the clone detached with the worker's commit off HEAD.
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    session_id = str(uuid.uuid4())

    # The worker's emit path, standing in for `podman compose run --rm -T cli`. The
    # credential lives INSIDE this command, exactly as it lives inside the container on a
    # real deployment: a direct worker reaches the governed CLI, and never the database.
    # `inherit_env` could not carry the DSN even if a route tried — a Hive authority
    # credential name refuses at the catalog and its value is dropped by the adapter — so
    # this is the only shape a working direct route can have, in a test or in production.
    cli = tmp_path / "hive-cli"
    cli.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'export OMEGAHIVE_DATABASE_URL="{db}"\n'
        'export OMEGAHIVE_GATEWAY_DATABASE_URL=""\n'
        f'exec {" ".join(_omegahive_cmd())} "$@"\n'
    )
    cli.chmod(0o755)

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(clean_home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HIVE_CLI_CMD": str(cli),
        "HIVE_FAKE_BEHAVIOUR": "success",
        "HIVE_FAKE_SCRIPT": str(script),
        "HIVE_FAKE_USAGE_FILE": str(usage_file),
        # Two credentials that must never reach the child.
        "GH_TOKEN": "ghp_must_not_propagate",
        "OMEGAHIVE_GATEWAY_DATABASE_URL": "postgres://must-not-propagate",
    }

    entries = [route(runner=runner(
        inherit_env=["HIVE_CLI_CMD", "HIVE_FAKE_BEHAVIOUR", "HIVE_FAKE_SCRIPT",
                     "HIVE_FAKE_USAGE_FILE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"]))]
    catalog_doc = catalog(*entries)
    catalog_path = tmp_path / "routes.json"
    catalog_path.write_text(json.dumps(catalog_doc, indent=2))

    plan = resolve_turn(catalog_doc, {
        "route": None, "task": TASK, "order_ref": ORDER_REF, "purpose": "work",
        "attempt": 1, "kickoff": "You are hive worker drill.",
        "task_root": str(task_root), "cwd": str(ws_root), "code_root": str(code_root),
        "run_dir": str(run_dir / "turns" / "001"), "session_id": session_id, "env": env,
        "turn_kind": "initial", "turn_id": "001", "resume_session_id": "",
    })
    plan.update({
        "run_id": run_id,
        "worker": worker,
        "bridge": {
            "ws_hub": str(hub), "code_repo": str(code_remote),
            "code_branch": CODE_BRANCH, "project": PROJECT,
            "order_ref": ORDER_REF, "title": "Drill",
        },
    })

    # Issue the interface with the SHIPPED code, not a copy of it.
    issue = subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail; source "{COMMON}"; '
         'issue_worker_interface "$1" "$2" "$3" "$4" "$5" "$6"',
         "bash", str(run_dir), str(ws_root), str(code_root), CODE_BRANCH, run_id, worker],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
        env={**os.environ, "OMEGA_DIR": str(REPO)})
    assert issue.returncode == 0, issue.stdout + issue.stderr

    turn_dir = run_dir / "turns" / "001"
    turn_dir.mkdir(parents=True, exist_ok=True)
    (turn_dir / "turn.json").write_text(json.dumps(plan))

    return {
        "tmux_env": tmux_isolation,
        "tmux_session": "hts-" + uuid.uuid4().hex[:8],
        "db": db, "run_id": run_id, "worker": worker, "plan": plan, "session_id": session_id,
        "tmp": tmp_path, "hub": hub, "hub_seed": hub_seed, "code_remote": code_remote,
        "ops_ws": ops_ws, "work_root": work_root, "task_root": task_root,
        "ws_root": ws_root, "code_root": code_root, "run_dir": run_dir,
        "turn_dir": turn_dir, "catalog": catalog_path, "script": script, "env": env,
        "usage_file": usage_file,
    }


def resolve_turn(catalog_doc, request: dict) -> dict:
    req = dict(request)
    req["catalog_b64"] = base64.b64encode(json.dumps(catalog_doc).encode()).decode()
    proc = subprocess.run([*_omegahive_cmd(), "harness-resolve"],
                          input=json.dumps(req), capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def shell_env(dep, **over) -> dict:
    env = dict(os.environ)
    # The tmux isolation FIRST, and never overridable by a caller's kwargs by accident:
    # a test that forgets it would act on the operator's live server.
    env.update(dep["tmux_env"])
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    # A git identity, named here rather than inherited. `hive-answer` commits the answer
    # into the operator's workspace clone with ordinary `git commit`, which needs one — and
    # a developer machine supplies it from ~/.gitconfig while CI does not. That is exactly
    # the "passes locally, fails on CI" shape the deployment fixture's own clean-HOME
    # comment already warns about, and it caught this file on its first CI run. The two
    # config layers are closed for the same reason: the tests must depend on what is set
    # here and on nothing ambient.
    env.update({
        "GIT_AUTHOR_NAME": "drill operator",
        "GIT_AUTHOR_EMAIL": "drill@example.invalid",
        "GIT_COMMITTER_NAME": "drill operator",
        "GIT_COMMITTER_EMAIL": "drill@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HIVE_TMUX_SESSION": dep["tmux_session"],
        "OMEGAHIVE_DATABASE_URL": dep["db"],
        "OMEGAHIVE_GATEWAY_DATABASE_URL": "",
        "HIVE_CLI_CMD": " ".join(_omegahive_cmd()),
        "OMEGA_DIR": str(REPO),
        "WORK_ROOT": str(dep["work_root"]),
        "WS_HUB": str(dep["hub"]),
        "OPS_WS": str(dep["ops_ws"]),
        "HIVE_ROUTE_CATALOG": str(dep["catalog"]),
        "HIVE_RUN_ID": dep["run_id"],
        "CANON_ROOT": str(dep["tmp"]),
    })
    env.update(over)
    return env


def run_turn(dep, turn_dir=None, *, timeout=300, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(LAUNCH), "--turn", str(turn_dir or dep["turn_dir"])],
        capture_output=True, text=True, env=env or shell_env(dep),
        cwd=str(REPO), timeout=timeout)


def worker_script(dep, body: str) -> None:
    """What the fake harness runs as the worker's session."""
    dep["script"].write_text(
        "#!/usr/bin/env bash\n"
        f'EMIT="{dep["run_dir"]}/emit"\n'
        f'HIVE="{dep["run_dir"]}/hive"\n'
        f'WS="{dep["ws_root"]}"\n'
        f'CODE="{dep["code_root"]}"\n'
        f'OUT="{dep["tmp"]}/worker-out.txt"\n'
        ': > "$OUT"\n'
        "say() { printf '%s\\n' \"$*\" >> \"$OUT\"; }\n"
        + body
    )
    dep["script"].chmod(0o755)
    dep["env"]["HIVE_FAKE_BEHAVIOUR"] = "protocol"
    set_child_env(dep, HIVE_FAKE_BEHAVIOUR="protocol")


def set_child_env(dep, turn_dir=None, **pairs) -> None:
    """The child's environment is fixed in turn.json at RESOLVE time by the route's
    allowlist; the turn runner never merges its own in. So a test that needs a different
    value sets it where a real deployment variable would be: in the resolved turn."""
    path = (turn_dir or dep["turn_dir"]) / "turn.json"
    doc = json.loads(path.read_text())
    doc["env"].update(pairs)
    path.write_text(json.dumps(doc))


def make_resume_turn(dep, turn_id: str, *, session=None) -> Path:
    """Build the next turn directory directly, for the runner tests in section 2.

    Nothing prepares turns on the production path any more — a worker is one interactive
    session — but `hive-launch --turn` is retained, and how the runner behaves on a second
    turn is still worth asserting for as long as that code is in the tree.
    """
    prev = json.loads((dep["turn_dir"] / "turn.json").read_text())
    turn_dir = dep["run_dir"] / "turns" / turn_id
    catalog_doc = json.loads(dep["catalog"].read_text())
    plan = resolve_turn(catalog_doc, {
        "route": prev["identity"]["route"], "task": TASK, "order_ref": ORDER_REF,
        "purpose": "work", "attempt": 1, "kickoff": "continue",
        "task_root": prev["task_root"], "cwd": prev["cwd"], "code_root": prev["code_root"],
        "run_dir": str(turn_dir),
        "session_id": session or dep["session_id"], "env": prev["env"],
        "turn_kind": "resume", "turn_id": turn_id,
        "resume_session_id": session or dep["session_id"],
    })
    plan.update({"run_id": dep["run_id"], "worker": dep["worker"], "bridge": prev["bridge"]})
    turn_dir.mkdir(parents=True, exist_ok=True)
    (turn_dir / "turn.json").write_text(json.dumps(plan))
    return turn_dir


def out(dep) -> str:
    path = dep["tmp"] / "worker-out.txt"
    return path.read_text() if path.exists() else ""


def events(dep) -> list[tuple[str, str, str, dict]]:
    with psycopg.connect(dep["db"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, actor_role, actor_id, payload FROM events "
            "WHERE run_id = %s ORDER BY seq", (dep["run_id"],))
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def finished(dep) -> dict:
    rows = [r for r in events(dep) if r[0] == "execution.finished"]
    assert rows, "the turn recorded no terminal fact"
    return rows[-1][3]


def seed_board(dep) -> None:
    """The board state a worker's `task.accepted` needs: created, registered, assigned."""
    env = dict(os.environ)
    env.update({"OMEGAHIVE_DATABASE_URL": dep["db"], "OMEGAHIVE_GATEWAY_DATABASE_URL": ""})
    for role, actor, etype, payload, task in (
        ("human", "operator", "task.created",
         {"title": "Drill", "task_type": "task", "acceptance": f"per {ORDER_REF}"}, TASK),
        ("human", "operator", "worker.registered", {"worker_id": dep["worker"]}, None),
        ("coordinator", "operator", "task.assigned", {"worker": dep["worker"]}, TASK),
    ):
        cmd = [*_omegahive_cmd(), "emit", "--run-id", dep["run_id"], "--role", role,
               "--actor", actor, "--type", etype, "--payload", json.dumps(payload)]
        if task:
            cmd += ["--task", task]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=str(REPO), timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr


# =====================================================================================
# 1. The turn: one process, visible, from prompt to classified exit
# =====================================================================================

def test_a_turn_records_started_and_finished_as_an_instrument_never_as_the_worker(deployment):
    """The separation the classification rests on. The identity that writes the exit
    record structurally cannot author a `task.*` event, which is what makes a
    classification derived from the worker's own events worth reading."""
    seed_board(deployment)
    assert run_turn(deployment).returncode == 0

    rows = events(deployment)
    lifecycle = [r for r in rows if r[0].startswith("execution.")]
    assert [r[0] for r in lifecycle] == ["execution.started", "execution.finished"]
    for _, role, actor, _ in lifecycle:
        assert role == "instrument"
        assert actor == f"turn-{deployment['worker']}"


def test_a_turn_retains_the_structured_stream_and_renders_it_in_the_pane(deployment):
    """Both consumers of one stdout. A raw JSON stream alone is retained evidence, not an
    operator interface, and a rendered stream alone is not evidence."""
    seed_board(deployment)
    proc = run_turn(deployment)
    assert proc.returncode == 0

    stream = (deployment["turn_dir"] / "stream.jsonl").read_text()
    assert '"type":"session"' in stream, "the stream is retained verbatim"
    assert "· session" in proc.stdout, proc.stdout
    assert "· terminal: completed" in proc.stdout, proc.stdout


def test_the_pane_keeps_an_intelligible_summary_after_the_process_is_gone(deployment):
    seed_board(deployment)
    proc = run_turn(deployment)
    summary = (deployment["turn_dir"] / "summary.txt").read_text()
    assert summary in proc.stdout, "the summary is printed, not only filed"
    assert "UNCLASSIFIED" in summary, "this worker never said how it went"
    assert "missing_worker_terminal_event" in summary
    assert deployment["session_id"] in summary, "the session it could be resumed from"


def test_the_turn_records_the_session_the_cursor_and_the_stream_digest(deployment):
    seed_board(deployment)
    run_turn(deployment)
    payload = finished(deployment)
    assert payload["session_id"] == deployment["session_id"]
    assert isinstance(payload["spine_cursor"], int)
    assert payload["spine_basis"] == "read"
    assert payload["stream_digest"].startswith("sha256:")
    assert payload["stream_records"] >= 2
    facts = json.loads((deployment["turn_dir"] / "facts.json").read_text())
    assert facts["digest"] == payload["stream_digest"]


def test_the_process_view_and_the_task_view_are_separate_fields(deployment):
    """`outcome` is what it has always been — the PROCESS ended cleanly or it did not.
    `classification` is the task-facing answer. Keeping both is what stops an OS exit
    code from ever becoming a `task.failed`."""
    seed_board(deployment)
    set_child_env(deployment, HIVE_FAKE_BEHAVIOUR="failure")
    proc = run_turn(deployment)
    assert proc.returncode == 3
    payload = finished(deployment)
    assert payload["outcome"] == "failure"
    assert payload["classification"] == "failed"
    assert payload["task_disposition"] is None
    assert not [r for r in events(deployment) if r[0] == "task.failed"], (
        "a process failure must never manufacture a worker-owned task event"
    )


def test_a_turn_that_already_finished_replays_its_terminal_fact_rather_than_rerunning(
        deployment):
    seed_board(deployment)
    assert run_turn(deployment).returncode == 0
    before = len([r for r in events(deployment) if r[0] == "execution.finished"])
    second = run_turn(deployment)
    assert second.returncode == 0
    assert "already recorded a terminal fact" in second.stderr
    after = len([r for r in events(deployment) if r[0] == "execution.finished"])
    assert after == before, "identical bytes must deduplicate onto the original event"


def test_the_child_environment_is_the_allowlist_and_no_credential_reaches_it(deployment):
    seed_board(deployment)
    worker_script(deployment, '''
say "GH_TOKEN=${GH_TOKEN:-<unset>}"
say "GATEWAY=${OMEGAHIVE_GATEWAY_DATABASE_URL:-<unset>}"
say "PATH_SET=${PATH:+yes}"
''')
    assert run_turn(deployment).returncode == 0
    text = out(deployment)
    assert "GH_TOKEN=<unset>" in text, text
    assert "GATEWAY=<unset>" in text, text
    assert "PATH_SET=yes" in text


# =====================================================================================
# 2. Classification, reached through the shell
# =====================================================================================

def test_a_worker_that_posts_a_result_exits_posted(deployment):
    seed_board(deployment)
    worker_script(deployment, f'''
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1
"$EMIT" --type task.result_posted --task {TASK} \\
  --payload '{{"artifact_refs": [{{"ref": "r.md@{"a" * 40}", "quality": "ok"}}]}}' >/dev/null 2>&1
''')
    assert run_turn(deployment).returncode == 0
    payload = finished(deployment)
    assert payload["classification"] == "posted"
    assert payload["task_disposition"] == "task.result_posted"
    assert isinstance(payload["terminal_event_seq"], int)
    assert payload["terminal_event_seq"] > payload["spine_cursor"]


def test_a_worker_that_blocks_exits_blocked(deployment):
    seed_board(deployment)
    worker_script(deployment, f'''
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1
"$EMIT" --type task.blocked --task {TASK} \\
  --payload '{{"reason": "needs a decision", "needs": "decision"}}' >/dev/null 2>&1
''')
    assert run_turn(deployment).returncode == 0
    assert finished(deployment)["classification"] == "blocked"


def test_a_prior_turns_block_is_not_read_as_the_next_turns_exit(deployment):
    """The cursor doing its job on the real path. Turn 001 blocks; turn 002 says nothing.
    Turn 002 must be `unclassified`, not a second `blocked` inherited from turn 001."""
    seed_board(deployment)
    worker_script(deployment, f'''
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1
"$EMIT" --type task.blocked --task {TASK} \\
  --payload '{{"reason": "needs a decision", "needs": "decision"}}' >/dev/null 2>&1
''')
    assert run_turn(deployment).returncode == 0
    assert finished(deployment)["classification"] == "blocked"

    second = make_resume_turn(deployment, "002")
    set_child_env(deployment, second, HIVE_FAKE_BEHAVIOUR="success")
    assert run_turn(deployment, second).returncode == 0
    payload = finished(deployment)
    assert payload["turn_id"] == "002"
    assert payload["classification"] == "unclassified"
    assert payload["classification_reason"] == "missing_worker_terminal_event"


def test_an_explicit_budget_exit_is_budget_and_leaves_the_board_alone(deployment):
    seed_board(deployment)
    set_child_env(deployment, HIVE_FAKE_BEHAVIOUR="budget")
    assert run_turn(deployment).returncode == 0
    payload = finished(deployment)
    assert payload["classification"] == "budget"
    assert payload["harness_terminal_reason"] == "fake_budget_exhausted"
    board_moving = [r for r in events(deployment) if r[0].startswith("task.")]
    assert {r[0] for r in board_moving} == {"task.created", "task.assigned"}


def test_a_truncated_stream_is_preserved_and_refuses_to_classify(deployment):
    seed_board(deployment)
    set_child_env(deployment, HIVE_FAKE_BEHAVIOUR="truncated")
    proc = run_turn(deployment)
    assert proc.returncode == 137
    payload = finished(deployment)
    assert payload["classification"] == "unclassified"
    assert "insufficient_harness_evidence" in payload["classification_reason"]
    assert payload["stream_truncated"] is True
    assert payload["stream_malformed"] == 1
    assert '"type":"session"' in (deployment["turn_dir"] / "stream.jsonl").read_text()


def test_a_malformed_line_does_not_blind_the_renderer_or_the_scan(deployment):
    seed_board(deployment)
    set_child_env(deployment, HIVE_FAKE_BEHAVIOUR="malformed")
    proc = run_turn(deployment)
    assert proc.returncode == 0
    assert "! unparsed harness line" in proc.stdout, proc.stdout
    assert "· terminal: completed" in proc.stdout, "the renderer survived the bad line"
    payload = finished(deployment)
    assert payload["stream_malformed"] == 1
    assert payload["harness_terminal_kind"] == "completed"


def test_a_shutdown_error_after_a_posted_result_keeps_posted_as_the_primary_fact(deployment):
    seed_board(deployment)
    worker_script(deployment, f'''
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1
"$EMIT" --type task.result_posted --task {TASK} \\
  --payload '{{"artifact_refs": [{{"ref": "r.md@{"a" * 40}", "quality": "ok"}}]}}' >/dev/null 2>&1
exit 9
''')
    proc = run_turn(deployment)
    assert proc.returncode == 9
    payload = finished(deployment)
    assert payload["classification"] == "posted"
    assert payload["harness_failed_after_disposition"] is True
    assert payload["outcome"] == "failure", "the process view still says it died"


def test_a_model_mismatch_is_a_terminal_failure_not_a_warning(deployment):
    seed_board(deployment)
    set_child_env(deployment, HIVE_FAKE_BEHAVIOUR="wrongmodel")
    proc = run_turn(deployment)
    assert "MODEL MISMATCH" in proc.stderr
    assert finished(deployment)["outcome"] == "failure"


def test_an_unreadable_spine_refuses_to_classify_and_keeps_the_evidence(deployment):
    """`unclassified(spine_unavailable)` with the harness half intact. A confident outcome
    derived from half the evidence would be the worst possible record."""
    seed_board(deployment)
    cli = deployment["tmp"] / "hive-cli"
    cli.write_text(cli.read_text().replace(deployment["db"], "postgres://127.0.0.1:1/nope"))
    env = shell_env(deployment, OMEGAHIVE_DATABASE_URL="postgres://127.0.0.1:1/nope")
    proc = run_turn(deployment, env=env)

    exit_record = json.loads((deployment["turn_dir"] / "exit.json").read_text())
    assert exit_record["classification"] == "unclassified"
    assert exit_record["spine_basis"] == "unavailable"
    assert exit_record["harness_terminal_kind"] == "completed", (
        "the half we COULD read is still recorded"
    )
    assert (deployment["turn_dir"] / "finished.json").exists(), (
        "the terminal payload is preserved for replay"
    )
    # The instrument wrapper could not emit either, so no `execution.finished` reached the
    # spine — and a pane that closed GREEN over a turn the spine has no record of is the
    # single outcome this whole path exists to prevent.
    assert proc.returncode == 70, proc.stdout + proc.stderr
    assert "FAILED to emit execution.finished" in proc.stderr


def test_a_killed_turn_is_recovered_from_its_evidence_not_run_again(deployment):
    """A turn whose process died between running and recording. Re-running the harness
    would spend a second model call, destroy the only copy of what the first one said, and
    classify against a cursor taken AFTER events the first turn itself emitted."""
    seed_board(deployment)
    worker_script(deployment, f'''
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1
"$EMIT" --type task.blocked --task {TASK} \\
  --payload '{{"reason": "needs a decision", "needs": "decision"}}' >/dev/null 2>&1
''')
    assert run_turn(deployment).returncode == 0
    td = deployment["turn_dir"]
    stream_before = (td / "stream.jsonl").read_text()
    cursor_before = json.loads((td / "started.json").read_text())["spine_cursor"]

    # Rewind to the state a SIGKILL between the run and the classification leaves behind.
    for name in ("finished.json", "exit.json", "facts.json", "summary.txt"):
        (td / name).unlink()

    proc = run_turn(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RECOVERING" in proc.stderr
    assert "harness is NOT run again" in proc.stderr
    assert (td / "stream.jsonl").read_text() == stream_before, "the evidence is intact"

    exit_record = json.loads((td / "exit.json").read_text())
    assert exit_record["spine_cursor"] == cursor_before, (
        "classified against the window the dead process ran in, not a fresh one"
    )
    assert exit_record["classification"] == "blocked"
    payload = finished(deployment)
    assert payload["outcome"] == "interrupted"
    assert payload["outcome_certainty"] == "uncertain"
    assert payload["exit_code"] is None, "no process is left to report one"


def test_a_second_runner_on_one_turn_is_refused_atomically(deployment):
    """Two runners would share a stream file, overwrite each other's terminal payload,
    and — capturing different cursors — emit two DIFFERENT payloads for one turn, which
    content-addressed idempotency cannot collapse."""
    seed_board(deployment)
    (deployment["turn_dir"] / "claim").write_text(str(os.getpid()))
    proc = run_turn(deployment)
    assert proc.returncode != 0
    assert "already being run by pid" in proc.stderr
    assert not (deployment["turn_dir"] / "stream.jsonl").exists(), (
        "the refused runner must not have touched the live turn's evidence"
    )


def test_a_stale_claim_from_a_dead_runner_is_taken_over(deployment):
    """A claim whose holder is gone must not strand a turn forever — a single kill would
    then cost the seat, which is worse than the race the claim protects against."""
    seed_board(deployment)
    (deployment["turn_dir"] / "claim").write_text("999999")
    proc = run_turn(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (deployment["turn_dir"] / "finished.json").exists()
    assert not (deployment["turn_dir"] / "claim").exists(), "released on the way out"


def test_reclassifying_a_saved_turn_yields_byte_identical_evidence(deployment):
    """Determinism over the saved stream and cursor. A later reconciliation recomputes
    the same record; it does not produce a second, competing answer."""
    seed_board(deployment)
    run_turn(deployment)
    first = (deployment["turn_dir"] / "exit.json").read_text()
    request = {
        "adapter": "fake",
        "stream": (deployment["turn_dir"] / "stream.jsonl").read_text(),
        "exit_code": 0,
        "cursor": json.loads(first)["spine_cursor"],
        "run": deployment["run_id"], "task": TASK, "worker": deployment["worker"],
        "turn_id": "001", "turn_kind": "initial", "route": "fake-subscription",
    }
    again = subprocess.run([*_omegahive_cmd(), "harness-turn"], input=json.dumps(request),
                           capture_output=True, text=True, cwd=REPO,
                           env=shell_env(deployment))
    assert again.returncode == 0, again.stdout + again.stderr
    assert json.dumps(json.loads(again.stdout)["exit"], sort_keys=True) == \
        json.dumps(json.loads(first), sort_keys=True)


# =====================================================================================
# 3. The nudge — hive-answer, both modes and every refusal it owes
#
# A worker is an interactive session that never ended (2026-08-23-direction.md §3), so
# continuing one is a line of text typed into the window that already holds it. There is no
# turn to prepare, no native session id to resolve and no adapter to re-check: what this
# section asserts is that the answer lands durably FIRST, that the line reaches a live
# harness, and that every state which is not a live harness is refused rather than typed at.
# =====================================================================================

FAKE_HARNESS = '''
import pathlib, sys, time
line = sys.stdin.readline()
pathlib.Path(sys.argv[1]).write_text(line)
# Scroll the tty's own echo of the typed line out of the composer region, the way a harness
# redraws after accepting a message. Without this the caller cannot tell "submitted" from
# "still sitting in the composer" — which is the distinction hive-answer checks.
sys.stdout.write("\\n" * 60 + "received\\n")
sys.stdout.flush()
time.sleep(600)
'''


def fake_harness_window(dep, session: str, task: str = TASK) -> Path:
    """Open a real tmux window running something that is NOT a shell and DOES consume a line.

    `python3` rather than a shell script: `#{pane_current_command}` reports the interpreter,
    so a bash one-liner would be indistinguishable from the abandoned-to-a-shell pane that
    hive-answer must refuse — and the test would then assert the opposite of the contract.
    """
    script = dep["tmp"] / "fake-harness.py"
    write(script, FAKE_HARNESS)
    received = dep["tmp"] / f"received-{task}.txt"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-n", task,
         "python3", str(script), str(received)],
        check=True, env={**os.environ, **dep["tmux_env"], "TMUX": "", "TMUX_PANE": ""},
    )
    return received


def emit_as(dep, role: str, actor: str, etype: str, *, task=None, payload=None) -> None:
    """One raw event, for the board states this section needs to stand a task in."""
    env = dict(os.environ)
    env.update({"OMEGAHIVE_DATABASE_URL": dep["db"], "OMEGAHIVE_GATEWAY_DATABASE_URL": ""})
    cmd = [*_omegahive_cmd(), "emit", "--run-id", dep["run_id"], "--role", role,
           "--actor", actor, "--type", etype, "--payload", json.dumps(payload or {})]
    if task:
        cmd += ["--task", task]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def tmux_kill(session: str, dep) -> None:
    subprocess.run(["tmux", "kill-session", "-t", f"={session}"],
                   env={**os.environ, **dep["tmux_env"], "TMUX": "", "TMUX_PANE": ""},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_answer(dep, *args, timeout=300, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(ANSWER), TASK, *args], capture_output=True, text=True,
                          env=env or shell_env(dep), cwd=str(REPO), timeout=timeout)


def block_the_worker(dep) -> None:
    """Put the task in the state an answer is FOR: owned, accepted, and blocked."""
    seed_board(dep)
    worker_script(dep, f'''
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1
"$EMIT" --type task.blocked --task {TASK} \\
  --payload '{{"reason": "needs a decision", "needs": "decision"}}' >/dev/null 2>&1
''')
    assert run_turn(dep).returncode == 0
    assert finished(dep)["classification"] == "blocked"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is the transport under test")
def test_the_answer_lands_and_the_nudge_reaches_a_live_harness(deployment):
    block_the_worker(deployment)
    session = deployment["tmux_session"]
    received = fake_harness_window(deployment, session)

    proc = run_answer(deployment, "use event time")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "appended to" in proc.stdout
    assert f"nudged {session}:{TASK}" in proc.stdout, proc.stdout

    # The line the WORKER received, read from the process that consumed it rather than from
    # the screen: what matters is that it was submitted, not that it was drawn.
    got = received.read_text()
    assert "an answer landed in commit" in got
    assert f"{RELATIVE_BRIDGE} sync workspace" in got, (
        "the nudge must name the stable relative token, never a reconstructed absolute path"
    )
    assert "task.unblocked ONLY if" in got
    assert "\n" not in got.strip(), "one line: a newline mid-nudge submits half a sentence"

    # And the answer is on the hub whatever the window did.
    order = (deployment["ops_ws"] / ORDER).read_text()
    assert "## Answers" in order and "use event time" in order


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is the transport under test")
def test_resume_only_appends_no_answer_and_asks_for_no_unblock(deployment):
    block_the_worker(deployment)
    before = (deployment["ops_ws"] / ORDER).read_text()
    received = fake_harness_window(deployment, deployment["tmux_session"])

    proc = run_answer(deployment, "--resume-only", "five hour window reset")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    got = received.read_text()
    assert "five hour window reset" in got, "the operator's reason is what explains the wake"
    assert "do NOT emit task.unblocked" in got
    assert f"{RELATIVE_BRIDGE} sync workspace" in got
    assert (deployment["ops_ws"] / ORDER).read_text() == before, (
        "--resume-only appends nothing; the order is untouched"
    )


def test_resume_only_without_a_reason_refuses(deployment):
    block_the_worker(deployment)
    proc = run_answer(deployment, "--resume-only")
    assert proc.returncode != 0
    assert "needs a reason" in proc.stderr


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is the transport under test")
def test_a_dead_pane_is_refused_rather_than_typed_at(deployment):
    """`remain-on-exit` keeps a window after its session exits, so a window's PRESENCE no
    longer proves a live process — and a dead pane swallows send-keys silently."""
    block_the_worker(deployment)
    session = deployment["tmux_session"]
    env = {**os.environ, **deployment["tmux_env"], "TMUX": "", "TMUX_PANE": ""}
    # The window is opened on a LONG-LIVED command and killed afterwards, in that order. Open
    # it on something that exits immediately and the window is already gone when the next
    # command runs — the session dies with its last window — so this raced its own setup.
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", TASK, "sleep", "600"],
                   check=True, env=env)
    # `-w`, exactly as `tmux_keep_window` in hive-common.sh does it: remain-on-exit is a
    # WINDOW option, and older tmux refuses to infer that from the target.
    subprocess.run(["tmux", "set-option", "-w", "-t", f"={session}:={TASK}",
                    "remain-on-exit", "on"], check=True, env=env)
    subprocess.run(["tmux", "respawn-pane", "-k", "-t", f"={session}:={TASK}", "true"],
                   check=True, env=env)
    for _ in range(20):
        dead = subprocess.run(["tmux", "display-message", "-p", "-t", f"={session}:={TASK}",
                               "#{pane_dead}"], capture_output=True, text=True, env=env)
        if dead.stdout.strip() == "1":
            break
        time.sleep(0.5)
    else:
        pytest.fail("the pane never reached pane_dead=1; the fixture, not the tool, is wrong")

    proc = run_answer(deployment, "yes")
    assert proc.returncode != 0
    assert "pane is dead" in proc.stderr, proc.stderr
    assert "committed and pushed" in proc.stderr, "a failed nudge must say the answer is safe"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is the transport under test")
def test_a_window_left_at_a_shell_is_refused_rather_than_executed(deployment):
    """The dangerous case: the harness exited and left the pane at a prompt. Typing an answer
    there hands operator prose to a shell as a command line."""
    block_the_worker(deployment)
    session = deployment["tmux_session"]
    env = {**os.environ, **deployment["tmux_env"], "TMUX": "", "TMUX_PANE": ""}
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", TASK, "sh"],
                   check=True, env=env)
    time.sleep(1)

    proc = run_answer(deployment, "yes")
    assert proc.returncode != 0
    assert "not a harness" in proc.stderr, proc.stderr


def test_a_nudge_refuses_when_the_session_is_gone(deployment):
    block_the_worker(deployment)
    tmux_kill(deployment["tmux_session"], deployment)
    proc = run_answer(deployment, "yes")
    assert proc.returncode != 0
    assert "no tmux session named" in proc.stderr
    assert "Dead worker recovery" in proc.stderr, "the refusal must name the recovery path"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is the transport under test")
def test_a_nudge_refuses_when_the_task_has_no_window_of_its_own(deployment):
    block_the_worker(deployment)
    env = {**os.environ, **deployment["tmux_env"], "TMUX": "", "TMUX_PANE": ""}
    subprocess.run(["tmux", "new-session", "-d", "-s", deployment["tmux_session"],
                    "-n", "someone-else", "sleep", "600"],
                   check=True, env=env)
    proc = run_answer(deployment, "yes")
    assert proc.returncode != 0
    assert f"has no window named '{TASK}'" in proc.stderr, proc.stderr


def test_a_nudge_refuses_from_a_terminal_board_state(deployment):
    """Review and close are the operator's acts; waking a worker into a finished task would
    produce work nobody asked for."""
    seed_board(deployment)
    emit_as(deployment, "worker", deployment["worker"], "task.accepted", task=TASK)
    emit_as(deployment, "worker", deployment["worker"], "task.result_posted", task=TASK,
             payload={"artifact_refs": [{"ref": f"{ORDER}@{'0' * 40}", "quality": "ok"}]})
    proc = run_answer(deployment, "yes")
    assert proc.returncode != 0
    assert "in_review" in proc.stderr


def test_a_nudge_refuses_an_unowned_task_rather_than_guessing_a_worker(deployment):
    emit_as(deployment, "human", "operator", "task.created", task=TASK,
             payload={"title": "t", "task_type": "task", "acceptance": "a"})
    proc = run_answer(deployment, "yes")
    assert proc.returncode != 0
    assert "no owner" in proc.stderr or "does not exist" in proc.stderr
# =====================================================================================
# 4. Worker function — the whole WORKER.md finish sequence, inside one turn
# =====================================================================================

def install_fake_tools(dep, *, ci_sequence: list[str], review_output: str) -> Path:
    """A fake `gh` and a fake reviewer on the worker's PATH.

    `gh pr checks` returns the next entry of `ci_sequence` each time it is called, so a
    test can drive red -> repair -> green through the real waiting loop the worker
    protocol asks for. Nothing here reaches a network.
    """
    bin_dir = dep["tmp"] / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    state = dep["tmp"] / "ci-state"
    state.write_text("0")
    (bin_dir / "gh").write_text(f"""#!/usr/bin/env bash
set -euo pipefail
SEQ=({" ".join(ci_sequence)})
case "$1 ${{2:-}}" in
  "pr checks")
    i=$(cat "{state}")
    printf '%s\\n' "${{SEQ[$i]}}"
    next=$((i + 1)); [ "$next" -lt "${{#SEQ[@]}}" ] || next=$((${{#SEQ[@]}} - 1))
    printf '%s' "$next" > "{state}"
    [ "${{SEQ[$i]}}" = "green" ] || exit 1
    ;;
  "pr view") echo "https://forge.invalid/pr/1" ;;
  "pr create") echo "https://forge.invalid/pr/1" ;;
  *) echo "fake gh: unhandled $*" >&2; exit 64 ;;
esac
""")
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "critic").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' " + json.dumps(review_output) + "\n")
    (bin_dir / "critic").chmod(0o755)
    set_child_env(dep, PATH=f"{bin_dir}:{dep['env']['PATH']}")
    return bin_dir


WORKER_FINISH = f'''
block() {{
  say "$("$EMIT" --type task.blocked --task {TASK} \\
    --payload "$(printf '{{"reason": "%s", "needs": "decision"}}' "$1")" 2>&1)"
}}

say "$("$EMIT" --type task.accepted --task {TASK} 2>&1)"

# 1. local verification + the authored independent review, BEFORE the PR exists.
printf 'change\\n' > "$CODE/CHANGE.md"
git -C "$CODE" -c user.name=w -c user.email=w@x add -A
git -C "$CODE" -c user.name=w -c user.email=w@x commit --quiet -m "the change"
REVIEW=$(critic)
say "REVIEW:$REVIEW"
[ -n "$REVIEW" ] || {{ say "REVIEW-EMPTY"; exit 1; }}

# 2. publish, through the direct commands and nothing else.
say "$("$HIVE" publish code 2>&1)"

# 3. the first CI run, and one repair inside the order's budget.
ATTEMPTS=0
until gh pr checks 1 >/dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS + 1))
  say "CI-RED-$ATTEMPTS"
  if [ "$ATTEMPTS" -gt 2 ]; then
    say "CI-BUDGET-EXHAUSTED"
    block "CI red after the authored repair budget"
    exit 1
  fi
  printf 'fix %s\\n' "$ATTEMPTS" >> "$CODE/CHANGE.md"
  git -C "$CODE" -c user.name=w -c user.email=w@x commit --quiet -am "repair $ATTEMPTS"
  "$HIVE" publish code >/dev/null 2>&1
done
say "CI-GREEN"

# 4. main moved: integrate once and re-verify.
git -C "$CODE" fetch --quiet origin main
if ! git -C "$CODE" merge-base --is-ancestor origin/main HEAD; then
  say "MAIN-MOVED"
  if ! git -C "$CODE" -c user.name=w -c user.email=w@x rebase origin/main >/dev/null 2>&1; then
    say "MAIN-CONFLICT"
    git -C "$CODE" rebase --abort >/dev/null 2>&1 || true
    block "main moved and the integration conflicts non-trivially"
    exit 1
  fi
  "$HIVE" publish code >/dev/null 2>&1
  gh pr checks 1 >/dev/null 2>&1 && say "CI-GREEN-AFTER-INTEGRATION"
fi

# 5. only now: the result.
mkdir -p "$WS/projects/{PROJECT}/reports"
printf '# result\\n' > "$WS/projects/{PROJECT}/reports/2026-08-21-{TASK}-result.md"
git -C "$WS" -c user.name=w -c user.email=w@x add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "report"
say "$("$HIVE" publish workspace 2>&1)"
SHA=$(git -C "$WS" rev-parse HEAD)
REF="projects/{PROJECT}/reports/2026-08-21-{TASK}-result.md@$SHA"
PAYLOAD=$(printf '{{"artifact_refs": [{{"ref": "%s", "quality": "ok"}}]}}' "$REF")
say "$("$EMIT" --type task.result_posted --task {TASK} --payload "$PAYLOAD" 2>&1)"
'''


def test_a_worker_does_the_whole_finish_sequence_and_exits_posted(deployment):
    """Review before the PR, publication through the direct commands, red -> repair ->
    green CI, one moved-main integration, then the result. The turn must classify
    `posted` — and every network-shaped operation here runs in the worker's own process,
    with no mediator anywhere."""
    seed_board(deployment)
    worker_script(deployment, WORKER_FINISH)
    install_fake_tools(deployment, ci_sequence=["red", "green", "green"],
                       review_output="finding: the loop is off by one")

    # main moves under the worker, once, between its first push and its integration check.
    moved = deployment["tmp"] / "moved"
    git(deployment["tmp"], "clone", "--quiet", str(deployment["code_remote"]), str(moved))
    write(moved / "OTHER.md", "someone else's commit\n")
    git(moved, "add", "-A")
    git(moved, "commit", "--quiet", "-m", "main moved")
    git(moved, "push", "--quiet", "origin", "main")

    proc = run_turn(deployment)
    text = out(deployment)
    assert "emitted · task.accepted" in text, text
    assert "REVIEW:finding" in text, "a non-empty external critic really ran"
    assert "CI-RED-1" in text and "CI-GREEN" in text, text
    assert "MAIN-MOVED" in text and "CI-GREEN-AFTER-INTEGRATION" in text, text
    assert "emitted · task.result_posted" in text, text
    assert proc.returncode == 0, proc.stdout + proc.stderr

    payload = finished(deployment)
    assert payload["classification"] == "posted"

    # The publications really landed on the real remotes.
    branch = subprocess.run(
        ["git", "-C", str(deployment["code_remote"]), "rev-parse", CODE_BRANCH],
        capture_output=True, text=True)
    assert branch.returncode == 0, "the worker's branch is on the code remote"
    report = subprocess.run(
        ["git", "-C", str(deployment["hub"]), "show",
         f"main:projects/{PROJECT}/reports/2026-08-21-{TASK}-result.md"],
        capture_output=True, text=True)
    assert report.returncode == 0, "the report is on the hub"


def test_a_worker_that_exhausts_its_ci_repair_budget_blocks_rather_than_calling_it_green(
        deployment):
    """The budget is a BLOCK, not a failure to hide and not a licence to keep looping.
    The fixture worker therefore emits `task.blocked` on the way out — an earlier version
    of this test appended that emit after a script path that had already exited, so the
    code it claimed to exercise was unreachable and the assertion quietly checked
    `failed` instead."""
    seed_board(deployment)
    worker_script(deployment, WORKER_FINISH)
    install_fake_tools(deployment, ci_sequence=["red", "red", "red", "red"],
                       review_output="finding: something")
    proc = run_turn(deployment)
    text = out(deployment)
    assert "CI-BUDGET-EXHAUSTED" in text, text
    assert "emitted · task.blocked" in text, text
    assert proc.returncode != 0, "the worker's own script failed; the process view says so"
    payload = finished(deployment)
    assert payload["classification"] == "blocked", (
        "the spine owns the task disposition, and the worker said blocked"
    )
    assert payload["outcome"] == "failure", "the process view is separate and unchanged"
    branch = subprocess.run(
        ["git", "-C", str(deployment["code_remote"]), "rev-parse", CODE_BRANCH],
        capture_output=True, text=True)
    assert branch.returncode == 0, "the work is preserved on the remote, not thrown away"


def test_a_non_trivial_main_conflict_blocks_instead_of_guessing_a_resolution(deployment):
    """The other half of step 5. `main` moved and its change collides with the worker's
    own; a rebase cannot resolve it, and a worker that forced one would be inventing an
    integration nobody reviewed."""
    seed_board(deployment)
    worker_script(deployment, WORKER_FINISH)
    install_fake_tools(deployment, ci_sequence=["green", "green", "green"],
                       review_output="finding: something")

    # A commit on main touching the SAME file the worker's change creates, with different
    # content — the shape a rebase cannot resolve for you.
    moved = deployment["tmp"] / "moved"
    git(deployment["tmp"], "clone", "--quiet", str(deployment["code_remote"]), str(moved))
    write(moved / "CHANGE.md", "somebody else's incompatible line\n")
    git(moved, "add", "-A")
    git(moved, "commit", "--quiet", "-m", "main moved, incompatibly")
    git(moved, "push", "--quiet", "origin", "main")

    proc = run_turn(deployment)
    text = out(deployment)
    assert "MAIN-MOVED" in text, text
    assert "MAIN-CONFLICT" in text, text
    assert "emitted · task.blocked" in text, text
    assert "task.result_posted" not in text, (
        "an uncertain integration must not be published as a result"
    )
    assert proc.returncode != 0
    assert finished(deployment)["classification"] == "blocked"


def test_a_worker_that_cannot_publish_fails_loudly_rather_than_being_bridged(deployment):
    """The doctrine's consequence, made concrete. Nothing widens a runner on its behalf
    and nothing crosses the boundary for it; the failure is the operator's signal."""
    seed_board(deployment)
    worker_script(deployment, '''
say "$("$HIVE" publish code 2>&1 || echo "exit=$?")"
''')
    subprocess.run(["git", "-C", str(deployment["code_root"]), "remote", "set-url",
                    "origin", "/nonexistent/remote.git"], check=True, capture_output=True)
    assert run_turn(deployment).returncode == 0
    assert "refused" in out(deployment) or "exit=" in out(deployment), out(deployment)


# =====================================================================================
# 5. Deletion
# =====================================================================================

RETIRED = ("hive-supervise", "worker_io", "HIVE_EXEC_ROOT", "HIVE_SPOOL_TIMEOUT",
           "emit-relay", "hive-worker")

# The LAUNCH PATH: everything a worker's turn actually travels through. If one of the
# retired names is live in any of these, some part of the product came back.
LAUNCH_PATH = (
    "scripts/hive-launch", "scripts/hive-answer", "scripts/hive-common.sh",
    "scripts/hive-routes",
    "schemas/route-catalog.example.json",
    "src/omegahive/harness/adapters.py", "src/omegahive/harness/plan.py",
    "src/omegahive/harness/turns.py", "src/omegahive/harness/__init__.py",
)

# The two deliberate exceptions, and why each is not a survival of the product:
#
#   records.py   NAMES the retired fields in order to REFUSE a catalog still carrying
#                them, by name, with the remedy. Removing the name would turn a legible
#                refusal into a field-level parser complaint.
#   migrate.py   NAMES them in order to REMOVE them. A migration that could not say what
#                it strips would not be a migration.
#
# Historical evidence — `docs/`, `taskbench/`, past reports — is excluded entirely and on
# purpose: a report that described the supervisor is still a true report about what
# happened, and rewriting it would be the dishonesty, not the reference.
DELIBERATE_EXCEPTIONS = (
    "src/omegahive/harness/records.py",
    "src/omegahive/harness/migrate.py",
    # The drill NAMES them to build a pre-cutover catalog and prove `hive-routes migrate`
    # removes them. A test harness may name what it verifies; that is the difference
    # between a reference and a survival.
    "scripts/hive-tooling-drill.sh",
)


def _code_lines(path: Path):
    """Lines that are not shell comments. A comment explaining what was removed is a
    reason to keep reading, not a reason to fail a build."""
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        yield line_no, line


def test_no_launch_path_still_references_the_retired_product():
    offenders = []
    for rel in LAUNCH_PATH:
        path = REPO / rel
        assert path.exists(), f"the deletion check names a path that is gone: {rel}"
        for line_no, line in _code_lines(path):
            for token in RETIRED:
                if token in line:
                    offenders.append(f"{rel}:{line_no}: {line.strip()[:100]}")
    assert not offenders, "retired product still referenced:\n" + "\n".join(offenders)


def test_the_committed_schema_offers_no_retired_field_to_a_hand_author():
    """The v2 schema is checked STRUCTURALLY rather than by string, because it is
    generated from the model — including the docstring that explains what was removed.
    What must not come back is a settable property, and that is what this asserts."""
    schema = json.loads((REPO / "schemas" / "route-catalog.v2.json").read_text())
    runner_props = schema["$defs"]["RunnerSpec"]["properties"]
    assert "worker_io" not in runner_props
    assert set(runner_props) == {
        "executable",
        "args",
        "inherit_env",
        # The environment pair, added 2026-08-28 so a provider endpoint and a credential
        # rename are route facts rather than things the operator's shell had better be
        # holding. `executable` and `args` are still the whole command.
        "inherit_env_as",
        "env",
    }
    assert schema["$defs"]["RunnerSpec"]["additionalProperties"] is False, (
        "a catalog carrying a retired field must refuse by name, not be quietly trimmed"
    )


def test_the_two_deliberate_exceptions_are_the_only_ones_and_are_still_doing_their_job():
    """A repository search finds the retired names in exactly two more modules, and both
    are there to refuse or remove them. This asserts that, so the exception list cannot
    quietly become a place things hide."""
    for rel in DELIBERATE_EXCEPTIONS:
        text = (REPO / rel).read_text()
        assert "worker_io" in text
        assert "retired" in text.lower() or "cutover" in text.lower()
    # And the drill's exception is only earned while it still exercises the migration.
    drill = (REPO / "scripts" / "hive-tooling-drill.sh").read_text()
    assert "hive-routes\" migrate" in drill or "hive-routes migrate" in drill
    assert "dropped runner.worker_io" in drill


def test_the_supervisor_and_its_spool_are_gone_from_the_tree():
    assert not (REPO / "scripts" / "hive-supervise").exists()
    assert not (REPO / "src" / "omegahive" / "harness" / "spool.py").exists()


# =====================================================================================
# 6. The worker-facing command contract — one stable token per operation, every task
# =====================================================================================
#
# The interface FILES are task-specific and absolute (`<task-root>/run/emit`,
# `<task-root>/run/hive`); what the prompts ISSUE is `../run/emit` and `../run/hive`.
# Both clones are siblings of `run/` and a turn starts in a clone root, so those two
# tokens resolve from either clone and never change between tasks — which is what lets
# one operator-approved runner rule cover every launch. Naming the absolute file instead
# forced a rule per task root, and the next task fell straight out of it: `capacity-view`
# could not publish on 2026-08-21.
#
# Every test below reads the prompt the harness would actually receive (`argv[-1]` of the
# resolved turn), never the source of the script that built it, and the interface is then
# EXECUTED from both clone roots — a string assertion alone would not notice a cwd that
# stopped being a clone root.

RELATIVE_EMIT = "../run/emit"
RELATIVE_BRIDGE = "../run/hive"


def no_absolute_worker_command(prompt: str, task_root) -> None:
    """Fail if the prompt asks the worker to EXECUTE a task-specific absolute path.

    Deliberately not "the task root never appears": the informational header still names
    the task root and both clones, and should. What must not appear is that root followed
    by `/run/emit` or `/run/hive` in command position — the exact shape a runner rule
    cannot generalize over.
    """
    offenders = [
        line for line in prompt.splitlines()
        if re.search(rf"{re.escape(str(task_root))}/run/(emit|hive)\b", line)
    ]
    assert not offenders, (
        "the prompt issues a task-specific absolute worker command:\n" + "\n".join(offenders)
    )


def stub_tmux(dep) -> Path:
    """A tmux stand-in for the launch path, so these tests read a prompt rather than race
    a pane.

    A real `tmux new-window` here would start a SECOND turn runner on the very turn
    directory the test wants to read, and its evidence would be indistinguishable from a
    deliberate one. Section 3b above owns real windows and asserts what they run; nothing
    here is about the pane. The stub also means the command contract is guarded on a host
    with no tmux installed, which is where a regression would otherwise slip through.
    """
    bin_dir = dep["tmp"] / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    # `publish code` ends in `gh`, so the same directory carries a forge stub. It is the
    # smallest one that answers what the bridge asks; section 4's richer fake drives CI
    # sequences these tests have no use for.
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'case "$1 ${2:-}" in\n'
        '  "pr view") exit 1 ;;\n'
        '  "pr create") echo "https://forge.invalid/pr/1" ;;\n'
        '  *) echo "fake gh: unhandled $*" >&2; exit 64 ;;\n'
        "esac\n"
    )
    gh.chmod(0o755)
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{dep["tmp"]}/tmux-calls.txt"\n'
        'case "${1:-}" in\n'
        "  has-session) exit 1 ;;\n"          # no session yet -> the new-session branch
        "  new-session|new-window) echo 'stub:1' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bin_dir


def launch_a_fresh_task(dep, task: str) -> dict:
    """Run the REAL `hive-launch` end to end on a second task in this deployment.

    Second task, because the fixture's own task root already exists and a launch clones
    into a fresh one. Everything else is the shipped path: the order is committed and
    pushed to the hub, the route resolves from the catalog, the clones are made, the
    interface is issued by `hive-common.sh`, and turn 001 is written — which is where the
    prompt the harness receives can be read.
    """
    worker = f"sess-{task}-0821"
    order_rel = f"projects/{PROJECT}/orders/2026-08-21-{task}.md"
    write(dep["ops_ws"] / order_rel, f"# Order: {task}\n\nA fixture order.\n")
    git(dep["ops_ws"], "add", "-A")
    git(dep["ops_ws"], "commit", "--quiet", "-m", f"order: {task}")
    git(dep["ops_ws"], "push", "--quiet", "origin", "HEAD:main")

    bin_dir = stub_tmux(dep)
    env = shell_env(
        dep,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        # The canonical checkout this host would clone from, and a compose command that is
        # never run: `HIVE_CLI_CMD` reaches the CLI directly, but `resolve_compose` still
        # insists on naming one, and a CI host need not have a container runtime at all.
        CANON_CODE=str(dep["tmp"] / "code-seed"),
        OMEGAHIVE_COMPOSE="compose-never-invoked-in-this-test",
    )
    proc = subprocess.run([str(LAUNCH), order_rel, "--worker", worker],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    task_root = dep["work_root"] / worker
    # Both facts come from what the launch ACTUALLY DID, never reconstructed here. The prompt
    # is the kickoff file the launcher externalizes on every launch, and the working directory
    # is the one it handed tmux (`-c <dir>`), captured by the stub. A test that rebuilt those
    # paths itself would stay green if `hive-launch` started opening the window somewhere
    # else, while every real worker's `../run/…` broke.
    prompt = (task_root / "kickoff.txt").read_text()
    calls = (dep["tmp"] / "tmux-calls.txt").read_text().splitlines()
    opened = [c for c in calls if c.startswith(("new-session", "new-window"))]
    assert len(opened) == 1, f"expected exactly one window to be opened, got: {opened}"
    m = re.search(r"-c (\S+)", opened[0])
    assert m, opened[0]
    cwd = Path(m.group(1))
    return {
        "task": task, "worker": worker, "task_root": task_root,
        "cwd": cwd, "code_root": task_root / PROJECT,
        "prompt": prompt, "env": env,
    }


def worker_env(dep, launched) -> dict:
    """The environment a worker's own commands run in: the governed CLI it reaches, a git
    identity, and nothing ambient."""
    return {
        "PATH": launched["env"]["PATH"],
        "HOME": str(dep["tmp"] / "clean-home"),
        "HIVE_CLI_CMD": dep["env"]["HIVE_CLI_CMD"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "drill worker", "GIT_AUTHOR_EMAIL": "w@example.invalid",
        "GIT_COMMITTER_NAME": "drill worker", "GIT_COMMITTER_EMAIL": "w@example.invalid",
    }


def test_the_launch_kickoff_issues_only_stable_relative_worker_commands(deployment):
    launched = launch_a_fresh_task(deployment, "path-contract")
    prompt = launched["prompt"]

    assert f"First: {RELATIVE_BRIDGE} sync workspace" in prompt.splitlines(), (
        "the very first instruction must be the stable token, on its own line"
    )
    assert f"{RELATIVE_EMIT} --type <t> --task <task> --payload <json>" in prompt
    assert f"{RELATIVE_BRIDGE} publish workspace" in prompt
    assert f"{RELATIVE_BRIDGE} publish code" in prompt
    # The protocol explanation further down names the emit command a second time; it must
    # carry the same token, not the absolute file.
    assert f"run '{RELATIVE_EMIT} ...'" in prompt

    no_absolute_worker_command(prompt, launched["task_root"])
    # The header is still informative: a worker is told where it lives, just not asked to
    # type it. Dropping this would let an empty prompt pass the assertion above.
    assert str(launched["task_root"]) in prompt
    assert str(launched["cwd"]) in prompt and str(launched["code_root"]) in prompt

    # The PREMISE of every relative token above, asserted rather than assumed: the turn the
    # harness will actually run starts in a clone root, and both clone roots are siblings
    # of the interface directory. A launch that recorded `<task-root>` as the cwd would
    # leave every assertion above true and every `../run/…` broken.
    assert launched["cwd"] == launched["task_root"] / "hive"
    assert launched["code_root"] == launched["task_root"] / PROJECT
    for clone in (launched["cwd"], launched["code_root"]):
        assert (clone / RELATIVE_EMIT).resolve() == launched["task_root"] / "run" / "emit"
        assert (clone / RELATIVE_BRIDGE).resolve() == launched["task_root"] / "run" / "hive"


def test_the_issued_interface_resolves_from_the_workspace_clone_root(deployment):
    """The tokens are executed, not merely read. Everything here runs with the clone root
    as cwd, which is where the launch starts a turn."""
    launched = launch_a_fresh_task(deployment, "path-exec-ws")
    env = worker_env(deployment, launched)
    ws = launched["cwd"]          # the turn's OWN recorded cwd, not a reconstructed path

    synced = subprocess.run([RELATIVE_BRIDGE, "sync", "workspace"], cwd=str(ws),
                            capture_output=True, text=True, env=env, timeout=120)
    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert "workspace synced to" in synced.stdout

    emitted = subprocess.run(
        [RELATIVE_EMIT, "--type", "task.accepted", "--task", launched["task"]],
        cwd=str(ws), capture_output=True, text=True, env=env, timeout=300)
    assert emitted.returncode == 0, emitted.stdout + emitted.stderr
    assert "task.accepted" in emitted.stdout

    report = f"projects/{PROJECT}/reports/2026-08-21-{launched['task']}-result.md"
    write(ws / report, "# result\n")
    git(ws, "add", "-A")
    git(ws, "commit", "--quiet", "-m", "report")
    published = subprocess.run([RELATIVE_BRIDGE, "publish", "workspace"], cwd=str(ws),
                               capture_output=True, text=True, env=env, timeout=120)
    assert published.returncode == 0, published.stdout + published.stderr
    hub = subprocess.run(["git", "-C", str(deployment["hub"]), "show", f"main:{report}"],
                         capture_output=True, text=True)
    assert hub.returncode == 0 and "# result" in hub.stdout


def test_the_issued_interface_resolves_from_the_code_clone_root(deployment):
    """`publish code` is typed from the CODE clone — the sibling, not the workspace — and
    that is the cwd a mismatch would break first."""
    launched = launch_a_fresh_task(deployment, "path-exec-code")
    env = worker_env(deployment, launched)
    code = launched["code_root"]

    write(code / "CHANGE.md", "change\n")
    git(code, "add", "-A")
    git(code, "commit", "--quiet", "-m", "the change")
    published = subprocess.run([RELATIVE_BRIDGE, "publish", "code"], cwd=str(code),
                               capture_output=True, text=True, env=env, timeout=120)
    assert published.returncode == 0, published.stdout + published.stderr
    remote = subprocess.run(
        ["git", "-C", str(deployment["code_remote"]), "show",
         f"worker/{launched['task']}:CHANGE.md"], capture_output=True, text=True)
    assert remote.returncode == 0 and "change" in remote.stdout
    # And the sync token resolves from here too, so neither clone is the privileged one.
    synced = subprocess.run([RELATIVE_BRIDGE, "sync", "workspace"], cwd=str(code),
                            capture_output=True, text=True, env=env, timeout=120)
    assert synced.returncode == 0, synced.stdout + synced.stderr

