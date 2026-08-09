import base64
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def test_single_skill_zip_accepts_skill_files_at_archive_root(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "root-skill.zip",
            "content_base64": _zip_content(
                {
                    "SKILL.md": "# Root Skill\n",
                    "scripts/run.py": "print('ok')\n",
                    "references/guide.md": "# Guide\n",
                    "__MACOSX/._SKILL.md": b"metadata",
                    ".DS_Store": b"metadata",
                }
            ),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["capabilities"] == [
        {
            "capability_key": "root-skill",
            "normalized_config": {
                "entry": "SKILL.md",
                "description": "",
                "version": "",
                "dependencies": {},
                "dependency_build_state": "NOT_REQUIRED",
            },
        }
    ]


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
            "skills/technical-design/scripts/check.sh": "#!/bin/sh\necho ready\n",
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

    mcp_validated = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "MCP",
            "mcp.json",
            '{"mcpServers":{"local-review":{"command":"python","args":["server.py"]}}}',
        ),
    )
    assert mcp_validated.status_code == 200, mcp_validated.text
    mcp_committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": mcp_validated.json()["import_token"]},
    )
    assert mcp_committed.status_code == 201, mcp_committed.text
    capabilities = [*capabilities, *mcp_committed.json()["capabilities"]]

    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "batch imported skills",
            "executor": {},
            "capabilities": capabilities,
        },
    )
    assert saved.status_code == 201, saved.text
    assert [item["capability_key"] for item in saved.json()["capabilities"]] == [
        "requirements-analysis",
        "technical-design",
        "local-review",
    ]
    workspace = Path("test-workspaces") / saved.json()["workspace_ref"]
    assert (workspace / "skills/requirements-analysis/references/checklist.md").read_text() == (
        "# Checklist\n"
    )
    assert (workspace / "skills/technical-design/scripts/validate.py").read_text() == (
        "print('ok')\n"
    )
    assert (workspace / "skills/technical-design/scripts/check.sh").stat().st_mode & 0o111
    mcp_config = (workspace / "mcp/local-review/config.json").read_text()
    assert '"command": "python"' in mcp_config
    assert '"cwd": "/workspaces/nodes/' in mcp_config
    assert (workspace / "files").is_dir()
    assert (workspace / "repositories").is_dir()

    duplicate = client.post(
        "/api/v1/node-assets",
        json={
            "name": "duplicate capability refs",
            "executor": {},
            "capabilities": [capabilities[0], capabilities[0]],
        },
    )
    assert duplicate.status_code == 422, duplicate.text


