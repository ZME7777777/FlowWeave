import type {
  AgentProfileVersion, ArtifactInput, ArtifactVersion, CapabilityAsset, CapabilityImportResult, FlowDefinition, FlowRun, FlowRunAutomaticRecord, FlowRunAutomaticRecordUpdate, FlowRunAutomaticRecordWrite, FlowRunConversation, FlowRunRuntimeOverview, FlowRunSummary, FlowWrite, MessageAttachmentInput, OpenHandsConversationEventBatch, McpSource, SkillSource,
  BlockedNodeDelete, BlockedProviderDelete, BulkDeleteResult, CapabilityBulkDeleteResult, CodexDeviceAuthorization, CodexOAuthStatus, ModelProvider, ModelProviderDiscoveryWrite, ModelProviderWrite, NodeAsset, NodeAssetWrite, NodeAttempt,
  AgentAttachment, AgentConversation, AgentConversationContext, AgentConversationInputReadiness, AgentConversationReference, AgentPendingConfirmation, AgentWorkDirectory, AgentWorkDirectoryList, AgentWorkspace, AgentWorkspaceCapability, AgentWorkspaceDetails, AgentWorkspaceMcpReadiness, AgentWorkspaceRuntime, CapabilityCollection, CapabilityCollectionWrite, ContextBundleManifest, MarketplaceCatalog, NodeDirectory, NodeRun, OpenHandsConversationEvent, PluginSourceResolution, RunEvent, RuntimeConfirmationBatch, TerminalEnvironment, TerminalEnvironmentWrite, EnvironmentSetupSession, EnvironmentVersion, GatePolicy, WebsiteCredential, WebsiteCredentialWrite,
} from '../types';
import { deploymentBasePath } from '../deploymentPath';

// Docker declares VITE_API_BASE_URL even when no explicit override is given.
// Treat that empty value as absent so prefix deployments keep requests under
// /flowweave instead of accidentally sending them to the host application's
// root-level /api route.
const API_BASE = import.meta.env.VITE_API_BASE_URL || deploymentBasePath;
const ROOT = '/api/v1';
const absoluteApiUrl = (path: string) => new URL(`${API_BASE}${ROOT}${path}`, window.location.origin);
export const randomId = () => {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const value = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
};

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

