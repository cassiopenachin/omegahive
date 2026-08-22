"""Materialize one reviewer packet, prove it is blind, and score what comes back.

Separate from `reviewbench` (which is the frozen description) because this is the part
that touches disks and processes: it exports two historical trees, runs the packet's
declared checks against the state under review, assembles a directory, and then scans that
directory for the two things that would void the cell — the answer, and the future.

The blinding here is narrower than the worker instrument's and is stated so nobody
over-reads it. There is no candidate to hide: the work under review is historical. What
must not reach the reviewer is (1) what the historical review found, (2) the repair, (3)
anything the repository did after the packet's head, and (4) the expected disposition.
(1) and (4) live only in `gold/`, which never enters a packet; (2) and (3) are excluded by
construction, because the packet is built from `git diff base..head` and from files read at
`head` — no later object is ever opened.

The reviewer's HOME is fresh per cell, and that is not decoration: corpus v0.1's reviewer
inherited an operator `$HOME` carrying transcripts of the very tasks it was grading.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .reviewbench import LoadedReviewCorpus, ReviewPacket


class PacketError(RuntimeError):
    """The packet could not be built honestly. Never downgraded to a scored cell."""


#: Filename fragments that mean "this file says how the work was judged or repaired".
_FORBIDDEN_FRAGMENTS = (
    "gold/", "-gold", "acceptance-facts", "grading/", "audit-findings", "review-findings",
)


@dataclass
class BuiltPacket:
    root: Path
    packet_id: str
    #: The meaningless label the reviewer sees, so nothing about the source is inferable.
    blind_id: str
    declared_inputs: list[str] = field(default_factory=list)
    check_exits: dict[str, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and out.returncode != 0:
        raise PacketError(f"git {' '.join(args)} in {repo} failed: {out.stderr.strip()}")
    return out.stdout


def _resolve(local_path: str | None, repo: str, overrides: dict[str, str]) -> Path:
    candidate = overrides.get(repo) or local_path
    if not candidate:
        raise PacketError(
            f"no local source for {repo}: set `local_path` in the packet or pass an override"
        )
    path = Path(os.path.expanduser(candidate))
    if not (path / ".git").exists() and not (path / "HEAD").exists():
        raise PacketError(f"{path} is not a git repository (source for {repo})")
    return path


def export_tree(source: Path, sha: str, dest: Path) -> None:
    """One commit's tree, extracted. No objects travel, so no later commit does either."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / f".{dest.name}.tar"
    with archive.open("wb") as fh:
        proc = subprocess.run(
            ["git", "-C", str(source), "archive", "--format=tar", sha],
            stdout=fh, stderr=subprocess.PIPE, check=False,
        )
    if proc.returncode != 0:
        archive.unlink(missing_ok=True)
        raise PacketError(f"git archive {sha} in {source} failed: {proc.stderr.decode().strip()}")
    with tarfile.open(archive) as tf:
        tf.extractall(dest, filter="data")
    archive.unlink()


def resolve_argv(argv: list[str], *, corpus_root: Path, tree: Path) -> list[str]:
    """Expand the two placeholders a manifest may use.

    Substitution is positional, per argument, so a corpus path can never become an extra
    argument or a shell fragment.
    """
    return [a.replace("{corpus}", str(corpus_root)).replace("{tree}", str(tree)) for a in argv]


def new_blind_id() -> str:
    """A meaningless label. A deterministic one would leak the thing being hidden."""
    return f"packet-{secrets.token_hex(5)}"


BRIEF_HEADER = """\
# Review — {blind_id}

{focus}

Everything you may use is in this directory. There is no earlier or later version of this
work to compare against, and there is no reference solution.

- `order.md` — the order this work is answering. It is the authority for scope.
- `change.patch` — the whole of the work under review, as a diff against its starting point.
- `artefacts/` — files you should read whole rather than as diff hunks.
- `sources/` — the documents this work cites, at the revisions it cited them. If a claim
  cannot be checked against anything here, say so; do not assume it is unsupported and do
  not assume it is fine.
- `verification/` — output of the checks that were run against this state.

**State of the work:** {state_note}

## What to write

`verdict.json`, matching the schema in `SCHEMA.json`. Two things decide it:

* `disposition` — `required_change` if this work should not ship as it stands, otherwise
  `no_required_change`. Answer the question you were asked, not a softer one.
* `findings` — every defect you would require changed before this ships, each with a
  severity, the file it lives in, and the evidence. A defect you cannot evidence is a
  preference, and preferences are not defects here.

`disposition` is `no_required_change` only when `findings` is empty.
"""

VERDICT_SCHEMA = {
    "packet_id": "the id printed in packet.json, echoed back",
    "disposition": "required_change | no_required_change",
    "findings": [
        {
            "summary": "one sentence naming the defect",
            "severity": "critical | high | medium | approach",
            "file": "the path the defect lives in",
            "why_blocking": "what breaks, for whom, when",
            "evidence": "the line, the artefact or the check output the claim rests on",
        }
    ],
}


