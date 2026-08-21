import type {
  AgentConversation, AgentMessage, AgentProfileBinding, AgentProfileSwitchPreview, AgentProfileSwitchResult, AgentProfileVersion, ArtifactInput, ArtifactVersion, CapabilityAsset, CapabilityImportResult, FlowDefinition, FlowRun, FlowRunSummary, FlowWrite, MessageAttachmentInput, SkillSource,
  BlockedCapabilityDelete, BlockedNodeDelete, BlockedProviderDelete, BulkDeleteResult, CodexDeviceAuthorization, CodexOAuthStatus, ModelProvider, ModelProviderDiscoveryWrite, ModelProviderWrite, NodeAsset, NodeAssetWrite, NodeAttempt,
  CapabilityCollection, CapabilityCollectionWrite, MarketplaceCatalog, NodeDirectory, NodeRun, PluginSourceResolution, RunEvent, RuntimeConfirmationBatch, RuntimeDiagnosticQuery, RuntimeGoalCommand, RuntimeSubagentTask, TerminalEnvironment, TerminalEnvironmentWrite, EnvironmentSetupSession, EnvironmentVersion, ToolPolicyCatalog,
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
  const code = body?.error?.code ?? 'REQUEST_FAILED';
  const backendMessage = typeof body?.error?.message === 'string' ? body.error.message : '';
  const chineseMessages: Record<string, string> = {
    FLOW_NAME_CONFLICT: '流程名称已存在，请使用其他名称。',
    NODE_ASSET_NAME_CONFLICT: '当前目录已存在同名节点资产，请使用其他名称。',
    FLOW_IN_USE: '该流程仍有关联运行，请先删除关联运行后再永久删除流程。',
    NODE_ASSET_IN_USE: '该节点仍被流程引用，请先从相关流程中移除节点。',
    CAPABILITY_IN_USE: '该能力仍被节点引用，请先解除引用后再删除。',
    ENVIRONMENT_VERSION_IN_USE: '该环境版本仍被运行引用，暂时不能删除。',
    ENVIRONMENT_RUNTIME_INCOMPATIBLE: '该终端镜像版本缺少当前运行契约信息，请在“终端环境”中基于此版本重新发布后再创建运行。',
    RESOURCE_NOT_FOUND: '请求的资源不存在或已被删除，请刷新页面后重试。',
    VERSION_CONFLICT: '数据已被其他操作修改，请刷新页面后重试。',
    DATA_CONFLICT: '提交的数据与现有记录冲突，请检查是否存在重名或重复关联。',
    INVALID_COMMAND: '提交的数据不符合要求，请检查必填项和输入格式。',
    FLOW_GRAPH_INVALID: '流程配置无效，请检查节点、连线、端口映射和门禁设置。',
    ILLEGAL_STATE_TRANSITION: '当前状态不允许执行此操作，请刷新页面确认最新状态。',
    EXECUTOR_UNAVAILABLE: '执行服务暂时不可用，请稍后重试。',
    REQUEST_FAILED: '请求处理失败，请稍后重试。',
  };
  const message = /[\u3400-\u9fff]/u.test(backendMessage)
    ? backendMessage
    : chineseMessages[code]
      ?? (response.status >= 500
        ? '服务器处理请求时发生错误，请稍后重试。'
        : response.status === 403
          ? '当前账号没有执行此操作的权限。'
          : response.status === 401
            ? '登录状态已失效，请重新登录。'
            : response.status === 404
              ? '请求的资源不存在或已被删除。'
              : '请求处理失败，请检查输入后重试。');
  return new ApiError(
    message,
    code,
    body?.error?.details ?? {},
    response.status,
  );
}
export const artifactContentUrl = (artifactId: string, download = false) =>
  `${API_BASE}${ROOT}/artifact-versions/${artifactId}/content${download ? '?download=true' : ''}`;
