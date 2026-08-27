from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


def _empty_outputs() -> dict[str, tuple[str, str]]:
    return {}


def _empty_output_targets() -> dict[str, dict[str, str]]:
    return {}


def _empty_semantic_history() -> tuple[dict[str, str], ...]:
    return ()


def _empty_headers() -> dict[str, str]:
    return {}


def _empty_hook_config() -> dict[str, list[dict[str, Any]]]:
    return {}


def _empty_tool_params() -> dict[str, Any]:
    return {}


def _empty_action_arguments() -> dict[str, Any]:
    return {}


def _empty_task_usage() -> tuple[RuntimeTaskUsageSnapshot, ...]:
    return ()


def _empty_probe_arguments() -> dict[str, Any]:
    return {}


def _empty_wakeup_events() -> tuple[dict[str, Any], ...]:
    return ()


def _empty_usage() -> tuple[RuntimeUsageSnapshot, ...]:
    return ()


@dataclass(frozen=True, slots=True)
class RuntimeCondenser:
    """Frozen, replayable subset of the OpenHands 1.40.0 condenser contract.

    FlowWeave deliberately does not accept arbitrary condenser class names or module
    paths.  ``NO_OP`` is serialized as an explicit ``NoOpCondenser`` so an
    OpenHands default change cannot alter an existing snapshot's runtime behavior.
    """

    kind: Literal["NO_OP", "LLM_SUMMARIZING"] = "NO_OP"
    max_size: int = 240
    max_tokens: int | None = None
    keep_first: int = 2
    minimum_progress: float = 0.1
    hard_context_reset_max_retries: int = 5
    hard_context_reset_context_scaling: float = 0.8


@dataclass(frozen=True, slots=True)
class RuntimeProvider:
    provider_id: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    auth_type: Literal["API_KEY", "CODEX_OAUTH"] = "API_KEY"
    extra_headers: dict[str, str] = field(default_factory=_empty_headers)
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSkill:
    name: str
    content: str
    description: str = ""
    source: str = ""
    workspace_path: str = ""
    dependency_runtime_path: str = ""
    activation_keywords: tuple[str, ...] = ()
    disable_model_invocation: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeMCP:
    name: str
    config: dict[str, Any]
    workspace_path: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeMCPToolCall:
    """One caller-attested read-only MCP invocation used during validation."""

    name: str
    arguments: dict[str, Any] = field(default_factory=_empty_probe_arguments)


@dataclass(frozen=True, slots=True)
class RuntimeMCPProbeRequest:
    """Target-Runtime input for OpenHands' formal ``POST /api/mcp/test``."""

    server: RuntimeMCP
    base_url: str
    runtime_resource_name: str
    timeout: float = 15.0
    read_only_tool_call: RuntimeMCPToolCall | None = None
    oauth_secret_reference_id: str | None = None
    oauth_secret_version: int | None = None
    oauth_state: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeMCPProbeResult:
    ok: bool
    tools: tuple[str, ...] = ()
    error_kind: Literal["timeout", "connection", "unknown"] | None = None
    tool_call_is_error: bool | None = None
    tool_call_text: str | None = field(default=None, repr=False)
    oauth_state: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeMCPOAuthStartRequest:
    """Start one formal OpenHands install-time MCP OAuth job."""

    server: RuntimeMCP
    base_url: str
    runtime_resource_name: str
    timeout: float = 15.0


@dataclass(frozen=True, slots=True)
class RuntimeMCPOAuthJobRequest:
    """Address one formal OAuth job in the Runtime that owns it."""

    job_id: str
    base_url: str
    runtime_resource_name: str


@dataclass(frozen=True, slots=True)
class RuntimeMCPOAuthCallbackRequest(RuntimeMCPOAuthJobRequest):
    """Forward a browser redirect without persisting its authorization code."""

    callback_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeMCPOAuthStatus:
    ok: bool
    status: Literal["pending", "authorizing", "succeeded", "failed"]
    job_id: str
    authorization_url: str | None = field(default=None, repr=False)
    callback_ready: bool = False
    tools: tuple[str, ...] = ()
    error_kind: Literal["timeout", "connection", "unknown"] | None = None
    oauth_state: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RuntimePlugin:
    """One immutable Plugin directory materialized for OpenHands."""

    name: str
    source: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RuntimePluginValidationRequest:
    """Validate one frozen local Plugin with the target image's native loader."""

    plugin: RuntimePlugin
    validation_id: str
    runtime_resource_id: str
    runtime_resource_name: str


