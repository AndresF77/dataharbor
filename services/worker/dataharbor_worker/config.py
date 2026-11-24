"""Worker settings from DATAHARBOR_* environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dataharbor_worker.api_client import ApiClient, UrllibTransport

REQUIRED = ("DATAHARBOR_API_URL", "DATAHARBOR_API_TOKEN", "DATAHARBOR_WORKSPACE_ID",
            "DATAHARBOR_SOURCE_DIR")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    api_url: str
    api_token: str = field(repr=False)
    workspace_id: str
    source_dir: Path
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WorkerConfig":
        env = os.environ if env is None else env
        missing = [name for name in REQUIRED if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(f"Set {', '.join(missing)} (see services/worker/README.md)")
        try:
            timeout = float(env.get("DATAHARBOR_API_TIMEOUT_SECONDS", "30"))
        except ValueError:
            raise ConfigError("DATAHARBOR_API_TIMEOUT_SECONDS must be a number") from None
        return cls(
            api_url=env["DATAHARBOR_API_URL"].strip(),
            api_token=env["DATAHARBOR_API_TOKEN"].strip(),
            workspace_id=env["DATAHARBOR_WORKSPACE_ID"].strip(),
            source_dir=Path(env["DATAHARBOR_SOURCE_DIR"].strip()),
            timeout_seconds=timeout,
        )

    def client(self) -> ApiClient:
        return ApiClient(
            UrllibTransport(self.api_url, self.timeout_seconds), self.api_token, self.workspace_id
        )
