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
    # When this evidence was last written, `...Z`. Set by the harvest, which knows where
    # the files are; an extractor reads lines and has no file to stat.
    finished_at: str = ""


def unavailable(reason: str) -> UsageEvidence:
    """The honest empty result. `reason` is required and becomes part of the fact."""
    return UsageEvidence(usage=ExecutionUsage(status="unavailable", reason=reason))


# Claude Code's marker for a message it generated locally rather than received from a
# model — an API error surfaced as an assistant turn is the common case. Its TOKENS are
# real and count; its "model" is not a model, and reporting it as the resolved one gets
# the whole execution fact refused, since the gateway rules that a resolved model which
# does not match the pinned one cannot be a success.
_NOT_A_MODEL = frozenset({"<synthetic>"})


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
        model = r["model"]
        if r["sidechain"] or not isinstance(model, str) or model in _NOT_A_MODEL:
            continue
        if model not in main_models:
            main_models.append(model)

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


def extract_claude_code_cost_state(lines: Iterable[str]) -> UsageEvidence:
    """Read Claude Code's OWN running total (`{"type":"cost-state"}`) rather than
    re-deriving one from the per-message records.

    Preferred over `extract_claude_code_transcript` wherever the record exists, because
    it is the harness's own answer to the question rather than ours. Measured against
    one real session 2026-09-14, the per-message derivation came out LOW — output
    258,841 against 293,124, cache read 34.6M against 35.8M, about 11% under. The gap is
    traffic that never appears as an `assistant` record with a usage block (quota and
    title calls, and messages folded away by compaction). Under-reporting is the same
    class of lie as the false zero, so where the harness states a total, that total wins.

    THE COUNTING HAZARD here is the mirror of the transcript's. Claude Code REWRITES
    cost-state as the session grows, so a long session's file holds many of them and each
    is a running total, not a delta. Summing them multiplies the bill by the number of
    records. The last one is the answer.

    `costUSD` is the best cost figure available anywhere in this deployment — the vendor's
    own arithmetic, including cache-TTL premiums no rate card here could reconstruct. It
    still may not enter `ExecutionUsage`, which structurally holds no currency (types.py
    `PriceBasis`: cost is derived, never authored). So it is kept on the evidence rows and
    named in the notes, where a reader can find it and no projection can mistake it for a
    fact the spine asserts.

    Every model in the session counts toward the tokens. A route pins one model, but the
    session's quota and title traffic runs on a cheaper one and those tokens were still
    consumed by this task. `main_chain_models` therefore lists all of them: unlike the
    transcript surface, cost-state does not distinguish subagent traffic, and inventing
    that distinction here would be a claim the record does not support.
    """
    records, bad = _iter_json_lines(lines)
    states = [r for r in records if r.get("type") == "cost-state"]
    if not states:
        reason = "claude code transcript held no cost-state record"
        if bad:
            reason += f" ({bad} unparseable line(s))"
        return unavailable(reason)

    state = states[-1]
    model_usage = state.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return unavailable("claude code cost-state carried no modelUsage block")

    rows: list[dict[str, Any]] = []
    for model, mu in model_usage.items():
        if not isinstance(mu, dict):
            continue
        rows.append(
            {
                # One row per MODEL, not per message: that is the granularity the record
                # has, and a row-per-message shape invented here would imply evidence
                # this surface does not carry.
                "message_id": f"model:{model}",
                "model": model,
                "input_tokens": int(mu.get("inputTokens") or 0),
                "cache_read_tokens": int(mu.get("cacheReadInputTokens") or 0),
                "cache_write_tokens": int(mu.get("cacheCreationInputTokens") or 0),
                "output_tokens": int(mu.get("outputTokens") or 0),
                # A subset of output on every build measured; kept, never added.
                "reasoning_output_tokens": int(mu.get("thinkingTokens") or 0),
                "reported_cost_usd": float(mu.get("costUSD") or 0.0),
                "sidechain": False,
            }
        )
    if not rows:
        return unavailable("claude code cost-state named no model with usage")

    totals = {
        key: sum(int(r[key] or 0) for r in rows)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    }
    rows_cost = sum(float(r["reported_cost_usd"] or 0.0) for r in rows)
    notes = [
        f"{len(states)} cost-state record(s) in the transcript; the LAST is the session "
        "total and the others are superseded running totals",
        "`thinkingTokens` is a subset of `outputTokens` and is kept on the row without "
        "being added to the total",
        # The ROW SUM rather than `totalCostUSD`, because it is the figure the retained
        # evidence can justify. `totalCostUSD` is reported too when the two disagree,
        # since a discrepancy is itself something a reader needs to see.
        f"the harness priced this session at {rows_cost:.2f} USD across "
        f"{len(rows)} model(s); that figure is evidence, not a spine fact",
    ]
    total_cost = state.get("totalCostUSD")
    if isinstance(total_cost, (int, float)) and abs(float(total_cost) - rows_cost) >= 0.01:
        notes.append(
            f"the record's own totalCostUSD is {float(total_cost):.2f} USD, which does "
            f"not match the {rows_cost:.2f} USD its per-model rows sum to"
        )
    if state.get("hasUnknownModelCost"):
        notes.append(
            "the harness flagged hasUnknownModelCost: its own dollar figure omits at "
            "least one model it could not price"
        )
    if bad:
        notes.append(f"{bad} unparseable line(s) skipped (truncated transcript is expected)")

    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="claude-code-cost-state",
            evidence_records=len(rows),
            **totals,
        ),
        rows=rows,
        main_chain_models=[
            str(r["model"]) for r in rows
            if r["model"] and str(r["model"]) not in _NOT_A_MODEL
        ],
        notes=notes,
    )


