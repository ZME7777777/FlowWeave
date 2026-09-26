import { Activity, Boxes, Database, MessageSquare, RefreshCw, RotateCw, Server, ShieldCheck, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { AdminRequestError, adminApi, type AdminOperation, type Alert, type BackgroundTask, type BackgroundTaskSummary, type Conversation, type MetricHistoryPoint, type Overview, type Runtime, type RuntimeDetail, type RuntimeDiagnostic, type Usage } from './api';

type Tab = 'overview' | 'alerts' | 'services' | 'runtimes' | 'conversations' | 'tasks' | 'operations';

type AdminData = {
  overview: Overview;
  runtimes: Runtime[];
  conversations: Conversation[];
  tasks: BackgroundTask[];
  taskSummary: BackgroundTaskSummary;
  operations: AdminOperation[];
  alerts: Alert[];
  alertSummary: { critical: number; warning: number };
};

type AdminSection = 'overview' | 'alerts' | 'runtimes' | 'conversations' | 'tasks' | 'operations';
type SectionFailure = { section: AdminSection; message: string; status: number | null; code: string; requestId: string | null };

const sectionLabels: Record<AdminSection, string> = {
  overview: '总览',
  alerts: '实时告警',
  runtimes: 'Runtime',
  conversations: '会话',
  tasks: '后台任务',
  operations: '操作审计',
};

const emptyOverview: Overview = {
  database: { runtime_states: [], tasks: [], database_connections: [] },
  services: {},
  container_observability: { available: false, services: [] },
};

const emptyTaskSummary: BackgroundTaskSummary = { states: [], expired_terminal: [], groups: [], retention_days: 0 };

const bytes = (value: number | undefined) => {
  if (value === undefined) return '—';
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} MiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
};

const compact = (value: string | null | undefined) => value ? `${value.slice(0, 8)}…` : '—';

const runtimeOwnerLabel = (runtime: Runtime) => {
  if (runtime.runtime_kind === 'AGENT_WORKSPACE') return runtime.workspace_display_name || 'Agent 工作区';
  if (runtime.flow_run_name) return `${runtime.flow_definition_name || '未命名流程'} / ${runtime.flow_run_name} #${runtime.flow_run_no ?? '—'}`;
  return '未关联的 FlowRun';
};

const runtimeExecutionLabel = (runtime: Runtime) => runtime.node_attempt_no === null
  ? (runtime.flow_run_state ? `FlowRun · ${runtime.flow_run_state}` : 'FlowRun 级 Runtime')
  : `${runtime.node_run_name || `节点 #${runtime.node_run_sequence_no ?? '—'}`} · Attempt #${runtime.node_attempt_no} · ${runtime.node_attempt_state || '未知'}`;

const taskOwnerLabel = (task: BackgroundTask) => {
  if (task.workspace_display_name) return `${task.workspace_display_name} / ${task.workspace_scope_key || 'Agent Workspace'}`;
  if (task.flow_run_name) return `${task.flow_definition_name || '未命名流程'} / ${task.flow_run_name} #${task.flow_run_no ?? '—'}`;
  return `${task.aggregate_type} · ${compact(task.aggregate_id)}`;
};

const taskExecutionLabel = (task: BackgroundTask) => task.node_attempt_no === null
  ? (task.flow_run_state ? `FlowRun · ${task.flow_run_state}` : task.aggregate_type)
  : `${task.node_run_name || `节点 #${task.node_run_sequence_no ?? '—'}`} · Attempt #${task.node_attempt_no} · ${task.node_attempt_state || '未知'}`;

function ResourceUsage({ usage }: { usage: Usage | null }) {
  if (!usage) return <span className="muted">暂不可用</span>;
  return <span className="usage"><b>{usage.cpu_usage_percent.toFixed(1)}%</b><small>CPU</small><b>{bytes(usage.memory_usage_bytes)}</b><small>内存</small></span>;
}

function MetricCount({ label, values }: { label: string; values: Array<{ value: string }> | undefined }) {
  const total = (values ?? []).reduce((sum, item) => sum + Number(item.value), 0);
  return <article className="metric-card"><span>{label}</span><b>{Number.isFinite(total) ? total.toLocaleString('zh-CN') : '—'}</b></article>;
}