async function requestText(path: string, signal?: AbortSignal): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${ROOT}${path}`, { signal });
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
      // Runtime replacement and other control-plane state may recover without
      // changing the route. Never let the browser reuse a stale dynamic GET.
      cache: init.method === undefined || init.method === 'GET' ? 'no-store' : init.cache,
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

const json = (method: string, body?: unknown, idempotencyKey?: string | true): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
  headers: idempotencyKey
    ? { 'Idempotency-Key': idempotencyKey === true ? randomId() : idempotencyKey }
    : undefined,
});

type AutomaticRunResponse = FlowRun & {
  parent_flow_run_id?: string | null;
  automation_plan?: {
    start_node_key?: string; reachable_node_keys?: string[];
    node_plans?: Record<string, FlowRunAutomaticRecord['node_plans'][string]>;
    readiness?: FlowRunAutomaticRecord['readiness'];
  } | null;
};

const automaticRecord = (run: AutomaticRunResponse): FlowRunAutomaticRecord => {
  const plan = run.automation_plan ?? {};
  return {
    ...run,
    flow_run_id: run.parent_flow_run_id ?? '',
    start_node_key: plan.start_node_key ?? '',
    reachable_node_keys: plan.reachable_node_keys ?? [],
    node_plans: plan.node_plans ?? {},
    readiness: plan.readiness ?? { ready: false, issues: [] },
  };
};

/** Automatic-run responses contain frozen, read-only audit fields (for
 * example agent_preset.capabilities). The strict write schema deliberately
 * rejects those fields, so project the editable record back to its command
 * shape instead of echoing the response object verbatim. */
const automaticNodePlansWrite = (nodePlans: FlowRunAutomaticRecordUpdate['node_plans']): FlowRunAutomaticRecordUpdate['node_plans'] =>
  Object.fromEntries(Object.entries(nodePlans).map(([nodeKey, plan]) => [nodeKey, {
    startup_prompt: plan.startup_prompt,
    agent_preset: {
      capability_version_ids: plan.agent_preset.capability_version_ids,
      model_provider_id: plan.agent_preset.model_provider_id,
      model_name: plan.agent_preset.model_name,
      reasoning_effort: plan.agent_preset.reasoning_effort,
      node_context_enabled: plan.agent_preset.node_context_enabled,
      node_context_prompt: plan.agent_preset.node_context_prompt,
    },
    gates: plan.gates.map(gate => ({
      stage: gate.stage,
      position: gate.position,
      gate_type: gate.gate_type,
      enabled: gate.enabled,
      timeout_seconds: gate.timeout_seconds,
      config: gate.config,
      agent_preset: {
        model_provider_id: gate.agent_preset?.model_provider_id,
        model_name: gate.agent_preset?.model_name,
        reasoning_effort: gate.agent_preset?.reasoning_effort,
      },
    })),
    artifact_ids: plan.artifact_ids,
    input_urls: plan.input_urls,
  }]));

/** The upload response includes display metadata which strict write schemas do
 * not accept. Only send the native attachment reference back to the API. */
const attachmentReferences = (attachments: AgentAttachment[]) => attachments.map(({ path, image_data_url, filename, mime_type, byte_size }) =>
  image_data_url ? { path, image_data_url, filename, mime_type, byte_size } : { path, filename, mime_type, byte_size },
);

export const api = {
  defaultAgentWorkspace: () => request<AgentWorkspace>('/agent-workspaces/default'),
  agentWorkspace: (id: string) => request<AgentWorkspace>(`/agent-workspaces/${encodeURIComponent(id)}`),
  updateAgentWorkspaceSettings: (id: string, default_model_provider_id: string | null) =>
    request<AgentWorkspace>(`/agent-workspaces/${encodeURIComponent(id)}/settings`, json('PATCH', { default_model_provider_id })),
  agentWorkspaceCapabilities: (id: string) =>
    request<AgentWorkspaceCapability[]>(`/agent-workspaces/${encodeURIComponent(id)}/capabilities`),
  replaceAgentWorkspaceCapabilities: (id: string, capability_version_ids: string[]) =>
    request<AgentWorkspaceCapability[]>(`/agent-workspaces/${encodeURIComponent(id)}/capabilities`, json('PUT', { capability_version_ids })),
  agentWorkspaceMcpReadiness: (id: string, capabilityVersionId: string) =>
    request<AgentWorkspaceMcpReadiness>(`/agent-workspaces/${encodeURIComponent(id)}/capabilities/${encodeURIComponent(capabilityVersionId)}/mcp-readiness`, json('POST')),
  agentWorkspaceRuntime: (id: string) => request<AgentWorkspaceRuntime>(`/agent-workspaces/${encodeURIComponent(id)}/runtime`),
  agentWorkDirectories: (id: string) => request<AgentWorkDirectoryList>(`/agent-workspaces/${encodeURIComponent(id)}/work-directories`),
  createAgentWorkDirectory: (id: string, display_name: string, selected_paths: string[]) =>
    request<AgentWorkDirectory>(`/agent-workspaces/${encodeURIComponent(id)}/work-directories`, json('POST', { display_name, selected_paths })),
  deleteAgentWorkDirectory: (id: string, directoryId: string) =>
    request<void>(`/agent-workspaces/${encodeURIComponent(id)}/work-directories/${encodeURIComponent(directoryId)}`, json('DELETE')),
  agentWorkspaceDetails: (id: string, options: { bindingId?: string; workDirectoryId?: string } = {}) => {
    const query = new URLSearchParams();
    if (options.bindingId) query.set('binding_id', options.bindingId);
    if (options.workDirectoryId) query.set('work_directory_id', options.workDirectoryId);
    return request<AgentWorkspaceDetails>(`/agent-workspaces/${encodeURIComponent(id)}/workspace${query.size ? `?${query}` : ''}`);
  },
  agentWorkspaceFilePreview: (id: string, path: string, options: { bindingId?: string; workDirectoryId?: string } = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams({ path });
    if (options.bindingId) query.set('binding_id', options.bindingId);
    if (options.workDirectoryId) query.set('work_directory_id', options.workDirectoryId);
    return requestText(`/agent-workspaces/${encodeURIComponent(id)}/workspace/file?${query}`, signal);
  },
  deleteAgentWorkspaceFile: (id: string, path: string, options: { bindingId?: string; workDirectoryId?: string; recursive?: boolean } = {}) => {
    const query = new URLSearchParams({ path });
    if (options.bindingId) query.set('binding_id', options.bindingId);
    if (options.workDirectoryId) query.set('work_directory_id', options.workDirectoryId);
    if (options.recursive) query.set('recursive', 'true');
    return request<void>(`/agent-workspaces/${encodeURIComponent(id)}/workspace/file?${query}`, json('DELETE'));
  },
  createAgentWorkspaceEntry: (id: string, parent_path: string, name: string, kind: 'FILE' | 'DIRECTORY', options: { bindingId?: string; workDirectoryId?: string } = {}) => {
    const query = new URLSearchParams();
    if (options.bindingId) query.set('binding_id', options.bindingId);
    if (options.workDirectoryId) query.set('work_directory_id', options.workDirectoryId);
    return request<void>(`/agent-workspaces/${encodeURIComponent(id)}/workspace/entries${query.size ? `?${query}` : ''}`, json('POST', { parent_path, name, kind }));
  },
  closeAgentWorkspaceTerminal: (id: string, terminalInstanceId: string, options: { bindingId?: string; workDirectoryId?: string } = {}) => {
    const query = new URLSearchParams();
    if (options.bindingId) query.set('binding_id', options.bindingId);
    if (options.workDirectoryId) query.set('work_directory_id', options.workDirectoryId);
    return request<void>(`/agent-workspaces/${encodeURIComponent(id)}/terminals/${encodeURIComponent(terminalInstanceId)}${query.size ? `?${query}` : ''}`, json('DELETE'));
  },
  agentConversations: (workspaceId: string) => request<AgentConversation[]>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations`),
  addAgentConversationCapability: (workspaceId: string, bindingId: string, capability_version_id: string) =>
    request<AgentConversation>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/capabilities`, json('POST', { capability_version_id })),
  bootstrapAgentConversation: (workspaceId: string, conversation_id: string, model_provider_id: string, model_name: string, reasoning_effort: string | null, content: string, attachments: AgentAttachment[] = [], references: AgentConversationReference[] = [], work_directory_id?: string, capability_version_ids: string[] = [], idempotencyKey = conversation_id) =>
    request<{ conversation: AgentConversation; accepted: boolean; cursor?: string | null }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations`, json('POST', { conversation_id, model_provider_id, model_name, reasoning_effort, content, attachments: attachmentReferences(attachments), references, work_directory_id, capability_version_ids }, idempotencyKey)),
  updateAgentConversation: (workspaceId: string, bindingId: string, title: string) =>
    request<AgentConversation>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}`, json('PATCH', { title })),
  deleteAgentConversation: (workspaceId: string, bindingId: string) =>
    request<void>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}`, json('DELETE', undefined, true)),
  agentConversationEvents: (workspaceId: string, bindingId: string, cursor?: string) =>
    request<OpenHandsConversationEventBatch>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  agentPendingConfirmation: (workspaceId: string, bindingId: string) =>
    request<AgentPendingConfirmation>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/pending-confirmation`),
  decideAgentConfirmation: (workspaceId: string, bindingId: string, expected_pending_digest: string, accept: boolean, reason: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/pending-confirmation/decision`, json('POST', { expected_pending_digest, accept, reason })),
  sendAgentMessage: (workspaceId: string, bindingId: string, content: string, attachments: AgentAttachment[] = [], references: AgentConversationReference[] = []) =>
    request<{ accepted: boolean; cursor?: string | null; compacted?: boolean }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/messages`, json('POST', { content, attachments: attachmentReferences(attachments), references })),
  uploadAgentAttachment: async (workspaceId: string, bindingId: string, file: File): Promise<AgentAttachment> => {
    const body = new FormData(); body.append('file', file, file.name);
    let response: Response;
    try { response = await fetch(`${API_BASE}${ROOT}/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/attachments`, { method: 'POST', body }); }
    catch { throw new ApiError('无法上传附件，请检查网络后重试。', 'NETWORK_ERROR', {}, 0); }
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<AgentAttachment>;
  },
  uploadAgentWorkspaceAttachment: async (workspaceId: string, file: File, workDirectoryId?: string, conversationId?: string): Promise<AgentAttachment> => {
    const body = new FormData(); body.append('file', file, file.name);
    const query = new URLSearchParams();
    if (workDirectoryId) query.set('work_directory_id', workDirectoryId);
    if (conversationId) query.set('conversation_id', conversationId);
    let response: Response;
    try { response = await fetch(`${API_BASE}${ROOT}/agent-workspaces/${encodeURIComponent(workspaceId)}/attachments${query.size ? `?${query}` : ''}`, { method: 'POST', body }); }
    catch { throw new ApiError('无法上传附件，请检查网络后重试。', 'NETWORK_ERROR', {}, 0); }
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<AgentAttachment>;
  },
  agentConversationContext: (workspaceId: string, bindingId: string) =>
    request<AgentConversationContext>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/context`),
  switchAgentConversationModel: (workspaceId: string, bindingId: string, model_provider_id: string, model_name: string, reasoning_effort: string | null) =>
    request<{ model_provider_id: string; model_name?: string | null; reasoning_effort?: string | null }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/model`, json('POST', { model_provider_id, model_name, reasoning_effort })),
  migrateAgentStreamingConversation: (workspaceId: string, bindingId: string, model_provider_id: string, model_name?: string | null, reasoning_effort?: string | null) =>
    request<AgentConversation>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/streaming-migration`, json('POST', { model_provider_id, model_name, reasoning_effort }, true)),
  condenseAgentConversation: (workspaceId: string, bindingId: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/condense`, json('POST')),
  forkAgentConversation: (workspaceId: string, bindingId: string, event_id: string) =>
    request<AgentConversation>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/fork`, json('POST', { event_id }, true)),
  rerunAgentMessage: (workspaceId: string, bindingId: string, eventId: string, content: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/messages/${encodeURIComponent(eventId)}/rerun`, json('POST', { content })),
  interruptAgentConversation: (workspaceId: string, bindingId: string) =>
    request<{ accepted: boolean }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/interrupt`, json('POST')),
  agentConversationInputReadiness: (workspaceId: string, bindingId: string) =>
    request<AgentConversationInputReadiness>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/input-readiness`),
  resumeAgentConversation: (workspaceId: string, bindingId: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/resume`, json('POST')),
  directories: () => request<NodeDirectory[]>('/node-directories'),
  createDirectory: (body: { name: string; parent_id?: string | null; position?: number }) =>
    request<NodeDirectory>('/node-directories', json('POST', body)),
  deleteDirectory: (id: string) => request<void>(`/node-directories/${encodeURIComponent(id)}`, json('DELETE')),
  nodes: (directoryId?: string) => request<NodeAsset[]>(`/node-assets${directoryId ? `?directory_id=${directoryId}` : ''}`),
  node: (id: string) => request<NodeAsset>(`/node-assets/${id}`),
  createNode: (body: NodeAssetWrite) => request<NodeAsset>('/node-assets', json('POST', body)),
  updateNode: (id: string, body: NodeAssetWrite) => request<NodeAsset>(`/node-assets/${id}`, json('PUT', body)),
  deleteNode: (id: string) => request<void>(`/node-assets/${id}`, json('DELETE')),
  deleteNodes: (ids: string[]) => request<BulkDeleteResult<BlockedNodeDelete>>('/node-assets', json('DELETE', { ids })),
  validateCapability: (body: { capability_type: string; filename: string; content_base64: string; context_title?: string; context_description?: string; context_bundle_manifest?: { entrypoint: string | null; documents: Array<{ path: string; title: string }>; conflict_policy: 'ORDERED_DOCUMENTS_LATER_WINS' }; mcp_scripts?: Array<{ server: string; filename: string; content_base64: string }>; hook_scripts?: Array<{ filename: string; content_base64: string }> }) =>
    request<{
      import_token: string;
      preview: {
        capabilities?: Array<{ capability_key?: string; normalized_config?: Record<string, unknown> }>;
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
  capabilityCollections: () => request<CapabilityCollection[]>('/capability-collections'),
  mcpSource: (id: string) => request<McpSource>(`/capabilities/${encodeURIComponent(id)}/mcp-source`),
  updateMcpSource: (id: string, content: string, mcp_scripts: Array<{ server: string; filename: string; content_base64: string }>) =>
    request<CapabilityAsset>(`/capabilities/${encodeURIComponent(id)}/mcp-source`, json('PUT', { content, mcp_scripts })),
  createCapabilityCollection: (body: CapabilityCollectionWrite) =>
    request<CapabilityCollection>('/capability-collections', json('POST', body)),
  updateCapabilityCollection: (id: string, body: CapabilityCollectionWrite) =>
    request<CapabilityCollection>(`/capability-collections/${id}`, json('PUT', body)),
  deleteCapabilityCollection: (id: string) =>
    request<void>(`/capability-collections/${id}`, json('DELETE')),
  capabilitySource: (id: string) => request<SkillSource>(`/capabilities/${encodeURIComponent(id)}/source`),
  contextSource: (id: string) => request<{ id: string; capability_key: string; filename: string; description: string; content_format: 'TEXT' | 'BUNDLE'; manifest: ContextBundleManifest | null; content: string }>(`/capabilities/${encodeURIComponent(id)}/context-source`),
  updateCapabilitySource: (id: string, content: string) =>
    request<CapabilityAsset>(`/capabilities/${encodeURIComponent(id)}/source`, json('PUT', { content })),
  deleteCapability: (id: string) => request<void>(`/capabilities/${encodeURIComponent(id)}`, json('DELETE')),
  deleteCapabilities: (ids: string[]) => request<CapabilityBulkDeleteResult>('/capabilities', json('DELETE', { ids })),
  agentProfileVersions: (packageId: string) =>
    request<AgentProfileVersion[]>(`/agent-profile-packages/${encodeURIComponent(packageId)}/versions`),
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
  publishEnvironmentSetup: (id: string, description = '') =>
    request<EnvironmentVersion>(`/environment-setup-sessions/${id}/publish`, json('POST', { description })),
  stopEnvironmentSetup: (id: string) => request<void>(`/environment-setup-sessions/${id}`, json('DELETE')),
  websiteCredentials: () => request<WebsiteCredential[]>('/website-credentials'),
  createWebsiteCredential: (body: WebsiteCredentialWrite) =>
    request<WebsiteCredential>('/website-credentials', json('POST', body)),
  updateWebsiteCredential: (id: string, body: WebsiteCredentialWrite) =>
    request<WebsiteCredential>(`/website-credentials/${encodeURIComponent(id)}`, json('PUT', body)),
  deleteWebsiteCredential: (id: string) =>
    request<void>(`/website-credentials/${encodeURIComponent(id)}`, json('DELETE')),
  deleteWebsiteCredentials: (ids: string[]) =>
    request<{ deleted_ids: string[] }>('/website-credentials', json('DELETE', { ids })),

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

  runFlow: (flowId: string, body: { name?: string; environment_version_id: string }) =>
    request<FlowRun>(`/flows/${flowId}/runs`, json('POST', body)),
  runs: () => request<FlowRunSummary[]>('/flow-runs'),
  flowRun: (id: string) => request<FlowRun>(`/flow-runs/${id}`),
  automaticRecords: async (runId: string) =>
    (await request<AutomaticRunResponse[]>(`/flow-runs/${encodeURIComponent(runId)}/automatic-runs`)).map(automaticRecord),
  createAutomaticRecord: async (runId: string, body: FlowRunAutomaticRecordWrite) =>
    automaticRecord(await request<AutomaticRunResponse>(`/flow-runs/${encodeURIComponent(runId)}/automatic-runs`, json('POST', body))),
  updateAutomaticRecord: async (runId: string, recordId: string, body: FlowRunAutomaticRecordUpdate) =>
    automaticRecord(await request<AutomaticRunResponse>(`/flow-runs/${encodeURIComponent(runId)}/automatic-runs/${encodeURIComponent(recordId)}`, json('PUT', {
      ...body,
      node_plans: automaticNodePlansWrite(body.node_plans),
    }))),
  copyAutomaticRecord: async (runId: string, recordId: string, name: string) =>
    automaticRecord(await request<AutomaticRunResponse>(`/flow-runs/${encodeURIComponent(runId)}/automatic-runs/${encodeURIComponent(recordId)}/copy`, json('POST', { name }))),
  startAutomaticRecord: async (runId: string, recordId: string, expected_row_version: number) =>
    automaticRecord(await request<AutomaticRunResponse>(`/flow-runs/${encodeURIComponent(runId)}/automatic-runs/${encodeURIComponent(recordId)}/start`, json('POST', { expected_row_version }, true))),
  deleteAutomaticRecord: (runId: string, recordId: string) =>
    request<void>(`/flow-runs/${encodeURIComponent(runId)}/automatic-runs/${encodeURIComponent(recordId)}`, json('DELETE')),
  deleteRun: (id: string) => request<void>(`/flow-runs/${id}`, json('DELETE')),
  nodeRun: (runId: string, nodeRunId: string) => request<NodeRun>(`/flow-runs/${runId}/nodes/${nodeRunId}`),
  deleteNodeRun: (runId: string, nodeRunId: string) =>
    request<void>(`/flow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeRunId)}`, json('DELETE')),
  copyNodeRun: (runId: string, nodeRunId: string, name: string) =>
    request<NodeRun>(`/flow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeRunId)}/copy`, json('POST', { name })),
  addArtifact: (runId: string, body: ArtifactInput) => request<ArtifactVersion>(`/flow-runs/${runId}/artifacts`, json('POST', body)),
  addNodeInputArtifact: (runId: string, nodeKey: string, body: ArtifactInput) =>
    request<ArtifactVersion>(`/flow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeKey)}/input-artifacts`, json('POST', body)),
  uploadArtifact: async (runId: string, fieldKey: string, displayName: string, file: File): Promise<ArtifactVersion> => {
    const body = new FormData();
    body.append('field_key', fieldKey);
    body.append('display_name', displayName);
    body.append('file', file, file.name);
    let response: Response;
    try { response = await fetch(`${API_BASE}${ROOT}/flow-runs/${encodeURIComponent(runId)}/artifacts/upload`, { method: 'POST', body }); }
    catch { throw new ApiError('无法上传输入文件，请检查网络后重试。', 'NETWORK_ERROR', {}, 0); }
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<ArtifactVersion>;
  },
  uploadNodeInputArtifact: async (runId: string, nodeKey: string, fieldKey: string, displayName: string, file: File): Promise<ArtifactVersion> => {
    const body = new FormData();
    body.append('field_key', fieldKey);
    body.append('display_name', displayName);
    body.append('file', file, file.name);
    let response: Response;
    try { response = await fetch(`${API_BASE}${ROOT}/flow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeKey)}/input-artifacts/upload`, { method: 'POST', body }); }
    catch { throw new ApiError('无法上传节点输入文件，请检查网络后重试。', 'NETWORK_ERROR', {}, 0); }
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<ArtifactVersion>;
  },
  artifactContent: (artifactId: string) => requestText(`/artifact-versions/${artifactId}/content`),
  activateNode: (runId: string, key: string, artifact_ids: Record<string, string>, gates: GatePolicy[] = [], input_urls: Record<string, string> = {}, startup_mode: 'PROMPT' | 'CHAT' = 'PROMPT', agent_preset?: import('../types').AgentPreset, startup_prompt?: string) =>
    request<NodeRun>(`/flow-runs/${runId}/nodes/${key}/runs`, json('POST', { artifact_ids, gates, input_urls, startup_mode, agent_preset, startup_prompt })),
  bindInputs: (attemptId: string, bindings: Record<string, string>, version?: number) =>
    request<NodeAttempt>(`/node-attempts/${attemptId}/input-bindings`, json('PUT', { bindings, expected_state_version: version })),
  confirmStart: (attemptId: string, version: number, startup: { startup_mode: 'SKILL' | 'PROMPT'; capability_key?: string; prompt?: string }) => request<NodeAttempt>(`/node-attempts/${attemptId}/confirm-start`, json('POST', { expected_state_version: version, ...startup }, true)),
  humanInput: (attemptId: string, content: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/human-input`, json('POST', { content, expected_state_version: version }, true)),
  submitManualOutputs: (attemptId: string, version: number, outputs: Record<string, { artifact_type: 'URL'; uri: string } | { artifact_type: 'FILE'; path: string }>) =>
    request<NodeAttempt>(`/node-attempts/${attemptId}/manual-outputs`, json('POST', { expected_state_version: version, outputs }, true)),
  decideRuntimeConfirmation: (batchId: string, accept: boolean, reason: string) =>
    request<RuntimeConfirmationBatch>(`/runtime-confirmation-batches/${batchId}/decision`, json('POST', { accept, reason }, true)),
  acceptAttempt: (attemptId: string, version: number) => request<FlowRun>(`/node-attempts/${attemptId}/accept`, json('POST', { expected_state_version: version }, true)),
  acceptGateRisk: (attemptId: string, version: number, reason: string) => request<FlowRun>(`/node-attempts/${attemptId}/accept-gate-risk`, json('POST', { expected_state_version: version, reason }, true)),
  remediateGateFailure: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/remediate-gate-failure`, json('POST', { expected_state_version: version }, true)),
  gateEvaluationEvents: (attemptId: string, evaluationId: string, cursor?: string) => request<OpenHandsConversationEventBatch>(`/node-attempts/${attemptId}/gate-evaluations/${evaluationId}/conversation/events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  rejectAttempt: (attemptId: string, reason: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/reject`, json('POST', { reason, copy_input_bindings: true, expected_state_version: version }, true)),
  retryGates: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/retry-gates`, json('POST', { expected_state_version: version })),
  cancelAttempt: (attemptId: string, version: number) => request<NodeAttempt>(`/node-attempts/${attemptId}/cancel`, json('POST', { expected_state_version: version }, true)),
  retryRuntimeCancel: (attemptId: string, version: number, mode: 'RECONCILE_PARENT' | 'DELETE_MANAGED_RUNTIME') => request<NodeAttempt>(`/node-attempts/${attemptId}/retry-runtime-cancel`, json('POST', { expected_state_version: version, mode }, true)),
  syncSnapshot: (runId: string, version: number) => request<FlowRun>(`/flow-runs/${runId}/sync-snapshot`, json('POST', { expected_active_version: version }, true)),
  completeRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/complete`, json('POST', undefined, true)),
  cancelRun: (runId: string) => request<FlowRun>(`/flow-runs/${runId}/cancel`, json('POST', undefined, true)),
  conversations: (runId: string) => request<FlowRunConversation[]>(`/flow-runs/${runId}/conversations`),
  createConversation: (runId: string, nodeAttemptId: string, title?: string, runtime?: { model_name?: string; reasoning_effort?: string }) =>
    request<FlowRunConversation>(`/flow-runs/${runId}/conversations`, json('POST', { node_attempt_id: nodeAttemptId, title, ...runtime }, true)),
  conversation: (runId: string, conversationId: string) =>
    request<FlowRunConversation>(`/flow-runs/${runId}/conversations/${conversationId}`),
  conversationEvents: (runId: string, conversationId: string, cursor?: string) =>
    request<OpenHandsConversationEventBatch>(`/flow-runs/${runId}/conversations/${conversationId}/events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  sendConversationQuestion: (runId: string, conversationId: string, content: string, attachments: MessageAttachmentInput[] = [], clientQuestionId = randomId()) =>
    request<{ accepted: boolean }>(`/flow-runs/${runId}/conversations/${conversationId}/questions`, json('POST', {
      client_question_id: clientQuestionId,
      content: [
        ...(content ? [{ type: 'text', text: content }] : []),
        ...attachments.map(item => ({ type: 'attachment', filename: item.filename, mime_type: item.mime_type, content_base64: item.content_base64 })),
      ],
    }, true)),
  stopConversation: (runId: string, conversationId: string) =>
    request<{ accepted: boolean }>(`/flow-runs/${runId}/conversations/${conversationId}/stop`, json('POST', undefined, true)),
  runtimeOverview: (runId: string) => request<FlowRunRuntimeOverview>(`/flow-runs/${runId}/runtime`),
  replaceRuntime: (runId: string, generation: number, sessionRowVersion: number) =>
    request<FlowRunRuntimeOverview>(`/flow-runs/${runId}/runtime/replacements`, json('POST', {
      expected_generation: generation, expected_session_row_version: sessionRowVersion,
    }, true)),
  pauseRuntime: (runId: string, generation: number, sessionRowVersion: number) =>
    request<FlowRunRuntimeOverview>(`/flow-runs/${runId}/runtime/pause`, json('POST', {
      expected_generation: generation, expected_session_row_version: sessionRowVersion,
    }, true)),
  resumeRuntime: (runId: string, generation: number, sessionRowVersion: number) =>
    request<FlowRunRuntimeOverview>(`/flow-runs/${runId}/runtime/resume`, json('POST', {
      expected_generation: generation, expected_session_row_version: sessionRowVersion,
    }, true)),
  flowEvents: (runId: string, after = 0) => request<RunEvent[]>(`/flow-runs/${runId}/event-history?after=${after}`),
};

export function environmentTerminalUrl(sessionId: string, rows = 24, columns = 80): string {
  const url = absoluteApiUrl(`/environment-setup-sessions/${encodeURIComponent(sessionId)}/terminal`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('rows', String(rows));
  url.searchParams.set('columns', String(columns));
  return url.toString();
}

export function agentTerminalUrl(runId: string, conversationId: string, rows = 24, columns = 80): string {
  const url = absoluteApiUrl(`/flow-runs/${encodeURIComponent(runId)}/conversations/${encodeURIComponent(conversationId)}/terminal`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('rows', String(rows));
  url.searchParams.set('columns', String(columns));
  return url.toString();
}

export interface AgentStreamEvent {
  type: 'delta' | 'event' | 'message_complete';
  content?: string;
  event?: OpenHandsConversationEvent;
}

export function agentStreamUrl(runId: string, conversationId: string): string {
  const url = absoluteApiUrl(`/flow-runs/${encodeURIComponent(runId)}/conversations/${encodeURIComponent(conversationId)}/stream`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export function subscribeToConversationStream(
  runId: string,
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
    socket = new WebSocket(agentStreamUrl(runId, conversationId));
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

export function agentWorkspaceTerminalUrl(workspaceId: string, rows = 24, columns = 80, options: { terminalInstanceId: string; bindingId?: string; workDirectoryId?: string }): string {
  const url = absoluteApiUrl(`/agent-workspaces/${encodeURIComponent(workspaceId)}/terminal`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('rows', String(rows));
  url.searchParams.set('columns', String(columns));
  url.searchParams.set('terminal_instance_id', options.terminalInstanceId);
  if (options.bindingId) url.searchParams.set('binding_id', options.bindingId);
  if (options.workDirectoryId) url.searchParams.set('work_directory_id', options.workDirectoryId);
  return url.toString();
}

export function agentWorkspaceFileUrl(workspaceId: string, path: string, options: { bindingId?: string; workDirectoryId?: string; download?: boolean } = {}): string {
  const url = absoluteApiUrl(`/agent-workspaces/${encodeURIComponent(workspaceId)}/workspace/file`);
  url.searchParams.set('path', path);
  if (options.bindingId) url.searchParams.set('binding_id', options.bindingId);
  if (options.workDirectoryId) url.searchParams.set('work_directory_id', options.workDirectoryId);
  if (options.download ?? true) url.searchParams.set('download', 'true');
  return url.toString();
}

export function agentWorkspaceStreamUrl(workspaceId: string, bindingId: string): string {
  const url = absoluteApiUrl(`/agent-workspaces/${encodeURIComponent(workspaceId)}/conversations/${encodeURIComponent(bindingId)}/stream`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export function subscribeToAgentWorkspaceStream(
  workspaceId: string,
  bindingId: string,
  onEvent: (event: AgentStreamEvent) => void,
  onStatus?: (status: 'connecting' | 'live' | 'recovering' | 'disabled') => void,
): () => void {
  let disposed = false;
  let socket: WebSocket | undefined;
  let reconnectTimer: number | undefined;
  let reconnectAttempt = 0;
  const reconnectDelays = [1000, 2000, 5000, 10_000, 30_000];

  const connect = () => {
    if (disposed) return;
    onStatus?.(reconnectAttempt ? 'recovering' : 'connecting');
    socket = new WebSocket(agentWorkspaceStreamUrl(workspaceId, bindingId));
    socket.onopen = () => { reconnectAttempt = 0; onStatus?.('live'); };
    socket.onmessage = message => {
      try {
        const event = JSON.parse(String(message.data)) as Partial<AgentStreamEvent>;
        if (event.type === 'delta' && typeof event.content === 'string') onEvent({ type: 'delta', content: event.content });
        else if (event.type === 'event' && event.event && typeof event.event.id === 'string') onEvent({ type: 'event', event: event.event });
        else if (event.type === 'message_complete') onEvent({ type: 'message_complete' });
      } catch {
        // The REST event source is authoritative; ignore an invalid live frame.
      }
    };
    socket.onclose = event => {
      socket = undefined;
      if (disposed || event.code === 4409) { onStatus?.('disabled'); return; }
      onStatus?.('recovering');
      const delay = reconnectDelays[Math.min(reconnectAttempt, reconnectDelays.length - 1)];
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

function nodeSessionBase(flowRunId: string, attemptId: string): string {
  return `/flow-runs/${encodeURIComponent(flowRunId)}/node-attempts/${encodeURIComponent(attemptId)}/agent-sessions`;
}

export const nodeSessionApi = {
  host: (flowRunId: string, attemptId: string) => request<import('../types').AgentSessionHostDetails>(`${nodeSessionBase(flowRunId, attemptId)}/host`),
  runtime: (flowRunId: string, attemptId: string) => request<import('../types').AgentSessionRuntime>(`${nodeSessionBase(flowRunId, attemptId)}/runtime`),
  conversations: (flowRunId: string, attemptId: string) => request<import('../types').AgentConversation[]>(nodeSessionBase(flowRunId, attemptId)),
  create: (flowRunId: string, attemptId: string, title?: string, model_name?: string, reasoning_effort?: string | null, idempotencyKey = randomId(), work_directory_id?: string) =>
    request<import('../types').AgentConversation>(nodeSessionBase(flowRunId, attemptId), json('POST', { title, model_name, reasoning_effort, work_directory_id }, idempotencyKey)),
  bootstrap: (flowRunId: string, attemptId: string, content: string, model_provider_id: string, model_name: string, reasoning_effort: string | null, attachments: AgentAttachment[] = [], references: AgentConversationReference[] = [], work_directory_id?: string, idempotencyKey = randomId()) =>
    request<{ conversation: import('../types').AgentConversation; accepted: boolean; cursor?: string | null }>(`${nodeSessionBase(flowRunId, attemptId)}/bootstrap`, json('POST', { conversation_id: idempotencyKey, content, attachments: attachmentReferences(attachments), references, model_provider_id, model_name, reasoning_effort, work_directory_id }, idempotencyKey)),
  update: (flowRunId: string, attemptId: string, bindingId: string, title: string) =>
    request<import('../types').AgentConversation>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}`, json('PATCH', { title })),
  events: (flowRunId: string, attemptId: string, bindingId: string, cursor?: string) =>
    request<import('../types').OpenHandsConversationEventBatch>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  inputReadiness: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<AgentConversationInputReadiness>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/input-readiness`),
  context: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<import('../types').AgentConversationContext>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/context`),
  switchModel: (flowRunId: string, attemptId: string, bindingId: string, model_provider_id: string, model_name: string, reasoning_effort: string | null) =>
    request<{ model_provider_id: string; model_name?: string | null; reasoning_effort?: string | null }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/model`, json('POST', { model_provider_id, model_name, reasoning_effort })),
  remove: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<void>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}`, json('DELETE', undefined, true)),
  message: (flowRunId: string, attemptId: string, bindingId: string, content: string, attachments: AgentAttachment[] = [], references: AgentConversationReference[] = [], idempotencyKey = randomId()) =>
    request<{ accepted: boolean; cursor?: string | null; compacted?: boolean }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/messages`, json('POST', { content, attachments: attachmentReferences(attachments), references }, idempotencyKey)),
  pendingConfirmation: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<import('../types').AgentPendingConfirmation>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/pending-confirmation`),
  decideConfirmation: (flowRunId: string, attemptId: string, bindingId: string, expected_pending_digest: string, accept: boolean, reason: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/pending-confirmation/decision`, json('POST', { expected_pending_digest, accept, reason })),
  uploadAttachment: async (flowRunId: string, attemptId: string, bindingId: string, file: File): Promise<AgentAttachment> => {
    const body = new FormData(); body.append('file', file, file.name);
    const response = await fetch(`${API_BASE}${ROOT}${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/attachments`, { method: 'POST', body });
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<AgentAttachment>;
  },
  uploadDraftAttachment: async (flowRunId: string, attemptId: string, file: File, workDirectoryId?: string, conversationId?: string): Promise<AgentAttachment> => {
    const body = new FormData(); body.append('file', file, file.name);
    const query = new URLSearchParams();
    if (workDirectoryId) query.set('work_directory_id', workDirectoryId);
    if (conversationId) query.set('conversation_id', conversationId);
    const response = await fetch(`${API_BASE}${ROOT}${nodeSessionBase(flowRunId, attemptId)}/attachments${query.size ? `?${query}` : ''}`, { method: 'POST', body });
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<AgentAttachment>;
  },
  condense: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/condense`, json('POST')),
  interrupt: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<{ accepted: boolean }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/interrupt`, json('POST')),
  resume: (flowRunId: string, attemptId: string, bindingId: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/resume`, json('POST')),
  fork: (flowRunId: string, attemptId: string, bindingId: string, event_id: string) =>
    request<import('../types').AgentConversation>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/fork`, json('POST', { event_id }, true)),
  migrate: (flowRunId: string, attemptId: string, bindingId: string, model_provider_id: string, model_name?: string | null, reasoning_effort?: string | null) =>
    request<import('../types').AgentConversation>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/streaming-migration`, json('POST', { model_provider_id, model_name, reasoning_effort }, true)),
  rerun: (flowRunId: string, attemptId: string, bindingId: string, eventId: string, content: string) =>
    request<{ accepted: boolean; cursor?: string | null }>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/messages/${encodeURIComponent(eventId)}/rerun`, json('POST', { content })),
  mcpReadiness: (flowRunId: string, attemptId: string, capabilityVersionId: string) => request<import('../types').AgentSessionMcpReadiness>(`${nodeSessionBase(flowRunId, attemptId)}/capabilities/${encodeURIComponent(capabilityVersionId)}/mcp-readiness`, json('POST')),
  addCapability: (flowRunId: string, attemptId: string, bindingId: string, capability_version_id: string) => request<import('../types').AgentConversation>(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/capabilities`, json('POST', { capability_version_id })),
  workDirectories: (flowRunId: string, attemptId: string) =>
    request<import('../types').AgentSessionWorkDirectoryList>(`${nodeSessionBase(flowRunId, attemptId)}/work-directories`),
  createWorkDirectory: (flowRunId: string, attemptId: string, display_name: string, selected_paths: string[]) =>
    request<import('../types').AgentSessionWorkDirectory>(`${nodeSessionBase(flowRunId, attemptId)}/work-directories`, json('POST', { display_name, selected_paths })),
  workspace: (flowRunId: string, attemptId: string, bindingId?: string, workDirectoryId?: string) => {
    const query = new URLSearchParams();
    if (bindingId) query.set('binding_id', bindingId);
    if (workDirectoryId) query.set('work_directory_id', workDirectoryId);
    return request<import('../types').AgentSessionWorkspaceDetails>(`${nodeSessionBase(flowRunId, attemptId)}/workspace${query.size ? `?${query}` : ''}`);
  },
  file: (flowRunId: string, attemptId: string, path: string, bindingId?: string, workDirectoryId?: string, download = false) => {
    const query = new URLSearchParams({ path });
    if (bindingId) query.set('binding_id', bindingId);
    if (workDirectoryId) query.set('work_directory_id', workDirectoryId);
    if (download) query.set('download', 'true');
    return `${API_BASE}${ROOT}${nodeSessionBase(flowRunId, attemptId)}/workspace/file?${query}`;
  },
  candidateOutputFile: (flowRunId: string, attemptId: string, fieldKey: string, path: string) => {
    const query = new URLSearchParams({ field_key: fieldKey, path });
    return `${API_BASE}${ROOT}${nodeSessionBase(flowRunId, attemptId)}/candidate-output/file?${query}`;
  },
  terminal: (flowRunId: string, attemptId: string, bindingId: string | undefined, rows = 24, columns = 80) => {
    const suffix = bindingId ? `/${encodeURIComponent(bindingId)}/terminal` : '/terminal';
    const url = absoluteApiUrl(`${nodeSessionBase(flowRunId, attemptId)}${suffix}`);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('rows', String(rows));
    url.searchParams.set('columns', String(columns));
    return url.toString();
  },
  stream: (flowRunId: string, attemptId: string, bindingId: string) => {
    const url = absoluteApiUrl(`${nodeSessionBase(flowRunId, attemptId)}/${encodeURIComponent(bindingId)}/stream`);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.toString();
  },
};

