"""The reviewer batch: audit the gold, then run five cells, then write one record.

Order matters and is enforced here rather than left to a launcher:

1. **The gold audit runs first, and it runs fresh.** A must-find set assembled by whoever
   built the corpus is one reading of history. Before any scored cell, one strong model is
   shown a packet and the proposed must-find set for it, and asked to dispute anything the
   order, the diff and the repair do not support. Its dissent is RECORDED — it never edits
   the corpus, because the corpus is frozen and an audit that could rewrite gold would
   simply move the problem. A disputed must-find stops the batch and becomes a decision.
2. **Then the cells**, one packet each, each behind its own isolation probe.
3. **Then the score and the fidelity verdict**, by the rule fixed before any cell ran.

Nothing here calls a candidate model. The batch's whole spend is the incumbent proving its
own instrument, which is the only kind of spend this order authorises.
"""

from __future__ import annotations

import json
import platform
from pathlib import Path

from . import record, review_cell, review_packet, review_score
from .review_cell import ReviewerCellSpec
from .reviewbench import LoadedReviewCorpus

CANARY_TEXT = (
    "taskbench reviewer-isolation canary.\n\n"
    "If a reviewer process can read this file, its packet is not its whole world and every\n"
    "score from that cell is void.\n"
)


class BatchAborted(RuntimeError):
    """The batch could not run honestly. Never downgraded to a red result."""


def build_config(
    *, record_id: str, date: str, corpus: LoadedReviewCorpus,
    reviewer: ReviewerCellSpec, supersedes: str | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "date": date,
        "instrument": "reviewer",
        "corpus_version": corpus.catalog.corpus_version,
        "corpus_content_hash": corpus.content_hash,
        "corpus_file_hashes": corpus.file_hashes,
        "taskbench_code_sha": record.taskbench_code_sha(),
        "reviewer_labels": dict(reviewer.labels),
        "packets": list(corpus.catalog.packets),
        "fidelity_rule": {
            "every_blocking_must_find_found": True,
            "min_dispositions_correct": review_score.MIN_DISPOSITIONS_CORRECT,
            "no_unsupported_high_severity_on_the_clean_packet": True,
        },
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "supersedes": supersedes,
    }


def ensure_canary(work_root: str | Path) -> Path:
    p = Path(work_root) / "CANARY-reviewer-isolation.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CANARY_TEXT)
    return p


AUDIT_BRIEF = """\
# Audit of a proposed must-find set

You are auditing a benchmark's hidden answer key, not reviewing code and not grading anyone.

`packet/` holds exactly what a reviewer will be given for one historical change: the order,
the diff, the artefacts, the sources, and the checks that were run. `proposed.json` holds the
defects the benchmark intends to REQUIRE a reviewer to find in it, plus the expected overall
disposition.

Your question, for each proposed must-find, is narrow: **does the order, the diff, or the
repair support it?** A defect that only exists because someone once said so is not gold.

Answer in `audit.json`:

```json
{schema}
```

`disputed` is for a proposed must-find you would NOT require a competent reviewer to find,
with the reason. `missing` is for a defect you would require that the proposal omits.
`severity_disagreement` is for one whose severity you would set differently. Say nothing at
all about how likely a model is to find any of it — that is what the benchmark measures and
your opinion of it would contaminate the measurement.
"""

AUDIT_SCHEMA = {
    "packet_id": "echoed back from proposed.json",
    "disputed": [{"id": "must-find id", "why": "what the order/diff/repair fails to support"}],
    "missing": [{"summary": "a defect the proposal omits", "why_required": "..."}],
    "severity_disagreement": [{"id": "must-find id", "proposed": "...", "yours": "...", "why": ""}],
    "verdict": "supported | disputed",
}


def build_audit_packet(
    corpus: LoadedReviewCorpus,
    packet_id: str,
    *,
    dest: str | Path,
    source_repos: dict[str, str] | None = None,
    workspace_repo_path: str | None = None,
) -> Path:
    """The reviewer's packet plus the proposed answer key — for the auditor only."""
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    built = review_packet.build_packet(
        corpus, packet_id, dest=root / "packet",
        source_repos=source_repos, workspace_repo_path=workspace_repo_path,
        run_checks=False,
    )
    if built.violations:
        raise BatchAborted(f"{packet_id}: packet scan failed — {'; '.join(built.violations)}")
    gold = corpus.gold(packet_id)
    (root / "proposed.json").write_text(
        json.dumps(
            {
                "packet_id": packet_id,
                "expected_disposition": gold.expected_disposition,
                "must_find": [
                    {
                        "id": m.id, "severity": m.severity, "summary": m.summary,
                        "basis": m.basis,
                    }
                    for m in gold.must_find
                ],
                "adjudication": gold.adjudication,
            },
            indent=2,
        )
        + "\n"
    )
    (root / "README.md").write_text(AUDIT_BRIEF.format(schema=json.dumps(AUDIT_SCHEMA, indent=2)))
    return root


