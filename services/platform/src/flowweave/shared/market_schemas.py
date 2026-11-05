"""User-managed discovery sources; credentials never belong in source definitions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MarketSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["PLUGIN_GIT", "SKILL_GIT", "SKILLS_SH"]
    url: str = Field(min_length=1, max_length=500)
    ref: str = Field(default="HEAD", min_length=1, max_length=120)
    repo_path: str = Field(default="", max_length=300)
    enabled: bool = True
    builtin: bool = False


class MarketSourcesWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    sources: list[MarketSource] = Field(max_length=30)


class MarketSkillImportWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(max_length=80)
    repository: str = Field(max_length=200)
    skill_id: str = Field(min_length=1, max_length=200)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    path: str | None = Field(default=None, max_length=500)
