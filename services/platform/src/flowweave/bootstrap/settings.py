from __future__ import annotations

import re
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
    flowweave_admin_password: str = ""
    flowweave_user_password: str = ""
    lark_api_base_url: str = "https://open.feishu.cn"

    runtime_adapter: str = "openhands"
    # Transitional switch used only until synchronous orchestration is removed.
    execution_mode: str = "worker"
    runtime_poll_seconds: float = Field(default=1.0, gt=0)
    runtime_wakeup_timeout_seconds: float = Field(default=10.0, gt=0, le=25)
    runtime_wakeup_backoff_max_seconds: float = Field(default=30.0, gt=0, le=300)
    sse_event_batch_size: int = Field(default=100, ge=1, le=500)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=120)
    openhands_session_api_key: str = "flowweave-internal"
    openhands_workspace_root: Path = Path("/workspaces")
    # Uploaded executable capability assets are mounted separately from the
    # writable node workspace. Keep this path outside openhands_workspace_root
    # so workspace-controlled symlinks cannot influence the mount target.
    openhands_managed_assets_root: Path = Path("/runtime/capabilities")
    # The Docker controller mounts the host workspace root read-only here so it
    # can validate FlowRun allocation ownership before asking the daemon to
    # bind the corresponding host paths.
    flow_run_runtime_validation_root: Path = Path("")
    # Absolute path as seen by the Docker daemon. Runtime Provider pairs this
    # with flow_run_runtime_validation_root instead of inspecting a shared
    # source container to discover host mounts.
    runtime_host_workspace_root: Path = Path("")
    # Optional SSH endpoint used by JetBrains Gateway/IDEA to open the
    # persistent workspace on the Docker host. The endpoint is deliberately
    # separate from Runtime containers, which may be replaced at any time.
    ide_ssh_host: str = ""
    ide_ssh_user: str = ""
    ide_ssh_port: int = Field(default=22, ge=1, le=65_535)
    conversation_limit_per_flow_run: int = Field(default=20, ge=1, le=100)
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
    dependency_builder_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    plugin_resolver_backend: str = "disabled"
    plugin_resolver_image: str = "flowweave-openhands-runtime:1"
    plugin_resolver_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    plugin_resolver_allowed_hosts: str = "github.com,gitlab.com"

    terminal_environment_backend: str = "disabled"
    terminal_environment_setup_image: str = "flowweave-openhands-runtime:1"
    openhands_runtime_builder_image: str = "flowweave-openhands-runtime:1"
    # The independent Agent Workspace resolves this platform-owned reference
    # to a digest at bootstrap, then Runtime generations use only that digest.
    agent_workspace_runtime_image: str = "flowweave-openhands-runtime:1"
    terminal_environment_session_ttl_seconds: int = Field(default=14_400, ge=300, le=86_400)
    terminal_environment_cleanup_seconds: int = Field(default=30, ge=5, le=3600)
    sandbox_manager_scope: str = "flowweave-local"
    sandbox_reconcile_seconds: int = Field(default=30, ge=5, le=3600)
    sandbox_reconcile_batch_size: int = Field(default=50, ge=1, le=500)
    sandbox_orphan_grace_seconds: int = Field(default=300, ge=30, le=86_400)
    sandbox_runtime_idle_ttl_seconds: int = Field(default=3_600, ge=300, le=86_400)
    sandbox_runtime_hard_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    sandbox_runtime_network_mode: str = "isolated"
    sandbox_storage_size: str = "4g"
    terminal_environment_memory: str = "2g"
    terminal_environment_cpus: float = Field(default=2.0, gt=0, le=16)
    terminal_environment_pids_limit: int = Field(default=512, ge=32, le=4096)
    terminal_environment_max_active_sessions: int = Field(default=4, ge=1, le=64)
    terminal_environment_start_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    terminal_environment_publish_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    docker_binary: str = "docker"
    docker_controller_mode: str = "local"
    docker_controller_url: str = "http://runtime-provider:8090"
    docker_controller_api_key: str = ""
    docker_controller_worker_api_key: str = ""
    docker_controller_terminal_idle_seconds: int = Field(default=1800, ge=60, le=86_400)

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.task_heartbeat_seconds >= self.task_lease_seconds:
            raise ValueError("TASK_HEARTBEAT_SECONDS must be less than TASK_LEASE_SECONDS")
        if self.runtime_wakeup_timeout_seconds >= self.task_lease_seconds:
            raise ValueError("RUNTIME_WAKEUP_TIMEOUT_SECONDS must be less than TASK_LEASE_SECONDS")
        if self.sandbox_backend not in {"process", "docker"}:
            raise ValueError("SANDBOX_BACKEND must be process or docker")
        if self.dependency_builder_backend not in {"disabled", "docker"}:
            raise ValueError("DEPENDENCY_BUILDER_BACKEND must be disabled or docker")
        if self.plugin_resolver_backend not in {"disabled", "docker"}:
            raise ValueError("PLUGIN_RESOLVER_BACKEND must be disabled or docker")
        if self.terminal_environment_backend not in {"disabled", "docker"}:
            raise ValueError("TERMINAL_ENVIRONMENT_BACKEND must be disabled or docker")
        if self.docker_controller_mode not in {"local", "remote"}:
            raise ValueError("DOCKER_CONTROLLER_MODE must be local or remote")
        if self.sandbox_runtime_idle_ttl_seconds > self.sandbox_runtime_hard_ttl_seconds:
            raise ValueError(
                "SANDBOX_RUNTIME_IDLE_TTL_SECONDS must not exceed SANDBOX_RUNTIME_HARD_TTL_SECONDS"
            )
        if self.sandbox_runtime_network_mode not in {"isolated", "egress"}:
            raise ValueError("SANDBOX_RUNTIME_NETWORK_MODE must be isolated or egress")
        storage_match = re.fullmatch(r"([1-9][0-9]*)([mMgG])", self.sandbox_storage_size)
        if storage_match is None:
            raise ValueError("SANDBOX_STORAGE_SIZE must be an integer followed by m or g")
        storage_bytes = int(storage_match.group(1)) * (
            1024**3 if storage_match.group(2).lower() == "g" else 1024**2
        )
        if not 128 * 1024**2 <= storage_bytes <= 100 * 1024**3:
            raise ValueError("SANDBOX_STORAGE_SIZE must be between 128m and 100g")
        docker_control_enabled = (
            self.terminal_environment_backend == "docker"
            or self.sandbox_backend == "docker"
            or self.dependency_builder_backend == "docker"
            or self.plugin_resolver_backend == "docker"
        )
        if docker_control_enabled and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.sandbox_manager_scope
        ):
            raise ValueError(
                "SANDBOX_MANAGER_SCOPE must be a non-empty Docker-label-safe identifier "
                "when a Docker backend is enabled"
            )
        if docker_control_enabled and self.docker_controller_mode == "remote":
            if not self.docker_controller_url.startswith(("http://", "https://")):
                raise ValueError("DOCKER_CONTROLLER_URL must be an HTTP(S) URL")
            if len(self.docker_controller_api_key) < 32:
                raise ValueError(
                    "DOCKER_CONTROLLER_API_KEY must contain at least 32 characters "
                    "when remote Docker control is enabled"
                )
        if self.runtime_adapter not in {"openhands", "mock"}:
            raise ValueError("RUNTIME_ADAPTER must be openhands or mock")
        if self.artifact_backend not in {"local", "s3"}:
            raise ValueError("ARTIFACT_BACKEND must be local or s3")
        if self.artifact_backend == "s3" and not self.artifact_s3_bucket:
            raise ValueError("ARTIFACT_S3_BUCKET is required for the s3 backend")
        if self.app_env == "production":
            if not self.credentials_master_key:
                raise ValueError("CREDENTIALS_MASTER_KEY is required in production")
            if len(self.flowweave_admin_password) < 12:
                raise ValueError(
                    "FLOWWEAVE_ADMIN_PASSWORD must contain at least 12 characters in production"
                )
            if len(self.flowweave_user_password) < 12:
                raise ValueError(
                    "FLOWWEAVE_USER_PASSWORD must contain at least 12 characters in production"
                )
        return self
