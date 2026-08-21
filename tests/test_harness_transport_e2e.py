"""End-to-end: the supervised-worker transport, on a whole simulated deployment.

A worker on a `supervised` route cannot reach the spine, the workspace hub or a forge
credential from inside its runner. This file proves the three capabilities the supervisor
supplies from outside it — spooled emits, workspace sync, and publication — with no paid
model call, no network and no vendor CLI installed.

What makes it end to end rather than a mock, and the reason it is worth its runtime:

  * the wrappers the fake worker runs are the ones `hive-launch` writes, issued by the
    same `issue_worker_interface` the launcher calls;
  * the plan comes from the real `harness-resolve`;
  * the drain, the bundle handling and every git command are the real
    `scripts/hive-supervise`;
  * the emits go through the real gateway and legality layer;
  * the hub and the code remote are real git repositories, and publication really pushes.

The negative half matters as much as the positive one. A worker may modify anything in
its own task root — including these wrappers, its git config and its hooks — so the tests
below plant exactly those things and assert the trusted side is unmoved.
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
COMMON = REPO / "scripts" / "hive-common.sh"

TASK = "drill-task"
PROJECT = "omegahive"
ORDER_REF = f"projects/{PROJECT}/orders/2026-08-20-{TASK}.md@" + "a" * 40
CODE_BRANCH = f"worker/{TASK}"
REPORT = f"projects/{PROJECT}/reports/2026-08-20-{TASK}-result.md"
QUESTION = f"projects/{PROJECT}/questions/2026-08-20-a-question.md"


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
def deployment(tmp_path):
    """A whole simulated deployment: a hub, a code remote, a task root, a run-dir."""
    db = _db_url()
    run_id = "drill-" + uuid.uuid4().hex[:8]
    worker = f"sess-{TASK}-0820"

    # --- the workspace hub, with the order the worker was launched on ------------------
    hub_seed = tmp_path / "hub-seed"
    hub_seed.mkdir()
    git(hub_seed, "init", "--quiet", "-b", "main")
    write(hub_seed / f"projects/{PROJECT}/orders/2026-08-20-{TASK}.md", "# Order: drill\n")
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

    # --- the worker's ONE task root ----------------------------------------------------
    task_root = tmp_path / worker
    task_root.mkdir()
    ws_root = task_root / "hive"
    code_root = task_root / PROJECT
    run_dir = task_root / "run"
    git(tmp_path, "clone", "--quiet", str(hub), str(ws_root))
    git(tmp_path, "clone", "--quiet", str(code_remote), str(code_root))
    git(code_root, "checkout", "--quiet", "-b", CODE_BRANCH)

    # --- the supervisor's state, OUTSIDE the task root ---------------------------------
    exec_dir = tmp_path / "exec"

    usage_file = tmp_path / "fake-usage.jsonl"
    script = tmp_path / "worker-script.sh"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "HIVE_FAKE_BEHAVIOUR": "protocol",
        "HIVE_FAKE_SCRIPT": str(script),
        "HIVE_FAKE_USAGE_FILE": str(usage_file),
        # Two credentials that must never reach the child.
        "GH_TOKEN": "ghp_must_not_propagate",
        "OMEGAHIVE_GATEWAY_DATABASE_URL": "postgres://must-not-propagate",
    }

    entries = [route(runner=runner(
        worker_io="supervised",
        inherit_env=["HIVE_FAKE_BEHAVIOUR", "HIVE_FAKE_SCRIPT", "HIVE_FAKE_USAGE_FILE",
                     "HIVE_SPOOL_TIMEOUT"]))]
    request = {
        "catalog_b64": base64.b64encode(json.dumps(catalog(*entries)).encode()).decode(),
        "route": None,
        "task": TASK,
        "order_ref": ORDER_REF,
        "purpose": "work",
        "attempt": 1,
        "kickoff": "You are hive worker drill.",
        "task_root": str(task_root),
        "cwd": str(ws_root),
        "code_root": str(code_root),
        "run_dir": str(run_dir),
        "session_id": str(uuid.uuid4()),
        "env": env,
    }
    resolved = subprocess.run([*_omegahive_cmd(), "harness-resolve"],
                              input=json.dumps(request), capture_output=True,
                              text=True, cwd=REPO)
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    plan = json.loads(resolved.stdout)
    plan.update({
        "run_id": run_id,
        "worker": worker,
        "bridge": {
            "ws_hub": str(hub), "code_repo": str(code_remote),
            "code_branch": CODE_BRANCH, "project": PROJECT,
            "order_ref": ORDER_REF, "title": "Drill",
        },
    })

    # Issue the interfaces with the SHIPPED code, not a copy of it.
    cli = " ".join(_omegahive_cmd())
    issue = subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail; source "{COMMON}"; '
         'issue_worker_interface "$1" "$2" "$3" "$4" "$5" "$6" "$7"; '
         'issue_supervisor_interface "$8" "$6" "$7" "$9"',
         "bash", str(run_dir), str(ws_root), str(code_root), CODE_BRANCH,
         "supervised", run_id, worker, str(exec_dir), TASK],
        capture_output=True, text=True, cwd=str(REPO), timeout=120)
    assert issue.returncode == 0, issue.stdout + issue.stderr

    # Point the two supervisor wrappers at this checkout instead of the container.
    # Everything else about them — the baked run, role and actor — is what shipped.
    for name, extra in (("emit.sh", f'--role instrument --actor "supervisor-{worker}" "$@"'),
                        ("emit-worker.sh",
                         f'emit-relay --run-id "{run_id}" --actor "{worker}" --task "{TASK}"')):
        path = exec_dir / name
        if name == "emit.sh":
            body = (f'cd "{REPO}"\nexec {cli} emit --run-id "{run_id}" {extra}\n')
        else:
            body = f'cd "{REPO}"\nexec {cli} {extra}\n'
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        path.chmod(0o755)

    (exec_dir / "plan.json").write_text(json.dumps(plan))

    return {
        "db": db, "run_id": run_id, "worker": worker, "plan": plan,
        "tmp": tmp_path, "hub": hub, "hub_seed": hub_seed, "code_remote": code_remote,
        "task_root": task_root, "ws_root": ws_root, "code_root": code_root,
        "run_dir": run_dir, "exec_dir": exec_dir, "script": script, "env": env,
    }


def set_child_env(dep, **pairs) -> None:
    """The child's environment is fixed in plan.json at RESOLVE time by the route's
    allowlist; the supervisor never merges its own in. So a test that needs a different
    value sets it where a real deployment variable would be: in the resolved plan."""
    path = dep["exec_dir"] / "plan.json"
    plan = json.loads(path.read_text())
    plan["env"].update(pairs)
    path.write_text(json.dumps(plan))


def run_supervisor(dep, *, timeout=300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "OMEGAHIVE_DATABASE_URL": dep["db"],
        "OMEGAHIVE_GATEWAY_DATABASE_URL": "",
        "HIVE_CLI_CMD": " ".join(_omegahive_cmd()),
    })
    return subprocess.run([str(SUPERVISE), str(dep["exec_dir"])],
                          capture_output=True, text=True, env=env,
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


def out(dep) -> str:
    path = dep["tmp"] / "worker-out.txt"
    return path.read_text() if path.exists() else ""


def events(dep) -> list[tuple[str, str, str, dict]]:
    with psycopg.connect(dep["db"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, actor_role, actor_id, payload FROM events "
            "WHERE run_id = %s ORDER BY seq", (dep["run_id"],))
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


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
# 1. Spooled emits
# =====================================================================================

def test_a_supervised_worker_emits_and_the_spine_records_it_as_the_worker(deployment):
    """The whole point: the worker's emit interface is unchanged, its identity on the
    spine is its own, and the process that carried the bytes was the supervisor."""
    seed_board(deployment)
    worker_script(deployment, '''
say "$("$EMIT" --type task.accepted --task ''' + TASK + ''' 2>&1)"
say "$("$EMIT" --type task.reported --task ''' + TASK + ''' \\
        --payload '{"kind": "progress", "ref": "notes.md@abcdef1"}' 2>&1)"
''')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    text = out(deployment)
    assert "emitted · task.accepted" in text, text
    assert "emitted · task.reported" in text, text

    rows = events(deployment)
    accepted = [r for r in rows if r[0] == "task.accepted"]
    assert len(accepted) == 1
    assert accepted[0][1] == "worker", "the spine must record the WORKER, not the supervisor"
    assert accepted[0][2] == deployment["worker"]


def test_a_rejected_emit_reaches_the_worker_as_a_rejection_on_the_same_call(deployment):
    """`rejected: <CODE>` is the worker protocol's branch point. A transport that turned
    a refusal into a timeout, or swallowed it, would make WORKER.md's rejection handling
    unreachable."""
    seed_board(deployment)
    # `review.passed` is an operator act; a worker may not emit it.
    worker_script(deployment, '''
"$EMIT" --type review.passed --task ''' + TASK + ''' >> "$OUT" 2>&1 || say "exit=$?"
''')
    assert run_supervisor(deployment).returncode == 0
    text = out(deployment)
    assert "rejected:" in text, text
    assert "exit=1" in text, "a rejection must be a non-zero exit, like the direct wrapper"


def test_a_request_naming_an_identity_is_refused_by_name(deployment):
    """The worker owns the wrapper that writes these files, so the test writes the
    request directly — which is exactly what a rewritten wrapper could do."""
    seed_board(deployment)
    worker_script(deployment, '''
mkdir -p "''' + str(deployment["run_dir"]) + '''/spool"
cat > "''' + str(deployment["run_dir"]) + '''/spool/000000900.json" <<'REQ'
{"kind":"emit","type":"task.accepted","actor":"operator","run_id":"other-run"}
REQ
for _ in $(seq 1 200); do
  [ -f "''' + str(deployment["run_dir"]) + '''/receipts/000000900.json" ] && break
  sleep 0.1
done
say "$(cat "''' + str(deployment["run_dir"]) + '''/receipts/000000900.json")"
''')
    assert run_supervisor(deployment).returncode == 0
    text = out(deployment)
    assert "REQUEST_FIELD_FORBIDDEN" in text, text
    assert "actor" in text and "run_id" in text
    assert not [r for r in events(deployment) if r[0] == "task.accepted"], (
        "nothing may be emitted for a request that tried to choose its own identity"
    )


def test_a_request_for_another_task_is_refused_not_rewritten(deployment):
    seed_board(deployment)
    worker_script(deployment, '''
"$EMIT" --type task.accepted --task someone-elses-task >> "$OUT" 2>&1 || say "exit=$?"
''')
    assert run_supervisor(deployment).returncode == 0
    assert "REQUEST_TASK_MISMATCH" in out(deployment)


def test_a_wait_times_out_loudly_rather_than_deadlocking(deployment):
    """With no supervisor draining, the wrapper must give the session back."""
    env = dict(os.environ)
    env["HIVE_SPOOL_TIMEOUT"] = "1"
    proc = subprocess.run(
        [str(deployment["run_dir"] / "emit"), "--type", "task.accepted", "--task", TASK],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "SUPERVISOR_TIMEOUT" in proc.stderr
    assert "transport failure, not a spine refusal" in proc.stderr


def test_a_request_left_by_a_dead_supervisor_is_reconciled_on_restart(deployment):
    """The timed-out request above is still on disk. A restarted supervisor delivers it
    before the child exists, so a resumed worker finds it answered rather than waiting
    out a second timeout on a receipt nobody was going to write."""
    seed_board(deployment)
    env = dict(os.environ)
    env["HIVE_SPOOL_TIMEOUT"] = "1"
    subprocess.run([str(deployment["run_dir"] / "emit"), "--type", "task.accepted",
                    "--task", TASK], capture_output=True, text=True, env=env, timeout=60)
    assert (deployment["run_dir"] / "spool" / "000000001.json").exists()

    worker_script(deployment, 'say "child ran"\n')
    assert run_supervisor(deployment).returncode == 0
    assert [r for r in events(deployment) if r[0] == "task.accepted"], (
        "the orphaned request must be delivered, not silently dropped"
    )
    receipt = json.loads((deployment["run_dir"] / "receipts" / "000000001.json").read_text())
    assert receipt["status"] == "accepted"


def test_the_drain_order_is_deterministic_per_worker(deployment):
    """Zero-padded, never-reused ids drained in lexicographic order. A second-resolution
    timestamp would tie, and a tie is a reordering."""
    seed_board(deployment)
    worker_script(deployment, "".join(
        f'"$EMIT" --type task.reported --task {TASK} '
        f'--payload \'{{"kind": "progress", "ref": "step-{i}.md@abcdef1"}}\' >/dev/null 2>&1\n'
        for i in range(5)))
    assert run_supervisor(deployment).returncode == 0
    refs = [r[3]["ref"] for r in events(deployment) if r[0] == "task.reported"]
    assert refs == [f"step-{i}.md@abcdef1" for i in range(5)], refs


# =====================================================================================
# 2. Workspace sync
# =====================================================================================

def test_sync_workspace_delivers_an_operator_side_answer_commit(deployment):
    """The answer/unblock round trip. WORKER.md's `git pull` has to work from inside a
    boundary that cannot reach the hub, or a blocked worker can never be unblocked."""
    # The operator answers the order on the hub while the worker is sandboxed.
    write(deployment["hub_seed"] / f"projects/{PROJECT}/orders/2026-08-20-{TASK}.md",
          "# Order: drill\n\n## Answers\n\nYes.\n")
    git(deployment["hub_seed"], "add", "-A")
    git(deployment["hub_seed"], "commit", "--quiet", "-m", "answer")
    git(deployment["hub_seed"], "push", "--quiet", str(deployment["hub"]), "main")

    worker_script(deployment, '''
say "$("$HIVE" sync workspace 2>&1)"
ORDER="$WS/projects/''' + PROJECT + '''/orders/2026-08-20-''' + TASK + '''.md"
say "ANSWER:$(grep -c Answers "$ORDER")"
''')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = out(deployment)
    assert "workspace synced" in text, text
    assert "ANSWER:1" in text, "the answer commit must be readable in the worker's clone"


def test_the_sync_bundle_is_the_only_thing_the_trusted_side_writes_into_the_task_root(
        deployment):
    worker_script(deployment, 'say "$("$HIVE" sync workspace 2>&1)"\n')
    assert run_supervisor(deployment).returncode == 0
    written = sorted(p.name for p in (deployment["run_dir"] / "sync").iterdir())
    assert written == ["workspace.bundle"]


# =====================================================================================
# 3. Publication
# =====================================================================================

def test_publish_workspace_pushes_the_report_and_preserves_the_workers_commit_sha(
        deployment):
    """The sha must survive, or a `path@sha` pin the worker took before publishing
    would not resolve on the hub — and every ref in its result report would be dead."""
    worker_script(deployment, '''
mkdir -p "$WS/projects/''' + PROJECT + '''/reports"
printf '# Result\\n' > "$WS/''' + REPORT + '''"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "result"
say "SHA:$(git -C "$WS" rev-parse HEAD)"
say "$("$HIVE" publish workspace 2>&1)"
''')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = out(deployment)
    worker_sha = next(ln.split(":", 1)[1] for ln in text.splitlines() if ln.startswith("SHA:"))
    assert f"published {worker_sha}" in text, text

    hub_sha = git(deployment["hub"], "rev-parse", "refs/heads/main").stdout.strip()
    assert hub_sha == worker_sha, "publication must preserve the worker's own commit sha"


def test_publish_workspace_refuses_a_path_outside_this_tasks_reports_and_questions(
        deployment):
    """The worker chose the content; the trusted side chose what content is allowed."""
    worker_script(deployment, '''
printf 'x\\n' > "$WS/projects/''' + PROJECT + '''/OPERATIONS.md"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "sneak"
say "$("$HIVE" publish workspace 2>&1)" || true
''')
    # The worker absorbs the bridge refusal itself (`|| true` in its script), so the
    # supervisor's own outcome is a clean success. An earlier version of this line read
    # `!= 0 or True`, which cannot fail and therefore checked nothing.
    assert run_supervisor(deployment).returncode == 0
    text = out(deployment)
    assert "PATH_NOT_ALLOWED" in text, text
    assert "OPERATIONS.md" in text
    hub_files = git(deployment["hub"], "ls-tree", "-r", "--name-only", "main").stdout
    assert "OPERATIONS.md" not in hub_files


def test_publish_workspace_allows_a_question_file(deployment):
    """A question is named by date and topic, not by task — so the allowance is this
    project's questions directory, exactly as the order words it."""
    worker_script(deployment, '''
mkdir -p "$WS/projects/''' + PROJECT + '''/questions"
printf '# Q\\n' > "$WS/''' + QUESTION + '''"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "question"
say "$("$HIVE" publish workspace 2>&1)"
''')
    assert run_supervisor(deployment).returncode == 0
    assert "published" in out(deployment)


