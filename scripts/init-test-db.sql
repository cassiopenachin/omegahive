-- Runs once on first container init. Creates the dedicated test database
-- (tests/conftest.py connects here, migrates once, and rolls back per test).
CREATE DATABASE omegahive_test;
