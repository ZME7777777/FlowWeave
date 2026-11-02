from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://flowweave:flowweave_dev@localhost:55432/flowweave"
    runtime_provider_url: str = "http://runtime-provider:8090"
    sandbox_manager_scope: str = "flowweave-local"
    admin_runtime_observer_key: str = ""
    admin_runtime_controller_key: str = ""
    admin_metrics_sample_interval_seconds: int = Field(default=30, ge=10, le=3600)
    admin_temporary_service_cleanup_interval_seconds: int = Field(default=3600, ge=300, le=86_400)
    admin_temporary_service_max_age_seconds: int = Field(default=86_400, ge=3_600, le=604_800)
    admin_metrics_retention_days: int = Field(default=14, ge=1, le=365)
    admin_metrics_request_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @property
    def normalized_database_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg", "postgresql", 1)

    @model_validator(mode="after")
    def validate_observer_key(self) -> Settings:
        if len(self.admin_runtime_observer_key) < 32:
            raise ValueError("ADMIN_RUNTIME_OBSERVER_KEY must contain at least 32 characters")
        if len(self.admin_runtime_controller_key) < 32:
            raise ValueError("ADMIN_RUNTIME_CONTROLLER_KEY must contain at least 32 characters")
        if self.admin_runtime_controller_key == self.admin_runtime_observer_key:
            raise ValueError(
                "ADMIN_RUNTIME_CONTROLLER_KEY must differ from ADMIN_RUNTIME_OBSERVER_KEY"
            )
        return self