def test_a_non_fast_forward_refuses_and_names_the_remedy(deployment):
    """And the remedy actually works from inside the boundary: sync, rebase, retry."""
    # The hub moves while the worker is writing.
    write(deployment["hub_seed"] / f"projects/{PROJECT}/questions/2026-08-20-other.md", "# Other\n")
    git(deployment["hub_seed"], "add", "-A")
    git(deployment["hub_seed"], "commit", "--quiet", "-m", "someone else")
    git(deployment["hub_seed"], "push", "--quiet", str(deployment["hub"]), "main")

    worker_script(deployment, '''
mkdir -p "$WS/projects/''' + PROJECT + '''/reports"
printf '# Result\\n' > "$WS/''' + REPORT + '''"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "result"
say "FIRST:$("$HIVE" publish workspace 2>&1)"
say "SYNC:$("$HIVE" sync workspace 2>&1)"
say "RETRY:$("$HIVE" publish workspace 2>&1)"
''')
    proc = run_supervisor(deployment)
    text = out(deployment)
    assert "NON_FAST_FORWARD" in text, text
    assert "sync workspace" in text and "rebase" in text
    assert "RETRY:published" in text, f"the named remedy must work: {text}"
    assert proc.returncode == 0, proc.stdout + proc.stderr

    hub_files = git(deployment["hub"], "ls-tree", "-r", "--name-only", "main").stdout
    assert REPORT in hub_files and "2026-08-20-other.md" in hub_files