@dataclass(frozen=True, slots=True)
class RuntimePluginValidationResult:
    plugin_name: str
    plugin_version: str
    skill_count: int
    command_count: int
    agent_count: int
    mcp_server_count: int
    has_hooks: bool


@dataclass(frozen=True, slots=True)
class RuntimeTool:
    """One explicitly frozen OpenHands tool and its validated parameters."""

    name: str
    params: dict[str, Any] = field(default_factory=_empty_tool_params)


@dataclass(frozen=True, slots=True)
class RuntimeCritic:
    """Frozen native OpenHands Critic configuration."""

    kind: Literal["AgentFinishedCritic"] = "AgentFinishedCritic"
    mode: Literal["finish_and_message", "all_actions"] = "finish_and_message"
    success_threshold: float = 0.6
    max_iterations: int = 1


@dataclass(frozen=True, slots=True)
class RuntimeAgentContext:
    """Frozen OpenHands AgentContext controls for reproducible execution.

    Explicit Skills are carried separately in ``RuntimeAgentSpec.skills``.
    Ambient user/public/project discovery and Marketplace auto-loading are
    frozen off by default and must never be inferred by a Runtime adapter.
    ``load_memory`` is composed from a separately governed Memory Policy.
    """

    system_message_suffix: str = ""
    user_message_suffix: str = ""
    load_user_skills: bool = False
    load_public_skills: bool = False
    marketplace_path: str | None = None
    registered_marketplaces: tuple[dict[str, Any], ...] = ()
    load_project_skills: bool = False
    load_memory: bool = False
    disabled_skills: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeAgentDefinition:
    """Governed OpenHands sub-agent definition."""

    name: str
    description: str
    tools: tuple[str, ...]
    system_prompt: str
    when_to_use_examples: tuple[str, ...] = ()
    permission_mode: str | None = None
    max_iteration_per_run: int | None = None
    max_budget_per_run: float | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBudgets:
    """Execution budgets frozen with the agent specification."""

    max_iterations: int = 100
    max_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAgentProfile:
    """Immutable provenance for a materialized Agent Profile.

    The Profile is never resolved from the mutable OpenHands Agent Profile
    store at execution time.  FlowWeave compiles it into the remaining fields
    of :class:`RuntimeAgentSpec` and carries this identity only for fencing,
    audit, and observability correlation.
    """

    capability_version_id: str
    capability_key: str
    digest: str
    content_hash: str
    schema_version: int = 2
    source_profile_id: str | None = None
    source_revision: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    """Frozen Agent Server compatibility requirements for one Snapshot node.

    The contract is compiled by FlowWeave, not inferred by the adapter.  It is
    intentionally limited to public Agent Server details, OpenAPI operations,
    request fields, declared capabilities, and the tools actually enabled by
    the node's immutable Tool Policy.
    """

    schema_version: int
    openhands_version: str
    source_commit: str
    source_ref: str
    package_versions: tuple[tuple[str, str], ...]
    required_http_operations: tuple[tuple[str, str], ...]
    required_start_fields: tuple[str, ...]
    required_server_capabilities: tuple[str, ...]
    required_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeAgentSpec:
    """Complete, replayable execution-plane configuration for one Agent.

    FlowWeave compiles this value from an immutable Snapshot Manifest. Runtime
    adapters must not add tools or capabilities that are absent from the spec.
    """

    schema_version: int = 1
    agent_kind: Literal["OPENHANDS", "ACP"] = "OPENHANDS"
    runtime_contract: RuntimeContract | None = None
    agent_profile: RuntimeAgentProfile | None = None
    provider: RuntimeProvider | None = None
    tools: tuple[RuntimeTool, ...] = ()
    tool_concurrency_limit: int = 1
    agent_context: RuntimeAgentContext = field(default_factory=RuntimeAgentContext)
    agent_definitions: tuple[RuntimeAgentDefinition, ...] = ()
    plugins: tuple[RuntimePlugin, ...] = ()
    skills: tuple[RuntimeSkill, ...] = ()
    mcp_servers: tuple[RuntimeMCP, ...] = ()
    hook_config: dict[str, list[dict[str, Any]]] = field(default_factory=_empty_hook_config)
    confirmation_policy: Literal["ALWAYS", "NEVER"] = "NEVER"
    condenser: RuntimeCondenser = field(default_factory=RuntimeCondenser)
    condenser_provider: RuntimeProvider | None = None
    critic: RuntimeCritic | None = None
    budgets: RuntimeBudgets = field(default_factory=RuntimeBudgets)


