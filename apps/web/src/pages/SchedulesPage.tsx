import { CalendarClock, Check, ChevronDown, ChevronRight, CircleDot, Clock3, Pause, Play, Plus, Send, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import { useWorkbenchStore } from '../store/workbench';
import type { AgentPreset, FlowDefinition, FlowRun, FlowRunSchedule, TerminalEnvironment } from '../types';

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

const defaultPreset = (): AgentPreset => ({ capability_version_ids: [], model_provider_id: null, model_name: null, reasoning_effort: null, node_context_enabled: false, node_context_prompt: null });
const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';
const attemptLabel = (state: string) => ({ EXECUTING: '执行中', ACCEPTED: '已验收', WAITING_INPUT: '等待输入', WAITING_HUMAN: '等待人工', WAITING_START_CONFIRMATION: '等待启动', END_BLOCKED: '门禁拦截', START_BLOCKED: '门禁拦截', CANCELLED: '已取消' }[state] ?? state);

function ScheduleCreateDialog({ flows, environments, onClose, onCreated }: { flows: FlowDefinition[]; environments: TerminalEnvironment[]; onClose: () => void; onCreated: () => Promise<void> }) {
  const [name, setName] = useState('');
  const [flowId, setFlowId] = useState(flows[0]?.id ?? '');
  const [environmentVersionId, setEnvironmentVersionId] = useState('');
  const [runMode, setRunMode] = useState<'MANUAL' | 'AUTOMATIC'>('MANUAL');
  const [startNodeKey, setStartNodeKey] = useState('');
  const [interval, setInterval] = useState('60');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEscapeClose(onClose);
  const flow = flows.find(item => item.id === flowId);
  const nodes = flow?.nodes ?? [];
  const flowOptions = flows.map(item => ({ value: item.id, label: item.name }));
  const environmentOptions = environments.flatMap(environment => environment.versions.filter(version => version.state === 'READY' && version.runtime_compatible && Boolean(version.image_digest)).map(version => ({ value: version.id, label: `${environment.name} · v${version.version_no}` })));
  const nodeOptions = nodes.map(node => ({ value: node.instance_key, label: node.alias || node.instance_key }));
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const minutes = Number(interval);
    if (!name.trim() || !environmentVersionId || !startNodeKey || !Number.isInteger(minutes) || minutes < 1) { setError('请填写任务名称、环境、起始节点和有效周期。'); return; }
    setBusy(true); setError('');
    try {
      await api.createFlowRunSchedule({ name: name.trim(), flow_definition_id: flowId, environment_version_id: environmentVersionId, run_mode: runMode, start_node_key: startNodeKey, interval_minutes: minutes, startup_prompt: prompt.trim() || '按节点配置完成本次工作。', agent_preset: defaultPreset(), input_urls: {} });
      await onCreated(); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建定时任务失败。'); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><form className="modal schedule-create-dialog" onSubmit={submit}><header><div><span className="eyebrow">CREATE SCHEDULE</span><h2>新建定时任务</h2><p>任务独立管理；每次触发都会创建新的 FlowRun。</p></div><button type="button" className="ghost product-dialog-close" aria-label="关闭" onClick={onClose}><X size={17}/></button></header><div className="schedule-form-grid"><label>任务名称<input value={name} maxLength={220} placeholder="例如：每小时数据检查" onChange={event => setName(event.target.value)}/></label><label>流程<ChoiceMenu label="选择流程" value={flowId} options={flowOptions} onChange={value => { setFlowId(value); setStartNodeKey(''); }}/></label><label>运行环境<ChoiceMenu label="选择运行环境" value={environmentVersionId} options={environmentOptions} onChange={setEnvironmentVersionId}/></label><label>运行方式<ChoiceMenu label="选择运行方式" value={runMode} options={[{ value: 'MANUAL', label: '逐步运行' }, { value: 'AUTOMATIC', label: '连续运行' }]} onChange={value => setRunMode(value as 'MANUAL' | 'AUTOMATIC')}/></label><label>起始节点<ChoiceMenu label="选择起始节点" value={startNodeKey} options={nodeOptions} disabled={!flowId} onChange={setStartNodeKey}/></label><label>触发周期（分钟）<input inputMode="numeric" value={interval} onChange={event => setInterval(event.target.value.replace(/[^0-9]/g, ''))}/><small>首版支持每 N 分钟执行。</small></label><label className="schedule-form-wide">启动提示词<textarea value={prompt} placeholder="为空时使用平台默认启动提示词" onChange={event => setPrompt(event.target.value)}/></label></div>{error && <p className="error">{error}</p>}<footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button><button className="primary" disabled={busy || !flows.length}>{busy ? '创建中…' : '创建定时任务'}</button></footer></form></div>;
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
  const { data: environments = [] } = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments });
  const refresh = async () => { await qc.invalidateQueries({ queryKey: ['flow-run-schedules'] }); };
  const toggle = (setter: React.Dispatch<React.SetStateAction<Set<string>>>, id: string) => setter(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const setState = async (schedule: FlowRunSchedule) => { await api.setFlowRunScheduleState(schedule.id, schedule.row_version, schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE'); await refresh(); };
  const trigger = async (schedule: FlowRunSchedule) => { await api.triggerFlowRunSchedule(schedule.id); await refresh(); };
  const remove = async (schedule: FlowRunSchedule) => { if (await dialog.confirm({ title: `删除定时任务“${schedule.name}”？`, message: '仅无执行记录的任务可以直接删除；已有 FlowRun 请先按运行记录的删除规则处理。', confirmLabel: '删除任务', tone: 'danger' })) { await api.deleteFlowRunSchedule(schedule.id); await refresh(); } };
  const flowIds = [...new Set([...flows.map(flow => flow.id), ...schedules.map(schedule => schedule.flow_definition_id)])];
  return <section className="page schedules-page"><div className="page-head"><div><span className="eyebrow">SCHEDULE DIRECTORY</span><h1>定时任务</h1><p>按 Flow、定时任务和 FlowRun 分层查看；Attempt 仅归属于具体运行记录。</p></div><button className="primary" disabled={!flows.length} onClick={() => setCreating(true)}><Plus size={15}/>新建定时任务</button></div>{!schedules.length && <div className="schedule-empty"><CalendarClock size={28}/><b>暂无定时任务</b><span>创建任务后，平台会按周期生成独立的 FlowRun。</span></div>}<div className="schedule-flow-groups">{flowIds.map(flowId => { const flow = flows.find(item => item.id === flowId); const items = schedules.filter(item => item.flow_definition_id === flowId); const open = expandedFlows.has(flowId); return <section className="schedule-flow-group" key={flowId}><header><button className="schedule-tree-toggle" type="button" aria-expanded={open} onClick={() => toggle(setExpandedFlows, flowId)}>{open ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}<span><b>{flow?.name ?? '已删除的流程'}</b><small>{items.length} 个定时任务</small></span></button><span className="schedule-flow-meta">{items.filter(item => item.status === 'ACTIVE').length} 已启用</span></header>{open && <div className="schedule-directory">{items.map(schedule => { const scheduleOpen = expandedSchedules.has(schedule.id); return <section className="schedule-directory-item" key={schedule.id}><header><button className="schedule-tree-toggle" type="button" aria-expanded={scheduleOpen} onClick={() => toggle(setExpandedSchedules, schedule.id)}>{scheduleOpen ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}<CalendarClock size={15}/><span><b>{schedule.name}</b><small>{schedule.run_mode === 'AUTOMATIC' ? '连续运行' : '逐步运行'} · 每 {schedule.interval_minutes} 分钟 · 下次 {formatTime(schedule.next_run_at)}</small></span></button><div className="schedule-actions"><button type="button" title="立即运行" onClick={() => void trigger(schedule)}><Send size={14}/>立即运行</button><button type="button" title={schedule.status === 'ACTIVE' ? '暂停' : '恢复'} onClick={() => void setState(schedule)}>{schedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>} {schedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="danger" title="删除任务" onClick={() => void remove(schedule)}><X size={14}/></button></div></header>{scheduleOpen && <div className="schedule-occurrences">{schedule.occurrences.length ? schedule.occurrences.map(occurrence => occurrence.flow_run ? <RunBranch key={occurrence.id} run={occurrence.flow_run}/> : <article className="schedule-event" key={occurrence.id}><Clock3 size={14}/><span><b>{occurrence.trigger_kind === 'MANUAL' ? '手动触发' : '计划触发'} · {formatTime(occurrence.scheduled_for)}</b><small>{occurrence.state === 'PENDING' ? '正在创建 FlowRun' : occurrence.state === 'FAILED' ? occurrence.error_detail || '启动失败' : '未关联 FlowRun'}</small></span></article>) : <div className="schedule-no-runs">尚未产生 FlowRun。首次触发会在此目录下创建运行记录。</div>}</div>}</section>; })}</div>}</section>; })}</div>{creating && <ScheduleCreateDialog flows={flows} environments={environments} onClose={() => setCreating(false)} onCreated={refresh}/>}</section>;
}