def test_publish_code_pushes_the_launch_recorded_branch(deployment):
    worker_script(deployment, '''
printf 'worker change\\n' >> "$CODE/README.md"
git -C "$CODE" add -A
git -C "$CODE" -c user.name=w -c user.email=w@x commit --quiet -m "the change"
say "SHA:$(git -C "$CODE" rev-parse HEAD)"
say "$("$HIVE" publish code 2>&1)"
''')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = out(deployment)
    sha = next(ln.split(":", 1)[1] for ln in text.splitlines() if ln.startswith("SHA:"))
    remote_sha = git(deployment["code_remote"], "rev-parse",
                     f"refs/heads/{CODE_BRANCH}").stdout.strip()
    assert remote_sha == sha
    assert "no forge configured" in text, (
        "a local destination gets the push and an honest note, not a fabricated PR"
    )


def test_a_request_cannot_redirect_the_publication(deployment):
    """The four things a request may never choose: source, branch, destination, refspec."""
    evil = deployment["tmp"] / "evil.git"
    git(deployment["tmp"], "init", "--quiet", "--bare", str(evil))
    worker_script(deployment, '''
mkdir -p "''' + str(deployment["run_dir"]) + '''/spool"
cat > "''' + str(deployment["run_dir"]) + '''/spool/000000900.json" <<'REQ'
{"kind":"bridge","op":"publish-code","remote":"''' + str(evil) + '''","branch":"main"}
REQ
for _ in $(seq 1 200); do
  [ -f "''' + str(deployment["run_dir"]) + '''/receipts/000000900.json" ] && break
  sleep 0.1
done
say "$(cat "''' + str(deployment["run_dir"]) + '''/receipts/000000900.json")"
''')
    assert run_supervisor(deployment).returncode == 0
    assert "REQUEST_FIELD_FORBIDDEN" in out(deployment)
    assert not git(evil, "branch", "-a").stdout.strip(), "nothing may reach a named destination"


