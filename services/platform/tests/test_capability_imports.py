import base64
import io
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from flowweave.shared.models import CapabilityImport


def skill_zip() -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sample/SKILL.md", "# Sample\n")
        archive.writestr("sample/reference.md", "evidence")
    return base64.b64encode(buffer.getvalue()).decode()


def test_import_is_persistent_hashed_one_time_and_stores_source(client, db_session_factory):
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={"capability_type": "SKILL", "filename": "sample.zip", "content_base64": skill_zip()},
    )
    assert validated.status_code == 200, validated.text
    body = validated.json()
    with db_session_factory() as db:
        row = db.scalar(select(CapabilityImport))
        assert row.token_digest != body["import_token"]
        assert len(row.token_digest) == 64
        assert row.state == "VALIDATED"
    committed = client.post(
        "/api/v1/capability-imports", json={"import_token": body["import_token"]}
    )
    assert committed.status_code == 201, committed.text
    committed_body = committed.json()
    assert committed_body["storage_key"].startswith("capability-imports/")
    assert committed_body["capabilities"][0]["capability_key"] == "sample"
    assert (
        committed_body["capabilities"][0]["normalized_config"]["import_id"] == committed_body["id"]
    )
    replay = client.post("/api/v1/capability-imports", json={"import_token": body["import_token"]})
    assert replay.status_code == 422


def test_skill_zip_imports_multiple_skills_and_saves_them_to_one_node(client):
    encoded = _zip_content(
        {
            "skills/requirements-analysis/SKILL.md": (
                "---\nname: requirements-analysis\ndescription: Analyze requirements\n---\n"
                "# Requirements analysis\n"
            ),
            "skills/requirements-analysis/references/checklist.md": "# Checklist\n",
            "skills/technical-design/SKILL.md": (
                "---\nname: technical-design\ndescription: Create a technical design\n---\n"
                "# Technical design\n"
            ),
            "skills/technical-design/scripts/validate.py": "print('ok')\n",
        }
    )
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "team-skills.zip",
            "content_base64": encoded,
        },
    )
    assert validated.status_code == 200, validated.text
    preview = validated.json()["preview"]
    assert [item["capability_key"] for item in preview["capabilities"]] == [
        "requirements-analysis",
        "technical-design",
    ]

    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    capabilities = committed.json()["capabilities"]
    assert len(capabilities) == 2
    assert {item["normalized_config"]["import_id"] for item in capabilities} == {
        committed.json()["id"]
    }
    assert {item["normalized_config"]["entry"] for item in capabilities} == {
        "skills/requirements-analysis/SKILL.md",
        "skills/technical-design/SKILL.md",
    }

    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "batch imported skills",
            "executor": {},
            "default_skill_ref": "requirements-analysis",
            "capabilities": capabilities,
        },
    )
    assert saved.status_code == 201, saved.text
    assert [item["capability_key"] for item in saved.json()["capabilities"]] == [
        "requirements-analysis",
        "technical-design",
    ]

    duplicate = client.post(
        "/api/v1/node-assets",
        json={
            "name": "duplicate capability refs",
            "executor": {},
            "default_skill_ref": "requirements-analysis",
            "capabilities": [capabilities[0], capabilities[0]],
        },
    )
    assert duplicate.status_code == 422, duplicate.text


def test_skill_zip_rejects_duplicate_skill_names_atomically(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "duplicate-skills.zip",
            "content_base64": _zip_content(
                {
                    "skill-a/SKILL.md": "---\nname: duplicate\n---\n# A\n",
                    "skill-b/SKILL.md": "---\nname: duplicate\n---\n# B\n",
                }
            ),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IMPORT_REJECTED"
    assert response.json()["error"]["details"]["capability_keys"] == ["duplicate"]


def test_expired_import_cannot_be_committed(client, db_session_factory):
    body = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "mcp.json",
            "content_base64": base64.b64encode(
                b'{"servers": {"docs": {"url": "https://mcp.test"}}}'
            ).decode(),
        },
    ).json()
    with db_session_factory() as db:
        row = db.scalar(select(CapabilityImport))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    response = client.post(
        "/api/v1/capability-imports", json={"import_token": body["import_token"]}
    )
    assert response.status_code == 422
    with db_session_factory() as db:
        assert db.scalar(select(CapabilityImport)).state == "EXPIRED"


def test_import_rejects_secrets_and_unsafe_filenames(client):
    secret = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "HOOK",
            "filename": "hook.yaml",
            "content_base64": base64.b64encode(b"api_key: no\n").decode(),
        },
    )
    assert secret.status_code == 422
    unsafe = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "MCP",
            "filename": "../mcp.json",
            "content_base64": base64.b64encode(b"{}").decode(),
        },
    )
    assert unsafe.status_code == 422


