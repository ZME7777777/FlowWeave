import base64
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from flowweave.modules.catalog.application.capability_repository import (
    publish_dependency_build,
    resolve_version,
)
from flowweave.shared.models import (
    AgentConversationBinding,
    AgentConversationCapability,
    CapabilityBlob,
    CapabilityImport,
    CapabilityVersion,
)


def skill_zip() -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sample/SKILL.md", "# Sample\n")
        archive.writestr("sample/reference.md", "evidence")
    return base64.b64encode(buffer.getvalue()).decode()


def test_context_import_is_frozen_as_utf8_text_and_rejects_plaintext_secrets(client):
    content = "# 产品背景\n\n回答前先核对需求、约束和验收标准。\n".encode()
    validated = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "CONTEXT",
            "filename": "product-background.md",
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    assert validated.status_code == 200, validated.text
    committed = client.post(
        "/api/v1/capability-imports",
        json={"import_token": validated.json()["import_token"]},
    )
    assert committed.status_code == 201, committed.text
    capability = committed.json()["capabilities"][0]
    assert capability["capability_type"] == "CONTEXT"
    assert capability["capability_key"] == "product-background"
    assert capability["normalized_config"]["text"] == content.decode().strip()

    rejected = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "CONTEXT",
            "filename": "unsafe.txt",
            "content_base64": base64.b64encode(b"api_key = super-secret").decode(),
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "IMPORT_REJECTED"


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


def test_identical_import_reuses_immutable_blob_and_version(client, db_session_factory):
    payload = {
        "capability_type": "SKILL",
        "filename": "sample.zip",
        "content_base64": skill_zip(),
    }
    first_validation = client.post("/api/v1/capability-imports/validate", json=payload)
    assert first_validation.status_code == 200, first_validation.text
    first = client.post(
        "/api/v1/capability-imports",
        json={"import_token": first_validation.json()["import_token"]},
    )
    assert first.status_code == 201, first.text

    second_validation = client.post("/api/v1/capability-imports/validate", json=payload)
    assert second_validation.status_code == 200, second_validation.text
    second = client.post(
        "/api/v1/capability-imports",
        json={"import_token": second_validation.json()["import_token"]},
    )
    assert second.status_code == 201, second.text
    assert (
        second.json()["capabilities"][0]["capability_id"]
        == first.json()["capabilities"][0]["capability_id"]
    )
    assert second.json()["storage_key"] == first.json()["storage_key"]

    with db_session_factory() as db:
        assert len(list(db.scalars(select(CapabilityBlob)))) == 1
        assert len(list(db.scalars(select(CapabilityVersion)))) == 1
        imports = list(db.scalars(select(CapabilityImport).order_by(CapabilityImport.created_at)))
        assert len(imports) == 2
        assert imports[0].storage_key == imports[1].storage_key


def test_identical_import_after_physical_delete_publishes_new_version(client, db_session_factory):
    payload = {
        "capability_type": "SKILL",
        "filename": "sample.zip",
        "content_base64": skill_zip(),
    }
    first_validation = client.post("/api/v1/capability-imports/validate", json=payload)
    assert first_validation.status_code == 200, first_validation.text
    first = client.post(
        "/api/v1/capability-imports",
        json={"import_token": first_validation.json()["import_token"]},
    )
    assert first.status_code == 201, first.text
    version_id = first.json()["capabilities"][0]["capability_id"]

    deleted = client.delete(f"/api/v1/capabilities/{version_id}")
    assert deleted.status_code == 204, deleted.text
    with db_session_factory() as db:
        assert db.get(CapabilityVersion, version_id) is None
    assert all(item["id"] != version_id for item in client.get("/api/v1/capabilities").json())

    second_validation = client.post("/api/v1/capability-imports/validate", json=payload)
    assert second_validation.status_code == 200, second_validation.text
    second = client.post(
        "/api/v1/capability-imports",
        json={"import_token": second_validation.json()["import_token"]},
    )
    assert second.status_code == 201, second.text
    assert second.json()["capabilities"][0]["capability_id"] != version_id

    listed = client.get("/api/v1/capabilities").json()
    assert version_id not in {item["id"] for item in listed}
    with db_session_factory() as db:
        assert db.get(CapabilityVersion, version_id) is None
        assert len(list(db.scalars(select(CapabilityVersion)))) == 1


