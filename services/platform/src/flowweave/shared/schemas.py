from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _empty_any_dict() -> dict[str, Any]:
    return {}


def _empty_str_dict() -> dict[str, str]:
    return {}


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class DirectoryWrite(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    parent_id: str | None = None
    position: int = Field(default=0, ge=0)


class IOFieldWrite(ApiModel):
    field_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    display_name: str = Field(min_length=1, max_length=160)
    data_type: Literal[
        "TEXT",
        "MARKDOWN",
        "JSON_OBJECT",
        "JSON_ARRAY",
        "FILE",
        "FILE_COLLECTION",
        "DOCUMENT",
        "URL",
        "REPOSITORY_REF",
    ]
    description: str = ""


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
    capability_type: Literal["SKILL", "MCP", "HOOK"]
    capability_key: str = Field(min_length=1, max_length=200)
    normalized_config: dict[str, Any] = Field(default_factory=_empty_any_dict)


def _empty_capabilities() -> list[CapabilityWrite]:
    return []


class NodeAssetWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    directory_id: str | None = None
    description: str = ""
    icon_kind: str = "LUCIDE"
    icon_value: str = "bot"
    default_skill_ref: str | None = None
    row_version: int | None = None
    inputs: list[IOFieldWrite] = Field(default_factory=_empty_io_fields)
    outputs: list[IOFieldWrite] = Field(default_factory=_empty_io_fields)
    executor: ExecutorWrite = Field(default_factory=ExecutorWrite)
    capabilities: list[CapabilityWrite] = Field(default_factory=_empty_capabilities)

    @model_validator(mode="after")
    def validate_default_skill(self) -> NodeAssetWrite:
        capability_keys = [item.capability_key for item in self.capabilities]
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("capability keys must be unique within a node")
        skill_keys = {
            item.capability_key for item in self.capabilities if item.capability_type == "SKILL"
        }
        if self.default_skill_ref and self.default_skill_ref not in skill_keys:
            raise ValueError("default_skill_ref must reference an imported SKILL")
        return self


class NodeAssetBulkDeleteWrite(ApiModel):
    ids: list[str] = Field(min_length=1, max_length=100)


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


class EdgeMappingWrite(ApiModel):
    source_output_key: str
    target_input_key: str


def _empty_mappings() -> list[EdgeMappingWrite]:
    return []


class FlowEdgeWrite(ApiModel):
    source_instance_key: str
    target_instance_key: str
    position: int = Field(default=0, ge=0)
    mappings: list[EdgeMappingWrite] = Field(default_factory=_empty_mappings)


def _empty_edges() -> list[FlowEdgeWrite]:
    return []


class FlowWrite(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    default_entry_key: str | None = None
    row_version: int | None = None
    nodes: list[FlowNodeWrite] = Field(min_length=1)
    edges: list[FlowEdgeWrite] = Field(default_factory=_empty_edges)


class ArtifactWrite(ApiModel):
    field_key: str = Field(min_length=1, max_length=100)
    artifact_type: str = Field(min_length=1, max_length=80)
    inline_content: str | None = None
    uri: str | None = None
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=_empty_any_dict)

    @model_validator(mode="after")
    def has_content(self) -> ArtifactWrite:
        if self.inline_content is None and self.uri is None:
            raise ValueError("artifact requires inline_content or uri")
        return self


def _empty_artifacts() -> list[ArtifactWrite]:
    return []


class RunStart(ApiModel):
    name: str | None = None
    flow_node_key: str | None = None
    artifacts: list[ArtifactWrite] = Field(default_factory=_empty_artifacts)
    input_bindings: dict[str, str] = Field(default_factory=_empty_str_dict)


class NodeRunStart(ApiModel):
    artifact_ids: dict[str, str] = Field(default_factory=_empty_str_dict)


class AttemptVersionWrite(ApiModel):
    expected_state_version: int = Field(ge=1)


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
