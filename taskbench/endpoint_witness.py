"""Drive every worker gate at both real endpoints, before any model sees the corpus.

A gate that passes at the pre-task baseline measures nothing; a gate that fails at the
outcome the operator accepted reports a grader defect as a model result. Both are silent
until someone checks, so this checks: each task's offline verifiers run in a tree at the
task's own `pre_task_base_sha` and again in a tree at the accepted outcome, and the corpus
is only honest if each one is RED at the first and GREEN at the second.

Some gates are deliberately green at both ends — a lint that was already clean is a
no-regression bar rather than a discriminator — so this reports each verifier's pair
rather than asserting one shape, and names which ones discriminate. A task where NOTHING
discriminates is a defect and is reported as one.

Nothing here calls a model. It needs the local source repositories and whatever a
verifier needs (a database, a linter); a verifier that cannot execute is reported as an
environment failure, never as a witness.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .grade import resolve_argv
from .manifest import LoadedCorpus
from .review_packet import export_tree


@dataclass
class VerifierPair:
    task_id: str
    verifier_id: str
    baseline_exit: int | None
    accepted_exit: int | None
    discriminates: bool
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class EndpointReport:
    pairs: list[VerifierPair] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: Gates that behave identically at both endpoints. That is legitimate — a lint that was
    #: already clean is a no-regression bar — but it is a NUMBER a reader should see rather
    #: than a note buried per row, because a corpus drifting toward all-bar-no-discriminator
    #: looks green the whole way.
    non_discriminating: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "non_discriminating": self.non_discriminating,
            "pairs": [p.to_json() for p in self.pairs],
            "problems": self.problems,
        }


def _tree(source: Path, sha: str, dest: Path) -> Path:
    export_tree(source, sha, dest)
    for cmd in (
        ["git", "-C", str(dest), "init", "--quiet", "--initial-branch=main"],
        ["git", "-C", str(dest), "config", "user.name", "taskbench endpoint"],
        ["git", "-C", str(dest), "config", "user.email", "taskbench@localhost"],
        ["git", "-C", str(dest), "add", "-A"],
        ["git", "-C", str(dest), "commit", "--quiet", "-m", "state", "--no-verify"],
    ):
        # A swallowed failure here leaves a tree with no baseline commit, and the graders
        # that read their own baseline would then report a difference between two broken
        # exports as a difference between the two endpoints.
        out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(
                f"could not build the endpoint tree at {dest}: `{' '.join(cmd[:4])}` failed: "
                f"{out.stderr.strip()[-300:]}"
            )
    return dest


def run(
    corpus: LoadedCorpus,
    *,
    task_ids: list[str] | None = None,
    source_repos: dict[str, str] | None = None,
) -> EndpointReport:
    report = EndpointReport()
    overrides = dict(source_repos or {})
    for tid in task_ids or list(corpus.catalog.held_in):
        m = corpus.manifests[tid]
        facts = corpus.acceptance_facts(tid)
        local = overrides.get(m.code.repo) or m.code.local_path
        if not local:
            report.problems.append(f"{tid}: no local source repository")
            continue
        source = Path(local).expanduser()
        if not facts.historical_solution_sha:
            report.problems.append(f"{tid}: the grading file names no accepted outcome sha")
            continue

        tmp = Path(tempfile.mkdtemp(prefix=f"taskbench-endpoint-{tid}-"))
        try:
            try:
                trees = {
                    "baseline": _tree(
                        source, m.code.pre_task_base_sha, tmp / "baseline" / "code"
                    ),
                    "accepted": _tree(
                        source, facts.historical_solution_sha, tmp / "accepted" / "code"
                    ),
                }
            except (RuntimeError, OSError) as exc:
                # One task whose endpoints cannot be built must not stop the other four from
                # being checked; the report is the product here.
                report.problems.append(f"{tid}: could not build its endpoints: {exc}")
                continue
            discriminating = 0
            for v in m.offline_verifiers():
                exits: dict[str, int | None] = {}
                note = ""
                for label, tree in trees.items():
                    cwd = tree.parent / v.cwd if v.cwd else tree.parent
                    argv = resolve_argv(v.argv, corpus_root=corpus.root, cell_root=tree.parent)
                    try:
                        proc = subprocess.run(  # noqa: S603 — argv from a hashed manifest
                            argv, cwd=str(cwd), capture_output=True, text=True,
                            timeout=v.timeout_s, check=False,
                        )
                        exits[label] = proc.returncode
                    except subprocess.TimeoutExpired:
                        exits[label] = None
                        note = f"timed out at the {label} end after {v.timeout_s}s"
                    except OSError as exc:
                        exits[label] = None
                        note = f"could not execute at the {label} end: {exc}"
                if None in exits.values():
                    report.problems.append(
                        f"{tid}/{v.id}: could not execute at the "
                        f"{'baseline' if exits['baseline'] is None else 'accepted'} end "
                        f"({note}). A gate nobody could run tells you nothing about either "
                        "endpoint, and a corpus is not proven while one is in that state."
                    )
                good = exits["baseline"] != v.expect_exit and exits["accepted"] == v.expect_exit
                if exits["accepted"] not in (v.expect_exit, None):
                    report.problems.append(
                        f"{tid}/{v.id}: RED at the accepted outcome (exit {exits['accepted']}). "
                        "A gate the operator's own accepted result cannot pass reports a grader "
                        "defect as a model result."
                    )
                if good:
                    discriminating += 1
                elif not note and exits["baseline"] == v.expect_exit:
                    note = "green at both ends — a no-regression bar, not a discriminator"
                report.pairs.append(
                    VerifierPair(tid, v.id, exits["baseline"], exits["accepted"], good, note)
                )
            report.non_discriminating += sum(
                1 for p in report.pairs if p.task_id == tid and not p.discriminates
            )
            if not discriminating:
                report.problems.append(
                    f"{tid}: no deterministic gate tells the pre-task baseline from the accepted "
                    "outcome. Every check here would pass having done nothing."
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return report
