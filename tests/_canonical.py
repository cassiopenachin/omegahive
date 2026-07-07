"""Canonicalize a log for comparison independent of random event_ids and wall clock.

The write path assigns event_id DB-side (gen_random_uuid), so two runs of the same
scenario differ in their UUIDs even when the structure is identical. This maps each
event_id to its seq-ordinal and rewrites every reference through that map — the
envelope's causation/correlation *and* any event_id embedded in a payload as provenance
(e.g. review.passed's ref_result) — drops wall_ts, and never compares the
idempotency_key. So determinism (and slice 3's transport-equivalence) is equality
*after* this normalization.

Shared by the determinism tests and the equivalence keystone (§8).
"""

from __future__ import annotations


def _canon_payload(value, ordinal_by_str):
    """Recursively replace any string that is a known event_id with its ordinal token."""
    if isinstance(value, str):
        o = ordinal_by_str.get(value)
        return f"#ev{o}" if o is not None else value
    if isinstance(value, dict):
        return {k: _canon_payload(v, ordinal_by_str) for k, v in value.items()}
    if isinstance(value, list):
        return [_canon_payload(v, ordinal_by_str) for v in value]
    return value


def canonical_log(events):
    ordered = sorted(events, key=lambda e: e.seq)
    ordinal = {e.event_id: i for i, e in enumerate(ordered)}
    ordinal_by_str = {str(eid): i for eid, i in ordinal.items()}

    def ref(u):
        return ordinal.get(u) if u is not None else None

    return [
        (
            e.seq,
            ordinal[e.event_id],                 # identity -> stable ordinal
            e.logical_ts,
            (e.actor.role, e.actor.id),
            e.event_type,
            e.task_id,
            _canon_payload(e.payload, ordinal_by_str),   # embedded event refs -> ordinals
            ref(e.causation_id),
            ref(e.correlation_id),
        )
        for e in ordered
    ]
