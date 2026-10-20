import { CalendarClock, ChevronDown, Clock3, Ellipsis, Filter, Pause, Play, Plus, Send } from 'lucide-react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../../api/client';
import type { AgentConversationSchedule, AgentConversationScheduleOccurrence, ModelProvider } from '../../types';

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';
type ScheduleFilter = 'ALL' | 'ACTIVE' | 'PAUSED' | 'COMPLETED';
type FormMode = 'create' | 'edit' | undefined;
type WorkspaceMode = 'AUTOMATION' | 'EXISTING';
type ConversationMode = 'NEW' | 'CONTINUE';

function occurrenceLabel(item: AgentConversationScheduleOccurrence) {
  return item.conversation_title || `${item.trigger_kind === 'MANUAL' ? '手动运行' : '定时运行'} · ${formatTime(item.scheduled_for || item.created_at)}`;
}

function defaultModel(providers: ModelProvider[]) {
  const provider = providers.find(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default))
    ?? providers.find(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled));
  const model = provider?.models.find(item => item.enabled && item.is_default) ?? provider?.models.find(item => item.enabled);
  return { providerId: provider?.id ?? '', modelName: model?.model_name ?? '', effort: model?.default_reasoning_effort ?? '' };
}

export function AgentConversationSchedulesPanel({ workspaceId, onOpenConversation }: {
  workspaceId: string;
  onOpenConversation: (bindingId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [formMode, setFormMode] = useState<FormMode>();
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [cronExpression, setCronExpression] = useState('0 9 * * 1-5');
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>('AUTOMATION');
  const [workDirectoryId, setWorkDirectoryId] = useState('');
  const [conversationMode, setConversationMode] = useState<ConversationMode>('NEW');
  const [modelProviderId, setModelProviderId] = useState('');
  const [modelName, setModelName] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState('');
  const [selectedScheduleId, setSelectedScheduleId] = useState<string>();
  const [actionMenuId, setActionMenuId] = useState<string>();
  const [filterOpen, setFilterOpen] = useState(false);
  const [filter, setFilter] = useState<ScheduleFilter>('ALL');
  const [busyId, setBusyId] = useState<string>();
  const [error, setError] = useState('');
  const { data: schedules = [], isLoading } = useQuery({
    queryKey: ['agent-conversation-schedules', workspaceId],
    queryFn: () => api.agentConversationSchedules(workspaceId),
    refetchInterval: 5_000,
  });
  const directoriesQuery = useQuery({ queryKey: ['agent-work-directories', workspaceId], queryFn: () => api.agentWorkDirectories(workspaceId) });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const providers = useMemo(() => (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled)), [providersQuery.data]);
  const models = providers.find(item => item.id === modelProviderId)?.models.filter(item => item.enabled) ?? [];
  const occurrenceQueries = useQueries({
    queries: schedules.map(schedule => ({
      queryKey: ['agent-conversation-schedule-occurrences', workspaceId, schedule.id],
      queryFn: () => api.agentConversationScheduleOccurrences(workspaceId, schedule.id),
      refetchInterval: 5_000,
    })),
  });
  const occurrencesBySchedule = new Map(schedules.map((schedule, index) => [schedule.id, occurrenceQueries[index]?.data ?? []]));
  const selectedSchedule = schedules.find(item => item.id === selectedScheduleId) ?? schedules[0];
  const visibleSchedules = schedules.filter(schedule => {
    if (filter === 'ACTIVE') return schedule.status === 'ACTIVE';
    if (filter === 'PAUSED') return schedule.status === 'PAUSED';
    return filter !== 'COMPLETED' || Boolean(schedule.last_run_at);
  });
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['agent-conversation-schedules', workspaceId] });
    await queryClient.invalidateQueries({ queryKey: ['agent-conversation-schedule-occurrences', workspaceId] });
  };
  const resetModel = () => {
    const next = defaultModel(providers);
    setModelProviderId(next.providerId);
    setModelName(next.modelName);
    setReasoningEffort(next.effort);
  };
  const openCreate = () => {
    setFormMode('create');
    setName('');
    setPrompt('');
    setCronExpression('0 9 * * 1-5');
    setWorkspaceMode('AUTOMATION');
    setWorkDirectoryId('');
    setConversationMode('NEW');
    resetModel();
    setError('');
  };
  const openEdit = (schedule: AgentConversationSchedule) => {
    setFormMode('edit');
    setName(schedule.name);
    setPrompt(schedule.prompt);
    setCronExpression(schedule.cron_expression);
    setWorkspaceMode(schedule.workspace_mode);
    setWorkDirectoryId(schedule.work_directory_id ?? '');
    setConversationMode(schedule.conversation_mode);
    setModelProviderId(schedule.model_provider_id ?? '');
    setModelName(schedule.model_name ?? '');
    setReasoningEffort(schedule.reasoning_effort ?? '');
    setActionMenuId(undefined);
    setError('');
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !prompt.trim() || !cronExpression.trim() || !modelProviderId || !modelName || (formMode === 'create' && workspaceMode === 'EXISTING' && !workDirectoryId)) {
      setError('请填写任务名称、业务提示词、Cron、工作区和模型配置。');
      return;
    }
    if (formMode === 'edit' && !selectedSchedule) return;
    setBusyId(formMode === 'edit' ? selectedSchedule!.id : 'create');
    setError('');
    try {
      const base = { name: name.trim(), prompt: prompt.trim(), cron_expression: cronExpression.trim(), conversation_mode: conversationMode, model_provider_id: modelProviderId, model_name: modelName, reasoning_effort: reasoningEffort || null };
      const schedule = formMode === 'edit'
        ? await api.updateAgentConversationSchedule(workspaceId, selectedSchedule!.id, { ...base, expected_row_version: selectedSchedule!.row_version })
        : await api.createAgentConversationSchedule(workspaceId, { ...base, workspace_mode: workspaceMode, work_directory_id: workspaceMode === 'EXISTING' ? workDirectoryId : undefined });
      setSelectedScheduleId(schedule.id);
      setFormMode(undefined);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存定时任务失败。');
    } finally {
      setBusyId(undefined);
    }
  };
  const setState = async (schedule: AgentConversationSchedule) => {
    setBusyId(schedule.id); setActionMenuId(undefined); setError('');
    try {
      await api.setAgentConversationScheduleState(workspaceId, schedule.id, schedule.row_version, schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE');
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '更新任务状态失败。'); }
    finally { setBusyId(undefined); }
  };
  const trigger = async (schedule: AgentConversationSchedule) => {
    setBusyId(schedule.id); setActionMenuId(undefined); setError('');
    try {
      await api.triggerAgentConversationSchedule(workspaceId, schedule.id);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '立即运行失败。'); }
    finally { setBusyId(undefined); }
  };
  const filterLabel: Record<ScheduleFilter, string> = { ALL: '全部状态', ACTIVE: '已开启', PAUSED: '已暂停', COMPLETED: '已完成' };
  return <section className="agent-schedule-page" role="region" aria-label="会话定时任务">
    <header className="agent-schedule-page-head"><h2>定时任务</h2></header>
    <div className="agent-schedule-layout">
      <aside className="agent-schedule-rail"><div className="agent-schedule-list-head"><div><b>定时任务</b><small>{visibleSchedules.length} 个任务</small></div><div className="agent-schedule-rail-actions"><div className="agent-schedule-filter"><button type="button" aria-label="筛选定时任务" aria-expanded={filterOpen} onClick={() => setFilterOpen(value => !value)}><Filter size={14}/></button>{filterOpen && <div role="menu"><span>筛选依据</span>{(['ALL', 'ACTIVE', 'PAUSED', 'COMPLETED'] as const).map(value => <button type="button" role="menuitemradio" aria-checked={filter === value} key={value} onClick={() => { setFilter(value); setFilterOpen(false); }}>{filterLabel[value]}{filter === value && <b>✓</b>}</button>)}</div>}</div><button type="button" className="agent-schedule-new" onClick={openCreate}><Plus size={14}/>新建</button></div></div>
        {isLoading ? <p className="agent-schedule-empty">正在加载…</p> : visibleSchedules.length ? <div className="agent-schedule-tree">{visibleSchedules.map(schedule => { const occurrences = occurrencesBySchedule.get(schedule.id) ?? []; const selected = schedule.id === selectedSchedule?.id; return <section className={`agent-schedule-tree-group${selected ? ' selected' : ''}`} key={schedule.id}><div className="agent-schedule-tree-task"><button type="button" className="agent-schedule-tree-select" onClick={() => { setSelectedScheduleId(schedule.id); setFormMode(undefined); }}><i className={schedule.status.toLowerCase()}/><span><b>{schedule.name}</b><small>{schedule.status === 'ACTIVE' ? `下次 ${formatTime(schedule.next_run_at)}` : '已暂停'}</small></span><ChevronDown size={13}/></button><div className="agent-schedule-menu"><button type="button" aria-label={`${schedule.name} 操作`} aria-expanded={actionMenuId === schedule.id} onClick={() => setActionMenuId(current => current === schedule.id ? undefined : schedule.id)}><Ellipsis size={16}/></button>{actionMenuId === schedule.id && <div role="menu"><button type="button" role="menuitem" disabled={busyId === schedule.id} onClick={() => void setState(schedule)}>{schedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>}{schedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" role="menuitem" disabled={busyId === schedule.id} onClick={() => void trigger(schedule)}><Send size={14}/>立即运行</button><button type="button" role="menuitem" onClick={() => openEdit(schedule)}><CalendarClock size={14}/>编辑</button></div>}</div></div><div className="agent-schedule-run-list">{occurrences.length ? occurrences.map(item => <button type="button" key={item.id} className={`agent-schedule-run ${item.state.toLowerCase()}`} disabled={item.state !== 'STARTED'} onClick={() => onOpenConversation(item.binding_id)}><Clock3 size={13}/><span><b>{occurrenceLabel(item)}</b>{item.state !== 'STARTED' && <small>{item.error_detail || (item.state === 'FAILED' ? '运行失败' : '正在创建会话')}</small>}</span><em>{item.state === 'STARTED' ? '打开' : item.state === 'FAILED' ? '失败' : '等待中'}</em></button>) : <p>尚无运行记录</p>}</div></section>; })}</div> : <p className="agent-schedule-empty"><CalendarClock size={20}/>没有符合“{filterLabel[filter]}”的任务。</p>}</aside>
      <main>{formMode ? <form className="agent-schedule-create" onSubmit={save}><div><span className="eyebrow">{formMode === 'edit' ? 'EDIT AUTOMATION' : 'NEW AUTOMATION'}</span><h3>{formMode === 'edit' ? '编辑定时任务' : '新建定时任务'}</h3><p>只填写业务提示词；平台会自动创建配置、记忆文件与运行上下文。</p></div><label>任务名称<input autoFocus value={name} maxLength={220} placeholder="例如：每日项目检查" onChange={event => setName(event.target.value)}/></label>{formMode === 'create' && <label>工作区模式<select value={workspaceMode} onChange={event => setWorkspaceMode(event.target.value as WorkspaceMode)}><option value="AUTOMATION">自动创建专属工作区</option><option value="EXISTING">绑定已有工作区</option></select></label>}{formMode === 'create' && workspaceMode === 'EXISTING' && <label>已有工作区<select value={workDirectoryId} onChange={event => setWorkDirectoryId(event.target.value)}><option value="">请选择工作区</option>{directoriesQuery.data?.items.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>}<label>运行方式<select value={conversationMode} onChange={event => setConversationMode(event.target.value as ConversationMode)}><option value="NEW">每次创建新会话</option><option value="CONTINUE">在同一会话中继续</option></select></label><label>模型供应商<select value={modelProviderId} onChange={event => { const provider = providers.find(item => item.id === event.target.value); const model = provider?.models.find(item => item.enabled && item.is_default) ?? provider?.models.find(item => item.enabled); setModelProviderId(event.target.value); setModelName(model?.model_name ?? ''); setReasoningEffort(model?.default_reasoning_effort ?? ''); }}><option value="">请选择供应商</option>{providers.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>模型名称<select value={modelName} onChange={event => { const model = models.find(item => item.model_name === event.target.value); setModelName(event.target.value); setReasoningEffort(model?.default_reasoning_effort ?? ''); }}><option value="">请选择模型</option>{models.map(item => <option key={item.model_name} value={item.model_name}>{item.model_name}</option>)}</select></label><label>思考程度<select value={reasoningEffort} onChange={event => setReasoningEffort(event.target.value)}><option value="">默认</option>{models.find(item => item.model_name === modelName)?.supported_reasoning_efforts?.map(item => <option key={item} value={item}>{item}</option>)}</select></label><label>业务提示词<textarea value={prompt} maxLength={200000} placeholder="例如：检查昨日运行结果，并总结需要我关注的异常。" onChange={event => setPrompt(event.target.value)}/><small>运行时会自动加入任务名称、任务 ID、memory.md、上次运行和工作区约束。</small></label><label>Cron 表达式<input value={cronExpression} spellCheck={false} placeholder="0 9 * * 1-5" onChange={event => setCronExpression(event.target.value)}/><small>标准五段 Cron，按 UTC 执行：分 时 日 月 周。</small></label>{error && <p className="agent-schedule-error">{error}</p>}<footer><button type="button" className="secondary" onClick={() => setFormMode(undefined)}>取消</button><button className="primary" disabled={Boolean(busyId)}>{busyId ? '保存中…' : '保存任务'}</button></footer></form> : selectedSchedule ? <section className="agent-schedule-detail"><header><div><span className="eyebrow">AUTOMATION DETAIL</span><h3>{selectedSchedule.name}</h3><p>{selectedSchedule.work_directory_name || '工作区不可用'} · {selectedSchedule.conversation_mode === 'CONTINUE' ? '持续会话' : '每次新会话'} · {filterLabel[selectedSchedule.status]}</p></div><div className="agent-schedule-actions"><button type="button" className="secondary" disabled={busyId === selectedSchedule.id} onClick={() => void setState(selectedSchedule)}>{selectedSchedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>}{selectedSchedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="primary" disabled={busyId === selectedSchedule.id} onClick={() => void trigger(selectedSchedule)}><Send size={14}/>{busyId === selectedSchedule.id ? '处理中…' : '立即运行'}</button><button type="button" className="secondary" disabled={busyId === selectedSchedule.id} onClick={() => openEdit(selectedSchedule)}><CalendarClock size={14}/>编辑</button></div></header>{error && <p className="agent-schedule-error">{error}</p>}<div className="agent-schedule-summary"><div><span>重复</span><b>Cron · {selectedSchedule.cron_expression}</b></div><div><span>工作区</span><b>{selectedSchedule.workspace_mode === 'AUTOMATION' ? '专属工作区' : '已有工作区'}</b></div><div><span>下次运行</span><b>{formatTime(selectedSchedule.next_run_at)}</b></div><div><span>上次运行</span><b>{formatTime(selectedSchedule.last_run_at)}</b></div></div><article className="agent-schedule-prompt"><span>业务提示词</span><p>{selectedSchedule.prompt}</p></article><section className="agent-schedule-history"><header><div><span className="eyebrow">RUN CONVERSATIONS</span><h4>运行会话</h4></div></header>{(occurrencesBySchedule.get(selectedSchedule.id) ?? []).length ? <div>{(occurrencesBySchedule.get(selectedSchedule.id) ?? []).map(item => <button className="agent-schedule-history-run" key={item.id} disabled={item.state !== 'STARTED'} onClick={() => onOpenConversation(item.binding_id)}><Clock3 size={14}/><span><b>{occurrenceLabel(item)}</b>{item.state !== 'STARTED' && <small>{item.error_detail || '正在创建会话'}</small>}</span><em className={item.state.toLowerCase()}>{item.state === 'STARTED' ? '打开会话' : item.state === 'FAILED' ? '失败' : '等待中'}</em></button>)}</div> : <p className="agent-schedule-empty">尚未产生运行会话。</p>}</section></section> : <div className="agent-schedule-empty agent-schedule-detail-empty"><CalendarClock size={30}/><b>选择或新建一个任务</b><span>每个任务都有独立的自动化配置、记忆和运行记录。</span></div>}</main>
    </div>
  </section>;
}
