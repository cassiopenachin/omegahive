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
import os
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
WORK_SURFACE_BY_HARNESS = {
    "claude-code": "claude-session",
    "opencode": "opencode-export",
    "codex": "codex-rollout",
}

# A SURFACE is a file shape, not a parser. One shape can need either of two parsers: a
# Claude session holds the harness's own `cost-state` total when the session was
# interactive, and holds only per-message records when it came from `claude -p` — which is
# every review. Naming the surface and choosing the parser at read time is what keeps a
# review from being recorded as unmeasurable merely because the better parser found
# nothing. The preference order is the accuracy order, best first.
EXTRACTORS_BY_SURFACE: dict[str, tuple[str, ...]] = {
    "claude-session": ("claude-code-cost-state", "claude-code-transcript"),
    "opencode-export": ("opencode-messages",),
    "codex-rollout": ("codex-rollout",),
}

# Surfaces a reviewer can write. A worker can write these too; which is why the entailment
# below is gated on the worker's own harness.
REVIEW_SURFACES = ("claude-session", "codex-rollout")


def _read(surface: str, path: Path, label: str) -> UsageEvidence:
    """Read one source, trying each parser the surface allows, best first."""
    last = unavailable(f"no parser is registered for the {surface!r} surface")
    for extractor in EXTRACTORS_BY_SURFACE.get(surface, ()):
        ev = extract(extractor, path.read_text().splitlines())
        if ev.usage.status == "reported":
            for row in ev.rows:
                row["source_file"] = label
            return ev
        last = ev
    return last


@dataclass
class Source:
    """One harvested file: what reads it, where it is now, and what it was called.

    `origin` and `path` differ for anything that came out of a sandbox: the file is a
    local copy, while the name the review sidecars use is the path INSIDE the VM. Keeping
    both is what lets a sidecar written by a reviewer at `/home/agent/.claude/...` be
    matched against a file the harvest pulled out to the host. Collapsing them would
    silently unattribute every sandboxed review.
    """

    surface: str
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
    # When the newest piece of evidence was last written, ISO-8601 UTC. Used as the
    # payload's `finished_at`, and derived rather than read off the clock so that two
    # harvests of one finished task produce byte-identical payloads — otherwise the
    # gateway's content-addressed idempotency cannot collapse them and one execution ends
    # up with two terminal facts. It is also the more truthful answer: a harvest does not
    # know when the process stopped, only when it last wrote.
    finished_at: str = ""
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
                sources.append(Source("claude-session", f))

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
        # A host route never had a sandbox, and saying so on every harvest of one would
        # be a note nobody reads. Anything ELSE that stops the listing is a real failure
        # — a VM that exists and cannot be entered is exactly the case worth naming.
        if "no sandbox named" not in out:
            notes.append(
                f"sandbox {sandbox!r} could not be read ({out.strip().splitlines()[0]})"
                if out.strip() else f"sandbox {sandbox!r} could not be read (exit {status})"
            )
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
        sources.append(Source("claude-session", local, origin=in_vm))

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
        # Stamp it from its own contents. Every other source is copied with its mtime
        # intact, but this one does not exist until now — so its mtime would be "now",
        # `finished_at` would move on every run, and the content-addressed idempotency
        # that makes re-harvesting safe would never collapse two harvests of one task.
        newest_ms = 0
        for line in out.splitlines():
            try:
                t = json.loads(line).get("time_completed")
            except json.JSONDecodeError:
                continue
            if isinstance(t, (int, float)):
                newest_ms = max(newest_ms, int(t))
        if newest_ms:
            os.utime(local, (newest_ms / 1000, newest_ms / 1000))
        sources.append(Source("opencode-export", local, origin=f"{sandbox}:{db}"))
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
        "time_completed": (o.get("time") or {}).get("completed"),
    }))
