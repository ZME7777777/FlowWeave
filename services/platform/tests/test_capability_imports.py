import base64
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from flowweave.modules.catalog.application.capability_repository import (
    publish_dependency_build,
    resolve_version,
)
from flowweave.shared.models import (
    CapabilityImport,
    CapabilityVersion,
    NodeAsset,
    NodeCapabilityRef,
)


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
    capability = committed_body["capabilities"][0]
    assert capability["capability_key"] == "sample"
    assert len(capability["capability_id"]) == 36
    assert capability["normalized_config"]["capability_version_id"] == capability["capability_id"]
    assert len(capability["normalized_config"]["digest"]) == 64
    assert "import_id" not in capability["normalized_config"]
    listed = client.get("/api/v1/capabilities").json()[0]
    assert listed["id"] == capability["capability_id"]
    assert listed["import_id"] == committed_body["id"]
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


def test_dependency_build_publishes_derived_version_without_rebinding_node(
    client, db_session_factory
):
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "dependent-skill.zip",
            "content_base64": _zip_content(
                {
                    "dependent/SKILL.md": (
                        "---\nname: dependent\ndependencies:\n"
                        "  python:\n    requests: 2.32.3\n---\n# Dependent\n"
                    )
                }
            ),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    capability = committed.json()["capabilities"][0]
    source_id = capability["capability_id"]

    with db_session_factory() as db:
        source = resolve_version(db, source_id)
        # Simulate a consumer persisted before dependency builds became a
        # publish-new-version operation. New API requests correctly reject
        # PENDING dependency versions, but existing references must not drift.
        node = NodeAsset(name="Pinned dependency node")
        db.add(node)
        db.flush()
        db.add(
            NodeCapabilityRef(
                node_asset_id=node.id,
                capability_type=source.package.capability_type,
                capability_key=source.package.capability_key,
                capability_version_id=source.version.id,
                normalized_config=source.runtime_config(),
            )
        )
        db.flush()
        ready_config = {
            **source.version.normalized_config_json,
            "dependency_build_state": "READY",
            "dependency_storage_key": "capability-dependencies/fixture.zip",
            "dependency_content_hash": "a" * 64,
            "dependency_manifest": {"files": []},
        }
        derived, created = publish_dependency_build(db, source, ready_config)
        db.commit()
        derived_id = derived.version.id

    assert created is True
    assert derived_id != source_id
    with db_session_factory() as db:
        source = db.get(CapabilityVersion, source_id)
        derived = db.get(CapabilityVersion, derived_id)
        node_ref = db.scalar(select(NodeCapabilityRef))
        assert source is not None and derived is not None and node_ref is not None
        assert source.normalized_config_json["dependency_build_state"] == "PENDING"
        assert derived.normalized_config_json["dependency_build_state"] == "READY"
        assert derived.package_id == source.package_id
        assert derived.blob_id == source.blob_id
        assert derived.version_no == source.version_no + 1
        assert node_ref.capability_version_id == source_id


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
    assert len({item["capability_id"] for item in capabilities}) == 2
    assert all(len(item["capability_id"]) == 36 for item in capabilities)
    assert all(
        item["normalized_config"]["capability_version_id"] == item["capability_id"]
        and len(item["normalized_config"]["digest"]) == 64
        and "import_id" not in item["normalized_config"]
        for item in capabilities
    )
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
        "flowweave-default-tools",
        "flowweave-default-context",
        "flowweave-memory-disabled",
        "flowweave-critic-disabled",
    ]
    assert [item["capability_type"] for item in saved.json()["capabilities"][-4:]] == [
        "TOOL_POLICY",
        "CONTEXT_POLICY",
        "MEMORY_POLICY",
        "CRITIC_POLICY",
    ]
    workspace = Path("test-workspaces") / saved.json()["workspace_ref"]
    assert (workspace / "skills/requirements-analysis/references/checklist.md").read_text() == (
        "# Checklist\n"
    )
    assert (workspace / "skills/technical-design/scripts/validate.py").read_text() == (
        "print('ok')\n"
    )
    assert (workspace / "skills/technical-design/scripts/check.sh").stat().st_mode & 0o111
    managed = Path("test-workspaces/.managed-assets") / saved.json()["workspace_ref"]
    mcp_config = (managed / "mcp/local-review/config.json").read_text()
    assert '"command": "python"' in mcp_config
    assert '"cwd": "/runtime/capabilities/nodes/' in mcp_config
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


