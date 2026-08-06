from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration; created once by a bootstrap entrypoint."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_env: str = "development"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:5173"

    database_url: str = "postgresql+psycopg://flowweave:flowweave_dev@localhost:55432/flowweave"
    pool_size: int = Field(default=10, ge=1, le=100)
    statement_timeout_ms: int = Field(default=30_000, ge=100)

    credentials_master_key: str = ""

    runtime_adapter: str = "mock"
    # Transitional switch used only until synchronous orchestration is removed.
    execution_mode: str = "worker"
    runtime_poll_seconds: float = Field(default=1.0, gt=0)
    sse_event_batch_size: int = Field(default=100, ge=1, le=500)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=120)
    openhands_base_url: str = "http://openhands-agent-server:8000"
    openhands_session_api_key: str = "flowweave-internal"
    conversation_limit_per_attempt: int = Field(default=20, ge=1, le=100)
    conversation_message_max_chars: int = Field(default=20_000, ge=1, le=100_000)

    artifact_backend: str = "local"
    artifact_root: Path = Path("./var/artifacts")
    artifact_s3_bucket: str = ""
    artifact_s3_prefix: str = "flowweave"
    artifact_s3_region: str = "us-east-1"
    artifact_s3_endpoint_url: str = ""
    artifact_s3_access_key: str = ""
    artifact_s3_secret_key: str = ""
    workspace_root: Path = Path("./var/workspaces")
    inline_artifact_limit: int = Field(default=65_536, ge=0)

    capability_import_ttl_seconds: int = Field(default=900, ge=60)
    seed_demo: bool = False
    worker_id: str = ""
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    task_lease_seconds: int = Field(default=30, ge=5)
    task_heartbeat_seconds: int = Field(default=10, ge=1)

    sandbox_backend: str = "process"
    sandbox_image_python: str = "flowweave-sandbox-python:1"
    sandbox_image_javascript: str = "flowweave-sandbox-javascript:1"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.task_heartbeat_seconds >= self.task_lease_seconds:
            raise ValueError("TASK_HEARTBEAT_SECONDS must be less than TASK_LEASE_SECONDS")
        if self.sandbox_backend not in {"process", "docker"}:
            raise ValueError("SANDBOX_BACKEND must be process or docker")
        if self.artifact_backend not in {"local", "s3"}:
            raise ValueError("ARTIFACT_BACKEND must be local or s3")
        if self.artifact_backend == "s3" and not self.artifact_s3_bucket:
            raise ValueError("ARTIFACT_S3_BUCKET is required for the s3 backend")
        if self.app_env == "production":
            if not self.credentials_master_key:
                raise ValueError("CREDENTIALS_MASTER_KEY is required in production")
        return self
