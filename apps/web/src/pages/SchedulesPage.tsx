import { CalendarClock, Check, ChevronDown, ChevronLeft, ChevronRight, CircleDot, Info, Pause, Play, Plus, Send, Trash2, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowRunSchedule, FlowRunScheduleOccurrence, FlowRunScheduleTemplate } from '../types';

type Option = { value: string; label: string };

function ChoiceMenu({ label, value, options, disabled, onChange, placeholder = '请选择' }: { label: string; value: string; options: Option[]; disabled?: boolean; onChange: (value: string) => void; placeholder?: string }) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const host = useRef<HTMLDivElement>(null);
  useEscapeClose(() => setOpen(false), open);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (event.target instanceof Node && !host.current?.contains(event.target)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  const current = options.find(item => item.value === value)?.label ?? placeholder;
  const normalizedFilter = filter.trim().toLocaleLowerCase();
  const filteredOptions = normalizedFilter ? options.filter(item => item.label.toLocaleLowerCase().includes(normalizedFilter)) : options;
  return <div ref={host} className="schedule-choice"><button type="button" aria-label={label} aria-haspopup="listbox" aria-expanded={open} disabled={disabled} onClick={() => { setFilter(''); setOpen(current => !current); }}><span>{current}</span><ChevronDown size={14}/></button>{open && <div className="schedule-choice-options" role="listbox" aria-label={label}><input autoFocus aria-label={`筛选${label}`} value={filter} placeholder="输入筛选" onChange={event => setFilter(event.target.value)}/>{filteredOptions.length ? filteredOptions.map(item => <button type="button" role="option" aria-selected={item.value === value} key={item.value} onClick={() => { onChange(item.value); setFilter(''); setOpen(false); }}><span>{item.label}</span>{item.value === value && <Check size={13}/>}</button>) : <p>没有匹配的记录</p>}</div>}</div>;
}

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';

function ScheduleCreateDialog({ templates, onClose, onCreated }: { templates: FlowRunScheduleTemplate[]; onClose: () => void; onCreated: () => Promise<void> }) {
  const [name, setName] = useState('');
  const [parentFlowRunId, setParentFlowRunId] = useState('');
  const [sourceFlowRunId, setSourceFlowRunId] = useState('');
  const [cronExpression, setCronExpression] = useState('0 * * * *');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEscapeClose(onClose);
  const parentOptions = Array.from(new Map(templates.map(item => [item.parent_flow_run_id, { value: item.parent_flow_run_id, label: `${item.flow_name || '流程'} · ${item.parent_flow_run_name || 'FlowRun'}（#${item.parent_flow_run_no ?? '—'}）` }])).values());
  const templateOptions = templates.filter(item => item.parent_flow_run_id === parentFlowRunId).map(item => ({ value: item.id, label: `${item.name}（连续运行 #${item.run_no}）` }));
  const selectParentFlowRun = (id: string) => { setParentFlowRunId(id); setSourceFlowRunId(''); };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !sourceFlowRunId || !cronExpression.trim()) { setError('请填写任务名称、FlowRun、连续运行记录和 Cron 表达式。'); return; }
    setBusy(true); setError('');
    try {
      await api.createFlowRunSchedule({ name: name.trim(), source_flow_run_id: sourceFlowRunId, cron_expression: cronExpression.trim() });
      await onCreated(); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建定时任务失败。'); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><form className="modal schedule-create-dialog" onSubmit={submit}><header><div><span className="eyebrow">CREATE SCHEDULE</span><h2>新建定时任务</h2><p>先选 FlowRun，再选其中已满足启动条件的连续运行记录；每个列表均可输入筛选。</p></div><button type="button" className="ghost product-dialog-close" aria-label="关闭" onClick={onClose}><X size={17}/></button></header><div className="schedule-form-grid"><label>任务名称<input value={name} maxLength={220} placeholder="例如：每小时数据检查" onChange={event => setName(event.target.value)}/></label><label>FlowRun<ChoiceMenu label="选择 FlowRun" value={parentFlowRunId} options={parentOptions} onChange={selectParentFlowRun} placeholder="先选择 FlowRun"/><small>仅显示包含可用连续运行记录的 FlowRun。</small></label><label className="schedule-form-wide">连续运行记录<ChoiceMenu label="选择连续运行记录" value={sourceFlowRunId} options={templateOptions} disabled={!parentFlowRunId} onChange={setSourceFlowRunId} placeholder="先选择 FlowRun"/><small>仅显示所选 FlowRun 下配置完整的连续运行记录；记录是否已启动不影响选择。</small></label><label className="schedule-form-wide">Cron 表达式<input value={cronExpression} spellCheck={false} placeholder="0 * * * *" onChange={event => setCronExpression(event.target.value)}/><small>标准五段 Cron：分 时 日 月 周（按北京时间计算）。例如 <code>0 * * * *</code> 表示每小时整点。</small></label></div>{error && <p className="error">{error}</p>}<footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button><button className="primary" disabled={busy || !templates.length || !sourceFlowRunId}>{busy ? '创建中…' : '创建定时任务'}</button></footer></form></div>;
}

