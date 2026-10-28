"""Current conversation capability selection over immutable registration history."""

from collections.abc import Iterable

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationCapability
from flowweave.modules.catalog.public import PublishedCapability, resolve_version
from flowweave.shared.errors import DomainError


def current_capabilities(
    references: Iterable[AgentConversationCapability],
) -> list[AgentConversationCapability]:
    # Keep the original row order while selecting the last acknowledged version.
    current: dict[tuple[str, str, str], AgentConversationCapability] = {}
    for reference in references:
        identity = (reference.binding_id, reference.capability_type, reference.capability_key)
        previous = current.get(identity)
        if previous is None or reference.position > previous.position:
            current[identity] = reference
    return list(current.values())


def validate_skill_upgrade(
    db: Session,
    previous: AgentConversationCapability,
    selected: PublishedCapability,
) -> None:
    """Only a newer version of the same standalone Skill may replace a load."""
    if previous.capability_type == selected.package.capability_type == "SKILL":
        old = resolve_version(db, previous.capability_version_id, include_retired=True)
        if (
            old.package.id == selected.package.id
            and selected.version.version_no > old.version.version_no
        ):
            return
    raise DomainError(
        "AGENT_CONVERSATION_CAPABILITY_CONFLICT",
        "仅支持将已注册 Skill 同步到同一技能的新版本",
        409,
        {"capability_key": selected.package.capability_key},
    )
