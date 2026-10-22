import { CalendarClock, Check, ChevronDown, ChevronRight, CircleDot, MoreHorizontal, Pause, Play, Plus, Send, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowRunSchedule, FlowRunScheduleTemplate } from '../types';

type Option = { value: string; label: string };

function ChoiceMenu({ label, value, options, disabled, onChange }: { label: string; value: string; options: Option[]; disabled?: boolean; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  useEscapeClose(() => setOpen(false), open);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (event.target instanceof Node && !host.current?.contains(event.target)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  const current = options.find(item => item.value === value)?.label ?? '请选择';
  return <div ref={host} className="schedule-choice"><button type="button" aria-label={label} aria-haspopup="listbox" aria-expanded={open} disabled={disabled} onClick={() => setOpen(value => !value)}><span>{current}</span><ChevronDown size={14}/></button>{open && <div className="schedule-choice-options" role="listbox" aria-label={label}>{options.map(item => <button type="button" role="option" aria-selected={item.value === value} key={item.value} onClick={() => { onChange(item.value); setOpen(false); }}><span>{item.label}</span>{item.value === value && <Check size={13}/>}</button>)}</div>}</div>;
}

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';

function ScheduleCreateDialog({ templates, onClose, onCreated }: { templates: FlowRunScheduleTemplate[]; onClose: () => void; onCreated: () => Promise<void> }) {
  const [name, setName] = useState('');
  const [sourceFlowRunId, setSourceFlowRunId] = useState(templates[0]?.id ?? '');
  const [cronExpression, setCronExpression] = useState('0 * * * *');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEscapeClose(onClose);
  const templateOptions = templates.map(item => ({ value: item.id, label: `${item.flow_name || '流程'} · ${item.name}（#${item.run_no}）` }));
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !sourceFlowRunId || !cronExpression.trim()) { setError('请填写任务名称、配置母版和 Cron 表达式。'); return; }
    setBusy(true); setError('');
    try {
      await api.createFlowRunSchedule({ name: name.trim(), source_flow_run_id: sourceFlowRunId, cron_expression: cronExpression.trim() });
      await onCreated(); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建定时任务失败。'); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><form className="modal schedule-create-dialog" onSubmit={submit}><header><div><span className="eyebrow">CREATE SCHEDULE</span><h2>新建定时任务</h2><p>从一条已满足启动条件的连续运行记录复制母版；以后每次触发都只按这份冻结配置执行。</p></div><button type="button" className="ghost product-dialog-close" aria-label="关闭" onClick={onClose}><X size={17}/></button></header><div className="schedule-form-grid"><label>任务名称<input value={name} maxLength={220} placeholder="例如：每小时数据检查" onChange={event => setName(event.target.value)}/></label><label>配置母版<ChoiceMenu label="选择配置母版" value={sourceFlowRunId} options={templateOptions} onChange={setSourceFlowRunId}/><small>仅显示已配置完整的连续运行记录；记录是否已启动不影响选择。</small></label><label className="schedule-form-wide">Cron 表达式<input value={cronExpression} spellCheck={false} placeholder="0 * * * *" onChange={event => setCronExpression(event.target.value)}/><small>标准五段 Cron：分 时 日 月 周（按北京时间计算）。例如 <code>0 * * * *</code> 表示每小时整点。</small></label></div>{error && <p className="error">{error}</p>}<footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button><button className="primary" disabled={busy || !templates.length}>{busy ? '创建中…' : '创建定时任务'}</button></footer></form></div>;
}

function ScheduleRecordList({ schedule }: { schedule: FlowRunSchedule }) {
  const openAutomaticRecord = useWorkbenchStore(state => state.openAutomaticRecord);
  const { data, isLoading } = useQuery({
    queryKey: ['flow-run-schedule-occurrences', schedule.id],
    queryFn: () => api.flowRunScheduleOccurrences(schedule.id, 1, 12),
    refetchInterval: 3000,
  });
  if (isLoading) return <div className="schedule-record-empty">正在加载该任务的执行记录…</div>;
  const occurrences = data?.items ?? [];
  if (!occurrences.length) return <div className="schedule-record-empty">尚未产生执行记录。任务首次触发后，记录会出现在这里。</div>;
  return <div className="schedule-record-list">{occurrences.map(occurrence => {
    const run = occurrence.flow_run;
    return <article className="schedule-record" key={occurrence.id}><div className="schedule-record-timeline"><i/><span/></div><div className="schedule-record-body"><header><div><span className="schedule-record-kicker">{occurrence.trigger_kind === 'MANUAL' ? '手动执行' : '计划执行'}</span><b>{formatTime(occurrence.scheduled_for)}</b></div><em className={`schedule-record-state ${occurrence.state.toLowerCase()}`}>{occurrence.state === 'STARTED' ? '已启动' : occurrence.state === 'FAILED' ? '失败' : '准备中'}</em></header><p>{run ? `连续运行 #${run.run_no} · ${run.name}` : occurrence.error_detail || '正在创建连续运行记录'}</p>{run?.node_runs.length ? <div className="schedule-record-nodes">{run.node_runs.map(node => <button type="button" key={node.id} disabled={!run.parent_flow_run_id} onClick={() => run.parent_flow_run_id && openAutomaticRecord(run.parent_flow_run_id, run.id, node.id, node.attempts.at(-1)?.id)}><CircleDot size={14}/><span><b>{node.name || node.flow_node_snapshot_key}</b><small>{node.state} · {node.attempts.length} 次 Attempt</small></span><ChevronRight size={14}/></button>)}</div> : null}</div></article>;
  })}</div>;
}

export function SchedulesPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [selectedScheduleId, setSelectedScheduleId] = useState<string>();
  const [triggerBusyId, setTriggerBusyId] = useState<string>();
  const [triggerFeedback, setTriggerFeedback] = useState<{ scheduleId: string; tone: 'success' | 'error'; message: string }>();
  const { data: schedules = [] } = useQuery({ queryKey: ['flow-run-schedules'], queryFn: api.flowRunSchedules, refetchInterval: 3000 });
  const { data: templates = [] } = useQuery({ queryKey: ['flow-run-schedule-templates'], queryFn: api.flowRunScheduleTemplates });
  const refresh = async () => { await qc.invalidateQueries({ queryKey: ['flow-run-schedules'] }); };
  useEffect(() => {
    if (!selectedScheduleId && schedules[0]) setSelectedScheduleId(schedules[0].id);
  }, [schedules, selectedScheduleId]);
  const selectedSchedule = schedules.find(item => item.id === selectedScheduleId) ?? schedules[0];
  const setState = async (schedule: FlowRunSchedule) => { await api.setFlowRunScheduleState(schedule.id, schedule.row_version, schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE'); await refresh(); };
  const trigger = async (schedule: FlowRunSchedule) => {
    setTriggerBusyId(schedule.id); setTriggerFeedback(undefined);
    try { await api.triggerFlowRunSchedule(schedule.id); setTriggerFeedback({ scheduleId: schedule.id, tone: 'success', message: '已创建连续运行记录，后台正在启动。' }); await refresh(); }
    catch (reason) { setTriggerFeedback({ scheduleId: schedule.id, tone: 'error', message: reason instanceof Error ? reason.message : '立即运行失败，请稍后重试。' }); }
    finally { setTriggerBusyId(undefined); }
  };
  const remove = async (schedule: FlowRunSchedule) => {
    if (await dialog.confirm({ title: `删除定时任务“${schedule.name}”？`, message: '仅无执行记录的任务可以直接删除；已有 FlowRun 请先按运行记录的删除规则处理。', confirmLabel: '删除任务', tone: 'danger' })) {
      await api.deleteFlowRunSchedule(schedule.id); if (selectedScheduleId === schedule.id) setSelectedScheduleId(undefined); await refresh();
    }
  };
  const openCreate = () => {
    if (templates.length) { setCreating(true); return; }
    void dialog.confirm({ title: '暂时不能新建定时任务', message: '当前没有可用的连续运行母版。请先进入“流程运行”→“连续运行”，新建一条记录并补齐所有节点配置；显示“草稿已就绪”后即可新建定时任务。', confirmLabel: '我知道了', cancelLabel: '关闭' });
  };
  return <section className="schedule-workspace"><aside className="schedule-list-panel"><header><div><h1>定时任务</h1><span>{schedules.filter(item => item.status === 'ACTIVE').length} 个正在执行</span></div><button type="button" className="schedule-create-button" onClick={openCreate}><Plus size={17}/>新建任务</button></header><div className="schedule-list-label"><span>即将执行</span><MoreHorizontal size={16}/></div><div className="schedule-task-list">{schedules.length ? schedules.map(schedule => <button type="button" className={schedule.id === selectedSchedule?.id ? 'active' : ''} key={schedule.id} onClick={() => setSelectedScheduleId(schedule.id)}><span className={`schedule-status-dot ${schedule.status.toLowerCase()}`}/><span><b>{schedule.name}</b><small>{schedule.status === 'ACTIVE' ? `下次：${formatTime(schedule.next_run_at)}` : '已暂停'}</small></span><ChevronRight size={15}/></button>) : <div className="schedule-list-empty"><CalendarClock size={22}/><b>暂无定时任务</b><span>从已就绪的连续运行记录创建第一项任务。</span></div>}</div></aside><main className="schedule-conversation">{selectedSchedule ? <><header className="schedule-conversation-head"><div><span className="eyebrow">AUTOMATION</span><h2>{selectedSchedule.name}</h2><p>连续运行母版：{selectedSchedule.source_flow_run ? `#${selectedSchedule.source_flow_run.run_no} · ${selectedSchedule.source_flow_run.name}` : '历史任务母版不可用'}</p></div><div className="schedule-head-actions"><button type="button" className="secondary" onClick={() => void setState(selectedSchedule)}>{selectedSchedule.status === 'ACTIVE' ? <Pause size={15}/> : <Play size={15}/>} {selectedSchedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="primary" disabled={triggerBusyId === selectedSchedule.id} onClick={() => void trigger(selectedSchedule)}><Send size={15}/>{triggerBusyId === selectedSchedule.id ? '启动中…' : '立即运行'}</button><button type="button" className="schedule-delete-button" aria-label="删除定时任务" title="删除定时任务" onClick={() => void remove(selectedSchedule)}><X size={17}/></button></div></header><div className="schedule-conversation-content">{triggerFeedback?.scheduleId === selectedSchedule.id && <p className={`schedule-trigger-feedback ${triggerFeedback.tone}`} role={triggerFeedback.tone === 'error' ? 'alert' : 'status'}>{triggerFeedback.message}</p>}<section className="schedule-summary-card"><div><span>重复</span><b>Cron · {selectedSchedule.cron_expression || '旧任务（已暂停）'}</b></div><div><span>状态</span><b>{selectedSchedule.status === 'ACTIVE' ? '正在执行' : '已暂停'}</b></div><div><span>下次执行</span><b>{formatTime(selectedSchedule.next_run_at)}</b></div><div><span>上次执行</span><b>{formatTime(selectedSchedule.last_run_at)}</b></div></section><section className="schedule-history"><header><div><span className="eyebrow">RUN HISTORY</span><h3>任务会话与执行记录</h3><p>按触发时间倒序展示；点击节点可打开对应的 Agent 会话。</p></div></header><ScheduleRecordList schedule={selectedSchedule}/></section></div></> : <div className="schedule-empty-state"><CalendarClock size={36}/><h2>创建你的第一个定时任务</h2><p>任务会在设定的 Cron 时间自动从冻结的连续运行母版启动，所有运行记录将集中保留在此页面。</p><button type="button" className="primary" onClick={openCreate}><Plus size={15}/>新建定时任务</button></div>}</main>{creating && <ScheduleCreateDialog templates={templates} onClose={() => setCreating(false)} onCreated={refresh}/>}</section>;
}
