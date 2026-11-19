"""Runtime settings, read from DATAHARBOR_* environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATAHARBOR_", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://dataharbor:change-me-postgres@127.0.0.1:5432/dataharbor",
        description="SQLAlchemy URL of the metadata database",
    )
    environment: str = Field(default="local", pattern=r"^(local|test|ci)$")
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")


@lru_cache
def get_settings() -> Settings:
    return Settings()
