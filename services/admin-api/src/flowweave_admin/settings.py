from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://flowweave:flowweave_dev@localhost:55432/flowweave"
    admin_database_pool_size: int = Field(default=2, ge=1, le=8)
    admin_database_pool_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    platform_api_url: str = "http://api:8080"
    platform_stream_api_url: str = "http://stream-api:8080"
    runtime_provider_url: str = "http://runtime-provider:8090"
    admin_runtime_observer_key: str = ""
    admin_control_api_key: str = ""
    admin_api_request_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    admin_alert_cpu_percent_threshold: float = Field(default=90.0, ge=1, le=100)
    admin_alert_memory_percent_threshold: float = Field(default=90.0, ge=1, le=100)
    admin_alert_database_active_connections_threshold: int = Field(default=40, ge=1, le=10_000)
    admin_alert_pending_task_threshold: int = Field(default=20, ge=1, le=100_000)
    task_terminal_retention_days: int = Field(default=30, ge=1, le=3_650)

    @model_validator(mode="after")
    def validate_control_key(self) -> Settings:
        if self.admin_control_api_key and len(self.admin_control_api_key) < 32:
            raise ValueError("ADMIN_CONTROL_API_KEY must contain at least 32 characters")
        if (
            self.admin_control_api_key
            and self.admin_control_api_key == self.admin_runtime_observer_key
        ):
            raise ValueError("ADMIN_CONTROL_API_KEY must differ from ADMIN_RUNTIME_OBSERVER_KEY")
        return self

    @property
    def normalized_database_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg", "postgresql", 1)
