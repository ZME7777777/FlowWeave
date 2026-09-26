export type Usage = {
  cpu_usage_percent: number;
  memory_usage_bytes: number;
  storage_usage_bytes: number;
  storage_limit: string | null;
};

export type ServiceObservation = {
  service: string;
  container_id: string;
  image: string;
  state: string;
  status: string;
  usage: Usage | null;
};

export type Runtime = {
  runtime_session_id: string;
  runtime_kind: string;
  owner_id: string;
  node_attempt_id: string | null;
  status: string;
  active_generation: number | null;
  row_version: number;
  runtime_image_digest: string;
  updated_at: string;
  failure_code: string | null;
  failure_summary: string | null;
  generation_state: string | null;
  ready_at: string | null;
  managed_sandbox_id: string | null;
  container_id: string | null;
  container_name: string | null;
  desired_state: string | null;
  observed_state: string | null;
  last_activity_at: string | null;
  idle_expires_at: string | null;
  hard_expires_at: string | null;
  last_error_code: string | null;
  last_error_detail: string | null;
  conversation_count: number;
  active_conversation_count: number;
  last_connected_at: string | null;
  flow_definition_name: string | null;
  flow_run_name: string | null;
  flow_run_no: number | null;
  flow_run_state: string | null;
  node_run_name: string | null;
  node_run_sequence_no: number | null;
  node_attempt_no: number | null;
  node_attempt_state: string | null;
  workspace_display_name: string | null;
  workspace_scope_key: string | null;
  business_diagnostic_status: 'OK' | 'DEGRADED' | 'NO_ACTIVE_CONVERSATION' | null;
  business_impacted_bindings: number | null;
  business_event_count: number | null;
  business_readiness_status: string | null;
  business_runtime_availability: string | null;
  business_stages: RuntimeDiagnostic['stages'] | null;
  business_observed_at: string | null;
  usage: Usage | null;
};

export type RuntimeGeneration = {
  generation: number;
  state: string;
  managed_runtime_id: string | null;
  started_at: string | null;
  ready_at: string | null;
  stopped_at: string | null;
  created_at: string;
  updated_at: string;
  failure_code: string | null;
  failure_summary: string | null;
};

export type RuntimeDiagnostic = {
  status: 'OK' | 'DEGRADED' | 'NO_ACTIVE_CONVERSATION';
  representative_binding_id?: string;
  impacted_bindings: number;
  stages: Array<{ name: string; outcome: 'ok' | 'error'; duration_ms: number; error_code?: string }>;
  event_count: number | null;
  readiness: { ready: boolean; execution_status: string } | null;
  runtime_availability: string | null;
};

export type RuntimeDetail = {
  runtime: Runtime & { created_at: string };
  generations: RuntimeGeneration[];
  conversation_summary: Array<{
    lifecycle: string;
    count: number;
    last_updated_at: string | null;
    last_connected_at: string | null;
  }>;
  operations: Array<{
    operation_id: string;
    action: string;
    status: string;
    runtime_kind: string;
    owner_id: string;
    expected_generation: number;
    expected_session_row_version: number;
    actor_username: string;
    reason: string;
    request_id: string;
    created_at: string;
  }>;
  container_observability_available: boolean;
};

export type Conversation = {
  binding_id: string;
  owner_user_id: string;
  username: string | null;
  host_kind: string;
  host_id: string;
  flow_run_id: string | null;
  node_run_id: string | null;
  node_attempt_id: string | null;
  runtime_session_id: string;
  openhands_conversation_id: string;
  display_title: string | null;
  lifecycle: string;
  model_name: string | null;
  created_at: string;
  updated_at: string;
  last_connected_at: string | null;
  unread: boolean;
};

export type RuntimeOperation = {
  operation_id: string;
  action: string;
  status: string;
  actor_user_id: string;
  actor_username: string;
  flow_run_id: string;
  runtime_session_id: string;
  expected_generation: number;
  expected_session_row_version: number;
  reason: string;
  request_id: string;
  created_at: string;
  current_runtime_status: string | null;
  current_generation: number | null;
  current_session_row_version: number | null;
  replacement_error_code: string | null;
  replacement_error_summary: string | null;
  replacement_generation_state: string | null;
  replacement_ready_at: string | null;
  replacement_failure_code: string | null;
  replacement_failure_summary: string | null;
  replacement_status: 'SUBMITTED' | 'RECOVERING' | 'RECOVERED' | 'FAILED';
};

export type AdminOperation = {
  id: string;
  action: 'REPLACE_RUNTIME' | 'ISOLATE_RUNTIME' | 'RESUME_RUNTIME' | 'ACKNOWLEDGE' | 'SILENCE';
  target_kind: 'RUNTIME' | 'ALERT';
  target_id: string;
  target_detail: string | null;
  actor_user_id: string;
  actor_username: string;
  reason: string;
  request_id: string;
  created_at: string;
  status: string;
  silenced_until: string | null;
};


export type Alert = {
  severity: 'CRITICAL' | 'WARNING';
  source: string;
  key: string;
  title: string;
  detail: string;
  lifecycle?: {
    acknowledged_at: string | null;
    acknowledged_by_username: string | null;
    silenced_until: string | null;
    reason: string | null;
  };
};



