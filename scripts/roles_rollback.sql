-- Per-database half of the two-role rollback (see scripts/roles_rollback.sh, which runs
-- this once per connectable database and then drops the roles).
--
-- Called with -v db=<database>. `\connect :"db"` reuses the host, port, user and password
-- of the current connection, so nothing here has to rewrite a DSN by string surgery.
\connect :"db"

-- DROP OWNED BY removes every privilege granted to the role in THIS database, including
-- the default-privilege entries migration 0003 created. Guarded because the roles are
-- cluster-global and may already be gone (a partially completed rollback re-runs cleanly),
-- and because DROP OWNED BY on an absent role is an error rather than a no-op.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['hive_gateway', 'hive_reader'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('DROP OWNED BY %I', r);
    END IF;
  END LOOP;
END $$;

-- Forget the forward migration so `db-migrate` re-applies it — a rollback that left the
-- ledger claiming 0003 was applied would make re-applying it require hand surgery, which is
-- exactly what a rehearsed rollback exists to avoid. Guarded on the table because most
-- databases in the cluster are not omegahive spines at all.
--
-- The guard and the DELETE resolve the table the SAME way. Naming `public.schema_migrations`
-- in the guard while letting the DELETE resolve through `search_path` means that on a
-- cluster whose owner does not have `public` first, the guard returns NULL, the DELETE is
-- skipped, and the ledger keeps its claim — the failure this statement exists to prevent.
-- `db.py` creates the table unqualified, i.e. in the first schema on the path, so
-- `to_regclass('schema_migrations')` is the honest test, and the regclass it returns is
-- what the DELETE then targets.
DO $$
DECLARE ledger regclass := to_regclass('schema_migrations');
BEGIN
  IF ledger IS NOT NULL THEN
    EXECUTE format('DELETE FROM %s WHERE filename = %L', ledger, '0003_two_roles.sql');
  END IF;
END $$;
