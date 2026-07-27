-- Runs once on first container init. Creates the database OMEGAHIVE_TEST_DATABASE_URL
-- names by default. Test runs no longer use it directly -- each creates and drops one of
-- its own on the same server (tests/scratch_db.py) -- but it is kept as the documented
-- base DSN's target, which is also the fallback maintenance database on an instance
-- without a `postgres` database.
CREATE DATABASE omegahive_test;
