"""A miniature corpus and a pair of scripted agents, for exercising taskbench without models.

The real corpus needs two repositories, a container stack and a Postgres. The tests need
neither: they need a task-shaped thing with a baseline, an order, a verifier and a rubric,
so that the harness's own invariants — fresh roots, no later history, argv safety, record
validation, held-out refusal, blinded ids, read denial — are what is under test.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import yaml

DUMMY_AGENT = """\
#!/usr/bin/env python3
'''A scripted candidate: reads the order, writes the file it asks for, commits.'''
import pathlib, subprocess, sys, os
root = pathlib.Path(os.environ["BENCH_CELL_ROOT"])
print("agent: starting", flush=True)
(root / "code" / "GREETING.txt").write_text("hello from the candidate\\n")
subprocess.run(["git", "add", "-A"], cwd=root / "code", check=True)
subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "work"],
               cwd=root / "code", check=True)
subprocess.run([str(root / "bin" / "bench-verify"), "true"], check=False)
print("agent: done", flush=True)
"""

IDLE_AGENT = """\
#!/usr/bin/env python3
'''A candidate that changes nothing — the shape of a cell that must go red.'''
print("agent: nothing to do", flush=True)
"""

DUMMY_REVIEWER = """\
#!/usr/bin/env python3
'''A scripted reviewer: passes when the artefact exists in the patch it was given.'''
import json, pathlib, os
packet = pathlib.Path(os.environ.get("TASKBENCH_PACKET", "."))
cell = json.loads((packet / "cell.json").read_text())["cell_id"]
patch = (packet / "candidate.patch").read_text()
defects = []
if "GREETING.txt" not in patch:
    defects.append({"summary": "the order's artefact is absent from the patch",
                    "why_blocking": "the order is not closed", "evidence": "candidate.patch"})
