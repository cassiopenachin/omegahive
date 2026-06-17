"""Settings — database URL via env, with a docker-compose-friendly default."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMEGAHIVE_", env_file=".env", extra="ignore")

    # Matches docker-compose.yml. Override with OMEGAHIVE_DATABASE_URL.
    database_url: str = "postgresql://omegahive:omegahive@localhost:5432/omegahive"


def get_settings() -> Settings:
    return Settings()
