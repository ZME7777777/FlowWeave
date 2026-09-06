import { CalendarClock, Check, ChevronDown, ChevronRight, CircleDot, Clock3, Pause, Play, Plus, Send, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowRun, FlowRunSchedule, FlowRunScheduleTemplate } from '../types';

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

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';
const attemptLabel = (state: string) => ({ EXECUTING: '执行中', ACCEPTED: '已验收', WAITING_INPUT: '等待输入', WAITING_HUMAN: '等待人工', WAITING_START_CONFIRMATION: '等待启动', END_BLOCKED: '门禁拦截', START_BLOCKED: '门禁拦截', CANCELLED: '已取消' }[state] ?? state);

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
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><form className="modal schedule-create-dialog" onSubmit={submit}><header><div><span className="eyebrow">CREATE SCHEDULE</span><h2>新建定时任务</h2><p>从一条已满足启动条件的连续运行记录复制母版；以后每次触发都只按这份冻结配置执行。</p></div><button type="button" className="ghost product-dialog-close" aria-label="关闭" onClick={onClose}><X size={17}/></button></header><div className="schedule-form-grid"><label>任务名称<input value={name} maxLength={220} placeholder="例如：每小时数据检查" onChange={event => setName(event.target.value)}/></label><label>配置母版<ChoiceMenu label="选择配置母版" value={sourceFlowRunId} options={templateOptions} onChange={setSourceFlowRunId}/><small>仅显示已配置完整的连续运行记录；记录是否已启动不影响选择。</small></label><label className="schedule-form-wide">Cron 表达式<input value={cronExpression} spellCheck={false} placeholder="0 * * * *" onChange={event => setCronExpression(event.target.value)}/><small>标准五段 Cron：分 时 日 月 周（按 UTC 计算）。例如 <code>0 * * * *</code> 表示每小时整点。</small></label></div>{error && <p className="error">{error}</p>}<footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button><button className="primary" disabled={busy || !templates.length}>{busy ? '创建中…' : '创建定时任务'}</button></footer></form></div>;
}

function RunBranch({ run }: { run: FlowRun }) {
  const openRun = useWorkbenchStore(state => state.openRun);
  const [open, setOpen] = useState(false);
  return <article className="schedule-run-branch"><button className="schedule-tree-toggle" type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}<span><b>FlowRun #{run.run_no} · {run.name}</b><small>{formatTime(run.started_at)} · {run.run_mode === 'AUTOMATIC' ? '连续运行' : '逐步运行'}</small></span><em className={`schedule-state ${run.state.toLowerCase()}`}>{run.state === 'ACTIVE' ? '运行中' : run.state === 'WAITING_HUMAN' ? '等待人工' : run.state === 'COMPLETED' ? '已完成' : run.state}</em></button><button className="schedule-run-open" type="button" onClick={() => openRun(run.id)}>查看工作台</button>{open && <div className="schedule-node-branches">{run.node_runs.length ? run.node_runs.map(node => <section key={node.id}><header><CircleDot size={12}/><span><b>{node.name || node.flow_node_snapshot_key}</b><small>{node.state} · {node.attempts.length} 次 Attempt</small></span></header>{node.attempts.map(attempt => <button type="button" key={attempt.id} onClick={() => openRun(run.id, node.id)}><span>Attempt {attempt.attempt_no}</span><small>{attemptLabel(attempt.state)}</small></button>)}</section>) : <p>此 FlowRun 正在准备运行环境，尚未生成节点执行记录。</p>}</div>}</article>;
}

