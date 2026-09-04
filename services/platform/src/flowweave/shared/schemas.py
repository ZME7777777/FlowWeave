from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


def _empty_any_dict() -> dict[str, Any]:
    return {}


def _empty_str_dict() -> dict[str, str]:
    return {}


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


def _http_url(value: str, label: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label} must be an HTTP(S) URL without credentials")
    return value.strip()


class DirectoryWrite(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    parent_id: str | None = None
    position: int = Field(default=0, ge=0)


class IOFieldWrite(ApiModel):
    field_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    display_name: str = Field(default="", max_length=160)
    data_type: Literal["URL", "FILE"] = "URL"
    description: str = ""
    # Kept only to accept older saved clients during rollout. It is ignored and
    # never persisted, projected, or passed to the Runtime.
    template_url: str = Field(default="", exclude=True)


def _empty_io_fields() -> list[IOFieldWrite]:
    return []


class ExecutorWrite(ApiModel):
    startup_prompt: str = ""
    context_prompt: str = ""
    context_capability_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_context_capabilities(self) -> ExecutorWrite:
        if len(self.context_capability_ids) != len(set(self.context_capability_ids)):
            raise ValueError("Context capability versions must be unique")
        return self


class NodeAssetWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    directory_id: str | None = None
    description: str = ""
    icon_kind: str = "LUCIDE"
    icon_value: str = "bot"
    row_version: int | None = None
    inputs: list[IOFieldWrite] = Field(default_factory=_empty_io_fields)
    outputs: list[IOFieldWrite] = Field(default_factory=_empty_io_fields)
    executor: ExecutorWrite = Field(default_factory=ExecutorWrite)


class TerminalEnvironmentWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    row_version: int | None = None


class WebsiteCredentialWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    target_host: str = Field(min_length=1, max_length=253)
    include_subdomains: bool = False
    auth_type: Literal["USERNAME_PASSWORD", "BEARER_TOKEN"] = "USERNAME_PASSWORD"
    username: str | None = Field(default=None, max_length=320)
    secret: SecretStr | None = Field(default=None, max_length=4096)
    row_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def normalize_target_host(self) -> WebsiteCredentialWrite:
        value = self.target_host.strip().rstrip(".").lower()
        if (
            not value
            or "/" in value
            or ":" in value
            or "@" in value
            or not all(
                part and len(part) <= 63 and part.replace("-", "").isalnum()
                for part in value.split(".")
            )
        ):
            raise ValueError("target_host must be a DNS host without scheme, path, or port")
        self.target_host = value
        self.name = self.name.strip()
        if self.username is not None:
            self.username = self.username.strip() or None
        return self


class CredentialBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class EnvironmentPublishWrite(ApiModel):
    description: str = Field(default="", max_length=2000)


class EnvironmentSetupWrite(ApiModel):
    base_version_id: str | None = None


class NodeAssetBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class NodeDirectoryBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class CapabilityBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class CapabilitySkillRevisionWrite(ApiModel):
    content: str = Field(min_length=1, max_length=1_048_576)


class AgentProfileRevisionWrite(ApiModel):
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: dict[str, Any]


class AgentProfileCopyWrite(ApiModel):
    capability_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class AgentProfileRetireWrite(ApiModel):
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityCollectionWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)
    capability_ids: list[str] = Field(min_length=1, max_length=100)
    row_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_collection(self) -> CapabilityCollectionWrite:
        self.name = self.name.strip()
        self.category = self.category.strip()
        self.description = self.description.strip()
        if not self.name:
            raise ValueError("name cannot be blank")
        if len(self.capability_ids) != len(set(self.capability_ids)):
            raise ValueError("Capability collection members must be unique")
        return self


class ProviderModelWrite(ApiModel):
    model_name: str = Field(min_length=1, max_length=240)
    enabled: bool = True
    is_default: bool = False


def _empty_provider_models() -> list[ProviderModelWrite]:
    return []


class ModelProviderBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class ModelProviderDiscoveryWrite(ApiModel):
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: SecretStr | None = None
    provider_id: str | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> ModelProviderDiscoveryWrite:
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.base_url:
            raise ValueError("base_url cannot be blank")
        return self


class ModelProviderWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    auth_type: Literal["API_KEY", "CODEX_OAUTH"] = "API_KEY"
    base_url: str = ""
    api_key: str | None = None
    row_version: int | None = None
    models: list[ProviderModelWrite] = Field(default_factory=_empty_provider_models)

    @model_validator(mode="after")
    def validate_models(self) -> ModelProviderWrite:
        self.base_url = self.base_url.strip()
        if self.auth_type == "API_KEY" and not self.base_url:
            raise ValueError("base_url is required for API key providers")
        if self.auth_type == "API_KEY" and not self.models:
            raise ValueError("at least one model is required for API key providers")
        names = [item.model_name for item in self.models]
        if len(names) != len(set(names)):
            raise ValueError("model names must be unique")
        if sum(item.is_default for item in self.models) > 1:
            raise ValueError("only one default model is allowed")
        if self.models and not any(item.is_default and item.enabled for item in self.models):
            self.models[0].enabled = True
            self.models[0].is_default = True
        return self


class AgentPresetWrite(ApiModel):
    """One launch-scoped Agent configuration.

    The caller names published capability versions; the service resolves and
    freezes their identities when it reserves the native Conversation.
    """

    capability_version_ids: list[str] = Field(default_factory=list)
    model_provider_id: str | None = Field(default=None, min_length=1, max_length=36)
    model_name: str | None = Field(default=None, min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    node_context_enabled: bool = False
    # An optional launch-only replacement for the node's saved free-text
    # context. It is persisted only on this Attempt, never back to the Node.
    node_context_prompt: str | None = Field(default=None, max_length=200_000)

    @model_validator(mode="after")
    def validate_preset(self) -> AgentPresetWrite:
        if len(self.capability_version_ids) != len(set(self.capability_version_ids)):
            raise ValueError("Agent capability versions must be unique")
        if self.model_name and not self.model_provider_id:
            raise ValueError("model_provider_id is required when model_name is selected")
        return self


class GateAgentPresetWrite(ApiModel):
    """The model-only configuration of an isolated gate Agent.

    Gate Agents deliberately do not inherit the primary launch's capabilities
    or node context.  They receive only the frozen gate payload in a separate
    native Conversation.
    """

    model_provider_id: str | None = Field(default=None, min_length=1, max_length=36)
    model_name: str | None = Field(default=None, min_length=1, max_length=240)
    reasoning_effort: str | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def validate_preset(self) -> GateAgentPresetWrite:
        if self.model_name and not self.model_provider_id:
            raise ValueError("model_provider_id is required when model_name is selected")
        return self


class GateWrite(ApiModel):
    stage: Literal["START", "END"]
    position: int = Field(ge=0)
    # A gate is always evaluated by an isolated Agent configuration.  Python
    # is an optional instruction/tool payload for that Agent, not a separate
    # platform-owned gate mode.
    gate_type: Literal["PROMPT", "PYTHON"] = "PROMPT"
    enabled: bool = True
    # Evaluation time is a Runtime safety bound, not a user-configurable gate
    # decision. The Agent's JSON decision is the only product-level outcome.
    timeout_seconds: int = Field(default=300, ge=1, le=300)
    config: dict[str, Any] = Field(default_factory=_empty_any_dict)
    # Gates are always evaluated by their own isolated Agent conversation.
    # There is no default/workspace fallback for a gate.
    agent_preset: GateAgentPresetWrite

    @model_validator(mode="after")
    def validate_gate_config(self) -> GateWrite:
        prompt = self.config.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Gate prompt must be non-empty text")
        code = self.config.get("code")
        if code is not None:
            if not isinstance(code, str):
                raise ValueError("Python gate code must be text")
            if len(code.encode()) > 256 * 1024:
                raise ValueError("Python gate scripts cannot exceed 256 KiB")
            filename = self.config.get("script_filename")
            if filename is not None and (
                not isinstance(filename, str) or not filename.endswith(".py")
            ):
                raise ValueError("Python gate script_filename must end in .py")
        return self


def _empty_gates() -> list[GateWrite]:
    return []


class FlowNodeWrite(ApiModel):
    instance_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
    node_asset_id: str
    alias: str | None = None
    position_x: int = 0
    position_y: int = 0
    config_override: dict[str, Any] = Field(default_factory=_empty_any_dict)
    gates: list[GateWrite] = Field(default_factory=_empty_gates)


class PortMappingWrite(ApiModel):
    source_instance_key: str
    source_output_key: str
    target_instance_key: str
    target_input_key: str


class FlowEdgeWrite(ApiModel):
    source_instance_key: str
    target_instance_key: str
    position: int = Field(default=0, ge=0)


def _empty_edges() -> list[FlowEdgeWrite]:
    return []


def _empty_port_mappings() -> list[PortMappingWrite]:
    return []


class FlowWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    # Accepted only so pre-FR-15 clients can save a template while rolling out.
    # Flow Definitions never persist or use this legacy Runtime selection.
    environment_version_id: str | None = Field(default=None, min_length=1, max_length=36)
    default_entry_key: str | None = None
    row_version: int | None = None
    nodes: list[FlowNodeWrite] = Field(min_length=1)
    edges: list[FlowEdgeWrite] = Field(default_factory=_empty_edges)
    port_mappings: list[PortMappingWrite] = Field(default_factory=_empty_port_mappings)


class ArtifactWrite(ApiModel):
    field_key: str = Field(min_length=1, max_length=100)
    artifact_type: Literal["URL", "FILE"] = "URL"
    inline_content: str | None = None
    uri: str | None = None
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=_empty_any_dict)

    @model_validator(mode="after")
    def has_content(self) -> ArtifactWrite:
        if self.artifact_type == "URL":
            if self.inline_content is not None:
                raise ValueError("URL artifacts must use uri, not inline_content")
            if self.uri is None:
                raise ValueError("URL artifact requires a URL")
            self.uri = _http_url(self.uri, "artifact uri")
            self.mime_type = "text/uri-list"
            return self
        if self.uri is not None:
            raise ValueError("FILE artifacts must upload content, not use uri")
        if self.inline_content is None or not self.inline_content:
            raise ValueError("FILE artifact requires uploaded or inline content")
        filename = str(self.metadata.get("filename") or "").strip()
        if (
            not filename
            or filename != filename.split("/")[-1]
            or filename != filename.split("\\")[-1]
        ):
            raise ValueError("FILE artifact requires a safe filename")
        return self


