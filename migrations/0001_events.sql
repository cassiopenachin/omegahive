CREATE TABLE events (
    seq            BIGSERIAL PRIMARY KEY,                       -- total order + replay cursor
    event_id       UUID NOT NULL UNIQUE,                        -- stable logical identity (DB-generated; see 0002)
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