def test_mcp_config_normalizes_flowweave_transports(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "MCP",
            "mcp.json",
            """
{
  "mcpServers": {
    "remote-docs": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp",
      "timeout": 30
    },
    "local-review": {
      "command": "python",
      "args": ["-m", "review_server"],
      "env": {"LOG_LEVEL": "info"}
    }
  }
}
""",
        ),
    )

    assert response.status_code == 200, response.text
    capabilities = response.json()["preview"]["capabilities"]
    assert capabilities == [
        {
            "capability_key": "remote-docs",
            "normalized_config": {
                "url": "https://mcp.example.com/mcp",
                "timeout": 30,
                "transport": "streamable-http",
            },
        },
        {
            "capability_key": "local-review",
            "normalized_config": {
                "command": "python",
                "args": ["-m", "review_server"],
                "env": {"LOG_LEVEL": "info"},
                "transport": "stdio",
            },
        },
    ]


def test_mcp_config_rejects_invalid_transport_contracts(client):
    cases = (
        ("remote-without-url", {"transport": "streamable-http"}),
        ("stdio-without-command", {"transport": "stdio"}),
        ("unknown-field", {"url": "https://mcp.test/mcp", "custom": True}),
        ("invalid-args", {"command": "python", "args": [1]}),
    )
    for name, server in cases:
        response = client.post(
            "/api/v1/capability-imports/validate",
            json=_validate_payload(
                "MCP",
                "mcp.json",
                json.dumps({"mcpServers": {name: server}}),
            ),
        )
        assert response.status_code == 422, (name, response.text)
        assert response.json()["error"]["code"] == "IMPORT_REJECTED"


def test_stdio_mcp_persists_multiple_scripts_and_materializes_them(client):
    payload = _validate_payload(
        "MCP",
        "mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "local-tools": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["scripts/server.py"],
                    },
                    "remote-docs": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.test/mcp",
                    },
                }
            }
        ),
    )
    payload["mcp_scripts"] = [
        {
            "server": "local-tools",
            "filename": "server.py",
            "content_base64": base64.b64encode(b"print('server')\n").decode(),
        },
        {
            "server": "local-tools",
            "filename": "settings.json",
            "content_base64": base64.b64encode(b'{"mode": "readonly"}\n').decode(),
        },
    ]

    validated = client.post("/api/v1/capability-imports/validate", json=payload)
    assert validated.status_code == 200, validated.text
    assert validated.json()["preview"]["script_count"] == 2
    local_preview = validated.json()["preview"]["capabilities"][0]
    assert local_preview["normalized_config"]["script_files"] == [
        "server.py",
        "settings.json",
    ]

    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    local_capability = committed.json()["capabilities"][0]
    assert local_capability["normalized_config"]["package_format"] == "mcp-bundle-v1"
    assert set(local_capability["normalized_config"]["script_hashes"]) == {
        "server.py",
        "settings.json",
    }

    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "scripted MCP node",
            "executor": {},
            "capabilities": [local_capability],
        },
    )
    assert saved.status_code == 201, saved.text
    managed = Path("test-workspaces/.managed-assets") / saved.json()["workspace_ref"]
    mcp_root = managed / "mcp/local-tools"
    assert (mcp_root / "scripts/server.py").read_text() == "print('server')\n"
    assert (mcp_root / "scripts/settings.json").read_text() == '{"mode": "readonly"}\n'
    config = json.loads((mcp_root / "config.json").read_text())
    assert config["command"] == "python"
    assert config["args"] == ["scripts/server.py"]
    expected_cwd = f"/runtime/capabilities/nodes/{saved.json()['id']}/mcp/local-tools"
    assert config["cwd"] == expected_cwd