@dataclass(frozen=True, slots=True)
class StartAttemptRequest:
    attempt_id: str
    execution_key: str
    node: dict[str, Any]
    bindings: list[dict[str, Any]]
    workspace_ref: str
    conversation_id: str | None = None
    agent_spec: RuntimeAgentSpec = field(default_factory=RuntimeAgentSpec)
    node_workspace_ref: str = ""
    interaction_mode: Literal["EXECUTION", "COLLABORATION"] = "EXECUTION"
    startup_prompt: str | None = None
    startup_capability_key: str | None = None
    semantic_history: tuple[dict[str, str], ...] = field(default_factory=_empty_semantic_history)
    output_targets: dict[str, dict[str, str]] = field(default_factory=_empty_output_targets)
    environment_image: str = ""
    environment_id: str = ""
    environment_version_id: str = ""
    environment_version_no: int = 0
    runtime_workspace_relative: str = ""
    runtime_working_dir_relative: str = ""
    memory_enabled: bool = False
    runtime_sandbox_id: str = ""
    runtime_resource_name: str = ""
    runtime_base_url: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    job_id: str
    conversation_id: str
    cursor: str | None = None
    runtime_resource_id: str = ""
    runtime_resource_name: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceEntry:
    path: str
    kind: Literal["file", "directory"]
    size: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceSnapshot:
    entries: tuple[RuntimeWorkspaceEntry, ...] = ()
    repositories: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceFile:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class RuntimeConversationIdentity:
    """Ephemeral OpenHands identity evidence used to verify an original-ID reload."""

    conversation_id: str
    workspace_working_dir: str
    persistence_dir: str
    event_id: str | None
    parent_id: str | None
    action_id: str | None
    tool_call_id: str | None


@dataclass(frozen=True, slots=True)
class RuntimeWakeup:
    """Transient notification and the minimal durable Bash compensation state."""

    channel: Literal["CONVERSATION", "BASH"]
    notified: bool = False
    cursor: str | None = None
    events: tuple[dict[str, Any], ...] = field(default_factory=_empty_wakeup_events)


@dataclass(frozen=True, slots=True)
class RuntimeForkResult:
    """Formal identity returned by one native Conversation fork."""

    handle: RuntimeHandle
    source_conversation_id: str
    source_event_id: str | None
    leaf_event_id: str | None
    reset_metrics: bool


@dataclass(frozen=True, slots=True)
class RuntimePendingAction:
    """One redacted OpenHands pending action used at the approval boundary."""

    action_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=_empty_action_arguments)
    security_risk: str = "UNKNOWN"
    summary: str = ""
    digest: str = ""


@dataclass(frozen=True, slots=True)
class RuntimePendingConfirmation:
    """The current OpenHands 1.40.0 confirmation unit: one action batch."""

    pending_actions_digest: str
    actions: tuple[RuntimePendingAction, ...]
    cursor: str | None = None


RuntimeEventType = Literal[
    "MESSAGE",
    "TOOL",
    "TOOL_CALL",
    "TOOL_RESULT",
    "THOUGHT",
    "STATE",
    "OUTPUT",
    "ERROR",
    "HUMAN_INPUT_REQUIRED",
    "COMPLETED",
    "CONDENSATION_REQUESTED",
    "CONDENSATION_COMPLETED",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    cursor: str
    event_type: RuntimeEventType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeTaskUsageSnapshot:
    """One transient cumulative OpenHands ``task:<task_id>`` metrics snapshot.

    OpenHands owns the child metrics and replaces this value after every
    blocking Task run/resume. FlowWeave may inspect it for a current operation
    but must not persist it as Conversation state.
    """

    task_id: str
    source_cursor: str | None
    digest: str
    model_name: str
    accumulated_cost: float
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    context_window: int
    per_turn_tokens: int


@dataclass(frozen=True, slots=True)
class RuntimeUsageSnapshot:
    """One cumulative, formally named OpenHands LLM usage bucket."""

    usage_id: str
    model_name: str
    accumulated_cost: float
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    context_window: int
    per_turn_tokens: int


@dataclass(frozen=True, slots=True)
class RuntimeAskAgentResult:
    """Stateless diagnostic response and its cumulative usage snapshots."""

    response: str
    before_usage: RuntimeUsageSnapshot | None = None
    after_usage: RuntimeUsageSnapshot | None = None


@dataclass(frozen=True, slots=True)
class RuntimeEventBatch:
    events: tuple[RuntimeEvent, ...] = ()
    cursor: str | None = None
    result: RuntimeResult | None = None
    cursor_anchor_found: bool = True
    task_usage: tuple[RuntimeTaskUsageSnapshot, ...] = field(default_factory=_empty_task_usage)
    usage: tuple[RuntimeUsageSnapshot, ...] = field(default_factory=_empty_usage)


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    status: str
    outputs: dict[str, tuple[str, str]] = field(default_factory=_empty_outputs)
    final_message: str | None = None
    human_question: str | None = None
    cursor: str | None = None
    error: str | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "outputs": {key: list(value) for key, value in self.outputs.items()},
            "final_message": self.final_message,
            "human_question": self.human_question,
            "cursor": self.cursor,
            "error": self.error,
        }


