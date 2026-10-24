import { Box, ChevronRight, CircleDot, Filter, Plus, Search, Terminal, Trash2 } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { StartRunDialog } from '../components/StartRunDialog';
import { FlowRunTerminalDialog } from '../components/AgentRuntimeSidebar';
import { useProductDialog } from '../components/ProductDialogContext';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowDefinition, FlowRunAutomaticRecordSummary, FlowRunStepwiseRecord, FlowRunSummary, TerminalEnvironment } from '../types';

const STATUS_LABELS: Record<string, string> = { ACTIVE: '运行中', WAITING_HUMAN: '等待人工', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消', DRAFT: '草稿' };
type RecordFilter = 'ALL' | 'MANUAL' | 'AUTOMATIC';

const isStartableFlow = (flow: FlowDefinition) => {
  if (!flow.nodes.length) return false;
  const targets = new Set(flow.edges.map(edge => edge.target_instance_key));
  return flow.nodes.some(node => !targets.has(node.instance_key));
};

const runtimeLabel = (run: FlowRunSummary) => {
  if (run.runtime_write_available) return '运行环境可用';
  if (run.runtime_status === 'STOPPED') return '运行环境已暂停';
  if (run.runtime_status === 'DEGRADED') return '运行环境暂不可用';
  return '运行环境准备中';
};

export function RunsPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const openRun = useWorkbenchStore(state => state.openRun);
  const openAutomaticRecord = useWorkbenchStore(state => state.openAutomaticRecord);
  const openStepwiseRecord = useWorkbenchStore(state => state.openStepwiseRecord);
  const [starting, setStarting] = useState<FlowDefinition>();
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [search, setSearch] = useState('');
  const [recordFilter, setRecordFilter] = useState<RecordFilter>('ALL');
  const [deleting, setDeleting] = useState(false);
  const [terminalRun, setTerminalRun] = useState<{ id: string; name: string }>();
  const [error, setError] = useState('');
  const { data: runs = [] } = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: deleting ? false : 1500 });
  const { data: flows = [] } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const { data: environments = [] } = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments });
  const selectedRun = runs.find(run => run.id === selectedRunId);
  const selectedFlow = flows.find(flow => flow.id === selectedRun?.flow_definition_id);
  const stepwise = useQuery({ queryKey: ['flow-run-stepwise-records', selectedRunId], queryFn: () => api.stepwiseRecords(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const automatic = useQuery({ queryKey: ['flow-run-automatic-records', selectedRunId], queryFn: () => api.automaticRecordSummaries(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const startableFlows = useMemo(() => flows.filter(isStartableFlow), [flows]);
  const visibleRuns = useMemo(() => {
    const term = search.trim().toLowerCase();
    return runs.filter(run => {
      const flow = flows.find(item => item.id === run.flow_definition_id);
      return !term || `${run.name} ${run.run_no} ${flow?.name ?? run.flow_name ?? ''} ${run.state}`.toLowerCase().includes(term);
    });
  }, [flows, runs, search]);
  const records = useMemo(() => {
    const manual = (stepwise.data ?? []).map(record => ({ kind: 'MANUAL' as const, id: record.id, name: record.name, state: record.state, startedAt: record.started_at, detail: record.start_node_key ? '逐步配置与确认执行' : '等待配置起始节点', record }));
    const continuous = (automatic.data ?? []).map(record => ({ kind: 'AUTOMATIC' as const, id: record.id, name: record.name, state: record.state, startedAt: record.started_at, detail: `${record.progress.accepted}/${record.plan.reachable_node_count} 节点完成`, record }));
    return [...manual, ...continuous].filter(item => recordFilter === 'ALL' || item.kind === recordFilter).sort((left, right) => right.startedAt.localeCompare(left.startedAt));
  }, [automatic.data, recordFilter, stepwise.data]);
  const removeRun = async (run: FlowRunSummary) => {
    if (!await dialog.confirm({ title: `永久删除 FlowRun“${run.name}”？`, message: '将删除该 FlowRun 下的运行记录、会话、产物和工作区，此操作不可撤销。', confirmLabel: '永久删除', tone: 'danger' })) return;
    setDeleting(true); setError('');
    try {
      await api.deleteRun(run.id);
      if (selectedRunId === run.id) setSelectedRunId(undefined);
      await qc.invalidateQueries({ queryKey: ['runs'] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '删除 FlowRun 失败。'); }
    finally { setDeleting(false); }
  };

  return <section className="page runs-page run-directory-page">
    <header className="page-head run-directory-head"><div><span className="eyebrow">FLOWRUN DIRECTORY</span><h1>流程运行</h1><p>每个 FlowRun 是独立的运行目录；选择目录后查看其下的逐步与连续运行记录。</p></div><button className="primary" disabled={!startableFlows.length} title={startableFlows.length ? undefined : '请先为流程添加一个启动节点'} onClick={() => setStarting(startableFlows[0])}><Plus size={16}/>新建 FlowRun</button></header>
    {error && <div className="notice error" role="alert">{error}</div>}
    <div className="run-directory-layout">
      <aside className="flow-run-directory"><header><b>FlowRun 目录</b><small>{visibleRuns.length} 个运行容器</small></header><label className="run-directory-search"><Search size={14}/><input aria-label="搜索 FlowRun" value={search} placeholder="搜索 FlowRun 或流程" onChange={event => setSearch(event.target.value)}/></label><div className="flow-run-directory-list">{visibleRuns.map(run => <article className={selectedRunId === run.id ? 'active' : ''} key={run.id}><button type="button" onClick={() => setSelectedRunId(run.id)}><i/><span><small>Run #{run.run_no} · {STATUS_LABELS[run.state] ?? run.state}</small><b>{run.name}</b><em>{runtimeLabel(run)}</em></span><ChevronRight size={15}/></button></article>)}{!visibleRuns.length && <p>没有匹配的 FlowRun。</p>}</div></aside>
      <main className="flow-run-directory-detail">{!selectedRun ? <div className="run-directory-empty"><Box size={30}/><b>选择一个 FlowRun 目录</b><span>左侧每一项对应一个独立的运行容器；选择后可查看它创建的所有逐步与连续运行记录。</span></div> : <><header className="flow-run-directory-detail-head"><div><span className="eyebrow">FLOWRUN</span><h2>{selectedRun.name}</h2><p>{selectedFlow?.name ?? selectedRun.flow_name ?? '历史流程快照'} · Run #{selectedRun.run_no} · {runtimeLabel(selectedRun)}</p></div><div><button className="secondary" onClick={() => setTerminalRun({ id: selectedRun.id, name: selectedRun.name })}><Terminal size={14}/>终端</button><button className="danger" disabled={deleting} onClick={() => void removeRun(selectedRun)}><Trash2 size={14}/>删除</button></div></header><div className="flow-run-directory-summary"><span><b>状态</b>{STATUS_LABELS[selectedRun.state] ?? selectedRun.state}</span><span><b>运行环境</b>{selectedRun.environment_version_id ? '已绑定' : '未绑定'}</span><span><b>快照</b>v{selectedRun.active_snapshot_version ?? '—'}</span><span><b>创建时间</b>{new Date(selectedRun.started_at).toLocaleString()}</span></div><section className="flow-run-record-directory"><header><div><span className="eyebrow">RUN RECORDS</span><h3>运行记录</h3><p>在此查看该 FlowRun 目录中创建的全部记录；选择一条记录后进入记录详情。</p></div><button className="primary" onClick={() => openRun(selectedRun.id)}><Plus size={14}/>管理运行记录</button></header><nav className="run-record-filter"><button className={recordFilter === 'ALL' ? 'active' : ''} onClick={() => setRecordFilter('ALL')}>全部</button><button className={recordFilter === 'MANUAL' ? 'active' : ''} onClick={() => setRecordFilter('MANUAL')}>逐步</button><button className={recordFilter === 'AUTOMATIC' ? 'active' : ''} onClick={() => setRecordFilter('AUTOMATIC')}>连续</button></nav><div className="flow-run-record-directory-list">{records.map(item => <button key={`${item.kind}:${item.id}`} onClick={() => item.kind === 'MANUAL' ? openStepwiseRecord(selectedRun.id, item.id) : openAutomaticRecord(selectedRun.id, item.id)}><i/><span><small>{item.kind === 'MANUAL' ? '逐步运行' : '连续运行'} · {STATUS_LABELS[item.state] ?? item.state}</small><b>{item.name}</b><em>{item.detail}</em></span><time>{new Date(item.startedAt).toLocaleString()}</time><ChevronRight size={15}/></button>)}{!records.length && <div className="run-directory-record-empty"><CircleDot size={20}/><span>该 FlowRun 尚未创建{recordFilter === 'AUTOMATIC' ? '连续' : recordFilter === 'MANUAL' ? '逐步' : ''}运行记录。</span><button className="secondary" onClick={() => openRun(selectedRun.id)}>进入记录管理</button></div>}</div></section></>}</main>
    </div>
    {starting && <StartRunDialog flow={starting} environments={environments} onClose={() => setStarting(undefined)} onStart={async input => { await api.runFlow(starting.id, input); setStarting(undefined); await qc.invalidateQueries({ queryKey: ['runs'] }); }}/>} {terminalRun && <FlowRunTerminalDialog runId={terminalRun.id} runName={terminalRun.name} onClose={() => setTerminalRun(undefined)}/>}
  </section>;
}
