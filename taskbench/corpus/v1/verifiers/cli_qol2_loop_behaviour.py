#!/usr/bin/env python3
"""cli-qol-2: drive the operator loop scripts against a fixture and check what they DO.

Usage: cli_qol2_loop_behaviour.py <repo-root>

This is a behavioural check, not a code read. It builds a disposable fixture — a fake
workspace clone with its own hub, a project, two orders, a calibration file already
carrying the two duplicated rows the order names, and a recording stub for the hive CLI
— and then runs the candidate's own `scripts/hive-close`, `scripts/hive-score` and
`scripts/hive-answer` against it, asserting the definition-of-done cases a fixture can
drive: (a) through (f). Case (g) — that the legacy text-answer path and the safe resume
stay green — is NOT driven here and is a rubric leg, because the resume needs a live
worker's turn state, a whole subsystem this order does not touch. What IS asserted of the
answer path is its verification half and its no-write stop-line.

Why a stub CLI rather than a spine: `hive-common.sh` already carries `HIVE_CLI_CMD`, the
seam an operator uses to run the CLI on the host instead of in the container. Pointing it
at a script that answers `report` and `board-view` from canned JSON and records `emit`
calls reproduces every input these scripts read, with no database and no spine write. The
same reasoning covers `tmux`: a recording stub ahead of the real binary means the resume
path is exercised and observed, and no session is ever created on the host's tmux server.
That is not a theoretical precaution — an earlier benchmark cell ran a drill that made a
session on the server holding every live worker pane.

The checks are the order's own DoD cases (a) through (g). None of them looks at how the
scripts are written; each states an input and the outcome the order requires.

Exit 0 when every case passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

COMPOSE_STUB = r"""#!/usr/bin/env bash
# Recording stub for the container-compose command. The scripts of this task's era reach
# the spine as `<compose> run --rm -T cli <args>` and have no host-side seam of their own,
# so the seam used here is the one they DO have: OMEGAHIVE_COMPOSE, which hive-common.sh
# takes at its word rather than probing. Everything up to and including the service name
# is dropped and the rest is answered by the CLI stub beside this file.
set -u
printf 'compose %s\n' "$*" >> "$TASKBENCH_STUB_LOG"
seen_cli=0
args=()
for a in "$@"; do
  if [ "$seen_cli" -eq 1 ]; then args+=("$a"); continue; fi
  [ "$a" = "cli" ] && seen_cli=1
done
[ "$seen_cli" -eq 1 ] || { echo "compose stub: no cli service in: $*" >&2; exit 1; }
exec "$(dirname "$0")/hivecli" "${args[@]}"
"""

CLI_STUB = r"""#!/usr/bin/env bash
# Recording stub for the hive CLI. Answers the two read commands taskbench's fixture
# needs and records every emit; it never touches a database.
set -u
LOG="$TASKBENCH_STUB_LOG"
printf '%s\n' "$*" >> "$LOG"
case "${1:-}" in
  report)      cat "$TASKBENCH_STUB_EVENTS" ;;
  board-view)  cat "$TASKBENCH_STUB_BOARD" ;;
  emit)        echo "emitted · stub" ;;
  *)           echo "stub: unhandled $*" >&2; exit 1 ;;
esac
"""

TMUX_STUB = r"""#!/usr/bin/env bash
# Recording stub for tmux. The real binary would create sessions on this host's tmux
# server, which is where every live worker pane lives.
set -u
printf 'tmux %s\n' "$*" >> "$TASKBENCH_STUB_LOG"
case "${1:-}" in
  has-session)   exit 1 ;;
  list-windows)  exit 0 ;;
  new-session)   echo "%0" ;;
  display*)      echo "%0" ;;
  *)             exit 0 ;;
esac
"""

ORDER = """\
# Order: {task} — a fixture order

- **Project:** demo (run `demorun`)

## Scope

1. Do the fixture thing.

## Definition of done

- The fixture thing is done.

## Predictions