def test_an_unknown_bridge_op_is_refused(deployment):
    worker_script(deployment, '''
mkdir -p "''' + str(deployment["run_dir"]) + '''/spool"
SPOOL="''' + str(deployment["run_dir"]) + '''/spool"
printf '{"kind":"bridge","op":"push-anywhere"}\\n' > "$SPOOL/000000900.json"
for _ in $(seq 1 200); do
  [ -f "''' + str(deployment["run_dir"]) + '''/receipts/000000900.json" ] && break
  sleep 0.1
done
say "$(cat "''' + str(deployment["run_dir"]) + '''/receipts/000000900.json")"
''')
    assert run_supervisor(deployment).returncode == 0
    assert "BRIDGE_OP_UNKNOWN" in out(deployment)


# =====================================================================================
# 4. The trusted side never runs worker content
# =====================================================================================

def test_worker_hooks_and_credential_helpers_never_run_on_the_trusted_side(deployment):
    """A worker-owned `.git` is worker-CONTROLLED INPUT (Codex boundary report, gate 4).
    The worker plants every hook surface it has — a hooks directory, a redirected
    `core.hooksPath`, a credential helper, a pack-objects hook — and none of them may
    fire in a trusted-side git command."""
    marker = deployment["tmp"] / "HOOK-RAN"
    hooks = deployment["tmp"] / "planted-hooks"
    hooks.mkdir()
    for name in ("pre-push", "post-commit", "prepare-commit-msg", "post-checkout"):
        h = hooks / name
        h.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        h.chmod(0o755)

    worker_script(deployment, f'''
git -C "$WS" config core.hooksPath "{hooks}"
git -C "$CODE" config core.hooksPath "{hooks}"
git -C "$WS" config credential.helper '!f() {{ touch "{marker}"; }}; f'
git -C "$CODE" config uploadpack.packObjectsHook 'sh -c "touch {marker}; exec git pack-objects $@"'
mkdir -p "$WS/projects/{PROJECT}/reports"
printf '# Result\\n' > "$WS/{REPORT}"
git -C "$WS" add -A
git -C "$WS" -c core.hooksPath=/dev/null -c user.name=w -c user.email=w@x commit --quiet -m "result"
printf 'x\\n' >> "$CODE/README.md"
git -C "$CODE" add -A
git -C "$CODE" -c core.hooksPath=/dev/null -c user.name=w -c user.email=w@x commit --quiet -m "code"
say "$("$HIVE" publish workspace 2>&1)"
say "$("$HIVE" publish code 2>&1)"
''')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "published" in out(deployment)
    assert not marker.exists(), (
        "a worker-planted hook or credential helper executed on the trusted side"
    )