def _empty_artifacts() -> list[ArtifactWrite]:
    return []


class RunStart(ApiModel):
    name: str | None = None
    environment_version_id: str = Field(min_length=1, max_length=36)
    # Backward-compatible command shape. New clients omit these fields and
    # create an empty run before activating a node explicitly.
    flow_node_key: str | None = None
    artifacts: list[ArtifactWrite] = Field(default_factory=_empty_artifacts)
    input_bindings: dict[str, str] = Field(default_factory=_empty_str_dict)


class AutomaticNodePlanWrite(ApiModel):
    """Editable, pre-runtime configuration for one automatic flow node."""

    startup_prompt: str = Field(min_length=1, max_length=200_000)
    agent_preset: AgentPresetWrite
    gates: list[GateWrite] = Field(default_factory=_empty_gates)
    artifact_ids: dict[str, str] = Field(default_factory=_empty_str_dict)
    input_urls: dict[str, str] = Field(default_factory=_empty_str_dict)

    @model_validator(mode="after")
    def validate_input_urls(self) -> AutomaticNodePlanWrite:
        self.input_urls = {
            field_key: _http_url(value, f"input URL for {field_key}")
            for field_key, value in self.input_urls.items()
        }
        return self


class AutomaticRunDraftWrite(ApiModel):
    name: str | None = Field(default=None, max_length=220)
    environment_version_id: str = Field(min_length=1, max_length=36)
    start_node_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
    node_plans: dict[str, AutomaticNodePlanWrite] = Field(default_factory=dict, max_length=200)