- Expected effort: 1-2 worker-hours — fixture.
- Expected questions: 0-1 — fixture.
- Expected review outcome: minor rework — fixture.
- Named risks: none, it is a fixture.

## Answers
"""

CALIBRATION = """\
# Prediction calibration — demo

Append-only. One entry per scoring act (`hive-score <task>`), newest at the bottom.

### alpha — closed 2026-01-05

| field | predicted | actual | verdict |
|---|---|---|---|
| effort | 1-2h | 1.5h | hit |
| questions | 0-1 | 0 | hit |
| review outcome | minor rework | review.failed x0 | minor rework |

### beta — closed 2026-01-06

| field | predicted | actual | verdict |
|---|---|---|---|
| effort | 1-2h | 1.5h | hit |
| questions | 0-1 | 0 | hit |
| review outcome | minor rework | review.failed x0 | unscored |

### alpha — closed 2026-01-07

| field | predicted | actual | verdict |
|---|---|---|---|
| effort | 1-2h | 1.5h | hit |
| questions | 0-1 | 0 | hit |
| review outcome | minor rework | review.failed x0 | unscored |
"""


def git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def events(task_review_failed: dict[str, int], closed: bool = True) -> str:
    """A spine history the scripts can read.

    `hive-score` derives existence and closure from the events themselves rather than from
    the board, so a task needs its whole arc — created, accepted, result posted, closed —
    plus however many `review.failed` events the case under test needs.
    """
    rows: list[dict] = []
    seq = 0

    def add(task: str, etype: str, payload: dict) -> None:
        nonlocal seq
        seq += 1
        rows.append(
            {
                "seq": seq, "event_type": etype, "task_id": task, "run_id": "demorun",
                "role": "worker", "actor_id": f"sess-{task}",
                "actor": {"role": "worker", "id": f"sess-{task}"},
                "payload": payload, "logical_ts": 1_770_000_000 + seq * 600,
                "created_at": "2026-01-05T00:00:00Z",
            }
        )

    for task, failed in task_review_failed.items():
        add(task, "task.created", {"title": task})
        add(task, "task.accepted", {})
        add(task, "task.result_posted",
            {"artifact_refs": [{"ref": f"projects/demo/reports/{task}.md@" + "a" * 40,
                                "quality": "ok"}]})
        for _ in range(failed):
            add(task, "review.failed", {"reason": "fixture"})
        if closed:
            add(task, "review.passed", {"ref_result": f"projects/demo/reports/{task}.md@"
                                        + "a" * 40})
            add(task, "task.status_override", {"status": "done", "reason": "fixture"})
    return json.dumps(rows)


class Fixture:
    """A disposable workspace + hub + stub tooling, torn down by the caller."""

    def __init__(self, root: Path, repo: Path) -> None:
        self.root = root
        self.repo = repo
        self.ws = root / "workspace"
        self.hub = root / "hub.git"
        self.bin = root / "bin"
        self.canon = root / "canon"
        self.log = root / "stub.log"
        self.events = root / "events.json"
        self.board = root / "board.json"

    def build(self) -> None:
        (self.ws / "projects" / "demo" / "orders").mkdir(parents=True)
        (self.ws / "projects" / "demo" / "metrics").mkdir(parents=True)
        (self.ws / "projects" / "demo" / "reports").mkdir(parents=True)
        (self.ws / "projects" / "demo" / "project.conf").write_text(
            "RUN_ID=demorun\nCODE_REPO=demo/code\n"
        )
        for task in ("alpha", "beta"):
            (self.ws / "projects" / "demo" / "orders" / f"2026-01-01-{task}.md").write_text(
                ORDER.format(task=task)
            )
        (self.ws / "projects" / "demo" / "metrics" / "calibration.md").write_text(CALIBRATION)

        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                        str(self.hub)], check=True, capture_output=True)
        git(self.ws, "init", "--quiet", "--initial-branch=main")
        git(self.ws, "config", "user.name", "taskbench fixture")
        git(self.ws, "config", "user.email", "taskbench@localhost")
        git(self.ws, "remote", "add", "origin", str(self.hub))
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "--quiet", "-m", "fixture", "--no-verify")
        git(self.ws, "push", "--quiet", "-u", "origin", "main")

        (self.canon / "code").mkdir(parents=True)
        git(self.canon / "code", "init", "--quiet", "--initial-branch=main")

        self.bin.mkdir()
        for name, body in (
            ("hivecli", CLI_STUB), ("hivecompose", COMPOSE_STUB), ("tmux", TMUX_STUB)
        ):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.set_board({"alpha": "in_review", "beta": "in_review"})
        self.set_events({"alpha": 0, "beta": 2})
        self.log.write_text("")

    def set_events(self, failed: dict[str, int], closed: bool = True) -> None:
        self.events.write_text(events(failed, closed))

    def set_board(self, statuses: dict[str, str]) -> None:
        self.board.write_text(
            json.dumps(
                [{"task": t, "status": st, "owner": f"sess-{t}"} for t, st in statuses.items()]
            )
        )

    def env(self) -> dict[str, str]:
        e = dict(os.environ)
        e.update(
            {
                "OPS_WS": str(self.ws),
                "WS_HUB": str(self.hub),
                "CANON_ROOT": str(self.canon),
                "OMEGA_DIR": str(self.repo),
                "WORK_ROOT": str(self.root / "work"),
                "HIVE_TMUX_SESSION": "taskbench-fixture",
                # Three seams, because the scripts changed which one they offer over the
                # window this corpus spans and a candidate may reasonably reach the spine
                # through any of them. Setting all three means the check measures the
                # candidate's behaviour rather than its choice of seam.
                "HIVE_CLI_CMD": str(self.bin / "hivecli"),
                "OMEGAHIVE_COMPOSE": str(self.bin / "hivecompose"),
                "HIVE_SPINE_JSON": str(self.events),
                "TASKBENCH_STUB_LOG": str(self.log),
                "TASKBENCH_STUB_EVENTS": str(self.events),
                "TASKBENCH_STUB_BOARD": str(self.board),
                "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "GIT_AUTHOR_NAME": "taskbench fixture",
                "GIT_AUTHOR_EMAIL": "taskbench@localhost",
                "GIT_COMMITTER_NAME": "taskbench fixture",
                "GIT_COMMITTER_EMAIL": "taskbench@localhost",
            }
        )
        return e

    def run(self, script: str, *args: str, timeout: int = 180):
        return subprocess.run(
            [str(self.repo / "scripts" / script), *args],
            capture_output=True, text=True, env=self.env(), cwd=str(self.root),
            check=False, timeout=timeout, stdin=subprocess.DEVNULL,
        )

    def calibration(self) -> str:
        return (self.ws / "projects" / "demo" / "metrics" / "calibration.md").read_text()

    def rows_for(self, task: str) -> int:
        return len(re.findall(rf"^### {re.escape(task)} — ", self.calibration(), re.M))

    def entry_for(self, task: str) -> str:
        """One task's calibration entry, ending at the next task's heading."""
        cal = self.calibration()
        m = re.search(rf"^### {re.escape(task)} — .*$", cal, re.M)
        if not m:
            return ""
        rest = cal[m.end():]
        nxt = re.search(r"^### ", rest, re.M)
        return rest[: nxt.start()] if nxt else rest

    @staticmethod
    def review_verdict_in(entry: str) -> str:
        """The verdict cell of the review-outcome row — the column that records the human's
        judgement, not the column that quotes what the order predicted."""
        for line in entry.splitlines():
            if "review outcome" in line.lower() and line.count("|") >= 4:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 4:
                    return cells[-1].lower()
        return ""

    def emits(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith("emit ")]

    def emitted(self, event_type: str, task: str) -> bool:
        """Did a close really happen? `emit ` alone proves only that something was said."""
        return any(
            event_type in line and f"--task {task}" in line for line in self.emits()
        )


def case(findings: list[str], ok: bool, label: str, detail: str = "") -> None:
    if ok:
        print(f"pass  {label}")
    else:
        findings.append(f"FAIL {label}{': ' + detail if detail else ''}")


def main(repo_root: str) -> int:
    repo = Path(repo_root).resolve()
    for script in ("hive-close", "hive-score", "hive-answer", "hive-common.sh"):
        if not (repo / "scripts" / script).is_file():
            print(f"FAIL missing: scripts/{script}")
            return 1
    for tool in ("git", "jq", "awk"):
        if shutil.which(tool) is None:
            print(f"FAIL environment: {tool} is not on PATH; this check cannot run")
            return 1

    root = Path(tempfile.mkdtemp(prefix="taskbench-cliqol2-"))
    findings: list[str] = []
    try:
        f = Fixture(root, repo)
        f.build()

        # (a) --review is required before a close, and its value is validated. The
        #     refusal must happen before any spine write: a close that emitted and then
        #     complained about its own argument is the failure this case exists to catch.
        before = len(f.emits())
        r = f.run("hive-close", "alpha")
        case(findings, r.returncode != 0 and "--review" in (r.stdout + r.stderr),
             "(a) close with no --review refuses", (r.stdout + r.stderr)[-300:])
        case(findings, len(f.emits()) == before, "(a) that refusal wrote nothing to the spine")

        r = f.run("hive-close", "alpha", "--review", "brilliant")
        combined = r.stdout + r.stderr
        case(findings,
             r.returncode != 0 and "clean" in combined and "rework" in combined,
             "(a) close with an invalid --review names the legal values", combined[-300:])
        case(findings, len(f.emits()) == before, "(a) the invalid verdict wrote nothing either")

        # (a) --no-score still closes, and records no calibration entry.
        rows_before = f.rows_for("alpha")
        r = f.run("hive-close", "alpha", "--no-score")
        case(findings, r.returncode == 0, "(a) --no-score still closes",
             (r.stdout + r.stderr)[-400:])
        case(findings,
             f.emitted("review.passed", "alpha") and f.emitted("task.status_override", "alpha"),
             "(a) --no-score still emitted both governed close events",
             "; ".join(f.emits()[-4:]))
        case(findings, f.rows_for("alpha") == rows_before,
             "(a) --no-score recorded no calibration entry")

        # (d)+(e) One task, one row. The fixture starts with alpha duplicated, which is
        #     the shape both pln tasks were left in. A replacement leaves exactly one.
        r = f.run("hive-score", "alpha", "--again", "--review", "rework", "--no-commit")
        case(findings, r.returncode == 0, "(d) --again on a duplicated task succeeds",
             (r.stdout + r.stderr)[-400:])
        case(findings, f.rows_for("alpha") == 1,
             "(d) --again leaves exactly one row for the task",
             f"{f.rows_for('alpha')} rows remain")
        case(findings, f.rows_for("beta") == 1, "(d) it left the other task's row alone")

        # (d) With no explicit --review, a replacement carries the newest human verdict
        #     forward instead of silently downgrading it to unscored.
        r = f.run("hive-score", "alpha", "--again", "--no-commit")
        alpha_entry = f.entry_for("alpha")
        verdict = f.review_verdict_in(alpha_entry)
        case(findings, r.returncode == 0, "(d) --again with no --review succeeds",
             (r.stdout + r.stderr)[-400:])
        # The VERDICT cell of the review-outcome row, not "does the word appear somewhere in
        # the entry": every alpha entry quotes `minor rework` in its PREDICTED column, so a
        # substring test over the whole entry would pass whatever the verdict became — and
        # the entry used to be sliced to end of file, which swept in the next task's row too.
        case(findings, verdict == "rework",
             "(d) it carried the newest human verdict forward rather than writing unscored",
             f"the review-outcome verdict cell reads {verdict!r}; entry was {alpha_entry[:300]}")

        # (c) A false clean is refused, and the refusal writes no row.
        cal_before = f.calibration()
        r = f.run("hive-score", "beta", "--again", "--review", "clean", "--no-commit")
        combined = r.stdout + r.stderr
        case(findings, r.returncode != 0 and "clean" in combined,
             "(c) --review clean against a nonzero review.failed count is refused",
             combined[-400:])
        case(findings, f.calibration() == cal_before,
             "(c) the refused clean changed no calibration row")

        # (b) A valid close produces one scored row; and an injected post-close scoring
        #     failure leaves the close intact and says so loudly. `beta` is that
        #     injection: it is closeable, and its scoring leg refuses the false clean.
        # `closed=False` here: the scoring cases above need a task the events show as
        # CLOSED, and a close case needs one they do not. Presenting both at once — a board
        # saying in_review over an event history saying done — would let an implementation
        # that reads the history correctly refuse, and one that trusts a stale board pass.
        f.set_events({"alpha": 0, "beta": 0}, closed=False)
        rows_before = f.rows_for("alpha")
        r = f.run("hive-close", "alpha", "--review", "minor rework")
        case(findings, r.returncode == 0, "(b) a valid close succeeds",
             (r.stdout + r.stderr)[-400:])
        case(findings,
             f.emitted("review.passed", "alpha") and f.emitted("task.status_override", "alpha"),
             "(b) and the close really happened — both governed events, on this task",
             "; ".join(f.emits()[-4:]))
        case(findings, f.rows_for("alpha") == 1, "(b) and leaves exactly one scored row")

        f.set_events({"alpha": 0, "beta": 3}, closed=False)
        cal_before = f.calibration()
        r = f.run("hive-close", "beta", "--review", "clean")
        combined = r.stdout + r.stderr
        case(findings, r.returncode == 0,
             "(b) a close whose scoring leg fails still succeeds", combined[-400:])
        case(findings,
             "WARNING" in combined and "hive-score" in combined,
             "(b) and says so loudly, naming the one recovery command", combined[-500:])
        case(findings, f.calibration() == cal_before,
             "(b) the failed scoring leg wrote no calibration row")

        # (f) --sha verifies an already-pushed append and refuses everything else.
        findings += sha_cases(f)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for x in findings:
        print(x)
    if findings:
        print(f"\n{len(findings)} case(s) failed")
        return 1
    print("\nok: every DoD case the fixture can drive behaves as the order requires")
    return 0


def sha_cases(f: Fixture) -> list[str]:
    """DoD (f): what `hive-answer <task> --sha` accepts and what it refuses."""
    findings: list[str] = []
    rel = "projects/demo/orders/2026-01-01-alpha.md"
    order = f.ws / rel

    def commit_push(message: str) -> str:
        (f.ws / "projects" / "demo" / "reports").mkdir(parents=True, exist_ok=True)
        git(f.ws, "add", "-A")
        git(f.ws, "commit", "--quiet", "-m", message, "--no-verify")
        git(f.ws, "push", "--quiet", "origin", "main")
        return git(f.ws, "rev-parse", "HEAD").strip()

    # A clean append below the heading — the one shape --sha exists to bless.
    order.write_text(order.read_text() + "\nThe fixture answer, appended below the heading.\n")
    good = commit_push("answer: append below the heading")

    # A mixed commit: the order plus another file.
    order.write_text(order.read_text() + "\nA second appended line.\n")
    (f.ws / "projects" / "demo" / "reports" / "note.md").write_text("unrelated\n")
    mixed = commit_push("answer plus an unrelated file")

    # An edit above the heading — a pure INSERTION, so the deletion check cannot fire
    # first and mask the one this case is testing.
    text = order.read_text().replace(
        "## Answers", "A note inserted above the heading.\n\n## Answers", 1
    )
    order.write_text(text + "\nA third appended line.\n")
    above = commit_push("answer, and a note inserted above the heading")

    # A deletion.
    lines = order.read_text().splitlines(keepends=True)
    order.write_text("".join(lines[:-1]) + "A replacement final line.\n")
    deletion = commit_push("answer that also deletes a line")

    # A merge commit reachable from main.
    git(f.ws, "checkout", "--quiet", "-b", "side", good)
    (f.ws / "projects" / "demo" / "reports").mkdir(parents=True, exist_ok=True)
    (f.ws / "projects" / "demo" / "reports" / "side.md").write_text("side\n")
    git(f.ws, "add", "-A")
    git(f.ws, "commit", "--quiet", "-m", "side", "--no-verify")
    git(f.ws, "checkout", "--quiet", "main")
    git(f.ws, "merge", "--quiet", "--no-ff", "-m", "merge side", "side")
    merge = git(f.ws, "rev-parse", "HEAD").strip()
    git(f.ws, "push", "--quiet", "origin", "main")

    # A commit that exists locally but was never pushed.
    (f.ws / "projects" / "demo" / "reports" / "unpushed.md").write_text("unpushed\n")
    git(f.ws, "add", "-A")
    git(f.ws, "commit", "--quiet", "-m", "not pushed", "--no-verify")
    unpushed = git(f.ws, "rev-parse", "HEAD").strip()
    git(f.ws, "reset", "--quiet", "--hard", merge)

    refusals = [
        ("an abbreviated sha", good[:12], ("40", "abbreviated")),
        ("a mixed commit", mixed, ("mixed",)),
        ("an edit above the heading", above, ("above",)),
        ("a deletion", deletion, ("delete",)),
        ("a merge commit", merge, ("merge",)),
        ("an unreachable commit", unpushed, ("reachable", "not found")),
    ]
    for label, sha, needles in refusals:
        r = f.run("hive-answer", "alpha", "--sha", sha)
        combined = (r.stdout + r.stderr).lower()
        ok = r.returncode != 0 and any(n in combined for n in needles)
        if ok:
            print(f"pass  (f) --sha refuses {label}")
        else:
            findings.append(
                f"FAIL (f) --sha did not refuse {label} with a diagnosis naming it "
                f"(exit {r.returncode}): {combined[-300:]}"
            )

    # The accepting half. The resume that follows a successful verification needs a live
    # worker's turn state — a whole other subsystem, and one this order does not touch —
    # so what is asserted here is the verification itself and its stop-line: `--sha`
    # verifies an ALREADY-PUSHED commit and never stages, commits, rebases or pushes.
    # Whether the nudge then fires is a rubric question, stated as one.
    before_file = order.read_text()
    before_head = git(f.ws, "rev-parse", "HEAD").strip()
    before_hub = git(f.hub, "rev-parse", "main").strip()
    before_status = git(f.ws, "status", "--porcelain").strip()
    r = f.run("hive-answer", "alpha", "--sha", good)
    combined = (r.stdout + r.stderr).lower()
    if "verified" in combined and "not committing" in combined:
        print("pass  (f) --sha verifies one pushed append below the heading")
    else:
        findings.append(
            f"FAIL (f) --sha did not verify a clean pushed append (exit {r.returncode}): "
            f"{combined[-400:]}"
        )
    unchanged = (
        order.read_text() == before_file
        and git(f.ws, "rev-parse", "HEAD").strip() == before_head
        and git(f.hub, "rev-parse", "main").strip() == before_hub
        # The INDEX too: "never stages" is half of the stop-line, and a staged file leaves
        # the working tree, HEAD and the hub all exactly as they were.
        and git(f.ws, "status", "--porcelain").strip() == before_status
    )
    if unchanged:
        print("pass  (f) verifying wrote nothing: no stage, no commit, no push")
    else:
        findings.append(
            "FAIL (f) --sha modified the order, the clone or the hub. It verifies an "
            "already-pushed commit and is stop-lined against writing anything."
        )
    return findings


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