def extract_opencode_messages(lines: Iterable[str]) -> UsageEvidence:
    """opencode's `message` rows, exported one JSON object per line by the harvest.

    Measured against opencode 1.18.23 (2026-09-11). This surface is the one place in
    this module where SUMMING ROWS IS CORRECT, and saying so explicitly matters because
    the neighbouring Claude extractor exists to do the opposite. opencode stores one row
    per assistant message carrying that message's own totals — there is no content-block
    fan-out to deduplicate, and deduplicating anyway would silently drop real traffic.

    `tokens.reasoning` is a subset of `tokens.output`, the same relationship codex has,
    and is kept on the row without entering the total.

    opencode also computes a per-message `cost`. Like Claude's, it is retained as evidence
    and never as a count. Unlike Claude's it is computed by the CLIENT from a pricing
    table, so it is a derived figure rather than the vendor's arithmetic — the note says
    so, because a reader comparing the two surfaces must not treat them as equally
    authoritative.
    """
    records, bad = _iter_json_lines(lines)
    rows: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("role") != "assistant":
            continue
        tokens = rec.get("tokens")
        if not isinstance(tokens, dict):
            continue
        raw_cache = tokens.get("cache")
        cache: dict[str, Any] = raw_cache if isinstance(raw_cache, dict) else {}
        rows.append(
            {
                "message_id": rec.get("id") or f"row-{len(rows) + 1}",
                "model": rec.get("modelID"),
                "input_tokens": int(tokens.get("input") or 0),
                "cache_read_tokens": int(cache.get("read") or 0),
                "cache_write_tokens": int(cache.get("write") or 0),
                "output_tokens": int(tokens.get("output") or 0),
                "reasoning_output_tokens": int(tokens.get("reasoning") or 0),
                "reported_cost_usd": float(rec.get("cost") or 0.0),
                "sidechain": False,
            }
        )
    if not rows:
        return unavailable(
            "the opencode export held no assistant message with a tokens block"
            + (f" ({bad} unparseable line(s))" if bad else "")
        )
    totals = {
        key: sum(int(r[key] or 0) for r in rows)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    }
    main_models: list[str] = []
    for r in rows:
        if isinstance(r["model"], str) and r["model"] not in main_models:
            main_models.append(r["model"])
    notes = [
        f"{len(rows)} assistant message(s), summed — opencode stores one row per message "
        "and does NOT repeat usage per content block",
        "`tokens.reasoning` is a subset of `tokens.output` and is kept on the row without "
        "being added to the total",
        f"opencode's own per-message cost sums to "
        f"{sum(float(r['reported_cost_usd'] or 0.0) for r in rows):.4f} USD; it is CLIENT-computed "
        "from a pricing table, not the provider's billed figure",
    ]
    if bad:
        notes.append(f"{bad} unparseable line(s) in the export")
    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="opencode-messages",
            evidence_records=len(rows),
            **totals,
        ),
        rows=rows,
        main_chain_models=main_models,
        notes=notes,
    )


