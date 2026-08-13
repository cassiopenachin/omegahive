"""Everything checkable before the first model call, checked before the first model call.

The batch costs real money and real operator attention, and a record written under the wrong
pins cannot be edited afterwards — it can only be superseded. So every precondition that can
be established locally is established locally, and any disagreement is a loud refusal rather
than a cell that fails halfway through for a reason nobody can attribute to the model.

The rule this module exists to enforce: **a red cell must mean the model**. Anything that
would make a cell red for an environmental reason belongs here instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .manifest import LoadedCorpus
from .review import ReviewerSpec, sandbox_wrapper
from .runner import AgentSpec

#: The canonical checkout the launcher must never touch or switch. Deployment fact; the
#: environment can override it, and the check is skipped only if the path does not exist.
CANONICAL_CHECKOUT = os.environ.get("TASKBENCH_CANONICAL_CHECKOUT", "~/src/SNET/omegahive")

#: Tools a held-in cell's own verifiers need. Absent means red cells that mean nothing.
REQUIRED_TOOLS = ("git", "uv", "python3", "shellcheck", "podman", "bwrap")


def _sha_present(repo: Path, sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, check=False,
    ).returncode == 0


def _blob_present(repo: Path, sha: str, path: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}:{path}"],
        capture_output=True, check=False,
    ).returncode == 0


def check_not_canonical(repo_root: Path) -> list[str]:
    """The launcher runs from the worker's clone, never the checkout the stack runs on."""
    canonical = Path(CANONICAL_CHECKOUT).expanduser()
    if not canonical.exists():
        return []
    try:
        if repo_root.resolve() == canonical.resolve():
            return [
                f"refusing to run from the canonical checkout {canonical}. Run from the "
                "worker's isolated clone; the canonical tree is the stack's and is never "
                "switched, built in, or written to by this batch."
            ]
    except OSError:
        return []
    return []


