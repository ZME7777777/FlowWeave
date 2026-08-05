import { ArrowRight, CheckSquare, ChevronDown, ChevronRight, CircleDot, Filter, Play, Plus, Search, Trash2 } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { StartRunDialog } from '../components/StartRunDialog';
import { useWorkbenchStore } from '../store/workbench';
import type { FlowDefinition, FlowRunSummary } from '../types';

const STATUS_LABELS: Record<string, string> = { ACTIVE: '运行中', WAITING_HUMAN: '等待人工', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消' };
const progressLabel = (run: FlowRunSummary) => run.progress.active ? `${run.progress.accepted} 已完成 / ${run.progress.terminal} 终态 / ${run.progress.active} 已激活` : '尚未激活节点';

type RunGroup = { id: string; name: string; rowVersion?: number | null; flow?: FlowDefinition; runs: FlowRunSummary[] };

export function RunsPage() {
  const qc = useQueryClient();
  const openRun = useWorkbenchStore(state => state.openRun);
  const [starting, setStarting] = useState<FlowDefinition>();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('ALL');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const { data: runs = [] } = useQuery({ queryKey: ['runs'], queryFn: api.runs, refetchInterval: deleting ? false : 4000 });
  const { data: flows = [] } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const { data: assets = [] } = useQuery({ queryKey: ['nodes'], queryFn: () => api.nodes() });
  const statuses = useMemo(() => [...new Set(runs.map(run => run.state))].sort(), [runs]);
  const groups = useMemo<RunGroup[]>(() => {
    const term = search.trim().toLowerCase();
    const flowById = new Map(flows.map(flow => [flow.id, flow]));
    const ids = new Set([...flows.map(flow => flow.id), ...runs.map(run => run.flow_definition_id)]);
    return [...ids].flatMap(id => {
      const flow = flowById.get(id);
      const related = runs.filter(run => run.flow_definition_id === id);
      const name = flow?.name ?? related[0]?.flow_name ?? '已删除的流程';
      const description = flow?.description ?? '';
      const flowMatches = !term || `${name} ${description}`.toLowerCase().includes(term);
      const instances = related.filter(run => {
        if (status !== 'ALL' && run.state !== status) return false;
        return flowMatches || `${run.name} ${run.run_no} ${run.current_node_name ?? ''} ${run.current_attempt_state ?? ''}`.toLowerCase().includes(term);
      });
      if ((!flowMatches || status !== 'ALL') && !instances.length) return [];
      return [{ id, name, rowVersion: flow?.row_version ?? related[0]?.flow_row_version, flow, runs: instances }];
    });
  }, [flows, runs, search, status]);
  const visibleIds = groups.flatMap(group => group.runs.map(run => run.id));
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));
  const toggleGroup = (flowId: string) => setCollapsed(old => { const next = new Set(old); if (next.has(flowId)) next.delete(flowId); else next.add(flowId); return next; });
  const toggleRun = (id: string) => setSelectedIds(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleVisible = () => setSelectedIds(old => { const next = new Set(old); if (allVisibleSelected) visibleIds.forEach(id => next.delete(id)); else visibleIds.forEach(id => next.add(id)); return next; });
  const removeMany = async (ids: string[], label: string) => {
    if (!ids.length || !window.confirm(`确定永久删除${label}吗？相关 Attempt、门禁结果、事件和产物也会被清理，此操作不可撤销。`)) return;
    setDeleting(true); setError(''); setNotice('');
    const results = await Promise.allSettled(ids.map(id => api.deleteRun(id)));
    const failed = ids.filter((_, index) => results[index].status === 'rejected');
    const succeeded = ids.length - failed.length;
    setSelectedIds(new Set(failed));
    if (failed.length) {
      const reason = results.find(item => item.status === 'rejected') as PromiseRejectedResult | undefined;
      setError(`已删除 ${succeeded} 个运行，${failed.length} 个失败：${reason?.reason instanceof Error ? reason.reason.message : '请求失败'}`);
    } else setNotice(`已永久删除 ${succeeded} 个流程运行。`);
    await qc.invalidateQueries({ queryKey: ['runs'] });
    setDeleting(false);
  };

  return <section className="page runs-page"><div className="page-head"><div><span className="eyebrow">FLOW RUNS</span><h1>流程运行</h1><p>按流程编排查看运行实例，跟踪激活节点、Attempt、门禁结果和产物版本。</p></div><button className="primary" disabled={!flows.length} onClick={() => setStarting(flows[0])}><Plus size={16}/>启动流程</button></div>
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    <div className="run-list-tools"><label><Search size={14}/><input aria-label="搜索流程或运行" value={search} placeholder="搜索流程、运行或当前节点" onChange={event => setSearch(event.target.value)}/></label><label><Filter size={14}/><select aria-label="运行状态筛选" value={status} onChange={event => setStatus(event.target.value)}><option value="ALL">全部状态</option>{statuses.map(value => <option key={value} value={value}>{STATUS_LABELS[value] ?? value}</option>)}</select></label><div className="bulk-actions"><button className="secondary" disabled={!visibleIds.length || deleting} onClick={toggleVisible}><CheckSquare size={14}/>{allVisibleSelected ? '取消全选' : '全选当前结果'}</button><button className="danger" disabled={!selectedIds.size || deleting} onClick={() => void removeMany([...selectedIds], `选中的 ${selectedIds.size} 个运行`)}><Trash2 size={14}/>{deleting ? '删除中…' : `批量删除 (${selectedIds.size})`}</button></div><span>{groups.length} 个流程 · {visibleIds.length} 个运行</span></div>
    {!runs.length && <div className="empty"><Play size={26}/><b>暂无流程运行</b><span>先创建流程编排，再启动第一个运行。</span></div>}{!!runs.length && !groups.length && <div className="empty"><Search size={24}/><b>没有匹配的流程运行</b><span>调整搜索关键词或状态筛选。</span></div>}
    <div className="run-groups">{groups.map(group => { const isCollapsed = collapsed.has(group.id); const pending = group.runs.filter(run => run.has_pending_action).length; return <section className="run-group" key={group.id}><header><button className="run-group-toggle" aria-label={`${isCollapsed ? '展开' : '收起'} ${group.name}`} aria-expanded={!isCollapsed} onClick={() => toggleGroup(group.id)}>{isCollapsed ? <ChevronRight size={15}/> : <ChevronDown size={15}/>}<span><b>{group.name}</b><small>{group.flow ? `流程编排 · 当前版本 v${group.rowVersion ?? '—'}` : `历史流程快照 · v${group.rowVersion ?? '—'}`}</small></span></button><span>{group.runs.length} 个运行 · {pending} 个待处理</span>{group.flow ? <button className="secondary" onClick={() => setStarting(group.flow)}><Plus size={13}/>启动</button> : <span className="deleted-resource">流程已删除</span>}</header>{!isCollapsed && <div className="run-table"><div className="table-head"><span/><span>运行实例</span><span>状态 / 当前节点</span><span>进度</span><span>时间</span><span/></div>{group.runs.map(run => <div className="run-row" key={run.id}><label className="resource-check"><input type="checkbox" aria-label={`选择运行 ${run.name}`} checked={selectedIds.has(run.id)} onChange={() => toggleRun(run.id)}/></label><button className="run-open" onClick={() => openRun(run.id)}><span><b>Run #{run.run_no} · {run.name}</b><small>Snapshot v{run.active_snapshot_version ?? '—'} · {run.id.slice(0, 8)}</small></span><span><span className={`run-state ${run.state.toLowerCase()}`}><CircleDot size={12}/>{STATUS_LABELS[run.state] ?? run.state}</span><small>{run.current_node_name || '无当前节点'} · {run.current_attempt_state || '未开始'}</small></span><span><b>{progressLabel(run)}</b>{run.has_pending_action && <small className="pending-action">需要人工处理</small>}</span><span><b>{new Date(run.started_at).toLocaleString()}</b><small>更新 {new Date(run.updated_at).toLocaleString()}</small></span><ArrowRight size={16}/></button><button className="run-delete" aria-label={`删除运行 ${run.name}`} title="删除运行" onClick={() => void removeMany([run.id], `运行“${run.name}”`)}><Trash2 size={15}/></button></div>)}</div>}</section>; })}</div>
    {starting && <StartRunDialog flow={starting} assets={assets} onClose={() => setStarting(undefined)} onStart={async input => { const run = await api.runFlow(starting.id, input); setStarting(undefined); openRun(run.id, run.node_runs[0]?.id); }}/>} </section>;
}
