"""Settings — database URLs via env, with docker-compose-friendly defaults."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMEGAHIVE_", env_file=".env", extra="ignore")

    # The READ path. Matches docker-compose.yml. Override with OMEGAHIVE_DATABASE_URL.
    # Under the two-role scheme (migrations/0003_two_roles.sql) this carries `hive_reader`'s
    # DSN — a credential structurally incapable of writing the spine.
    database_url: str = "postgresql://omegahive:omegahive@localhost:5432/omegahive"

    # The WRITE path — `hive_gateway`'s DSN, delivered per-service via gateway.env so it
    # reaches only the containers that emit. EMPTY means "this deployment has not cut
    # over": db.connect_gateway() then falls back to database_url and the stack behaves
    # exactly as it did under one role. That fallback is what makes the cutover a
    # deployment act rather than a code release.
    gateway_database_url: str = ""

    # The SCHEMA path — the database owner's DSN, used by `db-migrate` and nothing else.
    # DDL (and the CREATE ROLE in 0003) needs ownership, which is precisely the authority
    # the two-role split takes away from every running service, so migrations get their
    # own operator-held credential (owner.env) rather than borrowing the gateway's.
    # Empty falls back to database_url, which is the pre-cutover single-role behaviour.
    owner_database_url: str = ""

    # §5 flood control: identical (actor, op, code) refusals within this many
    # logical_ts units coalesce onto one gateway.rejected (counter incremented).
    # logical_ts == epoch seconds under DB-side time (§6), so this reads as ~seconds
    # in production and as ticks in the sim.
    rejection_coalesce_window: int = 5


def get_settings() -> Settings:
    return Settings()