def build_packet(
    corpus: LoadedReviewCorpus,
    packet_id: str,
    *,
    dest: str | Path,
    source_repos: dict[str, str] | None = None,
    workspace_repo_path: str | None = None,
    run_checks: bool = True,
) -> BuiltPacket:
    """Assemble one reviewer packet from its frozen manifest."""
    packet = corpus.packets[packet_id]
    overrides = dict(source_repos or {})
    root = Path(dest)
    if root.exists() and any(root.iterdir()):
        raise PacketError(f"{root} already exists and is not empty; packets are fresh")
    root.mkdir(parents=True, exist_ok=True)
    work = root / ".build"
    work.mkdir()

    src = _resolve(packet.code.local_path, packet.code.repo, overrides)
    for sha in (packet.code.base_sha, packet.code.head_sha):
        if not _git(src, "cat-file", "-t", sha, check=False).strip() == "commit":
            raise PacketError(f"{packet_id}: {sha} is not a commit in {src}")

    diff = _git(src, "diff", packet.code.base_sha, packet.code.head_sha)
    (root / "change.patch").write_text(diff or "(this state changes nothing)\n")

    # The tree at HEAD — used for whole artefacts and for the declared checks. Never the
    # tree at any later commit, and never a clone, so nothing after HEAD is reachable.
    code = work / "code"
    export_tree(src, packet.code.head_sha, code)

    artefacts = root / "artefacts"
    artefacts.mkdir()
    for rel in packet.whole_artefacts:
        matched = sorted(code.glob(rel))
        if not matched:
            raise PacketError(f"{packet_id}: whole artefact {rel} is absent at the packet head")
        for path in matched:
            target = artefacts / path.relative_to(code)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)

    ws = _resolve(workspace_repo_path, packet.workspace_repo, overrides)
    sources = root / "sources"
    sources.mkdir()
    for item in packet.inputs:
        blob = subprocess.run(
            ["git", "-C", str(ws), "show", f"{item.sha}:{item.path}"],
            capture_output=True, check=False,
        )
        if blob.returncode != 0:
            raise PacketError(
                f"{packet_id}: source {item.path}@{item.sha[:7]} is unresolvable in {ws}: "
                f"{blob.stderr.decode().strip()}"
            )
        target = (root / "order.md") if item.role == "order" else (sources / item.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)

    verification = root / "verification"
    verification.mkdir()
    exits: dict[str, int] = {}
    if run_checks:
        for check in packet.checks:
            cwd = work / check.cwd if check.cwd else work
            argv = resolve_argv(check.argv, corpus_root=corpus.root, tree=code)
            try:
                proc = subprocess.run(  # noqa: S603 — argv from a hashed manifest, shell=False
                    argv, cwd=str(cwd), capture_output=True, text=True,
                    timeout=check.timeout_s, check=False,
                )
                text, code_ = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
            except subprocess.TimeoutExpired:
                text, code_ = f"(timed out after {check.timeout_s}s)\n", -1
            except OSError as exc:
                text, code_ = f"(could not execute: {exc})\n", -1
            exits[check.id] = code_
            (verification / f"{check.id}.log").write_text(
                f"$ {' '.join(check.argv)}\n# {check.description}\nexit {code_}\n\n{text}"
            )
    if not exits:
        (verification / "README.txt").write_text(
            "No check was run against this state.\n"
        )

    blind_id = new_blind_id()
    (root / "packet.json").write_text(json.dumps({"packet_id": blind_id}, indent=2) + "\n")
    (root / "SCHEMA.json").write_text(json.dumps(VERDICT_SCHEMA, indent=2) + "\n")
    (root / "README.md").write_text(
        BRIEF_HEADER.format(
            blind_id=blind_id, focus=packet.review_focus.strip(),
            state_note=packet.state_note.strip(),
        )
        + "\n"
        + corpus.brief()
    )

    shutil.rmtree(work, ignore_errors=True)

    declared = sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    ) + ["packet-manifest.json"]
    (root / "packet-manifest.json").write_text(
        json.dumps({"packet_id": blind_id, "inputs": sorted(declared)}, indent=2) + "\n"
    )

    built = BuiltPacket(
        root=root, packet_id=packet_id, blind_id=blind_id,
        declared_inputs=sorted(declared), check_exits=exits,
    )
    built.violations = scan_packet(built, packet)
    return built


def scan_packet(built: BuiltPacket, packet: ReviewPacket) -> list[str]:
    """Prove the packet holds no answer. Empty list means clean."""
    violations: list[str] = []
    for p in built.root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(built.root))
        low = rel.lower()
        if any(frag in low for frag in _FORBIDDEN_FRAGMENTS):
            violations.append(f"answer-shaped file in the packet: {rel}")
    # The blind id must be the only id the reviewer can see. A packet that still names its
    # own task lets a model recognise the case rather than review it.
    for name in ("README.md", "packet.json", "SCHEMA.json"):
        text = (built.root / name).read_text(errors="replace")
        if packet.id in text:
            violations.append(
                f"{name} names the packet's real id {packet.id!r}; the reviewer sees "
                f"{built.blind_id} and nothing else"
            )
    if not (built.root / "order.md").is_file():
        violations.append("the packet has no order")
    if not (built.root / "change.patch").is_file():
        violations.append("the packet has no diff")
    return violations
