from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flowweave.modules.event_automations.infrastructure.models import (
    EventTriggerAction,
    EventTriggerDelivery,
    EventTriggerVersion,
)
from flowweave.shared.database import uid
from flowweave.shared.domain.event_automation import (
    ActionType,
    EventSource,
    EventTrigger,
    FailureClass,
    TriggerAction,
    TriggerEvent,
    TriggerFilter,
    action_idempotency_key,
    classify_failure,
)
from flowweave.shared.errors import DomainError
from flowweave.shared.models import RunEvent
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


def enqueue_matching_deliveries(db: Session, run_event: RunEvent) -> int:
    """Create idempotent delivery intents for one already-persisted RunEvent."""

    payload = run_event.payload_json or {}
    raw_event_id = payload.get("event_id") or payload.get("id")
    event_id = str(raw_event_id or f"run-event:{run_event.cursor}")
    raw_source = str(payload.get("source") or EventSource.ORCHESTRATION)
    try:
        source = EventSource(raw_source)
    except ValueError:
        return 0
    failure_class = None
    if run_event.event_type == "ERROR":
        failure_class = classify_failure(
            str(payload.get("error_code") or "") or None,
            str(payload.get("error") or payload.get("message") or "") or None,
        )
    event = TriggerEvent(
        event_id=event_id,
        event_type=run_event.event_type,
        source=source,
        flow_run_id=run_event.flow_run_id,
        node_run_id=run_event.node_run_id,
        attempt_id=run_event.attempt_id,
        conversation_id=_optional_string(payload.get("conversation_id")),
        parent_id=_optional_string(payload.get("parent_id")),
        action_id=_optional_string(payload.get("action_id")),
        tool_call_id=_optional_string(payload.get("tool_call_id")),
        failure_class=failure_class,
    )
    versions = db.scalars(
        select(EventTriggerVersion).where(EventTriggerVersion.enabled.is_(True))
    ).all()
    created = 0
    for version in versions:
        event_filter = version.event_filter_json or {}
        trigger_filter = TriggerFilter(
            event_types=frozenset(str(item) for item in event_filter.get("event_types", [])),
            sources=_enum_set(EventSource, event_filter.get("sources", [])),
            failure_classes=_enum_set(FailureClass, event_filter.get("failure_classes", [])),
            flow_run_ids=frozenset(str(item) for item in event_filter.get("flow_run_ids", [])),
            node_run_ids=frozenset(str(item) for item in event_filter.get("node_run_ids", [])),
        )
        actions = db.scalars(
            select(EventTriggerAction)
            .where(EventTriggerAction.trigger_version_id == version.id)
            .order_by(EventTriggerAction.position)
        ).all()
        if not actions or not EventTrigger(
            trigger_id=version.trigger_key,
            version=version.version_no,
            event_filter=trigger_filter,
            actions=tuple(
                TriggerAction(ActionType(action.action_type), action.config_json)
                for action in actions
            ),
            enabled=version.enabled,
        ).matches(event):
            continue
        trigger = EventTrigger(
            trigger_id=version.trigger_key,
            version=version.version_no,
            event_filter=trigger_filter,
            actions=tuple(
                TriggerAction(ActionType(action.action_type), action.config_json)
                for action in actions
            ),
            enabled=version.enabled,
        )
        for index, action in enumerate(actions):
            key = action_idempotency_key(trigger, event, index)
            if db.scalar(
                select(EventTriggerDelivery.id).where(EventTriggerDelivery.idempotency_key == key)
            ):
                continue
            db.add(
                EventTriggerDelivery(
                    trigger_version_id=version.id,
                    trigger_action_id=action.id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    flow_run_id=event.flow_run_id,
                    node_run_id=event.node_run_id,
                    attempt_id=event.attempt_id,
                    idempotency_key=key,
                    payload_json=_delivery_payload(event),
                )
            )
            created += 1
    if created:
        db.flush()
    return created


def _optional_string(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _enum_set(enum_type: type[EventSource] | type[FailureClass], values: object) -> frozenset[Any]:
    if not isinstance(values, list):
        return frozenset()
    result: set[Any] = set()
    for value in values:
        try:
            result.add(enum_type(str(value)))
        except ValueError:
            return frozenset()
    return frozenset(result)


def _delivery_payload(event: TriggerEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "source": event.source,
        "flow_run_id": event.flow_run_id,
        "node_run_id": event.node_run_id,
        "attempt_id": event.attempt_id,
        "conversation_id": event.conversation_id,
        "parent_id": event.parent_id,
        "action_id": event.action_id,
        "tool_call_id": event.tool_call_id,
        "failure_class": event.failure_class,
    }


__all__ = (
    "create_trigger_version",
    "enqueue_matching_deliveries",
    "list_trigger_versions",
    "read_latest_trigger",
)