class AutomaticRunDraftUpdateWrite(ApiModel):
    expected_row_version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=220)
    start_node_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
    node_plans: dict[str, AutomaticNodePlanWrite] = Field(default_factory=dict, max_length=200)


class AutomaticRunStartWrite(ApiModel):
    """Optimistic-lock the one-way transition from editable draft to Runtime work."""

    expected_row_version: int = Field(ge=1)


class AutomaticRunCopyWrite(ApiModel):
    """Name an independent draft copied from one frozen automatic run."""

    name: str | None = Field(default=None, max_length=220)


class NodeRunCopyWrite(ApiModel):
    """Create a fresh manual record from a prior record's launch configuration."""

    name: str | None = Field(default=None, max_length=220)


class NodeRunStart(ApiModel):
    startup_mode: Literal["PROMPT", "CHAT"] = "PROMPT"
    startup_prompt: str | None = Field(default=None, max_length=200_000)
    artifact_ids: dict[str, str] = Field(default_factory=_empty_str_dict)
    input_urls: dict[str, str] = Field(default_factory=_empty_str_dict)
    gates: list[GateWrite] = Field(default_factory=_empty_gates)
    agent_preset: AgentPresetWrite | None = None

    @model_validator(mode="after")
    def validate_input_urls(self) -> NodeRunStart:
        if self.startup_mode == "PROMPT" and self.agent_preset is None:
            raise ValueError("agent_preset is required for PROMPT startup")
        normalized: dict[str, str] = {}
        for field_key, value in self.input_urls.items():
            normalized[field_key] = _http_url(value, f"input URL for {field_key}")
        self.input_urls = normalized
        return self


class AttemptVersionWrite(ApiModel):
    expected_state_version: int = Field(ge=1)


class GateRiskAcceptanceWrite(AttemptVersionWrite):
    reason: str = Field(min_length=1, max_length=4000)


class GateRemediationWrite(AttemptVersionWrite):
    """Request an auditable native-Fork revision after an END-gate failure."""

    pass


class ManualAttemptOutputWrite(ApiModel):
    artifact_type: Literal["URL", "FILE"]
    uri: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def validate_value(self) -> ManualAttemptOutputWrite:
        if self.artifact_type == "URL":
            if self.path is not None or self.uri is None:
                raise ValueError("URL output requires uri and does not accept path")
            self.uri = _http_url(self.uri, "manual output uri")
            return self
        if self.uri is not None or self.path is None or not self.path.strip():
            raise ValueError("FILE output requires path and does not accept uri")
        self.path = self.path.strip()
        return self


class ManualAttemptOutputsWrite(AttemptVersionWrite):
    outputs: dict[str, ManualAttemptOutputWrite] = Field(max_length=100)


class RuntimeCancelRecoveryWrite(AttemptVersionWrite):
    mode: Literal["RECONCILE_PARENT", "DELETE_MANAGED_RUNTIME"] = "RECONCILE_PARENT"


class AttemptStartWrite(AttemptVersionWrite):
    startup_mode: Literal["SKILL", "PROMPT"] = "PROMPT"
    capability_key: str | None = None
    prompt: str | None = None

    @model_validator(mode="after")
    def validate_startup(self) -> AttemptStartWrite:
        if self.startup_mode == "SKILL" and not self.capability_key:
            raise ValueError("capability_key is required for SKILL startup")
        if self.startup_mode == "PROMPT" and self.prompt is not None and not self.prompt.strip():
            raise ValueError("prompt cannot be blank")
        return self


