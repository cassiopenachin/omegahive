"""Build a candidate root without handing the agent its own answer.

The failure this module exists to prevent: a full `git clone` of the code repo puts every
later commit in the candidate's object database, so the eventual solution is one
`git log --all` away. Materialization therefore **exports** rather than clones — the
pre-task tree is extracted from the source repository and committed into a brand-new
repository with exactly one commit and no remotes.

Cell root layout::

    <cell>/code/                the single-baseline repository (the candidate works here)
    <cell>/workspace/           the pinned workspace inputs, and nothing else
    <cell>/deps/                exported dependency snapshots (git bundles)
    <cell>/bin/bench-verify     shim that timestamps the candidate's first verifier run
    <cell>/TASK.md              the kickoff the agent is launched against
    <cell>/operator-only/       the historical solution — a canary, NEVER in the packet
                                and never reachable from the candidate root's siblings

`operator-only/` sits inside the cell but outside `code/` and `workspace/`; the
cold-reader probe (`taskbench.review`) asserts the review process cannot read it. It is
written by the grader, not by materialization, so a candidate run never has it on disk at
all — see `write_operator_only_solution`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import TaskManifest

#: A materialized repo carries this instead of the real history, so a candidate that
#: reasons about "what came before" gets an honest answer rather than a silent lie.
BASELINE_NOTE = """\
This repository is a single-baseline export prepared for a task replay.

It contains exactly one commit, holding the tree of the historical pre-task base. The
original history and every later commit are deliberately absent: the task is to produce a
valid outcome, not to find the one that was produced before.

Original base commit: {base_sha}
Source repository:    {repo}
"""

VERIFY_SHIM = """\
#!/bin/sh
# bench-verify — run this task's verifier. The first invocation is timestamped so the
# record can report when verification started; the exit status is the verifier's own.
# A leading `--` is accepted and dropped, because that is how the kickoff spells it.
[ "$1" = "--" ] && shift
printf '%s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$BENCH_VERIFY_LOG"
exec "$@"
"""


class MaterializationError(RuntimeError):
    pass


@dataclass
class Materialized:
    """Where everything landed, plus the leakage scan's verdict."""

    root: Path
    code: Path
    workspace: Path
    deps: Path
    baseline_sha: str
    exported_inputs: list[str] = field(default_factory=list)
    exported_deps: list[str] = field(default_factory=list)
    leakage_violations: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and out.returncode != 0:
        raise MaterializationError(
            f"git {' '.join(args)} in {repo} failed ({out.returncode}): {out.stderr.strip()}"
        )
    return out.stdout