def test_mcp_scripts_reject_remote_server_and_unsafe_filename(client):
    config = json.dumps(
        {
            "mcpServers": {
                "remote": {
                    "transport": "streamable-http",
                    "url": "https://mcp.example.test/mcp",
                },
                "local": {"transport": "stdio", "command": "python"},
            }
        }
    )
    cases = [
        ("remote", "server.py"),
        ("local", "../server.py"),
        ("local", "server.exe"),
    ]
    for server, filename in cases:
        payload = _validate_payload("MCP", "mcp.json", config)
        payload["mcp_scripts"] = [
            {
                "server": server,
                "filename": filename,
                "content_base64": base64.b64encode(b"content").decode(),
            }
        ]
        response = client.post("/api/v1/capability-imports/validate", json=payload)
        assert response.status_code == 422, (server, filename, response.text)
        assert response.json()["error"]["code"] == "IMPORT_REJECTED"


def test_hook_config_normalizes_form_json_and_binds_to_node(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "HOOK",
            "guardrails.json",
            json.dumps(
                {
                    "name": "security-guardrails",
                    "description": "Block unsafe terminal operations",
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "terminal",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "flowweave-policy-check",
                                        "timeout": 30,
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["preview"]["capabilities"] == [
        {
            "capability_key": "security-guardrails",
            "normalized_config": {
                "pre_tool_use": [
                    {
                        "matcher": "terminal",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "flowweave-policy-check",
                                "timeout": 30,
                            }
                        ],
                    }
                ],
                "description": "Block unsafe terminal operations",
            },
        }
    ]
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": response.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "hook-enabled node",
            "executor": {},
            "capabilities": committed.json()["capabilities"],
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["capabilities"][0]["capability_type"] == "HOOK"
    assert "pre_tool_use" in saved.json()["capabilities"][0]["normalized_config"]


def test_hook_config_rejects_async_blocking_action(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "HOOK",
            "guardrails.json",
            json.dumps(
                {
                    "name": "unsafe-hook",
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "terminal",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "policy-check",
                                        "async": True,
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IMPORT_REJECTED"
    assert response.json()["error"]["details"]["event"] == "pre_tool_use"


def test_hook_script_action_requires_and_persists_its_uploaded_attachment(client):
    config = json.dumps(
        {
            "name": "script-guardrails",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "terminal",
                        "hooks": [
                            {
                                "type": "script",
                                "name": "check-terminal",
                                "script": "check.py",
                                "timeout": 30,
                            }
                        ],
                    }
                ]
            },
        }
    )
    missing = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload("HOOK", "guardrails.json", config),
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["details"]["filenames"] == ["check.py"]

    payload = _validate_payload("HOOK", "guardrails.json", config)
    payload["hook_scripts"] = [
        {
            "filename": "check.py",
            "content_base64": base64.b64encode(b"print('allow')\n").decode(),
        }
    ]
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json=payload,
    )
    assert validated.status_code == 200, validated.text
    normalized = validated.json()["preview"]["capabilities"][0]["normalized_config"]
    assert normalized["script_files"] == ["check.py"]
    assert len(normalized["script_hashes"]["check.py"]) == 64
    assert normalized["pre_tool_use"][0]["hooks"][0] == {
        "type": "script",
        "name": "check-terminal",
        "script": "check.py",
        "timeout": 30,
    }

    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    committed_config = committed.json()["capabilities"][0]["normalized_config"]
    assert committed_config["package_format"] == "hook-bundle-v1"
    assert committed_config["storage_key"] == committed.json()["storage_key"]


