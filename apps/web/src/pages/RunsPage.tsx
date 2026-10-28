import { Box, CheckSquare, ChevronDown, ChevronRight, CircleDot, Copy, Download, FolderClosed, Info, Pause, Play, Plus, Search, Square, Terminal, Trash2, Upload, X } from 'lucide-react';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import { CreateRunRecordDialog } from '../components/CreateRunRecordDialog';
import { RecordConfigImportDialog } from './WorkbenchPage';
import { FlowRunTerminalDialog } from '../components/AgentRuntimeSidebar';
import { useProductDialog } from '../components/ProductDialogContext';
import { StartRunDialog } from '../components/StartRunDialog';
import { useWorkbenchStore } from '../store/workbench';
import type { AutomaticRecordConfigDocument, FlowDefinition, RecordConfigDocument, FlowRunSummary, StepwiseRecordConfigDocument } from '../types';

const STATUS_LABELS: Record<string, string> = { ACTIVE: '运行中', WAITING_HUMAN: '等待人工', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已取消', DRAFT: '草稿' };
type RecordKind = 'MANUAL' | 'AUTOMATIC';
type DirectoryRecord = { kind: RecordKind; id: string; name: string; state: string; startedAt: string; detail: string; scheduleId?: string; scheduleName?: string };

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

function CopyDirectoryRecordDialog({ record, onClose, onCopy }: { record: DirectoryRecord; onClose: () => void; onCopy: (name: string) => Promise<void> }) {
  const [name, setName] = useState(`${record.name} · 副本`);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const label = record.kind === 'MANUAL' ? '逐步运行记录' : '连续运行记录';
  const copy = async () => {
    if (!name.trim()) return;
    setBusy(true); setError('');
    try { await onCopy(name.trim()); onClose(); } catch (reason) { setError(reason instanceof Error ? reason.message : '拷贝记录失败。'); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop"><section className="modal automatic-record-dialog" role="dialog" aria-modal="true" aria-label={`拷贝${label}`}><header><div><span className="eyebrow">COPY RECORD</span><h2>拷贝{label}</h2><p>仅复制初始配置与人工输入，不包含会话、输出或运行过程。</p></div><button type="button" className="ghost" aria-label={`关闭拷贝${label}`} disabled={busy} onClick={onClose}><X size={17}/></button></header><label>副本名称<input aria-label="副本名称" value={name} maxLength={220} autoFocus onChange={event => setName(event.target.value)}/></label>{error && <p className="error">拷贝失败：{error}</p>}<footer><button type="button" className="ghost" disabled={busy} onClick={onClose}>取消</button><button type="button" className="primary" disabled={!name.trim() || busy} onClick={() => void copy()}>{busy ? '拷贝中…' : '确认拷贝'}</button></footer></section></div>;
}

export function RunsPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const openAutomaticRecord = useWorkbenchStore(state => state.openAutomaticRecord);
  const openStepwiseRecord = useWorkbenchStore(state => state.openStepwiseRecord);
  const runDirectory = useWorkbenchStore(state => state.runDirectory);
  const updateRunDirectory = useWorkbenchStore(state => state.updateRunDirectory);
  const [creatingRecordRunId, setCreatingRecordRunId] = useState<string>();
  const [terminalRun, setTerminalRun] = useState<{ id: string; name: string }>();
  const [starting, setStarting] = useState<FlowDefinition>();
  const [lifecycleRunId, setLifecycleRunId] = useState<string>();
  const [lifecycleError, setLifecycleError] = useState('');
  const [search, setSearch] = useState('');
  const selectedFlowId = runDirectory.selectedFlowId;
  const selectedRunId = runDirectory.selectedRunId;
  const expandedFlowIds = useMemo(() => new Set(runDirectory.expandedFlowIds), [runDirectory.expandedFlowIds]);
  const collapsedScheduleIds = useMemo(() => new Set(runDirectory.collapsedScheduleIds), [runDirectory.collapsedScheduleIds]);
  const recordFilter = runDirectory.recordFilter;
  const recordSearch = runDirectory.recordSearch;
  const [debouncedRecordSearch, setDebouncedRecordSearch] = useState({ runId: selectedRunId, term: recordSearch.trim() });
  const recordSearchTerm = debouncedRecordSearch.runId === selectedRunId && recordSearch.trim() ? debouncedRecordSearch.term : '';
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedRecordSearch({ runId: selectedRunId, term: recordSearch.trim() }), 250);
    return () => window.clearTimeout(timer);
  }, [recordSearch, selectedRunId]);
  const changeRecordSearch = (value: string) => updateRunDirectory({ recordSearch: value, selectedRecordKeys: [] });
  const selectedRecordKeys = useMemo(() => new Set(runDirectory.selectedRecordKeys), [runDirectory.selectedRecordKeys]);
  const [copyRecord, setCopyRecord] = useState<DirectoryRecord>();
  const [importKind, setImportKind] = useState<RecordKind>();
  const [managingRecords, setManagingRecords] = useState(false);
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
  const recordListRef = useRef<HTMLDivElement>(null);
  const runInfoRef = useRef<HTMLDetailsElement>(null);
  const recordPageSentinelRef = useRef<HTMLDivElement>(null);
  const directoryQuery = useInfiniteQuery({
    queryKey: ['flow-run-record-directory', selectedRunId, recordFilter, recordSearchTerm],
    queryFn: ({ pageParam, signal }) => api.recordDirectory(selectedRunId!, recordFilter, pageParam, signal, recordSearchTerm),
    initialPageParam: null as string | null,
    getNextPageParam: page => page.next_cursor ?? undefined,
    enabled: Boolean(selectedRunId),
    staleTime: 10_000,
    retry: false,
  });
  useEffect(() => {
    if (recordListRef.current) recordListRef.current.scrollTop = 0;
    if (runInfoRef.current) runInfoRef.current.open = false;
  }, [selectedRunId, recordFilter, recordSearchTerm]);
  useEffect(() => {
    const closeInfo = (event: PointerEvent) => {
      const info = runInfoRef.current;
      if (info?.open && event.target instanceof Node && !info.contains(event.target)) info.open = false;
    };
    const escapeInfo = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || !runInfoRef.current?.open) return;
      runInfoRef.current.open = false;
      event.preventDefault();
    };
    document.addEventListener('pointerdown', closeInfo, true);
    document.addEventListener('keydown', escapeInfo, true);
    return () => {
      document.removeEventListener('pointerdown', closeInfo, true);
      document.removeEventListener('keydown', escapeInfo, true);
    };
  }, []);
  const { fetchNextPage, hasNextPage, isFetching, isFetchNextPageError } = directoryQuery;
  useEffect(() => {
    const sentinel = recordPageSentinelRef.current;
    const root = recordListRef.current;
    if (!sentinel || !root || !hasNextPage || isFetching || isFetchNextPageError) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) void fetchNextPage({ cancelRefetch: false });
    }, { root, rootMargin: '160px 0px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetching, isFetchNextPageError, selectedRunId, recordFilter, recordSearchTerm]);
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
    const nextExpanded = new Set(expandedFlowIds);
    if (nextExpanded.has(flowId)) nextExpanded.delete(flowId); else nextExpanded.add(flowId);
    updateRunDirectory({
      selectedFlowId: flowId,
      selectedRunId: undefined,
      recordSearch: '',
      selectedRecordKeys: [],
      expandedFlowIds: [...nextExpanded],
    });
  };
  const selectRun = (flowId: string, runId: string) => {
    const nextExpanded = new Set(expandedFlowIds);
    nextExpanded.add(flowId);
    updateRunDirectory({ selectedFlowId: flowId, selectedRunId: runId, expandedFlowIds: [...nextExpanded],
      ...(runId !== selectedRunId ? { recordSearch: '', selectedRecordKeys: [] } : {}),
    });
    setLifecycleError('');
  };
  const toggleRuntime = async (run: FlowRunSummary) => {
    const isPaused = run.runtime_status === 'STOPPED';
    const action = isPaused ? '恢复' : '暂停';
    if (!await dialog.confirm({
      title: `${action}容器“${run.name}”？`,
      message: isPaused
        ? '将启动保留的运行容器，并恢复已有工作区和会话数据。'
        : '将停止当前运行容器以回收计算资源；工作区、会话和流程数据会保留。',
      confirmLabel: action,
    })) return;
    setLifecycleRunId(run.id);
    setLifecycleError('');
    try {
      const overview = await api.runtimeOverview(run.id);
      if (overview.active_generation == null || overview.session_row_version == null) throw new Error('当前运行环境不可执行该操作。');
      if (isPaused) await api.resumeRuntime(run.id, overview.active_generation, overview.session_row_version);
      else await api.pauseRuntime(run.id, overview.active_generation, overview.session_row_version);
      await qc.invalidateQueries({ queryKey: ['runs'] });
    } catch (reason) {
      setLifecycleError(reason instanceof Error ? reason.message : `${action}容器失败，请重试。`);
    } finally {
      setLifecycleRunId(undefined);
    }
  };
  const records = useMemo<DirectoryRecord[]>(() => {
    const seen = new Set<string>();
    return (directoryQuery.data?.pages.flatMap(page => page.items) ?? []).flatMap(item => {
      const key = `${item.kind}:${item.id}`;
      if (seen.has(key)) return [];
      seen.add(key);
      return [{ ...item, startedAt: item.started_at, scheduleId: item.schedule_id ?? undefined, scheduleName: item.schedule_name ?? undefined }];
    });
  }, [directoryQuery.data]);
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
  const recordKey = (item: DirectoryRecord) => `${item.kind}:${item.id}`;
  const selectedRecords = records.filter(item => selectedRecordKeys.has(recordKey(item)));
  const selectedKinds = new Set(selectedRecords.map(item => item.kind));
  const canExport = selectedRecords.length > 0 && selectedKinds.size === 1;
  const allSelected = records.length > 0 && records.every(item => selectedRecordKeys.has(recordKey(item)));
  const toggleRecord = (item: DirectoryRecord, checked: boolean) => {
    const next = new Set(selectedRecordKeys);
    if (checked) next.add(recordKey(item)); else next.delete(recordKey(item));
    updateRunDirectory({ selectedRecordKeys: [...next] });
  };
  const toggleAllRecords = () => updateRunDirectory({ selectedRecordKeys: allSelected ? [] : records.map(recordKey) });
  const refreshRecords = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['flow-run-stepwise-records', selectedRunId] }),
      qc.invalidateQueries({ queryKey: ['flow-run-record-directory', selectedRunId] }),
      qc.invalidateQueries({ queryKey: ['flow-run-automatic-records', selectedRunId] }),
    ]);
  };
  const copySelectedRecord = async (name: string) => {
    if (!selectedRunId || !copyRecord) return;
    const copied = copyRecord.kind === 'MANUAL'
      ? await api.copyStepwiseRecord(selectedRunId, copyRecord.id, name)
      : await api.copyAutomaticRecord(selectedRunId, copyRecord.id, name);
    await refreshRecords();
    updateRunDirectory({ selectedRecordKeys: [`${copyRecord.kind}:${copied.id}`] });
    if (copyRecord.kind === 'MANUAL') openStepwiseRecord(selectedRunId, copied.id);
    else openAutomaticRecord(selectedRunId, copied.id);
  };
  const deleteSelectedRecords = async () => {
    if (!selectedRunId || !selectedRecords.length) return;
    const count = selectedRecords.length;
    if (!await dialog.confirm({ title: count === 1 ? '删除运行记录？' : `删除 ${count} 条运行记录？`, message: '将永久删除所选记录及其关联会话、工作区、产物和执行历史；此操作不可恢复。', confirmLabel: '删除', tone: 'danger' })) return;
    setManagingRecords(true);
    try {
      const results = await Promise.allSettled(selectedRecords.map(record => record.kind === 'MANUAL'
        ? api.deleteStepwiseRecord(selectedRunId, record.id)
        : api.deleteAutomaticRecord(selectedRunId, record.id)));
      await refreshRecords();
      updateRunDirectory({ selectedRecordKeys: [] });
      const failed = results.filter(result => result.status === 'rejected');
      if (failed.length) window.alert(`${failed.length} 条记录删除失败，请刷新后重试。`);
    } finally { setManagingRecords(false); }
  };
  const exportSelectedRecords = async () => {
    if (!selectedRunId || !canExport) return;
    setManagingRecords(true);
    try {
      const kind = selectedRecords[0].kind;
      const config = kind === 'MANUAL'
        ? await api.exportStepwiseRecordConfigs(selectedRunId, selectedRecords.map(item => item.id))
        : await api.exportAutomaticRecordConfigs(selectedRunId, selectedRecords.map(item => item.id));
      const link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' }));
      link.download = `${kind === 'MANUAL' ? 'stepwise' : 'continuous'}-record-config.json`;
      link.click();
      URL.revokeObjectURL(link.href);
    } finally { setManagingRecords(false); }
  };
  const importRecords = async (config: RecordConfigDocument) => {
    if (!selectedRunId || !importKind) return;
    if (importKind === 'MANUAL') {
      if (config.format !== 'flowweave.stepwise-record-config') throw new Error('这不是逐步运行配置文件。');
      await api.importStepwiseRecordConfigs(selectedRunId, config as StepwiseRecordConfigDocument);
    } else {
      if (config.format !== 'flowweave.continuous-record-config') throw new Error('这不是连续运行配置文件。');
      await api.importAutomaticRecordConfigs(selectedRunId, config as AutomaticRecordConfigDocument);
    }
    await refreshRecords();
  };
  const recordItem = (item: DirectoryRecord) => {
    const selected = selectedRecordKeys.has(recordKey(item));
    const openRecord = () => item.kind === 'MANUAL'
      ? openStepwiseRecord(selectedRunId!, item.id)
      : openAutomaticRecord(selectedRunId!, item.id);
    return <article className={`flow-run-record-row${selected ? ' selected' : ''}`} key={recordKey(item)}>
      <label className="flow-run-record-selection" title={`选择运行记录 ${item.name}`}>
        <input type="checkbox" className="flow-run-record-select" aria-label={`选择运行记录 ${item.name}`} checked={selected} onChange={event => toggleRecord(item, event.target.checked)}/>
      </label>
      <button type="button" className="flow-run-record-content" aria-label={`选择运行记录 ${item.name}`} aria-pressed={selected} onClick={() => toggleRecord(item, !selected)}>
        <span className="flow-run-record-identity"><b className="flow-run-record-name" title={item.name}>{item.name}</b><code className="flow-run-record-id" title={`记录 ID：${item.id}`}>ID {item.id}</code></span>
        <span className="flow-run-record-kind">{item.kind === 'MANUAL' ? '逐步' : '连续'}</span>
        <span className="flow-run-record-state" data-state={item.state}>{STATUS_LABELS[item.state] ?? item.state}</span>
        <span className="flow-run-record-progress" title={item.detail}>{item.detail}</span>
        <time dateTime={item.startedAt} title={new Date(item.startedAt).toLocaleString('zh-CN', { hour12: false })}>{new Date(item.startedAt).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}</time>
      </button>
      <button type="button" className="flow-run-record-enter" aria-label={`进入运行记录 ${item.name}`} onClick={openRecord}>打开<ChevronRight size={14}/></button>
    </article>;
  };
  return <section className="page runs-page run-directory-page">
    <div className="run-directory-layout">
      <aside className="flow-run-directory"><header><div><b>流程编排</b><small>{runDirectories.length} 个流程</small></div><button className="agent-schedule-new" disabled={!selectedFlow || !isStartableFlow(selectedFlow)} title={!selectedFlow ? '请先选择一个流程编排' : isStartableFlow(selectedFlow) ? undefined : '该流程未配置启动节点'} onClick={() => selectedFlow && setStarting(selectedFlow)}><Plus size={14}/>新建</button></header><label className="run-directory-search"><Search size={14}/><input aria-label="搜索流程或运行" value={search} placeholder="搜索流程或运行" onChange={event => setSearch(event.target.value)}/></label><div className="flow-run-directory-list">{runDirectories.map(flow => { const expanded = expandedFlowIds.has(flow.id); const selected = selectedFlowId === flow.id; return <section className={`flow-run-tree-group${selected ? ' selected' : ''}`} key={flow.id}><div className="flow-run-tree-flow"><button type="button" className="flow-run-tree-select" aria-current={selected ? 'true' : undefined} aria-expanded={expanded} onClick={() => selectFlow(flow.id)}><FolderClosed size={14}/><span><b>{flow.name}</b><small>{flow.runs.length} 个 FlowRun</small></span></button></div>{expanded && <div className="flow-run-tree-runs">{flow.runs.length ? flow.runs.map(run => <button type="button" className={`flow-run-tree-run${selectedRunId === run.id ? ' active' : ''}`} key={run.id} onClick={() => selectRun(flow.id, run.id)}><i/><span><b>Run #{run.run_no} · {run.name}</b><small>快照 v{run.active_snapshot_version ?? '—'} · {environmentLabel(run)}</small><em className={`run-runtime-badge ${run.runtime_write_available ? 'available' : run.runtime_status === 'STOPPED' ? 'paused' : 'pending'}`}>{runtimeLabel(run)}</em></span></button>) : <p>该流程尚未创建 FlowRun。</p>}</div>}</section>; })}{!runDirectories.length && <p>没有匹配的流程或 FlowRun。</p>}</div></aside>
      <main className="flow-run-directory-detail">{selectedRun ? <section className="flow-run-record-directory">
        <header className="run-record-directory-header">
          <h3 title={selectedRun.name}>{selectedRun.name}</h3>
          <span className="run-record-header-number">Run #{selectedRun.run_no}</span>
          <span className={`run-record-header-runtime${selectedRun.runtime_write_available ? ' available' : ''}`}>{runtimeLabel(selectedRun)}</span>
          <span className="run-record-header-environment" title={environmentLabel(selectedRun)}>{environmentLabel(selectedRun)}</span>
          <details className="run-record-header-info" ref={runInfoRef}>
            <summary aria-label="运行信息" title="查看运行信息"><Info size={14}/></summary>
            <dl>
              <div><dt>流程</dt><dd>{selectedRun.flow_name ?? selectedFlow?.name}</dd></div>
              <div><dt>环境版本</dt><dd>{environmentLabel(selectedRun)}</dd></div>
              <div><dt>创建时间</dt><dd>{new Date(selectedRun.started_at).toLocaleString('zh-CN', { hour12: false })}</dd></div>
              <div><dt>流程版本</dt><dd>v{selectedRun.flow_row_version ?? '—'} · 快照 v{selectedRun.active_snapshot_version ?? '—'}</dd></div>
            </dl>
          </details>
          <div className="run-directory-actions"><button className="secondary" disabled={!selectedRun.runtime_write_available} onClick={() => setTerminalRun({ id: selectedRun.id, name: selectedRun.name })}><Terminal size={14}/>进入终端</button>{(selectedRun.runtime_status === 'ACTIVE' || selectedRun.runtime_status === 'STOPPED') && <button className="secondary" disabled={lifecycleRunId === selectedRun.id} onClick={() => void toggleRuntime(selectedRun)}>{selectedRun.runtime_status === 'STOPPED' ? <Play size={14}/> : <Pause size={14}/>}{lifecycleRunId === selectedRun.id ? '处理中…' : selectedRun.runtime_status === 'STOPPED' ? '恢复容器' : '暂停容器'}</button>}</div>
        </header>
        {lifecycleError && <p className="error run-directory-lifecycle-error" role="alert">{lifecycleError}</p>}
        <section className="flow-run-record-table" aria-label="运行记录表格">
          <div className="run-record-management-toolbar"><nav className="run-record-filter" aria-label="运行记录类型"><button className={recordFilter === 'ALL' ? 'active' : ''} onClick={() => updateRunDirectory({ recordFilter: 'ALL' })}>全部</button><button className={recordFilter === 'MANUAL' ? 'active' : ''} onClick={() => updateRunDirectory({ recordFilter: 'MANUAL' })}>逐步</button><button className={recordFilter === 'AUTOMATIC' ? 'active' : ''} onClick={() => updateRunDirectory({ recordFilter: 'AUTOMATIC' })}>连续</button></nav><button type="button" className="secondary" aria-pressed={allSelected} disabled={!records.length || managingRecords} title="选择当前已加载的记录" onClick={toggleAllRecords}>{allSelected ? <CheckSquare size={14}/> : <Square size={14}/>}{allSelected ? '取消全选' : '全选'}</button><span>已选择 {selectedRecords.length} 条</span><label className="run-record-search"><Search size={14}/><input type="search" aria-label="搜索运行记录" placeholder="搜索记录名称或 ID" maxLength={220} value={recordSearch} onChange={event => changeRecordSearch(event.target.value)}/>{recordSearch && <button type="button" className="ghost" aria-label="清除运行记录搜索" title="清除搜索" onClick={() => changeRecordSearch('')}><X size={13}/></button>}</label><button type="button" className="secondary" disabled={selectedRecords.length !== 1 || managingRecords} onClick={() => setCopyRecord(selectedRecords[0])}><Copy size={14}/>拷贝</button><button type="button" className="secondary" disabled={!canExport || managingRecords} title={canExport ? undefined : '请只选择同一类型的记录后导出'} onClick={() => void exportSelectedRecords()}><Download size={14}/>导出</button><button type="button" className="secondary" disabled={managingRecords} onClick={() => setImportKind(recordFilter === 'AUTOMATIC' ? 'AUTOMATIC' : 'MANUAL')}><Upload size={14}/>导入</button><button type="button" className="danger" disabled={!selectedRecords.length || managingRecords} onClick={() => void deleteSelectedRecords()}><Trash2 size={14}/>删除</button><button type="button" className="primary" disabled={managingRecords} onClick={() => setCreatingRecordRunId(selectedRun.id)}><Plus size={14}/>新增</button></div>
          <div className="flow-run-record-directory-list" ref={recordListRef} aria-busy={directoryQuery.isFetching}>{standaloneRecords.map(recordItem)}{scheduledContinuousGroups.map(group => { const open = !collapsedScheduleIds.has(group.id); return <section className="flow-run-schedule-directory" key={group.id}><button type="button" className="flow-run-schedule-directory-toggle" aria-expanded={open} onClick={() => { const next = new Set(collapsedScheduleIds); if (next.has(group.id)) next.delete(group.id); else next.add(group.id); updateRunDirectory({ collapsedScheduleIds: [...next] }); }}>{open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}<FolderClosed size={14}/><span><b>{group.name}</b><small>{group.records.length} 条定时连续运行记录</small></span></button>{open && <div className="flow-run-schedule-directory-records">{group.records.map(recordItem)}</div>}</section>; })}{directoryQuery.hasNextPage && <div ref={recordPageSentinelRef} className="run-record-page-sentinel" aria-hidden="true"/>}{directoryQuery.isPending && <div className="run-directory-record-empty" role="status">正在加载运行记录…</div>}{directoryQuery.isError && !directoryQuery.data && <div className="run-directory-record-empty" role="alert">读取运行记录失败<button type="button" className="secondary" onClick={() => void directoryQuery.refetch()}>重试</button></div>}{directoryQuery.isFetchingNextPage && <div className="run-record-page-status" role="status">正在加载更多…</div>}{directoryQuery.isFetchNextPageError && <div className="run-record-page-status" role="alert">加载更多失败<button type="button" onClick={() => void directoryQuery.fetchNextPage({ cancelRefetch: false })}>重试</button></div>}{!directoryQuery.isPending && !directoryQuery.isError && !records.length && <div className="run-directory-record-empty"><CircleDot size={20}/>{recordSearchTerm ? <><span>没有匹配的运行记录。</span><button className="secondary" onClick={() => changeRecordSearch('')}>清除搜索</button></> : <><span>该 FlowRun 尚未创建{recordFilter === 'AUTOMATIC' ? '连续' : recordFilter === 'MANUAL' ? '逐步' : ''}运行记录。</span><button className="secondary" onClick={() => setCreatingRecordRunId(selectedRun.id)}>新增运行记录</button></>}</div>}</div></section></section> : selectedFlow ? <div className="run-directory-empty"><FolderClosed size={30}/><b>{selectedFlow.name}</b><span>选择左侧的 FlowRun，查看运行记录或新增记录。</span></div> : <div className="run-directory-empty"><Box size={30}/><b>选择一个流程编排</b><span>点击流程目录后，选择其下的 FlowRun。</span></div>}</main>
    </div>
    {creatingRecordRunId && <CreateRunRecordDialog runId={creatingRecordRunId} onClose={() => setCreatingRecordRunId(undefined)}/>}
    {copyRecord && <CopyDirectoryRecordDialog record={copyRecord} onClose={() => setCopyRecord(undefined)} onCopy={copySelectedRecord}/>}
    {importKind && <RecordConfigImportDialog mode={importKind === 'MANUAL' ? 'STEPWISE' : 'AUTOMATIC'} onClose={() => setImportKind(undefined)} onImport={importRecords}/>}

    {terminalRun && <FlowRunTerminalDialog runId={terminalRun.id} runName={terminalRun.name} onClose={() => setTerminalRun(undefined)}/>}
    {starting && <StartRunDialog flow={starting} environments={environments} onClose={() => setStarting(undefined)} onStart={async input => { const created = await api.runFlow(starting.id, input); selectRun(starting.id, created.id); setStarting(undefined); await qc.invalidateQueries({ queryKey: ['runs'] }); }}/>}
  </section>;
}
