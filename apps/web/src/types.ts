export type ViewName = 'nodes' | 'models' | 'flows' | 'runs' | 'workbench';

export interface NodeDirectory {
  id: string; parent_id?: string | null; name: string; position: number; row_version: number;
}
export type ArtifactDataType =
  | 'TEXT' | 'MARKDOWN' | 'JSON_OBJECT' | 'JSON_ARRAY'
  | 'FILE' | 'FILE_COLLECTION' | 'DOCUMENT' | 'URL' | 'REPOSITORY_REF';
export interface IOField {
  id?: string; field_key: string; display_name: string; data_type: ArtifactDataType;
  description: string; position?: number;
}
export interface ExecutorConfig {
  model_provider_id?: string | null; model_name?: string | null;
  startup_prompt: string; context_prompt: string; timeout_seconds: number; max_iterations: number;
}
export interface CapabilityRef {
  id?: string; capability_type: 'SKILL' | 'MCP' | 'HOOK'; capability_key: string;
  normalized_config: Record<string, unknown>; position?: number;
}
export interface CapabilityImportResult {
  id: string; capability_type: CapabilityRef['capability_type']; filename: string;
  content_hash: string; storage_key: string; capabilities: CapabilityRef[];
}
export interface NodeAsset {
  id: string; directory_id?: string | null; name: string; description: string;
  icon_kind: string; icon_value: string; default_skill_ref?: string | null; row_version: number;
  inputs: IOField[]; outputs: IOField[]; executor: ExecutorConfig | null;
  capabilities: CapabilityRef[]; created_at: string; updated_at: string;
}
export interface NodeAssetWrite {
  directory_id?: string | null; name: string; description: string;
  icon_kind: string; icon_value: string; default_skill_ref: string;
  row_version?: number | null; inputs: IOField[]; outputs: IOField[];
  executor: ExecutorConfig; capabilities: CapabilityRef[];
}

export interface ProviderModel {
  id?: string; model_name: string; enabled: boolean; is_default: boolean;
}
export interface ModelProvider {
  id: string; name: string; base_url: string; has_api_key: boolean; api_key_hint?: string | null;
  connection_state: string; reference_node_count: number; available_for_nodes: boolean;
  row_version: number; models: ProviderModel[];
  created_at: string; updated_at: string;
}
export interface ModelProviderWrite {
  name: string; base_url: string; api_key?: string | null; row_version?: number | null;
  models: ProviderModel[];
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
export interface EdgeMapping { source_output_key: string; target_input_key: string }
export interface FlowEdge {
  id?: string; source_instance_key: string; target_instance_key: string;
  position: number; mappings: EdgeMapping[];
}
export interface FlowDefinition {
  id: string; name: string; description: string; default_entry_key?: string | null;
  row_version: number; nodes: FlowNode[]; edges: FlowEdge[];
  created_at: string; updated_at: string;
}
export interface FlowWrite {
  name: string; description: string; default_entry_key?: string | null;
  row_version?: number | null; nodes: FlowNode[]; edges: FlowEdge[];
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
  runtime_job_id?: string | null; conversation_id?: string | null; runtime_cursor?: string | null;
  workspace_ref?: string | null; error_code?: string | null; error_detail?: string | null;
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
  current_node_key?: string | null; current_node_name?: string | null;
  current_attempt_state?: AttemptState | null; has_pending_action: boolean;
  progress: { accepted: number; terminal: number; active: number };
  started_at: string; updated_at: string; finished_at?: string | null;
}
export interface FlowRun extends FlowRunSummary {
  row_version: number; active_snapshot_id: string; active_snapshot_version: number;
  progress: { accepted: number; terminal: number; active: number };
  snapshots: RunSnapshot[]; node_runs: NodeRun[]; artifacts: ArtifactVersion[];
}
export interface RunEvent {
  cursor: number; flow_run_id: string; node_run_id?: string | null; attempt_id?: string | null;
  event_type: string; payload: Record<string, unknown>; occurred_at: string;
}
export interface ArtifactInput {
  field_key: string; artifact_type: string; inline_content?: string; uri?: string;
  mime_type?: string; metadata?: Record<string, unknown>;
}