class InputBindingsWrite(AttemptVersionWrite):
    bindings: dict[str, str]


class HumanInputWrite(AttemptVersionWrite):
    content: str = Field(min_length=1)


class RuntimeConfirmationDecisionWrite(ApiModel):
    accept: bool
    reason: str = Field(min_length=1, max_length=4000)


class RejectWrite(AttemptVersionWrite):
    reason: str = Field(min_length=1)
    copy_input_bindings: bool = True


class SyncSnapshotWrite(ApiModel):
    expected_active_version: int | None = None


class MCPScriptWrite(ApiModel):
    server: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1)


def _empty_mcp_scripts() -> list[MCPScriptWrite]:
    return []


class CapabilityMcpRevisionWrite(CapabilitySkillRevisionWrite):
    mcp_scripts: list[MCPScriptWrite] = Field(default_factory=_empty_mcp_scripts, max_length=20)


class HookScriptWrite(ApiModel):
    filename: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1)


def _empty_hook_scripts() -> list[HookScriptWrite]:
    return []


class ContextBundleDocumentWrite(ApiModel):
    path: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=200)


class ContextBundleManifestWrite(ApiModel):
    """User-confirmed presentation metadata for an already validated Bundle."""

    entrypoint: str | None = Field(default=None, max_length=1000)
    documents: list[ContextBundleDocumentWrite] = Field(min_length=1, max_length=100)
    conflict_policy: Literal["ORDERED_DOCUMENTS_LATER_WINS"] = "ORDERED_DOCUMENTS_LATER_WINS"


class CapabilityValidateWrite(ApiModel):
    capability_type: Literal[
        "SKILL",
        "PLUGIN",
        "MCP",
        "HOOK",
        "AGENT_DEFINITION",
        "CONTEXT",
        "CONTEXT_POLICY",
        "MEMORY_POLICY",
        "CRITIC_POLICY",
        "AGENT_PROFILE",
    ]
    filename: str
    content_base64: str
    context_title: str | None = Field(default=None, max_length=200)
    context_description: str | None = Field(default=None, max_length=2000)
    context_bundle_manifest: ContextBundleManifestWrite | None = None
    mcp_scripts: list[MCPScriptWrite] = Field(default_factory=_empty_mcp_scripts, max_length=20)
    hook_scripts: list[HookScriptWrite] = Field(default_factory=_empty_hook_scripts, max_length=20)


class CapabilityCommitWrite(ApiModel):
    import_token: str


class MCPReadOnlyToolCallWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=_empty_any_dict)


class MCPProbeWrite(ApiModel):
    environment_version_id: str
    timeout: float = Field(default=15.0, gt=0, le=120)
    read_only_tool_call: MCPReadOnlyToolCallWrite | None = None
    oauth_secret_reference_id: str | None = None
    expected_oauth_secret_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_oauth_reference(self) -> MCPProbeWrite:
        if (self.oauth_secret_reference_id is None) != (self.expected_oauth_secret_version is None):
            raise ValueError(
                "oauth_secret_reference_id and expected_oauth_secret_version "
                "must be supplied together"
            )
        return self


class MCPOAuthSecretReferenceWrite(ApiModel):
    environment_version_id: str


class MCPOAuthSecretReferenceRevokeWrite(ApiModel):
    expected_state_version: int = Field(ge=1)


class MCPOAuthAuthorizationStartWrite(ApiModel):
    expected_state_version: int = Field(ge=1)
    timeout: float = Field(default=15.0, gt=0, le=120)


class MCPOAuthAuthorizationCallbackWrite(ApiModel):
    expected_authorization_version: int = Field(ge=1)
    callback_url: SecretStr


class PluginSourceResolveWrite(ApiModel):
    source_url: str = Field(min_length=1, max_length=2048)
    commit: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    repo_path: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    )


