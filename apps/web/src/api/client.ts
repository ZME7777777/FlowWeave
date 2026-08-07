import type {
  AgentConversation, AgentMessage, ArtifactInput, CapabilityImportResult, FlowDefinition, FlowRun, FlowRunSummary, FlowWrite, MessageAttachmentInput,
  ModelProvider, ModelProviderWrite, NodeAsset, NodeAssetWrite, NodeAttempt,
  NodeDirectory, NodeRun, RunEvent,
} from '../types';

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '';
const ROOT = '/api/v1';
export const randomId = () => typeof crypto.randomUUID === 'function'
  ? crypto.randomUUID()
  : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly details: Record<string, unknown>,
    public readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function responseError(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => ({}));
  return new ApiError(
    body?.error?.message ?? response.statusText ?? '请求失败',
    body?.error?.code ?? 'REQUEST_FAILED',
    body?.error?.details ?? {},
    response.status,
  );
}
export const artifactContentUrl = (artifactId: string, download = false) =>
  `${API_BASE}${ROOT}/artifact-versions/${artifactId}/content${download ? '?download=true' : ''}`;
export const workspaceImageUrl = (messageId: string, source: string) =>
  `${API_BASE}${ROOT}/agent-messages/${messageId}/workspace-image?source=${encodeURIComponent(source)}`;
export const messageAttachmentUrl = (messageId: string, attachmentId: string, download = false) =>
  `${API_BASE}${ROOT}/agent-messages/${messageId}/attachments/${attachmentId}${download ? '?download=true' : ''}`;

async function requestText(path: string): Promise<string> {
  const response = await fetch(`${API_BASE}${ROOT}${path}`);
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.text();
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${ROOT}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
const json = (method: string, body?: unknown, idempotent = false): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
  headers: idempotent ? { 'Idempotency-Key': randomId() } : undefined,
});

