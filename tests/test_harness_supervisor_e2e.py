"""End-to-end: the REAL supervisor runs the fake adapter and records what happened.

The lifecycle proof — route -> start -> finish ordering, task attribution, and honest
usage — obtained with no paid model call, no network, and no vendor CLI installed. It
drives `scripts/hive-supervise` as a subprocess, exactly as a tmux pane would, against
this run's scratch database.

What makes it an end-to-end test rather than a mock: the plan comes from the real
`harness-resolve`, the argv is executed by the real supervisor, the events go through the
real gateway and legality layer, and the usage is read by the real extractor. The only
fixture is the harness itself, which is the one thing that would otherwise cost money.

The supervised-worker transport — spooled emits, workspace sync, publication — has its
own end-to-end file, `test_harness_transport_e2e.py`.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest

import scratch_db
from harness_fixtures import catalog, route, runner

REPO = Path(__file__).resolve().parent.parent
SUPERVISE = REPO / "scripts" / "hive-supervise"

ORDER_REF = "projects/omegahive/orders/2026-08-13-e2e-task.md@" + "a" * 40
KICKOFF = "You are hive worker e2e.\nSecond line; with $shell `metacharacters`."

PRICE = {
    "currency": "USD",
    "per_mtok_input": 1.0,
    "per_mtok_cache_read": 0.1,
    "per_mtok_cache_write": 1.25,
    "per_mtok_output": 5.0,
    "source": "fixture prices",
    "captured_at": "2026-08-13",
}


def _db_url() -> str:
    url = os.environ.get(scratch_db.BASE_URL_ENV)
    if not url:
        pytest.skip("no scratch database published for this run")
    return url


def _omegahive_cmd() -> list[str]:
    """Invoke the CLI in this checkout, not in a container.

    The supervisor's HIVE_CLI_CMD seam exists for exactly this: the shipped path runs
    the containerized `cli` service, and a test that required a running container would
    be an integration test of podman rather than of this code.
    """
    return ["uv", "run", "--project", str(REPO), "omegahive"]


def resolve_plan(tmp_path, *, task="e2e-task", route_name=None, extra_env=None,
                 routes=None, task_root=None, run_dir=None):
    """Run the REAL resolver on a v2 catalog, exactly as `hive-launch` does."""
    task_root = task_root or tmp_path
    usage_file = tmp_path / "fake-usage.jsonl"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "HIVE_FAKE_USAGE_FILE": str(usage_file),
        # A credential that must NOT survive into the child's environment.
        "ANTHROPIC_API_KEY": "sk-must-not-propagate",
    }
    env.update(extra_env or {})

    entries = routes or [route(price_basis=PRICE)]
    catalog_bytes = json.dumps(catalog(*entries)).encode()
    request = {
        "catalog_b64": base64.b64encode(catalog_bytes).decode(),
        "route": route_name,
        "task": task,
        "order_ref": ORDER_REF,
        "purpose": "work",
        "attempt": 1,
        "kickoff": KICKOFF,
        "task_root": str(task_root),
        "cwd": str(task_root),
        "code_root": str(task_root / "code") if hasattr(task_root, "__truediv__") else "",
        "run_dir": str(run_dir) if run_dir else "",
        "session_id": str(uuid.uuid4()),
        "env": env,
    }
    proc = subprocess.run(
        [*_omegahive_cmd(), "harness-resolve"],
        input=json.dumps(request), capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout), usage_file


@pytest.fixture
def rig(tmp_path):
    """A supervisor run-dir with a real plan.json and a working instrument wrapper."""
    db = _db_url()
    run_id = "e2e-" + uuid.uuid4().hex[:8]
    plan, usage_file = resolve_plan(tmp_path)

    # The supervisor's state lives OUTSIDE the worker's task root, which here is
    # tmp_path. The launcher puts it under $HIVE_EXEC_ROOT for the same reason.
    run_dir = tmp_path.parent / (tmp_path.name + "-exec")
    run_dir.mkdir()
    plan["run_id"] = run_id
    plan["worker"] = "e2e"
    (run_dir / "plan.json").write_text(json.dumps(plan))

    wrapper = run_dir / "emit.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'cd "{REPO}"\n'
        f'exec {" ".join(_omegahive_cmd())} emit --run-id "{run_id}" '
        '--role instrument --actor "supervisor-e2e" "$@"\n'
    )
    wrapper.chmod(0o755)

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "plan": plan,
        "db": db,
        "usage_file": usage_file,
        "tmp": tmp_path,
    }


def _run_supervisor(rig, behaviour: str) -> subprocess.CompletedProcess:
    # The child's environment is fixed in plan.json at RESOLVE time, by the adapter's
    # allowlist — the supervisor never merges its own environment into the child's.
    # That is the property under test elsewhere in this file, so the fixture behaviour
    # is set where a real deployment variable would be: in the resolved plan.
    plan_path = rig["run_dir"] / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["env"]["HIVE_FAKE_BEHAVIOUR"] = behaviour
    plan_path.write_text(json.dumps(plan))

    env = dict(os.environ)
    env.update(
        {
            "OMEGAHIVE_DATABASE_URL": rig["db"],
            "OMEGAHIVE_GATEWAY_DATABASE_URL": "",
            "HIVE_CLI_CMD": " ".join(_omegahive_cmd()),
        }
    )
    return subprocess.run(
        [str(SUPERVISE), str(rig["run_dir"])],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300,
    )


def _events(rig) -> list[tuple[str, str, str, dict]]:
    """(event_type, actor_role, task_id, payload) in seq order, committed rows only."""
    with psycopg.connect(rig["db"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, actor_role, task_id, payload FROM events "
            "WHERE run_id = %s ORDER BY seq",
            (rig["run_id"],),
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def _approve_route(rig) -> None:
    """Emit `execution.route_approved` the way hive-launch does: role human, operator.

    Reproduced here rather than invoked through hive-launch because the launcher also
    clones repositories and opens tmux windows; what this test needs from it is the one
    fact and its authorship.
    """
    plan = rig["plan"]
    payload = {
        k: plan[k]
        for k in (
            "execution_id", "purpose", "attempt", "order_ref", "catalog_digest",
            "identity", "route_source", "runner_fingerprint", "worker_io",
            "model_identity_evidence", "usage_evidence", "price_basis",
        )
    }
    env = dict(os.environ)
    env.update({"OMEGAHIVE_DATABASE_URL": rig["db"], "OMEGAHIVE_GATEWAY_DATABASE_URL": ""})
    proc = subprocess.run(
        [
            *_omegahive_cmd(), "emit", "--run-id", rig["run_id"],
            "--role", "human", "--actor", "operator",
            "--type", "execution.route_approved", "--task", "e2e-task",
            "--payload", json.dumps(payload),
        ],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the lifecycle --------------------------------------------------------------------

def test_route_then_start_then_finish_in_order(rig):
    """The ordering claim, checked on the spine rather than on stdout.

    Route approval is a HUMAN act and precedes everything; start and finish are
    INSTRUMENT observations. The three roles in the recorded order are the authority
    model made visible.
    """
    _approve_route(rig)
    proc = _run_supervisor(rig, "success")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    rows = _events(rig)
    types = [r[0] for r in rows]
    assert types == [
        "execution.route_approved",
        "execution.started",
        "execution.finished",
    ], f"expected route -> start -> finish, got {types}"
    assert [r[1] for r in rows] == ["human", "instrument", "instrument"], (
        "the signer is a human and the observer is an instrument; a worker authors none"
    )
    assert len({r[3]["execution_id"] for r in rows}) == 1, (
        "all three facts must carry the one stable execution id"
    )

    approved, started, finished = rows
    assert started[2] == "e2e-task"
    assert finished[2] == "e2e-task"
    assert started[3]["harness_version"] == "fake-harness"

    fin = finished[3]
    assert fin["outcome"] == "success"
    assert fin["outcome_certainty"] == "certain"
    assert fin["exit_code"] == 0

    # The runner-trust facts a launch now records, and the retired ones it does not.
    assert approved[3]["route_source"] == "default"
    assert approved[3]["runner_fingerprint"].startswith("sha256:")
    assert approved[3]["order_ref"] == ORDER_REF
    assert approved[3].get("binding") is None
    assert approved[3].get("binding_ref") is None


def test_the_started_fact_carries_no_boundary_block(rig):
    """The permission-boundary product is retired, and its absence is deliberate. A
    reader distinguishes the two eras by this field rather than by a backfilled value."""
    assert _run_supervisor(rig, "success").returncode == 0
    started = [e for e in _events(rig) if e[0] == "execution.started"]
    assert len(started) == 1
    assert started[0][3].get("binding") is None
    assert "config_content" not in json.dumps(started[0][3])


def test_an_operator_override_is_recorded_as_such(tmp_path):
    """`--route` is authorized for one launch, and the spine says which launches used it."""
    plan, _ = resolve_plan(tmp_path, route_name="second",
                           routes=[route(price_basis=PRICE), route(name="second")])
    assert plan["route_source"] == "override"
    assert plan["identity"]["route"] == "second"


# --- usage ----------------------------------------------------------------------------

def test_usage_is_deduplicated_and_carries_the_price_basis(rig):
    """The fixture writes message m1 twice; a summing parser would report 300."""
    proc = _run_supervisor(rig, "success")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    fin = _events(rig)[-1][3]
    usage = fin["usage"]
    assert usage["status"] == "reported"
    assert usage["source"] == "fake-usage-file"
    assert usage["output_tokens"] == 200, (
        f"expected 200 output tokens (m1 + m2, deduplicated); got "
        f"{usage['output_tokens']} — 300 means the parser summed records, not messages"
    )
    assert usage["input_tokens"] == 30
    assert usage["cache_read_tokens"] == 3000
    assert usage["cache_write_tokens"] == 50
    assert usage["evidence_records"] == 2

    # The audit trail exists and holds no message content.
    evidence = json.loads(Path(usage["evidence_ref"]).read_text())
    assert {r["message_id"] for r in evidence["rows"]} == {"m1", "m2"}
    for row in evidence["rows"]:
        assert set(row) == {
            "message_id", "model", "input_tokens", "cache_read_tokens",
            "cache_write_tokens", "output_tokens", "sidechain",
        }

    # The price basis travels on the fact so a historical cost never needs today's
    # catalog. Cost itself is absent: it is derived, not authored.
    assert fin["price_basis"]["per_mtok_output"] == 5.0
    assert fin["price_basis"]["source"] == "fixture prices"


def test_absent_usage_surface_is_unavailable_never_zero(rig):
    """The rule the whole usage model exists for."""
    proc = _run_supervisor(rig, "nousage")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    usage = _events(rig)[-1][3]["usage"]
    assert usage["status"] == "unavailable"
    assert usage["reason"], "an unavailable surface must name its reason"
    for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
        assert usage[field] is None, f"{field} must be absent, not zero"


def test_a_generic_route_records_unavailable_usage_rather_than_failing(tmp_path):
    """The generic adapter's honest cost. It is not a launch gate: "we cannot read this
    harness's token counts" is neither a safety question nor a reason to refuse."""
    plan, _ = resolve_plan(
        tmp_path,
        routes=[route(adapter="generic", harness="a-harness-nobody-wrote-code-for")])
    assert plan["usage_evidence"] == "unavailable"
    assert plan["model_identity_evidence"] == "declared"
    assert plan["unproven_reason"]