def test_the_trusted_side_never_checks_out_worker_content(deployment):
    """Its repositories are bare, so there is no worktree for anything to land in."""
    worker_script(deployment, '''
printf 'x\\n' >> "$CODE/README.md"
git -C "$CODE" add -A
git -C "$CODE" -c user.name=w -c user.email=w@x commit --quiet -m "code"
say "$("$HIVE" publish code 2>&1)"
''')
    assert run_supervisor(deployment).returncode == 0
    for repo in (deployment["exec_dir"] / "publish").iterdir():
        assert (repo / "HEAD").exists(), f"{repo} is not a git dir"
        assert not (repo / ".git").exists(), f"{repo} was checked out"


# =====================================================================================
# 5. No credential anywhere near the worker
# =====================================================================================

def test_no_forge_hub_or_gateway_credential_reaches_the_child(deployment):
    env = deployment["plan"]["env"]
    assert "GH_TOKEN" not in env
    assert "OMEGAHIVE_GATEWAY_DATABASE_URL" not in env
    assert "PATH" in env


def test_the_task_root_holds_no_credential_and_no_trusted_wrapper(deployment):
    """Everything a trusted-side decision depends on is under the exec dir, and the exec
    dir is not under the task root."""
    worker_script(deployment, 'say "ran"\n')
    assert run_supervisor(deployment).returncode == 0

    exec_dir, task_root = deployment["exec_dir"], deployment["task_root"]
    assert exec_dir.resolve().relative_to(deployment["tmp"])
    assert task_root not in exec_dir.parents

    blob = ""
    for path in task_root.rglob("*"):
        if path.is_file() and ".git/" not in str(path):
            try:
                blob += path.read_text(errors="ignore")
            except OSError:
                pass
    assert "ghp_must_not_propagate" not in blob
    assert "postgres://" not in blob
    # And the relay wrapper — the file carrying the worker's identity — is not in reach.
    assert not list(task_root.rglob("emit-worker.sh"))


