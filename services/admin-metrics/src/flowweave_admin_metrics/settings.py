from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://flowweave:flowweave_dev@localhost:55432/flowweave"
    runtime_provider_url: str = "http://runtime-provider:8090"
    admin_runtime_observer_key: str = ""
    admin_metrics_sample_interval_seconds: int = Field(default=30, ge=10, le=3600)
    admin_metrics_retention_days: int = Field(default=14, ge=1, le=365)
    admin_metrics_request_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @property
    def normalized_database_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg", "postgresql", 1)

    @model_validator(mode="after")
    def validate_observer_key(self) -> Settings:
        if len(self.admin_runtime_observer_key) < 32:
            raise ValueError("ADMIN_RUNTIME_OBSERVER_KEY must contain at least 32 characters")
        return self