def test_hook_script_upload_rejects_unused_and_non_executable_files(client):
    config = json.dumps(
        {
            "name": "script-guardrails",
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": "echo ready"}],
                    }
                ]
            },
        }
    )
    for filename in ("unused.py", "policy.txt"):
        payload = _validate_payload("HOOK", "guardrails.json", config)
        payload["hook_scripts"] = [
            {
                "filename": filename,
                "content_base64": base64.b64encode(b"content\n").decode(),
            }
        ]
        response = client.post(
            "/api/v1/capability-imports/validate",
            json=payload,
        )
        assert response.status_code == 422, (filename, response.text)
        assert response.json()["error"]["code"] == "IMPORT_REJECTED"


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
    assert saved.json()["id"] != requirements["id"]
    assert saved.json()["lineage_id"] == requirements["lineage_id"]
    assert saved.json()["revision_number"] == requirements["revision_number"] + 1
    assert saved.json()["content_hash"] != requirements["content_hash"]

    current = client.get("/api/v1/capabilities").json()
    skills = [item for item in current if item["capability_type"] == "SKILL"]
    assert len(skills) == 3
    assert {item["id"] for item in skills} == {
        requirements["id"],
        design["id"],
        saved.json()["id"],
    }
    revisions = [item for item in skills if item["capability_key"] == "requirements-analysis"]
    assert len(revisions) == 2
    assert {item["is_latest"] for item in revisions} == {False, True}
    assert "v1" in client.get(f"/api/v1/capabilities/{requirements['id']}/source").json()["content"]
    assert "v2" in client.get(f"/api/v1/capabilities/{saved.json()['id']}/source").json()["content"]

    persisted_node = client.get(f"/api/v1/node-assets/{bound_node.json()['id']}").json()
    assert persisted_node["capabilities"][0]["capability_id"] == requirements["id"]
    assert (
        persisted_node["capabilities"][0]["normalized_config"]["content_hash"]
        == requirements["content_hash"]
    )
    assert "v1" in skill_file.read_text()

    unchanged = client.put(
        f"/api/v1/capabilities/{saved.json()['id']}/source",
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
            "capability_type": "MCP",
            "filename": "mcp.json",
            "content_base64": base64.b64encode(b'{"servers":{"docs":{"api_key":"no"}}}').decode(),
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
    assert forged.json()["error"]["code"] == "CAPABILITY_VERSION_REQUIRED"

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
    assert tampered.json()["error"]["code"] == "CAPABILITY_REFERENCE_INVALID"


def _zip_content(entries: dict[str, bytes | str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return base64.b64encode(buffer.getvalue()).decode()


def _plugin_zip() -> str:
    return _zip_content(
        {
            ".plugin/plugin.json": json.dumps(
                {
                    "name": "governed-review",
                    "version": "1.2.3",
                    "description": "Frozen review capabilities",
                }
            ),
            "skills/review/SKILL.md": "---\nname: review\n---\n# Review\n",
            "commands/check.md": "---\nname: check\n---\n# Check\n",
            "hooks/hooks.json": json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "terminal",
                                "hooks": [
                                    {
                                        "type": "prompt",
                                        "prompt": "Review the tool call.",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            ".mcp.json": json.dumps(
                {
                    "mcpServers": {
                        "docs": {
                            "url": "https://mcp.example.test",
                            "transport": "streamable-http",
                        }
                    }
                }
            ),
        }
    )


def test_plugin_import_freezes_manifest_contributions_and_node_binding(client):
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "PLUGIN",
            "filename": "governed-review.zip",
            "content_base64": _plugin_zip(),
        },
    )
    assert validated.status_code == 200, validated.text
    preview = validated.json()["preview"]
    assert preview["contributions"] == {
        "skills": ["review"],
        "mcp_servers": ["docs"],
        "hook_events": ["pre_tool_use"],
        "commands": ["check"],
    }
    normalized = preview["capabilities"][0]["normalized_config"]
    assert normalized["entry"] == "."
    assert normalized["package_format"] == "openhands-plugin-v1"
    assert set(normalized["file_hashes"]) == {
        ".plugin/plugin.json",
        "skills/review/SKILL.md",
        "commands/check.md",
        "hooks/hooks.json",
        ".mcp.json",
    }

    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    plugin = committed.json()["capabilities"][0]
    assert plugin["capability_type"] == "PLUGIN"
    assert plugin["capability_key"] == "governed-review"
    assert len(plugin["capability_id"]) == 36
    assert len(plugin["normalized_config"]["digest"]) == 64

    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "plugin governed node",
            "executor": {},
            "capabilities": [plugin],
        },
    )
    assert saved.status_code == 201, saved.text
    by_type = {item["capability_type"]: item for item in saved.json()["capabilities"]}
    assert by_type["PLUGIN"]["capability_id"] == plugin["capability_id"]
    assert by_type["TOOL_POLICY"]["capability_key"] == "flowweave-default-tools"


def test_plugin_import_rejects_multiple_roots_and_special_files(client):
    multiple = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "PLUGIN",
            "filename": "multiple.zip",
            "content_base64": _zip_content(
                {
                    "one/.plugin/plugin.json": '{"name":"one"}',
                    "two/.plugin/plugin.json": '{"name":"two"}',
                }
            ),
        },
    )
    assert multiple.status_code == 422
    assert multiple.json()["error"]["code"] == "IMPORT_REJECTED"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(".plugin/plugin.json", '{"name":"unsafe"}')
        link = zipfile.ZipInfo("commands/link.md")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "target")
    special = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "PLUGIN",
            "filename": "unsafe.zip",
            "content_base64": base64.b64encode(buffer.getvalue()).decode(),
        },
    )
    assert special.status_code == 422
    assert special.json()["error"]["code"] == "IMPORT_REJECTED"


