export type ViewName = 'nodes' | 'capabilities' | 'environments' | 'models' | 'flows' | 'runs' | 'workbench' | 'agent-chat';

export interface NodeDirectory {
  id: string; parent_id?: string | null; name: string; position: number; row_version: number;
}
export type ArtifactDataType = 'URL';
export interface IOField {
  id?: string; field_key: string; display_name: string; data_type: ArtifactDataType;
  description: string; template_url: string; position?: number;
}
export interface ExecutorConfig {
  model_provider_id?: string | null; model_name?: string | null;
  startup_prompt: string; context_prompt: string; timeout_seconds: number; max_iterations: number;
}
export type CapabilityAssetType = 'SKILL' | 'MCP' | 'HOOK';
export interface CapabilityRef {
  id?: string; capability_id?: string; capability_type: CapabilityAssetType; capability_key: string;
  normalized_config: Record<string, unknown>; position?: number;
}
export interface CapabilityAsset {
  id: string; lineage_id: string; revision_number: number; is_latest: boolean;
  capability_type: CapabilityAssetType; capability_key: string;
  description: string; version: string; filename: string; content_hash: string;
  byte_size: number; import_id: string; created_at: string; reference_count: number;
  dependencies: Record<string, Record<string, string>>;
  dependency_build_state: 'NOT_REQUIRED' | 'PENDING' | 'READY' | 'FAILED';
  dependency_build_error?: string | null;
}
export interface SkillCollection {
  id: string; name: string; category: string; description: string; row_version: number;
  members: CapabilityAsset[]; created_at: string; updated_at: string;
}
export interface SkillCollectionWrite {
  name: string; category: string; description: string; capability_ids: string[];
  row_version?: number | null;
}
export interface SkillSource {
  id: string; capability_key: string; filename: string; entry: string; content: string;
}
export interface CapabilityImportResult {
  id: string; capability_type: CapabilityAssetType; filename: string;
  content_hash: string; storage_key: string; capabilities: CapabilityAsset[];
}
export interface NamedDeleteReference { id: string; name: string }
export interface BlockedCapabilityDelete {
  id: string; name: string; relation: 'NODE_CAPABILITY' | 'SKILL_COLLECTION';
  nodes: NamedDeleteReference[]; collections?: NamedDeleteReference[];
}
export interface BlockedNodeDelete {
  id: string; name: string; relation: 'FLOW_NODE';
  flows: Array<NamedDeleteReference & { reference_count: number }>;
}
export interface BlockedProviderDelete {
  id: string; name: string; relation: 'NODE_EXECUTOR'; nodes: NamedDeleteReference[];
}
export interface BulkDeleteResult<T> { deleted_ids: string[]; blocked: T[] }
export interface EnvironmentVersion {
  id: string; environment_id: string; version_no: number; parent_version_id?: string | null;
  state: 'PUBLISHING' | 'READY' | 'FAILED'; image_reference: string; image_digest: string;
  manifest: { commands?: Record<string, string>; [key: string]: unknown };
  error_detail?: string | null; node_reference_count: number; run_reference_count: number;
  reference_count: number; created_at: string;
}
export interface EnvironmentSetupSession {
  id: string; environment_id: string; base_version_id?: string | null;
  state: 'STARTING' | 'RUNNING' | 'PUBLISHED' | 'FAILED' | 'CANCELLED' | 'EXPIRED';
  base_image_reference: string; expires_at: string; error_detail?: string | null;
}
export interface TerminalEnvironment {
  id: string; name: string; description: string; base_image: string; row_version: number;
  versions: EnvironmentVersion[]; active_sessions: EnvironmentSetupSession[];
  created_at: string; updated_at: string;
}
export interface TerminalEnvironmentWrite {
  name: string; description: string; base_image: string; row_version?: number | null;
}
export interface NodeAsset {
  id: string; directory_id?: string | null; name: string; description: string;
  icon_kind: string; icon_value: string; row_version: number;
  environment_version_id?: string | null; environment_version?: EnvironmentVersion | null;
  workspace_ref?: string;
  inputs: IOField[]; outputs: IOField[]; executor: ExecutorConfig | null;
  capabilities: CapabilityRef[]; created_at: string; updated_at: string;
}
export interface NodeAssetWrite {
  directory_id?: string | null; name: string; description: string;
  icon_kind: string; icon_value: string;
  environment_version_id?: string | null;
  row_version?: number | null; inputs: IOField[]; outputs: IOField[];
  executor: ExecutorConfig; capabilities: CapabilityRef[];
}

