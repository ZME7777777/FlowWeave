"""Explicit, additive credential synchronization for native Conversations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentConversationBinding,
    AgentConversationCredentialSync,
)
from flowweave.modules.credentials.application.service import (
    list_credentials,
    resolve_credentials_for_agent,
)
from flowweave.runtime.base import RuntimeHandle, RuntimePort
from flowweave.shared.database import now


def _normalized_ids(credential_ids: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(credential_ids))


def _sync_rows(db: Session, binding_id: str) -> dict[str, AgentConversationCredentialSync]:
    return {
        item.credential_id: item
        for item in db.scalars(
            select(AgentConversationCredentialSync).where(
                AgentConversationCredentialSync.binding_id == binding_id
            )
        )
    }


def list_credential_sync_state(db: Session, binding: AgentConversationBinding) -> dict[str, Any]:
    """Return safe credential metadata and the only trustworthy sync status."""

    credentials = list_credentials(db)
    synced = _sync_rows(db, binding.id)
    initialized = binding.credential_sync_initialized_at is not None
    return {
        "initialized_at": (
            binding.credential_sync_initialized_at.isoformat()
            if binding.credential_sync_initialized_at
            else None
        ),
        "credentials": [
            {
                **credential,
                "sync_state": (
                    "UNRECORDED"
                    if not initialized
                    else (
                        "CURRENT"
                        if (record := synced.get(str(credential["id"])))
                        and record.credential_row_version == credential["row_version"]
                        else "NEEDS_SYNC"
                    )
                ),
            }
            for credential in credentials
        ],
    }


def _record_versions(
    db: Session,
    binding: AgentConversationBinding,
    summaries: Iterable[dict[str, Any]],
) -> None:
    existing = _sync_rows(db, binding.id)
    timestamp = now()
    for credential in summaries:
        credential_id = str(credential["id"])
        record = existing.get(credential_id)
        if record is None:
            db.add(
                AgentConversationCredentialSync(
                    binding_id=binding.id,
                    credential_id=credential_id,
                    credential_row_version=int(credential["row_version"]),
                    synced_at=timestamp,
                )
            )
        else:
            record.credential_row_version = int(credential["row_version"])
            record.synced_at = timestamp
    binding.credential_sync_initialized_at = timestamp
    db.flush()


def record_initial_credential_sync(db: Session, binding: AgentConversationBinding) -> None:
    """Record the all-current set only after native creation has succeeded."""

    _record_versions(db, binding, list_credentials(db))


def synchronize_credentials(
    db: Session,
    binding: AgentConversationBinding,
    handle: RuntimeHandle,
    runtime: RuntimePort,
    credential_ids: Iterable[str],
) -> dict[str, Any]:
    """Send selected values first, then persist their versions on success only."""

    values, _prompt, summaries = resolve_credentials_for_agent(db, _normalized_ids(credential_ids))
    runtime.update_conversation_secrets(handle, values)
    _record_versions(db, binding, summaries)
    return list_credential_sync_state(db, binding)


__all__ = (
    "list_credential_sync_state",
    "record_initial_credential_sync",
    "synchronize_credentials",
)