(packet / "verdict.json").write_text(json.dumps({
    "cell_id": cell,
    "would_have_shipped_defects": defects,
    "dod_legs": [{"leg": "greeting", "met": "no" if defects else "yes", "note": ""}],
    "verdict": "fail" if defects else "pass",
}))
print("reviewer: wrote verdict")
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def make_source_repo(tmp: Path) -> tuple[Path, str, str]:
    """A code repo with a pre-task baseline and one later commit that must never leak."""
    repo = tmp / "src-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "user.email", "fixture@localhost")
    (repo / "README.md").write_text("# fixture\n")
    (repo / "keep.txt").write_text("baseline\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "GREETING.txt").write_text("hello from the FUTURE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the solution nobody may see")
    solution = _git(repo, "rev-parse", "HEAD").strip()
    return repo, base, solution


def make_workspace_repo(tmp: Path, *, extra: dict[str, str] | None = None) -> tuple[Path, str]:
    ws = tmp / "ws-repo"
    (ws / "projects/demo/orders").mkdir(parents=True)
    _git_init = ["git", "-C", str(ws), "init", "--quiet", "--initial-branch=main"]
    subprocess.run(_git_init, check=True)
    _git(ws, "config", "user.name", "fixture")
    _git(ws, "config", "user.email", "fixture@localhost")
    (ws / "projects/demo/orders/order.md").write_text(
        textwrap.dedent(
            """\
            # Order: greeting

            Write `GREETING.txt` at the repository root containing a greeting, and commit it.

            ## Definition of done

            The file exists and is committed.
            """
        )
    )
    (ws / "projects/demo/PROTOCOL.md").write_text("# Protocol\n\nDo what the order says.\n")
    for rel, text in (extra or {}).items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(ws, "add", "-A")
    _git(ws, "commit", "--quiet", "-m", "orders")
    return ws, _git(ws, "rev-parse", "HEAD").strip()


def make_corpus(
    tmp: Path,
    *,
    source_repo: Path,
    base_sha: str,
    solution_sha: str,
    ws_repo: Path,
    ws_sha: str,
    held_out: list[str] | None = None,
    extra_verifiers: list[dict] | None = None,
    extra_inputs: list[dict] | None = None,
) -> Path:
    root = tmp / "corpus"
    for sub in ("tasks", "rubrics", "grading"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    (root / "corpus.yaml").write_text(
        yaml.safe_dump(
            {
                "corpus_version": "fixture",
                "frozen_on": "2026-08-12",
                "description": "fixture corpus",
                "held_in": ["greeting"],
                "held_out": held_out or ["reserved"],
            }
        )
    )

    def manifest(task_id: str) -> dict:
        return {
            "id": task_id,
            "project": "demo",
            "run": "demo",
            "title": f"{task_id} — fixture task",
            "task_class": "bounded",
            "work_shape": "python-service",
            "bounded_because": "One file, one check.",
            "workspace_repo": str(ws_repo),
            "workspace_inputs": [
                {"path": "projects/demo/orders/order.md", "sha": ws_sha, "role": "order"},
                {"path": "projects/demo/PROTOCOL.md", "sha": ws_sha, "role": "context"},
                *(extra_inputs or []),
            ],
            "code": {
                "repo": "fixture/code",
                "pre_task_base_sha": base_sha,
                "local_path": str(source_repo),
            },
            "expected_output_kinds": ["code_patch"],
            "required_artefacts": ["GREETING.txt"],
            "environment_needs": ["nothing"],
            "verifiers": [
                {
                    "id": "greeting-exists",
                    "argv": ["python3", "-c", "import pathlib,sys;"
                             "sys.exit(0 if pathlib.Path('GREETING.txt').is_file() else 1)"],
                    "description": "the artefact the order names exists",
                },
                *(extra_verifiers or []),
            ],
            "rubric": f"rubrics/{task_id}.md",
            "grading": f"grading/{task_id}.yaml",
        }

    for task_id in ["greeting", *(held_out or ["reserved"])]:
        (root / "tasks" / f"{task_id}.yaml").write_text(yaml.safe_dump(manifest(task_id)))
        (root / "rubrics" / f"{task_id}.md").write_text(
            "# Rubric\n\nPass when GREETING.txt exists and the order is closed.\n"
        )
        (root / "grading" / f"{task_id}.yaml").write_text(
            yaml.safe_dump(
                {
                    "task_id": task_id,
                    "accepted_outcome": ["A greeting file exists at the repository root."],
                    "known_defect_classes": [],
                    "source_refs": ["fixture"],
                    "historical_solution_sha": solution_sha,
                }
            )
        )
    return root


def write_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def specs(tmp: Path, *, agent_body: str = DUMMY_AGENT, sandbox: list[str] | None = None):
    """An AgentSpec/ReviewerSpec pair driving the scripted agents above."""
    from taskbench.review import DEFAULT_SANDBOX_ARGV, ReviewerSpec
    from taskbench.runner import AgentSpec

    bindir = tmp / "scripts"
    bindir.mkdir(parents=True, exist_ok=True)
    agent = write_script(bindir / "agent.py", agent_body)
    reviewer = write_script(bindir / "reviewer.py", DUMMY_REVIEWER)
    return (
        AgentSpec(
            argv=["python3", str(agent)],
            labels={"vendor": "fixture", "model": "scripted-1", "harness": "pytest"},
            prompt_mode="file",
            timeout_s=120,
        ),
        ReviewerSpec(
            argv=["python3", str(reviewer)],
            labels={"vendor": "fixture", "model": "scripted-reviewer", "harness": "pytest"},
            sandbox_argv=DEFAULT_SANDBOX_ARGV if sandbox is None else sandbox,
            sandbox_ro_binds=[str(bindir)],
            timeout_s=120,
            prompt_mode="file",
        ),
    )


def bwrap_available() -> bool:
    try:
        return (
            subprocess.run(
                ["bwrap", "--dev-bind", "/", "/", "true"], capture_output=True, check=False
            ).returncode
            == 0
        )
    except FileNotFoundError:
        return False


__all__ = [
    "DUMMY_AGENT",
    "DUMMY_REVIEWER",
    "IDLE_AGENT",
    "bwrap_available",
    "make_corpus",
    "make_source_repo",
    "make_workspace_repo",
    "specs",
    "write_script",
]


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())