def run_batch(
    corpus: LoadedReviewCorpus,
    *,
    work_root: str | Path,
    out_dir: str | Path,
    record_id: str,
    date: str,
    reviewer: ReviewerCellSpec,
    audits: dict[str, dict] | None = None,
    source_repos: dict[str, str] | None = None,
    workspace_repo_path: str | None = None,
    packet_ids: list[str] | None = None,
    supersedes: str | None = None,
) -> tuple[Path, list[review_score.PacketScore]]:
    """Run the reviewer over every packet and write one immutable record."""
    selected = list(corpus.catalog.packets) if packet_ids is None else list(packet_ids)
    unknown = sorted(set(selected) - set(corpus.catalog.packets))
    if unknown:
        raise BatchAborted(f"unknown packet id(s): {unknown}")
    if len(set(selected)) != len(selected):
        raise BatchAborted(f"the same packet appears twice in this batch: {selected}")
    if not selected:
        raise BatchAborted("this batch selects no packet at all")

    # The audit is a precondition, not an attachment. The launcher already stops on a
    # dispute, and this refuses too: `run_batch` is callable directly, and a scored record
    # produced without the audit — or over one that objected — would look exactly like a
    # scored record produced with it.
    if audits is None:
        raise BatchAborted(
            "this batch has no gold audit. The audit is a precondition and not an "
            "attachment: a scored record produced without one is indistinguishable "
            "from a scored record produced with one."
        )
    if audits is not None:
        missing = sorted(set(selected) - set(audits))
        if missing:
            raise BatchAborted(
                f"the gold audit does not cover {missing}. Every packet in this batch needs "
                "one before any of them is scored."
            )
        objected = sorted(
            pid for pid in selected
            if (audits[pid].get("disputed") or audits[pid].get("missing")
                or str(audits[pid].get("verdict", "")).strip().lower() != "supported")
        )
        if objected:
            raise BatchAborted(
                f"the gold audit did not support {objected}. Resolving that is a decision: "
                "either the evidence carries the must-find and the audit is wrong, or it does "
                "not and this corpus needs a new version. It is not an edit to make here."
            )

    work = Path(work_root)
    canary = ensure_canary(work)
    config = build_config(
        record_id=record_id, date=date, corpus=corpus, reviewer=reviewer,
        supersedes=supersedes,
    )
    if audits:
        config["gold_audit"] = audits
    # What was actually run, beside what the corpus holds. A record that named the whole
    # catalog while a subset ran would misdescribe itself.
    config["packets_run"] = selected
    root = record.open_record(out_dir, config)
    (root / "cells").mkdir(exist_ok=True)

    scores: list[review_score.PacketScore] = []
    rows: list[dict] = []
    try:
        for pid in selected:
            cell = work / f"reviewcell-{pid}"
            built = review_packet.build_packet(
                corpus, pid, dest=cell / "packet",
                source_repos=source_repos, workspace_repo_path=workspace_repo_path,
            )
            if built.violations:
                raise BatchAborted(
                    f"{pid}: the packet failed its own scan and was not shown to a reviewer — "
                    + "; ".join(built.violations)
                )
            home = review_cell.build_fresh_home(reviewer, cell / "home")
            probe = review_cell.run_probe(
                reviewer,
                packet_dir=built.root,
                home=home,
                deny={
                    "canary": str(canary),
                    "gold": str(corpus.root / "gold" / f"{pid}.yaml"),
                    "operator_home": str(Path("~").expanduser() / ".config"),
                },
                declared_inputs=built.declared_inputs,
            )
            outcome = review_cell.run_reviewer(
                reviewer, packet_dir=built.root, home=home, packet_id=pid,
                blind_id=built.blind_id, probe=probe, log_dir=cell / "log",
            )
            score = review_score.score_packet(
                corpus.gold(pid), outcome.verdict, blind_id=built.blind_id
            )
            if not probe.ok:
                score.inconclusive = True
                score.because = f"isolation probe failed: {json.dumps(probe.denied)}"
            scores.append(score)

            cell_dir = root / "cells" / built.blind_id
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "probe.json").write_text(json.dumps(probe.to_json(), indent=2) + "\n")
            (cell_dir / "score.json").write_text(json.dumps(score.to_json(), indent=2) + "\n")
            (cell_dir / "packet-manifest.json").write_text(
                json.dumps({"packet_id": built.blind_id, "inputs": built.declared_inputs,
                            "checks": built.check_exits}, indent=2) + "\n"
            )
            if outcome.verdict is not None:
                (cell_dir / "verdict.json").write_text(
                    json.dumps(outcome.verdict, indent=2) + "\n"
                )
            (cell_dir / "run.json").write_text(
                json.dumps(
                    {
                        "packet_id": pid, "blind_id": built.blind_id,
                        "labels": dict(reviewer.labels), "exit": outcome.exit_code,
                        "ran": outcome.ran, "reason": outcome.reason, "usage": outcome.usage,
                    },
                    indent=2,
                )
                + "\n"
            )
            for name in ("reviewer-stdout.txt", "reviewer-stderr.txt"):
                src = cell / "log" / name
                if src.is_file():
                    (cell_dir / name).write_text(src.read_text(errors="replace"))
            rows.append(
                {
                    "blind_id": built.blind_id, "packet_id": pid,
                    "expected": score.expected_disposition,
                    "reported": score.reported_disposition,
                    "usage": outcome.usage,
                }
            )
    finally:
        (root / "packets.json").write_text(json.dumps(rows, indent=2) + "\n")
        fidelity = review_score.reviewer_fidelity(scores, expected_packets=len(selected))
        (root / "fidelity.json").write_text(json.dumps(fidelity.to_json(), indent=2) + "\n")
        (root / "aggregate.md").write_text(render_aggregate(config, corpus, scores, fidelity))
    return root, scores


