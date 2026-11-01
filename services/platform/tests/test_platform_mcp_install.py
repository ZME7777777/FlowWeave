"""Platform-managed MCP versions are installed from one trusted base URL."""

import pytest
from sqlalchemy import func, select

from flowweave.modules.cases.application import native_tools
from flowweave.modules.catalog.public import PlatformMcp, ensure_platform_mcp, validated_url
from flowweave.modules.users.application.security import tenant_user
from flowweave.shared.artifact_store import artifact_store_context
from flowweave.shared.errors import DomainError
from flowweave.shared.models import CapabilityPackage, CapabilityVersion
from flowweave.shared.settings import settings_context

CONTRACTS = (native_tools.CONTEXT, native_tools.FLOWS, native_tools.MATERIALS)


def _mcp(contract):
    return PlatformMcp(
        contract.server_name, contract.path, contract.auth_header, contract.display_name
    )


@pytest.fixture
def install_env(client, db_session_factory, settings, container, monkeypatch):
    owner = client.get("/api/v1/auth/me").json()["id"]
    configured = settings.model_copy(update={"platform_mcp_base_url": "http://platform.test/fw"})
    monkeypatch.setattr(native_tools, "get_settings", lambda: configured)
    with (
        tenant_user(owner),
        settings_context(configured),
        artifact_store_context(container.artifact_store),
        db_session_factory() as db,
    ):
        yield db


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda item: item.server_name)
def test_install_is_idempotent_and_the_version_passes_the_governed_checks(install_env, contract):
    db = install_env
    first = native_tools._published(db, contract=contract)
    again = native_tools._published(db, contract=contract)
    assert first.version.id == again.version.id
    assert first.version.normalized_config_json["url"] == "http://platform.test/fw" + contract.path
    assert first.version.state == "PUBLISHED"
    count = db.scalar(
        select(func.count())
        .select_from(CapabilityVersion)
        .where(CapabilityVersion.package_id == first.package.id)
    )
    assert count == 1


def test_a_changed_base_url_publishes_a_new_version_and_keeps_the_old_one(
    install_env, settings, monkeypatch
):
    db = install_env
    old = native_tools._published(db, contract=native_tools.CONTEXT).version
    moved = settings.model_copy(update={"platform_mcp_base_url": "https://moved.test"})
    monkeypatch.setattr(native_tools, "get_settings", lambda: moved)
    new = native_tools._published(db, contract=native_tools.CONTEXT).version
    assert new.id != old.id and new.version_no == old.version_no + 1
    assert old.state == "PUBLISHED"
    assert old.normalized_config_json["url"] == "http://platform.test/fw" + native_tools.MCP_PATH
    # A frozen reference to the old version keeps resolving to that exact URL.
    assert (
        native_tools.frozen_endpoint(db, old.id, contract=native_tools.CONTEXT)
        == (old.normalized_config_json["url"])
    )


def test_installed_servers_are_builtin_and_owned_by_the_platform(install_env):
    from flowweave.modules.catalog.application.capability_repository import is_builtin_capability

    db = install_env
    for contract in CONTRACTS:
        published = native_tools._published(db, contract=contract)
        assert is_builtin_capability("MCP", published.package.capability_key)
    packages = db.scalars(
        select(CapabilityPackage).where(
            CapabilityPackage.capability_key.in_([item.server_name for item in CONTRACTS])
        )
    ).all()
    assert len(packages) == 3


@pytest.mark.parametrize(
    "base",
    [
        "ftp://platform.test",
        "http://user:secret@platform.test",
        "http://platform.test?token=x",
        "http://platform.test/#frag",
        "platform.test",
        "",
    ],
)
def test_unsafe_or_missing_base_urls_are_refused(install_env, settings, monkeypatch, base):
    monkeypatch.setattr(
        native_tools,
        "get_settings",
        lambda: settings.model_copy(update={"platform_mcp_base_url": base}),
    )
    with pytest.raises(DomainError) as refused:
        native_tools._published(install_env, contract=native_tools.CONTEXT)
    assert refused.value.code == "CASE_TOOL_CAPABILITY_UNAVAILABLE"


def test_only_platform_managed_servers_can_be_installed(install_env):
    with pytest.raises(DomainError) as refused:
        ensure_platform_mcp(
            install_env,
            PlatformMcp("user-server", "/x", "Bearer ${X}", "User"),
            "http://platform.test/x",
        )
    assert refused.value.code == "PLATFORM_MCP_NOT_MANAGED"


def test_validated_url_joins_prefix_and_path_without_double_slashes():
    assert (
        validated_url("http://h:3000/flowweave/", "/api/v1/p") == "http://h:3000/flowweave/api/v1/p"
    )
    assert validated_url("http://h", "/api/v1/p") == "http://h/api/v1/p"


def test_startup_installation_is_idempotent_and_skipped_without_a_base_url(
    install_env, settings, monkeypatch
):
    db = install_env
    first = native_tools.ensure_installed(db)
    assert len(first) == 3 and len(set(first)) == 3
    assert native_tools.ensure_installed(db) == first
    monkeypatch.setattr(
        native_tools,
        "get_settings",
        lambda: settings.model_copy(update={"platform_mcp_base_url": ""}),
    )
    assert native_tools.ensure_installed(db) == []
