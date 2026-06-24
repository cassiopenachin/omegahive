# OmegaHive M0 — Spine Implementation Spec

**Status:** Build spec, ready to scaffold. **Implements:** the v0 spec's substrate + envelope (§3–4) and the planner slice of the taxonomy (§5).
**Goal of M0:** a runnable spine — append events to a real Postgres log, load a scenario that emits planner events, and render the trace back — with deterministic replay. No reducer, workers, coordinator, or instruments yet (those begin at M1).

This seeds the new `omegahive` repo. It records the concrete engineering decisions so we don't relitigate them per file.

---

## 1. Scope

**M0 does:**

- Create the `events` table and the append path (`seq`, `logical_ts`, envelope validation, emitter-authority check, `causation_id`, trigger-derived `correlation_id`).
- Load a scenario YAML and emit its **planner events** (`goal.received`, `task.created`, `dependency.added`, `priority.set`).
- Render a run's trace (console + JSON).
- Be deterministic: same `(scenario, seed, run_id)` into a fresh log ⇒ byte-identical rows.

**M0 does not:** board reducer (M1), workers/coordinator/review/promotion/metrics (M1+), the rendering/attention read stage, multi-process clients (Regime B). The gateway exists only as the `append()` chokepoint's emit-authority check.

## 2. Locked stack

