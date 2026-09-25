from __future__ import annotations

import re
import stat
from datetime import timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationBinding
from flowweave.modules.tasks.public import enqueue
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError

_RUNTIME_WORKSPACE_PATH = (
    r"/runtime/workspace/(?:project/(?:users/)?[0-9a-f-]{36}|project|[0-9a-f-]{36})"
)
ATTACHMENT_PATH = re.compile(
    rf"^(?P<workspace_root>{_RUNTIME_WORKSPACE_PATH})/uploads/"
    r"(?P<owner>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<object>[0-9a-f]{32})(?:--(?P<filename>[A-Za-z0-9][A-Za-z0-9._-]{0,180}))?$"
)

_DRAFT_ATTACHMENT_RETENTION = timedelta(days=30)
_HOST_KINDS = frozenset({"AGENT_WORKSPACE", "FLOW_NODE"})


def enqueue_draft_attachment_cleanup(
    db: Session,
    *,
    host_kind: str,
    host_id: str,
    owner_id: str,
    path: str,
    host_scope_id: str | None = None,
) -> None:
    owner = normalize_attachment_owner(owner_id)
    if host_kind not in _HOST_KINDS:
        raise DomainError("AGENT_SESSION_HOST_INVALID", "会话宿主无效", 422)
    digest = sha256(path.encode()).hexdigest()
    enqueue(
        db,
        task_type="CLEANUP_DRAFT_ATTACHMENT",
        aggregate_type="AGENT_CONVERSATION_DRAFT",
        aggregate_id=owner,
        idempotency_key=f"cleanup-draft-attachment:{host_kind}:{host_id}:{owner}:{digest}",
        payload={
            "host_kind": host_kind,
            "host_id": host_id,
            "host_scope_id": host_scope_id,
            "path": path,
        },
        available_at=now() + _DRAFT_ATTACHMENT_RETENTION,
    )


def assert_attachment_owner_unbound(
    db: Session,
    owner_id: str,
    *,
    host_kind: str,
    host_id: str,
    host_scope_id: str | None = None,
) -> str:
    owner = normalize_attachment_owner(owner_id)
    binding = db.get(AgentConversationBinding, owner)
    if binding is None:
        return owner
    if (
        binding.host_kind != host_kind
        or binding.host_id != host_id
        or (host_scope_id is not None and binding.node_attempt_id != host_scope_id)
    ):
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件不属于当前会话宿主", 404)
    raise DomainError("AGENT_DRAFT_ALREADY_CREATED", "草稿已创建为正式会话，不能清理其附件", 409)


def process_draft_attachment_cleanup(
    db: Session,
    owner_id: str,
    payload: dict[str, Any],
) -> None:
    host_kind = str(payload.get("host_kind") or "")
    host_id = str(payload.get("host_id") or "")
    host_scope_id = str(payload.get("host_scope_id") or "")
    path = str(payload.get("path") or "")
    owner = normalize_attachment_owner(owner_id)
    binding = db.get(AgentConversationBinding, owner)
    if binding is not None:
        if binding.host_kind != host_kind or binding.host_id != host_id:
            raise DomainError("AGENT_ATTACHMENT_INVALID", "附件不属于当前会话宿主", 404)
        return
    if host_kind == "AGENT_WORKSPACE":
        from flowweave.modules.agent_workspaces.application import service
        from flowweave.modules.users.application.security import user_runtime_project_root

        delete_owned_attachment_files(
            service.agent_workspace_record_path(db, host_id),
            user_runtime_project_root(host_id),
            owner,
            path=path,
        )
        return
    if host_kind == "FLOW_NODE":
        from flowweave.modules.agent_sessions import public as agent_sessions
        from flowweave.modules.sandboxes import public as sandboxes

        if not host_scope_id:
            raise DomainError("AGENT_SESSION_HOST_INVALID", "节点会话范围无效", 422)
        agent_sessions.resolve_flow_node_session_host(
            db,
            flow_run_id=host_id,
            attempt_id=host_scope_id,
            require_start_permission=False,
        )
        context = sandboxes.node_attempt_workspace_context(
            db, flow_run_id=host_id, node_attempt_id=host_scope_id
        )
        delete_owned_attachment_files(
            context.host_working_directory,
            str(context.runtime_working_directory),
            owner,
            path=path,
        )
        return
    raise DomainError("AGENT_SESSION_HOST_INVALID", "会话宿主无效", 422)


def normalize_attachment_owner(owner_id: str) -> str:
    try:
        return str(UUID(owner_id))
    except ValueError as exc:
        raise DomainError("AGENT_CONVERSATION_ID_INVALID", "会话标识无效", 422) from exc


def delete_owned_attachment_files(
    host_root: Path,
    runtime_root: str,
    owner_id: str,
    *,
    path: str | None = None,
) -> int:
    owner = normalize_attachment_owner(owner_id)
    candidates: list[Path]
    if path is not None:
        matched = ATTACHMENT_PATH.fullmatch(path)
        if (
            matched is None
            or matched.group("owner") != owner
            or matched.group("workspace_root") != runtime_root
        ):
            raise DomainError("AGENT_ATTACHMENT_INVALID", "附件不属于当前草稿", 422)
        relative = PurePosixPath(path).relative_to(PurePosixPath(runtime_root))
        candidates = [host_root.joinpath(*relative.parts)]
    else:
        uploads = host_root / "uploads"
        try:
            candidates = list(uploads.glob(f"{owner}-*"))
        except OSError:
            return 0

    deleted = 0
    resolved_root = host_root.resolve()
    for candidate in candidates:
        runtime_path = str(PurePosixPath(runtime_root) / "uploads" / candidate.name)
        matched = ATTACHMENT_PATH.fullmatch(runtime_path)
        if matched is None or matched.group("owner") != owner:
            continue
        try:
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                continue
            candidate.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DomainError(
                "AGENT_ATTACHMENT_CLEANUP_FAILED", "草稿附件暂时无法清理，请稍后重试", 503
            ) from exc
    return deleted
