import { CalendarClock, ChevronDown, Clock3, Ellipsis, Filter, Pause, Play, Plus, Send } from 'lucide-react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../api/client';
import type { AgentConversation, AgentConversationSchedule, AgentConversationScheduleOccurrence } from '../../types';

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';
type ScheduleFilter = 'ALL' | 'ACTIVE' | 'PAUSED' | 'COMPLETED';
type FormMode = 'create' | 'edit' | undefined;

function occurrenceLabel(item: AgentConversationScheduleOccurrence) {
  return item.conversation_title || `${item.trigger_kind === 'MANUAL' ? '手动运行' : '定时运行'} · ${formatTime(item.scheduled_for || item.created_at)}`;
}

export function AgentConversationSchedulesPanel({ workspaceId, conversations, selectedBindingId, onOpenConversation }: {
  workspaceId: string;
  conversations: AgentConversation[];
  selectedBindingId?: string;
  onOpenConversation: (bindingId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [formMode, setFormMode] = useState<FormMode>();
  const [name, setName] = useState('');
  const [bindingId, setBindingId] = useState(selectedBindingId ?? conversations[0]?.id ?? '');
  const [prompt, setPrompt] = useState('');
  const [cronExpression, setCronExpression] = useState('0 9 * * 1-5');
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
  const openCreate = () => {
    setFormMode('create');
    setName('');
    setBindingId(selectedBindingId ?? conversations[0]?.id ?? '');
    setPrompt('');
    setCronExpression('0 9 * * 1-5');
    setError('');
  };
  const openEdit = (schedule: AgentConversationSchedule) => {
    setFormMode('edit');
    setName(schedule.name);
    setBindingId(schedule.binding_id);
    setPrompt(schedule.prompt);
    setCronExpression(schedule.cron_expression);
    setActionMenuId(undefined);
    setError('');
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !bindingId || !prompt.trim() || !cronExpression.trim()) {
      setError('请填写任务名称、来源会话、提示词和 Cron 表达式。');
      return;
    }
    if (formMode === 'edit' && !selectedSchedule) return;
    setBusyId(formMode === 'edit' ? selectedSchedule!.id : 'create');
    setError('');
    try {
      const body = { name: name.trim(), binding_id: bindingId, prompt: prompt.trim(), cron_expression: cronExpression.trim() };
      const schedule = formMode === 'edit'
        ? await api.updateAgentConversationSchedule(workspaceId, selectedSchedule!.id, { ...body, expected_row_version: selectedSchedule!.row_version })
        : await api.createAgentConversationSchedule(workspaceId, body);
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
      <main>{formMode ? <form className="agent-schedule-create" onSubmit={save}><div><span className="eyebrow">{formMode === 'edit' ? 'EDIT SCHEDULE' : 'NEW SCHEDULE'}</span><h3>{formMode === 'edit' ? '编辑定时任务' : '新建会话定时任务'}</h3><p>每次运行都会从来源会话的模型、能力和工作目录创建独立会话，再投递保存的提示词。</p></div><label>任务名称<input autoFocus value={name} maxLength={220} placeholder="例如：每日项目检查" onChange={event => setName(event.target.value)}/></label><label>来源会话<select value={bindingId} onChange={event => setBindingId(event.target.value)}><option value="">请选择会话</option>{conversations.map(item => <option key={item.id} value={item.id}>{item.display_title || '未命名会话'}</option>)}</select></label><label>定时提示词<textarea value={prompt} maxLength={200000} placeholder="例如：请检查昨日运行结果，并总结需要我关注的异常。" onChange={event => setPrompt(event.target.value)}/></label><label>Cron 表达式<input value={cronExpression} spellCheck={false} placeholder="0 9 * * 1-5" onChange={event => setCronExpression(event.target.value)}/><small>标准五段 Cron，按 UTC 执行：分 时 日 月 周。</small></label>{error && <p className="agent-schedule-error">{error}</p>}<footer><button type="button" className="secondary" onClick={() => setFormMode(undefined)}>取消</button><button className="primary" disabled={Boolean(busyId)}>{busyId ? '保存中…' : '保存任务'}</button></footer></form> : selectedSchedule ? <section className="agent-schedule-detail"><header><div><span className="eyebrow">SCHEDULE DETAIL</span><h3>{selectedSchedule.name}</h3><p>来源会话：{selectedSchedule.conversation_title || '已删除会话'} · {filterLabel[selectedSchedule.status]}</p></div><div className="agent-schedule-actions"><button type="button" className="secondary" disabled={busyId === selectedSchedule.id} onClick={() => void setState(selectedSchedule)}>{selectedSchedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>}{selectedSchedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="primary" disabled={busyId === selectedSchedule.id} onClick={() => void trigger(selectedSchedule)}><Send size={14}/>{busyId === selectedSchedule.id ? '处理中…' : '立即运行'}</button><button type="button" className="secondary" disabled={busyId === selectedSchedule.id} onClick={() => openEdit(selectedSchedule)}><CalendarClock size={14}/>编辑</button></div></header>{error && <p className="agent-schedule-error">{error}</p>}<div className="agent-schedule-summary"><div><span>重复</span><b>Cron · {selectedSchedule.cron_expression}</b></div><div><span>状态</span><b>{selectedSchedule.status === 'ACTIVE' ? '已开启' : '已暂停'}</b></div><div><span>下次运行</span><b>{formatTime(selectedSchedule.next_run_at)}</b></div><div><span>上次运行</span><b>{formatTime(selectedSchedule.last_run_at)}</b></div></div><article className="agent-schedule-prompt"><span>保存的提示词</span><p>{selectedSchedule.prompt}</p></article><section className="agent-schedule-history"><header><div><span className="eyebrow">RUN CONVERSATIONS</span><h4>运行会话</h4></div></header>{(occurrencesBySchedule.get(selectedSchedule.id) ?? []).length ? <div>{(occurrencesBySchedule.get(selectedSchedule.id) ?? []).map(item => <button className="agent-schedule-history-run" key={item.id} disabled={item.state !== 'STARTED'} onClick={() => onOpenConversation(item.binding_id)}><Clock3 size={14}/><span><b>{occurrenceLabel(item)}</b>{item.state !== 'STARTED' && <small>{item.error_detail || '正在创建会话'}</small>}</span><em className={item.state.toLowerCase()}>{item.state === 'STARTED' ? '打开会话' : item.state === 'FAILED' ? '失败' : '等待中'}</em></button>)}</div> : <p className="agent-schedule-empty">尚未产生运行会话。</p>}</section></section> : <div className="agent-schedule-empty agent-schedule-detail-empty"><CalendarClock size={30}/><b>选择或新建一个任务</b><span>每次定时运行会创建独立的 Agent 会话。</span></div>}</main>
    </div>
  </section>;
}
