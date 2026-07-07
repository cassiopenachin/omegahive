-- Slice 2 (the write path): idempotency, DB-generated identity, and the run registry.

-- Idempotency key: NULL for rejections and non-op events; set for accepted op emits.
ALTER TABLE events ADD COLUMN idempotency_key TEXT;

-- Unique per (run, actor, key) — but only where a key is present, so rejections
-- (NULL key) are exempt (§3). A network retry with the same key hits this index;
-- the writer catches unique_violation and re-selects the existing event.
CREATE UNIQUE INDEX uq_events_idem
    ON events (run_id, actor_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Event identity moves DB-side: multi-writer-safe, no client counter. The former
-- deterministic uuid5(run_id, i) scheme cannot survive independent out-of-process
-- writers (they would collide). Replay determinism becomes equality-after-
-- canonicalization (test_replay.py), the same normalization the equivalence test uses.
ALTER TABLE events ALTER COLUMN event_id SET DEFAULT gen_random_uuid();

-- The run registry carries the log-generation token (§2): a cursor presented under a
-- stale generation gets GENERATION_MISMATCH instead of a silent skipping read. The
-- restore procedure (deployment spec) bumps generation; this milestone only creates
-- the column and the mismatch signal (used by the port in slice 3).
CREATE TABLE runs (
    run_id     TEXT PRIMARY KEY,
    generation BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
