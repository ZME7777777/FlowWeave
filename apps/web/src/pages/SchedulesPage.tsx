import { CalendarClock, Check, ChevronDown, ChevronRight, CircleDot, Clock3, Pause, Play, Plus, Send, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { Pagination } from '../components/Pagination';
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

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : '—';

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

function ScheduleDetailDialog({ schedule, onClose }: { schedule: FlowRunSchedule; onClose: () => void }) {
  const openAutomaticRecord = useWorkbenchStore(state => state.openAutomaticRecord);
  const [page, setPage] = useState(1);
  const pageSize = 10;
  useEscapeClose(onClose);
  const { data, isLoading } = useQuery({ queryKey: ['flow-run-schedule-occurrences', schedule.id, page], queryFn: () => api.flowRunScheduleOccurrences(schedule.id, page, pageSize) });
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="modal schedule-detail-dialog" role="dialog" aria-modal="true" aria-label={`定时任务 ${schedule.name} 的执行记录`}><header><div><span className="eyebrow">SCHEDULE RUNS</span><h2>{schedule.name}</h2><p>按触发时间倒序。点击 NodeRun 会回到原 FlowRun 的连续运行记录。</p></div><button type="button" className="ghost product-dialog-close" aria-label="关闭" onClick={onClose}><X size={17}/></button></header><div className="schedule-occurrence-list">{isLoading ? <div className="empty compact">加载执行记录…</div> : data?.items.length ? data.items.map(occurrence => { const run = occurrence.flow_run; return <article className="schedule-occurrence-record" key={occurrence.id}><header><Clock3 size={15}/><span><b>{occurrence.trigger_kind === 'MANUAL' ? '手动触发' : '计划触发'} · {formatTime(occurrence.scheduled_for)}</b><small>{run ? `连续运行 #${run.run_no} · ${run.name}` : occurrence.state === 'FAILED' ? occurrence.error_detail || '启动失败' : '正在创建连续运行记录'}</small></span></header>{run?.node_runs.length ? <div className="schedule-occurrence-nodes">{run.node_runs.map(node => <button type="button" key={node.id} disabled={!run.parent_flow_run_id} onClick={() => run.parent_flow_run_id && openAutomaticRecord(run.parent_flow_run_id, run.id, node.id, node.attempts.at(-1)?.id)}><CircleDot size={13}/><span><b>{node.name || node.flow_node_snapshot_key}</b><small>{node.state} · {node.attempts.length} 次 Attempt</small></span><ChevronRight size={14}/></button>)}</div> : run ? <p>此连续运行记录正在准备运行环境，尚未生成 NodeRun。</p> : null}</article>; }) : <div className="schedule-no-runs">尚未产生执行记录。</div>}</div>{data && <Pagination page={page} pageSize={pageSize} total={data.total} onPageChange={setPage}/>}</section></div>;
}

export function SchedulesPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [expandedFlows, setExpandedFlows] = useState<Set<string>>(new Set());
  const [expandedMasters, setExpandedMasters] = useState<Set<string>>(new Set());
  const [detailSchedule, setDetailSchedule] = useState<FlowRunSchedule | null>(null);
  const [triggerBusyId, setTriggerBusyId] = useState<string>();
  const [triggerFeedback, setTriggerFeedback] = useState<{ scheduleId: string; tone: 'success' | 'error'; message: string }>();
  const { data: schedules = [] } = useQuery({ queryKey: ['flow-run-schedules'], queryFn: api.flowRunSchedules, refetchInterval: 3000 });
  const { data: flows = [] } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const { data: templates = [] } = useQuery({ queryKey: ['flow-run-schedule-templates'], queryFn: api.flowRunScheduleTemplates });
  const refresh = async () => { await qc.invalidateQueries({ queryKey: ['flow-run-schedules'] }); };
  const toggle = (setter: React.Dispatch<React.SetStateAction<Set<string>>>, id: string) => setter(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const setState = async (schedule: FlowRunSchedule) => { if (schedule.runtime_frozen) return; await api.setFlowRunScheduleState(schedule.id, schedule.row_version, schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE'); await refresh(); };
  const trigger = async (schedule: FlowRunSchedule) => {
    if (schedule.runtime_frozen) return;
    setTriggerBusyId(schedule.id);
    setTriggerFeedback(undefined);
    try {
      await api.triggerFlowRunSchedule(schedule.id);
      setTriggerFeedback({ scheduleId: schedule.id, tone: 'success', message: '已创建连续运行记录，后台正在启动。' });
      await refresh();
    } catch (reason) {
      setTriggerFeedback({ scheduleId: schedule.id, tone: 'error', message: reason instanceof Error ? reason.message : '立即运行失败，请稍后重试。' });
    } finally {
      setTriggerBusyId(undefined);
    }
  };
  const remove = async (schedule: FlowRunSchedule) => { if (await dialog.confirm({ title: `删除定时任务“${schedule.name}”？`, message: '仅无执行记录的任务可以直接删除；已有 FlowRun 请先按运行记录的删除规则处理。', confirmLabel: '删除任务', tone: 'danger' })) { await api.deleteFlowRunSchedule(schedule.id); await refresh(); } };
  const openCreate = () => {
    if (templates.length) { setCreating(true); return; }
    void dialog.confirm({ title: '暂时不能新建定时任务', message: '当前没有可用的连续运行母版。请先进入“流程运行”→“连续运行”，新建一条记录并补齐所有节点配置；显示“草稿已就绪”后即可新建定时任务。', confirmLabel: '我知道了', cancelLabel: '关闭' });
  };
  const flowIds = [...new Set([...flows.map(flow => flow.id), ...schedules.map(schedule => schedule.flow_definition_id)])];
  return <section className="page schedules-page"><div className="page-head"><div><span className="eyebrow">SCHEDULE DIRECTORY</span><h1>定时任务</h1><p>按流程、FlowRun 母版和定时任务名称分层查看；定时运行记录与手动启动记录明确区分。</p></div><button className="primary" onClick={openCreate}><Plus size={15}/>新建定时任务</button></div>{!schedules.length && <div className="schedule-empty"><CalendarClock size={28}/><b>暂无定时任务</b><span>从已就绪的连续运行记录复制配置母版后，平台会按 Cron 在原 FlowRun 内生成连续运行记录。</span></div>}<div className="schedule-flow-groups">{flowIds.map(flowId => { const flow = flows.find(item => item.id === flowId); const items = schedules.filter(item => item.flow_definition_id === flowId); const open = expandedFlows.has(flowId); const masterIds = [...new Set(items.map(item => item.source_flow_run_id || 'legacy'))]; return <section className="schedule-flow-group" key={flowId}><header><button className="schedule-tree-toggle" type="button" aria-expanded={open} onClick={() => toggle(setExpandedFlows, flowId)}>{open ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}<span><b>{flow?.name ?? '已删除的流程'}</b><small>{items.length} 个定时任务</small></span><em className="schedule-flow-meta">{items.filter(item => item.status === 'ACTIVE').length} 已启用</em></button></header>{open && <div className="schedule-directory">{masterIds.map(masterId => { const masterSchedules = items.filter(item => (item.source_flow_run_id || 'legacy') === masterId); const master = masterSchedules[0]?.source_flow_run; const masterOpen = expandedMasters.has(masterId); return <section className="schedule-master-group" key={masterId}><header><button className="schedule-tree-toggle" type="button" aria-expanded={masterOpen} onClick={() => toggle(setExpandedMasters, masterId)}>{masterOpen ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}<span><b>{master ? `FlowRun #${master.run_no} · ${master.name}` : '历史定时任务母版不可用'}</b><small>配置母版 · {master?.state ?? '已暂停'}</small></span></button></header>{masterOpen && <div className="schedule-directory">{masterSchedules.map(schedule => <section className="schedule-directory-item" key={schedule.id}><header><span className="schedule-name"><CalendarClock size={15}/><span><b>{schedule.name}</b><small>{schedule.runtime_frozen ? '已冻结 · 运行环境镜像不兼容，不能触发或恢复。' : `连续运行 · Cron ${schedule.cron_expression || '旧任务（已暂停）'} · 下次 ${formatTime(schedule.next_run_at)}`}</small></span></span><div className="schedule-actions"><button type="button" title="查看详情" onClick={() => setDetailSchedule(schedule)}>查看详情</button><button type="button" title={schedule.runtime_frozen ? '镜像不兼容，定时任务已冻结' : '立即运行'} disabled={schedule.runtime_frozen || triggerBusyId === schedule.id} onClick={() => void trigger(schedule)}><Send size={14}/>{triggerBusyId === schedule.id ? '启动中…' : schedule.runtime_frozen ? '已冻结' : '立即运行'}</button><button type="button" title={schedule.runtime_frozen ? '镜像不兼容，定时任务已冻结' : schedule.status === 'ACTIVE' ? '暂停' : '恢复'} disabled={schedule.runtime_frozen} onClick={() => void setState(schedule)}>{schedule.status === 'ACTIVE' ? <Pause size={14}/> : <Play size={14}/>} {schedule.runtime_frozen ? '已冻结' : schedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="danger" title="删除任务" onClick={() => void remove(schedule)}><X size={14}/></button></div></header>{schedule.runtime_frozen && <p className="schedule-trigger-feedback error" role="alert">{schedule.runtime_freeze_reason}</p>}{triggerFeedback?.scheduleId === schedule.id && <p className={`schedule-trigger-feedback ${triggerFeedback.tone}`} role={triggerFeedback.tone === 'error' ? 'alert' : 'status'}>{triggerFeedback.message}</p>}</section>)}</div>}</section>; })}</div>}</section>; })}</div>{creating && <ScheduleCreateDialog templates={templates} onClose={() => setCreating(false)} onCreated={refresh}/>} {detailSchedule && <ScheduleDetailDialog schedule={detailSchedule} onClose={() => setDetailSchedule(null)}/>}</section>;
}
