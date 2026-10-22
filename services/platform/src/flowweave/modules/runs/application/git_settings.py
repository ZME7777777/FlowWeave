"""Record-scoped Git configuration, frozen independently of later profile edits."""

from __future__ import annotations

from sqlalchemy.orm import Session

from flowweave.modules.runs.infrastructure.models import FlowRun
from flowweave.modules.users.application.git_identity import profile_identity
from flowweave.shared.schemas import GitIdentityWrite


def default_git_identity(db: Session, explicit: GitIdentityWrite | None) -> dict[str, str] | None:
    if explicit is not None:
        return explicit.model_dump()
    profile = profile_identity(db)
    return {"author": profile[0], "email": profile[1]} if profile else None


def record_git_identity(run: FlowRun) -> dict[str, str] | None:
    value = (run.automation_plan_json or {}).get("git_identity")
    if value is None:
        return None
    return GitIdentityWrite.model_validate(value).model_dump()
