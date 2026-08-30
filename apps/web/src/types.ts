export type ViewName = 'nodes' | 'capabilities' | 'environments' | 'models' | 'flows' | 'runs' | 'workbench' | 'agent-workbench';

export interface NodeDirectory {
  id: string; parent_id?: string | null; name: string; position: number; row_version: number;
}
export type ArtifactDataType = 'URL';
export interface IOField {
  id?: string; field_key: string; display_name: string; data_type: ArtifactDataType;
  description: string; template_url: string; position?: number;
}
export interface ExecutorConfig {
  startup_prompt: string; context_prompt: string;
}
export type CapabilityAssetType =
  | 'SKILL' | 'PLUGIN' | 'MCP' | 'HOOK' | 'TOOL_POLICY' | 'AGENT_DEFINITION'
  | 'CONTEXT_POLICY' | 'MEMORY_POLICY' | 'CRITIC_POLICY' | 'AGENT_PROFILE';
export interface CapabilityRef {
  id?: string; capability_id?: string; capability_type: CapabilityAssetType; capability_key: string;
  normalized_config: Record<string, unknown>; position?: number;
}
export interface CapabilityAsset {
  id: string; lineage_id: string; revision_number: number; is_latest: boolean;
  capability_type: CapabilityAssetType; capability_key: string;
  description: string; version: string; filename: string; content_hash: string;
  byte_size: number; import_id: string; created_at: string; reference_count: number;
  is_builtin: boolean; document: Record<string, unknown>;
  dependencies: Record<string, Record<string, string>>;
  dependency_build_state: 'NOT_REQUIRED' | 'PENDING' | 'READY' | 'FAILED';
  dependency_build_error?: string | null;
}
export interface CapabilityCollection {
  id: string; name: string; category: string; description: string; row_version: number;
  members: CapabilityAsset[]; created_at: string; updated_at: string;
}
export interface CapabilityCollectionWrite {
  name: string; category: string; description: string; capability_ids: string[];
  row_version?: number | null;
}
export interface SkillSource {
  id: string; capability_key: string; filename: string; entry: string; content: string;
}
export interface McpSource {
  id: string; capability_key: string; filename: string; content: string;
  mcp_scripts: Array<{ server: string; filename: string; content_base64: string }>;
}
export interface CapabilityImportResult {
  id: string; capability_type: CapabilityAssetType; filename: string;
  content_hash: string; storage_key: string; capabilities: CapabilityAsset[];
}
export interface ToolPolicyParameter {
  type: 'string' | 'integer'; max_length?: number; minimum?: number; maximum?: number; enum?: string[];
}
export interface ToolPolicyCatalogItem {
  name: string; module: string; params: Record<string, ToolPolicyParameter>;
  access: 'READ_ONLY' | 'READ_WRITE' | 'CONTROL' | 'OPEN_WORLD';
  confirmation: 'NONE' | 'REQUIRED'; concurrency: 'READ_ONLY' | 'RESOURCE_LOCKED' | 'SERIAL_ONLY';
  policy_enabled: boolean; disabled_reason?: string | null;
}
export interface ToolPolicyCatalog {
  schema_version: number; openhands_version: string; source_commit: string; catalog_digest: string;
  max_tool_concurrency: number; tools: ToolPolicyCatalogItem[];
}
export interface PluginSourceResolution {
  id: string; source_kind: 'GIT' | 'MARKETPLACE'; source_url: string; requested_commit: string; repo_path?: string | null;
  marketplace_plugin_name?: string | null; resolved_source_url?: string | null;
  resolved_commit?: string | null; resolved_repo_path?: string | null;
  state: 'PENDING' | 'READY' | 'PUBLISHED' | 'FAILED' | 'EXPIRED'; state_version: number;
  content_hash?: string | null; byte_size?: number | null; error_detail?: string | null;
  preview: {
    contributions?: { skills?: string[]; commands?: string[]; mcp_servers?: string[]; hook_events?: string[] };
    file_count?: number;
  };
  capability?: { capability_id: string; capability_type: 'PLUGIN'; capability_key?: string } | null;
  expires_at: string; resolved_at?: string | null; published_at?: string | null;
}
export interface MarketplaceCatalog {
  schema_version: 1; source: string; commit: string; repo_path?: string | null;
  marketplace_name: string; description?: string | null; version?: string | null; owner: string;
  plugins: Array<{ name: string; description?: string | null; version?: string | null; category?: string | null; author?: string | null }>;
}
export interface AgentProfileVersion {
  id: string; package_id: string; capability_key: string; version_no: number;
  digest: string; content_hash: string; state: 'PUBLISHED' | 'RETIRED';
  document: Record<string, unknown>; compatibility: {
    openhands_version: string; source_commit: string; schema_version: number;
    fields: Record<string, string>; server_profile_store: string; activation_semantics: string;
  }; created_at: string;
}
export interface NamedDeleteReference { id: string; name: string }
export interface BlockedCapabilityDelete {
  id: string; name: string; relation: 'NODE_CAPABILITY' | 'CAPABILITY_COLLECTION' | 'BUILTIN_CAPABILITY' | 'AGENT_WORKSPACE' | 'CAPABILITY_GOVERNANCE';
  nodes?: NamedDeleteReference[]; collections?: NamedDeleteReference[];
  workspaces?: NamedDeleteReference[]; governance?: Array<{ id: string; relation: string }>;
}
export interface BlockedNodeDelete {
  id: string; name: string; relation: 'FLOW_NODE';
  flows: Array<NamedDeleteReference & { reference_count: number }>;
}
export interface BlockedProviderDelete {
  id: string; name: string; relation: 'AGENT_CONFIGURATION'; nodes: NamedDeleteReference[];
}
export interface BulkDeleteResult<T> { deleted_ids: string[]; blocked: T[] }
export interface CapabilityBulkDeleteResult extends BulkDeleteResult<BlockedCapabilityDelete> {
  collection_changes: { updated: string[]; deleted: string[] };
}
export interface EnvironmentVersion {
  id: string; environment_id: string; version_no: number; parent_version_id?: string | null;
  state: 'PUBLISHING' | 'READY' | 'FAILED'; image_reference: string; image_digest: string;
  base_image_reference: string; base_image_digest: string;
  manifest: { commands?: Record<string, string>; [key: string]: unknown };
  error_detail?: string | null; runtime_compatible: boolean;
  runtime_incompatibility_reason?: string | null; run_reference_count: number;
  reference_count: number; created_at: string;
}
export interface EnvironmentSetupSession {
  id: string; environment_id: string; base_version_id?: string | null;
  state: 'STARTING' | 'RUNNING' | 'PUBLISHING' | 'PUBLISHED' | 'FAILED' | 'CANCELLED' | 'EXPIRED';
  base_image_reference: string; expires_at: string; error_detail?: string | null;
}
export interface TerminalEnvironment {
  id: string; name: string; description: string; row_version: number;
  versions: EnvironmentVersion[]; active_sessions: EnvironmentSetupSession[];
  created_at: string; updated_at: string;
}
export interface TerminalEnvironmentWrite {
  name: string; description: string; row_version?: number | null;
}
export interface NodeAsset {
  id: string; directory_id?: string | null; name: string; description: string;
  icon_kind: string; icon_value: string; row_version: number;
  workspace_ref?: string;
  inputs: IOField[]; outputs: IOField[]; executor: ExecutorConfig | null;
  created_at: string; updated_at: string;
}
export interface NodeAssetWrite {
  directory_id?: string | null; name: string; description: string;
  icon_kind: string; icon_value: string;
  row_version?: number | null; inputs: IOField[]; outputs: IOField[];
  executor: ExecutorConfig;
}

