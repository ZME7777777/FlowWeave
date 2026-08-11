from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    model_provider_id: str | None = None
    model_name: str | None = None
    startup_prompt: str = ""
    context_prompt: str = ""
    timeout_seconds: int = Field(default=900, ge=1, le=86400)
    max_iterations: int = Field(default=100, ge=1, le=1000)


class CapabilityWrite(ApiModel):
    capability_id: str | None = Field(default=None, min_length=38, max_length=48)
    # Read models from older clients may still echo these fields. The server
    # ignores them and resolves the canonical version by capability_id.
    capability_type: Literal["SKILL", "MCP", "HOOK"] | None = None
    capability_key: str | None = Field(default=None, max_length=200)
    normalized_config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> CapabilityWrite:
        if self.capability_id is None and (
            self.capability_type is None
            or not self.capability_key
            or self.normalized_config is None
        ):
            raise ValueError("capability_id is required")
        return self


def _empty_capabilities() -> list[CapabilityWrite]:
    return []


class NodeAssetWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    directory_id: str | None = None
    description: str = ""
    icon_kind: str = "LUCIDE"
    icon_value: str = "bot"
    environment_version_id: str | None = None
    row_version: int | None = None
    inputs: list[IOFieldWrite] = Field(default_factory=_empty_io_fields)
    outputs: list[IOFieldWrite] = Field(default_factory=_empty_io_fields)
    executor: ExecutorWrite = Field(default_factory=ExecutorWrite)
    capabilities: list[CapabilityWrite] = Field(default_factory=_empty_capabilities)

    @model_validator(mode="after")
    def validate_capabilities(self) -> NodeAssetWrite:
        identities = [
            item.capability_id or f"{item.capability_type}:{item.capability_key}"
            for item in self.capabilities
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("capability references must be unique within a node")
        return self


class TerminalEnvironmentWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    base_image: str = Field(min_length=1, max_length=500)
    row_version: int | None = None


class EnvironmentSetupWrite(ApiModel):
    base_version_id: str | None = None


class NodeAssetBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class CapabilityBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class CapabilitySkillRevisionWrite(ApiModel):
    content: str = Field(min_length=1, max_length=1_048_576)


class ProviderModelWrite(ApiModel):
    model_name: str = Field(min_length=1, max_length=240)
    enabled: bool = True
    is_default: bool = False


class ModelProviderBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class ModelProviderWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1)
    api_key: str | None = None
    row_version: int | None = None
    models: list[ProviderModelWrite] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_models(self) -> ModelProviderWrite:
        names = [item.model_name for item in self.models]
        if len(names) != len(set(names)):
            raise ValueError("model names must be unique")
        if sum(item.is_default for item in self.models) > 1:
            raise ValueError("only one default model is allowed")
        if not any(item.is_default and item.enabled for item in self.models):
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
    environment_version_id: str | None = None
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


class RejectWrite(AttemptVersionWrite):
    reason: str = Field(min_length=1)
    copy_input_bindings: bool = True


class SyncSnapshotWrite(ApiModel):
    expected_active_version: int | None = None


class CapabilityValidateWrite(ApiModel):
    capability_type: Literal["SKILL", "MCP", "HOOK"]
    filename: str
    content_base64: str


class CapabilityCommitWrite(ApiModel):
    import_token: str


class ConversationCreateWrite(ApiModel):
    title: str | None = Field(default=None, max_length=160)
    expected_attempt_state_version: int = Field(ge=1)
    baseline: dict[str, Any] = Field(default_factory=_empty_any_dict)


class ConversationPatchWrite(ApiModel):
    title: str = Field(min_length=1, max_length=160)
    expected_conversation_version: int = Field(ge=1)


class ConversationStopWrite(ApiModel):
    expected_conversation_version: int = Field(ge=1)


class ConversationForkWrite(ApiModel):
    expected_conversation_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=160)


class ConversationReviseWrite(ApiModel):
    expected_conversation_version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=20_000)


class TextPartWrite(ApiModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class AttachmentPartWrite(ApiModel):
    type: Literal["attachment"] = "attachment"
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", min_length=1, max_length=160)
    content_base64: str = Field(min_length=1)


class CapabilityInvocationWrite(ApiModel):
    capability_type: Literal["SKILL", "MCP"]
    capability_key: str = Field(min_length=1, max_length=200)


def _empty_capability_invocations() -> list[CapabilityInvocationWrite]:
    return []


class MessageSendWrite(ApiModel):
    client_message_id: str = Field(min_length=1, max_length=100)
    content: list[TextPartWrite | AttachmentPartWrite] = Field(min_length=1, max_length=8)
    capability_refs: list[CapabilityInvocationWrite] = Field(
        default_factory=_empty_capability_invocations, max_length=50
    )
    delivery_mode: Literal["QUEUE_AFTER_TURN", "INTERRUPT_AND_RESUME"] = "QUEUE_AFTER_TURN"
    expected_conversation_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_capability_refs(self) -> MessageSendWrite:
        refs = [
            (item.capability_type, item.capability_key.strip()) for item in self.capability_refs
        ]
        if len(refs) != len(set(refs)):
            raise ValueError("capability_refs must be unique")
        if sum(item.type == "attachment" for item in self.content) > 4:
            raise ValueError("a message may contain at most 4 attachments")
        return self