def _export_tree(source_repo: Path, sha: str, dest: Path) -> None:
    """Extract one commit's tree into `dest` via `git archive` — no objects travel."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / f".{dest.name}.tar"
    with archive.open("wb") as fh:
        proc = subprocess.run(
            ["git", "-C", str(source_repo), "archive", "--format=tar", sha],
            stdout=fh,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        archive.unlink(missing_ok=True)
        raise MaterializationError(
            f"git archive {sha} in {source_repo} failed: {proc.stderr.decode().strip()}"
        )
    with tarfile.open(archive) as tf:
        tf.extractall(dest, filter="data")
    archive.unlink()


def _init_single_baseline(dest: Path, base_sha: str, repo: str) -> None:
    """Turn an exported tree into a one-commit repository with no remotes."""
    _git(dest, "init", "--quiet", "--initial-branch=main")
    _git(dest, "config", "user.name", "taskbench materializer")
    _git(dest, "config", "user.email", "taskbench@localhost")
    (dest / ".git" / "BASELINE").write_text(BASELINE_NOTE.format(base_sha=base_sha, repo=repo))
    _git(dest, "add", "-A")
    _git(dest, "commit", "--quiet", "-m", f"baseline: {repo} pre-task tree", "--no-verify")


def _resolve_source(pin_local_path: str | None, repo: str, overrides: dict[str, str]) -> Path:
    """Find the local object source for a pinned repo — a deployment fact, overridable."""
    # Most-to-least specific: a launch-time override, the manifest's hint, and finally the
    # repo identifier itself when it happens to be a path (which is how tests and one-off
    # materializations name a local clone).
    candidate = overrides.get(repo) or pin_local_path
    if not candidate and (Path(os.path.expanduser(repo)) / ".git").exists():
        candidate = repo
    if not candidate:
        raise MaterializationError(
            f"no local source for {repo}: set it in the manifest's `local_path` or pass "
            "--source-repo <repo>=<path>"
        )
    path = Path(os.path.expanduser(candidate))
    if not (path / ".git").exists() and not (path / "HEAD").exists():
        raise MaterializationError(f"{path} is not a git repository (source for {repo})")
    return path


def materialize(
    manifest: TaskManifest,
    cell_root: str | Path,
    *,
    source_repos: dict[str, str] | None = None,
    workspace_repo_path: str | None = None,
) -> Materialized:
    """Build one candidate root from a manifest. Refuses to reuse a non-empty root."""
    overrides = dict(source_repos or {})
    root = Path(cell_root)
    if root.exists() and any(root.iterdir()):
        raise MaterializationError(f"{root} already exists and is not empty; cells are fresh")
    root.mkdir(parents=True, exist_ok=True)

    code, workspace, deps = root / "code", root / "workspace", root / "deps"

    source = _resolve_source(manifest.code.local_path, manifest.code.repo, overrides)
    _export_tree(source, manifest.code.pre_task_base_sha, code)
    _init_single_baseline(code, manifest.code.pre_task_base_sha, manifest.code.repo)

    ws_source = _resolve_source(
        workspace_repo_path, manifest.workspace_repo, overrides
    )
    workspace.mkdir(parents=True, exist_ok=True)
    exported_inputs: list[str] = []
    for item in manifest.workspace_inputs:
        blob = subprocess.run(
            ["git", "-C", str(ws_source), "show", f"{item.sha}:{item.path}"],
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            raise MaterializationError(
                f"workspace input {item.path}@{item.sha[:7]} is unresolvable in {ws_source}: "
                f"{blob.stderr.decode().strip()}"
            )
        target = workspace / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)
        exported_inputs.append(item.path)

    deps.mkdir(parents=True, exist_ok=True)
    exported_deps = [_export_dependency(d, deps, overrides) for d in manifest.dependency_snapshots]

    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "bench-verify"
    shim.write_text(VERIFY_SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    m = Materialized(
        root=root,
        code=code,
        workspace=workspace,
        deps=deps,
        baseline_sha=manifest.code.pre_task_base_sha,
        exported_inputs=exported_inputs,
        exported_deps=[d for d in exported_deps if d],
    )
    m.leakage_violations = leakage_scan(m, manifest)
    return m


def _export_dependency(dep, deps_dir: Path, overrides: dict[str, str]) -> str | None:
    """Snapshot one dependency. Images and volumes are asserted, never fetched."""
    if dep.kind == "git_bundle":
        src = _resolve_source(dep.local_path, dep.ref, overrides)
        bundle = deps_dir / f"{dep.name}.bundle"
        rev = dep.sha or "HEAD"
        # A bundle needs a named ref, and a bare sha has none. Push the pinned commit into a
        # scratch bare repo under a ref of our own and bundle from there: the source repo is
        # never mutated, and the bundle carries the pinned commit's ancestry and nothing else
        # — no later upstream work travels with it.
        scratch = deps_dir / f".{dep.name}.scratch.git"
        shutil.rmtree(scratch, ignore_errors=True)
        for cmd in (
            ["git", "init", "--quiet", "--bare", str(scratch)],
            ["git", "-C", str(src), "push", "--quiet", str(scratch), f"{rev}:refs/heads/pinned"],
            ["git", "-C", str(scratch), "bundle", "create", str(bundle), "refs/heads/pinned"],
        ):
            out = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if out.returncode != 0:
                shutil.rmtree(scratch, ignore_errors=True)
                raise MaterializationError(
                    f"snapshotting {dep.name} @{rev} from {src} failed at "
                    f"`{' '.join(cmd[:4])}`: {out.stderr.strip()}"
                )
        shutil.rmtree(scratch, ignore_errors=True)
        (deps_dir / f"{dep.name}.README.txt").write_text(
            f"{dep.name} pinned at {rev}, as an offline git bundle.\n\n"
            f"  git clone -b pinned deps/{bundle.name} <wherever you want it>\n\n"
            "Run that from the cell root. There is no network fetch of this dependency; the\n"
            "bundle is the only source, and it holds the pinned commit's ancestry and no more.\n"
        )
        return f"{dep.name}.bundle"
    if dep.kind == "image":
        probe = subprocess.run(
            ["podman", "image", "exists", dep.ref], capture_output=True, check=False
        )
        if probe.returncode != 0:
            raise MaterializationError(
                f"dependency image {dep.ref} is not in local container storage. The runner "
                "never pulls: load it first so the replay is honest about what it needed."
            )
        return None
    return None  # volume: an operator precondition, recorded in the manifest and the guide


# --- leakage --------------------------------------------------------------------

#: Filename fragments that mean "this file is about how a task was actually solved".
#: Deliberately generic; the task-specific half is `<task id>` + a report suffix, added
#: per-scan below. A sibling task's result report is legitimate context — the order may
#: cite one — so the rule is "this task's own answer", not "any report".
_LEAK_FRAGMENTS = ("solution", "grading/", "acceptance-facts", "held-out", "held_out")
_REPORT_SUFFIXES = ("-result.md", "-reflection.md", "-result-2.md")


def leakage_scan(m: Materialized, manifest: TaskManifest) -> list[str]:
    """Prove the candidate root cannot see the future. Empty list ⇒ clean.

    Four ways the future leaks, checked directly rather than trusted:
    a full object database, a stray remote, a result report or grader-only file copied in
    beside the order, and any manifest but this task's own.
    """
    violations: list[str] = []

    rev_list = _git(m.code, "rev-list", "--all", check=False).split()
    if len(rev_list) != 1:
        violations.append(
            f"code repo holds {len(rev_list)} commits; a single-baseline export has exactly 1"
        )
    if _git(m.code, "remote", check=False).strip():
        violations.append("code repo has a remote configured; a candidate root has none")

    allowed = {w.path for w in manifest.workspace_inputs}
    for p in m.workspace.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(m.workspace))
        if rel not in allowed:
            violations.append(f"undeclared workspace file in candidate root: {rel}")

    # The filename heuristic is deliberately NOT applied to `code/`. That tree is a verbatim
    # export of the pre-task commit, so nothing later can be in it by construction, and the
    # repository legitimately contains words like "acceptance". The heuristic's job is to
    # catch a stray file the materializer or an operator dropped in beside the order.
    for p in m.root.rglob("*"):
        if not p.is_file():
            continue
        cell_rel = p.relative_to(m.root)
        if cell_rel.parts and cell_rel.parts[0] == "code":
            continue
        low = str(cell_rel).lower()
        if any(frag in low for frag in _LEAK_FRAGMENTS):
            violations.append(f"solution-shaped file in candidate root: {cell_rel}")
        elif manifest.id in low and any(low.endswith(s) for s in _REPORT_SUFFIXES):
            violations.append(
                f"this task's own result report is in the candidate root: {cell_rel}"
            )
    return violations


def write_operator_only_solution(
    cell_root: str | Path,
    manifest: TaskManifest,
    solution_patch: str,
    *,
    mode: int = 0o600,
) -> Path:
    """Place the historical solution where the cold-reader probe can prove it unreadable.

    Written after the candidate has finished, so it is never on disk during the run. Its
    only jobs are to be a read-denial canary for the review sandbox and to give the
    operator a diagnosis aid when a cell goes red.
    """
    d = Path(cell_root) / "operator-only"
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{manifest.id}-historical-solution.patch"
    target.write_text(solution_patch)
    target.chmod(mode)
    (d / "README.txt").write_text(
        "Operator eyes only. This directory holds the historical solution for diagnosis and\n"
        "is a deliberate read-denial canary for the blinded review sandbox. It is never a\n"
        "review input and never an expected output: the benchmark asks whether ANOTHER valid\n"
        "outcome closes the order, and exact-diff scoring is a stop-line.\n"
    )
    return target


def clean_cell(cell_root: str | Path) -> None:
    """Remove a cell root. Used by tests; never by the runner, which keeps history."""
    shutil.rmtree(cell_root, ignore_errors=True)