const SCHEDULE_RECORD_PAGE_SIZE = 10;

function ScheduleRecordNodesDialog({ occurrence, onClose }: { occurrence: FlowRunScheduleOccurrence; onClose: () => void }) {
  const openAutomaticRecord = useWorkbenchStore(state => state.openAutomaticRecord);
  const run = occurrence.flow_run;
  const nodes = run?.node_runs ?? [];
  useEscapeClose(onClose);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="modal schedule-record-nodes-dialog" role="dialog" aria-modal="true" aria-label="节点运行状态"><header><div><span className="eyebrow">NODE RUNS</span><h2>节点运行状态</h2><p>{formatTime(occurrence.scheduled_for)} · {occurrence.trigger_kind === 'MANUAL' ? '手动执行' : '计划执行'} · {run ? `连续运行 #${run.run_no}` : '执行记录'}</p></div><button type="button" className="ghost" aria-label="关闭节点运行状态" onClick={onClose}><X size={17}/></button></header><div className="schedule-record-node-list">{nodes.length ? nodes.map(node => <button type="button" key={node.id} disabled={!run?.parent_flow_run_id} onClick={() => run?.parent_flow_run_id && openAutomaticRecord(run.parent_flow_run_id, run.id, node.id, node.attempts.at(-1)?.id)}><CircleDot size={16}/><span><b>{node.name || node.flow_node_snapshot_key}</b><small>{node.flow_node_snapshot_key}</small></span><em>{node.state} · {node.attempts.length} 次 Attempt</em><ChevronRight size={16}/></button>) : <p>该次执行尚未生成节点运行状态。</p>}</div><footer><button type="button" className="primary" onClick={onClose}>完成</button></footer></section></div>;
}

