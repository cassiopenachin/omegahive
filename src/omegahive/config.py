"""Settings — database URL via env, with a docker-compose-friendly default."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMEGAHIVE_", env_file=".env", extra="ignore")

    # Matches docker-compose.yml. Override with OMEGAHIVE_DATABASE_URL.
    database_url: str = "postgresql://omegahive:omegahive@localhost:5432/omegahive"

    # §5 flood control: identical (actor, op, code) refusals within this many
    # logical_ts units coalesce onto one gateway.rejected (counter incremented).
    # logical_ts == epoch seconds under DB-side time (§6), so this reads as ~seconds
    # in production and as ticks in the sim.
    rejection_coalesce_window: int = 5


def get_settings() -> Settings:
    return Settings()
