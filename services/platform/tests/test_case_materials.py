"""Official output registration, native reading and revocation on real PostgreSQL."""

import base64
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from test_case_flow_tools import flow_case  # noqa: F401
from test_case_native_tools import native_case, run_locked_case_probe  # noqa: F401

from flowweave.modules.cases.application import materials, native_tools
from flowweave.modules.cases.infrastructure.models import (
    CaseArtifactReference,
    CaseMaterialToolGrant,
)
from flowweave.modules.orchestration.application import service as orchestration
from flowweave.modules.runs.infrastructure.models import (
    CandidateOutputSet,
    NodeAttempt,
    NodeRun,
)
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.errors import DomainError
from flowweave.shared.schemas import ArtifactWrite


@pytest.fixture
def material_case(flow_case):  # noqa: F811
    g = flow_case
    g.material_token = native_tools.prepare_creation(
        g.db, g.entry.id, g.binding, contract=native_tools.MATERIALS
    )[native_tools.MATERIALS.secret_name]
    g.content = ("场景正式文件\n" * 9000).encode()
    g.node = NodeRun(flow_run_id=g.execution.id, flow_node_snapshot_key="first", sequence_no=1)
    g.db.add(g.node)
    g.db.flush()
    g.attempt = NodeAttempt(
        node_run_id=g.node.id,
        attempt_no=1,
        snapshot_id=g.execution.active_snapshot_id,
        state="WAITING_HUMAN_REVIEW",
    )
    g.db.add(g.attempt)
    g.db.flush()
    prepared = orchestration.prepare_file_artifact(
        field_key="report",
        filename="结果.bin",
        mime_type="application/octet-stream",
        content=g.content,
    )
    g.artifact = orchestration._register_artifact(
        g.db,
        g.execution.id,
        prepared,
        source="RUNTIME",
        attempt_id=g.attempt.id,
        runtime_completion_event_id="formal-output-1",
    )
    g.candidate = CandidateOutputSet(
        attempt_id=g.attempt.id,
        completion_event_id="formal-output-1",
        artifact_ids_json=[g.artifact.id],
    )
    g.db.add(g.candidate)
    g.db.flush()
    g.attempt.current_candidate_output_set_id = g.candidate.id
    g.db.commit()
    return g


def rpc(g, args=None, *, token=None):
    response = g.client.post(
        native_tools.MATERIALS.path,
        headers={"Authorization": "Bearer " + (token or g.material_token)},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_case_material", "arguments": args or {}},
        },
    )
    return response


def unpack(response):
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_formal_outputs_register_idempotently_and_keep_provenance(material_case):
    g = material_case
    materials.project_flow_run_artifacts(g.db, g.execution.id)
    materials.project_flow_run_artifacts(g.db, g.execution.id)
    g.db.commit()
    assert g.db.scalar(select(func.count()).select_from(CaseArtifactReference)) == 1
    browser = g.client.get(f"/api/v1/cases/{g.item.id}/artifact-materials", params={"limit": 1})
    assert browser.status_code == 200
    assert browser.json()["items"][0]["artifact_version_id"] == g.artifact.id
    assert browser.json()["items"][0]["content_url"].startswith("/api/v1/artifact-versions/")
    entry = unpack(rpc(g))["items"][0]
    assert entry["artifact_version_id"] == g.artifact.id
    assert entry["stage_key"] == g.stage.stage_key
    assert entry["node_attempt_id"] == g.attempt.id
    assert entry["completion_event_id"] == "formal-output-1"
    assert entry["content_hash"] == hashlib.sha256(g.content).hexdigest()
    assert entry["name"] == "结果.bin" and entry["state"] == "CANDIDATE"
    assert entry["content_url"].endswith(f"/api/v1/artifact-versions/{g.artifact.id}/content")
    assert "storage_key" not in entry and g.material_token not in json.dumps(entry)
    g.node.accepted_attempt_id = g.attempt.id
    g.db.commit()
    assert unpack(rpc(g))["items"][0]["state"] == "ACCEPTED"
    g.candidate.status = "SUPERSEDED"
    g.db.commit()
    assert unpack(rpc(g))["items"][0]["state"] == "SUPERSEDED"