def test_node_asset_rejects_forged_or_tampered_capability_import(client):
    forged = client.post(
        "/api/v1/node-assets",
        json={
            "name": "forged capability",
            "executor": {},
            "default_skill_ref": "forged",
            "capabilities": [
                {
                    "capability_type": "SKILL",
                    "capability_key": "forged",
                    "normalized_config": {"import_id": "not-real"},
                }
            ],
        },
    )
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "CAPABILITY_IMPORT_REQUIRED"

    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={"capability_type": "SKILL", "filename": "sample.zip", "content_base64": skill_zip()},
    ).json()
    committed = client.post(
        "/api/v1/capability-imports", json={"import_token": validated["import_token"]}
    ).json()
    capability = committed["capabilities"][0]
    capability["capability_key"] = "tampered"
    tampered = client.post(
        "/api/v1/node-assets",
        json={
            "name": "tampered capability",
            "executor": {},
            "default_skill_ref": "tampered",
            "capabilities": [capability],
        },
    )
    assert tampered.status_code == 422
    assert tampered.json()["error"]["code"] == "CAPABILITY_IMPORT_INVALID"


def _zip_content(entries: dict[str, bytes | str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return base64.b64encode(buffer.getvalue()).decode()


def _validate_payload(capability_type: str, filename: str, content: bytes | str) -> dict:
    raw = content.encode() if isinstance(content, str) else content
    return {
        "capability_type": capability_type,
        "filename": filename,
        "content_base64": base64.b64encode(raw).decode(),
    }


def test_skill_import_rejects_nested_archives_extensions_depth_and_large_files(client):
    cases = [
        _zip_content({"sample/SKILL.md": "# Sample", "sample/payload.zip": b"not-a-zip"}),
        _zip_content({"sample/SKILL.md": "# Sample", "sample/program.exe": b"binary"}),
        _zip_content({"a/b/c/d/e/f/g/h/SKILL.md": "# Too deep"}),
        _zip_content(
            {"sample/SKILL.md": "# Sample", "sample/large.txt": b"x" * (2 * 1024 * 1024 + 1)}
        ),
    ]
    for encoded in cases:
        response = client.post(
            "/api/v1/capability-imports/validate",
            json={
                "capability_type": "SKILL",
                "filename": "unsafe.zip",
                "content_base64": encoded,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "IMPORT_REJECTED"


def test_config_import_rejects_alias_bombs_recursive_aliases_and_deep_values(client):
    alias_bomb = "base: &base {url: https://mcp.test}\nservers:\n" + "\n".join(
        f"  server_{index}: *base" for index in range(21)
    )
    recursive = "servers: &servers\n  self: *servers\n"
    deep_lines = ["servers:", "  docs:"]
    deep_lines.extend(f"{'  ' * (level + 2)}value:" for level in range(21))
    deep_lines.append(f"{'  ' * 23}leaf: true")
    deep = "\n".join(deep_lines) + "\n"
    for content in (alias_bomb, recursive, deep):
        response = client.post(
            "/api/v1/capability-imports/validate",
            json=_validate_payload("MCP", "unsafe.yaml", content),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "IMPORT_REJECTED"

    wrong_extension = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload("MCP", "servers.txt", '{"servers":{"docs":{}}}'),
    )
    assert wrong_extension.status_code == 422


def test_expired_import_cleanup_task_deletes_only_uncommitted_source(
    worker_client, worker_container, db_session_factory
):
    from flowweave.bootstrap.worker import TaskWorker
    from flowweave.shared.models import BackgroundTask

    response = worker_client.post(
        "/api/v1/capability-imports/validate",
        json={"capability_type": "SKILL", "filename": "sample.zip", "content_base64": skill_zip()},
    )
    assert response.status_code == 200, response.text
    with db_session_factory() as db:
        import_row = db.scalar(select(CapabilityImport))
        cleanup = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_CAPABILITY_IMPORT",
                BackgroundTask.aggregate_id == import_row.id,
            )
        )
        source = worker_container.settings.artifact_root / import_row.storage_key
        assert source.is_file()
        import_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        cleanup.available_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        import_id = import_row.id

    worker = TaskWorker(worker_container)
    assert worker._run_once_sync() is True
    with db_session_factory() as db:
        assert db.get(CapabilityImport, import_id).state == "EXPIRED"
        cleanup = db.scalar(select(BackgroundTask).where(BackgroundTask.aggregate_id == import_id))
        assert cleanup.state == "SUCCEEDED"
    assert not source.exists()
    assert worker._run_once_sync() is False

    committed_validate = worker_client.post(
        "/api/v1/capability-imports/validate",
        json={"capability_type": "SKILL", "filename": "sample.zip", "content_base64": skill_zip()},
    ).json()
    committed = worker_client.post(
        "/api/v1/capability-imports",
        json={"import_token": committed_validate["import_token"]},
    ).json()
    committed_source = worker_container.settings.artifact_root / committed["storage_key"]
    with db_session_factory() as db:
        cleanup = db.scalar(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "CLEANUP_CAPABILITY_IMPORT",
                BackgroundTask.aggregate_id == committed["id"],
            )
        )
        cleanup.available_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert worker._run_once_sync() is True
    with db_session_factory() as db:
        assert db.get(CapabilityImport, committed["id"]).state == "COMMITTED"
    assert committed_source.is_file()