def test_plugin_import_rejects_unmerged_agents_and_invalid_contributions(client):
    cases = [
        _zip_content(
            {
                ".plugin/plugin.json": '{"name":"plugin-agent"}',
                "agents/reviewer.md": "# Reviewer\n",
            }
        ),
        _zip_content(
            {
                ".plugin/plugin.json": '{"name":"empty"}',
                "README.md": "# No runtime contribution\n",
            }
        ),
        _zip_content(
            {
                ".plugin/plugin.json": '{"name":"bad-mcp"}',
                ".mcp.json": '{"mcpServers":{}}',
            }
        ),
        _zip_content(
            {
                ".plugin/plugin.json": '{"name":"bad-hook"}',
                "hooks/hooks.json": '{"hooks":{}}',
            }
        ),
    ]
    for encoded in cases:
        response = client.post(
            "/api/v1/capability-imports/validate",
            json={
                "capability_type": "PLUGIN",
                "filename": "rejected.zip",
                "content_base64": encoded,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "IMPORT_REJECTED"


def _validate_payload(capability_type: str, filename: str, content: bytes | str) -> dict:
    raw = content.encode() if isinstance(content, str) else content
    return {
        "capability_type": capability_type,
        "filename": filename,
        "content_base64": base64.b64encode(raw).decode(),
    }


def _import_tool_policy(
    client, name: str, tools: list[str], *, tool_concurrency_limit: int = 1
) -> dict:
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "TOOL_POLICY",
            f"{name}.json",
            json.dumps(
                {
                    "name": name,
                    "description": f"Policy {name}",
                    "tool_concurrency_limit": tool_concurrency_limit,
                    "tools": [{"name": tool, "params": {}} for tool in tools],
                }
            ),
        ),
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def _import_agent_definition(client, name: str, *, tools: list[str]) -> dict:
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "AGENT_DEFINITION",
            f"{name}.json",
            json.dumps(
                {
                    "name": name,
                    "description": f"Agent {name}",
                    "tools": tools,
                    "system_prompt": "Complete the delegated task and report evidence.",
                    "permission_mode": "never_confirm",
                }
            ),
        ),
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    return committed.json()["capabilities"][0]


def test_agent_profile_requires_complete_policy_refs_and_materializes_exactly(client):
    baseline = client.post(
        "/api/v1/node-assets",
        json={"name": "profile policy baseline", "executor": {}, "capabilities": []},
    )
    assert baseline.status_code == 201, baseline.text
    policies = {
        item["capability_type"]: item
        for item in baseline.json()["capabilities"]
        if item["capability_type"].endswith("_POLICY")
    }
    assert set(policies) == {
        "TOOL_POLICY",
        "CONTEXT_POLICY",
        "MEMORY_POLICY",
        "CRITIC_POLICY",
    }

    incomplete = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "AGENT_PROFILE",
            "incomplete-profile.json",
            json.dumps(
                {
                    "name": "incomplete-profile",
                    "tool_policy_version_id": policies["TOOL_POLICY"]["capability_id"],
                }
            ),
        ),
    )
    assert incomplete.status_code == 422, incomplete.text
    assert incomplete.json()["error"]["code"] == "IMPORT_REJECTED"

    document = {
        "name": "governed-profile",
        "description": "Fully materialized OpenHands profile",
        "agent_kind": "OPENHANDS",
        "model": "inherit",
        "tool_policy_version_id": policies["TOOL_POLICY"]["capability_id"],
        "context_policy_version_id": policies["CONTEXT_POLICY"]["capability_id"],
        "memory_policy_version_id": policies["MEMORY_POLICY"]["capability_id"],
        "critic_policy_version_id": policies["CRITIC_POLICY"]["capability_id"],
        "confirmation_policy": "ALWAYS",
        "max_iterations": 100,
    }
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload("AGENT_PROFILE", "governed-profile.json", json.dumps(document)),
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    profile = committed.json()["capabilities"][0]

    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "profile governed node",
            "executor": {"confirmation_policy": "ALWAYS", "max_iterations": 100},
            "capabilities": [profile],
        },
    )
    assert saved.status_code == 201, saved.text
    bound = {item["capability_type"]: item for item in saved.json()["capabilities"]}
    assert bound["AGENT_PROFILE"]["capability_id"] == profile["capability_id"]
    for field, capability_type in (
        ("tool_policy_version_id", "TOOL_POLICY"),
        ("context_policy_version_id", "CONTEXT_POLICY"),
        ("memory_policy_version_id", "MEMORY_POLICY"),
        ("critic_policy_version_id", "CRITIC_POLICY"),
    ):
        assert profile["normalized_config"][field] == bound[capability_type]["capability_id"]

    mismatch = client.post(
        "/api/v1/node-assets",
        json={
            "name": "profile executor mismatch",
            "executor": {"confirmation_policy": "NEVER", "max_iterations": 100},
            "capabilities": [profile],
        },
    )
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["error"]["code"] == "AGENT_PROFILE_EXECUTOR_MISMATCH"


