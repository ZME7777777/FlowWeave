import type {
  ArtifactInput, ArtifactVersion, CapabilityImportResult, FlowDefinition, FlowRun, FlowRunSummary, FlowWrite,
  ModelProvider, ModelProviderWrite, NodeAsset, NodeAssetWrite, NodeAttempt,
  NodeDirectory, NodeRun, RunEvent,
} from '../types';

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '';
const ROOT = '/api/v1';
const TOKEN_KEY = 'flowweave-human-write-token';
export const AUTH_REQUIRED_EVENT = 'flowweave-auth-required';
export interface AuthRequestDetail { resolve: () => void; reject: (reason: Error) => void }
let pendingAuthentication: Promise<void> | undefined;

function requireAuthentication(): Promise<void> {
  if (!pendingAuthentication) {
    pendingAuthentication = new Promise<void>((resolve, reject) => {
      window.dispatchEvent(new CustomEvent<AuthRequestDetail>(AUTH_REQUIRED_EVENT, {
        detail: { resolve, reject },
      }));
    }).finally(() => { pendingAuthentication = undefined; });
  }
  return pendingAuthentication;
}
export const getHumanWriteToken = () => sessionStorage.getItem(TOKEN_KEY);
export const clearHumanWriteToken = () => sessionStorage.removeItem(TOKEN_KEY);

export const artifactContentUrl = (artifactId: string, download = false) =>
  `${API_BASE}${ROOT}/artifact-versions/${artifactId}/content${download ? '?download=true' : ''}`;

async function requestText(path: string): Promise<string> {
  const response = await fetch(`${API_BASE}${ROOT}${path}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? response.statusText ?? '请求失败');
  }
  return response.text();
}
export async function verifyHumanWriteToken(token: string): Promise<void> {
  const response = await fetch(`${API_BASE}${ROOT}/auth/verify`, {
    method: 'POST', headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error('人工操作令牌无效');
  sessionStorage.setItem(TOKEN_KEY, token);
}

async function request<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const write = !['GET', 'HEAD', 'OPTIONS'].includes((init.method ?? 'GET').toUpperCase());
  if (write && !getHumanWriteToken()) await requireAuthentication();
  const token = getHumanWriteToken();
  const response = await fetch(`${API_BASE}${ROOT}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
  if (response.status === 401 && write && !retried) {
    clearHumanWriteToken(); await requireAuthentication(); return request<T>(path, init, true);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? response.statusText ?? '请求失败');
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
const json = (method: string, body?: unknown, idempotent = false): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
  headers: idempotent ? { 'Idempotency-Key': crypto.randomUUID() } : undefined,
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
  validateCapability: (body: { capability_type: string; filename: string; content_base64: string }) =>
    request<{ import_token: string; preview: unknown; content_hash: string }>('/capability-imports/validate', json('POST', body)),
  commitCapability: (import_token: string) => request<CapabilityImportResult>('/capability-imports', json('POST', { import_token })),

  providers: () => request<ModelProvider[]>('/model-providers'),
  createProvider: (body: ModelProviderWrite) => request<ModelProvider>('/model-providers', json('POST', body)),
  updateProvider: (id: string, body: ModelProviderWrite) => request<ModelProvider>(`/model-providers/${id}`, json('PUT', body)),
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
  createArtifact: (runId: string, body: ArtifactInput) => request<ArtifactVersion>(`/flow-runs/${runId}/artifacts`, json('POST', body)),
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
  syncSnapshot: (runId: string, version: number) => request<FlowRun>(`/flow-runs/${runId}/sync-snapshot`, json('POST', { expected_active_version: version }, true)),
  completeRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/complete`, json('POST', undefined, true)),
  cancelRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/cancel`, json('POST', undefined, true)),
  flowEvents: (runId: string, after = 0) => request<RunEvent[]>(`/flow-runs/${runId}/event-history?after=${after}`),
};

export function subscribeToRun(runId: string, onEvent: () => void): () => void {
  const source = new EventSource(`${API_BASE}${ROOT}/flow-runs/${runId}/events`);
  source.onmessage = onEvent;
  ['ATTEMPT_CREATED', 'HUMAN_CONFIRM_REQUIRED', 'ARTIFACT_VERSION_CREATED', 'NODE_RUN_ACCEPTED', 'SNAPSHOT_SYNCED', 'FLOW_RUN_COMPLETED'].forEach(type => source.addEventListener(type, onEvent));
  return () => source.close();
}