export interface ProviderModel {
  id?: string; model_name: string; enabled: boolean; is_default: boolean;
  default_reasoning_effort?: string | null; supported_reasoning_efforts?: string[];
  context_window?: number | null;
}
export interface ModelProvider {
  id: string; name: string; base_url: string; auth_type: 'API_KEY' | 'CODEX_OAUTH';
  has_api_key: boolean; api_key_hint?: string | null; oauth_connected: boolean;
  oauth_account_email?: string | null; oauth_device_pending: boolean;
  connection_state: string; reference_node_count: number; available_for_nodes: boolean;
  available_for_prompt_gates: boolean;
  row_version: number; models: ProviderModel[];
  created_at: string; updated_at: string;
}
export interface ModelProviderWrite {
  name: string; auth_type: 'API_KEY' | 'CODEX_OAUTH'; base_url: string;
  api_key?: string | null; row_version?: number | null;
  models: ProviderModel[];
}
export interface ModelProviderDiscoveryWrite {
  base_url: string; api_key?: string | null; provider_id?: string | null;
}
export interface CodexDeviceAuthorization {
  verification_url: string; user_code: string; expires_at: string; interval: number;
}
export interface CodexOAuthStatus {
  state: string; connected: boolean; account_email?: string | null;
  model_count?: number; model_sync_error?: string | null;
}
export interface GatePolicy {
  id?: string; stage: 'START' | 'END'; position: number;
  gate_type: 'PROMPT' | 'PYTHON' | 'JAVASCRIPT'; enabled: boolean;
  timeout_seconds: number; config: Record<string, unknown>; content_hash?: string;
}
export interface FlowNode {
  id?: string; instance_key: string; node_asset_id: string; alias?: string | null;
  position_x: number; position_y: number; config_override: Record<string, unknown>;
  gates: GatePolicy[];
}
export interface FlowEdge {
  id?: string; source_instance_key: string; target_instance_key: string;
  position: number;
}
export interface FlowPortMapping {
  id?: string; source_instance_key: string; source_output_key: string;
  target_instance_key: string; target_input_key: string;
}
export interface FlowDefinition {
  id: string; name: string; description: string; default_entry_key?: string | null;
  lark_root_folder_url: string;
  row_version: number; nodes: FlowNode[]; edges: FlowEdge[]; port_mappings: FlowPortMapping[];
  created_at: string; updated_at: string;
}
export interface FlowWrite {
  name: string; description: string; default_entry_key?: string | null;
  lark_root_folder_url: string;
  row_version?: number | null; nodes: FlowNode[]; edges: FlowEdge[]; port_mappings: FlowPortMapping[];
}

