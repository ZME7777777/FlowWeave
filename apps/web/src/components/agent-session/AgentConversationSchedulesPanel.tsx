import { CalendarClock, ChevronRight, Clock3, Pause, Play, Plus, Send, Trash2, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../api/client';
import type { AgentConversation, AgentConversationSchedule } from '../../types';
import { useProductDialog } from '../ProductDialogContext';
import { useEscapeClose } from '../useEscapeClose';

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';

export function AgentConversationSchedulesPanel({ workspaceId, conversations, selectedBindingId, onClose, onOpenConversation }: {
  workspaceId: string;
  conversations: AgentConversation[];
  selectedBindingId?: string;
  onClose: () => void;
  onOpenConversation: (bindingId: string) => void;
}) {
  const queryClient = useQueryClient();
  const dialog = useProductDialog();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [bindingId, setBindingId] = useState(selectedBindingId ?? conversations[0]?.id ?? '');
  const [prompt, setPrompt] = useState('');
  const [cronExpression, setCronExpression] = useState('0 9 * * 1-5');
  const [selectedScheduleId, setSelectedScheduleId] = useState<string>();
  const [busyId, setBusyId] = useState<string>();
  const [error, setError] = useState('');
  const { data: schedules = [], isLoading } = useQuery({
    queryKey: ['agent-conversation-schedules', workspaceId],
    queryFn: () => api.agentConversationSchedules(workspaceId),
    refetchInterval: 5_000,
  });
  const selectedSchedule = schedules.find(item => item.id === selectedScheduleId) ?? schedules[0];
  const { data: occurrences = [] } = useQuery({
    queryKey: ['agent-conversation-schedule-occurrences', workspaceId, selectedSchedule?.id],
    queryFn: () => api.agentConversationScheduleOccurrences(workspaceId, selectedSchedule!.id),
    enabled: Boolean(selectedSchedule),
    refetchInterval: 5_000,
  });
  useEscapeClose(onClose);
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['agent-conversation-schedules', workspaceId] });
    await queryClient.invalidateQueries({ queryKey: ['agent-conversation-schedule-occurrences', workspaceId] });
  };
  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !bindingId || !prompt.trim() || !cronExpression.trim()) {
      setError('请填写任务名称、目标会话、提示词和 Cron 表达式。');
      return;
    }
    setBusyId('create'); setError('');
    try {
      const schedule = await api.createAgentConversationSchedule(workspaceId, {
        name: name.trim(), binding_id: bindingId, prompt: prompt.trim(), cron_expression: cronExpression.trim(),
      });
      setSelectedScheduleId(schedule.id); setCreating(false); setName(''); setPrompt(''); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建定时会话任务失败。'); }
    finally { setBusyId(undefined); }
  };
  const setState = async (schedule: AgentConversationSchedule) => {
    setBusyId(schedule.id); setError('');
    try { await api.setAgentConversationScheduleState(workspaceId, schedule.id, schedule.row_version, schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE'); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '更新任务状态失败。'); }
    finally { setBusyId(undefined); }
  };
  const trigger = async (schedule: AgentConversationSchedule) => {
    setBusyId(schedule.id); setError('');
    try { await api.triggerAgentConversationSchedule(workspaceId, schedule.id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '立即发送失败。'); }
    finally { setBusyId(undefined); }
  };
  const remove = async (schedule: AgentConversationSchedule) => {
    if (!await dialog.confirm({ title: `删除定时任务“${schedule.name}”？`, message: '该操作会删除此任务及其投递记录，不会删除目标会话中的任何消息。', confirmLabel: '删除任务', tone: 'danger' })) return;
    setBusyId(schedule.id); setError('');
    try { await api.deleteAgentConversationSchedule(workspaceId, schedule.id); if (selectedSchedule?.id === schedule.id) setSelectedScheduleId(undefined); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '删除任务失败。'); }
    finally { setBusyId(undefined); }
  };
  return <div className="agent-schedule-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="agent-schedule-panel" role="dialog" aria-modal="true" aria-label="会话定时任务"><header><div><span className="eyebrow">AGENT AUTOMATION</span><h2>会话定时任务</h2><p>在指定独立 Agent 会话中，按计划发送保存的提示词。</p></div><button type="button" aria-label="关闭定时任务" onClick={onClose}><X size={17}/></button></header><div className="agent-schedule-layout"><aside><div className="agent-schedule-list-head"><b>我的任务</b><button type="button" className={creating ? 'active' : ''} onClick={() => { setCreating(current => !current); setError(''); }}><Plus size={14}/>新建</button></div>{isLoading ? <p className="agent-schedule-empty">正在加载…</p> : schedules.length ? <div className="agent-schedule-list">{schedules.map(schedule => <button type="button" className={schedule.id === selectedSchedule?.id ? 'active' : ''} key={schedule.id} onClick={() => { setSelectedScheduleId(schedule.id); setCreating(false); }}><i className={schedule.status.toLowerCase()}/><span><b>{schedule.name}</b><small>{schedule.status === 'ACTIVE' ? `下次 ${formatTime(schedule.next_run_at)}` : '已暂停'}</small></span><ChevronRight size={13}/></button>)}</div> : <p className="agent-schedule-empty"><CalendarClock size={20}/>还没有定时会话任务。</p>}</aside><main>{creating ? <form className="agent-schedule-create" onSubmit={create}><div><span className="eyebrow">NEW SCHEDULE</span><h3>新建会话定时任务</h3><p>到点后会将提示词作为一条正式用户消息，发送到目标独立会话。</p></div><label>任务名称<input autoFocus value={name} maxLength={220} placeholder="例如：每日项目检查" onChange={event => setName(event.target.value)}/></label><label>目标会话<select value={bindingId} onChange={event => setBindingId(event.target.value)}><option value="">请选择会话</option>{conversations.map(item => <option key={item.id} value={item.id}>{item.display_title || '未命名会话'}</option>)}</select></label><label>定时提示词<textarea value={prompt} maxLength={200000} placeholder="例如：请检查昨日运行结果，并总结需要我关注的异常。" onChange={event => setPrompt(event.target.value)}/></label><label>Cron 表达式<input value={cronExpression} spellCheck={false} placeholder="0 9 * * 1-5" onChange={event => setCronExpression(event.target.value)}/><small>标准五段 Cron，按 UTC 执行：分 时 日 月 周。</small></label>{error && <p className="agent-schedule-error">{error}</p>}<footer><button type="button" className="secondary" onClick={() => setCreating(false)}>取消</button><button className="primary" disabled={busyId === 'create'}>{busyId === 'create' ? '创建中…' : '创建任务'}</button></footer></form> : selectedSchedule ? <section className="agent-schedule-detail"><header><div><span className="eyebrow">SCHEDULE DETAIL</span><h3>{selectedSchedule.name}</h3><p>目标会话：{selectedSchedule.conversation_title || '已删除会话'}</p></div><div className="agent-schedule-actions"><button type="button" className="secondary" disabled={busyId === selectedSchedule.id} onClick={() => void setState(selectedSchedule)}>{selectedSchedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>}{selectedSchedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="primary" disabled={busyId === selectedSchedule.id} onClick={() => void trigger(selectedSchedule)}><Send size={14}/>{busyId === selectedSchedule.id ? '处理中…' : '立即发送'}</button><button type="button" className="agent-schedule-delete" aria-label="删除任务" onClick={() => void remove(selectedSchedule)}><Trash2 size={15}/></button></div></header>{error && <p className="agent-schedule-error">{error}</p>}<div className="agent-schedule-summary"><div><span>重复</span><b>Cron · {selectedSchedule.cron_expression}</b></div><div><span>状态</span><b>{selectedSchedule.status === 'ACTIVE' ? '正在执行' : '已暂停'}</b></div><div><span>下次发送</span><b>{formatTime(selectedSchedule.next_run_at)}</b></div><div><span>上次发送</span><b>{formatTime(selectedSchedule.last_run_at)}</b></div></div><article className="agent-schedule-prompt"><span>保存的提示词</span><p>{selectedSchedule.prompt}</p></article><section className="agent-schedule-history"><header><div><span className="eyebrow">DELIVERY HISTORY</span><h4>投递记录</h4></div><button type="button" onClick={() => onOpenConversation(selectedSchedule.binding_id)}>打开目标会话<ChevronRight size={13}/></button></header>{occurrences.length ? <div>{occurrences.map(item => <article key={item.id}><Clock3 size={14}/><span><b>{item.trigger_kind === 'MANUAL' ? '手动发送' : '计划发送'} · {formatTime(item.scheduled_for || item.created_at)}</b><small>{item.state === 'STARTED' ? '提示词已投递至 Agent 会话' : item.state === 'FAILED' ? item.error_detail || '投递失败' : '正在等待投递'}</small></span><em className={item.state.toLowerCase()}>{item.state === 'STARTED' ? '已投递' : item.state === 'FAILED' ? '失败' : '等待中'}</em></article>)}</div> : <p className="agent-schedule-empty">尚未产生投递记录。</p>}</section></section> : <div className="agent-schedule-empty agent-schedule-detail-empty"><CalendarClock size={30}/><b>选择或新建一个任务</b><span>定时提示词会被投递到你选择的独立 Agent 会话。</span></div>}</main></div></section></div>;
}
