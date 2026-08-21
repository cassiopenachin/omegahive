"""Normalizing what a harness reports it consumed — and refusing to invent the rest.

One rule governs this module: a number here is either something a provider or harness
reported, or it is absent. There is no estimate, no transcript heuristic, no model
self-report, and above all no zero standing in for an unread surface. `ExecutionUsage`
enforces that structurally; this module is what feeds it honestly.

The second rule is auditability. A normalized total that cannot be checked is a claim,
not a measurement, so every extractor returns the per-message evidence rows behind its
sum — message id, model, the four counts, and whether the record was subagent traffic.
Never message content: the evidence must be sufficient to re-derive the total and
insufficient to reconstruct a private transcript.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from omegahive.events.types import ExecutionUsage


@dataclass
class UsageEvidence:
    """A normalized total, the rows that justify it, and what the harness resolved."""

    usage: ExecutionUsage
    rows: list[dict[str, Any]] = field(default_factory=list)
    # Distinct model ids seen on the MAIN chain (subagent traffic excluded — see below).
    main_chain_models: list[str] = field(default_factory=list)
    # Everything the extractor noticed that a reader would want in the record.
    notes: list[str] = field(default_factory=list)


def unavailable(reason: str) -> UsageEvidence:
    """The honest empty result. `reason` is required and becomes part of the fact."""
    return UsageEvidence(usage=ExecutionUsage(status="unavailable", reason=reason))


def _iter_json_lines(lines: Iterable[str]) -> tuple[list[dict], int]:
    """Parse JSONL leniently, counting what did not parse.

    Lenient because a transcript from an interrupted session routinely ends in a
    half-written line; that is a truncated record, not a corrupt file, and refusing the
    whole extraction over it would turn every interrupted run into `unavailable`. The
    count of unparsed lines is reported rather than swallowed.
    """
    records: list[dict] = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            bad += 1
    return records, bad


def extract_claude_code_transcript(lines: Iterable[str]) -> UsageEvidence:
    """Read Claude Code's own session transcript (`<session-id>.jsonl`).

    THE COUNTING HAZARD, measured on a real transcript 2026-08-13: Claude Code writes
    ONE record PER CONTENT BLOCK of an assistant message — the text block, each thinking
    block, each tool_use block — and EVERY one of those records repeats the FULL
    message-level `usage` object. On the session used to verify this, 49 assistant
    records carried only 23 distinct `message.id`s, and naively summing `output_tokens`
    across records gave 29,740 against a true 11,580: an inflation of 2.57x, silent, and
    in the direction that makes a cheap model look expensive.

    The duplicates were checked to be identical rather than progressive: for every
    repeated id, the (input, cache_read, cache_write, output) tuple was the same in
    every record. So deduplicating by `message.id` and taking any one record is correct,
    and summing is not. This is the mirror of the "unavailable becomes a false zero"
    risk — same class, opposite sign.

    Model attribution splits from token attribution on purpose. TOKENS count every
    message including subagent (`isSidechain`) traffic, because a subagent's tokens are
    genuinely consumed by this task. The RESOLVED MODEL is read from main-chain records
    only, because a subagent may legitimately run on a different model than the session
    was pinned to; letting sidechain models into that set would make an ordinary
    delegation look like a routing violation.
    """
    records, bad = _iter_json_lines(lines)

    by_id: dict[str, dict[str, Any]] = {}
    seen_records = 0
    idless = 0
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        seen_records += 1
        mid = message.get("id")
        if not isinstance(mid, str) or not mid:
            # No id means no dedup key. Counting it would risk double-counting a
            # message whose other blocks DO carry ids, so it is dropped — and counted
            # here so the notes can report it rather than losing it silently.
            idless += 1
            continue
        if mid in by_id:
            continue
        by_id[mid] = {
            "message_id": mid,
            "model": message.get("model"),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "sidechain": bool(rec.get("isSidechain")),
        }

    if not by_id:
        reason = "claude code transcript held no assistant usage records"
        if bad:
            reason += f" ({bad} unparseable line(s))"
        return unavailable(reason)

    rows = list(by_id.values())
    totals = {
        key: sum(r[key] for r in rows)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    }

    main_models: list[str] = []
    for r in rows:
        if not r["sidechain"] and isinstance(r["model"], str) and r["model"] not in main_models:
            main_models.append(r["model"])

    notes = [f"{seen_records} usage record(s) deduplicated to {len(rows)} message(s)"]
    if bad:
        notes.append(f"{bad} unparseable line(s) skipped (truncated transcript is expected)")
    # An id-less record is excluded from the totals, and saying so is the whole point:
    # without this note, tokens dropped for want of a dedup key are indistinguishable
    # from ordinary content-block deduplication, and the total silently under-reports.
    # Under-reporting is the same class of lie as the false zero, just quieter.
    if idless:
        notes.append(
            f"{idless} usage record(s) had no message id and were EXCLUDED from the "
            "totals (no dedup key; counting them risks double-counting)"
        )
    sidechain_rows = sum(1 for r in rows if r["sidechain"])
    if sidechain_rows:
        notes.append(f"{sidechain_rows} message(s) were subagent traffic (counted in totals)")

    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="claude-code-transcript",
            evidence_records=len(rows),
            **totals,
        ),
        rows=rows,
        main_chain_models=main_models,
        notes=notes,
    )


def extract_fake_usage_file(lines: Iterable[str]) -> UsageEvidence:
    """The fixture surface: one JSON object per line, already one row per message.

    Deliberately a DIFFERENT shape from the Claude Code transcript so a test that
    passes here is not quietly testing the same parser twice.
    """
    records, bad = _iter_json_lines(lines)
    rows = []
    for rec in records:
        if "message_id" not in rec:
            continue
        rows.append(
            {
                "message_id": rec["message_id"],
                "model": rec.get("model"),
                "input_tokens": int(rec.get("input_tokens") or 0),
                "cache_read_tokens": int(rec.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(rec.get("cache_write_tokens") or 0),
                "output_tokens": int(rec.get("output_tokens") or 0),
                "sidechain": bool(rec.get("sidechain")),
            }
        )
    if not rows:
        return unavailable(
            "fake usage file held no records"
            + (f" ({bad} unparseable line(s))" if bad else "")
        )
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        seen.setdefault(r["message_id"], r)
    deduped = list(seen.values())
    totals = {
        key: sum(r[key] for r in deduped)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    }
    main_models: list[str] = []
    for r in deduped:
        if not r["sidechain"] and isinstance(r["model"], str) and r["model"] not in main_models:
            main_models.append(r["model"])
    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="fake-usage-file",
            evidence_records=len(deduped),
            **totals,
        ),
        rows=deduped,
        main_chain_models=main_models,
        notes=[f"{len(rows)} record(s) deduplicated to {len(deduped)} message(s)"],
    )


def extract_codex_turn_stream(lines: Iterable[str]) -> UsageEvidence:
    """Codex's own `codex exec --json` stream: one `turn.completed.usage` per turn.

    Measured against codex-cli 0.147.0 (probe, 2026-08-21). The block is per TURN, not
    per message, so there is exactly one evidence row per completed turn and the
    `message_id` is the turn's ordinal within this stream — an honest description of what
    the harness reports rather than a per-message shape invented to look like the Claude
    one.

    Two of Codex's five counts fold into this schema and one does not:
    `cached_input_tokens` is cache read, `cache_write_input_tokens` is cache write, and
    `reasoning_output_tokens` is a SUBSET of `output_tokens` on this build, so it is
    recorded in the row and deliberately not added to the total — adding it would double
    count. `input_tokens` is reported inclusive of the cached part, so the cached part is
    subtracted out to keep `input_tokens` meaning the same thing it means on every other
    row in this module.

    The model is absent because the stream never names it: `model_resolved` on a codex
    execution stays `null` with a named reason, and no row here pretends otherwise.
    """
    records, bad = _iter_json_lines(lines)
    rows = []
    for i, rec in enumerate(records, start=1):
        if rec.get("type") != "turn.completed":
            continue
        usage = rec.get("usage")
        if not isinstance(usage, dict):
            continue
        total_input = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cached_input_tokens") or 0)
        rows.append(
            {
                "message_id": f"turn-{i}",
                "model": None,
                "input_tokens": max(0, total_input - cache_read),
                "cache_read_tokens": cache_read,
                "cache_write_tokens": int(usage.get("cache_write_input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
                "sidechain": False,
            }
        )
    if not rows:
        return unavailable(
            "the codex stream carried no `turn.completed` usage block"
            + (f" ({bad} unparseable line(s))" if bad else "")
        )
    totals = {
        key: sum(int(r[key] or 0) for r in rows)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    }
    notes = [
        "codex reports usage per turn, not per message; one row per `turn.completed`",
        "`input_tokens` is reported inclusive of the cached part and is recorded here "
        "net of it, so the four counts do not double-count",
        "`reasoning_output_tokens` is a subset of `output_tokens` on 0.147.0 and is kept "
        "on the row without being added to the total",
        "codex exposes no resolved model id in this stream; every row's model is null",
    ]
    if bad:
        notes.append(f"{bad} unparseable line(s) in the stream")
    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="codex-turn-stream",
            evidence_records=len(rows),
            **totals,
        ),
        rows=rows,
        main_chain_models=[],
        notes=notes,
    )


_EXTRACTORS = {
    "claude-code-transcript": extract_claude_code_transcript,
    "codex-turn-stream": extract_codex_turn_stream,
    "fake-usage-file": extract_fake_usage_file,
}


def extract(extractor: str, lines: Iterable[str]) -> UsageEvidence:
    """Dispatch to a named extractor.

    `none` is a first-class answer, not an error: a harness whose consumption surface
    this deployment has not established records `unavailable` with that stated reason.
    An unknown extractor name is also `unavailable` rather than an exception — losing
    the whole finished fact over a parser lookup would trade a known-unknown for a
    missing terminal event, which is strictly worse.
    """
    if extractor == "none":
        return unavailable("harness has no usage surface established on this deployment")
    fn = _EXTRACTORS.get(extractor)
    if fn is None:
        return unavailable(f"no usage extractor named {extractor!r} in this build")
    return fn(lines)