# --- terminal facts ---------------------------------------------------------------------

def test_failure_is_recorded_as_failure_with_its_exit_code(rig):
    proc = _run_supervisor(rig, "failure")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    fin = _events(rig)[-1][3]
    assert fin["outcome"] == "failure"
    assert fin["exit_code"] == 3
    # A failed run still consumed tokens, and they are still recorded.
    assert fin["usage"]["status"] == "reported"


def test_model_mismatch_records_terminal_failure_and_never_falls_back(rig):
    """A harness that reports a different model did not run the approved route."""
    proc = _run_supervisor(rig, "wrongmodel")
    fin = _events(rig)[-1][3]
    assert fin["model_evidence"] == "harness-reported"
    assert fin["model_resolved"] == "some-other-model-9"
    assert fin["identity"]["model"] == "fixture-model-1"
    assert fin["outcome"] == "failure", (
        "a resolved model that differs from the pinned one must be a terminal failure, "
        "even though the child exited 0"
    )
    assert proc.returncode == 0  # the CHILD succeeded; the execution did not


def test_a_missing_executable_is_a_terminal_failure_with_no_started_fact(rig):
    """The cheap deterministic refusal the doctrine keeps. The launcher checks it before
    anything is created; the supervisor is the second line, beside the child."""
    plan_path = rig["run_dir"] / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["version_argv"] = ["/nonexistent/harness", "--version"]
    plan["argv"][0] = "/nonexistent/harness"
    plan_path.write_text(json.dumps(plan))

    proc = _run_supervisor(rig, "success")
    assert proc.returncode == 1
    assert "execution.started" not in [e[0] for e in _events(rig)]
    fin = [e for e in _events(rig) if e[0] == "execution.finished"]
    assert fin and fin[0][3]["outcome"] == "failure"
    assert fin[0][3]["exit_code"] == 127