export const api = {
  directories: () => request<NodeDirectory[]>('/node-directories'),
  createDirectory: (body: { name: string; parent_id?: string | null; position?: number }) =>
    request<NodeDirectory>('/node-directories', json('POST', body)),
  nodes: (directoryId?: string) => request<NodeAsset[]>(`/node-assets${directoryId ? `?directory_id=${directoryId}` : ''}`),
  node: (id: string) => request<NodeAsset>(`/node-assets/${id}`),
  createNode: (body: NodeAssetWrite) => request<NodeAsset>('/node-assets', json('POST', body)),
  updateNode: (id: string, body: NodeAssetWrite) => request<NodeAsset>(`/node-assets/${id}`, json('PUT', body)),
  deleteNode: (id: string) => request<void>(`/node-assets/${id}`, json('DELETE')),
  deleteNodes: (ids: string[]) => request<void>('/node-assets', json('DELETE', { ids })),
  validateCapability: (body: { capability_type: string; filename: string; content_base64: string }) =>
    request<{ import_token: string; preview: unknown; content_hash: string }>('/capability-imports/validate', json('POST', body)),
  commitCapability: (import_token: string) => request<CapabilityImportResult>('/capability-imports', json('POST', { import_token })),

  providers: () => request<ModelProvider[]>('/model-providers'),
  createProvider: (body: ModelProviderWrite) => request<ModelProvider>('/model-providers', json('POST', body)),
  updateProvider: (id: string, body: ModelProviderWrite) => request<ModelProvider>(`/model-providers/${id}`, json('PUT', body)),
  deleteProvider: (id: string) => request<void>(`/model-providers/${id}`, json('DELETE')),
  deleteProviders: (ids: string[]) => request<void>('/model-providers', json('DELETE', { ids })),
  testProvider: (id: string) => request<{ connection_state: string; model_count: number }>(`/model-providers/${id}/test`, json('POST')),
  discoverProviderModels: (id: string) => request<{ models: string[] }>(`/model-providers/${id}/discover-models`, json('POST')),

  flows: () => request<FlowDefinition[]>('/flows'),
  flow: (id: string) => request<FlowDefinition>(`/flows/${id}`),
  createFlow: (body: FlowWrite) => request<FlowDefinition>('/flows', json('POST', body)),
  updateFlow: (id: string, body: FlowWrite) => request<FlowDefinition>(`/flows/${id}`, json('PUT', body)),
  validateFlow: (id: string) => request<{ valid: boolean; errors: unknown[] }>(`/flows/${id}/validate`, json('POST')),
  deleteFlow: (id: string) => request<void>(`/flows/${id}`, json('DELETE')),

  runFlow: (flowId: string, body: { name?: string; flow_node_key?: string; artifacts?: ArtifactInput[]; input_bindings?: Record<string, string> }) =>
    request<FlowRun>(`/flows/${flowId}/runs`, json('POST', body)),
  runs: () => request<FlowRunSummary[]>('/flow-runs'),
  flowRun: (id: string) => request<FlowRun>(`/flow-runs/${id}`),
  deleteRun: (id: string) => request<void>(`/flow-runs/${id}`, json('DELETE')),
  nodeRun: (runId: string, nodeRunId: string) => request<NodeRun>(`/flow-runs/${runId}/nodes/${nodeRunId}`),
  artifactContent: (artifactId: string) => requestText(`/artifact-versions/${artifactId}/content`),
  activateNode: (runId: string, key: string, artifact_ids: Record<string, string>) =>
    request<NodeRun>(`/flow-runs/${runId}/nodes/${key}/runs`, json('POST', { artifact_ids })),
  bindInputs: (attemptId: string, bindings: Record<string, string>, version?: number) =>
    request<NodeAttempt>(`/node-attempts/${attemptId}/input-bindings`, json('PUT', { bindings, expected_state_version: version })),
  confirmStart: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/confirm-start`, json('POST', { expected_state_version: version }, true)),
  humanInput: (attemptId: string, content: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/human-input`, json('POST', { content, expected_state_version: version }, true)),
  acceptAttempt: (attemptId: string, version: number) => request<FlowRun>(`/node-attempts/${attemptId}/accept`, json('POST', { expected_state_version: version }, true)),
  rejectAttempt: (attemptId: string, reason: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/reject`, json('POST', { reason, copy_input_bindings: true, expected_state_version: version }, true)),
  retryGates: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/retry-gates`, json('POST', { expected_state_version: version })),
  retryRuntimeCancel: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/retry-runtime-cancel`, json('POST', { expected_state_version: version }, true)),
  syncSnapshot: (runId: string, version: number) => request<FlowRun>(`/flow-runs/${runId}/sync-snapshot`, json('POST', { expected_active_version: version }, true)),
  completeRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/complete`, json('POST', undefined, true)),
  cancelRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/cancel`, json('POST', undefined, true)),
  conversations: (attemptId: string) => request<AgentConversation[]>(`/node-attempts/${attemptId}/conversations`),
  createConversation: (attemptId: string, version: number, title?: string) =>
    request<AgentConversation>(`/node-attempts/${attemptId}/conversations`, json('POST', {
      title, expected_attempt_state_version: version, baseline: { include_current_artifacts: true },
    }, true)),
  conversation: (conversationId: string) => request<AgentConversation>(`/agent-conversations/${conversationId}`),
  deleteConversation: (conversationId: string) => request<void>(`/agent-conversations/${conversationId}`, json('DELETE')),
  conversationMessages: (conversationId: string, afterSequence = 0) =>
    request<AgentMessage[]>(`/agent-conversations/${conversationId}/messages?after_sequence=${afterSequence}&limit=200`),
  sendConversationMessage: (conversationId: string, content: string, version: number, capabilityRefs: Array<{ capability_type: 'SKILL' | 'MCP'; capability_key: string }> = [], attachments: MessageAttachmentInput[] = [], clientMessageId = randomId()) =>
    request<AgentMessage>(`/agent-conversations/${conversationId}/messages`, json('POST', {
      client_message_id: clientMessageId,
      content: [
        ...(content ? [{ type: 'text', text: content }] : []),
        ...attachments.map(item => ({ type: 'attachment', filename: item.filename, mime_type: item.mime_type, content_base64: item.content_base64 })),
      ],
      capability_refs: capabilityRefs,
      delivery_mode: 'QUEUE_AFTER_TURN',
      expected_conversation_version: version,
    }, true)),
  steerConversationMessage: (messageId: string) =>
    request<AgentMessage>(`/agent-messages/${messageId}/steer`, json('POST', undefined, true)),
  cancelQueuedConversationMessage: (messageId: string) =>
    request<AgentMessage>(`/agent-messages/${messageId}/cancel-queued`, json('POST', undefined, true)),
  retryConversationMessage: (messageId: string) =>
    request<AgentMessage>(`/agent-messages/${messageId}/retry`, json('POST', undefined, true)),
  flowEvents: (runId: string, after = 0) => request<RunEvent[]>(`/flow-runs/${runId}/event-history?after=${after}`),
};

export function subscribeToRun(runId: string, onEvent: () => void): () => void {
  const source = new EventSource(`${API_BASE}${ROOT}/flow-runs/${runId}/events`);
  source.onmessage = onEvent;
  ['ATTEMPT_CREATED', 'HUMAN_CONFIRM_REQUIRED', 'ARTIFACT_VERSION_CREATED', 'NODE_RUN_ACCEPTED', 'SNAPSHOT_SYNCED', 'FLOW_RUN_COMPLETED', 'CONVERSATION_CREATED', 'CONVERSATION_STATE_CHANGED', 'AGENT_MESSAGE_CREATED', 'AGENT_MESSAGE_DELIVERY_CHANGED'].forEach(type => source.addEventListener(type, onEvent));
  return () => source.close();
}