def test_native_reads_file_chunks_and_standard_url_without_login(material_case):
    g = material_case
    chunks, offset = [], 0
    while True:
        result = unpack(
            rpc(g, {"operation": "read", "artifact_version_id": g.artifact.id, "offset": offset})
        )
        chunks.append(base64.b64decode(result["content"]))
        assert len(chunks[-1]) <= 65536
        offset = result["next_offset"]
        if offset is None:
            break
    assert b"".join(chunks) == g.content
    path = f"/api/v1/artifact-versions/{g.artifact.id}/content"
    assert g.client.get(path).content == g.content
    g.client.cookies.clear()
    response = g.client.get(
        path, headers={"Authorization": "Bearer " + g.material_token}, params={"download": "true"}
    )
    assert response.status_code == 200, response.text
    assert response.content == g.content
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert g.client.get(path).status_code == 401
    assert g.client.get(path, headers={"Authorization": "Bearer " + g.token}).status_code == 401
    assert (
        g.client.get(
            "/api/v1/flow-runs", headers={"Authorization": "Bearer " + g.material_token}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("change", ["round", "binding", "directory", "terminal", "permission"])
def test_each_read_revalidates_current_authority(material_case, change):
    from flowweave.modules.users.infrastructure.models import User

    g = material_case
    if change == "round":
        g.item.current_control_round_id = None
    elif change == "binding":
        g.binding.openhands_conversation_id = str(uuid4())
    elif change == "directory":
        g.binding.work_directory_version_id = str(uuid4())
    elif change == "terminal":
        g.item.state = "COMPLETED"
    else:
        g.db.get(User, g.owner).is_active = False
    g.db.commit()
    assert rpc(g).status_code == 401
    g.client.cookies.clear()
    assert (
        g.client.get(
            f"/api/v1/artifact-versions/{g.artifact.id}/content",
            headers={"Authorization": "Bearer " + g.material_token},
        ).status_code
        == 401
    )


def test_unrelated_template_input_and_other_scene_never_gain_read_authority(material_case):
    g = material_case
    # Official input, even in the current run, is not an output.
    prepared = orchestration.prepare_artifact(
        ArtifactWrite(
            field_key="input",
            artifact_type="FILE",
            inline_content="private input",
            metadata={"filename": "input.txt"},
        )
    )
    other = orchestration._register_artifact(g.db, g.execution.id, prepared)
    g.db.commit()
    assert (
        unpack(rpc(g, {"operation": "read", "artifact_version_id": other.id}))["error_code"]
        == "CASE_MATERIAL_NOT_FOUND"
    )
    assert rpc(g, token=g.token).status_code == 401
    assert rpc(g, token=g.flow_token).status_code == 401
    # Linked template must not be registered even if it has a producer attempt.
    node = NodeRun(flow_run_id=g.template.id, flow_node_snapshot_key="first", sequence_no=1)
    g.db.add(node)
    g.db.flush()
    attempt = NodeAttempt(
        node_run_id=node.id, attempt_no=1, snapshot_id=g.template.active_snapshot_id
    )
    g.db.add(attempt)
    g.db.flush()
    prepared = orchestration.prepare_artifact(
        ArtifactWrite(
            field_key="private",
            artifact_type="FILE",
            inline_content="template evidence",
            metadata={"filename": "evidence.txt"},
        )
    )
    hidden = orchestration._register_artifact(
        g.db, g.template.id, prepared, source="RUNTIME", attempt_id=attempt.id
    )
    g.db.commit()
    assert len(unpack(rpc(g))["items"]) == 1
    assert (
        unpack(rpc(g, {"operation": "read", "artifact_version_id": hidden.id}))["error_code"]
        == "CASE_MATERIAL_NOT_FOUND"
    )
    entry = g.db.scalar(select(CaseArtifactReference))
    entry.case_id = g.other.id
    g.db.commit()
    assert unpack(rpc(g))["items"] == []
    assert (
        unpack(rpc(g, {"operation": "read", "artifact_version_id": g.artifact.id}))["error_code"]
        == "CASE_MATERIAL_NOT_FOUND"
    )


def test_links_are_references_and_never_fetched_and_new_versions_do_not_overwrite(material_case):
    g = material_case
    prepared = orchestration.prepare_artifact(
        ArtifactWrite(field_key="report", artifact_type="URL", uri="https://example.test/evidence")
    )
    link = orchestration._register_artifact(
        g.db, g.execution.id, prepared, source="RUNTIME", attempt_id=g.attempt.id
    )
    g.db.commit()
    page1 = unpack(rpc(g, {"limit": 1}))
    page2 = unpack(rpc(g, {"limit": 1, "offset": page1["next_offset"]}))
    assert page2["next_offset"] is None
    entries = page1["items"] + page2["items"]
    assert {e["version_no"] for e in entries} == {1, 2}
    assert (
        next(e for e in entries if e["artifact_version_id"] == link.id)["uri"]
        == "https://example.test/evidence"
    )
    assert next(e for e in entries if e["artifact_version_id"] == link.id)["content_url"] is None
    assert (
        next(e for e in entries if e["artifact_version_id"] == link.id)["content_access"]
        == "EXTERNAL_URI"
    )
    assert (
        unpack(rpc(g, {"operation": "read", "artifact_version_id": link.id}))["error_code"]
        == "ARTIFACT_EXTERNAL"
    )
    g.stage.current_execution_flow_run_id = None
    g.db.commit()
    assert len(unpack(rpc(g))["items"]) == 2
    assert unpack(rpc(g, {"operation": "read", "artifact_version_id": g.artifact.id}))["content"]


def test_source_retention_and_content_drift(material_case, container):
    g = material_case
    with pytest.raises(DomainError, match="场景资料"):
        orchestration.delete_run(g.db, g.execution.id)
    g.db.rollback()
    g.execution.parent_flow_run_id = g.template.id
    g.db.commit()
    with pytest.raises(DomainError, match="场景资料"):
        orchestration.delete_run(g.db, g.template.id)
    g.db.rollback()
    container.artifact_store.put(g.artifact.storage_key, b"changed")
    assert (
        unpack(rpc(g, {"operation": "read", "artifact_version_id": g.artifact.id}))["error_code"]
        == "CASE_MATERIAL_CONTENT_DRIFT"
    )
    with tenant_user(str(uuid4())), pytest.raises(DomainError, match="场景资料"):
        materials.assert_no_case_artifact_references(g.db, g.execution.id)
    materials.delete_case_artifact_references(g.db, g.item.id)
    materials.assert_no_case_artifact_references(g.db, g.execution.id)


def test_user_isolation_and_binding_delete_revoke_materials(material_case):
    g = material_case
    with tenant_user(str(uuid4())):
        assert g.db.scalar(select(CaseArtifactReference)) is None
        assert g.db.scalar(select(CaseMaterialToolGrant)) is None
    g.db.delete(g.binding)
    g.db.commit()
    assert g.db.scalar(select(CaseMaterialToolGrant)) is None
    assert rpc(g).status_code == 401


def test_native_material_tool_restores_same_frozen_authority(material_case, tmp_path):
    run_locked_case_probe(
        material_case,
        tmp_path,
        native_tools.MATERIALS,
        action_args={"operation": "read", "artifact_version_id": material_case.artifact.id},
    )


def test_distinct_runtime_container_reads_authorized_standard_content(material_case, monkeypatch):
    import os
    import subprocess
    import threading
    import time
    from pathlib import Path

    import uvicorn
    from test_case_native_tools import publish_case_tools

    image = os.getenv("CASE_MATERIAL_RUNTIME_IMAGE")
    if not image:
        pytest.skip("Set CASE_MATERIAL_RUNTIME_IMAGE to the source.lock Runtime image")
    g = material_case
    # Use a fresh governed capability for the separate container's reachable URL.
    g.db.query(CaseMaterialToolGrant).delete()
    from flowweave.modules.agent_sessions.infrastructure.models import AgentConversationCapability

    g.db.query(AgentConversationCapability).filter_by(
        binding_id=g.binding.id, capability_key=native_tools.MATERIALS.server_name
    ).delete()
    g.db.commit()
    endpoint = f"http://host.docker.internal:{g.listener.getsockname()[1]}"
    settings = native_tools.get_settings()
    publish_case_tools(
        g.db,
        settings,
        g.client.app.state.container.artifact_store,
        monkeypatch,
        endpoint + native_tools.MATERIALS.path,
        contract=native_tools.MATERIALS,
    )
    token = native_tools.prepare_creation(
        g.db, g.entry.id, g.binding, contract=native_tools.MATERIALS
    )[native_tools.MATERIALS.secret_name]
    # Host Uvicorn serves control-plane/object-store data. The Runtime has no mounts.
    server = uvicorn.Server(uvicorn.Config(g.client.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [g.listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        root = Path(__file__).parents[3]
        lock = json.loads((root / "infra/openhands/source.lock.json").read_text())
        script = (root / "infra/openhands/case_material_read_check.py").read_text()
        config = {
            "source_commit": lock["source_commit"],
            "archive_sha256": lock["archive_sha256"],
            "token": token,
            "mcp_url": endpoint + native_tools.MATERIALS.path,
            "content_url": endpoint + f"/api/v1/artifact-versions/{g.artifact.id}/content",
            "artifact_id": g.artifact.id,
            "content_hash": g.artifact.content_hash,
            "byte_size": len(g.content),
        }
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--entrypoint",
                "/runtime/.venv/bin/python",
                image,
                "-c",
                script,
            ],
            input=json.dumps(config),
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, "Runtime material probe failed: " + result.stdout
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["status"] == "passed"
        assert report["byte_size"] == len(g.content)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        g.listener.close()


def test_migration_rls_backfill_restrict_and_downgrade(material_case):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import IntegrityError

    g = material_case
    source = Path(__file__).parents[1] / "migrations/versions/0185_case_materials.py"
    spec = spec_from_file_location("case_materials_migration", source)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    role = "case_material_rls_" + uuid4().hex
    with g.db.bind.connect() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        connection.execute(
            text(
                "GRANT SELECT, UPDATE ON case_artifact_references, "
                f'case_material_tool_grants TO "{role}"'
            )
        )
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        connection.execute(text("SELECT set_config('flowweave.bypass', 'off', true)"))
        connection.execute(text("SELECT set_config('flowweave.user_id', 'other', true)"))
        for table in ("case_artifact_references", "case_material_tool_grants"):
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert (
                connection.execute(text(f"UPDATE {table} SET owner_user_id = 'bad'")).rowcount == 0
            )
        connection.execute(
            text("SELECT set_config('flowweave.user_id', :owner, true)"), {"owner": g.owner}
        )
        for table in ("case_artifact_references", "case_material_tool_grants"):
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        connection.execute(text("RESET ROLE"))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("DELETE FROM artifact_versions WHERE id = :id"), {"id": g.artifact.id}
            )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        assert connection.scalar(text("SELECT count(*) FROM case_artifact_references")) == 1
        assert connection.scalar(text("SELECT count(*) FROM case_material_tool_grants")) == 0
        assert (
            connection.scalar(
                text("SELECT count(*) FROM artifact_versions WHERE id = :id"), {"id": g.artifact.id}
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relname IN "
                    "('case_artifact_references','case_material_tool_grants') "
                    "AND relrowsecurity AND relforcerowsecurity"
                )
            )
            == 2
        )
        connection.rollback()