def test_no_receipt_or_event_carries_a_credential(deployment):
    worker_script(deployment, f'''
mkdir -p "$WS/projects/{PROJECT}/reports"
printf '# Result\\n' > "$WS/{REPORT}"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "result"
"$HIVE" publish workspace >/dev/null 2>&1
"$EMIT" --type task.reported --task {TASK} \\
  --payload '{{"kind": "progress", "ref": "n.md@abcdef1"}}' >/dev/null 2>&1
''')
    seed_board(deployment)
    assert run_supervisor(deployment).returncode == 0
    receipts = "".join(p.read_text() for p in (deployment["run_dir"] / "receipts").iterdir())
    assert "ghp_" not in receipts and "postgres://" not in receipts
    blob = json.dumps(events(deployment))
    assert "ghp_" not in blob and "postgres://" not in blob


# =====================================================================================
# 6. The execution facts are unchanged by the transport
# =====================================================================================

def test_the_lifecycle_facts_are_the_same_shape_on_a_supervised_route(deployment):
    """Adding a transport must not change what an execution records about itself."""
    worker_script(deployment, 'say "ran"\n')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    kinds = [r[0] for r in events(deployment)]
    assert kinds == ["execution.started", "execution.finished"]
    started = next(r[3] for r in events(deployment) if r[0] == "execution.started")
    finished = next(r[3] for r in events(deployment) if r[0] == "execution.finished")
    assert started["harness_version"] == "9.9.9"
    assert started["identity"]["route"] == "fake-subscription"
    assert finished["outcome"] == "success"
    assert finished["usage"]["status"] == "reported"


