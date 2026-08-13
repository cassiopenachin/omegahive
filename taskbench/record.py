"""The replay record: dated, pinned, validated, and immutable once written.

`taskbench/records/<date>-<record-id>/`::

    config.json              the pins — a record without all of them is invalid
    cells/<cell_id>/         run.json, verdict.json, candidate.patch, stdout/stderr,
                             verifier/*.log, review/{packet-manifest,probe,verdict}.json
    cells.json               cell id → task id + labels (operator-visible de-blinding map)
    aggregate.md             per-task table with per-task caveats
    RERUN.md                 written when this record supersedes an earlier failed one

Immutability is enforced by refusing to write into an existing record directory. A rerun
after a package repair is a **new** record that names the one it supersedes; the failed
record stays exactly as it was, because "we fixed the harness and re-ran" is a fact the
next reader needs and an overwrite destroys.

The atomic-write and canonical-hash idioms are lifted from `qual/record.py`, whose meaning
is unchanged here; nothing else is shared.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from omegahive.port.keys import canonical_payload

from .grade import TaskVerdict
from .manifest import LoadedCorpus, TaskManifest
from .review import ReviewerSpec
from .runner import AgentSpec, CellRun

REQUIRED_PINS = [
    "record_id",
    "date",
    "corpus_version",
    "corpus_content_hash",
    "taskbench_code_sha",
    "agent_labels",
    "reviewer_labels",
    "runner_config_hash",
    "held_in",
    "held_out",
    "host",
]


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def taskbench_code_sha() -> str:
    """Provenance of the harness itself — the same resolver shape `qual` uses."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        if out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return os.environ.get("TASKBENCH_CODE_SHA", "").strip() or "unpinned"


def runner_config_hash(agent: AgentSpec, reviewer: ReviewerSpec) -> str:
    """Hash what actually shaped the run — argv, labels, sandbox, timeouts. Not secrets."""
    return _sha256_bytes(
        canonical_payload(
            {
                "agent": {
                    "argv": agent.argv,
                    "labels": agent.labels,
                    "cwd": agent.cwd,
                    "timeout_s": agent.timeout_s,
                    "prompt_mode": agent.prompt_mode,
                },
                "reviewer": {
                    "argv": reviewer.argv,
                    "labels": reviewer.labels,
                    "sandbox_argv": reviewer.sandbox_argv,
                    "timeout_s": reviewer.timeout_s,
                    "prompt_mode": reviewer.prompt_mode,
                },
            }
        ).encode()
    )


def build_config(
    *,
    record_id: str,
    date: str,
    corpus: LoadedCorpus,
    agent: AgentSpec,
    reviewer: ReviewerSpec,
    supersedes: str | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "date": date,
        "corpus_version": corpus.catalog.corpus_version,
        "corpus_content_hash": corpus.content_hash,
        "corpus_file_hashes": corpus.file_hashes,
        "taskbench_code_sha": taskbench_code_sha(),
        "agent_labels": dict(agent.labels),
        "reviewer_labels": dict(reviewer.labels),
        "runner_config_hash": runner_config_hash(agent, reviewer),
        "held_in": list(corpus.catalog.held_in),
        "held_out": list(corpus.catalog.held_out),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "supersedes": supersedes,
    }


def next_record_id(out_dir: str | Path, base: str, date: str) -> tuple[str, str | None]:
    """Pick an unused record id, and name the most recent one it supersedes.

    The operator does not choose a record id — choosing one is an opportunity to overwrite
    history. The launcher takes `base`, and if that is taken it takes `base-2`, `base-3`, …
    and points `supersedes` at the newest existing record so the rerun says what it replaces
    instead of quietly replacing it.
    """
    out = Path(out_dir)
    if not (out / f"{date}-{base}").exists():
        return base, None
    existing = [base]
    n = 2
    while (out / f"{date}-{base}-{n}").exists():
        existing.append(f"{base}-{n}")
        n += 1
    return f"{base}-{n}", f"{date}-{existing[-1]}"