export interface ArtifactVersion {
  id: string; flow_run_id: string; producer_attempt_id?: string | null;
  field_key: string; version_no: number; artifact_type: string;
  storage_key?: string | null; uri?: string | null; inline_content?: string | null;
  content_hash: string; byte_size: number; mime_type: string; source: string;
  metadata: Record<string, unknown>; created_at: string;
}
export interface InputBinding {
  id: string; input_field_key: string; artifact_version_id: string; binding_source: string;
}
export interface GateEvaluation {
  id: string; stage: 'START' | 'END'; policy_snapshot_key: string; policy_position: number;
  evaluation_attempt: number; state: string; decision: 'PASS' | 'FAIL' | 'ERROR';
  result: { summary?: string; reasons?: string[]; [key: string]: unknown };
  error_code?: string | null; created_at: string;
}
export type AttemptState =
  | 'WAITING_INPUT' | 'START_GATES' | 'START_BLOCKED' | 'WAITING_START_CONFIRMATION'
  | 'EXECUTING' | 'WAITING_HUMAN' | 'WAITING_CONFIRMATION' | 'END_GATES' | 'END_BLOCKED'
  | 'WAITING_ACCEPTANCE' | 'ACCEPTED' | 'REJECTED' | 'CANCELLED';
export interface RuntimeConfirmationAction {
  action_id: string; tool_call_id: string; tool_name: string;
  arguments: Record<string, unknown>; security_risk: string; summary: string; digest: string;
}
export interface RuntimeConfirmationBatch {
  id: string; attempt_id: string; conversation_id: string; runtime_conversation_id: string;
  runtime_cursor?: string | null; pending_actions_digest: string;
  pending_actions: RuntimeConfirmationAction[];
  risk_summary: Array<{ action_id: string; security_risk: string; summary: string }>;
  action_count: number; state: 'PENDING' | 'DECIDING' | 'APPROVED' | 'REJECTED' | 'EXPIRED' | 'CANCELLED';
  decision_accept?: boolean | null; decision_reason?: string | null; decided_by?: string | null;
  decided_at?: string | null; runtime_response_cursor?: string | null; state_version: number;
  created_at: string; updated_at: string;
}
export interface NodeAttempt {
  id: string; node_run_id: string; attempt_no: number; snapshot_id: string;
  state: AttemptState; state_version: number; runtime_phase?: string | null;
  runtime_adapter?: string | null;
  runtime_job_id?: string | null; conversation_id?: string | null; runtime_cursor?: string | null;
  workspace_ref?: string | null; error_code?: string | null; error_detail?: string | null;
  runtime_cancel_recovery_modes: Array<'RECONCILE_PARENT' | 'DELETE_MANAGED_RUNTIME'>;
  startup_mode?: 'SKILL' | 'PROMPT'; startup_capability_key?: string | null;
  startup_prompt?: string | null;
  output_targets?: Record<string, { url: string; token: string; template_url: string; title: string }>;
  input_bindings: InputBinding[]; artifacts: ArtifactVersion[];
  gate_evaluations: GateEvaluation[]; runtime_confirmation_batches: RuntimeConfirmationBatch[];
  created_at: string; updated_at: string;
}
export interface NodeRun {
  id: string; flow_run_id: string; flow_node_snapshot_key: string; sequence_no: number;
  state: 'ACTIVE' | 'ACCEPTED' | 'CANCELLED'; accepted_attempt_id?: string | null;
  created_from: string; activated_at: string; attempts: NodeAttempt[];
}
export interface SnapshotFlowNode extends FlowNode { asset: NodeAsset }
export interface SnapshotDefinition extends Omit<FlowDefinition, 'nodes'> {
  nodes: SnapshotFlowNode[];
}
export interface RunSnapshot {
  id: string; version: number; schema_version: number; definition_hash: string;
  environment_version_id?: string | null;
  definition: SnapshotDefinition;
  created_at: string;
}
export interface FlowRunSummary {
  id: string; flow_definition_id: string; flow_name?: string | null;
  flow_row_version?: number | null; run_no: number; name: string; state: string;
  completion_mode?: string | null; active_snapshot_version?: number | null;
  environment_version_id?: string | null;
  current_node_key?: string | null; current_node_name?: string | null;
  current_attempt_state?: AttemptState | null; has_pending_action: boolean;
  progress: { accepted: number; terminal: number; active: number };
  started_at: string; updated_at: string; finished_at?: string | null;
}
export interface FlowRun extends FlowRunSummary {
  row_version: number; active_snapshot_id: string; active_snapshot_version: number;
  environment_version?: EnvironmentVersion | null;
  lark_folder_token: string | null; lark_folder_url: string | null;
  progress: { accepted: number; terminal: number; active: number };
  snapshots: RunSnapshot[]; node_runs: NodeRun[]; artifacts: ArtifactVersion[];
}
export interface RunEvent {
  cursor: number; flow_run_id: string; node_run_id?: string | null; attempt_id?: string | null;
  event_type: string; payload: Record<string, unknown>; occurred_at: string;
}
export interface ArtifactInput {
  field_key: string; artifact_type: 'URL'; uri: string;
  mime_type?: string; metadata?: Record<string, unknown>;
}