class RuntimePort(Protocol):
    def probe_mcp(self, request: RuntimeMCPProbeRequest) -> RuntimeMCPProbeResult: ...

    def validate_plugin(
        self, request: RuntimePluginValidationRequest
    ) -> RuntimePluginValidationResult: ...

    def start_mcp_oauth(self, request: RuntimeMCPOAuthStartRequest) -> RuntimeMCPOAuthStatus: ...

    def read_mcp_oauth(self, request: RuntimeMCPOAuthJobRequest) -> RuntimeMCPOAuthStatus: ...

    def submit_mcp_oauth_callback(
        self, request: RuntimeMCPOAuthCallbackRequest
    ) -> RuntimeMCPOAuthStatus: ...

    def create_conversation(self, request: StartAttemptRequest) -> RuntimeHandle: ...

    def rename_conversation(self, handle: RuntimeHandle, title: str) -> None: ...

    def delete_conversation(self, handle: RuntimeHandle) -> None: ...

    def start(self, request: StartAttemptRequest) -> RuntimeHandle: ...

    def reload_conversation(
        self,
        handle: RuntimeHandle,
        *,
        expected: RuntimeConversationIdentity | None = None,
    ) -> RuntimeConversationIdentity: ...

    def read_events(self, handle: RuntimeHandle) -> RuntimeEventBatch: ...

    def read_active_events(self, handle: RuntimeHandle) -> RuntimeEventBatch: ...

    def stream_events(self, handle: RuntimeHandle) -> AsyncIterator[dict[str, Any]]: ...

    def wait_for_wakeup(
        self,
        handle: RuntimeHandle,
        *,
        channel: Literal["CONVERSATION", "BASH"],
        timeout_seconds: float,
        cursor: str | None = None,
    ) -> RuntimeWakeup: ...

    def inspect(self, handle: RuntimeHandle) -> RuntimeResult: ...

    def switch_model(self, handle: RuntimeHandle, provider: RuntimeProvider) -> None: ...

    def interrupt(self, handle: RuntimeHandle) -> None: ...

    def can_accept_input(self, handle: RuntimeHandle) -> bool: ...

    def navigate(self, handle: RuntimeHandle, event_id: str | None) -> None: ...

    def run(self, handle: RuntimeHandle) -> RuntimeResult: ...

    def get_pending_confirmation(
        self, handle: RuntimeHandle
    ) -> RuntimePendingConfirmation | None: ...

    def respond_to_confirmation(
        self,
        handle: RuntimeHandle,
        expected_pending_digest: str,
        accept: bool,
        reason: str,
    ) -> RuntimeResult: ...

    def condense(self, handle: RuntimeHandle) -> RuntimeResult: ...

    def start_goal(self, handle: RuntimeHandle, objective: str, max_iterations: int) -> None: ...

    def stop_goal(self, handle: RuntimeHandle) -> None: ...

    def resume_goal(self, handle: RuntimeHandle) -> None: ...

    def ask_agent(
        self, handle: RuntimeHandle, question: str, *, timeout_seconds: float
    ) -> RuntimeAskAgentResult: ...

    def fork_conversation(
        self,
        handle: RuntimeHandle,
        *,
        target_conversation_id: str,
        title: str,
        from_event_id: str | None,
        expected_source_leaf_event_id: str,
        reset_metrics: bool,
    ) -> RuntimeForkResult: ...

    def send_message(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult: ...

    def upload_workspace_file(
        self, handle: RuntimeHandle, *, filename: str, content_type: str, content: bytes
    ) -> str: ...

    def workspace_snapshot(self, handle: RuntimeHandle, path: str) -> RuntimeWorkspaceSnapshot: ...

    def download_workspace_file(self, handle: RuntimeHandle, path: str) -> RuntimeWorkspaceFile: ...

    def conversation_context(self, handle: RuntimeHandle) -> dict[str, int | str | None]: ...

    def resume(
        self, handle: RuntimeHandle, content: str, image_urls: tuple[str, ...] = ()
    ) -> RuntimeResult: ...

    def cancel(self, handle: RuntimeHandle) -> None: ...