def test_deleted_agent_conversation_does_not_block_capability_deletion(
    client, db_session_factory, skill_capability
):
    capability_id = skill_capability["capability_id"]
    binding_id = str(uuid4())
    with db_session_factory() as db:
        capability = db.get(CapabilityVersion, capability_id)
        assert capability is not None
        db.add(
            AgentConversationBinding(
                id=binding_id,
                runtime_session_id=str(uuid4()),
                host_kind="AGENT_WORKSPACE",
                host_id=str(uuid4()),
                conversation_scope_id=str(uuid4()),
                openhands_conversation_id=str(uuid4()),
                display_title="已删除的会话",
                lifecycle="DELETED",
                create_idempotency_key=f"deleted-conversation:{binding_id}",
            )
        )
        db.add(
            AgentConversationCapability(
                binding_id=binding_id,
                capability_version_id=capability_id,
                capability_type="SKILL",
                capability_key="test-skill",
                digest=capability.digest,
                position=0,
            )
        )
        db.commit()

    deleted = client.delete(f"/api/v1/capabilities/{capability_id}")
    assert deleted.status_code == 204, deleted.text

    with db_session_factory() as db:
        assert db.get(CapabilityVersion, capability_id) is None
        assert db.get(AgentConversationBinding, binding_id) is not None
        assert (
            list(
                db.scalars(
                    select(AgentConversationCapability).where(
                        AgentConversationCapability.binding_id == binding_id
                    )
                )
            )
            == []
        )


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


def test_skill_import_normalizes_adjacent_codex_metadata_for_openhands(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "codex-skills.zip",
            "content_base64": _zip_content(
                {
                    "review/SKILL.md": "---\nname: review\n---\n# Review\n",
                    "review/agents/openai.yaml": (
                        "interface:\n"
                        '  display_name: "Code Review"\n'
                        '  short_description: "Find actionable defects"\n'
                        '  default_prompt: "Use $review to review this change."\n'
                        '  icon_small: "./assets/review-small.svg"\n'
                        '  icon_large: "./assets/review.png"\n'
                        '  brand_color: "#107C41"\n'
                        "policy:\n"
                        "  allow_implicit_invocation: false\n"
                    ),
                    "document/SKILL.md": (
                        "---\nname: document\ndescription: Canonical AgentSkills description\n"
                        "---\n# Document\n"
                    ),
                    "document/agents/openai.yaml": (
                        "interface:\n  short_description: Codex fallback must not override\n"
                    ),
                }
            ),
        },
    )
    assert response.status_code == 200, response.text
    capabilities = {
        item["capability_key"]: item["normalized_config"]
        for item in response.json()["preview"]["capabilities"]
    }
    assert capabilities["review"] == {
        "entry": "review/SKILL.md",
        "description": "Find actionable defects",
        "version": "",
        "codex_metadata": {
            "source": "review/agents/openai.yaml",
            "interface": {
                "display_name": "Code Review",
                "short_description": "Find actionable defects",
                "default_prompt": "Use $review to review this change.",
                "icon_small": "./assets/review-small.svg",
                "icon_large": "./assets/review.png",
                "brand_color": "#107C41",
            },
            "policy": {"allow_implicit_invocation": False},
        },
        "disable_model_invocation": True,
        "dependencies": {},
        "dependency_build_state": "NOT_REQUIRED",
    }
    assert capabilities["document"]["description"] == ("Canonical AgentSkills description")
    assert capabilities["document"]["codex_metadata"]["source"] == ("document/agents/openai.yaml")
    assert "disable_model_invocation" not in capabilities["document"]