@pytest.mark.parametrize("permission_mode", [None, "always_confirm", "confirm_risky"])
def test_agent_definition_rejects_confirmation_modes_without_server_callback(
    client, permission_mode
):
    document = {
        "name": "unsafe-confirmation-agent",
        "description": "Must not silently approve governed actions",
        "tools": ["terminal"],
        "system_prompt": "Inspect the workspace and report evidence.",
    }
    if permission_mode is not None:
        document["permission_mode"] = permission_mode
    rejected = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "AGENT_DEFINITION",
            "unsafe-confirmation-agent.json",
            json.dumps(document),
        ),
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "IMPORT_REJECTED"
    assert "must be never_confirm" in rejected.json()["error"]["message"]


def test_tool_policy_import_is_strict_and_node_binds_exactly_one_version(client):
    rejected = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "TOOL_POLICY",
            "unsafe.json",
            '{"name":"unsafe","tools":[{"name":"browser","params":{}}]}',
        ),
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "IMPORT_REJECTED"

    unsafe_parallel = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "TOOL_POLICY",
            "unsafe-parallel.json",
            json.dumps(
                {
                    "name": "unsafe-parallel",
                    "tool_concurrency_limit": 2,
                    "tools": [{"name": "terminal", "params": {}}],
                }
            ),
        ),
    )
    assert unsafe_parallel.status_code == 422, unsafe_parallel.text
    assert unsafe_parallel.json()["error"]["code"] == "IMPORT_REJECTED"

    read_only_parallel = _import_tool_policy(
        client,
        "read-only-parallel",
        ["grep", "glob", "list_directory", "read_file"],
        tool_concurrency_limit=4,
    )
    frozen = read_only_parallel["normalized_config"]
    assert frozen["unknown_tool"] == "DENY"
    assert frozen["openhands_version"] == "1.42.0"
    assert frozen["tool_concurrency_limit"] == 4
    assert frozen["confirmation_required_tools"] == []
    assert {tool["access"] for tool in frozen["tools"]} == {"READ_ONLY"}
    assert all(tool["source"]["version"] == "1.42.0" for tool in frozen["tools"])

    no_confirmation = client.post(
        "/api/v1/node-assets",
        json={
            "name": "mutating tools without confirmation",
            "executor": {"confirmation_policy": "NEVER"},
            "capabilities": [_import_tool_policy(client, "mutating-policy", ["file_editor"])],
        },
    )
    assert no_confirmation.status_code == 422, no_confirmation.text
    assert no_confirmation.json()["error"]["code"] == "TOOL_POLICY_CONFIRMATION_REQUIRED"

    terminal_only = _import_tool_policy(client, "terminal-only", ["terminal"])
    file_only = _import_tool_policy(client, "file-only", ["file_editor"])
    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "terminal governed node",
            "executor": {},
            "capabilities": [terminal_only],
        },
    )
    assert saved.status_code == 201, saved.text
    saved_capabilities = saved.json()["capabilities"]
    assert [
        item["capability_id"]
        for item in saved_capabilities
        if item["capability_type"] == "TOOL_POLICY"
    ] == [terminal_only["capability_id"]]
    assert [
        item["capability_key"]
        for item in saved_capabilities
        if item["capability_type"] == "CONTEXT_POLICY"
    ] == ["flowweave-default-context"]

    conflicting = client.post(
        "/api/v1/node-assets",
        json={
            "name": "conflicting policies",
            "executor": {},
            "capabilities": [terminal_only, file_only],
        },
    )
    assert conflicting.status_code == 422, conflicting.text
    assert conflicting.json()["error"]["code"] == "TOOL_POLICY_CONFLICT"


