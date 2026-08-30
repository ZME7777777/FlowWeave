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


def _lark_path(value: str, kind: Literal["docx", "folder", "wiki", "document", "root"]) -> str:
    parsed = urlparse(value.strip())
    expected_by_kind = {
        "docx": ("/docx/",),
        "folder": ("/drive/folder/",),
        "wiki": ("/wiki/",),
        "document": ("/docx/", "/wiki/"),
        "root": ("/wiki/", "/drive/folder/"),
    }
    expected_paths = expected_by_kind[kind]
    hostname = (parsed.hostname or "").lower().rstrip(".")
    is_lark_host = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ("feishu.cn", "larksuite.com", "larkoffice.com")
    )
    expected = next((path for path in expected_paths if path in parsed.path), None)
    if parsed.scheme != "https" or not is_lark_host or expected is None:
        label = {
            "docx": "docx document",
            "folder": "Drive folder",
            "wiki": "Wiki node",
            "document": "docx document or Wiki node",
            "root": "Wiki node or legacy Drive folder",
        }[kind]
        raise ValueError(f"value must be a Lark {label} URL")
    token = parsed.path.split(expected, 1)[1].split("/", 1)[0]
    if not token:
        raise ValueError("Lark URL token is missing")
    return value.strip()


class DirectoryWrite(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    parent_id: str | None = None
    position: int = Field(default=0, ge=0)


class IOFieldWrite(ApiModel):
    field_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    display_name: str = Field(default="", max_length=160)
    data_type: Literal["URL"] = "URL"
    description: str = ""
    template_url: str = ""

    @model_validator(mode="after")
    def validate_template(self) -> IOFieldWrite:
        self.template_url = self.template_url.strip()
        if self.template_url:
            self.template_url = _lark_path(self.template_url, "docx")
        return self


def _empty_io_fields() -> list[IOFieldWrite]:
    return []


class ExecutorWrite(ApiModel):
    startup_prompt: str = ""
    context_prompt: str = ""


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


class EnvironmentSetupWrite(ApiModel):
    base_version_id: str | None = None


class NodeAssetBulkDeleteWrite(ApiModel):
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


class GateWrite(ApiModel):
    stage: Literal["START", "END"]
    position: int = Field(ge=0)
    gate_type: Literal["PROMPT", "PYTHON", "JAVASCRIPT"]
    enabled: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    config: dict[str, Any] = Field(default_factory=_empty_any_dict)


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
    lark_root_folder_url: str = Field(min_length=1)
    row_version: int | None = None
    nodes: list[FlowNodeWrite] = Field(min_length=1)
    edges: list[FlowEdgeWrite] = Field(default_factory=_empty_edges)
    port_mappings: list[PortMappingWrite] = Field(default_factory=_empty_port_mappings)

    @model_validator(mode="after")
    def validate_lark_root(self) -> FlowWrite:
        self.lark_root_folder_url = _lark_path(self.lark_root_folder_url, "root")
        return self


class ArtifactWrite(ApiModel):
    field_key: str = Field(min_length=1, max_length=100)
    artifact_type: Literal["URL"] = "URL"
    inline_content: str | None = None
    uri: str | None = None
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=_empty_any_dict)

    @model_validator(mode="after")
    def has_content(self) -> ArtifactWrite:
        if self.inline_content is not None:
            raise ValueError("URL artifacts must use uri, not inline_content")
        if self.uri is None:
            raise ValueError("artifact requires a URL")
        parsed = urlparse(self.uri.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("artifact uri must be an HTTP(S) URL")
        self.uri = self.uri.strip()
        self.mime_type = "text/uri-list"
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


class NodeRunStart(ApiModel):
    artifact_ids: dict[str, str] = Field(default_factory=_empty_str_dict)
    input_urls: dict[str, str] = Field(default_factory=_empty_str_dict)

    @model_validator(mode="after")
    def validate_input_urls(self) -> NodeRunStart:
        normalized: dict[str, str] = {}
        for field_key, value in self.input_urls.items():
            parsed = urlparse(value.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"input URL for {field_key} must be HTTP(S)")
            normalized[field_key] = value.strip()
        self.input_urls = normalized
        return self


class AttemptVersionWrite(ApiModel):
    expected_state_version: int = Field(ge=1)


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


class CapabilityValidateWrite(ApiModel):
    capability_type: Literal[
        "SKILL",
        "PLUGIN",
        "MCP",
        "HOOK",
        "TOOL_POLICY",
        "AGENT_DEFINITION",
        "CONTEXT_POLICY",
        "MEMORY_POLICY",
        "CRITIC_POLICY",
        "AGENT_PROFILE",
    ]
    filename: str
    content_base64: str
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