export function subscribeToNodeSessionStream(
  flowRunId: string, attemptId: string, bindingId: string, onEvent: (event: AgentStreamEvent) => void,
  onStatus?: (status: 'connecting' | 'live' | 'recovering' | 'disabled') => void,
): () => void {
  let disposed = false; let socket: WebSocket | undefined; let retry: number | undefined; let attempts = 0;
  const connect = () => {
    if (disposed) return;
    onStatus?.(attempts ? 'recovering' : 'connecting');
    socket = new WebSocket(nodeSessionApi.stream(flowRunId, attemptId, bindingId));
    socket.onopen = () => { attempts = 0; onStatus?.('live'); };
    socket.onmessage = message => { try {
      const event = JSON.parse(String(message.data)) as Partial<AgentStreamEvent>;
      if (event.type === 'delta' && typeof event.content === 'string') onEvent({ type: 'delta', content: event.content });
      else if (event.type === 'event' && event.event && typeof event.event.id === 'string') onEvent({ type: 'event', event: event.event });
      else if (event.type === 'message_complete') onEvent({ type: 'message_complete' });
    } catch { /* REST remains authoritative. */ } };
    socket.onclose = event => { socket = undefined; if (disposed || event.code === 4409) { onStatus?.('disabled'); return; } onStatus?.('recovering'); retry = window.setTimeout(connect, Math.min(1000 * 2 ** attempts++, 10_000)); };
  };
  connect();
  return () => { disposed = true; if (retry !== undefined) window.clearTimeout(retry); socket?.close(1000, 'Conversation changed'); onStatus?.('disabled'); };
}

export function subscribeToRun(runId: string, onEvent: () => void): () => void {
  const source = new EventSource(`${API_BASE}${ROOT}/flow-runs/${runId}/events`);
  source.onmessage = onEvent;
  ['ATTEMPT_CREATED', 'HUMAN_CONFIRM_REQUIRED', 'ARTIFACT_VERSION_CREATED', 'NODE_RUN_ACCEPTED', 'SNAPSHOT_SYNCED', 'FLOW_RUN_COMPLETED'].forEach(type => source.addEventListener(type, onEvent));
  return () => source.close();
}
