"""The operation service: five operator operations, each run as the human who decided.

`answer`, `close`, `merge`, `abandon` and `launch` (salvage plan D6) are the operator's
existing scripts and `gh`, run from a fixed argv table with a sanitized environment. Each
call names an operation id. The sequence is always the same: take the task's lock (and,
for a launch, the deployment-wide launch lock); replay the receipt if the id was already
run with the same content, refuse if with different content; check the operation's
precondition against the board, read through the port as the task page reads it; run the
commands with `HIVE_ACTOR` (the human), `HIVE_EXECUTED_BY=operation-service` and
`HIVE_DECISION_REF=<operation id>`; write the receipt; release.

A receipt is one JSON file per operation id under the policy's `receipts_dir`. Nothing
reads receipts but the replay path and a human: the spine is the record (D5).

Who may ask. The service runs only what a human asked for: from the web, where the UI
refuses a request with no Tailscale identity and forwards the login, and this service is
the one place that checks it against the policy's `web_login`; or from `hive ops` on this
host. Local processes are trusted (D8):
anything already running here could reach this socket, exactly as it can already run the
scripts this service runs. There is one human, recorded as the policy's `operator_actor`.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import published
from .report.reader import PortFactory, read_view
from .worker_seat import WorkerSeats

EXECUTED_BY = "operation-service"
VERDICTS = ("clean", "minor rework", "rework")
ABANDONABLE = {"created", "ready", "assigned", "in_progress", "blocked", "in_review", "reopened"}
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
NUDGE_UNCONFIRMED = 3  # hive-answer: the answer landed; the nudge was typed, not confirmed
NUDGE_NOTE = ("answer landed; the nudge to the worker could not be confirmed. Check the "
              "worker output, and press Enter in its window if the line is still unsent")
MAX_OUTPUT = 16 * 1024
# What the scripts may inherit from the service's own environment. Everything else they
# need comes from the policy's env_file (route credentials) or from the policy itself.
ENV_ALLOW = ("PATH", "HOME", "LANG", "USER", "LOGNAME", "XDG_RUNTIME_DIR",
             "DBUS_SESSION_BUS_ADDRESS", "TMUX_TMPDIR", "SSH_AUTH_SOCK",
             "OMEGAHIVE_DEPLOYMENT", "OMEGAHIVE_SECRETS_DIR")
_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_SHA = re.compile(r"[0-9a-f]{40}")

Runner = Callable[[list[str], Mapping[str, str], int], dict[str, Any]]
Board = Mapping[str, Mapping[str, Any]]


class Refused(Exception):
    """The operation was not run; the message says why, for the human, verbatim."""


class Forbidden(Refused):
    """Not run because the asker may not operate the hive at all (D8)."""


def run_command(argv: list[str], env: Mapping[str, str], timeout: int) -> dict[str, Any]:
    try:
        done = subprocess.run(argv, env=dict(env), capture_output=True, text=True,  # noqa: S603
                              timeout=timeout, stdin=subprocess.DEVNULL, check=False)
        return {"argv": argv, "exit_status": done.returncode, "timed_out": False,
                "stdout": done.stdout[-MAX_OUTPUT:], "stderr": done.stderr[-MAX_OUTPUT:]}
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else ""
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else ""
        return {"argv": argv, "exit_status": None, "timed_out": True,
                "stdout": out[-MAX_OUTPUT:], "stderr": err[-MAX_OUTPUT:]}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(params: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = params.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise Refused(f"'{key}' is required")
    return value.strip()


def load_env_file(path: str | None) -> dict[str, str]:
    """KEY=VALUE lines, optionally `export`ed and quoted; comments and blanks skipped."""
    if not path:
        return {}
    values: dict[str, str] = {}
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path}: line {number} is not KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def github_repo(code_repo: str) -> str:
    """owner/repo from git@github.com:owner/repo.git or https://github.com/owner/repo."""
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
                         code_repo)
    if not match:
        raise Refused(f"CODE_REPO '{code_repo}' is not a GitHub repository")
    return match.group(1)


