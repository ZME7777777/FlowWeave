from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flowweave.modules.agent_sessions.infrastructure.models import (
    AgentAttachmentUpload,
    AgentAttachmentUploadPart,
)
from flowweave.shared.database import now
from flowweave.shared.errors import DomainError

CHUNK_SIZE = 256 * 1024
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024
UPLOAD_RETENTION = timedelta(hours=24)


def create_upload(
    db: Session,
    *,
    host_kind: str,
    host_id: str,
    host_scope_id: str | None,
    binding_id: str | None,
    work_directory_id: str | None,
    attachment_owner_id: str,
    filename: str,
    mime_type: str,
    total_size: int,
) -> AgentAttachmentUpload:
    try:
        owner_id = str(UUID(attachment_owner_id))
    except ValueError as exc:
        raise DomainError("AGENT_CONVERSATION_ID_INVALID", "附件必须关联有效会话", 422) from exc
    if not filename or len(filename) > 240 or "\x00" in filename:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件文件名无效", 422)
    if total_size <= 0 or total_size > MAX_ATTACHMENT_SIZE:
        raise DomainError("AGENT_ATTACHMENT_TOO_LARGE", "单个附件不能超过 25 MiB", 422)
    normalized_mime_type = mime_type.lower().strip() or "application/octet-stream"
    if len(normalized_mime_type) > 200:
        raise DomainError("AGENT_ATTACHMENT_INVALID", "附件类型无效", 422)
    upload = AgentAttachmentUpload(
        host_kind=host_kind,
        host_id=host_id,
        host_scope_id=host_scope_id,
        binding_id=binding_id,
        work_directory_id=work_directory_id,
        attachment_owner_id=owner_id,
        filename=filename,
        mime_type=normalized_mime_type,
        total_size=total_size,
        chunk_size=CHUNK_SIZE,
        expires_at=now() + UPLOAD_RETENTION,
    )
    db.add(upload)
    db.flush()
    return upload


def upload_for_host(
    db: Session,
    upload_id: str,
    *,
    host_kind: str,
    host_id: str,
    host_scope_id: str | None,
    binding_id: str | None,
    lock: bool = False,
) -> AgentAttachmentUpload:
    query = select(AgentAttachmentUpload).where(
        AgentAttachmentUpload.id == upload_id,
        AgentAttachmentUpload.host_kind == host_kind,
        AgentAttachmentUpload.host_id == host_id,
        AgentAttachmentUpload.host_scope_id == host_scope_id,
        AgentAttachmentUpload.binding_id == binding_id,
    )
    if lock:
        query = query.with_for_update()
    upload = db.scalar(query)
    if upload is None:
        raise DomainError("AGENT_ATTACHMENT_UPLOAD_NOT_FOUND", "上传会话不存在或不可访问", 404)
    if upload.status != "ACTIVE":
        raise DomainError("AGENT_ATTACHMENT_UPLOAD_CLOSED", "上传会话已结束", 409)
    if upload.expires_at <= now():
        upload.status = "CANCELLED"
        db.execute(delete(AgentAttachmentUploadPart).where(AgentAttachmentUploadPart.upload_id == upload.id))
        raise DomainError("AGENT_ATTACHMENT_UPLOAD_EXPIRED", "上传会话已过期，请重新上传", 410)
    return upload


def upload_status(upload: AgentAttachmentUpload, parts: list[AgentAttachmentUploadPart]) -> dict[str, object]:
    return {
        "upload_id": upload.id,
        "chunk_size": upload.chunk_size,
        "total_size": upload.total_size,
        "uploaded_parts": [part.part_number for part in parts],
        "expires_at": upload.expires_at.isoformat(),
    }


def list_parts(db: Session, upload: AgentAttachmentUpload) -> list[AgentAttachmentUploadPart]:
    return list(
        db.scalars(
            select(AgentAttachmentUploadPart)
            .where(AgentAttachmentUploadPart.upload_id == upload.id)
            .order_by(AgentAttachmentUploadPart.part_number)
        )
    )


def put_part(db: Session, upload: AgentAttachmentUpload, *, part_number: int, content: bytes) -> None:
    part_count = (upload.total_size + upload.chunk_size - 1) // upload.chunk_size
    if part_number < 0 or part_number >= part_count:
        raise DomainError("AGENT_ATTACHMENT_PART_INVALID", "附件分片序号无效", 422)
    expected_size = upload.chunk_size if part_number < part_count - 1 else upload.total_size - part_number * upload.chunk_size
    if len(content) != expected_size:
        raise DomainError("AGENT_ATTACHMENT_PART_INVALID", "附件分片大小无效", 422)
    existing = db.scalar(
        select(AgentAttachmentUploadPart).where(
            AgentAttachmentUploadPart.upload_id == upload.id,
            AgentAttachmentUploadPart.part_number == part_number,
        )
    )
    if existing is None:
        db.add(AgentAttachmentUploadPart(upload_id=upload.id, part_number=part_number, content=content))
        return
    if existing.content != content:
        raise DomainError("AGENT_ATTACHMENT_PART_CONFLICT", "附件分片与已上传内容不一致", 409)


def assemble(db: Session, upload: AgentAttachmentUpload) -> bytes:
    parts = list_parts(db, upload)
    part_count = (upload.total_size + upload.chunk_size - 1) // upload.chunk_size
    if [part.part_number for part in parts] != list(range(part_count)):
        raise DomainError("AGENT_ATTACHMENT_UPLOAD_INCOMPLETE", "附件尚未上传完成", 409)
    content = b"".join(part.content for part in parts)
    if len(content) != upload.total_size:
        raise DomainError("AGENT_ATTACHMENT_UPLOAD_INCOMPLETE", "附件分片大小不完整", 409)
    return content


def close_upload(db: Session, upload: AgentAttachmentUpload, *, status: str) -> None:
    upload.status = status
    db.execute(delete(AgentAttachmentUploadPart).where(AgentAttachmentUploadPart.upload_id == upload.id))
