from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).parents[2] / "src" / "flowweave"
REPOSITORY = Path(__file__).parents[4]
FORBIDDEN_DOMAIN_ROOTS = {"fastapi", "pydantic", "sqlalchemy", "httpx"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_domain_code_is_framework_free() -> None:
    violations: list[str] = []
    domain_files = list(SOURCE.glob("modules/*/domain/**/*.py")) + list(
        (SOURCE / "shared" / "domain").glob("**/*.py")
    )
    for path in domain_files:
        for imported in _imports(path):
            if imported.split(".")[0] in FORBIDDEN_DOMAIN_ROOTS:
                violations.append(f"{path.relative_to(SOURCE)} -> {imported}")
            if imported.startswith("flowweave.shared.models"):
                violations.append(f"{path.relative_to(SOURCE)} -> {imported}")
    assert not violations, "Domain framework imports:\n" + "\n".join(violations)


def test_bootstrap_is_factory_only() -> None:
    api_source = (SOURCE / "bootstrap" / "api.py").read_text()
    assert "app = create_app()" not in api_source
    database_source = (SOURCE / "shared" / "database.py").read_text()
    assert "create_engine(Settings()" not in database_source
    assert "SessionLocal" not in database_source


def test_external_container_images_are_immutable() -> None:
    violations: list[str] = []
    dockerfiles = (
        REPOSITORY / "services" / "platform" / "Dockerfile",
        REPOSITORY / "apps" / "web" / "Dockerfile",
        REPOSITORY / "infra" / "dependency-builder" / "Dockerfile",
        REPOSITORY / "infra" / "openhands" / "Dockerfile",
        REPOSITORY / "infra" / "sandbox" / "python" / "Dockerfile",
        REPOSITORY / "infra" / "sandbox" / "javascript" / "Dockerfile",
    )
    for path in dockerfiles:
        aliases: set[str] = set()
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            tokens = line.strip().split()
            if not tokens or tokens[0].upper() != "FROM":
                continue
            reference = next((token for token in tokens[1:] if not token.startswith("--")), "")
            if reference not in aliases and "@sha256:" not in reference:
                violations.append(f"{path.relative_to(REPOSITORY)}:{line_number} -> {reference}")
            upper_tokens = [token.upper() for token in tokens]
            if "AS" in upper_tokens:
                alias_index = upper_tokens.index("AS") + 1
                if alias_index < len(tokens):
                    aliases.add(tokens[alias_index])

    compose = (REPOSITORY / "infra" / "compose.yaml").read_text()
    for reference in (
        "alpine:3.22",
        "postgres:16.9-alpine3.21",
    ):
        if f"image: {reference}@sha256:" not in compose:
            violations.append(f"infra/compose.yaml -> {reference}")

    postgres_digest = (
        "postgres:16.9-alpine3.21"
        "@sha256:36e8aabaa6fa6037537cff64011fa45a200fe2ba202141b9aca48cff3df7ad42"
    )
    for relative in (
        "services/platform/tests/conftest.py",
        "services/platform/scripts/migration_check.py",
    ):
        if postgres_digest not in (REPOSITORY / relative).read_text().replace(
            '"\n            "', ""
        ):
            violations.append(f"{relative} -> postgres test image")

    assert not violations, "Mutable external container images:\n" + "\n".join(violations)


def test_openhands_runtime_uses_digest_locked_source_build() -> None:
    """The runtime must use verified source, never a floating local checkout."""

    dockerfile = (REPOSITORY / "infra" / "openhands" / "Dockerfile").read_text()
    compose = (REPOSITORY / "infra" / "compose.yaml").read_text()
    makefile = (REPOSITORY / "Makefile").read_text()
    project = (REPOSITORY / "infra" / "openhands" / "pyproject.toml").read_text()
    lockfile = (REPOSITORY / "infra" / "openhands" / "uv.lock").read_text()
    source_lock = json.loads((REPOSITORY / "infra" / "openhands" / "source.lock.json").read_text())

    assert "OPENHANDS_SDK_SOURCE" not in compose
    assert "OPENHANDS_SDK_SOURCE" not in makefile
    assert "--build-context openhands_sdk" not in makefile
    assert "COPY --from=openhands_sdk" not in dockerfile
    assert source_lock["source_kind"] == "upstream_source"
    assert source_lock["upstream_base_commit"] == ("9a24f6c8866f353042a57df0514ccc900e3a0691")
    assert source_lock["source_commit"] == source_lock["upstream_base_commit"]
    assert source_lock["fork_commit"] is None
    assert len(source_lock["source_commit"]) == 40
    assert len(source_lock["archive_sha256"]) == 64
    assert "fetch_openhands_source.py" in dockerfile
    assert "--lock /runtime/openhands-source.lock.json" in dockerfile
    assert "--destination /opt/openhands-source" in dockerfile
    assert "--overlay /runtime/patch_fork_condenser.py" in dockerfile
    assert "patch_fork_condenser.py /opt/openhands-source" in dockerfile
    assert "/opt/openhands-source/openhands-sdk" in dockerfile
    assert "/opt/openhands-source/openhands-agent-server" in dockerfile
    assert "expected='1.44.0'" in dockerfile
    for package in (
        "openhands-agent-server",
        "openhands-sdk",
        "openhands-tools",
        "openhands-workspace",
    ):
        assert f'"{package}==1.44.0"' in project
        assert f'name = "{package}"' in lockfile


def test_openhands_image_runs_installed_contract_probe() -> None:
    dockerfile = (REPOSITORY / "infra" / "openhands" / "Dockerfile").read_text()
    makefile = (REPOSITORY / "Makefile").read_text()
    probe = REPOSITORY / "infra" / "openhands" / "contract_check.py"

    assert probe.is_file()
    assert "COPY infra/openhands/contract_check.py /runtime/contract_check.py" in dockerfile
    assert "RUN /runtime/.venv/bin/python /runtime/contract_check.py" in dockerfile
    assert "openhands-contract-check:" in makefile
    assert "openhands-image-provenance:" in makefile
    assert "docker image inspect" in makefile
    assert "/runtime/contract_check.py" in makefile


def test_openhands_acp_providers_are_explicit_allowlisted_build_inputs() -> None:
    """FR-77: the default image is ACP-free; opt-in packages stay exact."""

    dockerfile = (REPOSITORY / "infra" / "openhands" / "Dockerfile").read_text()
    assert 'ARG INSTALL_ACP_PROVIDERS=""' in dockerfile
    assert "for provider in $(echo \"$INSTALL_ACP_PROVIDERS\" | tr ',' ' ')" in dockerfile
    assert "Unknown ACP provider '$provider'" in dockerfile
    assert "@agentclientprotocol/claude-agent-acp@0.63.0" in dockerfile
    assert "@agentclientprotocol/codex-acp@1.1.7" in dockerfile
    assert "@google/gemini-cli@0.46.0" in dockerfile
    assert "INSTALL_ACP_PROVIDERS is empty; no ACP providers will be installed" in dockerfile


def test_flowrun_runtime_has_no_shared_agent_server_or_legacy_launch_fallback() -> None:
    """FR-08: every Conversation must route through its FlowRun generation."""

    compose = (REPOSITORY / "infra" / "compose.yaml").read_text()
    settings = (SOURCE / "bootstrap" / "settings.py").read_text()
    runtime = (SOURCE / "runtime" / "openhands.py").read_text()
    docker_provider = (
        SOURCE / "modules" / "sandboxes" / "infrastructure" / "docker.py"
    ).read_text()
    sandbox_service = (SOURCE / "modules" / "sandboxes" / "application" / "service.py").read_text()
    runtime_create = sandbox_service.split("def _create_managed_runtime(", 1)[1].split(
        "def ensure_flow_run_runtime(", 1
    )[0]

    for forbidden in (
        "OPENHANDS_BASE_URL",
        "openhands-agent-server:",
        "flowweave-openhands-agent-server",
        "openhands-state:",
        "WORKSPACE_SOURCE_CONTAINER",
    ):
        assert forbidden not in compose
    assert "openhands_base_url" not in settings
    assert "terminal_environment_workspace_source_container" not in settings
    assert "self.base_url" not in runtime
    assert "or self.base_url" not in runtime
    assert "RUNTIME_ROUTE_REQUIRED" in runtime
    assert "terminal_environment_workspace_source_container" not in docker_provider
    assert "flowweave.workspace-source" not in docker_provider
    assert "/runtime/workspace:rw" not in docker_provider
    assert '"ATTEMPT"' not in runtime_create
    assert '"CONVERSATION"' not in runtime_create


def test_flowrun_conversation_model_has_no_platform_message_or_state_truth() -> None:
    """FR-09: active code keeps only locators and independent approvals."""

    models = (SOURCE / "modules" / "conversations" / "infrastructure" / "models.py").read_text()
    service = (SOURCE / "modules" / "conversations" / "application" / "service.py").read_text()
    orchestration = (
        SOURCE / "modules" / "orchestration" / "application" / "service.py"
    ).read_text()
    worker_handlers = (SOURCE / "modules" / "tasks" / "application" / "handlers.py").read_text()
    for forbidden in (
        "class AgentConversation",
        "class AgentMessage",
        "ConversationKind",
        "ConversationState",
        "HUMAN_CREATED",
        "runtime_cursor:",
    ):
        assert forbidden not in models
        assert forbidden not in service
        assert forbidden not in orchestration
    for task_type in (
        "CREATE_CONVERSATION",
        "DELIVER_CONVERSATION_MESSAGE",
        "POLL_CONVERSATION",
        "WAIT_CONVERSATION_WAKEUP",
    ):
        assert task_type not in worker_handlers
    assert "class FlowRunConversationBinding" in models
    assert "class RuntimeConfirmationApproval" in models


def test_runtime_request_has_one_structured_agent_spec_boundary() -> None:
    tree = ast.parse((SOURCE / "runtime" / "base.py").read_text())
    request = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "StartAttemptRequest"
    )
    fields = {
        node.target.id
        for node in request.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "agent_spec" in fields
    assert not fields.intersection(
        {
            "provider",
            "tools",
            "skills",
            "mcp_servers",
            "hook_config",
            "confirmation_policy",
            "condenser",
            "condenser_provider",
            "budgets",
        }
    )


def test_openhands_adapter_does_not_define_default_tools() -> None:
    tree = ast.parse((SOURCE / "runtime" / "openhands.py").read_text())
    create = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_create"
    )
    string_constants = {
        node.value
        for node in ast.walk(create)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not string_constants.intersection({"terminal", "file_editor", "task_tracker"})


def test_openhands_adapter_never_resolves_mutable_agent_profile_store() -> None:
    """Production starts must materialize the frozen Profile as explicit Agent JSON."""

    source = (SOURCE / "runtime" / "openhands.py").read_text()
    assert 'payload["agent_profile_id"]' not in source
    assert 'payload["agent_settings"]' not in source
    assert '"agent": agent' in source


def test_native_plugin_and_memory_loading_has_no_openhands_source_patch() -> None:
    """FR-10: keep upstream ambient Plugins and use the official Memory loader."""

    dockerfile = (REPOSITORY / "infra" / "openhands" / "Dockerfile").read_text()
    contract_probe = (REPOSITORY / "infra" / "openhands" / "contract_check.py").read_text()
    runtime = (SOURCE / "runtime" / "openhands.py").read_text()
    workspace = (SOURCE / "runtime" / "workspace.py").read_text()
    assert "patch_ambient_plugins" not in dockerfile
    assert "load_ambient_plugins" not in contract_probe
    assert '"load_ambient_plugins"' not in runtime
    assert 'loader_root = working_dir / ".openhands" / "memory"' in workspace
    assert "openhands_flow_run_capability_path(" in workspace


def test_openhands_144_stream_and_builtin_tool_contracts_are_behavioral() -> None:
    """FR-75: keep upstream behavior probes, not registry compatibility maps."""

    contract_probe = (REPOSITORY / "infra" / "openhands" / "contract_check.py").read_text()
    runtime = (SOURCE / "runtime" / "openhands.py").read_text()
    assert "get_tool_module_qualnames" not in contract_probe
    assert "EXPECTED_TOOL_MODULES" not in contract_probe
    assert "tool_module_qualnames" not in contract_probe
    assert '"streaming_delta_delivery_is_subscriber_scoped": True' in contract_probe
    assert '"remote_structured_builtin_resolution": True' in contract_probe
    assert '"tool_module_qualnames"' not in runtime


def test_openhands_144_profile_secret_condenser_and_title_boundaries() -> None:
    """FR-76: use upstream lifecycle behavior without dropping product-owned title CAS."""

    contract_probe = (REPOSITORY / "infra" / "openhands" / "contract_check.py").read_text()
    runtime = (SOURCE / "runtime" / "openhands.py").read_text()
    conversations = (
        SOURCE / "modules" / "agent_workspaces" / "application" / "conversations.py"
    ).read_text()
    titles = (SOURCE / "modules" / "agent_workspaces" / "application" / "titles.py").read_text()
    assert "agent_profile_fields ==" not in contract_probe
    assert "expected_condenser_defaults" not in contract_probe
    assert '"profile_v1_migration": True' in contract_probe
    assert '"provider_connection_read_at_use": True' in contract_probe
    assert '"nested_secret_serializer_probe": True' in contract_probe
    assert '"subscription_condenser_dispatch": True' in contract_probe
    assert '"remote_title_generation_fix_in_frozen_source": False' in contract_probe
    assert 'payload["autotitle"] = False' in runtime
    assert "_enqueue_title_task" in conversations
    assert "AgentConversationBinding.title_generation == generation" in titles


def test_agent_workspace_uses_the_single_agent_session_workbench_and_facade() -> None:
    """FR-94: `/agent` is the canonical session product, not a copied surface."""

    web_root = REPOSITORY / "apps" / "web" / "src"
    app = (web_root / "App.tsx").read_text()
    route_host = (web_root / "pages" / "AgentWorkbenchPage.tsx").read_text()
    workbench = (
        web_root / "components" / "agent-session" / "AgentSessionWorkbench.tsx"
    ).read_text()
    gateway = (web_root / "api" / "agent-session-gateway.ts").read_text()
    legacy_facade = (
        SOURCE / "modules" / "agent_workspaces" / "application" / "conversations.py"
    ).read_text()
    public_facade = (SOURCE / "modules" / "agent_sessions" / "public.py").read_text()

    assert "import { AgentWorkbenchPage }" in app
    # The legacy FlowRun page remains temporarily reachable only through its
    # own view.  It must never become the `/agent` route again before that
    # host is migrated to the shared workbench in a later slice.
    assert "isAgentRoute ? <AgentWorkbenchPage onNavigate={navigate}/>" in app
    assert "return <AgentSessionWorkbench {...props}/>;" in route_host
    assert "export function AgentSessionWorkbench" in workbench
    assert "agentWorkspaceSessionGateway" in gateway
    assert "from flowweave.modules.agent_sessions.application import conversations" in legacy_facade
    assert '"conversations"' in public_facade


def test_agent_workspace_conversation_compatibility_import_is_the_shared_module() -> None:
    """Do not let the historical Agent Workspace path grow a second service."""

    from flowweave.modules.agent_sessions.application import conversations as shared
    from flowweave.modules.agent_workspaces.application import conversations as legacy

    assert legacy is shared


def test_agent_session_host_contract_is_explicit_and_namespaced() -> None:
    """FR-95: shared UI accepts a host, never an Agent Workspace-shaped API."""

    web_root = REPOSITORY / "apps" / "web" / "src"
    gateway = (web_root / "api" / "agent-session-gateway.ts").read_text()
    host = (web_root / "components" / "agent-session" / "session-host.ts").read_text()
    workbench = (
        web_root / "components" / "agent-session" / "AgentSessionWorkbench.tsx"
    ).read_text()
    default_adapter = (
        SOURCE
        / "modules"
        / "agent_workspaces"
        / "application"
        / "session_host.py"
    ).read_text()

    assert "export interface AgentSessionApi" in gateway
    assert "typeof api." not in gateway
    assert "typeof agentWorkspace" not in gateway
    assert "queryKey(resource:" in host
    assert "workspaceToolsStorageKey(" in host
    assert "sessionQueryKey(host," in workbench
    assert "['agent-" not in workbench
    assert "AgentWorkspaceCapability" not in workbench
    assert "from flowweave.modules.agent_sessions.application.host import" in default_adapter
    assert "AgentSessionHostContext.create(" in default_adapter


def test_execution_and_conversation_share_runtime_manifest_projection() -> None:
    orchestration = (
        SOURCE / "modules" / "orchestration" / "application" / "service.py"
    ).read_text()
    conversations = (
        SOURCE / "modules" / "conversations" / "application" / "service.py"
    ).read_text()
    assert "from flowweave.runtime.manifest import" in orchestration
    assert "from flowweave.runtime.manifest import runtime_node" in conversations
    assert "node = runtime_node(" in conversations


def test_flowrun_api_and_web_do_not_restore_legacy_conversation_or_endpoint_truth() -> None:
    """FR-11: clients carry FlowRun locators and see only logical Runtime health."""

    router = (SOURCE / "modules" / "conversations" / "presentation" / "router.py").read_text()
    operations = (
        SOURCE / "modules" / "sandboxes" / "application" / "runtime_operations.py"
    ).read_text()
    web_root = REPOSITORY / "apps" / "web" / "src"
    web = "\n".join(
        (web_root / path).read_text()
        for path in (
            "api/client.ts",
            "pages/AgentChatPage.tsx",
            "components/AgentRuntimeSidebar.tsx",
            "components/RuntimeGovernancePanel.tsx",
        )
    )
    assert '"/flow-runs/{flow_run_id}/conversations"' in router
    assert '"/agent-conversations/' not in router
    assert '"/node-attempts/{attempt_id}/conversations"' not in router
    for forbidden in (
        "agent-conversations",
        "agent-messages",
        "HUMAN_CREATED",
        "AUTO",
        "runtime_cursor",
        "container_name",
    ):
        assert forbidden not in web
    for forbidden in ("backend_resource_name", "managed_runtime_id", "endpoint", "base_url"):
        assert forbidden not in operations
    assert '"physical_delete_operation": "DELETE_FLOW_RUN"' in operations


def test_bootstrap_entrypoints_import_in_clean_processes() -> None:
    """Catch import-order bugs hidden by pytest's already-populated module cache."""

    package_root = SOURCE.parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(package_root),
    }
    for module in (
        "flowweave.bootstrap.api",
        "flowweave.bootstrap.worker",
        "flowweave.bootstrap.runtime_provider",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
        assert completed.returncode == 0, (
            f"clean import failed for {module}: {(completed.stderr or completed.stdout)[-4000:]}"
        )


def test_production_package_contains_no_sqlite_compatibility() -> None:
    violations = []
    for path in SOURCE.rglob("*.py"):
        source = path.read_text().lower()
        if "sqlite" in source:
            violations.append(str(path.relative_to(SOURCE)))
    assert not violations, "SQLite compatibility remains: " + ", ".join(violations)


def test_modules_expose_public_facades() -> None:
    modules = [
        path
        for path in (SOURCE / "modules").iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    ]
    missing = [path.name for path in modules if not (path / "public.py").is_file()]
    assert not missing, f"Missing public.py facades: {missing}"


def test_api_presentation_uses_async_uow_only() -> None:
    violations: list[str] = []
    for path in SOURCE.glob("modules/*/presentation/**/*.py"):
        source = path.read_text()
        if "sync_sessions" in source:
            violations.append(f"{path.relative_to(SOURCE)} uses sync_sessions")
        if "flowweave.shared.http" in source and "run_sync" not in source:
            violations.append(f"{path.relative_to(SOURCE)} bypasses async run_sync")
    http_source = (SOURCE / "shared" / "http.py").read_text()
    assert "AsyncSession" in http_source
    assert "container.database.uow()" in http_source
    assert "sync_sessions" not in http_source
    runs_router = (SOURCE / "modules" / "runs" / "presentation" / "router.py").read_text()
    assert "container.database.session()" in runs_router
    assert not violations, "Synchronous API transaction paths:\n" + "\n".join(violations)


def test_cross_module_dependencies_use_public_facades() -> None:
    violations: list[str] = []
    modules_root = SOURCE / "modules"
    for path in modules_root.glob("*/**/*.py"):
        relative = path.relative_to(modules_root)
        owner = relative.parts[0]
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            prefix = "flowweave.modules."
            if not node.module.startswith(prefix):
                continue
            parts = node.module[len(prefix) :].split(".")
            target = parts[0]
            if target == owner:
                continue
            imports_public_module = len(parts) == 1 and any(
                alias.name == "public" for alias in node.names
            )
            imports_from_public = len(parts) == 2 and parts[1] == "public"
            if not (imports_public_module or imports_from_public):
                violations.append(f"{path.relative_to(SOURCE)} -> {node.module}")
    assert not violations, "Cross-module internal imports:\n" + "\n".join(violations)


def test_orm_mappings_are_owned_by_module_infrastructure() -> None:
    violations: list[str] = []
    allowed = {path.resolve() for path in SOURCE.glob("modules/*/infrastructure/models.py")}
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            declares_mapping = any(
                isinstance(base, ast.Name)
                and base.id == "Base"
                or isinstance(base, ast.Attribute)
                and base.attr == "Base"
                for base in node.bases
            )
            if declares_mapping and path.resolve() not in allowed:
                violations.append(f"{path.relative_to(SOURCE)}:{node.name}")

    shared_models = ast.parse(
        (SOURCE / "shared" / "models.py").read_text(),
        filename=str(SOURCE / "shared" / "models.py"),
    )
    assert not any(isinstance(node, ast.ClassDef) for node in shared_models.body)
    assert not violations, "ORM mappings outside module infrastructure:\n" + "\n".join(violations)