#: What a carried-forward cell must satisfy. A cell the environment killed says nothing about
#: the model and is re-run; a cell that produced a real verdict — green OR red — is carried,
#: because re-running a genuine red is re-rolling the dice for a better number.
def cell_is_conclusive(cell: Path) -> bool:
    """Derived from the cell's own evidence, never from a flag alone.

    Records written before the `inconclusive` flag existed carry rate-limited cells that look
    like ordinary reds. Trusting the flag would carry those forward as model results, so the
    terminal errors and the missing review verdict are read directly out of the cell.
    """
    verdict_file = cell / "verdict.json"
    if not verdict_file.is_file():
        return False
    try:
        verdict = json.loads(verdict_file.read_text())
    except json.JSONDecodeError:
        return False
    if verdict.get("inconclusive"):
        return False

    for name in ("run.json", "remediation-run.json"):
        path = cell / name
        if not path.is_file():
            continue
        try:
            run = json.loads(path.read_text())
        except json.JSONDecodeError:
            return False
        if (run.get("progress") or {}).get("terminal_error"):
            return False

    review = verdict.get("review") or {}
    if review.get("ran") and not review.get("passed") and "no verdict" in str(review.get("reason")):
        return False
    if not review.get("ran") and not review.get("probe_ok"):
        return False
    return True


def resumable_cells(prior_record: str | Path) -> dict[str, Path]:
    """task id → cell directory, for every cell in a prior record worth carrying forward."""
    root = Path(prior_record)
    out: dict[str, Path] = {}
    cells = root / "cells"
    if not cells.is_dir():
        return out
    for cell in sorted(cells.iterdir()):
        task_file = cell / "task.txt"
        if not cell.is_dir() or not task_file.is_file() or not cell_is_conclusive(cell):
            continue
        out[task_file.read_text().splitlines()[0].strip()] = cell
    return out


def check_resume_compatible(prior_config: dict, config: dict) -> list[str]:
    """A carried verdict is only evidence if it was produced under the same conditions.

    The harness sha is deliberately NOT required to match — a resume exists because something
    broke, and fixing it changes the code. The difference is recorded instead of forbidden, so
    a reader can see it.
    """
    problems = []
    for pin in ("corpus_version", "corpus_content_hash", "agent_labels", "reviewer_labels"):
        if prior_config.get(pin) != config.get(pin):
            problems.append(
                f"cannot resume: {pin} differs — prior {prior_config.get(pin)!r} vs "
                f"now {config.get(pin)!r}. A carried verdict would not be comparable."
            )
    return problems


def carry_cell(root: Path, cell: Path) -> None:
    """Copy a prior cell in verbatim, stamped with where it came from."""
    target = root / "cells" / cell.name
    shutil.copytree(cell, target)
    _atomic_write_text(
        target / "CARRIED-FORWARD.txt",
        f"This cell was not re-run. It was carried forward verbatim from {cell.parent.parent}\n"
        "because it produced a real verdict there and the corpus and labels are identical.\n"
        "It is evidence from that sitting, not this one.\n",
    )


class RecordExists(RuntimeError):
    """A record directory is written once. Reruns get their own id and name this one."""


def open_record(out_dir: str | Path, config: dict) -> Path:
    root = Path(out_dir) / f"{config['date']}-{config['record_id']}"
    if root.exists():
        raise RecordExists(
            f"{root} exists. Records are immutable: give the rerun a new --record-id and set "
            "--supersedes to this one, so the failed cell and its diagnosis both survive."
        )
    _atomic_write_text(root / "config.json", json.dumps(config, indent=2, sort_keys=True) + "\n")
    if config.get("supersedes"):
        _atomic_write_text(
            root / "RERUN.md",
            f"# Rerun\n\nThis record supersedes `{config['supersedes']}`.\n\n"
            "The superseded record is retained unmodified. State the package defect that "
            "made the rerun necessary in the result report; a rerun that cannot name one is "
            "a model result being re-rolled, which the order forbids.\n",
        )
    return root