- **Python 3.12, synchronous, single-process.** Determinism first; matches omegaclaw's sync loop.
- **Postgres 16** (real substrate; M0's only client is the harness).
- **uv** for envs + locking.
- **psycopg 3 + hand-written SQL**, behind a thin repository (no ORM).
- **Pydantic v2** for the envelope, payloads, scenario, and settings.
- **typer** CLI + **rich** trace rendering. **PyYAML** scenarios.
- Migrations: numbered `.sql` files + a tiny runner.
- Tests: **pytest** against a dedicated `omegahive_test` database, transaction-rolled-back per test.

## 3. Repo layout

```
omegahive/
  pyproject.toml            # uv-managed
  uv.lock
  README.md
  docker-compose.yml        # local Postgres 16
  migrations/
    0001_events.sql         # table + indexes + correlation trigger
  scenarios/
    m0_smoke.yaml
  src/omegahive/
    __init__.py
    config.py               # Settings (DB url) via pydantic-settings + env
    db.py                   # psycopg connection + tiny migration runner
    clock.py                # LogicalClock (fast driver)
    events/
      envelope.py           # Actor, Event, enums
      types.py              # event_type → payload model registry; EMIT_AUTHORITY
      log.py                # EventLog: append() chokepoint + read queries
    scenario/
      schema.py             # Scenario model (M0 = plan only)
      loader.py             # YAML → planner events via EventLog.append
    report/
      trace.py              # rich table + JSON export
    cli.py                  # typer: db-migrate | run | report
  tests/
    conftest.py             # ephemeral DB fixture (txn rollback)
    test_append.py
    test_loader.py
    test_replay.py
```

## 4. Schema — `migrations/0001_events.sql`

```sql
CREATE TABLE events (
    seq            BIGSERIAL PRIMARY KEY,                       -- total order + replay cursor
    event_id       UUID NOT NULL UNIQUE,                        -- stable logical identity (deterministic, §5)
    run_id         TEXT NOT NULL,
    logical_ts     BIGINT NOT NULL,                             -- authoritative clock (sim ticks)
    wall_ts        TIMESTAMPTZ,                                 -- null in v0; real time in Regime B
    actor_role     TEXT NOT NULL,                               -- planner|coordinator|worker|instrument
    actor_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    task_id        TEXT,
    payload        JSONB NOT NULL DEFAULT '{}',
    causation_id   UUID REFERENCES events(event_id),            -- direct trigger; FK guarantees causal integrity
    correlation_id UUID NOT NULL,                               -- thread root; set by trigger below
    recipient_role TEXT,                                        -- only on directed-message events
    recipient_id   TEXT
);

CREATE INDEX idx_events_run_seq     ON events (run_id, seq);
CREATE INDEX idx_events_run_task    ON events (run_id, task_id);
CREATE INDEX idx_events_run_type    ON events (run_id, event_type);
CREATE INDEX idx_events_run_corr    ON events (run_id, correlation_id);
CREATE INDEX idx_events_causation   ON events (causation_id);

-- correlation_id inheritance lives in the DB so the invariant holds for ANY writer
-- (M0 is single-process, but Regime B adds independent clients; this removes the read-before-write).
CREATE FUNCTION set_correlation_id() RETURNS trigger AS $$
BEGIN
  IF NEW.correlation_id IS NULL THEN
    IF NEW.causation_id IS NOT NULL THEN
      SELECT correlation_id INTO NEW.correlation_id FROM events WHERE event_id = NEW.causation_id;
    END IF;
    IF NEW.correlation_id IS NULL THEN
      NEW.correlation_id := NEW.event_id;   -- thread origin: correlation = own id
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_correlation BEFORE INSERT ON events
  FOR EACH ROW EXECUTE FUNCTION set_correlation_id();
```

Decision recorded: I moved correlation-inheritance into a **BEFORE INSERT trigger** (rather than app-side, as I first floated). It is ~10 lines, removes the read-before-write, and means the invariant holds the moment Regime B adds writers. `logical_ts` stays app-supplied (it comes from the sim clock, an app concern); `seq` is DB-assigned; `correlation_id` is DB-derived.

## 5. Event model — `events/envelope.py`, `events/types.py`

```python
class Actor(BaseModel):
    role: Literal["planner", "coordinator", "worker", "instrument"]
    id: str

class Event(BaseModel):
    event_id: UUID
    run_id: str
    logical_ts: int
    wall_ts: datetime | None = None
    actor: Actor
    event_type: str
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)
    causation_id: UUID | None = None
    correlation_id: UUID | None = None      # filled by DB trigger; read back after insert
    recipient: Actor | None = None
```

**Per-type payloads (M0 = planner only)**, validated on emit:

```python
class GoalReceived(BaseModel):    text: str
class TaskCreated(BaseModel):     title: str; task_type: str; acceptance: str | None = None; required_artifacts: list[str] = []
class DependencyAdded(BaseModel): depends_on: str
class PrioritySet(BaseModel):     priority: Literal["low","normal","high"]
class PlanRevised(BaseModel):     action: Literal["cancel","re_decompose"]; reason: str | None = None

PAYLOADS = {                       # event_type → payload model
  "goal.received": GoalReceived, "task.created": TaskCreated,
  "dependency.added": DependencyAdded, "priority.set": PrioritySet, "plan.revised": PlanRevised,
}

EMIT_AUTHORITY = {                 # role → allowed event_types (full map; M0 exercises planner)
  "planner":     {"goal.received","task.created","dependency.added","priority.set","plan.revised"},
  "coordinator": {"task.assigned","task.reassigned","task.escalated","task.status_override","note.posted"},
  "worker":      {"task.accepted","task.rejected","task.progress","task.blocked","task.unblocked",
                  "task.result_posted","task.failed","question.asked"},
  "instrument":  {"promotion.created","promotion.suppressed","metric.threshold_crossed",
                  "review.passed","review.failed"},
}
```

**Deterministic ids.** `event_id = uuid5(NAMESPACE, f"{run_id}:{emit_index}")`, where `emit_index` is a per-run monotonic counter held by `EventLog`. Consequences: the emitter knows an event's id before insert (so `causation_id` references are stable), and a replay with the same `run_id` into a fresh log reproduces byte-identical rows. `run_id` is the determinism boundary — canonical runs pass an explicit `run_id`; ad-hoc runs get `f"{scenario_id}-{uuid4 short}"`.

## 6. Logical clock — `clock.py`

```python
class LogicalClock:
    def __init__(self, t: int = 0): self._t = t
    def now(self) -> int:    return self._t
    def advance(self, n=1):  self._t += n; return self._t
```

M0 emits the whole plan at `logical_ts = 0` (the plan is the initial state); `seq` carries order. The clock matters once workers act over time (M1+), but it exists now so `append()` reads `logical_ts` from one source from day one.

## 7. Append path — `events/log.py`

The single chokepoint. Every write goes through it.

```python
class EmitDenied(Exception): ...

class EventLog:
    def __init__(self, conn, clock, run_id):
        self.conn, self.clock, self.run_id, self._i = conn, clock, run_id, 0

    def append(self, *, actor: Actor, event_type: str, payload: dict,
               task_id=None, causation_id=None, recipient=None, logical_ts=None) -> Event:
        # 1. authority: role must be allowed to emit this type
        if event_type not in EMIT_AUTHORITY.get(actor.role, set()):
            raise EmitDenied(f"{actor.role} may not emit {event_type}")
        # 2. payload validation against the per-type model
        PAYLOADS[event_type](**payload)
        # 3. deterministic id + clock
        event_id = uuid5(NAMESPACE, f"{self.run_id}:{self._i}"); self._i += 1
        ts = self.clock.now() if logical_ts is None else logical_ts
        # 4. INSERT (correlation_id left NULL → trigger fills); RETURNING seq, correlation_id
        ...
        return Event(...)   # fully populated, incl. seq + correlation_id read back
```

Emit-authority + payload validation here *are* the gateway in M0. When Regime B arrives, the per-agent adapter wraps this same call.

## 8. Scenario + loader — `scenario/schema.py`, `scenario/loader.py`

```yaml
# scenarios/m0_smoke.yaml
scenario_id: m0_smoke
seed: 123
plan:
  goal: "Draft a short technical note from research"
  tasks:
    - {id: t1, title: "Research the topic",      task_type: research}
    - {id: t2, title: "Write the note",          task_type: writing, acceptance: "<= 1 page, cites sources"}
  dependencies: [[t2, t1]]          # t2 depends on t1
  priorities:   {t1: high}
```

Loader (planner role) emits, in order, with a causal tree rooted at the goal:

1. `goal.received` — origin (no causation ⇒ correlation = its own id; the plan's thread).
2. `task.created` per task — `causation_id = goal_event.event_id` (so all plan events share the goal's `correlation_id`).
3. `dependency.added` — `causation_id = ` the dependent task's `task.created`.
4. `priority.set` — `causation_id = ` that task's `task.created`.

`seed` is recorded on the run (unused in M0; drives stub RNG from M1).

## 9. Run report + CLI — `report/trace.py`, `cli.py`

```
omegahive db-migrate                       # apply migrations/*.sql in order
omegahive run scenarios/m0_smoke.yaml       # load + emit planner events; prints the run_id
omegahive report <run_id> [--json]          # render the trace
```

Trace = events for `run_id` ordered by `seq`, as a rich table: `seq · logical_ts · actor · event_type · task_id · caused_by(seq) · corr · payload`. `--json` dumps raw rows for diffing. (A causal-tree view is a nice-to-have, deferred.)

## 10. Tests & M0 definition-of-done

`conftest.py`: connect to `omegahive_test`, run migrations once, wrap each test in a transaction that rolls back.

- **test_append** — `seq` strictly increases; `logical_ts` set; **correlation inheritance** (child inherits parent's `correlation_id`; an origin's `correlation_id == event_id`); `causation_id` FK rejects a dangling parent; **emit-authority** rejects a `worker` emitting `task.created`; payload validation rejects a malformed `task.created`.
- **test_loader** — `m0_smoke` yields exactly `[goal.received, task.created×2, dependency.added, priority.set]` with the expected causal links; all plan events share one `correlation_id`.
- **test_replay** — same `(scenario, seed, run_id)` into a fresh log ⇒ **identical** rows on `(event_id, logical_ts, actor, event_type, task_id, payload, causation_id, correlation_id)`. `seq` matches too when the table is reset with `TRUNCATE events RESTART IDENTITY` between runs (Postgres sequences are non-transactional, so a plain rollback does not rewind `seq`).

**M0 is done when:** `db-migrate` + `run m0_smoke` + `report` shows the five planner events with correct causal/correlation structure, and all three test modules pass including deterministic replay.

## 11. Deferred to M1+ (so M0 stays a spine)

Board reducer + transition graph; worker/coordinator/review/promotion/metrics roles and their event types (the `EMIT_AUTHORITY` rows exist but are unexercised); the read/attention stage; per-agent adapters and central policy file; multi-process clients; real `wall_ts`. None require schema changes to the envelope — they add event types and projections over the same table.

## 12. Remaining small choices

- **Dev/test Postgres:** `docker-compose.yml` for local dev; tests target a `omegahive_test` DB on the same server. (Alternative: `testcontainers` for zero local setup — say the word if you'd rather.)
- **`NAMESPACE`** for `uuid5`: a fixed project UUID constant committed in `events/types.py`.
