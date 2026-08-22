"""Everything the middle-tier study can check without calling a model — checked before it does.

Two instruments run in one batch here, so this composes the worker preflight with the checks
the reviewer instrument adds, and with the one dependency class corpus v1 introduces: a
pinned public dataset the runner is forbidden to fetch.

The rule is the one the worker preflight already states and this extends rather than
loosens: **a red cell must mean the model.** Anything that could turn a cell red for an
environmental reason belongs here, refused loudly, before anything spends.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from . import preflight
from .manifest import LoadedCorpus
from .review import ReviewerSpec
from .review_cell import ReviewerCellSpec
from .reviewbench import LoadedReviewCorpus
from .runner import AgentSpec

#: Where the operator keeps content-hashed public sources. The runner never fetches; a task
#: that needs one of these is measured against THIS packet and the manifest says so.
SOURCE_CACHE = Path(
    os.environ.get("TASKBENCH_SOURCE_CACHE", "~/work/taskbench/sources")
).expanduser()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_source_snapshots(
    corpus: LoadedCorpus, task_ids: list[str], *, cache: Path = SOURCE_CACHE
) -> list[str]:
    """Every pinned public file present in the cache, at its pinned digest.

    A digest mismatch is a refusal rather than a warning. The upstream of a public dataset
    can be edited under a stable URL, and measuring one dataset while reporting another's
    name is exactly the drift the pinning exists to catch.
    """
    problems: list[str] = []
    for tid in task_ids:
        for dep in corpus.manifests[tid].dependency_snapshots:
            if dep.kind != "source_snapshot":
                continue
            for f in dep.files:
                path = cache / dep.name / f.name
                if not path.is_file():
                    problems.append(
                        f"{tid}: source snapshot {dep.name}/{f.name} is missing from {cache}. "
                        f"The runner never fetches. Place it yourself, once:\n"
                        f"      mkdir -p {cache / dep.name}\n"
                        f"      curl -fsSL -o {path} {f.url}\n"
                        f"      # then confirm: sha256sum {path}  ->  {f.sha256}"
                    )
                    continue
                actual = _sha256_file(path)
                if actual != f.sha256:
                    problems.append(
                        f"{tid}: source snapshot {dep.name}/{f.name} hashes {actual}, pinned "
                        f"{f.sha256}. The upstream moved, or this is a different file. Do not "
                        "re-pin to whatever a fetch returns today — that silently changes what "
                        "the benchmark measured."
                    )
    return problems


def check_review_corpus(
    corpus: LoadedReviewCorpus, *, expect_hash: str, expect_packets: list[str]
) -> list[str]:
    problems: list[str] = []
    if corpus.content_hash != expect_hash:
        problems.append(
            f"reviewer corpus content hash is {corpus.content_hash}, the launch expects "
            f"{expect_hash}. A corpus that moved after it was frozen invalidates every earlier "
            "cell; investigate rather than re-freeze."
        )
    if sorted(corpus.catalog.packets) != sorted(expect_packets):
        problems.append(
            f"reviewer packets are {sorted(corpus.catalog.packets)}, the launch expects "
            f"{sorted(expect_packets)}"
        )
    clean = [p for p in corpus.catalog.packets if corpus.gold(p).expected_disposition
             == "no_required_change"]
    if not clean:
        problems.append(
            "no packet in the reviewer corpus is expected to need no change. Without one, the "
            "instrument cannot measure false positives at all, and a reviewer that flags "
            "everything scores well."
        )
    return problems


def check_review_pins(
    corpus: LoadedReviewCorpus,
    *,
    source_repos: dict[str, str],
    workspace_repo_path: str | None,
) -> list[str]:
    """Every commit and blob a packet needs, resolvable now rather than mid-batch."""
    problems: list[str] = []
    ws = Path(workspace_repo_path).expanduser() if workspace_repo_path else None
    if ws is None or not (ws / ".git").exists():
        return [f"workspace clone {workspace_repo_path!r} is not a git repository"]
    for pid, packet in sorted(corpus.packets.items()):
        local = source_repos.get(packet.code.repo) or packet.code.local_path
        repo = Path(local).expanduser() if local else None
        if repo is None or not (repo / ".git").exists():
            problems.append(f"{pid}: no local clone resolved for {packet.code.repo}")
        else:
            for label, sha in (
                ("base", packet.code.base_sha),
                ("head", packet.code.head_sha),
                ("accepted", corpus.gold(pid).accepted_sha),
            ):
                if not preflight._sha_present(repo, sha):
                    problems.append(f"{pid}: {label} {sha[:9]} is absent from {repo}")
        for item in packet.inputs:
            if not preflight._blob_present(ws, item.sha, item.path):
                problems.append(f"{pid}: {item.path}@{item.sha[:9]} is unresolvable in {ws}")
    return problems


def check_reviewer_cell(
    spec: ReviewerCellSpec, *, must_not_reach: list[Path] | None = None
) -> list[str]:
    """The reviewer's own launch, checkable without launching it.

    `must_not_reach` is the set of directories a reviewer must never see: the corpus that
    holds the answer key, the repositories that hold the repairs, the workspace. The probe
    proves the boundary per cell at run time, and this refuses the configuration that would
    have failed it — an operator who binds a parent of the corpus to give the reviewer its
    toolchain should learn that here, not from five red cells.
    """
    problems: list[str] = []
    if not spec.argv:
        return ["the reviewer command is empty"]
    resolved_cmd = shutil.which(spec.argv[0]) or (
        str(Path(spec.argv[0])) if Path(spec.argv[0]).is_file() else None
    )
    if resolved_cmd is None:
        problems.append(f"reviewer command {spec.argv[0]!r} is not on PATH and is not a file")
    elif not os.access(resolved_cmd, os.X_OK):
        problems.append(f"reviewer command {resolved_cmd!r} is not executable")
    for arg in spec.argv:
        # Only characters that would corrupt the record or the wrapper. argv is never
        # shell-interpreted here, and refusing an argument for containing `$` would refuse
        # a perfectly ordinary prompt.
        if "\n" in arg or "\x00" in arg:
            problems.append(f"reviewer argv carries a newline or NUL: {arg!r}")
    if not spec.sandbox_argv:
        problems.append(
            "the reviewer has no sandbox wrapper. The isolation this instrument's validity "
            "rests on would not exist, and the probe would pass by seeing everything."
        )
    else:
        if shutil.which(spec.sandbox_argv[0]) is None:
            problems.append(f"sandbox wrapper {spec.sandbox_argv[0]!r} is not on PATH")
        if not any("{packet}" in a for a in spec.sandbox_argv):
            problems.append(
                "the sandbox wrapper never mentions {packet}, so the packet would not be "
                "bound into it and the cell would review an empty directory"
            )
    real_home = Path("~").expanduser()
    if spec.home_seed is None:
        problems.append(
            "the reviewer launch does not say what its fresh home needs. Name the files its "
            "tool needs to authenticate, or set `home_seed: []` to state that it needs none "
            "— an omission that was not meant produces five cells that write no verdict."
        )
    for rel in spec.home_seed or []:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            problems.append(f"home seed {rel!r} must be a relative path under the operator's home")
        elif not (real_home / rel).exists():
            problems.append(f"home seed {rel!r} does not exist under {real_home}")
        elif (real_home / rel).is_dir():
            problems.append(
                f"home seed {rel!r} is a directory. An agent CLI's state directory holds its "
                "prompt history and its per-project transcripts beside its credential; seeding "
                "it hands a reviewer the record of the task it is grading."
            )
    forbidden = [Path(p).expanduser().resolve() for p in (must_not_reach or [])]
    for path in spec.sandbox_ro_binds:
        resolved = Path(path).expanduser()
        if not resolved.exists():
            problems.append(f"sandbox read-only bind {path!r} does not exist")
            continue
        bind = resolved.resolve()
        for target in forbidden:
            if bind == target or bind in target.parents or target in bind.parents:
                problems.append(
                    f"sandbox read-only bind {path!r} reaches {target} — the reviewer would "
                    "be able to read the answer key, a repair, or the workspace"
                )
    return problems


def run_middle_preflight(
    *,
    worker_corpus: LoadedCorpus,
    review_corpus: LoadedReviewCorpus,
    repo_root: Path,
    agent: AgentSpec,
    worker_reviewer: ReviewerSpec,
    reviewer_cell: ReviewerCellSpec,
    task_ids: list[str],
    expect_worker_hash: str,
    expect_review_hash: str,
    expect_review_packets: list[str],
    work_root: Path,
    out_dir: Path,
    record_id: str,
    date: str,
    source_repos: dict[str, str],
    workspace_repo_path: str | None,
    source_cache: Path = SOURCE_CACHE,
) -> list[str]:
    """Every locally checkable precondition for BOTH instruments. Empty list ⇒ may spend."""
    return [
        *preflight.run_preflight(
            corpus=worker_corpus, repo_root=repo_root, agent=agent, reviewer=worker_reviewer,
            task_ids=task_ids, expect_hash=expect_worker_hash, work_root=work_root,
            out_dir=out_dir, record_id=record_id, date=date, source_repos=source_repos,
            workspace_repo_path=workspace_repo_path,
        ),
        *check_source_snapshots(worker_corpus, task_ids, cache=source_cache),
        *check_review_corpus(
            review_corpus, expect_hash=expect_review_hash, expect_packets=expect_review_packets
        ),
        *check_review_pins(
            review_corpus, source_repos=source_repos, workspace_repo_path=workspace_repo_path
        ),
        *check_reviewer_cell(
            reviewer_cell,
            must_not_reach=[
                worker_corpus.root,
                review_corpus.root,
                *([Path(workspace_repo_path)] if workspace_repo_path else []),
                *[Path(p) for p in source_repos.values()],
            ],
        ),
    ]