def test_skill_import_rejects_invalid_codex_metadata(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json={
            "capability_type": "SKILL",
            "filename": "invalid-codex-skill.zip",
            "content_base64": _zip_content(
                {
                    "review/SKILL.md": "# Review\n",
                    "review/agents/openai.yaml": (
                        "policy:\n  allow_implicit_invocation: sometimes\n"
                    ),
                }
            ),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IMPORT_REJECTED"


def test_dependency_build_publishes_derived_version(client, db_session_factory):
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
        assert source is not None and derived is not None
        assert source.normalized_config_json["dependency_build_state"] == "PENDING"
        assert derived.normalized_config_json["dependency_build_state"] == "READY"
        assert derived.package_id == source.package_id
        assert derived.blob_id == source.blob_id
        assert derived.version_no == source.version_no + 1


def test_skill_zip_imports_multiple_skills_and_mcp(client):
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
    assert [item["capability_key"] for item in capabilities] == [
        "requirements-analysis",
        "technical-design",
        "local-review",
    ]


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
        (
            "remote-with-local-args",
            {"transport": "http", "url": "https://mcp.test/mcp", "args": ["--readonly"]},
        ),
        (
            "remote-with-local-env",
            {"transport": "sse", "url": "https://mcp.test/sse", "env": {"MODE": "readonly"}},
        ),
        (
            "stdio-with-remote-headers",
            {"transport": "stdio", "command": "python", "headers": {"X-Test": "value"}},
        ),
        (
            "stdio-with-remote-auth",
            {"transport": "stdio", "command": "python", "auth": {"strategy": "none"}},
        ),
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


def test_stdio_mcp_persists_multiple_scripts(client):
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


def test_hook_config_normalizes_form_json(client):
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
                "hook_set_schema_version": 1,
                "openhands_version": "1.44.0",
                "source_commit": "9a24f6c8866f353042a57df0514ccc900e3a0691",
                "allowed_events": [
                    "post_tool_use",
                    "pre_tool_use",
                    "session_end",
                    "session_start",
                    "stop",
                    "user_prompt_submit",
                ],
                "runtime_mutation": "FORBIDDEN",
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
    hook = committed.json()["capabilities"][0]
    assert hook["capability_type"] == "HOOK"
    assert "pre_tool_use" in hook["normalized_config"]


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


def test_editing_one_skill_saves_in_place(client):
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


def test_node_asset_rejects_agent_capability_configuration(client):
    request = client.post(
        "/api/v1/node-assets",
        json={
            "name": "Agent configuration is not a node property",
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
    assert request.status_code == 422
    assert request.json()["error"]["code"] == "INVALID_COMMAND"


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


def test_plugin_import_freezes_manifest_contributions(client):
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


def test_tool_policy_is_not_an_importable_or_node_capability(client):
    response = client.post(
        "/api/v1/capability-imports/validate",
        json=_validate_payload(
            "TOOL_POLICY",
            "retired-tool-policy.json",
            '{"name":"retired","tools":[{"name":"terminal","params":{}}]}',
        ),
    )
    assert response.status_code == 422

    node = client.post(
        "/api/v1/node-assets",
        json={"name": "No Agent configuration", "executor": {}},
    )
    assert node.status_code == 201, node.text
    assert "capabilities" not in node.json()


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


def test_capability_bulk_delete_remains_independent_from_nodes(client, skill_capability):
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
    assert all(item["reference_count"] == 0 for item in skills)
    result = client.request(
        "DELETE",
        "/api/v1/capabilities",
        json={"ids": [item["id"] for item in skills]},
    )
    assert result.status_code == 200, result.text
    assert set(result.json()["deleted_ids"]) == {item["id"] for item in skills}
    assert result.json()["blocked"] == []
    capabilities = client.get("/api/v1/capabilities").json()
    assert [item for item in capabilities if item["capability_type"] == "SKILL"] == []