def test_the_supervisor_still_cannot_speak_as_the_worker(deployment):
    """Two wrappers, two identities. The relay carries the worker's actor because the
    supervisor is delivering the worker's own words; `emit.sh` carries `instrument`,
    which the gateway authorizes for `execution.*` and for no task event at all."""
    text = (deployment["exec_dir"] / "emit.sh").read_text()
    assert "--role instrument" in text
    assert f'--actor "supervisor-{deployment["worker"]}"' in text
    relay = (deployment["exec_dir"] / "emit-worker.sh").read_text()
    assert "emit-relay" in relay and deployment["worker"] in relay


def test_a_worker_cannot_kill_its_own_supervisor_by_breaking_its_receipts_dir(deployment):
    """Found by reading the drain as code rather than by running it.

    The receipts directory is inside the WORKER's writable root. Every step of writing a
    receipt can therefore be made to fail by the worker — and under `set -e` a failure
    inside the drain loop would exit the supervisor before it could record the
    execution's terminal fact, which is the one outcome that whole script exists to
    prevent. So a broken receipts directory is logged and the run continues.
    """
    seed_board(deployment)
    set_child_env(deployment, HIVE_SPOOL_TIMEOUT="3")
    receipts = deployment["run_dir"] / "receipts"
    worker_script(deployment, f'''
chmod 0500 "{receipts}"
"$EMIT" --type task.accepted --task {TASK} >/dev/null 2>&1 || true
chmod 0700 "{receipts}"
say "worker finished its turn"
''')
    proc = run_supervisor(deployment)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "worker finished its turn" in out(deployment)
    assert "could not write a receipt" in proc.stderr, proc.stderr

    kinds = [r[0] for r in events(deployment)]
    assert "execution.finished" in kinds, (
        "the terminal fact must be recorded even when the worker broke its own transport"
    )
    # And the emit itself still happened: the receipt is the worker's copy, not the record.
    assert "task.accepted" in kinds


# =====================================================================================
# 7. What the independent review found (2026-08-20), each with the case that catches it
# =====================================================================================