function ScheduleRecordList({ schedule }: { schedule: FlowRunSchedule }) {
  const [page, setPage] = useState(1);
  const [selectedOccurrence, setSelectedOccurrence] = useState<FlowRunScheduleOccurrence>();
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = 0; }, [page]);
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['flow-run-schedule-occurrences', schedule.id, page],
    queryFn: () => api.flowRunScheduleOccurrences(schedule.id, page, SCHEDULE_RECORD_PAGE_SIZE),
    refetchInterval: 3000,
  });
  const occurrences = data?.items ?? [];
  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / SCHEDULE_RECORD_PAGE_SIZE));
  return <section className="schedule-record-list" aria-label="执行记录表格">
    <header className="schedule-record-toolbar"><h3>执行记录</h3><span>{data ? `共 ${total} 条` : '按触发时间倒序'}</span></header>
    <div className="schedule-record-table" role="list" aria-label="执行记录" ref={scrollRef}>
      {isLoading && !data ? <div className="schedule-record-empty" role="status">正在加载该任务的执行记录…</div>
        : isError && !data ? <div className="schedule-record-empty" role="alert">读取执行记录失败<button type="button" className="secondary" onClick={() => void refetch()}>重试</button></div>
          : !occurrences.length ? <div className="schedule-record-empty">尚未产生执行记录。任务首次触发后，记录会出现在这里。</div>
            : occurrences.map(occurrence => {
              const run = occurrence.flow_run;
              const state = occurrence.state === 'STARTED' ? '已启动' : occurrence.state === 'FAILED' ? '失败' : '准备中';
              const timestamp = occurrence.scheduled_for ?? run?.started_at;
              const name = run?.name || occurrence.error_detail || '正在创建连续运行记录';
              return <article className="schedule-record" key={occurrence.id} role="listitem">
                <button type="button" className="schedule-record-summary" aria-label={`查看执行记录 ${name}`} aria-haspopup="dialog" onClick={() => setSelectedOccurrence(occurrence)}>
                  <span className={`schedule-record-marker ${occurrence.state.toLowerCase()}`}/>
                  <span className="schedule-record-run" title={name}><b>{name}</b>{run && <small>#{run.run_no}</small>}</span>
                  <span className="schedule-record-kind">{occurrence.trigger_kind === 'MANUAL' ? '手动执行' : '计划执行'}</span>
                  <time className="schedule-record-time" dateTime={timestamp ?? undefined} title={`${occurrence.scheduled_for ? '计划触发时间' : '运行创建时间'}：${formatTime(timestamp)}`}>{timestamp ? new Date(timestamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—'}</time>
                  <em className={`schedule-record-state ${occurrence.state.toLowerCase()}`}>{state}</em>
                  <span className="schedule-record-open">查看节点<ChevronRight size={13}/></span>
                </button>
              </article>;
            })}
    </div>
    {pageCount > 1 && <nav className="schedule-record-pagination" aria-label="执行记录分页"><button type="button" aria-label="上一页" disabled={page === 1} onClick={() => setPage(current => current - 1)}><ChevronLeft size={15}/>上一页</button><span>第 {page} / {pageCount} 页 · 共 {total} 条</span><button type="button" aria-label="下一页" disabled={page === pageCount} onClick={() => setPage(current => current + 1)}>下一页<ChevronRight size={15}/></button></nav>}
    {selectedOccurrence && <ScheduleRecordNodesDialog occurrence={selectedOccurrence} onClose={() => setSelectedOccurrence(undefined)}/>}
  </section>;
}

function ScheduleInfo({ schedule }: { schedule: FlowRunSchedule }) {
  const host = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (host.current?.open && event.target instanceof Node && !host.current.contains(event.target)) host.current.open = false;
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented && host.current?.open) {
        host.current.open = false;
        event.preventDefault();
      }
    };
    document.addEventListener('pointerdown', close, true);
    document.addEventListener('keydown', escape, true);
    return () => { document.removeEventListener('pointerdown', close, true); document.removeEventListener('keydown', escape, true); };
  }, []);
  return <details ref={host} className="run-record-header-info schedule-header-info">
    <summary aria-label="任务信息" title="查看任务信息"><Info size={14}/></summary>
    <dl>
      <div><dt>重复</dt><dd>{schedule.cron_expression ? `Cron · ${schedule.cron_expression}` : '旧任务（已暂停）'}</dd></div>
      <div><dt>时区</dt><dd>北京时间</dd></div>
      <div><dt>下次执行</dt><dd>{formatTime(schedule.next_run_at)}</dd></div>
      <div><dt>上次执行</dt><dd>{formatTime(schedule.last_run_at)}</dd></div>
      <div><dt>母版</dt><dd>{schedule.source_flow_run?.name || '历史任务'}</dd></div>
    </dl>
  </details>;
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
    if (await dialog.confirm({ title: `删除定时任务“${schedule.name}”？`, message: '将删除该定时任务及其全部连续运行记录、节点 Attempt、运行会话和关联运行资源；此操作不可恢复。', confirmLabel: '删除任务', tone: 'danger' })) {
      await api.deleteFlowRunSchedule(schedule.id); if (selectedScheduleId === schedule.id) setSelectedScheduleId(undefined); await refresh();
    }
  };
  const openCreate = () => {
    if (templates.length) { setCreating(true); return; }
    void dialog.confirm({ title: '暂时不能新建定时任务', message: '当前没有可用的连续运行母版。请先进入“流程运行”→“连续运行”，新建一条记录并补齐所有节点配置；显示“草稿已就绪”后即可新建定时任务。', confirmLabel: '我知道了', cancelLabel: '关闭' });
  };
  return <section className="schedule-workspace"><aside className="schedule-list-panel"><header><div><h1>定时任务</h1><span>{schedules.filter(item => item.status === 'ACTIVE').length} 个正在执行</span></div><button type="button" className="schedule-create-button" onClick={openCreate}><Plus size={17}/>新建任务</button></header><div className="schedule-task-list">{schedules.length ? schedules.map(schedule => <button type="button" className={schedule.id === selectedSchedule?.id ? 'active' : ''} key={schedule.id} onClick={() => setSelectedScheduleId(schedule.id)}><span className={`schedule-status-dot ${schedule.status.toLowerCase()}`}/><span><b>{schedule.name}</b><small>{schedule.status === 'ACTIVE' ? `下次：${formatTime(schedule.next_run_at)}` : '已暂停'}</small></span><ChevronRight size={15}/></button>) : <div className="schedule-list-empty"><CalendarClock size={22}/><b>暂无定时任务</b><span>从已就绪的连续运行记录创建第一项任务。</span></div>}</div></aside><main className="schedule-conversation">{selectedSchedule ? <><header className="schedule-conversation-head"><h2 title={selectedSchedule.name}>{selectedSchedule.name}</h2><span className={`schedule-header-status ${selectedSchedule.status.toLowerCase()}`}>{selectedSchedule.status === 'ACTIVE' ? '正在执行' : '已暂停'}</span><span className="schedule-header-cron" title={`Cron · ${selectedSchedule.cron_expression || '旧任务（已暂停）'}`}>{selectedSchedule.cron_expression || '旧任务'}</span><ScheduleInfo key={selectedSchedule.id} schedule={selectedSchedule}/><div className="schedule-head-actions"><button type="button" className="secondary" onClick={() => void setState(selectedSchedule)}>{selectedSchedule.status === 'ACTIVE' ? <Pause size={15}/> : <Play size={15}/>} {selectedSchedule.status === 'ACTIVE' ? '暂停' : '恢复'}</button><button type="button" className="primary" disabled={triggerBusyId === selectedSchedule.id} onClick={() => void trigger(selectedSchedule)}><Send size={15}/>{triggerBusyId === selectedSchedule.id ? '启动中…' : '立即运行'}</button><button type="button" className="schedule-delete-button" aria-label="删除定时任务" title="删除定时任务" onClick={() => void remove(selectedSchedule)}><Trash2 size={17}/></button></div></header><div className="schedule-conversation-content">{triggerFeedback?.scheduleId === selectedSchedule.id && <p className={`schedule-trigger-feedback ${triggerFeedback.tone}`} role={triggerFeedback.tone === 'error' ? 'alert' : 'status'}>{triggerFeedback.message}</p>}<section className="schedule-history"><ScheduleRecordList key={selectedSchedule.id} schedule={selectedSchedule}/></section></div></> : <div className="schedule-empty-state"><CalendarClock size={36}/><h2>创建你的第一个定时任务</h2><p>任务会在设定的 Cron 时间自动从冻结的连续运行母版启动，所有运行记录将集中保留在此页面。</p><button type="button" className="primary" onClick={openCreate}><Plus size={15}/>新建定时任务</button></div>}</main>{creating && <ScheduleCreateDialog templates={templates} onClose={() => setCreating(false)} onCreated={refresh}/>}</section>;
}
