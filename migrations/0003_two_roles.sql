-- The two-role write path (port spec "DB roles"; OPERATIONS §Phase 2 gate mechanism 1).
--
-- `hive_gateway` is the ONLY role that may write the spine; `hive_reader` may only read
-- it. Every consumer that does not emit connects as `hive_reader` and is therefore
-- structurally incapable of an INSERT — the raw-SQL side door closes at the credential,
-- not at a documented convention.
--
-- Roles are CLUSTER-global while grants are per-database, so this file is idempotent
-- about the roles (another database's migration run may have created them already) and
-- plainly declarative about the grants (re-granting is a no-op). Applying it to a
-- scratch test database is therefore safe and is what the open-test runs against.
--
-- NO PASSWORD is set here, deliberately: a role with a NULL password cannot authenticate
-- under scram-sha-256, so this migration creates capability without creating a
-- credential, and no secret ever enters the repo, an image, or a record. The operator
-- sets both passwords at cutover (RUNBOOK "Two-role cutover").
--
-- Rollback is scripts/roles_rollback.sh — the migration runner has no down-path, and a
-- cluster-global object needs a cluster-wide revoke, which a per-database migration
-- cannot express.

-- Catching duplicate_object rather than testing pg_roles first: the roles are CLUSTER
-- objects while this migration runs per DATABASE, and concurrent runs are the documented
-- normal mode (every test run and every deploy-checks run creates its own scratch database
-- and migrates it — tests/scratch_db.py). A check-then-act pair loses that race, and the
-- loser fails with a duplicate_object error that reads like a broken migration and
-- disappears on a re-run: a flake nobody can reproduce. Each CREATE gets its own block so
-- one role losing the race cannot skip the other.
DO $$
BEGIN
  CREATE ROLE hive_reader LOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  CREATE ROLE hive_gateway LOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- The gateway is the reader plus the write grants below. Membership rather than a
-- duplicated read grant, so there is exactly one place that defines read scope — and so
-- a future read grant cannot be added for one role and forgotten for the other.
-- Note the direction: gateway is a member of reader, never the reverse, so a session
-- authenticated as hive_reader cannot SET ROLE its way into the write grants.
GRANT hive_reader TO hive_gateway;

-- CONNECT is granted to PUBLIC by default; stated explicitly so the grants still hold on
-- a database where PUBLIC's default has been revoked. Dynamic because GRANT ... ON
-- DATABASE takes a literal name and this file runs against whichever database it is
-- applied to (the durable spine, or a per-run scratch database).
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO hive_reader', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO hive_reader;

-- The read surface: the spine, the run registry, and the migration ledger (the last so a
-- read-only operator can answer "which migrations are applied?" without a write
-- credential).
GRANT SELECT ON events, runs, schema_migrations TO hive_reader;

-- The write surface, and nothing else.
--
-- The UPDATE grants are COLUMN-scoped, because table-wide UPDATE would make "append-only"
-- false in the direction that leaves no trace: a deleted row leaves a gap in `seq`, but a
-- rewritten `actor_id` or `event_type` leaves nothing at all, and the whole point of the
-- log is that it can be replayed and believed. The running system needs exactly two
-- updates and no others -- the rejection-coalescing counter (events/log.py's jsonb_set on
-- `payload`) and the log-generation bump a restore performs (`runs.generation`) -- so
-- those two columns are what the gateway may write. The SELECT these statements' WHERE
-- clauses need comes from the hive_reader membership above.
--
-- No DELETE and no TRUNCATE to either role: nothing in the running system removes an event.
GRANT INSERT ON events TO hive_gateway;
GRANT INSERT ON runs   TO hive_gateway;
GRANT UPDATE (payload)    ON events TO hive_gateway;
GRANT UPDATE (generation) ON runs   TO hive_gateway;
GRANT USAGE ON SEQUENCE events_seq_seq TO hive_gateway;

-- Future tables read automatically, write NEVER automatically. A new table is readable
-- by every consumer the moment it exists (otherwise the next migration silently breaks
-- every read path), but a new write grant is a deliberate line in the migration that
-- introduces it — which is the whole point of this scheme. If you add a table the
-- gateway must write, grant it there, explicitly.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO hive_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO hive_reader;
