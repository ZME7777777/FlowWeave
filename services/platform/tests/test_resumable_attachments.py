from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from flowweave.modules.agent_sessions.application import resumable_attachments
from flowweave.shared.errors import DomainError


def _upload(db: Session, *, total_size: int):
    return resumable_attachments.create_upload(
        db,
        host_kind="AGENT_WORKSPACE",
        host_id=str(uuid4()),
        host_scope_id=None,
        binding_id=None,
        work_directory_id=None,
        attachment_owner_id=str(uuid4()),
        filename="example.bin",
        mime_type="application/octet-stream",
        total_size=total_size,
    )


def test_resumable_upload_accepts_fixed_chunks_and_reports_resume_state(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        upload = _upload(db, total_size=resumable_attachments.CHUNK_SIZE + 7)
        resumable_attachments.put_part(db, upload, part_number=0, content=b"a" * resumable_attachments.CHUNK_SIZE)
        db.commit()
        current = resumable_attachments.upload_for_host(
            db, upload.id, host_kind="AGENT_WORKSPACE", host_id=upload.host_id, host_scope_id=None, binding_id=None
        )
        status = resumable_attachments.upload_status(current, resumable_attachments.list_parts(db, current))
        assert status["chunk_size"] == 256 * 1024
        assert status["uploaded_parts"] == [0]


def test_resumable_upload_rejects_bad_chunk_and_requires_all_parts(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        upload = _upload(db, total_size=resumable_attachments.CHUNK_SIZE + 1)
        with pytest.raises(DomainError, match="分片大小"):
            resumable_attachments.put_part(db, upload, part_number=0, content=b"a")
        resumable_attachments.put_part(db, upload, part_number=0, content=b"a" * resumable_attachments.CHUNK_SIZE)
        with pytest.raises(DomainError, match="尚未上传完成"):
            resumable_attachments.assemble(db, upload)


def test_resumable_upload_rejects_conflicting_retry(
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        upload = _upload(db, total_size=1)
        resumable_attachments.put_part(db, upload, part_number=0, content=b"a")
        with pytest.raises(DomainError, match="不一致"):
            resumable_attachments.put_part(db, upload, part_number=0, content=b"b")