export function SchedulesPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [expandedFlows, setExpandedFlows] = useState<Set<string>>(new Set());
  const [expandedSchedules, setExpandedSchedules] = useState<Set<string>>(new Set());
  const { data: schedules = [] } = useQuery({ queryKey: ['flow-run-schedules'], queryFn: api.flowRunSchedules, refetchInterval: 3000 });
  const { data: flows = [] } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const { data: templates = [] } = useQuery({ queryKey: ['flow-run-schedule-templates'], queryFn: api.flowRunScheduleTemplates });
  const refresh = async () => { await qc.invalidateQueries({ queryKey: ['flow-run-schedules'] }); };
  const toggle = (setter: React.Dispatch<React.SetStateAction<Set<string>>>, id: string) => setter(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const setState = async (schedule: FlowRunSchedule) => { await api.setFlowRunScheduleState(schedule.id, schedule.row_version, schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE'); await refresh(); };
  const trigger = async (schedule: FlowRunSchedule) => { await api.triggerFlowRunSchedule(schedule.id); await refresh(); };
  const remove = async (schedule: FlowRunSchedule) => { if (await dialog.confirm({ title: `删除定时任务“${schedule.name}”？`, message: '仅无执行记录的任务可以直接删除；已有 FlowRun 请先按运行记录的删除规则处理。', confirmLabel: '删除任务', tone: 'danger' })) { await api.deleteFlowRunSchedule(schedule.id); await refresh(); } };
  const flowIds = [...new Set([...flows.map(flow => flow.id), ...schedules.map(schedule => schedule.flow_definition_id)])];
  return <section className="page schedules-page"><div className="page-head"><div><span className="eyebrow">SCHEDULE DIRECTORY</span><h1>定时任务</h1><p>按 Flow、定时任务和 FlowRun 分层查看；定时生成的运行记录在任务目录下，和手动启动的记录明确区分。</p></div><button className="primary" disabled={!templates.length} onClick={() => setCreating(true)}><Plus size={15}/>新建定时任务</button></div>{!schedules.length && <div className="schedule-empty"><CalendarClock size={28}/><b>暂无定时任务</b><span>从已就绪的连续运行记录复制配置母版后，平台会按 Cron 生成独立 FlowRun。</span></div>}<div className="schedule-flow-groups">{flowIds.map(flowId => { const flow = flows.find(item => item.id === flowId); const items = schedules.filter(item => item.flow_definition_id === flowId); const open = expandedFlows.has(flowId); return <section className="schedule-flow-group" key={flowId}><header><button className="schedule-tree-toggle" type="button" aria-expanded={open} onClick={() => toggle(setExpandedFlows, flowId)}>{open ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}<span><b>{flow?.name ?? '已删除的流程'}</b><small>{items.length} 个定时任务</small></span></button><span className="schedule-flow-meta">{items.filter(item => item.status === 'ACTIVE').length} 已启用</span></header>{open && <div className="schedule-directory">{items.map(schedule => { const scheduleOpen = expandedSchedules.has(schedule.id); return <section className="schedule-directory-item" key={schedule.id}><header><button className="schedule-tree-toggle" type="button" aria-expanded={scheduleOpen} onClick={() => toggle(setExpandedSchedules, schedule.id)}>{scheduleOpen ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}<CalendarClock size={15}/><span><b>{schedule.name}</b><small>连续运行 · Cron {schedule.cron_expression || '旧任务（已暂停）'} · 下次 {formatTime(schedule.next_run_at)}</small></span></button><div className="schedule-actions"><button type="button" title="立即运行" onClick={() => void trigger(schedule)}><Send size={14}/>立即运行</button><button type="button" title={schedule.status === 'ACTIVE' ? '暂停' : '恢复'} onClick={() => void setState(schedule)}>{schedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>} {schedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="danger" title="删除任务" onClick={() => void remove(schedule)}><X size={14}/></button></div></header>{scheduleOpen && <div className="schedule-occurrences">{schedule.occurrences.length ? schedule.occurrences.map(occurrence => occurrence.flow_run ? <RunBranch key={occurrence.id} run={occurrence.flow_run}/> : <article className="schedule-event" key={occurrence.id}><Clock3 size={14}/><span><b>{occurrence.trigger_kind === 'MANUAL' ? '手动触发' : '计划触发'} · {formatTime(occurrence.scheduled_for)}</b><small>{occurrence.state === 'PENDING' ? '正在创建 FlowRun' : occurrence.state === 'FAILED' ? occurrence.error_detail || '启动失败' : '未关联 FlowRun'}</small></span></article>) : <div className="schedule-no-runs">尚未产生 FlowRun。首次触发会在此目录下创建运行记录。</div>}</div>}</section>; })}</div>}</section>; })}</div>{creating && <ScheduleCreateDialog templates={templates} onClose={() => setCreating(false)} onCreated={refresh}/>}</section>;
}