def test_terminal_emission_is_idempotent(rig):
    """Re-running the pane command re-emits the SAME fact, never a second one."""
    assert _run_supervisor(rig, "success").returncode == 0
    first = _events(rig)
    second_run = _run_supervisor(rig, "success")
    assert second_run.returncode == 0, second_run.stdout + second_run.stderr
    after = _events(rig)
    assert [r[0] for r in after] == [r[0] for r in first], (
        "a re-run must not append a second terminal fact; the payload is replayed "
        "byte-for-byte and the spine deduplicates it"
    )


def test_reconcile_marks_an_unobserved_end_as_uncertain(rig):
    """The honest answer when tmux died: interrupted, and we say we are not sure."""
    plan = json.loads((rig["run_dir"] / "plan.json").read_text())
    (rig["run_dir"] / "started.json").write_text(
        json.dumps({"execution_id": plan["execution_id"]}))

    env = dict(os.environ)
    env.update({"OMEGAHIVE_DATABASE_URL": rig["db"], "OMEGAHIVE_GATEWAY_DATABASE_URL": ""})
    proc = subprocess.run(
        [str(SUPERVISE), "--reconcile", str(rig["run_dir"].parent)],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    fin = _events(rig)[-1]
    assert fin[0] == "execution.finished"
    assert fin[3]["outcome"] == "interrupted"
    assert fin[3]["outcome_certainty"] == "uncertain", (
        "an end the supervisor did not observe must not be recorded as certain"
    )
    assert fin[3]["usage"]["status"] == "unavailable"
    assert fin[3]["usage"]["reason"]


# --- the child's environment and argv -----------------------------------------------

def test_the_child_environment_excludes_credentials(rig):
    """The allowlist is a structural control, checked on the plan the supervisor execs."""
    env = rig["plan"]["env"]
    assert "ANTHROPIC_API_KEY" not in env, (
        "a credential the route did not name must not reach the worker"
    )
    assert "PATH" in env and "HOME" in env
    assert "HIVE_FAKE_USAGE_FILE" in env  # named by the route, so allowed


def test_a_provider_credential_the_route_names_does_reach_the_child(tmp_path):
    """The other half of the same rule, and the one the retired shape-based ban broke:
    an operator running an api-market route has to be able to configure this."""
    plan, _ = resolve_plan(
        tmp_path,
        routes=[route(runner=runner(inherit_env=["SOME_PROVIDER_KEY"]))],
        extra_env={"SOME_PROVIDER_KEY": "value-the-operator-configured"})
    assert plan["env"]["SOME_PROVIDER_KEY"] == "value-the-operator-configured"
    # ...and the preflight still never prints it.
    assert "value-the-operator-configured" not in json.dumps(plan["argv_redacted"])


def test_kickoff_survives_as_one_argv_element(rig):
    """Shell metacharacters in the prompt are data, not syntax."""
    argv = rig["plan"]["argv"]
    assert argv.count(KICKOFF) == 1, (
        f"the kickoff must be exactly one argv element, unmodified; got {argv!r}"
    )


def test_the_supervisor_state_is_not_world_readable(tmp_path):
    """plan.json is the supervisor's root of trust: it stamps the worker's identity onto
    every relayed request and takes every publication destination from it."""
    import re

    launcher = (REPO / "scripts" / "hive-launch").read_text()
    common = (REPO / "scripts" / "hive-common.sh").read_text()
    assert re.search(r'chmod 0600 "\$EXEC_DIR/plan\.json"', launcher)
    assert re.search(r'chmod 0700 "\$EXEC_DIR"', common)


def test_the_supervisor_state_is_outside_the_worker_task_root(tmp_path):
    """The structural rule the whole transport rests on: a worker may modify anything in
    its writable root, so nothing the trusted side reads for a decision may live there."""
    launcher = (REPO / "scripts" / "hive-launch").read_text()
    assert 'TASK_ROOT="$WORK_ROOT/$WORKER"' in launcher
    assert 'EXEC_DIR="$HIVE_EXEC_ROOT/$WORKER"' in launcher
    assert 'EXEC_DIR="$TASK_ROOT' not in launcher
