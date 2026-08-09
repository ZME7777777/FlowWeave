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
    credential_subject_key: str = "local-user"
    credential_internal_base_url: str = "http://api:8080/api/v1"
    credential_internal_api_key: str = "flowweave-credential-internal"
    credential_lease_ttl_seconds: int = Field(default=300, ge=30, le=1800)
    credential_lease_max_uses: int = Field(default=20, ge=1, le=100)
    oauth_session_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    lark_oauth_client_id: str = ""
    lark_oauth_client_secret: str = ""
    lark_oauth_authorize_url: str = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    lark_oauth_token_url: str = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    lark_api_base_url: str = "https://open.feishu.cn"
    lark_oauth_redirect_url: str = "http://localhost:8080/api/v1/oauth/lark/callback"
    lark_oauth_default_scopes: tuple[str, ...] = ()

    runtime_adapter: str = "openhands"
    # Transitional switch used only until synchronous orchestration is removed.
    execution_mode: str = "worker"
    runtime_poll_seconds: float = Field(default=1.0, gt=0)
    sse_event_batch_size: int = Field(default=100, ge=1, le=500)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=120)
    openhands_base_url: str = "http://openhands-agent-server:8000"
    openhands_session_api_key: str = "flowweave-internal"
    openhands_workspace_root: Path = Path("/workspaces")
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

    dependency_builder_backend: str = "disabled"
    dependency_builder_image: str = "flowweave-dependency-builder:1"
    dependency_builder_network: str = "flowweave_dependency-build"
    dependency_builder_timeout_seconds: int = Field(default=300, ge=30, le=1800)

    terminal_environment_backend: str = "disabled"
    terminal_environment_base_image: str = "flowweave-openhands-runtime:1"
    terminal_environment_setup_network: str = "bridge"
    terminal_environment_runtime_network: str = "flowweave_runtime"
    terminal_environment_workspace_source_container: str = "flowweave-openhands-agent-server"
    terminal_environment_session_ttl_seconds: int = Field(default=14_400, ge=300, le=86_400)
    terminal_environment_memory: str = "2g"
    terminal_environment_cpus: float = Field(default=2.0, gt=0, le=16)
    terminal_environment_pids_limit: int = Field(default=512, ge=32, le=4096)
    terminal_environment_start_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    terminal_environment_publish_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    docker_binary: str = "docker"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.task_heartbeat_seconds >= self.task_lease_seconds:
            raise ValueError("TASK_HEARTBEAT_SECONDS must be less than TASK_LEASE_SECONDS")
        if self.sandbox_backend not in {"process", "docker"}:
            raise ValueError("SANDBOX_BACKEND must be process or docker")
        if self.dependency_builder_backend not in {"disabled", "docker"}:
            raise ValueError("DEPENDENCY_BUILDER_BACKEND must be disabled or docker")
        if self.terminal_environment_backend not in {"disabled", "docker"}:
            raise ValueError("TERMINAL_ENVIRONMENT_BACKEND must be disabled or docker")
        if self.runtime_adapter not in {"openhands", "mock"}:
            raise ValueError("RUNTIME_ADAPTER must be openhands or mock")
        if self.artifact_backend not in {"local", "s3"}:
            raise ValueError("ARTIFACT_BACKEND must be local or s3")
        if self.artifact_backend == "s3" and not self.artifact_s3_bucket:
            raise ValueError("ARTIFACT_S3_BUCKET is required for the s3 backend")
        if self.app_env == "production":
            if not self.credentials_master_key:
                raise ValueError("CREDENTIALS_MASTER_KEY is required in production")
            if not self.credential_subject_key.strip():
                raise ValueError("CREDENTIAL_SUBJECT_KEY is required in production")
            if not self.credential_internal_api_key.strip():
                raise ValueError("CREDENTIAL_INTERNAL_API_KEY is required in production")
        return self