def test_editing_one_skill_saves_in_place_and_refreshes_bound_node(client):
    encoded = _zip_content(
        {
            "skills/requirements-analysis/SKILL.md": (
                "---\nname: requirements-analysis\ndescription: Analyze requirements\n---\n"
                "# Requirements analysis v1\n"
            ),
            "skills/technical-design/SKILL.md": (
                "---\nname: technical-design\ndescription: Create a design\n---\n"
                "# Technical design v1\n"
            ),
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
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text

    initial = client.get("/api/v1/capabilities").json()
    assert len(initial) == 2
    requirements = next(
        item for item in initial if item["capability_key"] == "requirements-analysis"
    )
    design = next(item for item in initial if item["capability_key"] == "technical-design")

    bound_node = client.post(
        "/api/v1/node-assets",
        json={
            "name": "使用需求分析能力的节点",
            "executor": {},
            "capabilities": [
                {
                    "capability_id": requirements["id"],
                    "capability_type": "SKILL",
                    "capability_key": "requirements-analysis",
                    "normalized_config": {},
                }
            ],
        },
    )
    assert bound_node.status_code == 201, bound_node.text
    workspace = Path("test-workspaces") / bound_node.json()["workspace_ref"]
    skill_file = workspace / "skills/requirements-analysis/SKILL.md"
    assert "v1" in skill_file.read_text()

    source = client.get(f"/api/v1/capabilities/{requirements['id']}/source")
    assert source.status_code == 200, source.text
    revised_content = source.json()["content"].replace("v1", "v2")
    saved = client.put(
        f"/api/v1/capabilities/{requirements['id']}/source",
        json={"content": revised_content},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] == requirements["id"]
    assert saved.json()["content_hash"] != requirements["content_hash"]

    current = client.get("/api/v1/capabilities").json()
    assert len(current) == 2
    assert {item["id"] for item in current} == {requirements["id"], design["id"]}
    assert len([item for item in current if item["capability_key"] == "requirements-analysis"]) == 1
    assert "v2" in client.get(f"/api/v1/capabilities/{requirements['id']}/source").json()["content"]

    persisted_node = client.get(f"/api/v1/node-assets/{bound_node.json()['id']}").json()
    assert persisted_node["capabilities"][0]["capability_id"] == requirements["id"]
    assert (
        persisted_node["capabilities"][0]["normalized_config"]["content_hash"]
        == saved.json()["content_hash"]
    )
    assert "v2" in skill_file.read_text()

    unchanged = client.put(
        f"/api/v1/capabilities/{requirements['id']}/source",
        json={"content": revised_content},
    )
    assert unchanged.status_code == 422, unchanged.text
    assert unchanged.json()["error"]["code"] == "CAPABILITY_SOURCE_UNCHANGED"


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
            {"sample/SKILL.md": "# Sample", "sample/large.txt": b"x" * (25 * 1024 * 1024 + 1)}
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


def test_skill_import_accepts_files_larger_than_two_mib(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "large-reference.zip",
            "content_base64": _zip_content(
                {
                    "large-reference/SKILL.md": "# Large reference\n",
                    "large-reference/references/data.txt": b"x" * (2 * 1024 * 1024 + 1),
                }
            ),
        },
    )
    assert response.status_code == 200, response.text


def test_skill_zip_entry_limit_is_reported_with_actual_and_maximum(client):
    entries: dict[str, bytes | str] = {"sample/SKILL.md": "# Sample\n"}
    entries.update({f"sample/references/reference-{index}.txt": "" for index in range(1000)})

    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "too-many-entries.zip",
            "content_base64": _zip_content(entries),
        },
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "IMPORT_REJECTED"
    assert error["message"] == "ZIP contains 1001 entries; maximum is 1000"
    assert error["details"] == {"actual_entries": 1001, "max_entries": 1000}


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


def test_capability_bulk_delete_skips_active_references_and_ignores_deleted_nodes(
    client, skill_capability
):
    referenced_asset = client.post(
        "/api/v1/node-assets",
        json={
            "name": "引用能力的节点",
            "executor": {},
            "capabilities": [skill_capability],
        },
    )
    assert referenced_asset.status_code == 201, referenced_asset.text
    referenced_asset = referenced_asset.json()

    second_validate = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "unreferenced.zip",
            "content_base64": skill_zip(),
        },
    )
    assert second_validate.status_code == 200, second_validate.text
    second_commit = client.post(
        "/api/v1/capability-imports",
        json={"import_token": second_validate.json()["import_token"]},
    )
    assert second_commit.status_code == 201, second_commit.text

    capabilities = client.get("/api/v1/capabilities").json()
    referenced = next(item for item in capabilities if item["reference_count"] == 1)
    unreferenced = next(item for item in capabilities if item["reference_count"] == 0)
    result = client.request(
        "DELETE",
        "/api/v1/capabilities",
        json={"ids": [referenced["id"], unreferenced["id"]]},
    )
    assert result.status_code == 200, result.text
    assert result.json() == {
        "deleted_ids": [unreferenced["id"]],
        "blocked": [
            {
                "id": referenced["id"],
                "name": referenced["capability_key"],
                "relation": "NODE_CAPABILITY",
                "nodes": [{"id": referenced_asset["id"], "name": referenced_asset["name"]}],
            }
        ],
    }

    assert client.delete(f"/api/v1/node-assets/{referenced_asset['id']}").status_code == 204
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities == [{**referenced, "reference_count": 0}]
    deleted = client.request(
        "DELETE",
        "/api/v1/capabilities",
        json={"ids": [referenced["id"]]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted_ids": [referenced["id"]], "blocked": []}
    assert client.get("/api/v1/capabilities").json() == []
