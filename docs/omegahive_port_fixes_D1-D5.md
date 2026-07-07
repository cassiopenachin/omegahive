# Port build — post-audit fixes D1–D5 (self-contained work order)

**Context (all you need):** the port milestone (slices 1–3, merged at `ce869d9`) was audited against its build spec after human review. Five gaps + one cosmetic item. This file is self-contained — no other document required. File:line references are as of `ce869d9`; anchor by function name if lines drifted. **Order: D1 first (critical), then D2–D5 in any order.** All tests need live Postgres: `docker-compose up -d` then `OMEGAHIVE_TEST_DATABASE_URL=... pytest`.

---

## D1 — CRITICAL: stranded transaction in unique-violation recovery

**Where:** `src/omegahive/gateway/gateway.py`, `emit()`'s `UniqueViolation` recovery (~lines 117–124).
**Mechanism:** when a retried emit hits the unique index, the `with conn.transaction()` block rolls back correctly — but the follow-up `find_by_key(...)` re-select then runs as a **bare SELECT on a non-autocommit connection** (`db.py` sets autocommit off), which opens an implicit transaction that is never committed or rolled back. From that moment, on that connection: (a) every subsequent `emit()`'s `conn.transaction()` nests as a **savepoint** inside the stranded transaction — it RELEASEs instead of COMMITting, so those "accepted" events **silently never persist**; (b) each of those emits takes `pg_advisory_xact_lock(hashtext(run_id))`, which is only released at top-level transaction end — i.e. **never** — so every other writer on the run **stalls until this connection dies**.
This is the same bug class the review-fix commit (`e90609d`) closed in `read()`; this is the sibling path it missed. The existing `test_retry_storm_one_event` passes because it never emits again on the losing connection.
**Fix (one line):** wrap the recovery re-select in a transaction:
```python
except UniqueViolation:
    # (rollback has happened via the context manager)
    with self._conn.transaction():
        existing = self._log.find_by_key(run_id, actor_id, key)
    return Accepted(existing)
```
**Same pattern, lower priority:** `Gateway.check()` (~lines 70–75) has the same bare-SELECT shape; it's currently sim-only under rollback fixtures (benign), but fix it the same way while there.
**Regression test (required):** on one port/connection — (1) force a `UniqueViolation` recovery (two clients, same key: emit on A, then emit same key on B so B goes through recovery); (2) on B, emit a *new* op with a fresh key; (3) assert from a **separate connection** that the new event is durably visible (it will not be, pre-fix); (4) assert a third client's emit on the same run completes within a timeout (it will hang on the advisory lock, pre-fix).

## D2 — Generation-mismatch signal must not be one-shot

**Where:** `src/omegahive/port/port.py` `read()` mismatch branch (~lines 104–108); `src/omegahive/sim/reference_client.py` (~lines 51–56).
**Mechanism:** on `GENERATION_MISMATCH` the port currently updates its own `self._generation` before returning. Result: re-presenting the **same stale cursor** on the next call reads silently — and since a restore reuses seq values past the restore point, that is precisely the silent-skipping-read the token exists to prevent. The reference client compounds it by returning on the mismatch view without dropping `self.cursor`.
**Fix:** (a) in the mismatch branch, return the signal **without** adopting the new generation — adoption happens only on a full-snapshot read (`cursor=None`); (b) reference client: on `view.generation_mismatch`, set `self.cursor = None` (and clear any cached board) so its next read is a full snapshot, which adopts the generation.
**Test changes:** extend `test_restore_invalidates_cursors` / `test_generation_mismatch_on_crash_resume`: after receiving one mismatch, present the stale cursor **again** → must get the mismatch signal again (not a silent read); then full-snapshot read → succeeds and adopts; subsequent cursor reads work.

## D3 — Read snapshot isolation

