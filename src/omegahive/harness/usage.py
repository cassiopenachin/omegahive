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


def extract_codex_rollout(lines: Iterable[str]) -> UsageEvidence:
    """Read Codex's own session rollout (`sessions/**/rollout-*.jsonl`).

    Codex reports consumption per TURN, not per message, and it reports it
    CUMULATIVELY: each `turn.completed` (and its rollout-side equivalent) carries the
    running totals for the thread, not that turn's delta. Summing them inflates by
    roughly the triangular number of turns — the same class of silent, one-directional
    error the Claude Code extractor's per-content-block duplication produces, and in
    the same direction. So the rule here is LAST WINS, not sum: the final usage object
    in the file is the thread's total.

    That is checked rather than assumed. If the counts do not increase monotonically
    the file is not the cumulative shape this parser was written against, and the
    extraction says so in its notes rather than quietly reporting the last row of
    something it misread.

    MODEL ATTRIBUTION IS WEAKER HERE THAN ON CLAUDE CODE, and the note says so on
    every extraction. Codex records the model it was CONFIGURED with; its event stream
    carries no server-resolved identity. That is a harness statement, not a provider
    one, and a launch alias must never be promoted into a resolved id on its strength.
    """
    records, bad = _iter_json_lines(lines)

    rows: list[dict[str, Any]] = []
    models: list[str] = []
    for rec in records:
        raw_payload = rec.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        # Two spellings of one number, read by one parser because the supervisor sees
        # the rollout and the probe runner sees the `--json` stream, and two parsers
        # for one vendor would drift:
        #   rollout: {"type":"event_msg","payload":{"type":"token_count",
        #             "info":{"total_token_usage":{...}}}}
        #   stream:  {"type":"turn.completed","usage":{...}}
        usage: Any = None
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            usage = info["total_token_usage"]
        elif isinstance(rec.get("usage"), dict):
            usage = rec["usage"]
        if isinstance(usage, dict) and any(
            k in usage for k in ("input_tokens", "output_tokens")
        ):
            rows.append(
                {
                    "message_id": f"turn-{len(rows)}",
                    "model": None,
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cached_input_tokens") or 0),
                    "cache_write_tokens": int(usage.get("cache_write_input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "sidechain": False,
                }
            )

        # Model attribution comes from `turn_context` ONLY. The rollout mentions a
        # model id in several places — the collaboration-mode block, a compaction
        # setting — and treating every mention as a main-chain model would make an
        # ordinary nested setting look like a mid-session model switch, which the
        # caller turns into a terminal failure. One record kind, deliberately.
        if rec.get("type") == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m and m not in models:
                models.append(m)

    if not rows:
        reason = "codex rollout held no usage records"
        if bad:
            reason += f" ({bad} unparseable line(s))"
        return unavailable(reason)

    notes: list[str] = []
    keys = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    monotonic = all(
        all(rows[i][k] <= rows[i + 1][k] for k in keys) for i in range(len(rows) - 1)
    )
    if monotonic:
        final = rows[-1]
        totals = {k: final[k] for k in keys}
        notes.append(
            f"{len(rows)} cumulative usage record(s); the last one is the thread total "
            "(codex reports running totals per turn, so summing would inflate)"
        )
    else:
        totals = {k: sum(r[k] for r in rows) for k in keys}
        notes.append(
            f"{len(rows)} usage record(s) did NOT increase monotonically, so they were "
            "summed as per-turn deltas rather than read as cumulative totals. Verify "
            "against the harness before trusting this number"
        )
    if bad:
        notes.append(f"{bad} unparseable line(s) skipped (truncated rollout is expected)")
    notes.append(
        "model identity is the harness's CONFIGURED model, not a server-resolved id: "
        "codex exposes no resolved-model surface, so this must not be read as proof of "
        "which model answered"
    )

    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="codex-rollout",
            evidence_records=len(rows),
            **totals,
        ),
        rows=rows,
        main_chain_models=models,
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


_EXTRACTORS = {
    "claude-code-transcript": extract_claude_code_transcript,
    "codex-rollout": extract_codex_rollout,
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