def test_agent_definition_is_immutable_and_requires_governed_task_tools(client):
    nested = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "AGENT_DEFINITION",
            "unsafe.json",
            json.dumps(
                {
                    "name": "unsafe",
                    "tools": ["terminal"],
                    "skills": ["floating-skill"],
                    "system_prompt": "Do work.",
                }
            ),
        ),
    )
    assert nested.status_code == 422, nested.text
    assert nested.json()["error"]["code"] == "IMPORT_REJECTED"

    definition = _import_agent_definition(client, "reviewer", tools=["terminal"])
    missing_task_tool = client.post(
        "/api/v1/node-assets",
        json={
            "name": "missing native task tool",
            "executor": {},
            "capabilities": [definition],
        },
    )
    assert missing_task_tool.status_code == 422, missing_task_tool.text
    assert missing_task_tool.json()["error"]["code"] == "AGENT_DEFINITION_TASK_TOOL_REQUIRED"

    policy = _import_tool_policy(client, "native-subagents", ["terminal", "task_tool_set"])
    saved = client.post(
        "/api/v1/node-assets",
        json={
            "name": "native subagent node",
            "executor": {},
            "capabilities": [definition, policy],
        },
    )
    assert saved.status_code == 201, saved.text
    by_type = {item["capability_type"]: item for item in saved.json()["capabilities"]}
    assert by_type["AGENT_DEFINITION"]["capability_id"] == definition["capability_id"]
    assert by_type["TOOL_POLICY"]["capability_id"] == policy["capability_id"]


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
    assert error["message"] == "ZIP contains 1001 effective entries; maximum is 1000"
    assert error["details"] == {
        "actual_entries": 1001,
        "max_entries": 1000,
        "ignored_entries": 0,
    }


def test_skill_zip_ignores_macos_metadata_before_effective_entry_limit(client):
    entries: dict[str, bytes | str] = {
        "sample/SKILL.md": "# Sample\n",
        "sample/assets/template.jsx": "export default () => <main />;",
        "sample/assets/template.html": "<main></main>",
        "sample/references/schema.xml": "<schema />",
    }
    entries.update({f"__MACOSX/sample/._metadata-{index}": b"metadata" for index in range(1000)})

    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "macos-skill.zip",
            "content_base64": _zip_content(entries),
        },
    )

    assert response.status_code == 200, response.text
    preview = response.json()["preview"]
    assert preview["file_count"] == 4
    assert preview["raw_entry_count"] == 1004
    assert preview["effective_entry_count"] == 4
    assert preview["ignored_entry_count"] == 1000


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
    skills = [item for item in capabilities if item["capability_type"] == "SKILL"]
    referenced = next(item for item in skills if item["reference_count"] == 1)
    unreferenced = next(item for item in skills if item["reference_count"] == 0)
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
    skills = [item for item in capabilities if item["capability_type"] == "SKILL"]
    assert skills == [{**referenced, "reference_count": 0}]
    deleted = client.request(
        "DELETE",
        "/api/v1/capabilities",
        json={"ids": [referenced["id"]]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted_ids": [referenced["id"]], "blocked": []}
    capabilities = client.get("/api/v1/capabilities").json()
    assert [item for item in capabilities if item["capability_type"] == "SKILL"] == []
    builtin = next(
        item
        for item in capabilities
        if item["capability_type"] == "TOOL_POLICY"
        and item["capability_key"] == "flowweave-default-tools"
    )
    protected = client.request(
        "DELETE",
        "/api/v1/capabilities",
        json={"ids": [builtin["id"]]},
    )
    assert protected.status_code == 200, protected.text
    assert protected.json() == {
        "deleted_ids": [],
        "blocked": [
            {
                "id": builtin["id"],
                "name": "flowweave-default-tools",
                "relation": "BUILTIN_CAPABILITY",
                "nodes": [],
            }
        ],
    }