def render_aggregate(
    config: dict,
    corpus: LoadedReviewCorpus,
    scores: list[review_score.PacketScore],
    fidelity: review_score.ReviewerFidelity,
) -> str:
    lines = [
        f"# Reviewer fidelity — {config['record_id']}",
        "",
        f"**{'GREEN' if fidelity.green else 'RED'}** — {fidelity.because}",
        "",
        f"Corpus `{config['corpus_version']}` at `{config['corpus_content_hash']}`; reviewer "
        f"labels `{json.dumps(config['reviewer_labels'], sort_keys=True)}`.",
        "",
        "| packet | expected | reported | disposition | must-find found | unsupported |",
        "|---|---|---|---|---|---|",
    ]
    for s in scores:
        found = f"{sum(1 for m in s.must_find if m.found)}/{len(s.must_find)}"
        verdict_cell = (
            "INCONCLUSIVE" if s.inconclusive else ("yes" if s.disposition_correct else "NO")
        )
        lines.append(
            f"| {s.packet_id} | {s.expected_disposition} | {s.reported_disposition or '—'} | "
            f"{verdict_cell} | {found} | {len(s.unsupported_high_severity)} |"
        )
    lines += ["", "## What decided each cell", ""]
    for s in scores:
        lines.append(f"- **{s.packet_id}** — {s.because}")
        for m in s.must_find:
            if not m.found:
                lines.append(f"  - missed `{m.id}` ({m.severity})")
    if config.get("gold_audit"):
        lines += ["", "## Gold audit, before any cell ran", ""]
        for pid, audit in sorted(config["gold_audit"].items()):
            verdict = audit.get("verdict", "?")
            lines.append(f"- **{pid}** — {verdict}")
            for d in audit.get("disputed") or []:
                lines.append(f"  - disputed `{d.get('id')}`: {d.get('why')}")
            for m in audit.get("missing") or []:
                lines.append(f"  - would also require: {m.get('summary')}")
    lines += [
        "",
        "## Reading this",
        "",
        "Disposition and must-find coverage are separate on purpose. A reviewer that returns "
        "`required_change` for a reason that is not the reason has answered correctly by "
        "accident, and only the second column tells you which happened. An extra finding is "
        "reported and never counted against a reviewer — except on the packet that shipped "
        "unchanged, where an unsupported high-severity finding IS the failure being measured.",
        "",
        "Read `cells/<id>/probe.json` before trusting any cell. A cell whose probe failed was "
        "never launched — the reviewer process does not start behind a failed probe — and its "
        "row below is marked INCONCLUSIVE. The row and its `score.json` still exist, because "
        "the diagnosis is the thing worth keeping; what they do not contain is a reviewer "
        "opinion, and nothing in the fidelity verdict counts them as one.",
        "",
    ]
    return "\n".join(lines)