def extract_codex_rollout(lines: Iterable[str]) -> UsageEvidence:
    """codex's on-disk rollout file (`~/.codex/sessions/**/rollout-*.jsonl`).

    A different surface from `extract_codex_turn_stream`, which reads the live
    `codex exec --json` stream. The rollout is what survives on disk after an interactive
    session, and it is the only codex surface a close-time harvest can read.

    THE COUNTING HAZARD: the rollout carries a `token_count` event per turn whose
    `total_token_usage` is CUMULATIVE for the whole session, alongside a `last_token_usage`
    that is the delta. Summing the cumulative blocks squares the bill on a long session.
    The last cumulative record is the total, and there is exactly one evidence row because
    that is how many facts this surface actually states.

    `input_tokens` is reported inclusive of the cached part and is recorded net of it, so
    it means here what it means on every other row in this module.
    `reasoning_output_tokens` is a subset of `output_tokens` and is kept, never added.

    The rollout never names the model on the usage record, so `main_chain_models` is
    empty rather than guessed. Measured against codex-cli 0.147.0.
    """
    records, bad = _iter_json_lines(lines)
    totals_block = None
    seen = 0
    for rec in records:
        if rec.get("type") != "event_msg":
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        block = info.get("total_token_usage")
        if not isinstance(block, dict):
            continue
        seen += 1
        totals_block = block
    if totals_block is None:
        return unavailable(
            "the codex rollout carried no `token_count` event with a cumulative total"
            + (f" ({bad} unparseable line(s))" if bad else "")
        )

    total_input = int(totals_block.get("input_tokens") or 0)
    cache_read = int(totals_block.get("cached_input_tokens") or 0)
    row: dict[str, Any] = {
        "message_id": "session-total",
        "model": None,
        "input_tokens": max(0, total_input - cache_read),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": int(totals_block.get("cache_write_input_tokens") or 0),
        "output_tokens": int(totals_block.get("output_tokens") or 0),
        "reasoning_output_tokens": int(totals_block.get("reasoning_output_tokens") or 0),
        "sidechain": False,
    }
    notes = [
        f"{seen} cumulative `token_count` record(s); the LAST is the session total and "
        "the others are superseded running totals",
        "`input_tokens` is reported inclusive of the cached part and is recorded here net "
        "of it, so the four counts do not double-count",
        "`reasoning_output_tokens` is a subset of `output_tokens` and is kept on the row "
        "without being added to the total",
        "the rollout exposes no resolved model id on the usage record; the row's model is null",
    ]
    if bad:
        notes.append(f"{bad} unparseable line(s) in the rollout")
    return UsageEvidence(
        usage=ExecutionUsage(
            status="reported",
            source="codex-rollout",
            evidence_records=1,
            input_tokens=int(row["input_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_tokens=int(row["cache_write_tokens"]),
            output_tokens=int(row["output_tokens"]),
        ),
        rows=[row],
        main_chain_models=[],
        notes=notes,
    )


_EXTRACTORS = {
    "claude-code-cost-state": extract_claude_code_cost_state,
    "claude-code-transcript": extract_claude_code_transcript,
    "codex-rollout": extract_codex_rollout,
    "codex-turn-stream": extract_codex_turn_stream,
    "fake-usage-file": extract_fake_usage_file,
    "opencode-messages": extract_opencode_messages,
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