class Operations:
    def __init__(self, deployment: Mapping[str, Any], port_factory: PortFactory, *,
                 runner: Runner = run_command, seats: WorkerSeats | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.d = deployment
        self.port_factory = port_factory
        self.runner = runner
        self.seats = seats or WorkerSeats(deployment["tmux_session"])
        self.environ = os.environ if environ is None else environ
        self.receipts = Path(deployment["receipts_dir"])
        self.scripts = Path(deployment["scripts_root"])
        self.hub = deployment["workspace_hub"]
        self.timeout = int(deployment["bounds"]["operation_timeout_s"])

    # --- the one entry point -----------------------------------------------------------

    def execute(self, operation: str, operation_id: str, surface: str,
                params: Mapping[str, Any], login: str | None = None) -> dict[str, Any]:
        check = getattr(self, f"_prepare_{operation}", None)
        if check is None:
            raise Refused(f"unknown operation '{operation}'")
        if not _ID.fullmatch(operation_id or ""):
            raise Refused("operation id must match [A-Za-z0-9._-]{1,100}")
        self._authorize(surface, login)
        request = {"operation": operation, "surface": surface,
                   "params": {k: params[k] for k in sorted(params)}}
        run, task = self._target(operation, params)
        with self._lock(f"task.{run}.{task}"), self._launch_lock(operation):
            replay = self._replay(operation_id, request)
            if replay is not None:
                return replay
            receipt: dict[str, Any] = {
                "operation_id": operation_id, "request": request, "run": run, "task": task,
                "actor": self.d["operator_actor"], "started_at": _now(), "commands": [],
                "notes": [],
            }
            path = self.receipts / f"{operation_id}.json"
            self.receipts.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, "x") as handle:   # the id is now taken, for any content
                    json.dump({**receipt, "status": "running"}, handle)
            except FileExistsError:               # taken meanwhile, under another task's lock
                return self._replay(operation_id, request) or {}
            board = self._board(run)
            receipt["status_before"] = board.get(task, {}).get("status")
            try:
                commands = check(run, task, params, board, receipt)
                env = self._env(operation_id)
                for argv in commands:
                    result = self.runner(argv, env, self.timeout)
                    receipt["commands"].append(result)
                    if result["exit_status"] != 0:
                        break
                if operation == "merge" and receipt["commands"][-1]["exit_status"] == 0:
                    # gh's exit status is not the outcome: what GitHub now says is.
                    view = self.runner(self._pr_view(run, params), env, 60)
                    receipt["commands"].append(view)
                    try:
                        state = json.loads(view["stdout"]).get("state")
                    except (json.JSONDecodeError, AttributeError):
                        state = None
                    receipt["merged"] = state == "MERGED"
                ok = {0, NUDGE_UNCONFIRMED} if operation == "answer" else {0}
                failed = [c for c in receipt["commands"] if c["exit_status"] not in ok]
                if not failed and receipt["commands"] \
                        and receipt["commands"][-1]["exit_status"] == NUDGE_UNCONFIRMED:
                    receipt["notes"].append(NUDGE_NOTE)
                receipt["status"] = "failed" if failed else "done"
                receipt["message"] = (failed[0]["stderr"] or failed[0]["stdout"]).strip() \
                    if failed else "done"
                if not failed and receipt.get("merged") is False:
                    receipt["status"] = "failed"
                    receipt["message"] = (f"gh pr merge exited 0, but GitHub now calls the PR "
                                          f"{state or 'unreadable'}; it is not merged")
            except Refused as exc:
                receipt["status"], receipt["message"] = "refused", str(exc)
            receipt["status_after"] = self._board(run).get(task, {}).get("status")
            receipt["finished_at"] = _now()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(receipt, indent=2))
            tmp.replace(path)
            return receipt

    def _authorize(self, surface: str, login: str | None) -> None:
        if surface == "cli":
            return
        if surface == "web":
            expected = self.d["authority"]["web_login"]
            if expected and login == expected:
                return
            raise Forbidden("this web identity may not operate the hive (policy web_login)")
        raise Forbidden(f"unknown surface '{surface}'")

    def _target(self, operation: str, params: Mapping[str, Any]) -> tuple[str, str]:
        if operation == "launch":
            match = published.ORDER_PATH.fullmatch(_text(params, "order_path"))
            if not match:
                raise Refused("order_path must be projects/<project>/orders/<file>.md")
            project, stem = match.groups()
            return self._project_conf(project)["RUN_ID"], published.task_of(stem)
        run, task = _text(params, "run"), _text(params, "task")
        if not (_NAME.fullmatch(run) and _NAME.fullmatch(task)):
            raise Refused("run and task must match [A-Za-z0-9._-]+")
        return run, task

    def _replay(self, operation_id: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
        path = self.receipts / f"{operation_id}.json"
        if not path.exists():
            return None
        previous = json.loads(path.read_text())
        if previous.get("request") != request:
            raise Refused(f"operation id {operation_id} was already used for a different "
                          "request; nothing was run")
        return {**previous, "replayed": True}

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        locks = self.receipts / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        with open(locks / f"{name}.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @contextmanager
    def _launch_lock(self, operation: str) -> Iterator[None]:
        if operation != "launch":
            yield
            return
        with self._lock("launch"):
            yield

    def _env(self, operation_id: str) -> dict[str, str]:
        env = {k: self.environ[k] for k in ENV_ALLOW if k in self.environ}
        env.update(load_env_file(self.d.get("env_file")))
        env.update(HIVE_ACTOR=self.d["operator_actor"], HIVE_EXECUTED_BY=EXECUTED_BY,
                   HIVE_DECISION_REF=operation_id)
        return env

    # --- reads -------------------------------------------------------------------------

    def _view(self, run: str) -> Any:
        return read_view(self.port_factory, run, None, None)

    def _board(self, run: str) -> dict[str, dict[str, Any]]:
        board = self._view(run).board
        tasks = board.tasks if board is not None else {}
        return {tid: {"status": t.status, "owner": t.owner} for tid, t in tasks.items()}

    def _events(self, run: str, task: str) -> list[Any]:
        return sorted((e for e in self._view(run).events if e.task_id == task),
                      key=lambda e: e.seq or 0)

    def _project_conf(self, project: str) -> dict[str, str]:
        path = Path(self.d["operator_workspace"]) / "projects" / project / "project.conf"
        if not path.exists():
            raise Refused(f"no project.conf for project '{project}'")
        return load_env_file(str(path))

    def _conf_for_run(self, run: str) -> dict[str, str]:
        for path in sorted(Path(self.d["operator_workspace"]).glob("projects/*/project.conf")):
            conf = load_env_file(str(path))
            if conf.get("RUN_ID") == run:
                return conf
        raise Refused(f"no project.conf names run '{run}'")

    def _hub_text(self, spec: str) -> str | None:
        return published.git_read(self.hub, "show", spec)

    def latest_result_ref(self, run: str, task: str) -> str | None:
        posted = [e for e in self._events(run, task) if e.event_type == "task.result_posted"]
        refs = posted[-1].payload.get("artifact_refs") if posted else None
        return refs[0].get("ref") if refs else None

    def latest_question_seq(self, run: str, task: str) -> int | None:
        asked = [e.seq for e in self._events(run, task) if e.event_type == "question.asked"]
        return asked[-1] if asked else None

    def named_prs(self, run: str, task: str) -> list[int]:
        """PRs of the project's code repository that the latest result report names."""
        ref = self.latest_result_ref(run, task)
        path, _, sha = (ref or "").rpartition("@")
        report = self._hub_text(f"{sha}:{path}") if ref else None
        if report is None:
            return []
        repo = github_repo(self._conf_for_run(run)["CODE_REPO"])
        found = re.findall(rf"https://github\.com/{re.escape(repo)}/pull/(\d+)", report)
        return sorted({int(n) for n in found})

    def _pr_view(self, run: str, params: Mapping[str, Any]) -> list[str]:
        repo = github_repo(self._conf_for_run(run)["CODE_REPO"])
        return ["gh", "pr", "view", str(params["pr"]), "--repo", repo, "--json",
                "number,url,state,headRefOid,mergeable,statusCheckRollup"]

    def merge_candidates(self, run: str, task: str) -> list[dict[str, Any]]:
        out = []
        for number in self.named_prs(run, task):
            view = self.runner(self._pr_view(run, {"pr": number}), self._env("read"), 60)
            try:
                pr = json.loads(view["stdout"]) if view["exit_status"] == 0 else {}
            except json.JSONDecodeError:
                pr = {}
            out.append({"number": number, "url": pr.get("url"), "state": pr.get("state"),
                        "head": pr.get("headRefOid"), "checks": _checks(pr),
                        "error": None if pr else (view["stderr"] or "unreadable").strip()})
        return out

    def routes(self) -> list[str]:
        catalog = json.loads(Path(self.d["route_catalog"]).read_text())
        return [r["name"] for r in catalog.get("routes", []) if r.get("enabled")]

    def tail(self, task: str) -> list[str] | None:
        return self.seats.tail(task)

    # --- preconditions (D6) and the argv table -----------------------------------------

    def _script(self, name: str) -> str:
        return str(self.scripts / name)

    def _prepare_answer(self, run: str, task: str, params: Mapping[str, Any], board: Board,
                  receipt: dict[str, Any]) -> list[list[str]]:
        text = _text(params, "text")
        if "\n" in text or text in ("--sha", "--resume-only"):
            raise Refused("an answer from the page is one line; a long answer is committed "
                          "under ## Answers and sent with hive-answer --sha")
        _require_status(board, task, {"blocked"})
        latest = self.latest_question_seq(run, task)
        if latest is None or params.get("question_seq") != latest:
            raise Refused(f"this answer is for question {params.get('question_seq')}, but the "
                          f"task's latest question is {latest}; reload and answer that one")
        return [[self._script("hive-answer"), task, text]]

    def _prepare_close(self, run: str, task: str, params: Mapping[str, Any], board: Board,
                  receipt: dict[str, Any]) -> list[list[str]]:
        verdict = _text(params, "verdict")
        if verdict not in VERDICTS:
            raise Refused(f"verdict must be one of {', '.join(VERDICTS)}")
        _require_status(board, task, {"in_review"})
        latest = self.latest_result_ref(run, task)
        if params.get("result_ref") != latest:
            raise Refused(f"this close is for result {params.get('result_ref')}, but the "
                          f"latest posted result is {latest}; reload and read that one")
        reason = _text(params, "reason", required=False)
        return [[self._script("hive-close"), task, "--review", verdict,
                 *(["--reason", reason] if reason else [])]]

    def _prepare_merge(self, run: str, task: str, params: Mapping[str, Any], board: Board,
                  receipt: dict[str, Any]) -> list[list[str]]:
        pr, head = params.get("pr"), _text(params, "head_sha")
        if not isinstance(pr, int) or isinstance(pr, bool) or not _SHA.fullmatch(head):
            raise Refused("merge needs a PR number and the full head sha")
        _require_status(board, task, {"in_review"})
        if pr not in self.named_prs(run, task):
            raise Refused(f"the latest result report does not name PR #{pr}")
        view = self.runner(self._pr_view(run, params), self._env(receipt["operation_id"]), 60)
        receipt["commands"].append(view)
        try:
            state = json.loads(view["stdout"]) if view["exit_status"] == 0 else None
        except json.JSONDecodeError:
            state = None
        if not isinstance(state, dict):
            raise Refused(f"PR #{pr} could not be read: {view['stderr'].strip()}")
        if state.get("headRefOid") != head:
            raise Refused(f"PR #{pr}'s head moved to {state.get('headRefOid')}; reload")
        if state.get("state") != "OPEN":
            raise Refused(f"PR #{pr} is {state.get('state')}, not open")
        if _checks(state) != "green":
            raise Refused(f"PR #{pr}'s checks on {head[:10]} are {_checks(state)}, not green")
        repo = github_repo(self._conf_for_run(run)["CODE_REPO"])
        return [["gh", "pr", "merge", str(pr), "--repo", repo, "--squash",
                 "--match-head-commit", head]]

    def _prepare_abandon(self, run: str, task: str, params: Mapping[str, Any], board: Board,
                  receipt: dict[str, Any]) -> list[list[str]]:
        reason = _text(params, "reason")
        _require_status(board, task, ABANDONABLE)
        seat = self.seats.clear_dead_seat(task)
        if seat == "occupied":
            raise Refused(f"'{task}' still has a running worker in its window; stop it first")
        if seat == "cleared":
            receipt["notes"].append("seat cleared: the worker's process had exited")
        return [[self._script("hive-abandon"), task, "--reason", reason]]

    def _prepare_launch(self, run: str, task: str, params: Mapping[str, Any], board: Board,
                  receipt: dict[str, Any]) -> list[list[str]]:
        order = _text(params, "order_path")
        text = self._hub_text(f"main:{order}")
        if text is None:
            raise Refused(f"{order} is not published on the hub's main")
        if task in board:
            raise Refused(f"task '{task}' already exists on run '{run}' "
                          f"({board[task]['status']})")
        route = _text(params, "route", required=False)
        route = route or published.order_route(text) or ""
        cap = self.d["bounds"]["max_concurrent"]
        if cap is not None:
            live = self.seats.live_workers(published.orders(self.hub))
            if len(live) >= cap:
                raise Refused(f"{len(live)} workers are live ({', '.join(live)}); "
                              f"the deployment allows {cap} at once")
        receipt["route"] = route or "(catalog default)"
        launch = [self._script("hive-launch"), order, *(["--route", route] if route else [])]
        # In a transient scope of its own, so a tmux server this launch starts never lives
        # in this service's cgroup: restarting the service must not kill the workers.
        return [["git", "-C", self.d["operator_workspace"], "pull", "--ff-only", "--quiet"],
                ["systemd-run", "--user", "--scope", "--quiet", "--", *launch]]


def _require_status(board: Board, task: str, allowed: set[str]) -> None:
    status = board.get(task, {}).get("status")
    if status not in allowed:
        raise Refused(f"task is {status or 'not on the board'}; this needs "
                      f"{' or '.join(sorted(allowed))}")


def _checks(pr: Mapping[str, Any]) -> str:
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return "absent"
    states = [c.get("conclusion") or c.get("state") or c.get("status")
              for c in rollup if isinstance(c, dict)]
    if all(s in GREEN for s in states):
        return "green"
    return "pending" if any(s in {"PENDING", "IN_PROGRESS", "QUEUED", None, ""}
                            for s in states) else "red"
