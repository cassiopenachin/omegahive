"""Collecting what a finished task consumed, once the processes that consumed it are gone.

The turn runner used to observe consumption while it happened, and `execution.finished`
carried the result. With the turn runner off the production path nothing observes a
launch, so from 2026-08-23 the spine recorded no consumption at all — while every harness
went on writing it to disk, in four different shapes, in four different places, two of
them inside sandbox VMs that `sbx prune` destroys.

This module reads those files after the fact. Three rules make that honest rather than a
reconstruction:

**Attribution before arithmetic.** The hard question is not how to add tokens up — that is
`harness.usage` — it is which file belongs to which execution. Work and review bill
differently and often to different accounts, so a transcript filed under the wrong purpose
is a number that looks right and is not. Every source arrives here already labelled by
something that KNOWS: the worker's surface is the one its own harness writes, and a review
is attributed only by the sidecar the review wrapper wrote at the moment it ran.

**An unattributable file is kept and not counted.** A transcript no sidecar names, a round
whose sidecar lists two, a harness with no usage surface — each records `unavailable` with
a named reason. A total that quietly contains an unknown is worse than one that says it is
missing, and a zero is worse than both.

**Evidence is copied, not cited.** Sources are copied into the task root before anything is
read, because the two most expensive surfaces live inside VMs the next cleanup deletes. A
recorded path to a destroyed file is not a record.

Nothing here emits. It returns payloads for `hive-usage` to hand to the gateway, so the
whole of the decision-making is testable without a spine, a sandbox or a network.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omegahive.events.types import ExecutionUsage
from omegahive.harness.usage import UsageEvidence, extract, unavailable

# What each worker harness writes, and where a harvest can find it. A harness absent from
# this map is not an error: it is a harness whose consumption surface this deployment has
# not established, and it is recorded as exactly that.
WORK_EXTRACTOR_BY_HARNESS = {
    "claude-code": "claude-code-cost-state",
    "opencode": "opencode-messages",
    "codex": "codex-rollout",
}

# The reviewers all write Claude Code transcripts today; a codex reviewer writes a rollout.
# Keyed by the extractor the caller already determined from the file itself, so this module
# never has to guess a format from a path.
REVIEW_EXTRACTORS = ("claude-code-cost-state", "claude-code-transcript", "codex-rollout")


@dataclass
class HarvestRequest:
    """Everything the harvest needs, resolved by its caller.

    `host_sources` and `sandbox_sources` are `(extractor, path)` pairs the caller has
    already located — on the host, or copied out of the sandbox. Locating them needs a
    live `sbx` and the operator's home; deciding what they MEAN does not, and keeping the
    two apart is what makes this module testable.
    """

    task: str
    task_root: Path
    worker_id: str
    # The catalog identity of the work execution, or None when the spine has no
    # `execution.route_approved` to read one from. None is a real case — a hand-recovered
    # task — and it means the evidence is kept and nothing is attributed.
    work_identity: dict[str, Any] | None
    reviewer_identity: dict[str, Any] | None
    execution_id: str | None
    attempt: int
    order_ref: str | None
    host_sources: list[tuple[str, Path]] = field(default_factory=list)
    sandbox_sources: list[tuple[str, Path]] = field(default_factory=list)


@dataclass
class HarvestResult:
    work: UsageEvidence
    review: UsageEvidence
    manifest_path: Path
    # Files copied into the task root that no execution claims. Named so the operator can
    # see what the harvest could not place, rather than having it vanish.
    unattributed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _usage_dir(task_root: Path) -> Path:
    d = task_root / "run" / "usage"
    (d / "raw").mkdir(parents=True, exist_ok=True)
    return d


def _copy_in(task_root: Path, origin: Path, subdir: str) -> Path:
    """Copy a source into the task root, keeping its name and refusing to collide.

    The name is kept because it is the only link back to the session it came from; a
    collision is resolved by suffixing rather than overwriting, since two sandboxes can
    legitimately produce the same session id and losing one silently is the failure this
    whole module exists to stop.
    """
    dest_dir = _usage_dir(task_root) / "raw" / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / origin.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{origin.stem}.{n}{origin.suffix}"
        n += 1
    shutil.copy2(origin, dest)
    return dest


def _review_rounds(task_root: Path) -> list[tuple[str, list[str]]]:
    """Each saved review round and the transcripts its sidecar names, in round order.

    `run/reviews/` is the counted directory and `meta/` holds the sidecars — the same
    split the review wrapper enforces, for the same reason: a sidecar in the counted
    directory would match the round-counting glob and inflate the budget.
    """
    reviews = task_root / "run" / "reviews"
    if not reviews.is_dir():
        return []
    rounds = []
    for path in sorted(p for p in reviews.iterdir() if p.is_file()):
        sidecar = reviews / "meta" / f"{path.name}.transcripts"
        listed: list[str] = []
        if sidecar.is_file():
            listed = [ln.strip() for ln in sidecar.read_text().splitlines() if ln.strip()]
        rounds.append((path.name, listed))
    return rounds


def _merge(parts: list[UsageEvidence], source: str) -> UsageEvidence:
    """Add several reported extractions into one execution's total.

    Only `reported` parts are added, and if none is reported the merge is `unavailable`
    rather than a zero. Rows carry through so the total stays re-derivable from the
    evidence: that is the property `evidence_ref` promises.
    """
    reported = [p for p in parts if p.usage.status == "reported"]
    if not reported:
        return unavailable("no source for this execution produced a usable total")
    rows: list[dict[str, Any]] = []
    models: list[str] = []
    notes: list[str] = []
    for part in reported:
        rows.extend(part.rows)
        notes.extend(part.notes)
        for m in part.main_chain_models:
            if m not in models:
                models.append(m)
    totals = {
        key: sum(int(getattr(p.usage, key) or 0) for p in reported)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    }
    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported", source=source, evidence_records=len(reported), **totals
        ),
        rows=rows,
        main_chain_models=models,
        notes=notes,
    )


def _write_evidence(usage_dir: Path, purpose: str, evidence: UsageEvidence) -> Path | None:
    """The rows behind a total, written where `evidence_ref` can point at them.

    Never the sources themselves — those are in `raw/` — and never message content. This
    file exists so a number on the spine can be re-derived by hand, and for no other
    purpose.
    """
    if evidence.usage.status != "reported":
        return None
    path = usage_dir / f"{purpose}-usage.json"
    path.write_text(
        json.dumps(
            {
                "purpose": purpose,
                "source": evidence.usage.source,
                "totals": {
                    "input_tokens": evidence.usage.input_tokens,
                    "cache_read_tokens": evidence.usage.cache_read_tokens,
                    "cache_write_tokens": evidence.usage.cache_write_tokens,
                    "output_tokens": evidence.usage.output_tokens,
                },
                "models": evidence.main_chain_models,
                "notes": evidence.notes,
                "rows": evidence.rows,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def harvest(req: HarvestRequest) -> HarvestResult:
    """Copy every source into the task root, attribute it, and total each purpose."""
    usage_dir = _usage_dir(req.task_root)
    notes: list[str] = []

    # 1. Copy first. Everything after this reads the copy, so a VM removed between the
    #    harvest and the next question costs nothing.
    copied: list[tuple[str, Path, Path]] = []   # (extractor, origin, kept)
    for extractor, origin in req.host_sources:
        if Path(origin).is_file():
            copied.append((extractor, Path(origin), _copy_in(req.task_root, Path(origin), "host")))
    for extractor, origin in req.sandbox_sources:
        if Path(origin).is_file():
            copied.append((extractor, Path(origin), _copy_in(req.task_root, Path(origin), "vm")))

    by_origin = {str(origin): (extractor, kept) for extractor, origin, kept in copied}

    # 2. Reviews, attributed strictly by the sidecar the wrapper wrote at the time.
    review_parts: list[UsageEvidence] = []
    review_refusals: list[str] = []
    claimed: set[str] = set()
    rounds = _review_rounds(req.task_root)
    for name, listed in rounds:
        if not listed:
            review_refusals.append(
                f"{name} named no transcript (a round from before the sidecar existed, or "
                "one whose sidecar could not be written)"
            )
            continue
        if len(listed) > 1:
            # One `claude -p` writes one session file. Two means something else moved in
            # the window — most plausibly the worker's own session — and adding it would
            # bill an entire worker run to a review.
            review_refusals.append(
                f"{name} named {len(listed)} transcripts, so which one it wrote is "
                "ambiguous and none is counted"
            )
            claimed.update(listed)
            continue
        named = listed[0]
        claimed.add(named)
        found = by_origin.get(named)
        if found is None:
            review_refusals.append(
                f"{name} named {named}, which the harvest could not read"
            )
            continue
        extractor, kept = found
        part = extract(extractor, kept.read_text().splitlines())
        for row in part.rows:
            row["source_file"] = kept.name
        if part.usage.status == "reported":
            review_parts.append(part)
        else:
            review_refusals.append(f"{name}: {part.usage.reason}")

    if not rounds:
        review = unavailable("this task saved no review, so no review execution consumed anything")
    elif review_parts and not review_refusals:
        review = _merge(review_parts, "review-transcripts")
    elif review_parts:
        review = _merge(review_parts, "review-transcripts")
        review.notes.append(
            "PARTIAL: " + "; ".join(review_refusals)
            + " — this total covers only the rounds that could be attributed"
        )
    else:
        review = unavailable("no review round could be attributed: " + "; ".join(review_refusals))

    # 3. Work: the surface the worker's own harness writes, and nothing a review claimed.
    harness = (req.work_identity or {}).get("harness")
    want = WORK_EXTRACTOR_BY_HARNESS.get(str(harness)) if harness else None
    if harness and want is None:
        work = unavailable(
            f"the {harness!r} harness exposes no usage surface this deployment can read"
        )
    elif want is None:
        # No identity, so no worker harness, so no way to say which surface was the
        # worker's. Everything unclaimed stays unattributed rather than being swept into
        # a total — a guess with a dollar figure on it is the one output worse than none.
        work = unavailable(
            "this task has no recorded route, so no worker surface could be identified"
        )
    else:
        work_parts = []
        for extractor, origin, kept in copied:
            if str(origin) in claimed:
                continue
            if extractor != want:
                # A transcript on a route whose worker writes a different surface is a
                # review nobody attributed. Kept, named, never counted.
                continue
            part = extract(extractor, kept.read_text().splitlines())
            for row in part.rows:
                row["source_file"] = kept.name
            if part.usage.status == "reported":
                work_parts.append(part)
        if work_parts:
            work = _merge(work_parts, str(want))
        else:
            work = unavailable(f"no readable {want} surface was found for this worker")

    unattributed = [
        kept.name for extractor, origin, kept in copied
        if str(origin) not in claimed
        and (want is None or extractor != want)
    ]
    if unattributed:
        notes.append(
            f"{len(unattributed)} harvested file(s) belong to no execution the spine "
            "records; they are kept as evidence and counted in nothing: "
            + ", ".join(sorted(unattributed))
        )

    work_ref = _write_evidence(usage_dir, "work", work)
    review_ref = _write_evidence(usage_dir, "review", review)
    if work_ref:
        work.usage.evidence_ref = str(work_ref)
    if review_ref:
        review.usage.evidence_ref = str(review_ref)

    manifest = usage_dir / "harvest.json"
    manifest.write_text(
        json.dumps(
            {
                "task": req.task,
                "worker_id": req.worker_id,
                "execution_id": req.execution_id,
                "attempt": req.attempt,
                "order_ref": req.order_ref,
                "sources": [
                    {"extractor": e, "origin": str(o), "kept": str(k.relative_to(req.task_root))}
                    for e, o, k in copied
                ],
                "review_rounds": [{"round": n, "transcripts": t} for n, t in rounds],
                "review_refusals": review_refusals,
                "unattributed": unattributed,
                "work": work.usage.model_dump(),
                "review": review.usage.model_dump(),
                "notes": notes,
            },
            indent=2,
        )
        + "\n"
    )
    return HarvestResult(
        work=work, review=review, manifest_path=manifest,
        unattributed=unattributed, notes=notes,
    )


def finished_payload(
    *,
    execution_id: str,
    purpose: str,
    attempt: int,
    identity: dict[str, Any],
    evidence: UsageEvidence,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """An `execution.finished` payload for a harvest, honest about what a harvest knows.

    `outcome_certainty` is always `uncertain`. The field exists for exactly this case —
    types.py: "A process it reaped itself is `certain`; one found already gone is
    `uncertain`" — and a harvest never reaps anything. `outcome` is `success` because the
    task reached the close that triggered this; it is the PROCESS view and a harvest
    cannot see a process, so the certainty flag is what carries that.

    `classification` is left unset. It is derived from the harness's structured output,
    which a harvest does not have, and `None` is what a reader is told never to read as
    `posted`.

    `price_basis` is null unless the catalog carries one. It is copied at approval time,
    and a harvest inventing one now would attach today's rates to a week-old execution —
    the precise error the field's docstring exists to prevent.
    """
    model_resolved = evidence.main_chain_models[0] if evidence.main_chain_models else None
    return {
        "execution_id": execution_id,
        "purpose": purpose,
        "attempt": attempt,
        "identity": identity,
        "outcome": "success",
        "outcome_certainty": "uncertain",
        "finished_at": finished_at or datetime.now(UTC).isoformat(),
        "model_resolved": model_resolved,
        "model_evidence": "harness-reported" if model_resolved else "none",
        "usage": evidence.usage.model_dump(),
        "price_basis": None,
    }