export type MetricHistoryPoint = { observed_at: string; value: number };
export type MetricHistory = {
  scope: 'SERVICE' | 'RUNTIME';
  subject: string;
  metric: 'cpu_usage_percent' | 'memory_usage_bytes' | 'storage_usage_bytes';
  hours: number;
  items: MetricHistoryPoint[];
};


export type BackgroundTask = {
  id: string;
  task_type: string;
  aggregate_type: string;
  aggregate_id: string;
  state: 'PENDING' | 'RETRY' | 'RUNNING' | 'SUCCEEDED' | 'DEAD';
  attempts: number;
  max_attempts: number;
  available_at: string;
  created_at: string;
  updated_at: string;
  failure_code: string | null;
  flow_definition_name: string | null;
  flow_run_name: string | null;
  flow_run_no: number | null;
  flow_run_state: string | null;
  node_run_name: string | null;
  node_run_sequence_no: number | null;
  node_attempt_no: number | null;
  node_attempt_state: string | null;
  workspace_display_name: string | null;
  workspace_scope_key: string | null;
};

export type BackgroundTaskSummary = {
  states: Array<{ state: string; count: number }>;
  expired_terminal: Array<{ state: string; count: number }>;
  groups: Array<{
    state: string;
    task_type: string;
    count: number;
    oldest_created_at: string;
    newest_updated_at: string;
  }>;
  retention_days: number;
};

export type Overview = {
  database: {
    runtime_states: Array<{ runtime_kind: string; status: string; count: number }>;
    tasks: Array<{ state: string; count: number; oldest_at: string | null }>;
    database_connections: Array<{ state: string | null; count: number }>;
  };
  services: Record<string, { health: string; metrics: Record<string, Array<{ labels: string; value: string }>> }>;
  container_observability: { available: boolean; services: ServiceObservation[] };
};

const adminApiBase = `${import.meta.env.BASE_URL.replace(/admin\/?$/, 'admin-api').replace(/\/?$/, '/')}`;

export class AdminRequestError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
    readonly code: string,
    readonly requestId: string,
    message: string,
  ) {
    super(message);
    this.name = 'AdminRequestError';
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const requestId = crypto.randomUUID();
  const headers = new Headers(options?.headers);
  headers.set('X-Request-ID', requestId);
  const response = await fetch(`${adminApiBase}${path.replace(/^\//, '')}`, {
    credentials: 'include',
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as {
      error?: { code?: string; message?: string; request_id?: string };
    } | null;
    const error = body?.error;
    const effectiveRequestId = response.headers.get('X-Request-ID') || error?.request_id || requestId;
    if (response.status === 401 || response.status === 403) {
      throw new AdminRequestError(path, response.status, 'ADMIN_AUTH_REQUIRED', effectiveRequestId, '需要使用 FlowWeave 超级管理员账户登录后访问管理中心。');
    }
    throw new AdminRequestError(
      path,
      response.status,
      error?.code || 'ADMIN_REQUEST_FAILED',
      effectiveRequestId,
      error?.message || `管理数据读取失败（${response.status}）。`,
    );
  }
  return response.json() as Promise<T>;
}

export const adminApi = {
  overview: () => request<Overview>('/v1/admin/overview'),
  alerts: () => request<{ items: Alert[]; summary: { critical: number; warning: number } }>('/v1/admin/alerts'),
  updateAlertLifecycle: (input: { alert_key: string; action: 'ACKNOWLEDGE' | 'SILENCE'; reason: string; silence_minutes?: number }) => request('/v1/admin/alerts/lifecycle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) }),
  metricHistory: (scope: 'SERVICE' | 'RUNTIME', subject: string, metric: 'cpu_usage_percent' | 'memory_usage_bytes' | 'storage_usage_bytes') => request<MetricHistory>(`/v1/admin/metric-history?scope=${scope}&subject=${encodeURIComponent(subject)}&metric=${metric}`),
  runtimes: () => request<{ items: Runtime[]; container_observability_available: boolean }>('/v1/admin/runtimes'),
  runtimeDetail: (runtimeSessionId: string) => request<RuntimeDetail>(`/v1/admin/runtimes/${encodeURIComponent(runtimeSessionId)}`),
  conversations: () => request<{ items: Conversation[] }>('/v1/admin/conversations'),
  backgroundTasks: () => request<{ summary: BackgroundTaskSummary; items: BackgroundTask[] }>('/v1/admin/background-tasks'),
  runtimeOperations: () => request<{ items: RuntimeOperation[] }>('/v1/admin/runtime-operations'),
  operations: () => request<{ items: AdminOperation[] }>('/v1/admin/operations'),
  diagnoseRuntime: (input: {
    runtime_kind: 'FLOW_RUN' | 'AGENT_WORKSPACE';
    owner_id: string;
    runtime_session_id: string;
    expected_generation: number;
    expected_session_row_version: number;
  }) => request<RuntimeDiagnostic>('/v1/admin/runtime-diagnostics', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) }),
  controlRuntime: (input: {
    action: 'REPLACE_RUNTIME' | 'ISOLATE_RUNTIME' | 'RESUME_RUNTIME';
    runtime_kind: 'FLOW_RUN' | 'AGENT_WORKSPACE';
    owner_id: string;
    flow_run_id?: string;
    runtime_session_id: string;
    expected_generation: number;
    expected_session_row_version: number;
    reason: string;
    idempotency_key: string;
  }) => request('/v1/admin/runtime-controls', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }),
};
