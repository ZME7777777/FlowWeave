from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.event_automations.infrastructure.models import (
    EventTriggerAction,
    EventTriggerVersion,
)
from flowweave.shared.database import uid
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import EventTriggerWrite


def _event_filter(payload: EventTriggerWrite) -> dict[str, Any]:
    return {
        "event_types": list(payload.event_types),
        "sources": list(payload.sources),
        "failure_classes": list(payload.failure_classes),
        "flow_run_ids": list(payload.flow_run_ids),
        "node_run_ids": list(payload.node_run_ids),
    }


def _project(db: Session, version: EventTriggerVersion) -> dict[str, Any]:
    actions = db.scalars(
        select(EventTriggerAction)
        .where(EventTriggerAction.trigger_version_id == version.id)
        .order_by(EventTriggerAction.position)
    ).all()
    event_filter = version.event_filter_json or {}
    return {
        "id": version.id,
        "trigger_key": version.trigger_key,
        "version_no": version.version_no,
        "name": version.name,
        "enabled": version.enabled,
        "event_types": event_filter.get("event_types", []),
        "sources": event_filter.get("sources", []),
        "failure_classes": event_filter.get("failure_classes", []),
        "flow_run_ids": event_filter.get("flow_run_ids", []),
        "node_run_ids": event_filter.get("node_run_ids", []),
        "actions": [
            {
                "id": action.id,
                "position": action.position,
                "action_type": action.action_type,
                "config": action.config_json,
                "description": action.description,
            }
            for action in actions
        ],
        "created_at": version.created_at,
    }


def list_trigger_versions(db: Session) -> list[dict[str, Any]]:
    versions = db.scalars(
        select(EventTriggerVersion).order_by(
            EventTriggerVersion.trigger_key, EventTriggerVersion.version_no.desc()
        )
    ).all()
    return [_project(db, version) for version in versions]


def read_latest_trigger(db: Session, trigger_key: str) -> dict[str, Any]:
    version = db.scalar(
        select(EventTriggerVersion)
        .where(EventTriggerVersion.trigger_key == trigger_key)
        .order_by(EventTriggerVersion.version_no.desc())
    )
    if version is None:
        raise DomainError("EVENT_TRIGGER_NOT_FOUND", "事件触发器不存在", 404, {"key": trigger_key})
    return _project(db, version)


def create_trigger_version(
    db: Session, payload: EventTriggerWrite, *, trigger_key: str | None = None
) -> dict[str, Any]:
    key = trigger_key or payload.trigger_key
    if trigger_key is not None and payload.trigger_key != trigger_key:
        raise DomainError("EVENT_TRIGGER_KEY_MISMATCH", "触发器路径与请求 key 不一致", 422)
    latest = db.scalar(
        select(func.max(EventTriggerVersion.version_no)).where(
            EventTriggerVersion.trigger_key == key
        )
    )
    version = EventTriggerVersion(
        trigger_key=key,
        version_no=int(latest or 0) + 1,
        name=payload.name,
        enabled=payload.enabled,
        event_filter_json=_event_filter(payload),
    )
    db.add(version)
    db.flush()
    for position, action in enumerate(payload.actions):
        db.add(
            EventTriggerAction(
                id=uid(),
                trigger_version_id=version.id,
                position=position,
                action_type=action.action_type,
                config_json=action.config,
                description=action.description,
            )
        )
    db.flush()
    return _project(db, version)


__all__ = ("create_trigger_version", "list_trigger_versions", "read_latest_trigger")