export interface MessageAttachmentInput {
  filename: string; mime_type: string; content_base64: string; byte_size: number;
}
export interface FlowRunConversation {
  id: string;
  flow_run_id: string;
  runtime_session_id: string;
  openhands_conversation_id: string;
  display_label?: string | null;
  created_at: string;
  last_connected_at: string;
}
export interface OpenHandsConversationEvent {
  id: string;
  event_type: 'MESSAGE' | 'TOOL_CALL' | 'TOOL_RESULT' | 'THOUGHT' | 'STATE' | 'ERROR' | 'COMPLETED' | 'CONDENSATION_REQUESTED' | 'CONDENSATION_COMPLETED';
  payload: {
    source_type?: string;
    source?: string | null;
    parent_id?: string | null;
    action_id?: string;
    tool_call_id?: string;
    tool_name?: string;
    content?: string;
    thought?: string;
    summary?: string;
    timestamp?: string;
    event_name?: string;
    details?: Record<string, unknown>;
    display_content?: string;
    attachments?: AgentAttachment[];
    [key: string]: unknown;
  };
}
export interface OpenHandsConversationEventBatch {
  events: OpenHandsConversationEvent[];
  next_cursor?: string | null;
  result?: { status?: string; final_message?: string | null; error?: string | null } | null;
}
/**
 * Host-neutral data consumed by the shared Agent session workbench. A host
 * adapter may expose the current Agent Workspace, a future node scope, or
 * another authorized session scope without changing the workbench contract.
 */
export interface AgentSessionHostDetails {
  id: string;
  display_name: string;
  default_model_provider_id?: string | null;
  desired_state: 'RUNNING' | 'MAINTENANCE';
  updated_at: string;
}
export type AgentWorkspace = AgentSessionHostDetails;

export interface AgentSessionRuntime {
  state: 'ACTIVE' | 'RECOVERING';
  write_available: boolean;
  message?: string | null;
  updated_at: string;
}
export type AgentWorkspaceRuntime = AgentSessionRuntime;

export interface AgentSessionCapability {
  id: string;
  capability_type: 'SKILL' | 'MCP' | 'PLUGIN';
  capability_key: string;
  digest: string;
}
export type AgentWorkspaceCapability = AgentSessionCapability;

export interface AgentSessionMcpReadiness {
  state: 'READY' | 'UNAVAILABLE';
  error_kind: 'timeout' | 'connection' | 'unknown' | null;
  checked_at: string;
}
export type AgentWorkspaceMcpReadiness = AgentSessionMcpReadiness;