class MarketplacePluginSourceResolveWrite(ApiModel):
    marketplace_source_url: str = Field(min_length=1, max_length=2048)
    marketplace_commit: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    marketplace_repo_path: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    )
    plugin_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )


class MarketplaceCatalogWrite(ApiModel):
    marketplace_source_url: str = Field(min_length=1, max_length=2048)
    marketplace_commit: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    marketplace_repo_path: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$",
    )


class MarketplaceCatalogPluginRead(ApiModel):
    name: str
    description: str | None = None
    version: str | None = None
    category: str | None = None
    author: str | None = None


class MarketplaceCatalogRead(ApiModel):
    schema_version: Literal[1]
    source: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    repo_path: str | None = None
    marketplace_name: str
    description: str | None = None
    version: str | None = None
    owner: str
    plugins: list[MarketplaceCatalogPluginRead]


class PluginSourcePublishWrite(ApiModel):
    expected_state_version: int = Field(ge=1)


class PluginProbeWrite(ApiModel):
    environment_version_id: str


class MemorySourceCreateWrite(ApiModel):
    source_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    display_name: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    scope: Literal["USER", "PROJECT"]
    scope_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=262_144)


class MemorySourceRevisionWrite(ApiModel):
    content: str = Field(min_length=1, max_length=262_144)


class MemorySourceReviewWrite(ApiModel):
    expected_governance_version: int = Field(ge=1)
    decision: Literal["APPROVE", "REJECT"]
    note: str = Field(default="", max_length=2000)


class MemorySourceScanWrite(ApiModel):
    expected_governance_version: int = Field(ge=1)


class MemorySourceActivateWrite(ApiModel):
    expected_governance_version: int = Field(ge=1)
    retention_days: int = Field(default=30, ge=1, le=3650)


class MemorySourceLifecycleWrite(ApiModel):
    expected_governance_version: int = Field(ge=1)


class ConversationCreateWrite(ApiModel):
    title: str | None = Field(default=None, max_length=160)
    expected_attempt_state_version: int = Field(ge=1)
    work_directory_id: str | None = Field(default=None, min_length=1, max_length=36)


class FlowRunConversationCreateWrite(ApiModel):
    node_attempt_id: str = Field(min_length=1, max_length=36)
    title: str | None = Field(default=None, max_length=160)
    work_directory_id: str | None = Field(default=None, min_length=1, max_length=36)


class ConversationPatchWrite(ApiModel):
    title: str = Field(min_length=1, max_length=160)


class ConversationGoalWrite(ApiModel):
    action: Literal["START", "STOP", "RESUME"]
    objective: str | None = Field(default=None, max_length=20_000)
    max_iterations: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_goal_action(self) -> ConversationGoalWrite:
        if self.action == "START" and not (self.objective or "").strip():
            raise ValueError("Goal START requires an objective")
        if self.action != "START" and self.objective is not None:
            raise ValueError("Goal STOP/RESUME cannot replace the frozen objective")
        if self.action != "START" and "max_iterations" in self.model_fields_set:
            raise ValueError("Goal STOP/RESUME cannot replace frozen governance limits")
        return self


class ConversationAskAgentWrite(ApiModel):
    question: str = Field(min_length=1, max_length=20_000)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class TextPartWrite(ApiModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class AttachmentPartWrite(ApiModel):
    type: Literal["attachment"] = "attachment"
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", min_length=1, max_length=160)
    content_base64: str = Field(min_length=1)


class ConversationQuestionWrite(ApiModel):
    client_question_id: str = Field(min_length=1, max_length=100)
    content: list[TextPartWrite | AttachmentPartWrite] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_attachments(self) -> ConversationQuestionWrite:
        if sum(item.type == "attachment" for item in self.content) > 4:
            raise ValueError("a question may contain at most 4 attachments")
        return self


class RuntimeReplacementWrite(ApiModel):
    expected_generation: int = Field(ge=1)
    expected_session_row_version: int = Field(ge=1)