function MetricTrend({ points, label, format }: { points: MetricHistoryPoint[]; label: string; format: (value: number) => string }) {
  if (!points.length) return <p className="empty">尚无历史样本；采样服务启动后将显示最近 24 小时趋势。</p>;
  const width = 720;
  const height = 180;
  const values = points.map(point => point.value);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || 1;
  const polyline = points.map((point, index) => {
    const x = points.length === 1 ? width / 2 : index / (points.length - 1) * width;
    const y = height - 18 - (point.value - minimum) / range * (height - 36);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return <section className="trend"><header><b>{label}</b><span>{format(minimum)} – {format(maximum)}</span></header><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label}趋势`}><polyline points={polyline} fill="none" stroke="#6aa8ff" strokeWidth="3" vectorEffect="non-scaling-stroke"/></svg><footer><span>{new Date(points[0].observed_at).toLocaleTimeString()}</span><span>{new Date(points.at(-1)!.observed_at).toLocaleTimeString()}</span></footer></section>;
}

function HistoryDialog({ scope, subject, title, onClose }: { scope: 'SERVICE' | 'RUNTIME'; subject: string; title: string; onClose: () => void }) {
  const [cpu, setCpu] = useState<MetricHistoryPoint[]>();
  const [memory, setMemory] = useState<MetricHistoryPoint[]>();
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void Promise.all([
      adminApi.metricHistory(scope, subject, 'cpu_usage_percent'),
      adminApi.metricHistory(scope, subject, 'memory_usage_bytes'),
    ]).then(([cpuHistory, memoryHistory]) => {
      if (!active) return;
      setCpu(cpuHistory.items);
      setMemory(memoryHistory.items);
    }).catch(reason => {
      if (active) setError(reason instanceof Error ? reason.message : '历史指标读取失败。');
    });
    return () => { active = false; };
  }, [scope, subject]);
  return <div className="modal-backdrop" role="presentation"><section className="history-dialog" role="dialog" aria-modal="true" aria-labelledby="history-title"><header><div><span className="eyebrow">BOUNDED METRIC HISTORY</span><h2 id="history-title">{title} 趋势</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18}/></button></header><p>显示最近 24 小时的分钟聚合样本。历史仅保存资源数值，不保存容器日志、会话正文或凭据。</p>{error ? <p className="operation-warning">{error}</p> : !cpu || !memory ? <section className="loading"><Activity className="spin" size={22}/><p>读取历史样本…</p></section> : <><MetricTrend points={cpu} label="CPU 使用率" format={value => `${value.toFixed(1)}%`}/><MetricTrend points={memory} label="内存使用量" format={bytes}/></>}<footer><button className="secondary" onClick={onClose}>关闭</button></footer></section></div>;
}


function AlertLifecycleDialog({ alert, onClose, onSubmitted }: { alert: Alert; onClose: () => void; onSubmitted: () => Promise<void> }) {
  const [action, setAction] = useState<'ACKNOWLEDGE' | 'SILENCE'>('ACKNOWLEDGE');
  const [reason, setReason] = useState('');
  const [minutes, setMinutes] = useState(60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async () => {
    if (reason.trim().length < 10) return;
    setBusy(true); setError('');
    try {
      await adminApi.updateAlertLifecycle({ alert_key: alert.key, action, reason: reason.trim(), ...(action === 'SILENCE' ? { silence_minutes: minutes } : {}) });
      await onSubmitted(); onClose();
    } catch (failure) { setError(failure instanceof Error ? failure.message : '告警状态更新失败。'); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation"><section className="replacement-dialog" role="dialog" aria-modal="true" aria-labelledby="alert-lifecycle-title"><header><div><span className="eyebrow">ALERT LIFECYCLE</span><h2 id="alert-lifecycle-title">确认或静默告警</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18}/></button></header><p><b>{alert.title}</b><br/><small>{alert.key}</small></p><label>操作<select value={action} onChange={event => setAction(event.target.value as 'ACKNOWLEDGE' | 'SILENCE')}><option value="ACKNOWLEDGE">确认：已知悉，继续显示</option><option value="SILENCE">静默：在限定时间内隐藏</option></select></label>{action === 'SILENCE' && <label>静默时长<select value={minutes} onChange={event => setMinutes(Number(event.target.value))}><option value={30}>30 分钟</option><option value={60}>1 小时</option><option value={240}>4 小时</option><option value={1440}>24 小时</option></select></label>}<label>处理说明<textarea value={reason} minLength={10} maxLength={500} placeholder="说明已确认的原因或静默依据。" onChange={event => setReason(event.target.value)}/><small>至少 10 个字符；操作人、原因与静默截止时间会进入追加式审计。</small></label>{error && <p className="operation-warning">{error}</p>}<footer><button className="secondary" onClick={onClose} disabled={busy}>取消</button><button className="danger" disabled={busy || reason.trim().length < 10} onClick={() => void submit()}>{busy ? '提交中…' : action === 'SILENCE' ? '确认静默' : '确认告警'}</button></footer></section></div>;
}

function RuntimeDetailDialog({ runtime, onClose, onSubmitted }: { runtime: Runtime; onClose: () => void; onSubmitted: () => Promise<void> }) {
  const [detail, setDetail] = useState<RuntimeDetail>();
  const [diagnostic, setDiagnostic] = useState<RuntimeDiagnostic>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void adminApi.runtimeDetail(runtime.runtime_session_id).then(value => { if (active) setDetail(value); }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Runtime 详情读取失败。'); });
    return () => { active = false; };
  }, [runtime.runtime_session_id]);
  const current = detail?.runtime;
  const persistedDiagnostic = current?.business_diagnostic_status ? {
    status: current.business_diagnostic_status,
    impacted_bindings: current.business_impacted_bindings ?? 0,
    event_count: current.business_event_count,
    readiness: current.business_readiness_status ? { ready: current.business_readiness_status === 'ready', execution_status: current.business_readiness_status } : null,
    runtime_availability: current.business_runtime_availability,
    stages: current.business_stages ?? [],
  } as RuntimeDiagnostic : undefined;
  const displayedDiagnostic = diagnostic ?? persistedDiagnostic;
  const canDiagnose = current && current.active_generation !== null && current.status === 'ACTIVE';
  const canControl = current && current.active_generation !== null && current.node_attempt_id === null && (current.status === 'ACTIVE' || current.status === 'DEGRADED' || current.status === 'MAINTENANCE');
  const diagnose = async () => {
    if (!current || current.active_generation === null) return;
    setBusy(true); setError('');
    try {
      const result = await adminApi.diagnoseRuntime({ runtime_kind: current.runtime_kind as 'FLOW_RUN' | 'AGENT_WORKSPACE', owner_id: current.owner_id, runtime_session_id: current.runtime_session_id, expected_generation: current.active_generation, expected_session_row_version: current.row_version });
      setDiagnostic(result);
      await onSubmitted();
      setDetail(await adminApi.runtimeDetail(runtime.runtime_session_id));
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : '正式业务诊断失败。'); }
    finally { setBusy(false); }
  };
  const control = async (action: 'ISOLATE_RUNTIME' | 'RESUME_RUNTIME') => {
    if (!current || current.active_generation === null) return;
    const reason = action === 'ISOLATE_RUNTIME' ? '管理员基于正式 Runtime 诊断隔离新写入。' : '管理员确认当前 Runtime generation 可恢复路由。';
    setBusy(true); setError('');
    try {
      await adminApi.controlRuntime({ action, runtime_kind: current.runtime_kind as 'FLOW_RUN' | 'AGENT_WORKSPACE', owner_id: current.owner_id, ...(current.runtime_kind === 'FLOW_RUN' ? { flow_run_id: current.owner_id } : {}), runtime_session_id: current.runtime_session_id, expected_generation: current.active_generation, expected_session_row_version: current.row_version, reason, idempotency_key: crypto.randomUUID() });
      await onSubmitted();
      setDetail(await adminApi.runtimeDetail(runtime.runtime_session_id));
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Runtime 控制操作失败。'); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation"><section className="runtime-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="runtime-detail-title"><header><div><span className="eyebrow">RUNTIME OBSERVATION</span><h2 id="runtime-detail-title">Runtime 详情</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18}/></button></header><p>业务诊断只读取一个已关联 Conversation 的正式 Runtime availability、当前事件窗口和输入 readiness；不读取消息正文、不触发自动替换。隔离只拒绝新写入，保留持久化数据与现有诊断现场。</p>{error ? <p className="operation-warning">{error}</p> : !detail || !current ? <section className="loading"><Activity className="spin" size={22}/><p>读取 Runtime 事实快照…</p></section> : <div className="runtime-detail"><dl><dt>关联资源</dt><dd><b>{runtimeOwnerLabel(current)}</b><br/><small>{runtimeExecutionLabel(current)} · Owner <code>{current.owner_id}</code></small></dd><dt>状态 / 当前 generation</dt><dd><b className={`state ${current.status.toLowerCase()}`}>{current.status}</b> · {current.active_generation ?? '未分配'} · {current.generation_state ?? '未知'}</dd><dt>容器观察</dt><dd>{detail.container_observability_available ? '可用' : '暂不可用（控制面记录仍可查看）'} · <code>{compact(current.container_id)}</code></dd><dt>最近控制面活动</dt><dd>{current.last_activity_at ? new Date(current.last_activity_at).toLocaleString() : '无记录'}</dd><dt>最近诊断</dt><dd>{current.failure_code || current.last_error_code || '无控制面诊断错误'}</dd></dl><section className="runtime-controls"><h3>业务诊断与处置</h3><p>{displayedDiagnostic ? `结果：${displayedDiagnostic.status} · 影响绑定 ${displayedDiagnostic.impacted_bindings} · 事件 ${displayedDiagnostic.event_count ?? '—'} · Runtime ${displayedDiagnostic.runtime_availability ?? '—'}` : '尚未执行正式业务诊断。'}</p><div><button className="locator-button" disabled={!canDiagnose || busy} onClick={() => void diagnose()}>运行正式诊断</button>{current.node_attempt_id !== null ? <small>Node Attempt Runtime 暂不提供人工路由控制。</small> : current.status === 'MAINTENANCE' ? <button className="locator-button" disabled={!canControl || busy} onClick={() => void control('RESUME_RUNTIME')}>恢复新写入</button> : <button className="locator-button" disabled={!canControl || busy} onClick={() => void control('ISOLATE_RUNTIME')}>隔离新写入</button>}</div>{displayedDiagnostic && <div className="detail-table">{displayedDiagnostic.stages.map(stage => <div key={stage.name}><b>{stage.name}</b><span>{stage.outcome} · {stage.duration_ms}ms</span><small>{stage.error_code || '正式读取成功；未返回内容。'}</small></div>)}</div>}</section><section><h3>Generation 生命周期</h3><div className="detail-table">{detail.generations.map(generation => <div key={generation.generation}><b>Gen {generation.generation}</b><span>{generation.state}</span><small>开始 {generation.started_at ? new Date(generation.started_at).toLocaleString() : '—'} · 就绪 {generation.ready_at ? new Date(generation.ready_at).toLocaleString() : '—'} · 停止 {generation.stopped_at ? new Date(generation.stopped_at).toLocaleString() : '—'}</small>{generation.failure_code && <small className="detail-error">{generation.failure_code} · {generation.failure_summary || '无摘要'}</small>}</div>) || <p className="empty">暂无 generation 记录。</p>}</div></section><section><h3>会话影响范围</h3><div className="detail-table">{detail.conversation_summary.map(item => <div key={item.lifecycle}><b>{item.lifecycle}</b><span>{item.count} 个绑定</span><small>最近连接 {item.last_connected_at ? new Date(item.last_connected_at).toLocaleString() : '—'} · 最近更新 {item.last_updated_at ? new Date(item.last_updated_at).toLocaleString() : '—'}</small></div>) || <p className="empty">暂无绑定会话。</p>}</div></section><section><h3>最近受控操作</h3><div className="detail-table">{detail.operations.map(operation => <div key={operation.operation_id}><b>{operation.action}</b><span>{operation.status} · Gen {operation.expected_generation}</span><small>{operation.actor_username} · {new Date(operation.created_at).toLocaleString()} · {operation.reason}</small></div>) || <p className="empty">暂无管理操作审计。</p>}</div></section></div>}<footer><button className="secondary" onClick={onClose}>关闭</button></footer></section></div>;
}

function RuntimeReplacementDialog({ runtime, onClose, onSubmitted }: {
  runtime: Runtime;
  onClose: () => void;
  onSubmitted: () => Promise<void>;
}) {
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [idempotencyKey] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const eligible = (runtime.runtime_kind === 'FLOW_RUN' || runtime.runtime_kind === 'AGENT_WORKSPACE')
    && runtime.active_generation !== null
    && (runtime.status === 'ACTIVE' || runtime.status === 'DEGRADED' || runtime.status === 'MAINTENANCE');
  const submit = async () => {
    if (!eligible || runtime.active_generation === null || reason.trim().length < 10 || !confirmed) return;
    setBusy(true);
    setError('');
    try {
      await adminApi.controlRuntime({ action: 'REPLACE_RUNTIME',
        runtime_kind: runtime.runtime_kind as 'FLOW_RUN' | 'AGENT_WORKSPACE',
        owner_id: runtime.owner_id,
        ...(runtime.runtime_kind === 'FLOW_RUN' ? { flow_run_id: runtime.owner_id } : {}),
        runtime_session_id: runtime.runtime_session_id,
        expected_generation: runtime.active_generation,
        expected_session_row_version: runtime.row_version,
        reason: reason.trim(),
        idempotency_key: idempotencyKey,
      });
      await onSubmitted();
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '替换请求提交失败。');
    } finally {
      setBusy(false);
    }
  };
  return <div className="modal-backdrop" role="presentation"><section className="replacement-dialog" role="dialog" aria-modal="true" aria-labelledby="replace-title"><header><div><span className="eyebrow">CONTROLLED RUNTIME OPERATION</span><h2 id="replace-title">替换 Runtime generation</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18}/></button></header><p>该操作会 fence 当前 generation 的新写入，并通过既有 FlowWeave 生命周期恢复持久的 OpenHands 会话和 Workspace；不会执行 Docker restart、删除容器或清理持久数据。</p><dl><dt>Runtime 类型</dt><dd>{runtime.runtime_kind}</dd><dt>Runtime Session</dt><dd><code>{runtime.runtime_session_id}</code></dd><dt>当前 generation</dt><dd>{runtime.active_generation ?? '—'}</dd><dt>当前状态</dt><dd>{runtime.status}</dd></dl>{!eligible && <p className="operation-warning">仅可替换 ACTIVE 或 DEGRADED 且存在当前 generation 的受管 Runtime；请先刷新详情确认其控制面状态。</p>}<label>替换原因<textarea value={reason} minLength={10} maxLength={500} placeholder="例如：正式会话读取持续超时，需要受控恢复 Runtime。" onChange={event => setReason(event.target.value)}/><small>至少 10 个字符；原因会进入不可变管理操作审计。</small></label><label className="confirm-check"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)}/><span>我确认将替换当前 generation，并理解运行中会话将短暂进入恢复状态。</span></label>{error && <p className="operation-warning">{error}</p>}<footer><button className="secondary" onClick={onClose} disabled={busy}>取消</button><button className="danger" disabled={!eligible || !confirmed || reason.trim().length < 10 || busy} onClick={() => void submit()}>{busy ? '提交中…' : '确认替换 generation'}</button></footer></section></div>;
}


export function App() {
  const [tab, setTab] = useState<Tab>('overview');
  const [data, setData] = useState<AdminData>();
  const [historyTarget, setHistoryTarget] = useState<{ scope: 'SERVICE' | 'RUNTIME'; subject: string; title: string }>();
  const [alertTarget, setAlertTarget] = useState<Alert>();
  const [replacementTarget, setReplacementTarget] = useState<Runtime>();
  const [detailTarget, setDetailTarget] = useState<Runtime>();
  const [operationAction, setOperationAction] = useState<'ALL' | AdminOperation['action']>('ALL');
  const [operationActor, setOperationActor] = useState('');
  const [operationTarget, setOperationTarget] = useState('');
  const [runtimeFilter, setRuntimeFilter] = useState('');
  const [conversationRuntimeFilter, setConversationRuntimeFilter] = useState('');
  const [taskStateFilter, setTaskStateFilter] = useState<'ALL' | BackgroundTask['state']>('ALL');
  const [taskFilter, setTaskFilter] = useState('');
  const [failures, setFailures] = useState<SectionFailure[]>([]);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date>();

  const refresh = useCallback(async () => {
    setLoading(true);
    const requests: Array<{ section: AdminSection; request: Promise<unknown> }> = [
      { section: 'overview', request: adminApi.overview() },
      { section: 'alerts', request: adminApi.alerts() },
      { section: 'runtimes', request: adminApi.runtimes() },
      { section: 'conversations', request: adminApi.conversations() },
      { section: 'tasks', request: adminApi.backgroundTasks() },
      { section: 'operations', request: adminApi.operations() },
    ];
    const results = await Promise.allSettled(requests.map(item => item.request));
    const nextFailures: SectionFailure[] = [];
    setData(previous => {
      const next: AdminData = previous ?? {
        overview: emptyOverview,
        runtimes: [],
        conversations: [],
        tasks: [],
        taskSummary: emptyTaskSummary,
        operations: [],
        alerts: [],
        alertSummary: { critical: 0, warning: 0 },
      };
      results.forEach((result, index) => {
        const section = requests[index].section;
        if (result.status === 'rejected') {
          const failure = result.reason;
          nextFailures.push(failure instanceof AdminRequestError
            ? { section, message: failure.message, status: failure.status, code: failure.code, requestId: failure.requestId }
            : { section, message: failure instanceof Error ? failure.message : '管理数据读取失败。', status: null, code: 'ADMIN_REQUEST_UNCLASSIFIED', requestId: null });
          return;
        }
        if (section === 'overview') next.overview = result.value as Overview;
        if (section === 'alerts') {
          const value = result.value as { items: Alert[]; summary: { critical: number; warning: number } };
          next.alerts = value.items; next.alertSummary = value.summary;
        }
        if (section === 'runtimes') next.runtimes = (result.value as { items: Runtime[] }).items;
        if (section === 'conversations') next.conversations = (result.value as { items: Conversation[] }).items;
        if (section === 'tasks') {
          const value = result.value as { summary: BackgroundTaskSummary; items: BackgroundTask[] };
          next.tasks = value.items; next.taskSummary = value.summary;
        }
        if (section === 'operations') next.operations = (result.value as { items: AdminOperation[] }).items;
      });
      return { ...next };
    });
    setFailures(nextFailures);
    if (nextFailures.length < requests.length) setUpdatedAt(new Date());
    setLoading(false);
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => { void refresh(); }, 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const services = data?.overview.container_observability.services ?? [];
  const metrics = data?.overview.services;
  const runtimeStates = data?.overview.database.runtime_states ?? [];
  const taskStates = data?.overview.database.tasks ?? [];
  const databaseStates = data?.overview.database.database_connections ?? [];
  const visibleAlerts = (data?.alerts ?? []).filter(alert => {
    const until = alert.lifecycle?.silenced_until;
    return !until || new Date(until).getTime() <= Date.now();
  });
  const filteredOperations = (data?.operations ?? []).filter(operation =>
    (operationAction === 'ALL' || operation.action === operationAction)
    && operation.actor_username.toLocaleLowerCase().includes(operationActor.trim().toLocaleLowerCase())
    && operation.target_id.toLocaleLowerCase().includes(operationTarget.trim().toLocaleLowerCase())
  );
  const filteredRuntimes = (data?.runtimes ?? []).filter(runtime => {
    const query = runtimeFilter.trim().toLocaleLowerCase();
    return !query || [
      runtime.runtime_session_id,
      runtime.owner_id,
      runtime.flow_definition_name,
      runtime.flow_run_name,
      runtime.node_run_name,
      runtime.workspace_display_name,
      runtime.workspace_scope_key,
    ].some(value => value?.toLocaleLowerCase().includes(query));
  });
  const filteredConversations = (data?.conversations ?? []).filter(conversation =>
    conversation.runtime_session_id.includes(conversationRuntimeFilter.trim())
  );
  const filteredTasks = (data?.tasks ?? []).filter(task => {
    const query = taskFilter.trim().toLocaleLowerCase();
    return (taskStateFilter === 'ALL' || task.state === taskStateFilter)
      && (!query || [task.task_type, taskOwnerLabel(task), taskExecutionLabel(task), task.failure_code, task.aggregate_id]
        .some(value => value?.toLocaleLowerCase().includes(query)));
  });
  const taskStateCount = (state: string) => data?.taskSummary.states.find(item => item.state === state)?.count ?? 0;
  const expiredTerminalCount = (data?.taskSummary.expired_terminal ?? []).reduce((sum, item) => sum + item.count, 0);
  const locateRuntime = (runtimeSessionId: string) => {
    setRuntimeFilter(runtimeSessionId); setTab('runtimes');
  };
  const locateConversations = (runtimeSessionId: string) => {
    setConversationRuntimeFilter(runtimeSessionId); setTab('conversations');
  };
  const locateOperations = (targetId: string) => {
    setOperationTarget(targetId); setTab('operations');
  };
  const runtimeIdFromAlert = (key: string) => {
    const match = /^runtime:([^:]+)/.exec(key);
    return match?.[1];
  };


  const visibleContent = () => {
    if (!data) return null;
    if (tab === 'services') return <section className="table-panel"><header><div><span className="eyebrow">CONTROL PLANE</span><h2>服务与容器</h2></div><p>仅采集 Compose 服务的只读资源快照。</p></header><div className="table"><div className="table-head"><span>服务</span><span>状态</span><span>CPU / 内存</span><span>容器</span><span>镜像</span></div>{services.map(service => <div className="table-row" key={service.container_id}><span><b>{service.service}</b><small>{service.status || '—'}</small></span><span className={`state ${service.state.toLowerCase()}`}>{service.state}</span><ResourceUsage usage={service.usage}/><span><code>{compact(service.container_id)}</code></span><span><button className="history-button" onClick={() => setHistoryTarget({ scope: 'SERVICE', subject: service.service, title: service.service })}>趋势</button><small>{service.image}</small></span></div>)}{!services.length && <p className="empty">Runtime Provider 尚未返回 Compose 容器资源快照。</p>}</div></section>;
    if (tab === 'runtimes') return <section className="table-panel"><header><div><span className="eyebrow">OPENHANDS EXECUTION</span><h2>Runtime 与容器</h2></div><p>Runtime Session 是管理身份；容器仅是可替换 generation 的载体。列表仅展示控制面事实，不把未执行的业务探针显示为健康。</p></header><div className="resource-filter"><input value={runtimeFilter} placeholder="按 Runtime、FlowRun、节点或工作区定位" onChange={event => setRuntimeFilter(event.target.value)}/>{runtimeFilter && <button className="secondary" onClick={() => setRuntimeFilter('')}>清除定位</button>}</div><div className="table wide"><div className="table-head runtime-head"><span>Runtime</span><span>状态</span><span>会话</span><span>容器资源</span><span>活动</span><span>诊断</span><span>操作</span></div>{filteredRuntimes.map(runtime => <div className="table-row runtime-head" key={runtime.runtime_session_id}><span><b>{runtime.runtime_kind}</b><small>{runtimeOwnerLabel(runtime)}</small><small>{runtimeExecutionLabel(runtime)}</small><small title={runtime.runtime_session_id}>Session {compact(runtime.runtime_session_id)} · Owner {compact(runtime.owner_id)} · Gen {runtime.active_generation ?? '—'}</small></span><span><b className={`state ${runtime.status.toLowerCase()}`}>{runtime.status}</b><small>{runtime.generation_state ?? '未分配'} · {runtime.observed_state ?? '—'}</small></span><span><b>{runtime.active_conversation_count} / {runtime.conversation_count}</b><small>活跃 / 关联</small></span><ResourceUsage usage={runtime.usage}/><span><code>{compact(runtime.container_id)}</code><small>{runtime.last_activity_at ? new Date(runtime.last_activity_at).toLocaleString() : '无活动记录'}</small></span><span><b>{runtime.business_diagnostic_status ? `业务 ${runtime.business_diagnostic_status}` : runtime.failure_code || runtime.last_error_code || '未见控制面错误'}</b><small>{runtime.business_diagnostic_status ? `${runtime.business_impacted_bindings ?? 0} 个绑定 · ${runtime.business_observed_at ? new Date(runtime.business_observed_at).toLocaleString() : '刚刚诊断'}` : runtime.failure_summary || runtime.last_error_detail || '未执行业务探针'}</small></span><span><button className="history-button" onClick={() => setHistoryTarget({ scope: 'RUNTIME', subject: runtime.managed_sandbox_id || runtime.runtime_session_id, title: `Runtime ${compact(runtime.runtime_session_id)}` })}>趋势</button><button className="locator-button" onClick={() => setDetailTarget(runtime)}>详情</button><button className="locator-button" onClick={() => locateConversations(runtime.runtime_session_id)}>会话</button><button className="locator-button" onClick={() => locateOperations(runtime.runtime_session_id)}>审计</button><button className="replace-button" disabled={runtime.active_generation === null || !['ACTIVE', 'DEGRADED'].includes(runtime.status)} onClick={() => setReplacementTarget(runtime)}><RotateCw size={14}/>替换</button></span></div>)}{!filteredRuntimes.length && <p className="empty">没有符合当前定位条件的 Runtime。</p>}</div></section>;
    if (tab === 'alerts') return <section className="table-panel"><header><div><span className="eyebrow">LIVE HEALTH SIGNALS</span><h2>实时健康告警</h2></div><p>基于当前服务、容器、Runtime、数据库连接和任务积压快照计算；此处不发送外部通知。</p></header><div className="alert-summary"><b className="critical-count">{data.alertSummary.critical} 严重</b><b className="warning-count">{data.alertSummary.warning} 警告</b><small>阈值由 Admin API 环境配置控制。</small></div><div className="table wide"><div className="table-head alert-head"><span>级别</span><span>来源</span><span>告警</span><span>详情</span><span>操作</span></div>{visibleAlerts.map(alert => <div className="table-row alert-head" key={alert.key}><span><b className={`alert-severity ${alert.severity.toLowerCase()}`}>{alert.severity}</b></span><span><b>{alert.source}</b></span><span>{alert.title}<small>{alert.lifecycle?.acknowledged_by_username ? `已由 ${alert.lifecycle.acknowledged_by_username} 确认` : '未确认'}</small></span><span><small className="reason">{alert.detail}</small></span><span><button className="history-button" onClick={() => setAlertTarget(alert)}>确认 / 静默</button><button className="locator-button" onClick={() => locateOperations(alert.key)}>审计</button>{runtimeIdFromAlert(alert.key) && <button className="locator-button" onClick={() => locateRuntime(runtimeIdFromAlert(alert.key)!)}>Runtime</button>}</span></div>)}{!visibleAlerts.length && <p className="empty">当前没有未静默的实时健康告警。</p>}</div></section>;

    if (tab === 'conversations') return <section className="table-panel"><header><div><span className="eyebrow">OPENHANDS LOCATORS</span><h2>会话关联</h2></div><p>只显示定位与运行元数据，不读取会话正文或事件内容。</p></header><div className="resource-filter"><input value={conversationRuntimeFilter} placeholder="按 Runtime、FlowRun、节点或工作区定位" onChange={event => setConversationRuntimeFilter(event.target.value)}/>{conversationRuntimeFilter && <button className="secondary" onClick={() => setConversationRuntimeFilter('')}>清除定位</button>}</div><div className="table wide"><div className="table-head conversation-head"><span>会话</span><span>用户 / 宿主</span><span>Runtime</span><span>模型</span><span>连接时间</span></div>{filteredConversations.map(conversation => <div className="table-row conversation-head" key={conversation.binding_id}><span><b>{conversation.display_title || '未命名会话'}</b><small>Binding {compact(conversation.binding_id)} · {conversation.lifecycle}</small><small>OpenHands {compact(conversation.openhands_conversation_id)}</small></span><span><b>{conversation.username || compact(conversation.owner_user_id)}</b><small>{conversation.host_kind} · {compact(conversation.host_id)}</small></span><span><code>{compact(conversation.runtime_session_id)}</code><button className="locator-button" onClick={() => locateRuntime(conversation.runtime_session_id)}>查看 Runtime</button><small>{conversation.flow_run_id ? `FlowRun ${compact(conversation.flow_run_id)}` : 'Agent Workspace'}</small></span><span>{conversation.model_name || '—'}</span><span>{conversation.last_connected_at ? new Date(conversation.last_connected_at).toLocaleString() : '从未连接'}</span></div>)}{!filteredConversations.length && <p className="empty">没有符合当前定位条件的会话绑定。</p>}</div></section>;
    if (tab === 'tasks') return <section className="table-panel"><header><div><span className="eyebrow">DURABLE DELIVERY LEDGER</span><h2>后台任务</h2></div><p>活跃工作与终态执行账本分开展示；列表不返回 payload、会话正文或原始错误。终态记录只会在超过 {data.taskSummary.retention_days} 天保留期后由既有 Worker 策略分批回收。</p></header><section className="task-summary"><article><span>活跃待处理</span><b>{taskStateCount('PENDING') + taskStateCount('RETRY') + taskStateCount('RUNNING')}</b><small>PENDING / RETRY / RUNNING</small></article><article><span>最终失败账本</span><b>{taskStateCount('DEAD')}</b><small>DEAD，不会继续重试</small></article><article><span>最终成功账本</span><b>{taskStateCount('SUCCEEDED')}</b><small>SUCCEEDED，保留供诊断</small></article><article><span>已到期可回收</span><b>{expiredTerminalCount}</b><small>仅终态；无手工删除入口</small></article></section><div className="operation-filters"><label>状态<select value={taskStateFilter} onChange={event => setTaskStateFilter(event.target.value as 'ALL' | BackgroundTask['state'])}><option value="ALL">全部状态</option><option value="PENDING">PENDING</option><option value="RETRY">RETRY</option><option value="RUNNING">RUNNING</option><option value="DEAD">DEAD</option><option value="SUCCEEDED">SUCCEEDED</option></select></label><label>任务 / 资源<input value={taskFilter} placeholder="按类型、FlowRun、节点、工作区或失败码筛选" onChange={event => setTaskFilter(event.target.value)}/></label>{taskFilter && <button className="secondary" onClick={() => setTaskFilter('')}>清除定位</button>}<small>当前加载最新 500 条；下方分组展示全量 Top 100。</small></div><div className="table wide"><div className="table-head task-head"><span>任务</span><span>状态 / 重试</span><span>关联资源</span><span>时间</span><span>失败分类</span></div>{filteredTasks.map(task => <div className="table-row task-head" key={task.id}><span><b>{task.task_type}</b><small>{task.aggregate_type} · {compact(task.id)}</small></span><span><b className={`operation-status ${task.state.toLowerCase()}`}>{task.state}</b><small>{task.attempts} / {task.max_attempts} 次 · {task.state === 'DEAD' ? '最终失败' : task.state === 'SUCCEEDED' ? '已完成' : '可处理'}</small></span><span><b>{taskOwnerLabel(task)}</b><small>{taskExecutionLabel(task)}</small></span><span><small>创建 {new Date(task.created_at).toLocaleString()}</small><small>更新 {new Date(task.updated_at).toLocaleString()}</small></span><span><b>{task.failure_code || '—'}</b><small>{task.failure_code ? '已脱敏稳定分类' : '无最终错误'}</small></span></div>)}{!filteredTasks.length && <p className="empty">没有符合筛选条件的后台任务。</p>}</div><section className="task-groups"><h3>类型 / 状态汇总</h3><div className="detail-table">{data.taskSummary.groups.map(group => <div key={`${group.state}-${group.task_type}`}><b>{group.task_type}</b><span>{group.state} · {group.count}</span><small>最早 {new Date(group.oldest_created_at).toLocaleString()} · 最近 {new Date(group.newest_updated_at).toLocaleString()}</small></div>)}</div></section></section>;

    if (tab === 'operations') return <section className="table-panel"><header><div><span className="eyebrow">IMMUTABLE ADMIN AUDIT</span><h2>全部管理操作审计</h2></div><p>合并展示 Runtime 替换、告警确认与静默；不显示密钥、会话正文或日志内容。</p></header><div className="operation-filters"><label>操作<select value={operationAction} onChange={event => setOperationAction(event.target.value as 'ALL' | AdminOperation['action'])}><option value="ALL">全部操作</option><option value="REPLACE_RUNTIME">Runtime 替换</option><option value="ISOLATE_RUNTIME">隔离 Runtime</option><option value="RESUME_RUNTIME">恢复 Runtime</option><option value="ACKNOWLEDGE">告警确认</option><option value="SILENCE">告警静默</option></select></label><label>操作人<input value={operationActor} placeholder="按管理员筛选" onChange={event => setOperationActor(event.target.value)}/></label><label>目标<input value={operationTarget} placeholder="按 Runtime 或告警键定位" onChange={event => setOperationTarget(event.target.value)}/></label>{operationTarget && <button className="secondary" onClick={() => setOperationTarget('')}>清除定位</button>}<small>当前加载最近 7 天的 200 条记录。</small></div><div className="table wide"><div className="table-head unified-operation-head"><span>操作</span><span>操作人</span><span>目标</span><span>原因</span><span>状态 / 时限</span></div>{filteredOperations.map(operation => <div className="table-row unified-operation-head" key={operation.id}><span><b>{operation.action}</b><small>{new Date(operation.created_at).toLocaleString()}</small><small>请求 {compact(operation.request_id)}</small></span><span><b>{operation.actor_username}</b><small>{compact(operation.actor_user_id)}</small></span><span><b>{operation.target_kind}</b><code>{compact(operation.target_id)}</code><small>{operation.target_detail ? `FlowRun ${compact(operation.target_detail)}` : '告警稳定键'}</small></span><span><small className="reason">{operation.reason}</small></span><span><b className={`operation-status ${operation.status.toLowerCase()}`}>{operation.status}</b><small>{operation.silenced_until ? `静默至 ${new Date(operation.silenced_until).toLocaleString()}` : operation.action === 'REPLACE_RUNTIME' ? '基于 Runtime / Generation 正式状态投影' : '已追加审计'}</small></span></div>)}{!filteredOperations.length && <p className="empty">没有符合筛选条件的管理操作。</p>}</div></section>;

    return <><section className="hero"><div><span className="eyebrow">FLOWWEAVE ADMIN</span><h1>运行资源管理中心</h1><p>独立监控服务、请求连接池与 OpenHands Runtime 容器。两类 Runtime 的替换均须经原因确认、并发 fencing 与不可变审计后提交。</p></div><ShieldCheck size={44}/></section><section className="metrics"><MetricCount label="活跃 Runtime" values={runtimeStates.filter(item => item.status === 'ACTIVE').map(item => ({ value: String(item.count) }))}/><MetricCount label="运行中任务" values={taskStates.filter(item => item.state === 'RUNNING').map(item => ({ value: String(item.count) }))}/><MetricCount label="数据库连接" values={databaseStates.map(item => ({ value: String(item.count) }))}/><MetricCount label="API 请求计数" values={metrics?.api.metrics.flowweave_http_requests_total}/></section><section className="overview-grid"><article className="panel"><header><Activity size={17}/><h2>服务健康</h2></header>{Object.entries(metrics ?? {}).map(([name, value]) => <div className="status-line" key={name}><span>{name}</span><b className={`state ${value.health.toLowerCase()}`}>{value.health}</b></div>)}</article><article className="panel"><header><Database size={17}/><h2>数据库连接状态</h2></header>{databaseStates.map(item => <div className="status-line" key={item.state ?? 'unknown'}><span>{item.state || 'unknown'}</span><b>{item.count}</b></div>)}</article><article className="panel"><header><Boxes size={17}/><h2>Runtime 状态</h2></header>{runtimeStates.map(item => <div className="status-line" key={`${item.runtime_kind}-${item.status}`}><span>{item.runtime_kind} · {item.status}</span><b>{item.count}</b></div>)}</article><article className="panel"><header><MessageSquare size={17}/><h2>后台任务</h2></header>{taskStates.map(item => <div className="status-line" key={item.state}><span>{item.state}</span><b>{item.count}</b></div>)}</article></section></>;

  };

  return <><main><header className="topbar"><div><span className="brand-mark"><Server size={18}/></span><b>FlowWeave 管理中心</b><small>独立运维入口</small></div><nav>{([['overview', '总览'], ['alerts', '实时告警'], ['services', '服务'], ['runtimes', 'Runtime'], ['conversations', '会话'], ['tasks', '后台任务'], ['operations', '操作审计']] as const).map(([value, label]) => <button className={tab === value ? 'active' : ''} key={value} onClick={() => setTab(value)}>{label}</button>)}</nav><button className="refresh" disabled={loading} onClick={() => void refresh()}><RefreshCw size={15} className={loading ? 'spin' : ''}/>{loading ? '刷新中' : '刷新'}</button></header><div className="content">{updatedAt && <p className="updated">最近刷新：{updatedAt.toLocaleTimeString()} · 每 15 秒自动更新</p>}{failures.length > 0 && <section className="partial-error"><h2>{failures.length === 6 ? '管理数据暂时不可用' : '部分管理数据暂时不可用'}</h2><p>其余数据会保留并继续刷新；请使用下列错误码和请求 ID 查询服务端日志。</p>{failures.map(failure => <div key={failure.section}><b>{sectionLabels[failure.section]}</b><span>{failure.message}</span><code>{failure.code}{failure.status !== null ? ` · HTTP ${failure.status}` : ''}{failure.requestId ? ` · 请求 ${compact(failure.requestId)}` : ''}</code></div>)}<button onClick={() => void refresh()}>重新尝试</button></section>}{loading && !data ? <section className="loading"><Activity className="spin" size={28}/><p>正在读取独立管理数据…</p></section> : visibleContent()}</div>{replacementTarget && <RuntimeReplacementDialog runtime={replacementTarget} onClose={() => setReplacementTarget(undefined)} onSubmitted={refresh}/>}</main>{detailTarget && <RuntimeDetailDialog runtime={detailTarget} onClose={() => setDetailTarget(undefined)} onSubmitted={refresh}/>} {historyTarget && <HistoryDialog {...historyTarget} onClose={() => setHistoryTarget(undefined)}/>} {alertTarget && <AlertLifecycleDialog alert={alertTarget} onClose={() => setAlertTarget(undefined)} onSubmitted={refresh}/>}</>;
}