export interface ProviderModel {
  id?: string; model_name: string; enabled: boolean; is_default: boolean;
  default_reasoning_effort?: string | null; supported_reasoning_efforts?: string[];
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
  | 'EXECUTING' | 'WAITING_HUMAN' | 'END_GATES' | 'END_BLOCKED'
  | 'WAITING_ACCEPTANCE' | 'ACCEPTED' | 'REJECTED' | 'CANCELLED';
export interface NodeAttempt {
  id: string; node_run_id: string; attempt_no: number; snapshot_id: string;
  state: AttemptState; state_version: number; runtime_phase?: string | null;
  runtime_adapter?: string | null;
  runtime_job_id?: string | null; conversation_id?: string | null; runtime_cursor?: string | null;
  workspace_ref?: string | null; error_code?: string | null; error_detail?: string | null;
  startup_mode?: 'SKILL' | 'PROMPT'; startup_capability_key?: string | null;
  startup_prompt?: string | null;
  model_name?: string | null; reasoning_effort?: string | null;
  output_targets?: Record<string, { url: string; token: string; template_url: string; title: string }>;
  input_bindings: InputBinding[]; artifacts: ArtifactVersion[];
  gate_evaluations: GateEvaluation[]; created_at: string; updated_at: string;
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


export type ConversationState = 'CREATING' | 'IDLE' | 'GENERATING' | 'STOPPING' | 'WAITING_HUMAN' | 'WAITING_SUBAGENTS' | 'FAILED' | 'READ_ONLY';
export type MessageDeliveryState = 'QUEUED' | 'DELIVERING' | 'DELIVERED' | 'FAILED' | 'CANCELLED';
export interface AgentMessageTextPart { type: 'text'; text: string }
export interface AgentMessageAttachmentPart {
  type: 'attachment'; attachment_id: string; filename: string; mime_type: string;
  byte_size: number; content_hash: string; runtime_path: string;
}
export type AgentMessagePart = AgentMessageTextPart | AgentMessageAttachmentPart;
export interface MessageAttachmentInput {
  filename: string; mime_type: string; content_base64: string; byte_size: number;
}
export interface AgentMessageContent {
  parts?: AgentMessagePart[];
  tool?: Record<string, unknown>;
  state?: Record<string, unknown>;
  error?: Record<string, unknown>;
  presentation?: 'final' | 'progress' | 'question' | 'queued' | 'cancelled-queue' | 'chat';
  capability_refs?: Array<Pick<CapabilityRef, 'capability_type' | 'capability_key'>>;
  runtime_selection?: { model_name?: string | null; reasoning_effort?: string | null };
  [key: string]: unknown;
}
export interface AgentMessage {
  id: string; conversation_id: string; sequence_no: number;
  source: 'PROGRAM' | 'HUMAN' | 'AGENT'; transport_role: 'user' | 'assistant';
  message_type: 'TEXT' | 'TOOL_CALL' | 'TOOL_RESULT' | 'STATE' | 'ERROR';
  content: AgentMessageContent; delivery_state: MessageDeliveryState;
  delivery_mode?: 'QUEUE_AFTER_TURN' | 'INTERRUPT_AND_RESUME' | null; client_message_id?: string | null;
  runtime_cursor?: string | null; error_code?: string | null; error_detail?: string | null;
  created_by?: string | null; created_at: string; delivered_at?: string | null;
  conversation_state_version?: number;
}
export interface AgentConversation {
  id: string; parent_conversation_id?: string | null; attempt_id: string; conversation_no: number;
  kind: 'AUTO' | 'HUMAN_CREATED' | 'SUBAGENT'; title: string; state: ConversationState;
  editable_message_id?: string | null;
  delegation_batch_key?: string | null; delegation_instruction?: string | null;
  state_version: number; model_name?: string | null; reasoning_effort?: string | null; runtime_job_id?: string | null; runtime_conversation_id?: string | null; runtime_adapter?: string | null;
  runtime_resource?: {
    sandbox_id: string; container_name: string; owner_type: 'ATTEMPT' | 'CONVERSATION'; owner_id: string;
    desired_state: string; observed_state: string; lifecycle: 'RUNNING' | 'DELETING' | 'DELETED' | 'ERROR';
    cleanup_policy: 'DELETE_WITH_CONVERSATION' | 'DELETE_WITH_ATTEMPT';
  } | null;
  connection_status?: { phase: 'WAITING_WORKER' | 'PREPARING_CONTEXT' | 'STARTING_RUNTIME' | 'CONNECTING_AGENT' | 'READY' | 'FAILED'; started_at: string; elapsed_seconds?: number; detail?: string | null };
  context_baseline: Record<string, unknown>; message_count: number;
  last_message?: AgentMessage | null; created_at: string; updated_at: string;
}
