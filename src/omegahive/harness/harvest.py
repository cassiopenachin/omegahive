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

import base64
import json
import shutil
from collections.abc import Callable
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
class Source:
    """One harvested file: what reads it, where it is now, and what it was called.

    `origin` and `path` differ for anything that came out of a sandbox: the file is a
    local copy, while the name the review sidecars use is the path INSIDE the VM. Keeping
    both is what lets a sidecar written by a reviewer at `/home/agent/.claude/...` be
    matched against a file the harvest pulled out to the host. Collapsing them would
    silently unattribute every sandboxed review.
    """

    extractor: str
    path: Path
    origin: str | None = None

    def key(self) -> str:
        return self.origin if self.origin is not None else str(self.path)


@dataclass
class HarvestRequest:
    """Everything the harvest needs, resolved by its caller.

    `host_sources` and `sandbox_sources` are `Source`s the caller has already located —
    on the host, or copied out of the sandbox. Locating them needs a live `sbx` and the
    operator's home; deciding what they MEAN does not, and keeping the two apart is what
    makes this module testable.
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
    host_sources: list[Source] = field(default_factory=list)
    sandbox_sources: list[Source] = field(default_factory=list)


@dataclass
class HarvestResult:
    work: UsageEvidence
    review: UsageEvidence
    manifest_path: Path
    # Files copied into the task root that no execution claims. Named so the operator can
    # see what the harvest could not place, rather than having it vanish.
    unattributed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def locate_host_sources(
    *, task_root: Path, home: Path, codex_home: Path
) -> list[Source]:
    """Every usage surface on the HOST that belongs to this task root.

    Two harnesses write outside the task root and have to be found by other means.

    Claude Code files a session under a directory named for the cwd it ran in, with the
    separators flattened — `/home/x/work/sess-t-0914/hive` becomes
    `-home-x-work-sess-t-0914-hive`. A task root usually has several such directories,
    one per repo the worker touched, so the match is on the flattened task root plus a
    separator. Plus a separator, and not a bare prefix: `sess-t-0914` is a prefix of
    `sess-t-0914b`, and a bare prefix would put a neighbouring task's tokens on this
    task's bill.

    codex files by date instead, so its rollouts are found by reading the `cwd` off each
    `session_meta` header and keeping the ones under this task root. Only the first line
    of each file is read; a rollout is megabytes and the header is the first record.
    """
    sources: list[Source] = []
    slug = str(task_root).replace("/", "-")
    projects = home / ".claude" / "projects"
    if projects.is_dir():
        for d in sorted(projects.iterdir()):
            if not d.is_dir():
                continue
            if d.name != slug and not d.name.startswith(slug + "-"):
                continue
            for f in sorted(d.glob("*.jsonl")):
                sources.append(Source("claude-code-cost-state", f))

    sessions = codex_home / "sessions"
    if sessions.is_dir():
        for f in sorted(sessions.rglob("rollout-*.jsonl")):
            try:
                with f.open() as fh:
                    head = json.loads(fh.readline() or "{}")
            except (OSError, json.JSONDecodeError):
                continue
            cwd = str((head.get("payload") or {}).get("cwd") or "")
            if cwd == str(task_root) or cwd.startswith(str(task_root) + "/"):
                sources.append(Source("codex-rollout", f))
    return sources


def pull_sandbox_sources(
    *, sandbox: str, staging: Path, runner: Callable[[list[str]], tuple[int, str]]
) -> tuple[list[Source], list[str]]:
    """Copy this task's sandbox surfaces onto the host, before the VM is pruned.

    `runner` is the seam — it takes an argv and returns (exit status, stdout) — so the
    decision logic here is exercised without a live `sbx`, matching how `HIVE_CLI_CMD`
    and `HIVE_MODEL_PROBE_CMD` are already stubbed elsewhere.

    Claude transcripts come out with `sbx cp`, which is byte-exact. opencode's store does
    NOT: it is SQLite with a write-ahead log, and copying `opencode.db` alone silently
    drops everything still in the `-wal` — which on a session that just ended is most of
    it. So the export runs INSIDE the VM, where SQLite can read the log, and only the
    resulting JSONL crosses the boundary.

    Every failure is returned as a note rather than raised. A sandbox that has been
    removed, or that holds neither surface, is an ordinary outcome of harvesting an old
    task, and it must not cost the surfaces that WERE readable.
    """
    notes: list[str] = []
    sources: list[Source] = []
    staging.mkdir(parents=True, exist_ok=True)

    status, out = runner(["exec", sandbox, "--", "sh", "-lc",
                          "ls -1 /home/agent/.claude/projects/*/*.jsonl 2>/dev/null"])
    if status != 0:
        notes.append(f"sandbox {sandbox!r} could not be read ({out.strip() or 'no output'})")
        return sources, notes
    for line in out.splitlines():
        in_vm = line.strip()
        if not in_vm.endswith(".jsonl"):
            continue
        local = staging / Path(in_vm).name
        cp_status, cp_out = runner(["cp", f"{sandbox}:{in_vm}", str(staging) + "/"])
        if cp_status != 0 or not local.is_file():
            notes.append(f"could not copy {in_vm} out of {sandbox} ({cp_out.strip()})")
            continue
        # The transcript is read for its cost-state record, and the per-message
        # derivation is the fallback the extractor itself does not make — a review run
        # with `claude -p` writes no cost-state, so this is the surface that has one only
        # sometimes. `extract` returns `unavailable` when it is missing, and the caller
        # retries with the transcript reader.
        sources.append(Source("claude-code-cost-state", local, origin=in_vm))

    db = "/home/agent/.local/share/opencode/opencode.db"
    # base64 rather than a heredoc or `python3 -c`: the export is multi-line Python
    # travelling through `sh -lc` inside an argv, and every quoting scheme that survives
    # that is one someone will break later without noticing. This one has nothing to
    # quote.
    encoded = base64.b64encode(_OPENCODE_EXPORT.encode()).decode()
    # Exit 3 says "this sandbox has no opencode store", which is the ordinary answer for
    # every claude and antigravity route and must not be reported as a failure. Anything
    # else that fails IS one, and gets named. Distinguishing them is the difference
    # between a harvest that is quiet when it should be and one nobody reads.
    status, out = runner([
        "exec", sandbox, "--", "sh", "-lc",
        f"if [ -f {db} ]; then echo {encoded} | base64 -d | python3 -; else exit 3; fi",
    ])
    if status == 0 and out.strip():
        local = staging / "opencode-messages.jsonl"
        local.write_text(out)
        sources.append(Source("opencode-messages", local, origin=f"{sandbox}:{db}"))
    elif status == 3:
        pass
    elif status != 0:
        detail = out.strip().splitlines()[-1] if out.strip() else f"exit {status}"
        notes.append(f"opencode export from {sandbox} failed ({detail})")
    return sources, notes


# Runs inside the sandbox. Reads the message rows and writes one JSON object per line —
# ids, model, the counts and the harness's own cost, and no message content, which is the
# same line `harness.usage` draws for its evidence rows.
_OPENCODE_EXPORT = """
import json, sqlite3
c = sqlite3.connect(
    "file:/home/agent/.local/share/opencode/opencode.db?mode=ro", uri=True)
for (data,) in c.execute("select data from message order by time_created"):
    try:
        o = json.loads(data)
    except Exception:
        continue
    if o.get("role") != "assistant":
        continue
    print(json.dumps({
        "id": o.get("id") or o.get("parentID"),
        "role": "assistant",
        "modelID": o.get("modelID"),
        "cost": o.get("cost"),
        "tokens": o.get("tokens"),
    }))
"""


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
    copied: list[tuple[str, str, Path]] = []   # (extractor, origin key, kept copy)
    for src, subdir in [(s, "host") for s in req.host_sources] + [
        (s, "vm") for s in req.sandbox_sources
    ]:
        if src.path.is_file():
            copied.append((src.extractor, src.key(), _copy_in(req.task_root, src.path, subdir)))

    by_origin = {origin: (extractor, kept) for extractor, origin, kept in copied}

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
            if origin in claimed:
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
        if origin not in claimed and (want is None or extractor != want)
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
                    {"extractor": e, "origin": o, "kept": str(k.relative_to(req.task_root))}
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