**Where:** `src/omegahive/port/port.py` `read()` (~lines 100–120).
**Mechanism:** the read runs generation-check / head-check / prefix-select as **three statements under READ COMMITTED**. The spec licenses two forms: single statement, or `REPEATABLE READ`. Today it's torn-read-free only via an unstated argument (append-only log + advisory-lock commit ordering) — and there's a real residual: rejection-coalescing mutates a past event's counter in place, so a prefix read can see a counter newer than its anchor point.
**Fix (two words, effectively):** open the read transaction with `ISOLATION LEVEL REPEATABLE READ` (psycopg: `conn.transaction()` after setting the isolation level for that transaction, or execute `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` as the first statement inside it). No behavioral change expected in tests; add a comment naming why (one-snapshot anchoring for multi-statement reads).

## D4 — Crash-redispatch dedupe: close the untested matrix row + the no-change basis gap

**Where:** `src/omegahive/port/port.py` (`_advance_basis`, no-change short-circuit ~lines 110–114, basis write-through ~132–138); `src/omegahive/port/keys.py` (`BasisStore`); tests.
**Two gaps:**
1. The core crash-redispatch property — *a port recreated from an existing workdir re-derives the same key for the same decision and dedupes* — is implemented (constructor seeds from `BasisStore`) but **no test exercises it**: nothing destroys and recreates a client mid-scenario.
2. The no-change short-circuit **returns before advancing the basis**. A client resumed without its in-memory state whose first read comes back empty keeps a stale basis; its next emit of content identical to an old op reproduces the old key and **wrongly dedupes** — silent duplication-suppression of a legitimate new decision.
**Fix:** (a) in the no-change path, advance the in-memory basis (and write through to `BasisStore` when a workdir is present) to the confirmed head before returning; (b) make the docstring explicit that `workdir=None` is licensed for the sim harness only — binding clients must pass a workdir (the durable basis is what makes crash-redispatch dedupe work).
**Tests (required):** (1) *redispatch-dedupe:* create port with workdir, read, emit (accepted), **destroy the object**, recreate from the same workdir, emit the identical op without any intervening read → same key, `Accepted(existing)`, exactly one event in the log; (2) *no-change basis advance:* port with workdir, read at head (no-change path), assert persisted basis == head; recreate from workdir, emit op identical to an older op → **new** key, new event.

## D5 — Untested rev-3 surface: batch envelope + connection-loss retry

**Where:** `src/omegahive/port/wire.py` (`BatchOp`, ~lines 80–84), `src/omegahive/port/port.py` (batch handling ~145–156; `_with_retry` ~164–192).
**Mechanism:** both are implemented but have **zero test coverage**; and the existing `test_ack_loss_recovery` tests same-key-retry semantics without an actual connection kill, so the retry machinery itself is trusted by inspection only.
**Tests (required, no production-code change expected):**
1. *Batch order + occ:* emit a `BatchOp` of several ops including two with identical `(op_type, payload)` → events land in emitted order (per-run seq order matches list order); the identical pair get distinct keys via `occ` (two events); replaying the whole batch (same basis) → zero new events.
2. *Connection-loss retry:* using a `connect`-callable port, kill the backend between server COMMIT and client ack (e.g. `pg_terminate_backend` from a second connection triggered mid-emit, or a fault-injecting connection wrapper) → the library retries with the **same key**, bounded backoff, and returns `Accepted` carrying the original event id; exactly one event in the log. This completes the spec's ack-loss test as written.

## Cosmetic (do last, no tests)

- Delete the dead uuid5 `NAMESPACE` constant and stale "deterministic uuid5" comments (`src/omegahive/events/types.py` ~17–19; `migrations/0001_events.sql` header comment) — event ids are DB-side `gen_random_uuid()` now; the comments describe the retired scheme.

## Definition of done

All five regression tests above green on live Postgres; full existing suite green; D1's two-part regression demonstrably fails on `ce869d9` and passes post-fix; no changes outside `gateway/gateway.py`, `port/port.py`, `port/keys.py`, `sim/reference_client.py`, tests, and the cosmetic files. After merge, run once: the equivalence suite + the three original concurrency properties, to confirm no behavioral drift.
