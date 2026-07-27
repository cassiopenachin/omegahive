-- Runs once on first container init. Creates the database OMEGAHIVE_TEST_DATABASE_URL
-- names by default. Test runs no longer use it directly -- each creates and drops one of
-- its own on the same server (tests/scratch_db.py) -- but it is kept so the documented base
-- DSN names a database that exists, which keeps `psql "$OMEGAHIVE_TEST_DATABASE_URL"` and
-- similar ad-hoc connections working.
CREATE DATABASE omegahive_test;