def write_cell(
    root: Path,
    manifest: TaskManifest,
    run: CellRun,
    verdict: TaskVerdict,
    *,
    verifier_logs: dict[str, str],
    packet_manifest: list[str],
    probe: dict[str, Any],
    review_verdict: dict[str, Any] | None,
    cycle: Any = None,
    remediation_run: CellRun | None = None,
    spend: dict[str, Any] | None = None,
) -> Path:
    cell = root / "cells" / run.cell_id
    _atomic_write_text(
        cell / "run.json", json.dumps(run.to_json(), indent=2, sort_keys=True) + "\n"
    )
    _atomic_write_text(
        cell / "verdict.json", json.dumps(verdict.to_json(), indent=2, sort_keys=True) + "\n"
    )
    _atomic_write_text(cell / "candidate.patch", run.diff or "")
    if run.outward_actions:
        _atomic_write_text(
            cell / "outward-actions.json",
            json.dumps(run.outward_actions, indent=2, sort_keys=True) + "\n",
        )
    for name, text in verifier_logs.items():
        _atomic_write_text(cell / "verifier" / f"{name}.log", text)
    _atomic_write_text(
        cell / "review" / "packet-manifest.json",
        json.dumps({"cell_id": run.cell_id, "inputs": packet_manifest}, indent=2) + "\n",
    )
    _atomic_write_text(cell / "review" / "probe.json", json.dumps(probe, indent=2) + "\n")
    _atomic_write_text(
        cell / "review" / "verdict.json",
        json.dumps(review_verdict, indent=2, sort_keys=True) + "\n" if review_verdict else "null\n",
    )
    if run.workspace_diff:
        _atomic_write_text(cell / "workspace.patch", run.workspace_diff)
    if cycle is not None:
        _atomic_write_text(
            cell / "cycle.json", json.dumps(cycle.to_json(), indent=2, sort_keys=True) + "\n"
        )
    if remediation_run is not None:
        _atomic_write_text(
            cell / "remediation-run.json",
            json.dumps(remediation_run.to_json(), indent=2, sort_keys=True) + "\n",
        )
        _atomic_write_text(cell / "remediation.patch", remediation_run.diff or "")
        if remediation_run.workspace_diff:
            _atomic_write_text(
                cell / "remediation-workspace.patch", remediation_run.workspace_diff
            )
    if spend is not None:
        _atomic_write_text(cell / "spend.json", json.dumps(spend, indent=2, sort_keys=True) + "\n")
    _atomic_write_text(
        cell / "task.txt",
        f"{manifest.id}\n{manifest.project}\n{manifest.work_shape}\n",
    )
    return cell


def write_cells_map(root: Path, rows: list[dict]) -> None:
    """The de-blinding map. Operator-visible, never inside a review packet."""
    _atomic_write_text(root / "cells.json", json.dumps(rows, indent=2, sort_keys=True) + "\n")


