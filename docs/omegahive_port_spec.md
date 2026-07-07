# OmegaHive — Port Milestone Spec (rev 3, build-ready)

**Status:** Build spec for the **port** milestone (stage 1 of [omegahive_design_1_1.md](omegahive_design_1_1.md) §7): make the substrate correct under independent, out-of-process writers, and give it one binding surface — the `HiveCoordinatorPort` — that every coordinator (scripted-greedy control, plain-LLM, OmegaClaw) and later every executor plugs into. **Rev 3** folds in the client-shape pressure test (Jul 6): the write path (§§3–6) is unchanged from panel-hardened rev 2; the port surface (§2), key rule (§3a), and proof suite (§8) are amended; §14 records the deltas. **Scope: contained within the omegahive repo.** The fork/deployment side is [omegahive_deployment_spec.md](omegahive_deployment_spec.md), which delivers this milestone's two external inputs: a pinned, buildable OmegaClaw fork image and its container policy file. Companions: design doc §3.3 (the R1–R5 invariants this implements) and §3.4 (the binding rules), [omegahive_omegaclaw_binding.md](omegahive_omegaclaw_binding.md) (the consumer), [omegahive_test_plan.md](omegahive_test_plan.md) §A (where the proof obligations live permanently).

## 0. Orientation for the implementing agent

You are building in the **omegahive repo** (Python + Postgres). The substrate directories (`events/`, `gateway/`, `board/`, `migrations/`) are frozen: changes land only via reviewed PR under CI (full test suite + ruff + mypy). The existing single-process implementation (~125 deterministic tests, the simulation harness, the greedy coordinator) is the baseline your work must not break: **the sim stays green through every slice.** Build in the §11 slice order; each slice lands independently with its tests. Definition of done per slice = its named tests green + the §8 obligations it owns + no regression in the existing suite. Where this spec says "documented," the documentation lives in-repo beside the code. Do not add dependencies without justification; do not touch anything under `sim/` except as §8 directs; nothing here contacts the OmegaClaw-Core upstream repo.

## 1. Goal and non-goals

**Goal.** After this milestone, two agent processes on two machines can safely coordinate through the board over TCP-to-Postgres: every emit is atomic and durable, every refusal is informative and recorded, nothing accepted is silently inert, retries never duplicate, time is server-set, and the whole thing is provably transport-changed-but-semantics-preserved (§8). The port serves a **mixed client population** (§2): short-lived subprocess-per-call clients *and* long-lived in-process clients (a resident coordinator holding a client for weeks) are both first-class.

**Non-goals.** No coordinator cognition (stage 2). No OmegaClaw-Core work at all — the fork belongs to the deployment spec; the mid-July upstream release is not disturbed. No chat binding, no artifact store, no capability policy service, no board-cache optimization, no multi-run orchestration. LISTEN/NOTIFY push signaling stays deferred, with its trigger now **armed** (§12.1) rather than open-ended. Anything not needed for one coordinator plus stub-or-simple executors on one run stays out.

## 2. The port API

One Python package (`omegahive.port`). A port instance is cheap to construct, holds **no server-side session state**, and every call is self-contained — but clients themselves may be short-lived (the OmegaClaw binding spawns a child process per skill call) or long-lived (a resident coordinator); **binding clients may hold durable local state** (cursor, key-derivation basis — §3a) in their workdir, and both shapes are first-class.

```python
class HiveCoordinatorPort:
    def read(self, cursor: int | None = None) -> PortView:
        """Board + events + new cursor + generation, all anchored to ONE log point.
        cursor=None → full snapshot (subprocess clients' normal mode).
        cursor=N    → board = server fold of the full run prefix up to S;
                      events = (N, S]; both from one snapshot (see contract below).
        Visibility filtered per the constructed actor/role."""

    def emit(self, op: Op, idempotency_key: str) -> Accepted | Rejected:
        """One board operation through the gateway.
        NEVER raises for policy/legality refusals — returns Rejected(code, reason, rejection_event_id).
        Raises only for infrastructure faults after internal same-key retry is exhausted (§2a)."""
```