def check_checkout_clean(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return [f"{repo_root} is not a git checkout"]
    if out.stdout.strip():
        n = len(out.stdout.strip().splitlines())
        return [
            f"{repo_root} has {n} uncommitted change(s). The record pins the harness sha, so "
            "an unclean tree makes that pin a lie. Commit or stash first."
        ]
    return []


def check_corpus(corpus: LoadedCorpus, *, expect_hash: str, expect_held_in: list[str]) -> list[str]:
    problems: list[str] = []
    if corpus.content_hash != expect_hash:
        problems.append(
            f"corpus content hash is {corpus.content_hash}, the launch expects {expect_hash}. "
            "The corpus moved: either restore it or increment the corpus version, because a "
            "record pinning a hash nobody can reproduce is not evidence."
        )
    if list(corpus.catalog.held_in) != list(expect_held_in):
        problems.append(
            f"held-in set is {corpus.catalog.held_in}, the launch expects {expect_held_in}"
        )
    for tid in expect_held_in:
        if tid in corpus.catalog.held_out:
            problems.append(f"{tid} is held out and must never be run")
    return problems


def check_tools() -> list[str]:
    return [
        f"`{tool}` is not on PATH; a cell needing it would go red for the environment"
        for tool in REQUIRED_TOOLS
        if shutil.which(tool) is None
    ]


def check_agent_command(spec: AgentSpec, label: str) -> list[str]:
    problems: list[str] = []
    if not spec.argv:
        return [f"{label}: empty argv"]
    if shutil.which(spec.argv[0]) is None and not Path(spec.argv[0]).exists():
        problems.append(f"{label}: `{spec.argv[0]}` is not executable or on PATH")
    missing = spec.required_labels_present()
    if missing:
        problems.append(f"{label}: labels incomplete: {missing}")
    if spec.result_envelope == "claude-code-json" and "json" not in " ".join(spec.argv):
        problems.append(
            f"{label}: declares the claude-code-json result envelope but its argv never asks "
            "for it, so the record would carry no resolved model id and no token counts"
        )
    if spec.result_envelope is None and "--output-format" in spec.argv:
        # The converse, and the one that actually bit: the reviewer ran with --output-format
        # json and reported its spend on stdout, and the record threw it away because nothing
        # declared the envelope. Per-leg spend is what the economics clause needs.
        problems.append(
            f"{label}: its argv asks the harness for a JSON result envelope but the config "
            "declares no `result_envelope`, so its identity and spend would be dropped"
        )
    for arg in spec.argv:
        if any(ch in arg for ch in "|&;<>$`"):
            problems.append(
                f"{label}: argv element {arg!r} carries shell metacharacters. Commands are "
                "argv arrays and are never shell-evaluated; this looks assembled."
            )
    return problems


def check_reviewer_sandbox(spec: ReviewerSpec) -> list[str]:
    problems = check_agent_command(
        AgentSpec(
            argv=spec.argv, labels=spec.labels, prompt_mode=spec.prompt_mode,
            result_envelope=spec.result_envelope,
        ),
        "reviewer",
    )
    if not spec.sandbox_argv:
        problems.append(
            "reviewer: no sandbox wrapper. The cold-reader probe would fail every cell, and "
            "a review leg that never runs is not a review leg."
        )
    for path in [*spec.sandbox_ro_binds, *spec.sandbox_rw_binds]:
        if not Path(path).expanduser().exists():
            problems.append(f"reviewer: sandbox bind {path} does not exist")
    wrapper = sandbox_wrapper(spec, Path("/nonexistent-packet"))
    if wrapper and shutil.which(wrapper[0]) is None:
        problems.append(f"reviewer: sandbox wrapper `{wrapper[0]}` is not on PATH")
    return problems


#: A tiny TCP+TLS probe run *through the sandbox wrapper*. Found by doing: on this host
#: `/etc/resolv.conf` is a symlink into `/run`, so a sandbox that binds `/etc` but not the
#: resolver's runtime directory resolves nothing and the reviewer hangs until its timeout.
#: The cold-reader probe cannot catch that — it does no network — so a whole batch of review
#: legs would time out one by one with nothing saying why.
_REACH_PY = """
import socket, ssl, sys
host = sys.argv[1]
try:
    s = socket.create_connection((host, 443), timeout=20)
    ssl.create_default_context().wrap_socket(s, server_hostname=host).close()
    print("reachable")
except Exception as exc:
    print(f"unreachable: {type(exc).__name__}: {exc}")
"""


def check_sandbox_reachability(spec: ReviewerSpec, *, host: str = "api.anthropic.com") -> list[str]:
    """The reviewer must be able to reach its API from inside its own sandbox."""
    wrapper = sandbox_wrapper(spec, Path("/tmp"))
    if not wrapper:
        return []
    try:
        out = subprocess.run(  # noqa: S603 — argv list, shell=False
            [*wrapper, "python3", "-c", _REACH_PY, host],
            capture_output=True, text=True, check=False, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return [
            f"reviewer sandbox cannot reach {host}: the probe itself timed out. Every review "
            "leg would hang. On a systemd-resolved host this usually means the resolver's "
            "runtime directory is not bound into the sandbox."
        ]
    if "reachable" not in out.stdout:
        return [
            f"reviewer sandbox cannot reach {host}: {out.stdout.strip() or out.stderr.strip()}. "
            "Every review leg would fail for the environment, not for the work."
        ]
    return []


def check_destinations(*, work_root: Path, out_dir: Path, record_id: str, date: str) -> list[str]:
    problems: list[str] = []
    record = out_dir / f"{date}-{record_id}"
    if record.exists():
        problems.append(
            f"record {record} already exists. Records are immutable; a rerun takes a new id "
            "and names what it supersedes."
        )
    existing_cells = sorted(work_root.glob("cell-*")) if work_root.exists() else []
    if existing_cells:
        problems.append(
            f"work root {work_root} already holds {len(existing_cells)} cell root(s); cells are "
            "built fresh and this run would sit on top of an earlier one"
        )
    for parent in (work_root.parent, out_dir):
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"cannot create {parent}: {exc}")
            continue
        if not os.access(parent, os.W_OK):
            problems.append(f"{parent} is not writable")
    return problems


def check_manifest_pins(
    corpus: LoadedCorpus,
    task_ids: list[str],
    *,
    source_repos: dict[str, str],
    workspace_repo_path: str | None,
) -> list[str]:
    """Every sha the batch will need, resolvable now rather than mid-batch."""
    problems: list[str] = []
    ws = Path(workspace_repo_path).expanduser() if workspace_repo_path else None
    if ws is None or not (ws / ".git").exists():
        return [f"workspace clone {workspace_repo_path!r} is not a git repository"]

    for tid in task_ids:
        m = corpus.manifests[tid]
        local = source_repos.get(m.code.repo) or m.code.local_path
        repo = Path(local).expanduser() if local else None
        if repo is None or not (repo / ".git").exists():
            problems.append(f"{tid}: no local clone resolved for {m.code.repo}")
        elif not _sha_present(repo, m.code.pre_task_base_sha):
            problems.append(
                f"{tid}: pre-task base {m.code.pre_task_base_sha[:9]} is absent from {repo}"
            )
        for item in m.workspace_inputs:
            if not _blob_present(ws, item.sha, item.path):
                problems.append(f"{tid}: {item.path}@{item.sha[:9]} is unresolvable in {ws}")
        for dep in m.dependency_snapshots:
            if dep.kind == "image":
                if subprocess.run(
                    ["podman", "image", "exists", dep.ref], capture_output=True, check=False
                ).returncode != 0:
                    problems.append(
                        f"{tid}: image {dep.ref} is not in local storage. The runner never "
                        "pulls; load it first so the replay is honest about what it needed."
                    )
            elif dep.kind == "git_bundle":
                src = source_repos.get(dep.ref) or dep.local_path
                path = Path(src).expanduser() if src else None
                if path is None or not (path / ".git").exists():
                    problems.append(f"{tid}: no local clone resolved for dependency {dep.ref}")
                elif dep.sha and not _sha_present(path, dep.sha):
                    problems.append(f"{tid}: dependency {dep.ref}@{dep.sha[:9]} absent from {path}")
    return problems


def run_preflight(
    *,
    corpus: LoadedCorpus,
    repo_root: Path,
    agent: AgentSpec,
    reviewer: ReviewerSpec,
    task_ids: list[str],
    expect_hash: str,
    work_root: Path,
    out_dir: Path,
    record_id: str,
    date: str,
    source_repos: dict[str, str],
    workspace_repo_path: str | None,
) -> list[str]:
    """Every locally checkable precondition. Empty list ⇒ the batch may call a model."""
    return [
        *check_not_canonical(repo_root),
        *check_checkout_clean(repo_root),
        *check_corpus(corpus, expect_hash=expect_hash, expect_held_in=task_ids),
        *check_tools(),
        *check_agent_command(agent, "candidate"),
        *check_reviewer_sandbox(reviewer),
        *check_sandbox_reachability(reviewer),
        *check_destinations(
            work_root=work_root, out_dir=out_dir, record_id=record_id, date=date
        ),
        *check_manifest_pins(
            corpus, task_ids, source_repos=source_repos, workspace_repo_path=workspace_repo_path
        ),
    ]