def render_aggregate(
    config: dict,
    corpus: LoadedCorpus,
    verdicts: list[TaskVerdict],
) -> str:
    labels = config["agent_labels"]
    resolved = config.get("resolved_models") or []
    green = sum(1 for v in verdicts if v.passed)
    lines = [
        f"# Task-replay aggregate — {config['record_id']} ({config['date']})",
        "",
        f"Corpus `{config['corpus_version']}` (`{config['corpus_content_hash'][:19]}…`) · "
        f"candidate **{labels.get('vendor')}/"
        f"{', '.join(resolved) if resolved else labels.get('model')}** on "
        f"`{labels.get('harness')}` · reviewer **{config['reviewer_labels'].get('model')}**.",
        "",
        (
            f"Requested as `--model {labels.get('model')}`; the identifier above is what each "
            "run's own report said it resolved to. An alias is a request, not an identity."
            if resolved else
            "**No resolved model id was reported** — the identifier above is the alias the "
            "launch requested, which is not evidence of what ran."
        ),
        "",
        f"**{green}/{len(verdicts)} task-level verdicts green.**",
        "",
        "| task | project | work shape | first shot | after one repair | deterministic | "
        "review | because |",
        "|---|---|---|---|---|---|---|---|",
    ]
    cycles = config.get("cycles") or {}
    for v in sorted(verdicts, key=lambda x: x.task_id):
        m = corpus.manifests[v.task_id]
        det = "pass" if v.deterministic.passed else "FAIL"
        rev = "pass" if v.review.passed else ("FAIL" if v.review.ran else "not run")
        c = cycles.get(v.task_id) or {}
        first = "green" if c.get("first_passed") else "RED"
        after = "—" if not c.get("remediated") else ("green" if v.passed else "RED")
        lines.append(
            f"| {v.task_id} | {m.project} | {m.work_shape} | {first} | {after} | {det} | "
            f"{rev} | {v.because} |"
        )
    first_green = sum(1 for c in cycles.values() if c.get("first_passed"))
    lines += [
        "",
        f"**First-shot generation quality: {first_green}/{len(verdicts)} green without any "
        f"repair. Pipeline quality: {green}/{len(verdicts)} green after at most one "
        "review-and-repair cycle.** Both are reported because a model that routinely needs "
        "rescue must not read as a clean generator.",
    ]
    spend = config.get("spend_by_leg") or {}
    if spend:
        lines += [
            "",
            "| leg | reported list-price spend |",
            "|---|---|",
            *(f"| {k} | {v if v is not None else 'not reported'} |" for k, v in spend.items()),
            "",
            "A leg whose harness reported nothing is *not reported*, never zero.",
        ]
    lines += ["", "## Per-task caveats", ""]
    for v in sorted(verdicts, key=lambda x: x.task_id):
        m = corpus.manifests[v.task_id]
        lines.append(f"**{v.task_id}** — {m.bounded_because}")
        if m.non_replayable_legs:
            for leg in m.non_replayable_legs:
                lines.append(
                    f"  - Excluded leg (`{leg.executed_by}`): {leg.leg} — {leg.reason}"
                )
        if v.deterministic.checks:
            for c in v.deterministic.checks:
                lines.append(f"  - `{c.id}`: {'pass' if c.passed else 'FAIL'} — {c.detail}")
        lines.append("")
    lines += [
        "## What this table is not",
        "",
        f"{len(verdicts)} tasks give task-grain resolution of "
        f"{100 // max(len(verdicts), 1)} points and a fidelity check. They do not measure a "
        "stable population, and this corpus is hive-infrastructure-heavy: the work-shape "
        "column is there so a later reader can see that rather than read one pass rate.",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_aggregate(root: Path, text: str) -> None:
    _atomic_write_text(root / "aggregate.md", text)


def validate_record(path: str | Path) -> list[str]:
    """Return every problem found. Empty list ⇒ the record is valid.

    Checks the config pins, then that every cell carries the artefacts a later reader needs
    — a record whose pins are complete but whose cells are half-written is not valid, and
    the fidelity claim rests on this returning empty.
    """
    p = Path(path)
    root = p if p.is_dir() else p.parent
    cfg_path = root / "config.json"
    try:
        config = json.loads(cfg_path.read_text())
    except FileNotFoundError:
        return [f"{cfg_path}: missing"]
    except json.JSONDecodeError as exc:
        return [f"{cfg_path}: not valid JSON ({exc})"]

    problems = [
        f"config pin missing or empty: {pin}" for pin in REQUIRED_PINS if not config.get(pin)
    ]
    for key in ("agent_labels", "reviewer_labels"):
        labels = config.get(key) or {}
        problems += [
            f"{key}.{f} missing" for f in ("vendor", "model", "harness") if not labels.get(f)
        ]

    cells_dir = root / "cells"
    if not cells_dir.is_dir():
        problems.append("cells/ missing")
        return problems

    held_out = set(config.get("held_out") or [])
    for cell in sorted(cells_dir.iterdir()):
        if not cell.is_dir():
            continue
        for required in ("run.json", "verdict.json", "candidate.patch", "task.txt"):
            if not (cell / required).is_file():
                problems.append(f"{cell.name}: {required} missing")
        for required in ("review/packet-manifest.json", "review/probe.json"):
            if not (cell / required).is_file():
                problems.append(f"{cell.name}: {required} missing")
        run_file = cell / "run.json"
        if run_file.is_file():
            try:
                run = json.loads(run_file.read_text())
            except json.JSONDecodeError:
                problems.append(f"{cell.name}: run.json is not valid JSON")
            else:
                identity = run.get("resolved_identity") or {}
                if identity.get("available") and not identity.get("resolved_model"):
                    problems.append(
                        f"{cell.name}: the harness reported a run but no resolved model id; "
                        "the cell is unattributable"
                    )
                surface = identity.get("missing_surface", "no result envelope")
                # A harness that exposes no identity surface at all leaves an honest gap; a
                # launch that ASKED for one and did not get it leaves a record pinning the
                # alias it requested rather than what ran, which is the thing this instrument
                # exists to stop.
                if not identity.get("available") and "declared no result_envelope" not in surface:
                    problems.append(
                        f"{cell.name}: the launch asked the harness for its execution identity "
                        f"and got none — {surface}. The record would pin the alias the launch "
                        "requested, not what ran."
                    )
        task_file = cell / "task.txt"
        if task_file.is_file():
            task_id = task_file.read_text().splitlines()[0].strip()
            if task_id in held_out:
                problems.append(
                    f"{cell.name}: records a held-out task ({task_id}) — the reservation is "
                    "broken and this corpus version can no longer hold it out"
                )
    if not (root / "aggregate.md").is_file():
        problems.append("aggregate.md missing")
    if not (root / "cells.json").is_file():
        problems.append("cells.json missing")
    return problems