- **Actor binding:** a port instance is constructed for one actor (id + role). No caller-supplied identity per call. **Nothing assumes one live client per actor** — a resident and its own subprocess skill-call may hold clients simultaneously; correctness under that configuration is pinned by test (§8, dual-client-same-actor).
- **Consistency contract for `read` (both modes):** board and events in a `PortView` are **anchored to the same log point S**, produced from one snapshot (single statement or `REPEATABLE READ`); the board is always the **server's** fold of the full run prefix ending at S — a cursor read returns the delta events *plus the authoritative board*, never a board fragment. **Client-side incremental folding is forbidden** until a board cache exists server-side; when it does, its equivalence to the reference fold becomes a property test, not an assumption. (This kills the third-fold-site failure class before it exists.)
- **No-change short-circuit:** `read(cursor=N)` first checks `max(seq) WHERE run_id = :run` (O(1), indexed); if nothing past N, return an empty view **without folding**. A fast-polling resident on a quiet board costs near nothing — which is the residency common case (N mostly-quiet projects) and what makes poll-only viable at stage 2 wake intervals.
- **Log-generation token:** every `PortView` carries a `generation` id (initialized at run creation, **bumped by the restore procedure** — deployment spec §5). A cursor presented under a stale generation returns a distinguishable `GENERATION_MISMATCH` signal — never a silent skipping read. **The signal is not one-shot (rev 3.1):** the port must *not* adopt the new generation on the mismatch branch — adoption happens only when the client takes a full snapshot; re-presenting the stale cursor keeps signaling. Client obligation on mismatch: drop the cursor, full-snapshot read (which adopts the new generation), rebuild. Restore-invalidates-cursors is thereby a *signal*, not a footgun.
- **Polling, not push.** No `wait()`; clients poll (the no-change short-circuit prices this). The push trigger is armed in §12.1.
- **Coordinator visibility is pinned:** the coordinator role sees the full board and full event history, no time-varying filter. (Worker visibility stays a first-executor decision, §7 — and note §7's long-lived-reader caveat.)
- **Serialization:** only wire-crossing types are pydantic — `Op`, `Accepted`, `Rejected`, `PortView` envelope. `Board`/`TaskState` stay dataclasses; rendering (S-expression, prose) happens inside the binding client. The port stays comparison-neutral across coordinator types.
- **`Op`** is a closed union mirroring the op vocabulary (assign / reassign / escalate / close / reopen; plan ops behind a flag), plus a **batch envelope**: an ordered list of ops the client sequences explicitly (design §3.4 — agents whose runtimes don't guarantee evaluation order emit one batch, the client preserves order; each op in the batch carries its own key). **`Rejected`** carries a stable machine code (`NOT_READY`, `ALREADY_OWNED`, `UNKNOWN_TASK`, `NOT_AUTHORIZED`, `ILLEGAL_TRANSITION`, `GENERATION_MISMATCH`), a human-readable reason, and the persisted rejection event id (§5).
- The **greedy coordinator is re-expressed against this API** as the reference client and the control arm of every later comparison.

### 2a. Connection lifecycle

- Connections are acquired **per call or from a pool**; because the write path bans every session-scoped construct (xact-scoped advisory lock, per-emit commit — §3), the port is safe under transaction-mode poolers. That is a **requirement, not an accident**: no code may introduce session state (`SET`, session locks, session `LISTEN`) on pooled connections; a future LISTEN connection is dedicated and un-pooled.
- **On connection loss during `emit`,** the client library retries internally **with the same idempotency key** under bounded backoff — safe purely because of §3's idempotency machinery, and the reason `emit` raising means "retry exhausted," not "unknown outcome."
- Long-lived clients reconnect with bounded backoff, never hot-loop (the stage-4 chaos drill asserts this behaviorally; the library implements it now).

## 3. The write path: atomicity, identity, ordering (unchanged from rev 2)

The emit transaction, exactly:

1. `BEGIN` (isolation: `READ COMMITTED` suffices — correctness rests on the lock and the unique index).
2. **Idempotency lookup first:** `SELECT` the event by `(run_id, actor_id, idempotency_key)`. Hit ⇒ return `Accepted(existing)` — **before** any fold or gate, so a replay of an already-committed op can never be re-gated.
3. `SELECT pg_advisory_xact_lock(hashtext(:run_id))` — key computed **inside Postgres** with `hashtext`, never Python `hash()`. Cross-run collisions merely over-serialize — safe at this scale.
4. Fold the board (from the log, within this transaction), evaluate authority + legality (§4).
5. **Accept:** append the op event; `COMMIT`. **Reject:** append **only** a `gateway.rejected` event (§5); `COMMIT`.

**Ordering guarantees.** `seq` is a global BIGSERIAL; cross-run interleaving exists and is benign **only because every cursor read is run-scoped** (`WHERE run_id = :run AND seq > :cursor` — mandatory, not an optimization). Within a run, the xact-scoped advisory lock is released only at commit, so seq-allocation order equals commit order per run. The session-scoped lock variant is forbidden.

**Event identity.** `event_id` is assigned **DB-side via `gen_random_uuid()` column default** — server-assigned, multi-writer-safe by construction, unique across restore generations. Not client-supplied (retire the in-memory uuid5 counter), and **not seq-derived**: a `uuid5(run_id, seq)` trigger would be nondeterministic anyway under rollbacks and cross-run BIGSERIAL interleaving, would add a trigger with a name-order dependency on the correlation trigger, and would mint colliding ids for different events across restore generations (seq values are reused past the restore point — the generation token exists precisely because of this). Determinism where it matters lives in per-run seq order, `logical_ts`, payloads, and causal shape; the §8 equivalence test and `test_replay` compare canonically (ids mapped to seq-ordinals, causation/correlation rewritten through the map).

**Idempotency.** Unique index on `(run_id, actor_id, idempotency_key)`, applying to **accepted op events only** (rejections exempt, §5). Residual ack-loss race: `INSERT` hits `unique_violation` → `ROLLBACK` → re-`SELECT` by key in a fresh transaction → return `Accepted(existing)`. A key that previously produced a *rejection* is **not** cached: the retry re-runs the gate (the board may have legally moved). Keys bind to accepted outcomes only.

**Contention scope.** One advisory lock per run, deliberately coarse. Known growth edge: the gate folds from the log under the lock, so hold time grows with run length; the incremental board cache is the designed remedy, trigger now armed against the run-mapping decision (§12.2). Note rejections also fold under the lock — see §5 flood control and the client backoff rule.

### 3a. Idempotency key rule (client-side; replaces rev 2's turn-counter scheme)

The write path treats the key as an opaque string; **key generation lives in the binding client library**:

> `key = SHA-256(run_id ‖ actor_id ‖ op_type ‖ canonical_payload ‖ basis_seq ‖ occ)`
> where **`basis_seq`** = max(log seq anchoring this client's most recent read, seq of this client's most recent accepted emit); **`occ`** = occurrence index among identical `(op_type, canonical_payload)` ops within one batch (else 0). Keys are derived at parse time from the canonicalized parsed `Op`, never from raw LLM text.
>
> **Basis mechanics (amended rev 3.1, adopting the implementation's improvement):** the in-memory basis is the always-on primary mechanism (a client with no workdir must still advance its basis — a basis frozen at 0 silently dedupes legitimate later re-emits of identical content); **workdir persistence is layered on and is mandatory for binding clients** (`workdir=None` is licensed for the sim harness only), write-through on every advance, constructor-seeded on resume — that persistence is what makes crash-redispatch dedupe work. The basis advances on **every** read, including no-change short-circuit reads (a resumed client whose first read is empty must not keep a stale basis). The **generation token is persisted alongside the cursor and basis** — an unseeded resumed client bypasses restore detection; persistence of all three is one binding obligation.

**Semantics:** two emits share a key **iff they are the same op decided from the same observed board.** An accidental replay (library retry, crashed-and-redispatched turn with persisted basis, LLM retry against a stale view) reproduces the key ⇒ dedupes. An intentional repeat is a new decision, which necessarily interposes new observed state (its own accepted intervening emit or a fresh read) ⇒ `basis_seq` moved ⇒ new key (e.g. escalate → resolving op → re-escalate of identical content resolves correctly). Two clients of one actor flushing the same decision from the same basis produce one event.

**Stated residuals:** (i) a retry issued after the client already observed the commit re-gates instead of returning `Accepted(existing)` — truthful for transition ops; for non-transition ops (escalate, notes) it is the one real duplicate hole, boundable later by an escalation legality row (additive, slice-1-shaped); (ii) a repeat with *neither* a re-read *nor* an intervening emit is formally indistinguishable from a replay and wrongly dedupes — the binding contract is "read or emit between repeats," with `occ` covering the within-batch case; (iii) basis-persistence loss degrades to the re-gate path, never to silent duplication of gated ops.

**Slice-2 checkpoint:** no `turn_id` (or any turn-counter parameter) appears in any write-path or port signature. If one has landed, remove it before slice 3 freezes the API.

## 4. One legality spec (unchanged from rev 2)

A single declarative structure is the only definition of stateful legality, consulted by both the gateway gate and the board fold: **key** `(event_type, payload_discriminant)`; **guard** — a predicate over `(board_state, actor, payload)` (from-state membership, field predicates like `ready AND owner IS NULL`, actor-relational rules, payload conditions like `done requires latest_review == passed`); **effect** — the fold's state application. **Derived transitions stay in the fold** (dependency-resolution readiness is a post-fold rule, not a table row). Policy: stateful op events are **default-deny**; non-board events (metrics, promotion, notes) are an explicit pass-through whitelist. One test asserts the spec covers exactly the emit-authority vocabulary; another asserts gate and fold agree on every row (no accepted-but-inert events, by construction). Existing behavior (done-gating on review, reopen-from-in-review, dependency-derived readiness) is pinned by regression tests *before* the refactor.

## 5. Rejections as recorded feedback (rev 2 + flood-control wording)

On refusal, the op event is never appended; the writer transaction (same advisory lock, so rejections order correctly) appends a **`gateway.rejected`** event — actor: gateway; payload: refused op descriptor, machine code, reason, refusing actor — and commits. Rejections are always legal (exempt from the gate) and exempt from the idempotency unique index (no client key). The binding surfaces `Rejected` values through skill-error feedback, and re-surfaces unresolved rejections in the next rendered view (design §3.4 — skill results are one-cycle-visible in the OmegaClaw binding).

**Flood control — bounds log volume, not gate cost:** identical `(actor, op, code)` rejections within a window (config, default short) are coalesced — first event kept, counter incremented. Since every rejection still folds under the run lock, the **binding client backs off on `Rejected` with identical content** (detectable for free: the §3a key hasn't changed); the retry-loop detector covers the observability side.

## 6. Time (unchanged from rev 2)

`wall_ts` and `logical_ts` are set **DB-side at insert from the same instant**: `wall_ts := now()`, `logical_ts := GREATEST(extract(epoch from now())::bigint, last_logical_ts_in_run + 1)` — monotonic per run, computed under the advisory lock, immune to client clock skew. Caller-supplied timestamps are rejected outside the quarantined sim binding. Rules and metrics read `logical_ts` only. **Coordinator decisions must be a pure function of the read log** — timers/poll intervals only trigger a re-scan; the scan's output depends solely on what was read. (Correctness rule + precondition for §8 equivalence.)

## 7. Read path and visibility

- Reads fold from the log per call, behind the §2 no-change short-circuit (the cache trigger is §12.2).
- **Coordinator:** full visibility, pinned (§2).
- **Workers:** visibility computed at read time against the current board — *documented as time-varying and non-monotone*; cursor-monotonicity is defined **only for full-visibility readers**. A long-lived filtered reader widens the retroactive-filtering window, which is why the **expected outcome** of the first-executor decision is a *static, assignment-scoped* visibility rule — a time-varying filter is not a semantics to build durable workers on.

## 8. Proof: equivalence, concurrency, resumption

- **Sim quarantine.** `engine/`, stub workers, and the scenario runner move under `omegahive.sim`; the substrate package imports none of it.
- **The equivalence test (keystone, deterministic):** the same scenario driven (a) through the sim engine and (b) through the port binding in a serialized single-writer harness produces identical event logs after canonicalization (`wall_ts`, `event_id`, causation/correlation ids normalized; **`idempotency_key` included in canonicalization** — client-derived keys are deterministic in the harness, but the diff must not depend on that argument holding forever).
- **Concurrency and lifecycle properties (targeted, not generative):**
  1. *race-to-assign* — two writers race for one ready task → exactly one `Accepted`, one recorded `Rejected(ALREADY_OWNED)`.
  2. *retry-storm* — fixed key hammered → exactly one event.
  3. *cursor-never-skips* — full-visibility run-scoped cursor under two concurrent writers, looped.
  4. *ack-loss-recovery* — connection killed between server COMMIT and client ack; same-key retry → one event, retry returns `Accepted` with the original event id. (Rev 2 specified this path and never tested it.)
  5. *crash-resume-cursor* — client persists cursor, dies, resumes → pre- and post-crash reads concatenate gapless, duplicate-free.
  6. *dual-client-same-actor* — resident + subprocess client for one actor: same decision, same basis → one event; racing conflicting ops → one `Accepted`, one rejection.
  7. *repeat-after-intervening-op* — an identical op legitimately re-emitted after an intervening accepted emit, one client, no external re-read → distinct events. (Rev 3.1 correction: the original "assign → unassign → assign" example is unimplementable — no unassign exists in the §2 op union; the escalate-resolve-re-escalate variant is the honest form.)
  8. *replay-vs-repeat matrix* — {library retry, redispatched turn w/ persisted basis, later-turn retry w/ stale basis, intentional repeat after fresh read} → {dedupe, dedupe, dedupe, execute}; plus the truthful re-gate case.
  9. *no-change-poll-cheap* — `read(cursor=head)` returns empty without invoking the fold (fold-invocation counter).
  10. *restore-invalidates-cursors* — restore + divergent append; stale-generation cursor gets `GENERATION_MISMATCH`, never a silent skip.
- **Crash-on-reject regression:** an illegal immediate emit leaves the client alive, the run consistent, a rejection event logged.
- **Long-run fold budget (profile guard, not a CI gate):** record fold latency at 10⁴ and 10⁵ events; these numbers arm §12's triggers with data.

All of the above join T0 permanently (test plan §A).

## 9. The generic environment (rev 2 + restore/pool notes)

One compose profile at the repo root: `postgres:16` (pinned digest) + migrations job; the `omegahive` gateway-library image; the pinned OmegaClaw fork image (**delivered by the deployment spec**, which bakes in `SAFE_VARS` DSN passage, resolver-readable policy, and the embedding model); `openclaw-gateway` pinned, loopback-bound. Secrets via env only; volumes for Postgres data and agent memory; no absolute host paths; no privileged containers.

**Ops floor (rules here, procedures in the deployment spec):** migrations only with coordinators drained; scheduled `pg_dump` of the log store; gateway library pinned per run (never hot-swap under a live run — the long-run collision with residency is recorded in deployment spec §5 and resolved by the run-mapping decision); **restore bumps the log generation** (§2) — until the token lands, restore ⇒ restart every client; if a pooler is introduced it runs in transaction mode, protected by §2a's no-session-state rule.

**DB roles:** single application role this milestone; "only the gateway library INSERTs" as documented convention. The two-role scheme (`hive_gateway` / `hive_reader`) lands at first-executor, when an external agent first holds credentials.

**Acceptance test — runnable by someone with no project history:** seed script, demo plan file, `board-view` read-and-print script. On a stock Linux box with only Docker: `compose up` → seed → greedy via the port → expected terminal board state → equivalence + property suites green. (This sequence is also deployment #0's bring-up — deployment spec §7.)

## 10. Deployment interface (what this milestone consumes and owes)

- **Consumes:** the pinned fork image + container policy file (slice 4 only; slices 1–3 have no dependency on the deployment track).
- **Owes:** the repo follows the hygiene floor (deployment spec §2) from slice 1 onward — substrate changes via reviewed PR under CI, tags for anything a deployment consumes, experiment records in-repo.

## 11. Build slices (each lands green independently)

1. **legality + rejections** — the §4 legality spec wired into gate and fold behind pinned regression tests; `gateway.rejected` persisted with configurable coalescing; crash-on-reject fixed. *(Sim stays green.)*
2. **write path** — the §3 transaction: idempotency-lookup-first, `hashtext` advisory lock, per-emit commit, unique index + catch-and-reselect, DB-side monotonic time. **Checkpoint: no turn-counter parameter in any signature (§3a).**
3. **port + proof** — the §2 API: consistency contract (both read modes, one snapshot), no-change short-circuit, generation token, batch envelope, connection lifecycle (§2a), the §3a key module with basis persistence; greedy re-expressed as reference client; `sim/` move; equivalence test; property tests 1–10. *(The pressure-test delta lands entirely in this slice.)*
4. **environment** — compose profile, seed/demo/board-view deliverables, ops floor, acceptance run on a clean machine (= Beastie bring-up, deployment spec §7). *(Depends on the deployment track's fork image; everything else is independent of it.)*

Slices 1–2 are pure hardening of existing code and start immediately; 3 depends on 2; 4 depends on 3 plus the fork image.

## 12. Armed triggers (deferred, with named decision points)

1. **LISTEN/NOTIFY push signaling** — deferred; **armed against the stage-2 wake-mechanism decision** (design §7 stage 2): if the chosen resident wake interval must undercut what no-change polling sustains, build NOTIFY then — as a loss-tolerant wake *hint* only (cursors remain the sole correctness carrier), on a dedicated un-pooled connection.
2. **Incremental board cache** — deferred; **armed against the run-mapping decision** (design §10.3): if residency = one long-lived run, cache-or-log-compaction becomes a stage-6 prerequisite, decided there. The §8 fold-budget numbers inform both triggers.
3. **Escalation legality row** (bounds §3a residual (i)) — additive slice-1-shaped change, adopt if non-transition duplicates prove noisy in practice.
4. **Library vs service** stands as designed (library over Postgres, invariants in the DB): revisit only when live policy flips, cross-client timers, or rate limiting appear on a milestone.

## 13. Panel dispositions (rev 1 → rev 2) — retained

**Correctness seat:** run-scoped cursors mandatory; `hashtext`-in-Postgres; idempotency lookup before fold/gate with `unique_violation` recovery, keys bind to accepted outcomes only; legality flat table → discriminant + guard + effect, derived-ready stays in fold; rejection transaction structure (op never appended); DB-side same-instant monotonic time; coordinator purity rule; equivalence redefined as deterministic single-writer + canonicalized diff with concurrency split into targeted tests; filtered-reader non-monotonicity documented. **Simplification seat (accepted):** LISTEN/NOTIFY cut to poll-only; fold cache cut with trigger recorded; two-role DB scheme deferred to first-executor; board-view container → script; pydantic limited to wire types; generative concurrency suite → targeted tests; slices merged. **Kept despite appearances:** equivalence test, crash-on-reject, sim quarantine, recorded rejections, pinning/hygiene. **Integration seat:** API designed for the stateless subprocess client — no `wait()`, snapshot reads, coordinator visibility pinned; fork/image requirements named and moved to the hygiene (now deployment) spec; ops floor added; acceptance made naive-operator-runnable; rejection coalescing adopted.

## 14. Pressure-test dispositions (rev 2 → rev 3, Jul 6)

Client-shape pressure test (mixed population + content-derived keys; full findings in the project record): **write path (§§3–6) unchanged** — rev 2's ban on session-scoped constructs made it client-lifetime-indifferent by construction. Amendments: **§3a** replaces the turn-counter key-generation rule (`turn_id` struck from the emit contract; slice-2 signature checkpoint added) with the content+basis rule, residuals stated; **§2** consistency contract defined for cursor-mode reads (board+events anchored at one point; client incremental folds forbidden), no-change short-circuit added, generation token added (restore-safety), batch envelope added, mixed client population made explicit, "nothing assumes in-process state" restated as no *server-side* session state with stateful binding clients first-class; **§2a** connection lifecycle specified (pool-safety as requirement, same-key internal retry, bounded backoff); **§5** flood-control wording (bounds log volume, not gate cost) + client backoff-on-identical-`Rejected`; **§7** long-lived filtered-reader caveat, static rule named the expected outcome; **§8** gains property tests 4–10 + key canonicalization + fold-budget profile; **§9** restore/generation procedure + pooler note, long-run pin collision routed to deployment spec §5 and design §10.3; **§12** deferrals converted to armed triggers with named decision points. References to the repo-hygiene spec now point to [omegahive_deployment_spec.md](omegahive_deployment_spec.md), which absorbed it. **Build-time addendum (same day):** `event_id` generation pinned to DB-side `gen_random_uuid()` (§3) — raised by the implementing agent during planning; seq-derived uuid5 rejected (nondeterministic under rollback/interleaving; trigger name-order footgun; cross-generation id collision after restore).

## 15. Rev 3.1 — post-build conformance audit dispositions (Jul 6, slices 1–3 @ ce869d9)

Independent audit of the built code against this spec: 12/19 conform, 6 partial, 1 new defect. **Spec moved (this rev):** §3a basis mechanics adopt the implementation's always-on in-memory basis with mandatory workdir persistence for bindings, basis advance on no-change reads, and generation-persistence as a binding obligation; §2 generation signal made explicitly non-one-shot (no implicit adoption on the mismatch branch); §8 test 7 corrected (no unassign in the op union). **Code must move (handed to the build workflow):** D1 — unique-violation recovery's re-select runs outside a transaction on a non-autocommit connection, stranding an implicit open transaction (savepointed follow-up emits silently un-persist; the advisory lock is held until connection death, stalling every writer on the run) — same class as the read() bug the review fixed; one-line fix + emit-after-recovery regression. D2 — port self-heals generation on the mismatch branch and the reference client never drops its cursor (both violate the amended §2). D3 — read transaction runs three statements under READ COMMITTED; set REPEATABLE READ (two words) rather than licensing the append-only argument. D4 — add the crash-redispatch-with-workdir dedupe test (§8 matrix row currently unproven) and advance basis on the no-change path. D5 — batch envelope and connection-loss retry have zero tests; add both (retry test kills the connection between COMMIT and ack, completing §8 test 4 as specified). Cosmetic: dead uuid5 constants/comments. Live-run items: torn-read stress, cross-run monotonicity under concurrency, psycopg savepoint semantics on the D1 repro.
