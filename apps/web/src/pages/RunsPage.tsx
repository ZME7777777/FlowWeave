import { Box, ChevronDown, ChevronRight, CircleDot, FolderClosed, Plus, Search, Terminal } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { CreateRunRecordDialog } from '../components/CreateRunRecordDialog';
import { FlowRunTerminalDialog } from '../components/AgentRuntimeSidebar';
import { StartRunDialog } from '../components/StartRunDialog';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowDefinition, FlowRunSummary } from '../types';

const STATUS_LABELS: Record<string, string> = { ACTIVE: '运行中', WAITING_HUMAN: '等待人工', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消', DRAFT: '草稿' };
type RecordFilter = 'ALL' | 'MANUAL' | 'AUTOMATIC';

const isStartableFlow = (flow: FlowDefinition) => {
  if (!flow.nodes.length) return false;
  const targets = new Set(flow.edges.map(edge => edge.target_instance_key));
  return flow.nodes.some(node => !targets.has(node.instance_key));
};

const runtimeLabel = (run: FlowRunSummary) => {
  if (run.runtime_write_available) return '容器运行中';
  if (run.runtime_status === 'STOPPED') return '容器已暂停';
  if (run.runtime_status === 'DEGRADED') return '容器暂不可用';
  return '容器准备中';
};

export function RunsPage() {
  const qc = useQueryClient();
  const openAutomaticRecord = useWorkbenchStore(state => state.openAutomaticRecord);
  const openStepwiseRecord = useWorkbenchStore(state => state.openStepwiseRecord);
  const [creatingRecordRunId, setCreatingRecordRunId] = useState<string>();
  const [terminalRun, setTerminalRun] = useState<{ id: string; name: string }>();
  const [starting, setStarting] = useState<FlowDefinition>();
  const [selectedFlowId, setSelectedFlowId] = useState<string>();
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [expandedFlowIds, setExpandedFlowIds] = useState<Set<string>>(new Set());
  const [collapsedScheduleIds, setCollapsedScheduleIds] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const [recordFilter, setRecordFilter] = useState<RecordFilter>('ALL');
  const { data: runs = [] } = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: 1500 });
  const { data: flows = [] } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const { data: environments = [] } = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments });
  const environmentLabel = (run: FlowRunSummary) => {
    for (const environment of environments) {
      const version = environment.versions.find(item => item.id === run.environment_version_id);
      if (version) return `${environment.name} · v${version.version_no}`;
    }
    return '独立运行环境';
  };
  const selectedRun = runs.find(run => run.id === selectedRunId);
  const selectedFlow = flows.find(flow => flow.id === selectedFlowId);
  const stepwise = useQuery({ queryKey: ['flow-run-stepwise-records', selectedRunId], queryFn: () => api.stepwiseRecords(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const automatic = useQuery({ queryKey: ['flow-run-automatic-records', selectedRunId], queryFn: () => api.automaticRecordSummaries(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const runDirectories = useMemo(() => {
    const term = search.trim().toLowerCase();
    const flowById = new Map(flows.map(flow => [flow.id, flow]));
    const orderedIds = [...new Set([...flows.map(flow => flow.id), ...runs.map(run => run.flow_definition_id)])];
    return orderedIds.flatMap(flowId => {
      const flow = flowById.get(flowId);
      const flowRuns = runs.filter(run => run.flow_definition_id === flowId)
        .filter(run => !term || `${flow?.name ?? run.flow_name ?? ''} ${flow?.description ?? ''} ${run.name} ${run.run_no} ${run.state}`.toLowerCase().includes(term));
      const flowMatches = !term || `${flow?.name ?? ''} ${flow?.description ?? ''}`.toLowerCase().includes(term);
      if (!flowRuns.length && !flowMatches) return [];
      return [{ id: flowId, name: flow?.name ?? runs.find(run => run.flow_definition_id === flowId)?.flow_name ?? '历史流程快照', description: flow?.description ?? '', runs: flowRuns }];
    });
  }, [flows, runs, search]);
  const selectFlow = (flowId: string) => {
    setSelectedFlowId(flowId);
    setSelectedRunId(undefined);
    setExpandedFlowIds(current => current.has(flowId) ? current : new Set([...current, flowId]));
  };
  const selectRun = (flowId: string, runId: string) => {
    setSelectedFlowId(flowId);
    setSelectedRunId(runId);
    setExpandedFlowIds(current => current.has(flowId) ? current : new Set([...current, flowId]));
  };
  const records = useMemo(() => {
    const manual = (stepwise.data ?? []).map(record => ({ kind: 'MANUAL' as const, id: record.id, name: record.name, state: record.state, startedAt: record.started_at, detail: record.start_node_key ? '逐步配置与确认执行' : '等待配置起始节点', scheduleId: undefined, scheduleName: undefined, record }));
    const continuous = (automatic.data ?? []).map(record => ({ kind: 'AUTOMATIC' as const, id: record.id, name: record.name, state: record.state, startedAt: record.started_at, detail: `${record.progress.accepted}/${record.plan.reachable_node_count} 节点完成`, scheduleId: record.schedule_id ?? undefined, scheduleName: record.schedule_name ?? undefined, record }));
    return [...manual, ...continuous].filter(item => recordFilter === 'ALL' || item.kind === recordFilter).sort((left, right) => right.startedAt.localeCompare(left.startedAt));
  }, [automatic.data, recordFilter, stepwise.data]);
  const scheduledContinuousGroups = useMemo(() => {
    if (recordFilter !== 'AUTOMATIC') return [];
    const grouped = new Map<string, typeof records>();
    records.filter(item => item.kind === 'AUTOMATIC' && item.scheduleId).forEach(item => {
      const group = grouped.get(item.scheduleId!) ?? [];
      group.push(item);
      grouped.set(item.scheduleId!, group);
    });
    return [...grouped.entries()].map(([id, group]) => ({ id, name: group[0].scheduleName ?? '定时任务', records: group }));
  }, [recordFilter, records]);
  const standaloneRecords = recordFilter === 'AUTOMATIC'
    ? records.filter(item => item.kind !== 'AUTOMATIC' || !item.scheduleId)
    : records;
  const recordItem = (item: typeof records[number]) => <button className="flow-run-record-row" key={`${item.kind}:${item.id}`} onClick={() => item.kind === 'MANUAL' ? openStepwiseRecord(selectedRunId!, item.id) : openAutomaticRecord(selectedRunId!, item.id)}><i/><span><small>{item.kind === 'MANUAL' ? '逐步运行' : '连续运行'} · {STATUS_LABELS[item.state] ?? item.state}</small><b>{item.name}</b><em>{item.detail}</em></span><time>{new Date(item.startedAt).toLocaleString()}</time><ChevronRight size={15}/></button>;
  return <section className="page runs-page run-directory-page">
    <div className="run-directory-layout">
      <aside className="flow-run-directory"><header><div><b>流程编排</b><small>{runDirectories.length} 个流程</small></div><button className="agent-schedule-new" disabled={!selectedFlow || !isStartableFlow(selectedFlow)} title={!selectedFlow ? '请先选择一个流程编排' : isStartableFlow(selectedFlow) ? undefined : '该流程未配置启动节点'} onClick={() => selectedFlow && setStarting(selectedFlow)}><Plus size={14}/>新建</button></header><label className="run-directory-search"><Search size={14}/><input aria-label="搜索流程或运行" value={search} placeholder="搜索流程或运行" onChange={event => setSearch(event.target.value)}/></label><div className="flow-run-directory-list">{runDirectories.map(flow => { const expanded = expandedFlowIds.has(flow.id); const selected = selectedFlowId === flow.id; return <section className={`flow-run-tree-group${selected ? ' selected' : ''}`} key={flow.id}><div className="flow-run-tree-flow"><button type="button" className="flow-run-tree-select" aria-current={selected ? 'true' : undefined} aria-expanded={expanded} onClick={() => selectFlow(flow.id)}><FolderClosed size={14}/><span><b>{flow.name}</b><small>{flow.runs.length} 个 FlowRun</small></span></button></div>{expanded && <div className="flow-run-tree-runs">{flow.runs.length ? flow.runs.map(run => <button type="button" className={`flow-run-tree-run${selectedRunId === run.id ? ' active' : ''}`} key={run.id} onClick={() => selectRun(flow.id, run.id)}><i/><span><b>Run #{run.run_no} · {run.name}</b><small>快照 v{run.active_snapshot_version ?? '—'} · {environmentLabel(run)}</small><em className={`run-runtime-badge ${run.runtime_write_available ? 'available' : run.runtime_status === 'STOPPED' ? 'paused' : 'pending'}`}>{runtimeLabel(run)}</em></span></button>) : <p>该流程尚未创建 FlowRun。</p>}</div>}</section>; })}{!runDirectories.length && <p>没有匹配的流程或 FlowRun。</p>}</div></aside>
      <main className="flow-run-directory-detail">{selectedRun ? <section className="flow-run-record-directory"><header><div><span className="eyebrow">RUN RECORDS</span><h3>{selectedRun.name}</h3><p>Run #{selectedRun.run_no} · {selectedRun.flow_name ?? selectedFlow?.name} · 快照 v{selectedRun.active_snapshot_version ?? '—'}</p></div><div className="run-directory-actions"><button className="secondary" disabled={!selectedRun.runtime_write_available} onClick={() => setTerminalRun({ id: selectedRun.id, name: selectedRun.name })}><Terminal size={14}/>进入终端</button><button className="primary" onClick={() => setCreatingRecordRunId(selectedRun.id)}><Plus size={14}/>新增运行记录</button></div></header><div className="flow-run-directory-summary"><span><b>运行环境</b>{runtimeLabel(selectedRun)}</span><span><b>环境版本</b>{environmentLabel(selectedRun)}</span><span><b>创建时间</b>{new Date(selectedRun.started_at).toLocaleString()}</span><span><b>流程版本</b>v{selectedRun.flow_row_version ?? '—'}</span></div><nav className="run-record-filter"><button className={recordFilter === 'ALL' ? 'active' : ''} onClick={() => setRecordFilter('ALL')}>全部</button><button className={recordFilter === 'MANUAL' ? 'active' : ''} onClick={() => setRecordFilter('MANUAL')}>逐步</button><button className={recordFilter === 'AUTOMATIC' ? 'active' : ''} onClick={() => setRecordFilter('AUTOMATIC')}>连续</button></nav><div className="flow-run-record-directory-list">{standaloneRecords.map(recordItem)}{scheduledContinuousGroups.map(group => { const open = !collapsedScheduleIds.has(group.id); return <section className="flow-run-schedule-directory" key={group.id}><button type="button" className="flow-run-schedule-directory-toggle" aria-expanded={open} onClick={() => setCollapsedScheduleIds(current => { const next = new Set(current); if (next.has(group.id)) next.delete(group.id); else next.add(group.id); return next; })}>{open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}<FolderClosed size={14}/><span><b>{group.name}</b><small>{group.records.length} 条定时连续运行记录</small></span></button>{open && <div className="flow-run-schedule-directory-records">{group.records.map(recordItem)}</div>}</section>; })}{!records.length && <div className="run-directory-record-empty"><CircleDot size={20}/><span>该 FlowRun 尚未创建{recordFilter === 'AUTOMATIC' ? '连续' : recordFilter === 'MANUAL' ? '逐步' : ''}运行记录。</span><button className="secondary" onClick={() => setCreatingRecordRunId(selectedRun.id)}>新增运行记录</button></div>}</div></section> : selectedFlow ? <div className="run-directory-empty"><FolderClosed size={30}/><b>{selectedFlow.name}</b><span>选择左侧的 FlowRun，查看运行记录或新增记录。</span></div> : <div className="run-directory-empty"><Box size={30}/><b>选择一个流程编排</b><span>点击流程目录后，选择其下的 FlowRun。</span></div>}</main>
    </div>
    {creatingRecordRunId && <CreateRunRecordDialog runId={creatingRecordRunId} onClose={() => setCreatingRecordRunId(undefined)}/>}
    {terminalRun && <FlowRunTerminalDialog runId={terminalRun.id} runName={terminalRun.name} onClose={() => setTerminalRun(undefined)}/>}
    {starting && <StartRunDialog flow={starting} environments={environments} onClose={() => setStarting(undefined)} onStart={async input => { const created = await api.runFlow(starting.id, input); selectRun(starting.id, created.id); setStarting(undefined); await qc.invalidateQueries({ queryKey: ['runs'] }); }}/>}
  </section>;
}
