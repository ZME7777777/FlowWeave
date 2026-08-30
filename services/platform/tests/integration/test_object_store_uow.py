from __future__ import annotations

import base64
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from flowweave.bootstrap.container import Container
from flowweave.bootstrap.settings import Settings
from flowweave.modules.catalog.presentation import router as catalog_router
from flowweave.modules.runs.presentation import router as runs_router
from flowweave.shared.application.artifact_store import ArtifactStorePort
from flowweave.shared.application.transactions import mark_uow_owned
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.errors import DomainError
from flowweave.shared.infrastructure.artifact_store import LocalArtifactStore
from flowweave.shared.models import FlowDefinition, FlowRun
from flowweave.shared.schemas import ArtifactWrite, CapabilityCommitWrite, CapabilityValidateWrite
from flowweave.shared.settings import settings_context


class TransactionProbeStore:
    """Record whether object-store I/O observes an active AsyncSession transaction."""

    def __init__(self, delegate: ArtifactStorePort, session: AsyncSession) -> None:
        self.delegate = delegate
        self.session = session
        self.calls: list[tuple[str, bool, str]] = []

    def _record(self, operation: str, key: str) -> None:
        self.calls.append((operation, self.session.in_transaction(), key))

    def put_temporary(self, namespace: str, content: bytes) -> str:
        self._record("put_temporary", namespace)
        return self.delegate.put_temporary(namespace, content)

    def finalize(self, temporary_key: str, final_key: str) -> str:
        self._record("finalize", final_key)
        return self.delegate.finalize(temporary_key, final_key)

    def put(self, key: str, content: bytes) -> str:
        self._record("put", key)
        return self.delegate.put(key, content)

    def read(self, key: str) -> bytes:
        self._record("read", key)
        return self.delegate.read(key)

    def exists(self, key: str) -> bool:
        self._record("exists", key)
        return self.delegate.exists(key)

    def delete(self, key: str) -> None:
        self._record("delete", key)
        self.delegate.delete(key)


@pytest.mark.asyncio
async def test_lark_url_artifacts_never_touch_object_storage(
    tmp_path: Path,
    container: Container,
    settings: Settings,
    db_session_factory,
) -> None:
    with db_session_factory() as db:
        flow = FlowDefinition(
            name="artifact-store-uow-flow",
        )
        db.add(flow)
        db.flush()
        run = FlowRun(
            flow_definition_id=flow.id,
            run_no=1,
            name="artifact-store-uow-run",
            lark_folder_token="artifact-store-uow-run-folder",
            lark_folder_url=(
                "https://example.feishu.cn/drive/folder/artifact-store-uow-run-folder"
            ),
        )
        db.add(run)
        db.commit()
        run_id = run.id

    async with container.database.sessions() as session:
        mark_uow_owned(session.sync_session)
        delegate = LocalArtifactStore(tmp_path / "artifact-uow")
        store = TransactionProbeStore(delegate, session)
        with settings_context(settings), artifact_store_context(store):
            created = await runs_router.add_artifact(
                run_id,
                ArtifactWrite(
                    field_key="document",
                    artifact_type="URL",
                    uri="https://example.feishu.cn/docx/registered-document",
                ),
                session,
            )
            assert created["uri"] == ("https://example.feishu.cn/docx/registered-document")
            with pytest.raises(DomainError) as external:
                await runs_router.artifact_content(created["id"], session)
            assert external.value.code == "ARTIFACT_EXTERNAL"

            with pytest.raises(DomainError):
                await runs_router.add_artifact(
                    "missing-run",
                    ArtifactWrite(
                        field_key="orphan",
                        artifact_type="URL",
                        uri="https://example.feishu.cn/docx/orphan-document",
                    ),
                    session,
                )

        assert store.calls == []


@pytest.mark.asyncio
async def test_capability_import_object_io_is_outside_database_transactions(
    tmp_path: Path,
    container: Container,
    settings: Settings,
) -> None:
    async with container.database.sessions() as session:
        mark_uow_owned(session.sync_session)
        delegate = LocalArtifactStore(tmp_path / "capability-uow")
        store = TransactionProbeStore(delegate, session)
        payload = CapabilityValidateWrite(
            capability_type="MCP",
            filename="servers.json",
            content_base64=base64.b64encode(
                b'{"servers":{"docs":{"url":"https://mcp.example.test"}}}'
            ).decode(),
        )
        with settings_context(settings), artifact_store_context(store):
            validated = await catalog_router.validate_capability(payload, session)
            committed = await catalog_router.commit_capability(
                CapabilityCommitWrite(import_token=str(validated["import_token"])),
                session,
            )

        assert str(committed["storage_key"]).startswith("capability-imports/")
        assert delegate.exists(str(committed["storage_key"]))
        assert store.calls
        assert all(in_transaction is False for _, in_transaction, _ in store.calls)
        assert [operation for operation, _, _ in store.calls] == ["put_temporary", "finalize"]
