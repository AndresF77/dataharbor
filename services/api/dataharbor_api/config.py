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
    object_store: str = Field(
        default="minio",
        pattern=r"^(minio|memory)$",
        description="`minio` for the Compose MinIO; `memory` keeps landed files in the process",
    )
    minio_endpoint: str = Field(default="http://127.0.0.1:9000")
    minio_access_key: str = Field(default="dataharbor")
    minio_secret_key: str = Field(default="change-me-minio", repr=False)
    landing_bucket: str = Field(default="dataharbor-landing-local", min_length=3, max_length=63)
    max_landing_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)


@lru_cache
def get_settings() -> Settings:
    return Settings()
