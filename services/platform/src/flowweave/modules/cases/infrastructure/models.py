from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flowweave.shared.database import Base, now, uid


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('DRAFT', 'ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED')",
            name="ck_case_state",
        ),
        CheckConstraint("row_version >= 1", name="ck_case_row_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(220))
    objective: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    control_conversation_binding_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    control_work_directory_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CaseStage(Base):
    __tablename__ = "case_stages"
    __table_args__ = (
        UniqueConstraint("case_id", "stage_key", name="uq_case_stage_key"),
        UniqueConstraint("case_id", "position", name="uq_case_stage_position"),
        CheckConstraint(
            "state IN ('PENDING', 'ACTIVE', 'COMPLETED', 'SKIPPED')",
            name="ck_case_stage_state",
        ),
        CheckConstraint("position >= 0", name="ck_case_stage_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(220))
    objective: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CaseAgentTask(Base):
    __tablename__ = "case_agent_tasks"
    __table_args__ = (
        UniqueConstraint("case_id", "task_key", name="uq_case_agent_task_key"),
        CheckConstraint(
            "task_type IN ('OBSERVE', 'PROPOSE_REVISION', 'REVIEW')",
            name="ck_case_agent_task_type",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_case_agent_task_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_id: Mapped[str | None] = mapped_column(String(36), index=True)
    task_key: Mapped[str] = mapped_column(String(120))
    task_type: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    node_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    input_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ("Case", "CaseAgentTask", "CaseStage")