export interface AgentConversationInputReadiness { ready: boolean }
export interface AgentPendingConfirmationAction {
  action_id: string; tool_call_id: string; tool_name: string; arguments: Record<string, unknown>;
  security_risk: string; summary: string; digest: string;
}
export interface AgentPendingConfirmation {
  pending: boolean; pending_actions_digest?: string; cursor?: string | null;
  actions?: AgentPendingConfirmationAction[];
}
export interface AgentAttachment {
  filename: string; mime_type: string; byte_size: number; path: string; image_data_url?: string | null;
}
export interface AgentConversationContext {
  used_tokens?: number | null; window_tokens?: number | null; cumulative_tokens?: number | null;
  provider_id?: string | null;
  model_name?: string | null; reasoning_effort?: string | null;
  condenser_max_size?: number | null;
  usage_current?: boolean;
  proactive_compaction_ratio?: number;
  proactive_compaction_tokens?: number | null;
  compaction_policy_current?: boolean;
}
export interface AgentConversation {
  id: string;
  display_title?: string | null;
  title_state?: 'PENDING' | 'GENERATED' | 'MANUAL' | 'FALLBACK';
  model_provider_id?: string | null;
  model_name?: string | null;
  reasoning_effort?: string | null;
  work_directory_id?: string | null;
  work_directory_version_id?: string | null;
  working_directory?: string | null;
  capabilities?: AgentSessionCapability[];
  streaming_callback_ready: boolean;
  lifecycle: 'PROVISIONING' | 'ACTIVE' | 'DELETE_PENDING' | 'FAILED';
  created_at: string;
  updated_at: string;
  last_connected_at?: string | null;
}
export interface AgentSessionWorkDirectory {
  id: string;
  display_name: string;
  state: 'ACTIVE' | 'ARCHIVED';
  current_version: {
    id: string;
    version: number;
    selected_paths: string[];
    working_directory: string;
  };
}
export type AgentWorkDirectory = AgentSessionWorkDirectory;

export interface AgentSessionWorkDirectoryList {
  root: { kind: 'ROOT'; display_name: string; working_directory: string };
  items: AgentSessionWorkDirectory[];
}
export type AgentWorkDirectoryList = AgentSessionWorkDirectoryList;

export interface AgentSessionWorkspaceDetails {
  root: string;
  scope: { kind: 'ROOT' | 'WORK_DIRECTORY'; id?: string; display_name: string };
  working_directory: string;
  work_directory?: AgentSessionWorkDirectory | null;
  files: Array<{ path: string; kind: 'file' | 'directory'; size: number }>;
  repositories: Array<{ path: string; remote?: string; branch?: string; head?: string }>;
  runtime: { container_id?: string | null };
  ide: { workspace_path: string; gateway: { supported: boolean; status: string; note: string } };
}
export type AgentWorkspaceDetails = AgentSessionWorkspaceDetails;
export interface FlowRunRuntimeGeneration {
  generation: number;
  state: 'PROVISIONING' | 'READY' | 'DRAINING' | 'STOPPED' | 'DELETED' | 'FAILED';
  row_version: number;
  failure_code?: string | null;
  failure_summary?: string | null;
  started_at?: string | null;
  ready_at?: string | null;
  draining_at?: string | null;
  stopped_at?: string | null;
  deleted_at?: string | null;
}
export interface FlowRunRuntimeOverview {
  flow_run_id: string;
  runtime_session_id?: string | null;
  status: 'NOT_STARTED' | 'ARCHIVED' | 'STARTING' | 'ACTIVE' | 'REPLACING' | 'RECONNECTING' | 'DEGRADED' | 'STOPPED' | 'DELETING';
  connection_state: 'NOT_STARTED' | 'READY' | 'ARCHIVED' | 'STARTING' | 'REPLACING' | 'RECONNECTING' | 'DEGRADED' | 'STOPPED' | 'DELETING';
  active_generation?: number | null;
  replacement_generation?: number | null;
  session_row_version?: number | null;
  write_available: boolean;
  read_only: boolean;
  rerun_required: boolean;
  diagnostic_code?: string | null;
  diagnostic_summary?: string | null;
  generations: FlowRunRuntimeGeneration[];
  retention: {
    mode: 'FLOW_RUN_LIFETIME';
    workspace_preserved_during_replacement: boolean;
    physical_delete_operation: 'DELETE_FLOW_RUN';
  };
}