export const workspaceImageUrl = (messageId: string, source: string) =>
  `${API_BASE}${ROOT}/agent-messages/${messageId}/workspace-image?source=${encodeURIComponent(source)}&v=2`;
export const messageAttachmentUrl = (messageId: string, attachmentId: string, download = false) =>
  `${API_BASE}${ROOT}/agent-messages/${messageId}/attachments/${attachmentId}${download ? '?download=true' : ''}`;

async function requestText(path: string): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${ROOT}${path}`);
  } catch {
    throw new ApiError('无法连接服务器，请检查网络连接后重试。', 'NETWORK_ERROR', {}, 0);
  }
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.text();
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${ROOT}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init.headers },
    });
  } catch {
    throw new ApiError('无法连接服务器，请检查网络连接后重试。', 'NETWORK_ERROR', {}, 0);
  }
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
  deleteNodes: (ids: string[]) => request<BulkDeleteResult<BlockedNodeDelete>>('/node-assets', json('DELETE', { ids })),
  validateCapability: (body: { capability_type: string; filename: string; content_base64: string; mcp_scripts?: Array<{ server: string; filename: string; content_base64: string }>; hook_scripts?: Array<{ filename: string; content_base64: string }> }) =>
    request<{
      import_token: string;
      preview: {
        capabilities?: Array<{ capability_key?: string }>;
        file_count?: number;
        raw_entry_count?: number;
        effective_entry_count?: number;
        ignored_entry_count?: number;
      };
      content_hash: string;
    }>('/capability-imports/validate', json('POST', body)),
  commitCapability: (import_token: string) => request<CapabilityImportResult>('/capability-imports', json('POST', { import_token })),
  createPluginSourceResolution: (body: { source_url: string; commit: string; repo_path?: string | null }) =>
    request<PluginSourceResolution>('/plugin-source-resolutions', json('POST', body)),
  previewMarketplaceCatalog: (body: { marketplace_source_url: string; marketplace_commit: string; marketplace_repo_path?: string | null }) =>
    request<MarketplaceCatalog>('/plugin-marketplace-catalogs/preview', json('POST', body)),
  createMarketplacePluginResolution: (body: { marketplace_source_url: string; marketplace_commit: string; marketplace_repo_path?: string | null; plugin_name: string }) =>
    request<PluginSourceResolution>('/plugin-source-resolutions/marketplace', json('POST', body)),
  pluginSourceResolution: (id: string) =>
    request<PluginSourceResolution>(`/plugin-source-resolutions/${encodeURIComponent(id)}`),
  publishPluginSourceResolution: (id: string, expectedStateVersion: number) =>
    request<PluginSourceResolution>(`/plugin-source-resolutions/${encodeURIComponent(id)}/publish`, json('POST', { expected_state_version: expectedStateVersion })),
  capabilities: () => request<CapabilityAsset[]>('/capabilities'),
  toolPolicyCatalog: () => request<ToolPolicyCatalog>('/tool-policy-catalog'),
  capabilityCollections: () => request<CapabilityCollection[]>('/capability-collections'),
  createCapabilityCollection: (body: CapabilityCollectionWrite) =>
    request<CapabilityCollection>('/capability-collections', json('POST', body)),
  updateCapabilityCollection: (id: string, body: CapabilityCollectionWrite) =>
    request<CapabilityCollection>(`/capability-collections/${id}`, json('PUT', body)),
  deleteCapabilityCollection: (id: string) =>
    request<void>(`/capability-collections/${id}`, json('DELETE')),
  capabilitySource: (id: string) => request<SkillSource>(`/capabilities/${encodeURIComponent(id)}/source`),
  updateCapabilitySource: (id: string, content: string) =>
    request<CapabilityAsset>(`/capabilities/${encodeURIComponent(id)}/source`, json('PUT', { content })),
  deleteCapability: (id: string) => request<void>(`/capabilities/${encodeURIComponent(id)}`, json('DELETE')),
  deleteCapabilities: (ids: string[]) => request<BulkDeleteResult<BlockedCapabilityDelete>>('/capabilities', json('DELETE', { ids })),
  agentProfileVersions: (packageId: string) =>
    request<AgentProfileVersion[]>(`/agent-profile-packages/${encodeURIComponent(packageId)}/versions`),
  agentProfileBindings: (versionId: string) =>
    request<AgentProfileBinding[]>(`/agent-profiles/${encodeURIComponent(versionId)}/bindings`),

  terminalEnvironments: () => request<TerminalEnvironment[]>('/terminal-environments'),
  createTerminalEnvironment: (body: TerminalEnvironmentWrite) =>
    request<TerminalEnvironment>('/terminal-environments', json('POST', body)),
  updateTerminalEnvironment: (id: string, body: TerminalEnvironmentWrite) =>
    request<TerminalEnvironment>(`/terminal-environments/${id}`, json('PUT', body)),
  deleteTerminalEnvironment: (id: string) => request<void>(`/terminal-environments/${id}`, json('DELETE')),
  deleteEnvironmentVersion: (environmentId: string, versionId: string) =>
    request<void>(`/terminal-environments/${environmentId}/versions/${versionId}`, json('DELETE')),
  createEnvironmentSetup: (id: string, base_version_id?: string) =>
    request<EnvironmentSetupSession>(`/terminal-environments/${id}/setup-sessions`, json('POST', { base_version_id: base_version_id || null })),
  publishEnvironmentSetup: (id: string) =>
    request<EnvironmentVersion>(`/environment-setup-sessions/${id}/publish`, json('POST')),
  stopEnvironmentSetup: (id: string) => request<void>(`/environment-setup-sessions/${id}`, json('DELETE')),

  providers: () => request<ModelProvider[]>('/model-providers'),
  createProvider: (body: ModelProviderWrite) => request<ModelProvider>('/model-providers', json('POST', body)),
  updateProvider: (id: string, body: ModelProviderWrite) => request<ModelProvider>(`/model-providers/${id}`, json('PUT', body)),
  deleteProvider: (id: string) => request<void>(`/model-providers/${id}`, json('DELETE')),
  deleteProviders: (ids: string[]) => request<BulkDeleteResult<BlockedProviderDelete>>('/model-providers', json('DELETE', { ids })),
  testProvider: (id: string) => request<{ connection_state: string; model_count: number }>(`/model-providers/${id}/test`, json('POST')),
  discoverProviderModels: (id: string) => request<{ models: string[]; provider?: ModelProvider }>(`/model-providers/${id}/discover-models`, json('POST')),
  previewProviderModels: (body: ModelProviderDiscoveryWrite) => request<{ models: string[] }>('/model-providers/discover-models', json('POST', body)),
  startCodexOAuth: (id: string) => request<CodexDeviceAuthorization>(`/model-providers/${id}/oauth/device/start`, json('POST')),
  pollCodexOAuth: (id: string) => request<CodexOAuthStatus>(`/model-providers/${id}/oauth/device/poll`, json('POST')),
  codexOAuthStatus: (id: string) => request<CodexOAuthStatus>(`/model-providers/${id}/oauth/status`),
  disconnectCodexOAuth: (id: string) => request<ModelProvider>(`/model-providers/${id}/oauth`, json('DELETE')),
  flows: () => request<FlowDefinition[]>('/flows'),
  flow: (id: string) => request<FlowDefinition>(`/flows/${id}`),
  createFlow: (body: FlowWrite) => request<FlowDefinition>('/flows', json('POST', body)),
  updateFlow: (id: string, body: FlowWrite) => request<FlowDefinition>(`/flows/${id}`, json('PUT', body)),
  validateFlow: (id: string) => request<{ valid: boolean; errors: unknown[] }>(`/flows/${id}/validate`, json('POST')),
  deleteFlow: (id: string) => request<void>(`/flows/${id}`, json('DELETE')),

  runFlow: (flowId: string, body: { name?: string }) =>
    request<FlowRun>(`/flows/${flowId}/runs`, json('POST', body)),
  runs: () => request<FlowRunSummary[]>('/flow-runs'),
  flowRun: (id: string) => request<FlowRun>(`/flow-runs/${id}`),
  previewAgentProfileSwitch: (runId: string, flowNodeKey: string, profileVersionId: string) =>
    request<AgentProfileSwitchPreview>(`/flow-runs/${runId}/agent-profile-switch-preview?flow_node_key=${encodeURIComponent(flowNodeKey)}&profile_version_id=${encodeURIComponent(profileVersionId)}`),
  switchAgentProfile: (runId: string, body: { expected_active_version: number; flow_node_key: string; profile_version_id: string; source_profile_version_id?: string | null; expected_profile_digest: string; copy_input_bindings_from_attempt_id?: string | null; model_cost_comparison?: Record<string, unknown> }) =>
    request<AgentProfileSwitchResult>(`/flow-runs/${runId}/agent-profile-switch`, json('POST', body, true)),
  deleteRun: (id: string) => request<void>(`/flow-runs/${id}`, json('DELETE')),
  nodeRun: (runId: string, nodeRunId: string) => request<NodeRun>(`/flow-runs/${runId}/nodes/${nodeRunId}`),
  addArtifact: (runId: string, body: ArtifactInput) => request<ArtifactVersion>(`/flow-runs/${runId}/artifacts`, json('POST', body)),
  artifactContent: (artifactId: string) => requestText(`/artifact-versions/${artifactId}/content`),
  activateNode: (runId: string, key: string, artifact_ids: Record<string, string>, input_urls: Record<string, string> = {}) =>
    request<NodeRun>(`/flow-runs/${runId}/nodes/${key}/runs`, json('POST', { artifact_ids, input_urls })),
  bindInputs: (attemptId: string, bindings: Record<string, string>, version?: number) =>
    request<NodeAttempt>(`/node-attempts/${attemptId}/input-bindings`, json('PUT', { bindings, expected_state_version: version })),
  confirmStart: (attemptId: string, version: number, startup: { startup_mode: 'SKILL' | 'PROMPT'; capability_key?: string; prompt?: string }) => request<NodeAttempt>(`/node-attempts/${attemptId}/confirm-start`, json('POST', { expected_state_version: version, ...startup }, true)),
  humanInput: (attemptId: string, content: string, version: number, runtime?: { model_name?: string; reasoning_effort?: string | null }) => request<NodeAttempt>(`/node-attempts/${attemptId}/human-input`, json('POST', { content, expected_state_version: version, ...runtime }, true)),
  decideRuntimeConfirmation: (batchId: string, accept: boolean, reason: string) =>
    request<RuntimeConfirmationBatch>(`/runtime-confirmation-batches/${batchId}/decision`, json('POST', { accept, reason }, true)),
  acceptAttempt: (attemptId: string, version: number) => request<FlowRun>(`/node-attempts/${attemptId}/accept`, json('POST', { expected_state_version: version }, true)),
  rejectAttempt: (attemptId: string, reason: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/reject`, json('POST', { reason, copy_input_bindings: true, expected_state_version: version }, true)),
  retryGates: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/retry-gates`, json('POST', { expected_state_version: version })),
  cancelAttempt: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/cancel`, json('POST', { expected_state_version: version }, true)),
  retryRuntimeCancel: (attemptId: string, version: number, mode: 'RECONCILE_PARENT' | 'DELETE_MANAGED_RUNTIME') => request<NodeAttempt>(`/node-attempts/${attemptId}/retry-runtime-cancel`, json('POST', { expected_state_version: version, mode }, true)),
  syncSnapshot: (runId: string, version: number) => request<FlowRun>(`/flow-runs/${runId}/sync-snapshot`, json('POST', { expected_active_version: version }, true)),
  completeRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/complete`, json('POST', undefined, true)),
  cancelRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/cancel`, json('POST', undefined, true)),
  conversations: (attemptId: string) => request<AgentConversation[]>(`/node-attempts/${attemptId}/conversations`),
  createConversation: (attemptId: string, version: number, title?: string, runtime?: { model_name?: string; reasoning_effort?: string }) =>
    request<AgentConversation>(`/node-attempts/${attemptId}/conversations`, json('POST', {
      title, expected_attempt_state_version: version, baseline: { include_current_artifacts: true }, ...runtime,
    }, true)),
  conversation: (conversationId: string) => request<AgentConversation>(`/agent-conversations/${conversationId}`),
  conversationSubagents: (conversationId: string) =>
    request<RuntimeSubagentTask[]>(`/agent-conversations/${conversationId}/subagents`),
  controlConversationGoal: (conversationId: string, version: number, body: { action: 'START' | 'STOP' | 'RESUME'; objective?: string; max_iterations?: number; max_tokens?: number | null; max_cost_usd?: number | null }) =>
    request<RuntimeGoalCommand>(`/agent-conversations/${conversationId}/goal`, { ...json('POST', { expected_conversation_version: version, ...body }, true), headers: { ...json('POST', undefined, true).headers, 'X-Actor-ID': 'web-console' } }),
  askAgent: (conversationId: string, question: string) =>
    request<RuntimeDiagnosticQuery>(`/agent-conversations/${conversationId}/ask-agent`, { ...json('POST', { question, timeout_seconds: 30, output_classification: 'INTERNAL' }, true), headers: { ...json('POST', undefined, true).headers, 'X-Actor-ID': 'web-console' } }),
  diagnosticQuery: (id: string) => request<RuntimeDiagnosticQuery>(`/runtime-diagnostic-queries/${id}`, { headers: { 'X-Actor-ID': 'web-console' } }),
  stopConversation: (conversationId: string, version: number) =>
    request<AgentConversation>(`/agent-conversations/${conversationId}/stop`, json('POST', { expected_conversation_version: version }, true)),
  deleteConversation: (conversationId: string) => request<void>(`/agent-conversations/${conversationId}`, json('DELETE')),
  conversationMessages: (conversationId: string, afterSequence = 0) =>
    request<AgentMessage[]>(`/agent-conversations/${conversationId}/messages?after_sequence=${afterSequence}&limit=200`),
  sendConversationMessage: (conversationId: string, content: string, version: number, capabilityRefs: Array<{ capability_type: 'SKILL' | 'MCP'; capability_key: string }> = [], attachments: MessageAttachmentInput[] = [], runtime?: { model_name?: string; reasoning_effort?: string | null }, clientMessageId = randomId()) =>
    request<AgentMessage>(`/agent-conversations/${conversationId}/messages`, json('POST', {
      client_message_id: clientMessageId,
      content: [
        ...(content ? [{ type: 'text', text: content }] : []),
        ...attachments.map(item => ({ type: 'attachment', filename: item.filename, mime_type: item.mime_type, content_base64: item.content_base64 })),
      ],
      capability_refs: capabilityRefs,
      delivery_mode: 'QUEUE_AFTER_TURN',
      expected_conversation_version: version,
      ...runtime,
    }, true)),
  forkConversationMessage: (messageId: string, version: number, forkKind: 'RUNTIME' | 'SEMANTIC') =>
    request<AgentConversation>(`/agent-messages/${messageId}/fork`, json('POST', {
      expected_conversation_version: version,
      fork_kind: forkKind,
      fork_scope: 'MESSAGE',
      acknowledge_semantic_state_loss: forkKind === 'SEMANTIC',
    }, true)),
  reviseConversationMessage: (messageId: string, version: number, text: string) =>
    request<AgentConversation>(`/agent-messages/${messageId}/revise`, json('POST', {
      expected_conversation_version: version, text,
    }, true)),
  steerConversationMessage: (messageId: string) =>
    request<AgentMessage>(`/agent-messages/${messageId}/steer`, json('POST', undefined, true)),
  cancelQueuedConversationMessage: (messageId: string) =>
    request<AgentMessage>(`/agent-messages/${messageId}/cancel-queued`, json('POST', undefined, true)),
  retryConversationMessage: (messageId: string) =>
    request<AgentMessage>(`/agent-messages/${messageId}/retry`, json('POST', undefined, true)),
  flowEvents: (runId: string, after = 0) => request<RunEvent[]>(`/flow-runs/${runId}/event-history?after=${after}`),
};