"""


def _usage_dir(task_root: Path) -> Path:
    d = task_root / "run" / "usage"
    (d / "raw").mkdir(parents=True, exist_ok=True)
    return d


def _copy_in(task_root: Path, origin: Path, subdir: str, taken: set[Path]) -> Path:
    """Copy a source into the task root, keeping its name.

    The name is kept because it is the only link back to the session it came from, which
    makes the two collision cases mean opposite things.

    WITHIN one harvest, two sources with one name are two different files, and losing
    either silently is the failure this module exists to stop — so the second is suffixed.

    ACROSS harvests it is the same source again, and this command is meant to be re-run:
    at close, and by hand afterwards. Suffixing there would leave the first copy AND a
    second beside it, both matching the same surface, and every total would double on the
    second run — silently, in the direction that makes everything look twice as expensive.
    So a name this run has not already taken is overwritten. Transcripts only grow, so the
    newer copy is a superset of the one it replaces.
    """
    dest_dir = _usage_dir(task_root) / "raw" / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / origin.name
    n = 1
    while dest in taken:
        dest = dest_dir / f"{origin.stem}.{n}{origin.suffix}"
        n += 1
    taken.add(dest)
    shutil.copy2(origin, dest)
    return dest


def _review_rounds(task_root: Path) -> list[tuple[str, list[str]]]:
    """Each saved review round and the transcripts its sidecar names, in round order.

    `run/reviews/` is the counted directory and `meta/` holds the sidecars — the same
    split the review wrapper enforces, for the same reason: a sidecar in the counted
    directory would match the round-counting glob and inflate the budget.

    A round is a file whose NAME contains `review`, which is the wrapper's own counting
    rule and has to stay the same rule. The directory also holds the worker's
    `disposition.md`; counting that made it an unattributable round, turned a clean
    harvest into a partial one, and put a refusal about a file that is not a review in
    front of the operator.
    """
    reviews = task_root / "run" / "reviews"
    if not reviews.is_dir():
        return []
    rounds = []
    for path in sorted(p for p in reviews.iterdir() if p.is_file() and "review" in p.name):
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


def _stamp(evidence: UsageEvidence, raw_root: Path) -> str:
    """When this execution's own evidence was last written, `...Z`.

    Per purpose rather than per task: a task-wide timestamp would say the review finished
    when the worker last wrote, which on a task whose reviews ran hours later is simply
    false. Derived from the files the rows actually cite, and from their mtimes rather
    than from the clock, so two harvests of one finished task produce byte-identical
    payloads and the gateway's content-addressed idempotency can collapse them.
    """
    names = {str(r.get("source_file")) for r in evidence.rows if r.get("source_file")}
    newest = 0.0
    for name in names:
        for path in raw_root.rglob(name):
            newest = max(newest, path.stat().st_mtime)
    if not newest:
        return ""
    return datetime.fromtimestamp(newest, tz=UTC).isoformat().replace("+00:00", "Z")


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
    copied: list[tuple[str, str, Path]] = []   # (surface, origin key, kept copy)
    taken: set[Path] = set()
    for src, subdir in [(s, "host") for s in req.host_sources] + [
        (s, "vm") for s in req.sandbox_sources
    ]:
        if src.path.is_file():
            copied.append(
                (src.surface, src.key(), _copy_in(req.task_root, src.path, subdir, taken))
            )

    by_origin = {origin: (surface, kept) for surface, origin, kept in copied}

    # 2. What the ROUTE entails, before anything is attributed. Nothing in an opencode,
    #    antigravity or codex sandbox runs Claude Code except the reviewer — so a Claude
    #    transcript there is a review by entailment, and matching it to a particular round
    #    is a refinement rather than a prerequisite.
    #
    #    The entailment is PER SURFACE, not per route, and getting that wrong left whole
    #    classes uncounted: the claude-opus route runs a CODEX reviewer, whose rollout is
    #    a shape the Claude worker never writes and so cannot be confused with. What
    #    genuinely needs a sidecar is only the case where the reviewer writes the SAME
    #    shape as the worker — a Claude reviewer on a Claude route, both filing into one
    #    home.
    harness = (req.work_identity or {}).get("harness")
    want = WORK_SURFACE_BY_HARNESS.get(str(harness)) if harness else None
    shares_surface_with_reviewer = want in REVIEW_SURFACES

    # 3. Reviews. Sidecars first, because they are exact and they carry the round name.
    review_parts: list[UsageEvidence] = []
    review_refusals: list[str] = []
    # Rounds whose sidecar names nothing. Not a refusal on its own: on a route the
    # entailment covers, the transcripts are still found and the only thing lost is which
    # round wrote which. On a Claude route it IS the refusal, because there the sidecar is
    # the only thing separating a review's transcript from the worker's.
    unmatched_rounds: list[str] = []
    claimed: set[str] = set()
    rounds = _review_rounds(req.task_root)
    for name, listed in rounds:
        if len(listed) > 1 and shares_surface_with_reviewer:
            # One `claude -p` writes one session file. Two, on a route where the worker
            # writes transcripts too, most plausibly means the worker's own session moved
            # in the window — and adding it would bill an entire worker run to a review.
            review_refusals.append(
                f"{name} named {len(listed)} transcripts and this route's worker writes "
                "the same shape, so which one it wrote is ambiguous"
            )
            claimed.update(listed)
            continue
        for named in listed:
            claimed.add(named)
            found = by_origin.get(named)
            if found is None:
                review_refusals.append(f"{name} named {named}, which the harvest could not read")
                continue
            part = _read(found[0], found[1], found[1].name)
            if part.usage.status == "reported":
                review_parts.append(part)
            else:
                review_refusals.append(f"{name}: {part.usage.reason}")
        if not listed:
            unmatched_rounds.append(name)

    # 4. On a route the entailment covers, every remaining reviewer-shaped source is a
    #    review — including the rounds whose sidecars predate this mechanism.
    entailed = 0
    # Gated on knowing the ROUTE, not on the route being measurable. Requiring a readable
    # worker surface disabled the entailment exactly where it is strongest: an antigravity
    # worker writes no usage at all, so it cannot be the author of a Claude transcript in
    # its own sandbox and every one of them is a review.
    if req.work_identity is not None:
        for surface, origin, kept in copied:
            if origin in claimed or surface == want:
                continue
            if surface not in REVIEW_SURFACES:
                continue
            claimed.add(origin)
            part = _read(surface, kept, kept.name)
            if part.usage.status == "reported":
                review_parts.append(part)
                entailed += 1
            else:
                review_refusals.append(f"{kept.name}: {part.usage.reason}")

    if not rounds and not review_parts:
        review = unavailable("this task saved no review, so no review execution consumed anything")
    elif review_parts:
        review = _merge(review_parts, "review-transcripts")
        if entailed:
            review.notes.append(
                f"{entailed} transcript(s) were attributed to the review by the route "
                "rather than by a sidecar: this worker's harness writes none, so nothing "
                "else in its environment could have written them"
            )
        if unmatched_rounds:
            review.notes.append(
                f"{len(unmatched_rounds)} round(s) named no transcript and are covered in "
                f"aggregate rather than individually: {', '.join(unmatched_rounds)}"
            )
        if review_refusals:
            review.notes.append(
                "PARTIAL: " + "; ".join(review_refusals)
                + " — this total covers only what could be attributed"
            )
    else:
        why = review_refusals + [
            f"{n} named no transcript" for n in unmatched_rounds
        ]
        review = unavailable("no review round could be attributed: " + "; ".join(why))

    # 5. Work: the surface the worker's own harness writes, and nothing a review claimed.
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
        for surface, origin, kept in copied:
            if origin in claimed or surface != want:
                continue
            part = _read(surface, kept, kept.name)
            if part.usage.status == "reported":
                work_parts.append(part)
        if work_parts:
            work = _merge(work_parts, str(want))
        else:
            work = unavailable(f"no readable {want} surface was found for this worker")

    unattributed = [
        kept.name for surface, origin, kept in copied
        if origin not in claimed and (want is None or surface != want)
    ]
    if unattributed:
        notes.append(
            f"{len(unattributed)} harvested file(s) belong to no execution the spine "
            "records; they are kept as evidence and counted in nothing: "
            + ", ".join(sorted(unattributed))
        )

    raw_root = usage_dir / "raw"
    work.finished_at = _stamp(work, raw_root)
    review.finished_at = _stamp(review, raw_root)

    work_ref = _write_evidence(usage_dir, "work", work)
    review_ref = _write_evidence(usage_dir, "review", review)
    if work_ref:
        work.usage.evidence_ref = str(work_ref)
    if review_ref:
        review.usage.evidence_ref = str(review_ref)

    newest = max((k.stat().st_mtime for _, _, k in copied), default=0.0)
    # `...Z`, not `+00:00`: the gateway validates the shape and refuses the latter, and a
    # payload refused at emit time is a harvest that ran and recorded nothing.
    finished_at = (
        datetime.fromtimestamp(newest, tz=UTC).isoformat().replace("+00:00", "Z")
        if newest else ""
    )

    manifest = usage_dir / "harvest.json"
    manifest.write_text(
        json.dumps(
            {
                "task": req.task,
                "worker_id": req.worker_id,
                "execution_id": req.execution_id,
                "attempt": req.attempt,
                "order_ref": req.order_ref,
                "finished_at": finished_at,
                "sources": [
                    {"surface": e, "origin": o, "kept": str(k.relative_to(req.task_root))}
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
        unattributed=unattributed, notes=notes, finished_at=finished_at,
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
    # WHICH model, when the evidence names several. A session's quota and title calls run
    # on a cheaper model beside the pinned one, so "whichever came first" is a coin toss —
    # and the gateway refuses a success whose resolved model does not match the pinned
    # one, so losing that toss loses the whole fact.
    #
    # The pinned model wins when the evidence names it: the harness DID report running it,
    # and the auxiliary traffic beside it is not what the execution ran. A single reported
    # model that is NOT the pinned one is reported as itself — a genuine mismatch is a
    # fact, and being refused is the correct outcome for an execution that ran something
    # other than what was approved. Several models, none of them pinned, resolves to
    # nothing: picking one would be either a false mismatch or a false confirmation.
    models = evidence.main_chain_models
    pinned = identity.get("model")
    if pinned and pinned in models:
        model_resolved: str | None = str(pinned)
    elif len(models) == 1:
        model_resolved = models[0]
    else:
        model_resolved = None
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


# --- the host entry point --------------------------------------------------------------
#
# Invoked by `hive-usage`, which runs on the HOST because that is where `~/.claude`,
# `~/.codex` and `sbx` are — the containerised CLI can reach none of them. It prints one
# JSON object and emits nothing: the spine write stays in the shell, with the same `emit`
# helper, the same actor and the same refusal handling every other operator-tier write
# uses.


def _sbx_runner(sbx_cmd: str) -> Callable[[list[str]], tuple[int, str]]:
    import shlex
    import subprocess

    base = shlex.split(sbx_cmd)

    def run(argv: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(  # noqa: S603
                base + argv, capture_output=True, text=True, timeout=300
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        # stdout is the payload for the export and the listing; stderr only matters when
        # something failed, and folding it in then is what makes a note say anything.
        return p.returncode, p.stdout if p.returncode == 0 else (p.stdout + p.stderr)

    return run


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import tempfile

    from omegahive.harness.plan import execution_id_for
    from omegahive.harness.records import RefusalError, load_catalog, resolve_reviewer_route

    ap = argparse.ArgumentParser(prog="omegahive-harvest", description=__doc__)
    ap.add_argument("--task", required=True)
    ap.add_argument("--task-root", required=True, type=Path)
    ap.add_argument("--worker", required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--order-ref", default=None)
    ap.add_argument("--execution-id", default=None)
    ap.add_argument("--identity", default=None, help="the work execution's identity, JSON")
    ap.add_argument("--catalog", type=Path, default=None)
    ap.add_argument("--sandbox", default=None)
    ap.add_argument("--sbx-cmd", default=os.environ.get("HIVE_SBX_CMD", "sbx"))
    ap.add_argument("--home", type=Path, default=Path(os.path.expanduser("~")))
    ap.add_argument("--codex-home", type=Path,
                    default=Path(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))))
    args = ap.parse_args(argv)

    notes: list[str] = []
    work_identity = json.loads(args.identity) if args.identity else None

    reviewer_identity = None
    if args.catalog and args.catalog.is_file():
        try:
            entry = resolve_reviewer_route(load_catalog(args.catalog.read_bytes()))
            reviewer_identity = {
                "route": entry.name, "model_vendor": entry.model_vendor,
                "provider": entry.provider, "model": entry.model, "harness": entry.harness,
                "billing_market": entry.billing_market,
                "credential_pool": entry.credential_pool, "adapter": entry.adapter,
            }
        except RefusalError as exc:
            notes.append(f"{exc.code}: {exc.message}")
    else:
        notes.append("no catalog was given, so a review cannot be attributed to a route")

    host_sources = locate_host_sources(
        task_root=args.task_root, home=args.home, codex_home=args.codex_home
    )
    sandbox_sources: list[Source] = []
    if args.sandbox:
        staging = Path(tempfile.mkdtemp(prefix="hive-usage-"))
        sandbox_sources, pull_notes = pull_sandbox_sources(
            sandbox=args.sandbox, staging=staging, runner=_sbx_runner(args.sbx_cmd)
        )
        notes.extend(pull_notes)

    result = harvest(HarvestRequest(
        task=args.task, task_root=args.task_root, worker_id=args.worker,
        work_identity=work_identity, reviewer_identity=reviewer_identity,
        execution_id=args.execution_id, attempt=args.attempt, order_ref=args.order_ref,
        host_sources=host_sources, sandbox_sources=sandbox_sources,
    ))
    notes.extend(result.notes)

    payloads: dict[str, Any] = {"work": None, "review": None}
    if not result.finished_at:
        # No evidence at all was found, so there is nothing to report but an absence — and
        # an absence emitted with a wall-clock timestamp would duplicate on every re-run.
        # The manifest still records what was looked for.
        notes.append(
            "no usage evidence of any kind was found for this task; nothing is emitted, "
            f"and what was searched is recorded in {result.manifest_path}"
        )
    else:
        if work_identity and args.execution_id:
            payloads["work"] = finished_payload(
                execution_id=args.execution_id, purpose="work", attempt=args.attempt,
                identity=work_identity, evidence=result.work,
                finished_at=result.work.finished_at or result.finished_at,
            )
        elif result.work.usage.status == "reported":
            notes.append("the work surface was read but has no identity to attribute it to")
        if reviewer_identity and args.order_ref and result.review.usage.status == "reported":
            payloads["review"] = finished_payload(
                execution_id=execution_id_for(
                    task=args.task, order_ref=args.order_ref,
                    purpose="review", attempt=args.attempt,
                ),
                purpose="review", attempt=args.attempt,
                identity=reviewer_identity, evidence=result.review,
                finished_at=result.review.finished_at or result.finished_at,
            )
        elif result.review.usage.status == "reported":
            notes.append("the reviews were read but have no identity to attribute them to")

    print(json.dumps({
        "manifest": str(result.manifest_path),
        "work": payloads["work"],
        "review": payloads["review"],
        "work_status": result.work.usage.status,
        "work_reason": result.work.usage.reason,
        "review_status": result.review.usage.status,
        "review_reason": result.review.usage.reason,
        "unattributed": result.unattributed,
        "notes": notes,
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `hive-usage`
    raise SystemExit(main())