def test_publish_code_refuses_a_branch_with_no_ancestor_in_the_repository(deployment):
    """A worker owns its own `.git` and can build an ORPHAN commit — history with no
    ancestor in the repository at all. Bundled and published, that would land unrelated
    history on the launch-recorded branch, and `git push` would not object because the
    branch is new. `push` only refuses a non-fast-forward over an EXISTING branch; this
    is the case it cannot see, and the workspace path already checked its equivalent.

    The check is a shared merge base, not "contains main": a pull request perfectly well
    sits behind main, and requiring otherwise would force a rebase every time main moved.
    """
    worker_script(deployment, '''
git -C "$CODE" checkout --quiet --orphan rewritten
git -C "$CODE" rm -rq --cached . 2>/dev/null || true
printf 'not this repository\\n' > "$CODE/README.md"
git -C "$CODE" add -A
git -C "$CODE" -c user.name=w -c user.email=w@x commit --quiet -m "orphan"
git -C "$CODE" branch -f "''' + CODE_BRANCH + '''" HEAD
say "$("$HIVE" publish code 2>&1)" || true
''')
    assert run_supervisor(deployment).returncode == 0
    assert "UNRELATED_HISTORY" in out(deployment), out(deployment)
    remote_branches = git(deployment["code_remote"], "branch", "--list").stdout
    assert CODE_BRANCH not in remote_branches, (
        "unrelated history must not reach the launch-recorded branch"
    )


def test_publish_workspace_refuses_a_deletion_even_under_an_allowed_path(deployment):
    """A worker may write its own report. It may not remove anything — including another
    worker's report, which the path rule alone would already refuse, and its own, which
    it would not. Rename detection is off in the trusted repository, so a file renamed
    OUT of the allowed set arrives here as a deletion of its source path."""
    # Seed a report belonging to a DIFFERENT task on the hub.
    other = f"projects/{PROJECT}/reports/2026-01-01-other-task-result.md"
    write(deployment["hub_seed"] / other, "# Someone else's result\n")
    git(deployment["hub_seed"], "add", "-A")
    git(deployment["hub_seed"], "commit", "--quiet", "-m", "other worker's report")
    git(deployment["hub_seed"], "push", "--quiet", str(deployment["hub"]), "main")

    worker_script(deployment, f'''
"$HIVE" sync workspace >/dev/null 2>&1
git -C "$WS" rm -q "{other}"
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "tidy up"
say "$("$HIVE" publish workspace 2>&1)" || true
''')
    assert run_supervisor(deployment).returncode == 0
    text = out(deployment)
    assert "PATH_NOT_ALLOWED" in text and "deleted:" in text, text
    hub_files = git(deployment["hub"], "ls-tree", "-r", "--name-only", "main").stdout
    assert other in hub_files, "the other worker's report must still be on the hub"


def test_publish_workspace_lets_a_worker_ADD_a_question_but_not_change_one(deployment):
    """`questions/` cannot be narrowed by task — a question is named by date and topic —
    so it is narrowed by STATUS instead. Without that, this project's whole questions
    directory is writable by every worker in it, and a pending question someone is
    waiting on could be edited or removed by an unrelated task."""
    existing = f"projects/{PROJECT}/questions/2026-01-01-someone-elses.md"
    write(deployment["hub_seed"] / existing, "# Their question\n")
    git(deployment["hub_seed"], "add", "-A")
    git(deployment["hub_seed"], "commit", "--quiet", "-m", "a pending question")
    git(deployment["hub_seed"], "push", "--quiet", str(deployment["hub"]), "main")

    worker_script(deployment, f'''
"$HIVE" sync workspace >/dev/null 2>&1
printf '# Mine\\n' > "$WS/{QUESTION}"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "my question"
say "ADD:$("$HIVE" publish workspace 2>&1)"
printf 'edited\\n' >> "$WS/{existing}"
git -C "$WS" add -A
git -C "$WS" -c user.name=w -c user.email=w@x commit --quiet -m "edit theirs"
say "EDIT:$("$HIVE" publish workspace 2>&1)" || true
''')
    assert run_supervisor(deployment).returncode == 0
    text = out(deployment)
    assert "ADD:published" in text, text
    assert "EDIT:" in text and "PATH_NOT_ALLOWED" in text, text
    assert "changed-not-added:" in text, text
    on_hub = git(deployment["hub"], "show", f"main:{existing}").stdout
    assert "edited" not in on_hub, "the other worker's question must be unchanged"