export function environmentTerminalUrl(sessionId: string, rows = 24, columns = 80): string {
  const base = API_BASE || window.location.origin;
  const url = new URL(`${ROOT}/environment-setup-sessions/${encodeURIComponent(sessionId)}/terminal`, base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('rows', String(rows));
  url.searchParams.set('columns', String(columns));
  return url.toString();
}

export function agentTerminalUrl(conversationId: string, rows = 24, columns = 80): string {
  const base = API_BASE || window.location.origin;
  const url = new URL(`${ROOT}/agent-conversations/${encodeURIComponent(conversationId)}/terminal`, base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('rows', String(rows));
  url.searchParams.set('columns', String(columns));
  return url.toString();
}

export interface AgentStreamEvent {
  type: 'delta' | 'message_complete';
  content?: string;
}

export function agentStreamUrl(conversationId: string): string {
  const base = API_BASE || window.location.origin;
  const url = new URL(`${ROOT}/agent-conversations/${encodeURIComponent(conversationId)}/stream`, base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export function subscribeToConversationStream(
  conversationId: string,
  onEvent: (event: AgentStreamEvent) => void,
  onStatus?: (status: 'connecting' | 'live' | 'recovering' | 'disabled') => void,
): () => void {
  let disposed = false;
  let socket: WebSocket | undefined;
  let reconnectTimer: number | undefined;
  let reconnectAttempt = 0;

  const connect = () => {
    if (disposed) return;
    onStatus?.(reconnectAttempt ? 'recovering' : 'connecting');
    socket = new WebSocket(agentStreamUrl(conversationId));
    socket.onopen = () => { reconnectAttempt = 0; onStatus?.('live'); };
    socket.onmessage = message => {
      try {
        const event = JSON.parse(String(message.data)) as Partial<AgentStreamEvent>;
        if (event.type === 'delta' && typeof event.content === 'string') {
          onEvent({ type: 'delta', content: event.content });
        } else if (event.type === 'message_complete') {
          onEvent({ type: 'message_complete' });
        }
      } catch {
        // A malformed transient frame must not disrupt durable message polling.
      }
    };
    socket.onclose = event => {
      socket = undefined;
      if (disposed || event.code === 4409) { onStatus?.('disabled'); return; }
      onStatus?.('recovering');
      const delay = Math.min(500 * (2 ** reconnectAttempt), 5000);
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(connect, delay);
    };
  };

  connect();
  return () => {
    disposed = true;
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    socket?.close(1000, 'Conversation changed');
    onStatus?.('disabled');
  };
}

export function subscribeToRun(runId: string, onEvent: () => void): () => void {
  const source = new EventSource(`${API_BASE}${ROOT}/flow-runs/${runId}/events`);
  source.onmessage = onEvent;
  ['ATTEMPT_CREATED', 'HUMAN_CONFIRM_REQUIRED', 'ARTIFACT_VERSION_CREATED', 'NODE_RUN_ACCEPTED', 'SNAPSHOT_SYNCED', 'FLOW_RUN_COMPLETED', 'CONVERSATION_CREATED', 'CONVERSATION_STATE_CHANGED', 'AGENT_MESSAGE_CREATED', 'AGENT_MESSAGE_DELIVERY_CHANGED'].forEach(type => source.addEventListener(type, onEvent));
  return () => source.close();
}
