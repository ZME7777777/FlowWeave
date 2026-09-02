import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { AlertTriangle, ArrowLeft, Bot, Boxes, Check, ChevronDown, Download, ExternalLink, Eye, FileText, Layers3, Play, Plus, RefreshCw, Send, StopCircle, Trash2, Upload, X } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { api, artifactContentUrl, subscribeToRun } from '../api/client';
import { flowMappingEdgeTypes, withMappingLabelOffsets } from '../components/flowMappingEdgeLayout';
import { useProductDialog } from '../components/ProductDialogContext';
import { RuntimeConfirmationPanel } from '../components/RuntimeConfirmationPanel';
import { useEscapeClose } from '../components/useEscapeClose';
import { useWorkbenchStore } from '../store/workbench';
import type { AgentPreset, ArtifactVersion, AttemptState, AutomaticNodePlan, CapabilityAsset, CapabilityCollection, FlowRun, FlowRunAutomaticRecord, GateAgentPreset, GateEvaluation, GatePolicy, NodeAttempt, NodeRun, SnapshotFlowNode } from '../types';
import { withDeploymentBase } from '../deploymentPath';

const attemptState = (run: NodeRun) => run.attempts.at(-1)?.state ?? run.state;

const ATTEMPT_STATE_LABELS: Record<AttemptState, string> = {
  WAITING_INPUT: '等待补充输入',
  START_GATES: '正在检查启动条件',
  START_BLOCKED: '启动条件未通过',
  WAITING_START_CONFIRMATION: '等待确认开始',
  EXECUTING: '正在执行',
  WAITING_HUMAN: '等待人工回复',
  WAITING_CONFIRMATION: '等待工具批次确认',
  END_GATES: '正在检查完成条件',
  END_BLOCKED: '完成条件未通过',
  WAITING_ACCEPTANCE: '等待验收',
  ACCEPTED: '已验收',
  REJECTED: '已退回',
  CANCELLED: '已取消',
};
const FLOW_STATE_LABELS: Record<string, string> = {
  ACTIVE: '运行中', WAITING_HUMAN: '等待人工处理', COMPLETED: '已完成', FAILED: '运行失败', CANCELLED: '已取消',
};
// Bump this whenever graph rendering changes. It also guarantees that a web
// deployment produces a new content-hashed bundle instead of reusing an
// immutable asset cached by an earlier graph renderer.
const GRAPH_RENDER_REVISION = '2026-09-03.1';

const nodeForRun = (run: FlowRun, nodeRun: NodeRun) => {
  const snapshotId = nodeRun.attempts.at(-1)?.snapshot_id;
  const snapshot = run.snapshots.find(item => item.id === snapshotId)
    ?? run.snapshots.find(item => item.id === run.active_snapshot_id)
    ?? run.snapshots.at(-1);
  return snapshot?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
};

const nodeRunName = (run: FlowRun, nodeRun: NodeRun) => {
  const node = nodeForRun(run, nodeRun);
  return node?.alias || node?.asset.name || nodeRun.flow_node_snapshot_key;
};

const nodeVisitNumber = (run: FlowRun, nodeRun: NodeRun) => run.node_runs
  .filter(item => item.flow_node_snapshot_key === nodeRun.flow_node_snapshot_key && item.sequence_no <= nodeRun.sequence_no)
  .length;

const attemptStateLabel = (run: NodeRun) => {
  const attempt = run.attempts.at(-1);
  const state = attempt?.state ?? run.state;
  if (attempt?.error_code === 'RUNTIME_FAILED') return '节点执行失败';
  return state in ATTEMPT_STATE_LABELS ? ATTEMPT_STATE_LABELS[state as AttemptState] : state;
};

function openNodeSession(
  flowRunId: string,
  nodeRunId: string,
  attemptId: string,
  bindingId?: string,
): void {
  const base = `/flow-runs/${encodeURIComponent(flowRunId)}/nodes/${encodeURIComponent(nodeRunId)}/attempts/${encodeURIComponent(attemptId)}/agent-sessions`;
  // Preserve the originating Workbench selection in the previous history
  // entry. The node-session route is browser-addressable, but Back should
  // return to this exact Attempt rather than lose Zustand's transient state.
  window.history.replaceState({
    flowweaveFlowRun: { runId: flowRunId, nodeRunId, attemptId },
  }, '', window.location.href);
  window.history.pushState({ flowweaveNodeSession: true }, '', withDeploymentBase(bindingId ? `${base}/${encodeURIComponent(bindingId)}` : base));
  window.dispatchEvent(new PopStateEvent('popstate'));
}

type WorkbenchMode = 'MANUAL' | 'AUTOMATIC';

function RunRail({ run, mode, automaticRecords, selected, selectedNodeDeletable, manualBusyId, selectedAutomaticId, automaticBusyId, onModeChange, onSelect, onDeleteNode, onSelectAutomatic, onClearSelection, onCreateAutomatic, onDeleteAutomatic, onStartAutomatic }: {
  run: FlowRun; mode: WorkbenchMode; automaticRecords: FlowRunAutomaticRecord[]; selected?: string;
  selectedNodeDeletable: boolean; manualBusyId?: string;
  selectedAutomaticId?: string; automaticBusyId?: string; onModeChange: (mode: WorkbenchMode) => void;
  onSelect: (id: string) => void; onDeleteNode: () => void; onSelectAutomatic: (id: string) => void; onCreateAutomatic: () => void;
  onClearSelection: () => void; onDeleteAutomatic: () => void; onStartAutomatic: (record: FlowRunAutomaticRecord) => void;
}) {
  const active = run.node_runs.filter(item => item.state === 'ACTIVE').length;
  const manualToolbar = <div className="automatic-record-toolbar manual-record-toolbar"><button type="button" className="danger" title={selected && !selectedNodeDeletable ? '请先取消该节点运行并等待停止完成' : undefined} disabled={!selectedNodeDeletable || Boolean(manualBusyId)} onClick={onDeleteNode}><Trash2 size={13}/>{manualBusyId ? '删除中…' : '删除'}</button></div>;
  return <aside className="run-rail flow-run-inner-rail" onClick={event => { if (!isInteractiveClick(event.target)) onClearSelection(); }}><span className="eyebrow">流程运行</span><h2>{run.name}</h2><span data-testid="flow-run-state" className={`run-state ${run.state.toLowerCase()}`}>{FLOW_STATE_LABELS[run.state] ?? run.state}</span><div className="run-progress"><span>{run.progress.accepted} 已验收</span><span>{run.progress.terminal} 已结束</span><span>{active} 进行中</span></div><nav className="inner-run-mode-tabs" role="tablist" aria-label="当前流程运行方式"><button type="button" role="tab" aria-selected={mode === 'MANUAL'} className={mode === 'MANUAL' ? 'active' : ''} onClick={() => onModeChange('MANUAL')}>单节点运行</button><button type="button" role="tab" aria-selected={mode === 'AUTOMATIC'} className={mode === 'AUTOMATIC' ? 'active' : ''} onClick={() => onModeChange('AUTOMATIC')}>自动运行</button></nav>{mode === 'MANUAL' ? <>{manualToolbar}<div className="run-history-title"><b>节点执行记录</b><small>节点和修订轮次只引用 OpenHands Conversation；全部会话归属于当前 FlowRun Runtime。</small></div><div className="timeline">{run.node_runs.map(item => <button type="button" key={item.id} className={selected === item.id ? 'active' : ''} onClick={() => onSelect(item.id)}><i className={String(attemptState(item)).toLowerCase()}/><span><b>{nodeRunName(run, item)}</b><small>第 {nodeVisitNumber(run, item)} 次执行 · {attemptStateLabel(item)}</small></span></button>)}</div></> : <><div className="automatic-record-toolbar"><button type="button" className="danger" disabled={!selectedAutomaticId || Boolean(automaticBusyId)} onClick={onDeleteAutomatic}><Trash2 size={13}/>删除</button><button type="button" className="primary" disabled={Boolean(automaticBusyId)} onClick={onCreateAutomatic}><Plus size={13}/>新增</button></div><div className="run-history-title"><b>自动运行记录</b><small>每条记录独立保存整条可达流程的节点配置，并共享当前 FlowRun Runtime 与 Workspace。</small></div><div className="automatic-record-list">{automaticRecords.map(record => <article key={record.id} className={record.id === selectedAutomaticId ? 'active' : ''}><button type="button" className="automatic-record-select" onClick={() => onSelectAutomatic(record.id)}><span><b>{record.name}</b><small>{record.state === 'DRAFT' ? record.readiness.ready ? '草稿已就绪' : '草稿待补齐' : FLOW_STATE_LABELS[record.state] ?? record.state}</small></span></button>{record.state === 'DRAFT' && <button type="button" className="automatic-record-start" aria-label={`启动自动运行 ${record.name}`} disabled={!record.readiness.ready || Boolean(automaticBusyId)} onClick={() => onStartAutomatic(record)}><Play size={12}/>{automaticBusyId === record.id ? '启动中…' : '启动'}</button>}</article>)}{!automaticRecords.length && <p>暂无自动运行记录。</p>}</div></>}</aside>;
}

function isInteractiveClick(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest('button, a, input, textarea, select, [role="button"]'));
}

type SnapshotGraphNodeData = {
  label: string;
  status: string;
  stateLabel?: string;
  visits: number;
  inputs: SnapshotFlowNode['asset']['inputs'];
  outputs: SnapshotFlowNode['asset']['outputs'];
};

function SnapshotGraphNode({ data, selected }: NodeProps<Node<SnapshotGraphNodeData>>) {
  return <article className={`run-graph-node ${data.status}${selected ? ' snapshot-selected' : ''}`}>
    <Handle id="flow-target" className="run-flow-handle" type="target" position={Position.Left} style={{ top: 18 }}/>
    <Handle id="flow-source" className="run-flow-handle" type="source" position={Position.Right} style={{ top: 18 }}/>
    <header><b>{data.label}</b>{(data.stateLabel || data.visits) && <small>{data.stateLabel}{data.stateLabel && data.visits ? ' · ' : ''}{data.visits ? `运行 ${data.visits} 次` : ''}</small>}</header>
    <div className="run-node-contract">
      <section aria-label="输入端口"><span>输入</span>{data.inputs.length ? data.inputs.map(field => <div key={field.field_key}><Handle id={`input:${field.field_key}`} className="run-data-handle input" type="target" position={Position.Left} style={{ top: '50%' }}/><b>{field.display_name || field.field_key}</b><small>{field.data_type}</small></div>) : <em>无输入</em>}</section>
      <section aria-label="输出端口"><span>输出</span>{data.outputs.length ? data.outputs.map(field => <div key={field.field_key}><b>{field.display_name || field.field_key}</b><small>{field.data_type}</small><Handle id={`output:${field.field_key}`} className="run-data-handle output" type="source" position={Position.Right} style={{ top: '50%' }}/></div>) : <em>无输出</em>}</section>
    </div>
  </article>;
}

const runSnapshotNodeTypes = { snapshotNode: SnapshotGraphNode };

function reachableNodeKeys(run: FlowRun, startNodeKey?: string): Set<string> {
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  if (!startNodeKey || !snapshot) return new Set(snapshot?.definition.nodes.map(node => node.instance_key));
  const downstream = new Map<string, string[]>();
  for (const edge of snapshot.definition.edges ?? []) {
    const targets = downstream.get(edge.source_instance_key) ?? [];
    targets.push(edge.target_instance_key);
    downstream.set(edge.source_instance_key, targets);
  }
  const reachable = new Set<string>();
  const pending = [startNodeKey];
  while (pending.length) {
    const key = pending.pop();
    if (!key || reachable.has(key)) continue;
    reachable.add(key);
    pending.push(...(downstream.get(key) ?? []));
  }
  return reachable;
}

/** A draft node becomes configurable only after every in-scope predecessor
 * has a saved plan with no outstanding readiness issue.  This makes the
 * graph follow the same order users see at runtime without changing the
 * batch draft API used by imports and automation. */
function unlockedAutomaticNodeKeys(run: FlowRun, record: FlowRunAutomaticRecord): Set<string> {
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  if (!snapshot || record.state !== 'DRAFT') return new Set(record.reachable_node_keys);
  const reachable = new Set(record.reachable_node_keys);
  const incomplete = new Set(record.readiness.issues.map(issue => issue.node_key));
  const readyPlans = new Set(Object.keys(record.node_plans).filter(key => !incomplete.has(key)));
  const predecessors = new Map<string, string[]>();
  for (const edge of snapshot.definition.edges ?? []) {
    if (!reachable.has(edge.source_instance_key) || !reachable.has(edge.target_instance_key)) continue;
    const items = predecessors.get(edge.target_instance_key) ?? [];
    items.push(edge.source_instance_key);
    predecessors.set(edge.target_instance_key, items);
  }
  const unlocked = new Set<string>();
  for (const key of record.reachable_node_keys) {
    const upstream = predecessors.get(key) ?? [];
    if (key === record.start_node_key || upstream.every(parent => readyPlans.has(parent))) unlocked.add(key);
  }
  return unlocked;
}

function readyAutomaticPlanKeys(record?: FlowRunAutomaticRecord): Set<string> {
  if (!record) return new Set();
  const incomplete = new Set(record.readiness.issues.map(issue => issue.node_key));
  return new Set(Object.keys(record.node_plans).filter(key => !incomplete.has(key)));
}

function SnapshotGraph({ run, selectedKey, onSelect, onClearSelection, reachableKeys, selectableKeys, configuredPlanKeys, executionRun, missingPlanKeys, showExecutionState = true, neutralHelp, neutralView = false }: { run: FlowRun; selectedKey?: string; onSelect: (key: string) => void; onClearSelection?: () => void; reachableKeys?: Iterable<string>; selectableKeys?: Iterable<string>; configuredPlanKeys?: Iterable<string>; executionRun?: FlowRun; missingPlanKeys?: Iterable<string>; showExecutionState?: boolean; neutralHelp?: string; neutralView?: boolean }) {
  const [linkMode, setLinkMode] = useState<'flow' | 'data'>('flow');
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const reachable = useMemo(() => new Set(reachableKeys ?? (snapshot?.definition.nodes ?? []).map(node => node.instance_key)), [reachableKeys, snapshot]);
  const selectable = useMemo(() => new Set(selectableKeys ?? reachable), [reachable, selectableKeys]);
  const configuredPlans = useMemo(() => new Set(configuredPlanKeys), [configuredPlanKeys]);
  const missingPlans = useMemo(() => new Set(missingPlanKeys), [missingPlanKeys]);
  const [nodes, edges] = useMemo(() => {
    const graphNodes: Node<SnapshotGraphNodeData>[] = (snapshot?.definition.nodes ?? []).map(item => {
      const visits = (executionRun?.node_runs ?? []).filter(nodeRun => nodeRun.flow_node_snapshot_key === item.instance_key);
      const latest = visits.at(-1);
      const latestAttempt = latest?.attempts.at(-1);
      const blocked = latestAttempt?.state === 'START_BLOCKED' || latestAttempt?.state === 'END_BLOCKED';
      const waiting = latestAttempt?.state === 'WAITING_HUMAN'
        || latestAttempt?.state === 'WAITING_CONFIRMATION'
        || latestAttempt?.state === 'WAITING_ACCEPTANCE';
      const status = neutralView ? 'neutral'
        : !reachable.has(item.instance_key) ? 'out-of-scope'
        : !selectable.has(item.instance_key) ? 'automatic-locked'
          : configuredPlans.has(item.instance_key) ? 'automatic-configured'
          : missingPlans.has(item.instance_key) ? 'automatic-missing'
          : !showExecutionState ? 'neutral'
            : latest?.state === 'ACTIVE' ? blocked ? 'failed' : waiting ? 'waiting' : 'current'
              : latest?.state === 'ACCEPTED' ? 'accepted'
                : latest?.state === 'CANCELLED' ? 'cancelled' : 'inactive';
      const stateLabel = showExecutionState
        ? status === 'current' ? (executionRun?.run_mode === 'AUTOMATIC' && latestAttempt ? ATTEMPT_STATE_LABELS[latestAttempt.state] : '当前激活')
          : status === 'waiting' || status === 'failed' ? (latestAttempt ? ATTEMPT_STATE_LABELS[latestAttempt.state] : undefined)
            : status === 'accepted' ? '已完成' : status === 'cancelled' ? '已取消' : undefined
        : undefined;
      return { id: item.instance_key, type: 'snapshotNode', selected: item.instance_key === selectedKey, selectable: selectable.has(item.instance_key), position: { x: item.position_x, y: item.position_y }, data: { label: item.alias || item.asset.name, status, stateLabel, visits: showExecutionState ? visits.length : 0, inputs: item.asset.inputs, outputs: item.asset.outputs } };
    });
    const directionEdges: Edge[] = (snapshot?.definition.edges ?? []).map((item, index) => ({ id: `flow-${item.id ?? index}`, source: item.source_instance_key, sourceHandle: 'flow-source', target: item.target_instance_key, targetHandle: 'flow-target', type: 'bezier', className: 'run-direction-edge' }));
    const mappingEdges = withMappingLabelOffsets((snapshot?.definition.port_mappings ?? []).map((item, index) => ({
      id: `mapping-${item.id ?? index}`,
      source: item.source_instance_key,
      sourceHandle: `output:${item.source_output_key}`,
      target: item.target_instance_key,
      targetHandle: `input:${item.target_input_key}`,
      type: 'bezier',
      className: 'run-mapping-edge',
      label: `${item.source_output_key} → ${item.target_input_key}`,
    })));
    const graphEdges = [
      ...directionEdges.map(edge => ({ ...edge, selectable: false, style: { opacity: linkMode === 'flow' ? 1 : 0.16 } })),
      ...mappingEdges.map(edge => ({ ...edge, selectable: false, style: { opacity: linkMode === 'data' ? 1 : 0.16 } })),
    ];
    return [graphNodes, graphEdges] as const;
  }, [configuredPlans, executionRun?.node_runs, executionRun?.run_mode, linkMode, missingPlans, neutralView, reachable, selectable, selectedKey, showExecutionState, snapshot]);
  const graphKey = `${GRAPH_RENDER_REVISION}:${snapshot?.id ?? 'snapshot'}:${snapshot?.definition_hash ?? ''}:${selectedKey ?? 'node-run'}`;
  const help = neutralView ? neutralHelp ?? '选择一条运行记录后查看执行状态。' : selectable.size < reachable.size ? '灰色节点需先完成上游配置；红色节点仍需补齐当前配置。' : missingPlans.size ? '红色节点尚未完成自动运行配置；点击节点可单独配置。' : showExecutionState ? '灰色节点尚未激活；实线表示流程走向，蓝线表示产物映射。' : neutralHelp ?? '选择一条运行记录后查看执行状态。';
  return <section className="run-graph" data-graph-render-revision={GRAPH_RENDER_REVISION}><header><div><h3>运行快照 v{snapshot?.version ?? '-'}</h3><small>{help}</small></div><div className="flow-link-mode run-link-mode" aria-label="运行图连线模式"><button type="button" className={linkMode === 'flow' ? 'active' : ''} aria-pressed={linkMode === 'flow'} onClick={() => setLinkMode('flow')}>流程走向</button><button type="button" className={linkMode === 'data' ? 'active' : ''} aria-pressed={linkMode === 'data'} onClick={() => setLinkMode('data')}>产物流转</button></div><span>定义 Hash {snapshot?.definition_hash.slice(0, 8)}</span></header><div className="run-graph-canvas"><ReactFlow key={graphKey} nodeTypes={runSnapshotNodeTypes} edgeTypes={flowMappingEdgeTypes} nodes={nodes} edges={edges} nodesDraggable={false} nodesConnectable={false} fitView onPaneClick={onClearSelection} onNodeClick={(_, node) => { if (selectable.has(node.id)) onSelect(node.id); }}><Background/><Controls showInteractive={false}/></ReactFlow></div></section>;
}
function GateList({ evaluations, policies = [] }: { evaluations: GateEvaluation[]; policies?: GatePolicy[] }) {
  const configured = [...policies].sort((left, right) => left.stage.localeCompare(right.stage) || left.position - right.position);
  const unconfiguredResults = evaluations.filter(item => !policies.some(policy => policy.id === item.policy_snapshot_key));
  if (!configured.length && !unconfiguredResults.length) return <div className="empty compact">本轮未配置门禁。</div>;
  return <div className="gate-results">{configured.map(policy => {
    const evaluation = evaluations.find(item => item.policy_snapshot_key === policy.id);
    const label = `${policy.stage === 'START' ? '启动' : '完成'}门禁 · #${policy.position + 1}`;
    return <div className="gate-result" key={policy.id ?? `${policy.stage}-${policy.position}`}><span><b>{label}</b><small>{evaluation ? String(evaluation.result.summary ?? '') : `${policy.gate_type === 'PYTHON' ? 'Python 脚本' : 'Prompt 判断'} · 等待执行`}</small></span>{evaluation ? <strong className={evaluation.decision === 'PASS' ? 'good' : 'bad'}>{evaluation.decision}</strong> : <strong className="pending">待执行</strong>}</div>;
  })}{unconfiguredResults.map(item => <div className="gate-result" key={item.id}><span><b>{item.stage} · #{item.policy_position + 1}</b><small>{String(item.result.summary ?? '')}</small></span><strong className={item.decision === 'PASS' ? 'good' : 'bad'}>{item.decision}</strong></div>)}</div>;
}

function ArtifactList({ artifacts, expectedFields = [] }: { artifacts: ArtifactVersion[]; expectedFields?: SnapshotFlowNode['asset']['outputs'] }) {
  const pendingFields = expectedFields.filter(field => !artifacts.some(artifact => artifact.field_key === field.field_key));
  return <section className="artifacts" aria-label="节点输出"><h3>节点输出</h3>{artifacts.map(item => <article key={item.id}><span className="artifact-version">v{item.version_no}</span><div><b>{item.field_key} · {item.artifact_type === 'FILE' ? '文件' : 'URL'}</b><small>{item.content_hash.slice(0, 10)} · {item.mime_type} · {item.byte_size} B</small><p>{item.uri || String(item.metadata?.filename || '')}</p><div className="artifact-actions">{item.uri ? <a className="ghost" href={item.uri} target="_blank" rel="noreferrer"><ExternalLink size={13}/>打开链接</a> : <><a className="ghost" href={artifactContentUrl(item.id)} target="_blank" rel="noreferrer"><FileText size={13}/>预览</a><a className="ghost" href={artifactContentUrl(item.id, true)}><Download size={13}/>下载</a></>}</div></div></article>)}{pendingFields.map(field => <article className="artifact-pending" key={field.field_key}><span className="artifact-version">—</span><div><b>{field.display_name || field.field_key} · {field.data_type}</b><small>{field.description || '节点定义的输出字段'}</small><p>等待本轮执行产出。</p></div></article>)}{!artifacts.length && !pendingFields.length && <div className="empty compact">该节点没有定义输出。</div>}</section>;
}

function artifactLabel(item: ArtifactVersion) {
  const name = typeof item.metadata?.display_name === 'string' && item.metadata.display_name.trim()
    ? item.metadata.display_name.trim()
    : item.field_key;
  return `${name} · v${item.version_no}`;
}

function artifactHref(item: ArtifactVersion, download = false) {
  return item.uri || artifactContentUrl(item.id, download);
}

async function waitForStartGate(runId: string, initial: NodeRun): Promise<NodeRun> {
  let current = initial;
  for (let poll = 0; poll < 60; poll += 1) {
    const attempt = current.attempts.at(-1);
    if (!attempt || !['WAITING_INPUT', 'START_GATES'].includes(attempt.state)) return current;
    await new Promise(resolve => window.setTimeout(resolve, 250));
    current = await api.nodeRun(runId, current.id);
  }
  return current;
}

type InputContract = SnapshotFlowNode['asset']['inputs'][number];

function InputSummary({ fields, bindings, artifacts }: { fields: InputContract[]; bindings: Record<string, string>; artifacts: ArtifactVersion[] }) {
  return <section className="input-summary" aria-label="节点输入"><h4>输入</h4>{fields.length ? fields.map(field => {
    const artifact = artifacts.find(item => item.id === bindings[field.field_key]);
    return <article key={field.field_key}><header><span><b>{field.display_name || field.field_key}</b><small>{field.description || '未填写字段说明'}</small></span><code>{field.field_key} · {field.data_type}</code></header>{artifact ? <><strong>{artifactLabel(artifact)}</strong><a href={artifactHref(artifact)} target="_blank" rel="noreferrer">{artifact.uri || String(artifact.metadata?.filename || '查看文件')}</a></> : <span className="input-summary-empty">尚未填写</span>}</article>;
  }) : <div className="empty compact">该节点无需输入。</div>}</section>;
}

type NodeInputResult = { bindings: Record<string, string>; artifacts: ArtifactVersion[] };

function mergeArtifacts(current: ArtifactVersion[], incoming: ArtifactVersion[]): ArtifactVersion[] {
  const replacements = new Map(incoming.map(item => [item.id, item]));
  return [...current.map(item => replacements.get(item.id) ?? item), ...incoming.filter(item => !current.some(existing => existing.id === item.id))];
}

function NodeInputDialog({ run, node, initialBindings = {}, onClose, onSubmit }: { run: FlowRun; node: SnapshotFlowNode; initialBindings?: Record<string, string>; onClose: () => void; onSubmit: (result: NodeInputResult) => void }) {
  useEscapeClose(onClose);
  const fields = node.asset.inputs;
  const [urls, setUrls] = useState<Record<string, string>>(() => Object.fromEntries(fields.filter(field => field.data_type === 'URL').map(field => {
    const artifact = run.artifacts.find(item => item.id === initialBindings[field.field_key]);
    return [field.field_key, artifact?.uri ?? ''];
  })));
  const [files, setFiles] = useState<Record<string, File | undefined>>({});
  const [error, setError] = useState('');
  const mutation = useMutation({
    mutationFn: async () => {
      const bindings: Record<string, string> = {};
      const artifacts: ArtifactVersion[] = [];
      for (const field of fields) {
        const retained = initialBindings[field.field_key];
        if (field.data_type === 'URL') {
          const value = urls[field.field_key]?.trim();
          const current = run.artifacts.find(item => item.id === retained);
          if (retained && current?.uri === value) { bindings[field.field_key] = retained; continue; }
          if (!value && retained) { bindings[field.field_key] = retained; continue; }
          if (!value) throw new Error(`请填写“${field.display_name || field.field_key}”。`);
          const artifact = await api.addNodeInputArtifact(run.id, node.instance_key, {
            field_key: field.field_key, artifact_type: 'URL', uri: value, mime_type: 'text/uri-list',
            metadata: { display_name: field.display_name || field.field_key },
          });
          bindings[field.field_key] = artifact.id;
          artifacts.push(artifact);
          continue;
        }
        const file = files[field.field_key];
        if (!file && retained) { bindings[field.field_key] = retained; continue; }
        if (!file) throw new Error(`请上传“${field.display_name || field.field_key}”。`);
        const artifact = await api.uploadNodeInputArtifact(run.id, node.instance_key, field.field_key, field.display_name || field.field_key, file);
        bindings[field.field_key] = artifact.id;
        artifacts.push(artifact);
      }
      return { bindings, artifacts };
    },
    onSuccess: onSubmit,
    onError: reason => setError(reason instanceof Error ? reason.message : '保存输入失败'),
  });
  return <div className="modal-backdrop"><section className="modal node-input-dialog" role="dialog" aria-modal="true" aria-label="填写节点输入"><header><div><span className="eyebrow">NODE INPUTS</span><h2>填写 {node.alias || node.asset.name} 的输入</h2><p>字段由节点定义冻结生成；每个值只绑定当前节点，不会进入可复用产物池。</p></div><button type="button" className="ghost" aria-label="关闭输入表单" onClick={onClose}><X size={17}/></button></header><div className="node-input-dialog-body">{fields.map(field => {
    const current = run.artifacts.find(item => item.id === initialBindings[field.field_key]);
    return <label className="node-input-control" key={field.field_key}><span><b>{field.display_name || field.field_key}</b><code>{field.field_key} · {field.data_type}</code><small>{field.description || '请按节点定义填写此字段。'}</small></span>{field.data_type === 'URL' ? <input aria-label={`填写输入 ${field.field_key}`} type="url" value={urls[field.field_key] ?? ''} onChange={event => setUrls(old => ({ ...old, [field.field_key]: event.target.value }))} placeholder="https://example.com/resource"/> : <span className="platform-file-upload"><Upload size={16}/><span><b>{files[field.field_key]?.name || (typeof current?.metadata?.filename === 'string' ? current.metadata.filename : '选择文件')}</b><small>{files[field.field_key] ? `${files[field.field_key]?.size} B` : '图片、PDF、文档、压缩包等，最大 25 MiB'}</small></span><input aria-label={`上传输入文件 ${field.field_key}`} type="file" onChange={event => setFiles(old => ({ ...old, [field.field_key]: event.target.files?.[0] }))}/></span>}{current && <a className="current-input" href={artifactHref(current)} target="_blank" rel="noreferrer">当前绑定：{artifactLabel(current)}</a>}</label>; })}{!fields.length && <div className="empty compact">该节点没有定义输入，可直接创建执行。</div>}</div>{error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? '保存输入中…' : '保存输入并继续'}</button></footer></section></div>;
}

function TerminalRunDelete({ run, onDeleted }: { run: FlowRun; onDeleted: () => void }) {
  const dialog = useProductDialog();
  const mutation = useMutation({ mutationFn: () => api.deleteRun(run.id), onSuccess: onDeleted });
  return <div className="flow-run-management" aria-label="流程运行态管理"><button className="danger" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '永久删除这个流程运行？', message: '相关记录都会被清理且不可恢复。', confirmLabel: '永久删除', tone: 'danger' }).then(ok => ok && mutation.mutate())}><Trash2 size={14}/>{mutation.isPending ? '删除中…' : '永久删除此运行'}</button>{mutation.error && <p className="error">{mutation.error.message}</p>}</div>;
}

type GateDraftDialog = { index?: number; gate: GatePolicy; readOnly: boolean };

const gateText = (gate: GatePolicy) => String(gate.config.prompt || gate.config.code || '');
const emptyGateAgentPreset = (): GateAgentPreset => ({});

type LaunchMenuOption = { value: string; label: string };

function LaunchOptionMenu({ label, value, options, disabled = false, onChange }: { label: string; value: string; options: LaunchMenuOption[]; disabled?: boolean; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  useEscapeClose(() => setOpen(false), open);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (event.target instanceof Node && !host.current?.contains(event.target)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  const selected = options.find(item => item.value === value)?.label ?? options[0]?.label ?? '未选择';
  return <div ref={host} className="launch-option-menu"><button type="button" aria-label={label} aria-expanded={open} disabled={disabled} onClick={() => setOpen(current => !current)}><span>{selected}</span><ChevronDown size={15}/></button>{open && <div className="launch-option-popover" role="listbox" aria-label={label}>{options.map(item => <button type="button" role="option" aria-selected={item.value === value} key={item.value} onClick={() => { onChange(item.value); setOpen(false); }}><span>{item.label}</span>{item.value === value && <Check size={14}/>}</button>)}</div>}</div>;
}

function ModelPresetFields({ preset, onChange, readOnly = false, label = 'Agent' }: { preset: GateAgentPreset; onChange: (preset: GateAgentPreset) => void; readOnly?: boolean; label?: string }) {
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const selectedProvider = providers.data?.find(item => item.id === preset.model_provider_id);
  const models = selectedProvider?.models.filter(item => item.enabled) ?? [];
  const update = (patch: Partial<GateAgentPreset>) => onChange({ ...preset, ...patch });
  const providerOptions = [{ value: '', label: '使用工作区默认模型' }, ...(providers.data ?? []).filter(item => item.available_for_nodes).map(item => ({ value: item.id, label: item.name }))];
  const modelOptions = [{ value: '', label: '默认模型' }, ...models.map(item => ({ value: item.model_name, label: item.model_name }))];
  const effortOptions = [{ value: '', label: '默认' }, ...(models.find(item => item.model_name === preset.model_name)?.supported_reasoning_efforts ?? []).map(value => ({ value, label: value }))];
  return <div className="agent-model-fields"><label>模型供应商<LaunchOptionMenu label={`${label}模型供应商`} value={preset.model_provider_id ?? ''} options={providerOptions} disabled={readOnly} onChange={value => { const provider = providers.data?.find(item => item.id === value); const model = provider?.models.find(item => item.enabled && item.is_default) ?? provider?.models.find(item => item.enabled); update({ model_provider_id: provider?.id ?? null, model_name: model?.model_name ?? null, reasoning_effort: model?.default_reasoning_effort ?? null }); }}/></label><label>模型<LaunchOptionMenu label={`${label}模型`} value={preset.model_name ?? ''} options={modelOptions} disabled={readOnly || !selectedProvider} onChange={value => { const model = models.find(item => item.model_name === value); update({ model_name: model?.model_name ?? null, reasoning_effort: model?.default_reasoning_effort ?? null }); }}/></label><label>思考程度<LaunchOptionMenu label={`${label}思考程度`} value={preset.reasoning_effort ?? ''} options={effortOptions} disabled={readOnly || !selectedProvider} onChange={value => update({ reasoning_effort: value || null })}/></label></div>;
}

function GateContentDialog({ kind, initial, readOnly, onClose, onSave }: { kind: 'prompt' | 'python'; initial: string; readOnly: boolean; onClose: () => void; onSave: (content: string, filename?: string) => void }) {
  const [content, setContent] = useState(initial);
  const [error, setError] = useState('');
  const label = kind === 'prompt' ? '判定提示词' : '可选 Python 脚本';
  const importFile = async (file?: File) => {
    if (!file) return;
    const valid = kind === 'prompt' ? /\.(md|txt)$/i.test(file.name) : /\.py$/i.test(file.name);
    if (!valid) { setError(kind === 'prompt' ? '判定提示词仅支持导入 .md 或 .txt 文件。' : 'Python 脚本仅支持导入 .py 文件。'); return; }
    onSave(await file.text(), file.name);
    onClose();
  };
  useEscapeClose(onClose);
  return <div className="modal-backdrop"><section className="modal gate-content-dialog" role="dialog" aria-modal="true" aria-label={`编辑${label}`}><header><div><span className="eyebrow">GATE CONTENT</span><h2>{readOnly ? `查看${label}` : `编辑${label}`}</h2></div><button type="button" className="ghost" onClick={onClose}><X size={17}/></button></header><label>{label}<textarea aria-label={label} className={kind === 'python' ? 'code' : ''} readOnly={readOnly} value={content} onChange={event => setContent(event.target.value)}/></label>{!readOnly && <label className="gate-file-import"><span><Upload size={15}/>从文件导入</span><small>{kind === 'prompt' ? '支持 .md 或 .txt 文件' : '仅支持 .py 文件'}</small><input aria-label={`导入${label}`} type="file" accept={kind === 'prompt' ? '.md,.txt,text/markdown,text/plain' : '.py,text/x-python'} onChange={event => void importFile(event.target.files?.[0])}/></label>}{error && <p className="error">{error}</p>}<footer>{!readOnly && <button type="button" className="ghost" onClick={onClose}>取消</button>}<button type="button" className="primary" onClick={() => { if (!readOnly) onSave(content); onClose(); }}>{readOnly ? '完成' : '保存'}</button></footer></section></div>;
}

function GateDraftDialog({ draft, onClose, onSave }: { draft: GateDraftDialog; onClose: () => void; onSave: (gate: GatePolicy) => void }) {
  const [gate, setGate] = useState(draft.gate);
  const [editor, setEditor] = useState<'prompt' | 'python' | 'model'>();
  const [error, setError] = useState('');
  useEscapeClose(() => { if (editor) setEditor(undefined); else onClose(); });
  const editable = !draft.readOnly;
  const save = () => { if (!String(gate.config.prompt || '').trim()) { setError('请填写判定提示词。'); return; } onSave({ ...gate, timeout_seconds: 300 }); };
  const gatePreset = gate.agent_preset ?? emptyGateAgentPreset();
  const summary = (text: unknown, empty: string) => String(text || '').trim().replace(/\s+/g, ' ').slice(0, 64) || empty;
  return <div className="modal-backdrop"><section className="modal gate-draft-dialog" role="dialog" aria-modal="true" aria-label="配置门禁"><header><div><span className="eyebrow">{gate.stage === 'START' ? 'START GATE' : 'END GATE'}</span><h2>配置门禁</h2></div><button type="button" className="ghost" aria-label="关闭门禁弹窗" onClick={onClose}><X size={17}/></button></header><div className="gate-config-lines"><article><span><b>判定提示词</b><small>{summary(gate.config.prompt, '尚未填写')}</small></span><button type="button" className="secondary" onClick={() => setEditor('prompt')}>{editable ? '编辑' : '查看'}</button></article><article><span><b>可选 Python 脚本</b><small>{summary(gate.config.code, '未配置')}</small></span><button type="button" className="secondary" onClick={() => setEditor('python')}>{String(gate.config.code || '').trim() ? (editable ? '编辑' : '查看') : '添加'}</button></article><article><span><b>配置大模型</b><small>{gatePreset.model_name || (gatePreset.model_provider_id ? '默认模型' : '使用工作区默认模型')}{gatePreset.reasoning_effort ? ` · ${gatePreset.reasoning_effort}` : ''}</small></span><button type="button" className="secondary" onClick={() => setEditor('model')}>{editable ? '配置' : '查看'}</button></article></div>{error && <p className="error">{error}</p>}<footer>{editable ? <><button type="button" className="ghost" onClick={onClose}>取消</button><button type="button" className="primary" onClick={save}>保存门禁</button></> : <button type="button" className="primary" onClick={onClose}>完成</button>}</footer></section>{editor === 'prompt' && <GateContentDialog kind="prompt" initial={String(gate.config.prompt || '')} readOnly={!editable} onClose={() => setEditor(undefined)} onSave={(prompt, filename) => { setGate(current => ({ ...current, config: { ...current.config, prompt, ...(filename ? { source_filename: filename } : {}) } })); setError(''); }}/>} {editor === 'python' && <GateContentDialog kind="python" initial={String(gate.config.code || '')} readOnly={!editable} onClose={() => setEditor(undefined)} onSave={(code, filename) => setGate(current => ({ ...current, config: { ...current.config, code, ...(filename ? { script_filename: filename } : {}) } }))}/>} {editor === 'model' && <div className="modal-backdrop"><section className="modal agent-preset-model-dialog" role="dialog" aria-modal="true" aria-label="配置大模型"><header><div><span className="eyebrow">GATE MODEL</span><h2>配置大模型</h2><p>门禁独立运行，不继承主 Agent 的能力或上下文。</p></div><button type="button" className="ghost" onClick={() => setEditor(undefined)}><X size={17}/></button></header><ModelPresetFields label="门禁" preset={gatePreset} readOnly={!editable} onChange={agent_preset => setGate(current => ({ ...current, agent_preset }))}/><footer><button type="button" className="primary" onClick={() => setEditor(undefined)}>完成</button></footer></section></div>}</div>;
}

function GateDraftEditor({ gates, onChange }: { gates: GatePolicy[]; onChange: (gates: GatePolicy[]) => void }) {
  const [dialog, setDialog] = useState<GateDraftDialog>();
  useEscapeClose(() => setDialog(undefined), Boolean(dialog));
  const renumber = (items: GatePolicy[]) => items.map((gate, index, all) => ({ ...gate, position: all.slice(0, index).filter(candidate => candidate.stage === gate.stage).length }));
  const openNew = (stage: 'START' | 'END') => setDialog({ gate: { stage, position: gates.filter(gate => gate.stage === stage).length, gate_type: 'PROMPT', enabled: true, timeout_seconds: 300, config: { prompt: '', code: '' }, agent_preset: emptyGateAgentPreset() }, readOnly: false });
  const save = (gate: GatePolicy) => {
    onChange(dialog?.index === undefined ? [...gates, gate] : gates.map((item, index) => index === dialog.index ? gate : item));
    setDialog(undefined);
  };
  const remove = (index: number) => onChange(renumber(gates.filter((_, itemIndex) => itemIndex !== index)));
  return <section className="attempt-side-section gate-draft-editor"><p className="field-hint">门禁只应用于即将创建的这一次执行；创建后将冻结为本轮的门禁结果依据。</p>{(['START', 'END'] as const).map(stage => <section key={stage} className="gate-draft-stage"><header><h4>{stage === 'START' ? '开始门禁' : '结束门禁'}</h4><button type="button" className="secondary" onClick={() => openNew(stage)}><Plus size={13}/>添加门禁</button></header><div className="gate-draft-list">{gates.filter(gate => gate.stage === stage).map(gate => {
    const index = gates.indexOf(gate); const content = gateText(gate).trim();
    return <article key={`${stage}-${gate.position}`}><button type="button" className="gate-draft-open" onClick={() => setDialog({ index, gate, readOnly: true })}><span><b>门禁判定</b><small>{content ? content.replace(/\s+/g, ' ').slice(0, 72) : '尚未填写判定提示词'}</small></span><Eye size={14}/></button><div><button type="button" className="ghost" onClick={() => setDialog({ index, gate: gate.agent_preset ? gate : { ...gate, agent_preset: emptyGateAgentPreset() }, readOnly: false })}>编辑</button><button type="button" className="ghost gate-delete" onClick={() => remove(index)}><Trash2 size={14}/>删除</button></div></article>;
  })}</div></section>)}{dialog && <GateDraftDialog key={`${dialog.index ?? 'new'}-${dialog.gate.stage}-${dialog.readOnly}`} draft={dialog} onClose={() => setDialog(undefined)} onSave={save}/>}</section>;
}

const MANUAL_NODE_CONTEXT_ID = '__node_context_prompt__';

type NodeContextItem = { id: string; title: string; meta: string; text: string; source: 'NODE' | 'REPOSITORY' };

function nodeContextItems(node: SnapshotFlowNode): NodeContextItem[] {
  const manual = node.asset.executor?.context_prompt?.trim();
  return [
    ...(manual ? [{ id: MANUAL_NODE_CONTEXT_ID, title: '专属上下文', meta: '节点专属 · 自由文本 Context', text: manual, source: 'NODE' as const }] : []),
    ...node.asset.context_capabilities.map(item => ({
      id: item.id,
      title: item.capability_key,
      meta: `Context 管理 · ${item.digest.slice(0, 12)}`,
      text: item.text,
      source: 'REPOSITORY' as const,
    })),
  ];
}

function NodeContextSummary({ node, contextIds, editable = false, onChange, mode }: {
  node: SnapshotFlowNode; contextIds: string[] | null; editable?: boolean;
  onChange?: (ids: string[]) => void; mode?: 'PROMPT' | 'CHAT';
}) {
  const [viewing, setViewing] = useState<NodeContextItem>();
  useEscapeClose(() => setViewing(undefined), Boolean(viewing));
  const items = nodeContextItems(node);
  const selected = contextIds === null ? new Set(items.map(item => item.id)) : new Set(contextIds);
  const visible = editable ? items : items.filter(item => selected.has(item.id));
  const toggle = (id: string) => {
    if (!onChange) return;
    onChange(selected.has(id) ? [...selected].filter(item => item !== id) : [...selected, id]);
  };
  const empty = mode === 'CHAT'
    ? '仅创建会话不会应用节点上下文；进入会话后由你自行决定是否补充上下文。'
    : items.length === 0
      ? '该节点没有可用的 Context。'
      : '本次未选择节点上下文。';
  return <section className="attempt-side-section node-context-summary"><header><div><h4>节点上下文</h4><small>{editable ? '可多选；创建后会冻结为本次运行上下文。' : contextIds === null ? '历史执行按当时节点定义的全部 Context 展示。' : '仅展示本次运行实际冻结的 Context。'}</small></div>{editable && <span className="context-selection-count">已选 {selected.size}</span>}</header>{visible.length ? <div className="node-context-list">{visible.map(item => <article key={item.id} className={`${item.source === 'NODE' ? 'node-context-owned' : 'node-context-repository'}${editable && selected.has(item.id) ? ' selected' : ''}`}>{editable ? <button type="button" className="node-context-toggle" aria-pressed={selected.has(item.id)} onClick={() => toggle(item.id)}><i className="context-checkbox" aria-hidden="true">{selected.has(item.id) ? '✓' : ''}</i><span><b>{item.title}</b><small>{item.meta}</small></span></button> : <span className="node-context-label"><b>{item.title}</b><small>{item.meta}</small></span>}<button type="button" className="ghost context-detail-button" aria-label={`查看 ${item.title}`} onClick={() => setViewing(item)}><Eye size={14}/>查看</button></article>)}</div> : <p className="field-hint">{empty}</p>}{viewing && <div className="modal-backdrop"><section className="modal context-preview-dialog" role="dialog" aria-modal="true" aria-label={`查看 Context ${viewing.title}`}><header><div><span className="eyebrow">FROZEN CONTEXT</span><h2>{viewing.title}</h2></div><button className="ghost" onClick={() => setViewing(undefined)}><X size={15}/>关闭</button></header><p>{viewing.meta}</p><pre>{viewing.text}</pre><footer><button className="primary" onClick={() => setViewing(undefined)}>完成</button></footer></section></div>}</section>;
}

function CapabilityPresetDialog({ selectedIds, onClose, onSave }: { selectedIds: string[]; onClose: () => void; onSave: (ids: string[]) => void }) {
  const catalog = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const collections = useQuery({ queryKey: ['capability-collections'], queryFn: api.capabilityCollections });
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<'ALL' | CapabilityAsset['capability_type']>('ALL');
  const [draft, setDraft] = useState(selectedIds);
  useEscapeClose(onClose);
  const capabilities = useMemo(() => (catalog.data ?? []).filter(item => ['SKILL', 'MCP', 'PLUGIN', 'CONTEXT'].includes(item.capability_type) && item.is_latest), [catalog.data]);
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return capabilities.filter(item => (kind === 'ALL' || item.capability_type === kind) && (!needle || `${item.capability_key} ${item.description} ${item.filename}`.toLocaleLowerCase().includes(needle)));
  }, [capabilities, kind, query]);
  const toggle = (item: CapabilityAsset) => setDraft(current => {
    if (current.includes(item.id)) return current.filter(id => id !== item.id);
    const sameCapability = current.filter(id => {
      const existing = capabilities.find(candidate => candidate.id === id);
      return existing?.capability_type === item.capability_type && existing.capability_key === item.capability_key;
    });
    return [...current.filter(id => !sameCapability.includes(id)), item.id].slice(0, 30);
  });
  const toggleCollection = (collection: CapabilityCollection) => setDraft(current => {
    const members = collection.members.filter(item => capabilities.some(candidate => candidate.id === item.id));
    const ids = members.map(item => item.id);
    if (!ids.length) return current;
    if (ids.every(id => current.includes(id))) return current.filter(id => !ids.includes(id));
    return members.reduce((next, item) => {
      if (next.includes(item.id)) return next;
      const sameCapability = next.filter(id => {
        const existing = capabilities.find(candidate => candidate.id === id);
        return existing?.capability_type === item.capability_type && existing.capability_key === item.capability_key;
      });
      return [...next.filter(id => !sameCapability.includes(id)), item.id].slice(0, 30);
    }, current);
  });
  return <div className="modal-backdrop"><section className="modal agent-preset-capability-dialog" role="dialog" aria-modal="true" aria-label="配置能力"><header><div><span className="eyebrow">LAUNCH CAPABILITIES</span><h2>配置能力</h2><p>选择这次自动启动首个会话可用的能力。</p></div><button type="button" className="ghost" onClick={onClose}><X size={17}/></button></header><div className="agent-capability-toolbar"><nav className="agent-capability-tabs" aria-label="能力类型">{(['ALL', 'SKILL', 'MCP', 'PLUGIN', 'CONTEXT'] as const).map(value => <button type="button" key={value} className={kind === value ? 'active' : ''} onClick={() => setKind(value)}>{value === 'ALL' ? '全部' : value === 'SKILL' ? '技能' : value === 'MCP' ? 'MCP' : value === 'PLUGIN' ? '插件' : '上下文'}</button>)}</nav><label className="agent-capability-search"><input aria-label="搜索能力" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索能力"/></label></div>{(kind === 'ALL' || kind === 'SKILL') && collections.data?.length ? <section className="agent-capability-collections"><span>Skill 组合</span><div>{collections.data.map(collection => { const ids = collection.members.map(item => item.id).filter(id => capabilities.some(item => item.id === id)); const selected = ids.length > 0 && ids.every(id => draft.includes(id)); return <button type="button" key={collection.id} className={selected ? 'selected' : ''} onClick={() => toggleCollection(collection)}><Layers3 size={13}/><b>{collection.name}</b><em>{ids.length}</em>{selected && <Check size={13}/>}</button>; })}</div></section> : null}<div className="agent-capability-summary"><span>已选 <b>{draft.length}</b> 项</span><span>仅加载已发布的最新版本</span></div><div className="agent-capability-list">{catalog.isLoading ? <p>正在读取能力目录…</p> : visible.length ? visible.map(item => <button type="button" key={item.id} className={draft.includes(item.id) ? 'selected' : ''} aria-pressed={draft.includes(item.id)} onClick={() => toggle(item)}><span className={`agent-capability-icon ${item.capability_type.toLowerCase()}`}>{item.capability_type.slice(0, 1)}</span><span><b>{item.capability_key}</b><small>{item.description || item.filename || '未填写说明'}</small><em>{item.capability_type}</em></span><i aria-hidden="true">{draft.includes(item.id) ? '✓' : ''}</i></button>) : <p>没有匹配的能力。</p>}</div><footer><button type="button" className="ghost" onClick={onClose}>取消</button><button type="button" className="primary" onClick={() => onSave(draft)}>保存能力</button></footer></section></div>;
}

function AgentPresetEditor({ preset, nodeContext, onChange }: { preset: AgentPreset; nodeContext: string; onChange: (preset: AgentPreset) => void }) {
  const catalog = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const [dialog, setDialog] = useState<'capabilities' | 'model' | 'context'>();
  useEscapeClose(() => setDialog(undefined), Boolean(dialog));
  const selectedCapabilities = (catalog.data ?? []).filter(item => preset.capability_version_ids.includes(item.id));
  const provider = providers.data?.find(item => item.id === preset.model_provider_id);
  const modelSummary = provider ? `${provider.name} · ${preset.model_name || '默认模型'}` : '使用工作区默认模型';
  const modelPreset: GateAgentPreset = { model_provider_id: preset.model_provider_id, model_name: preset.model_name, reasoning_effort: preset.reasoning_effort };
  const updateModel = (next: GateAgentPreset) => onChange({ ...preset, ...next });
  const contextText = preset.node_context_prompt ?? nodeContext;
  return <section className="attempt-side-section agent-preset-editor"><header><div><h4>首会话 Agent 配置</h4><small>只在本次自动启动时冻结；进入会话后新建的会话不继承这些预设。</small></div></header><div className="agent-preset-modules"><button type="button" className="agent-preset-module" onClick={() => setDialog('capabilities')}><span className="agent-preset-module-icon"><Boxes size={17}/></span><span><b>能力</b><small>{selectedCapabilities.length ? `已选 ${selectedCapabilities.length} 项 · ${selectedCapabilities.slice(0, 2).map(item => item.capability_key).join('、')}${selectedCapabilities.length > 2 ? '…' : ''}` : '未配置额外能力'}</small></span><em>配置</em></button><button type="button" className="agent-preset-module" onClick={() => setDialog('model')}><span className="agent-preset-module-icon"><Bot size={17}/></span><span><b>大模型</b><small>{modelSummary}{preset.reasoning_effort ? ` · ${preset.reasoning_effort}` : ''}</small></span><em>配置</em></button><article className="agent-preset-module context-module"><span className="agent-preset-module-icon context"><FileText size={17}/></span><span><b>大模型专属上下文</b><small>{preset.node_context_enabled ? (contextText.trim() ? '已启用，可在本次运行中临时修改' : '已启用，但尚未填写内容') : '未启用'}</small></span><div><button type="button" className="secondary" onClick={() => setDialog('context')}>配置</button><button type="button" className={preset.node_context_enabled ? 'ghost' : 'primary'} onClick={() => onChange({ ...preset, node_context_enabled: !preset.node_context_enabled })}>{preset.node_context_enabled ? '停用' : '启用'}</button></div></article></div>{dialog === 'capabilities' && <CapabilityPresetDialog selectedIds={preset.capability_version_ids} onClose={() => setDialog(undefined)} onSave={capability_version_ids => { onChange({ ...preset, capability_version_ids }); setDialog(undefined); }}/>} {dialog === 'model' && <div className="modal-backdrop"><section className="modal agent-preset-model-dialog" role="dialog" aria-modal="true" aria-label="配置大模型"><header><div><span className="eyebrow">LAUNCH MODEL</span><h2>配置大模型</h2><p>用于本次自动启动的首个主 Agent 会话。</p></div><button type="button" className="ghost" onClick={() => setDialog(undefined)}><X size={17}/></button></header><ModelPresetFields label="首会话" preset={modelPreset} onChange={updateModel}/><footer><button type="button" className="primary" onClick={() => setDialog(undefined)}>完成</button></footer></section></div>} {dialog === 'context' && <div className="modal-backdrop"><section className="modal agent-preset-context-dialog" role="dialog" aria-modal="true" aria-label="配置大模型专属上下文"><header><div><span className="eyebrow">NODE CONTEXT</span><h2>配置大模型专属上下文</h2><p>仅修改本次启动预设，不会改动节点定义。</p></div><button type="button" className="ghost" onClick={() => setDialog(undefined)}><X size={17}/></button></header><pre className="agent-context-preview">{contextText.trim() || '尚未填写专属上下文。'}</pre><label className="agent-context-editor">专属上下文<textarea aria-label="大模型专属上下文" value={contextText} onChange={event => onChange({ ...preset, node_context_prompt: event.target.value })} placeholder="输入本次运行的专属上下文"/></label><footer><button type="button" className="primary" onClick={() => setDialog(undefined)}>保存</button></footer></section></div>}</section>;
}

type PromptConfigurationTab = 'inputs' | 'agent' | 'gates' | 'history';

function PromptConfigurationTabs({ active, onChange }: { active: PromptConfigurationTab; onChange: (tab: PromptConfigurationTab) => void }) {
  return <nav className="attempt-detail-tabs node-console-subtabs" aria-label="提示词执行配置"><button className={active === 'inputs' ? 'active' : ''} onClick={() => onChange('inputs')}>输入与上下文</button><button className={active === 'agent' ? 'active' : ''} onClick={() => onChange('agent')}>Agent 配置</button><button className={active === 'gates' ? 'active' : ''} onClick={() => onChange('gates')}>门禁配置</button><button className={active === 'history' ? 'active' : ''} onClick={() => onChange('history')}>执行记录</button></nav>;
}

function StartupPromptSummary({ prompt, freezeHint, editable = true, onEdit }: { prompt: string; freezeHint: string; editable?: boolean; onEdit: () => void }) {
  return <section className="attempt-side-section startup-prompt-summary"><header><div><h4>启动提示词</h4><small>{freezeHint}</small></div></header><p title={prompt.trim() || undefined}>{prompt.trim() || '尚未填写启动提示词。'}</p>{editable && <footer><button type="button" className="secondary" onClick={onEdit}>编辑</button></footer>}</section>;
}

function StartupPromptDialog({ prompt, label = '节点启动提示词', onChange, onClose }: { prompt: string; label?: string; onChange: (prompt: string) => void; onClose: () => void }) {
  useEscapeClose(onClose);
  return <div className="modal-backdrop"><section className="modal startup-prompt-dialog" role="dialog" aria-modal="true" aria-label="编辑启动提示词"><header><div><span className="eyebrow">STARTUP PROMPT</span><h2>编辑启动提示词</h2></div><button type="button" className="ghost" aria-label="关闭启动提示词编辑" onClick={onClose}><X size={17}/></button></header><label>启动提示词<textarea aria-label={label} value={prompt} onChange={event => onChange(event.target.value)} placeholder="输入发送给 AI 的启动提示词"/></label><footer><button type="button" className="ghost" onClick={onClose}>取消</button><button type="button" className="primary" onClick={onClose}>完成</button></footer></section></div>;
}

function NodeExecutionHistory({ run, node, onSelectExecution }: { run: FlowRun; node: SnapshotFlowNode; onSelectExecution?: (nodeRun: NodeRun) => void }) {
  const nodeRuns = run.node_runs.filter(item => item.flow_node_snapshot_key === node.instance_key);
  return <section className="node-execution-history"><header><h4>执行记录</h4><small>可随时查看，且不影响再次配置</small></header>{nodeRuns.length ? nodeRuns.map(item => <button type="button" key={item.id} disabled={!onSelectExecution} onClick={() => onSelectExecution?.(item)}><span><b>第 {nodeVisitNumber(run, item)} 次执行</b><small>{item.attempts.length} 个轮次 · {attemptStateLabel(item)}</small></span><ExternalLink size={13}/></button>) : <p className="field-hint">还没有执行记录。</p>}</section>;
}

type NodeConfigurationPanelProps = {
  title: string;
  subtitle: string;
  mode: 'PROMPT' | 'CHAT';
  onModeChange: (mode: 'PROMPT' | 'CHAT') => void;
  promptTab: PromptConfigurationTab;
  onPromptTabChange: (tab: PromptConfigurationTab) => void;
  promptContent: ReactNode;
  agentContent: ReactNode;
  gateContent: ReactNode;
  historyContent: ReactNode;
  chatContent: ReactNode;
  action?: ReactNode;
  feedback?: ReactNode;
  belowContent?: ReactNode;
  chatEnabled?: boolean;
  className?: string;
};

/**
 * The manual-node and automatic-run editors intentionally share one panel
 * shell.  Their persistence semantics differ, but the visible configuration
 * navigation, hierarchy, and scrolling contract must never drift apart.
 */
function NodeConfigurationPanel({
  title,
  subtitle,
  mode,
  onModeChange,
  promptTab,
  onPromptTabChange,
  promptContent,
  agentContent,
  gateContent,
  historyContent,
  chatContent,
  action,
  feedback,
  belowContent,
  chatEnabled = true,
  className,
}: NodeConfigurationPanelProps) {
  const panelClassName = ['action-panel', 'node-console', className].filter(Boolean).join(' ');
  const chatDisabledTitle = chatEnabled ? undefined : '自动运行记录只支持提示词执行';
  return <aside className={panelClassName} data-testid="node-configuration-panel"><header><div><b>{title}</b><small>{subtitle}</small></div></header>{feedback}<div className="node-console-mode-bar"><nav className="node-console-mode-tabs" aria-label="启动方式"><button className={mode === 'PROMPT' ? 'active' : ''} aria-pressed={mode === 'PROMPT'} onClick={() => onModeChange('PROMPT')}><span>提示词执行</span><small>按节点配置自动执行</small></button><button className={mode === 'CHAT' ? 'active' : ''} aria-pressed={mode === 'CHAT'} disabled={!chatEnabled} title={chatDisabledTitle} onClick={() => onModeChange('CHAT')}><span>会话启动</span><small>人工进入会话引导</small></button></nav>{action}</div>{mode === 'PROMPT' && <PromptConfigurationTabs active={promptTab} onChange={onPromptTabChange}/>}<div className="action-content">{mode === 'PROMPT' ? <>{promptTab === 'inputs' && promptContent}{promptTab === 'agent' && agentContent}{promptTab === 'gates' && gateContent}{promptTab === 'history' && historyContent}</> : chatContent}{belowContent}</div></aside>;
}

function NodeConsole({ run, node, refresh, onActivated, onSelectExecution }: { run: FlowRun; node: SnapshotFlowNode; refresh: () => void; onActivated: (nodeRun: NodeRun) => void; onSelectExecution: (nodeRun: NodeRun) => void }) {
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const [topTab, setTopTab] = useState<'PROMPT' | 'CHAT'>('PROMPT');
  const [promptTab, setPromptTab] = useState<PromptConfigurationTab>('inputs');
  const [inputDialogOpen, setInputDialogOpen] = useState(false);
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [inputArtifacts, setInputArtifacts] = useState<ArtifactVersion[]>(run.artifacts);
  const [gates, setGates] = useState<GatePolicy[]>([]);
  const [agentPreset, setAgentPreset] = useState<AgentPreset>({ capability_version_ids: [], node_context_enabled: false, node_context_prompt: node.asset.executor?.context_prompt ?? '' });
  const [mode, setMode] = useState<'PROMPT' | 'CHAT'>('PROMPT');
  const [prompt, setPrompt] = useState(node.asset.executor?.startup_prompt ?? '');
  useEffect(() => { setTopTab('PROMPT'); setPromptTab('inputs'); setInputDialogOpen(false); setPromptDialogOpen(false); setBindings({}); setGates([]); setAgentPreset({ capability_version_ids: [], node_context_enabled: false, node_context_prompt: node.asset.executor?.context_prompt ?? '' }); setMode('PROMPT'); setPrompt(node.asset.executor?.startup_prompt ?? ''); }, [node.instance_key, node.asset.executor?.startup_prompt, node.asset.executor?.context_prompt]);
  useEffect(() => setInputArtifacts(current => mergeArtifacts(current, run.artifacts)), [run.artifacts]);
  const mutation = useMutation({
    mutationFn: async (nextBindings: Record<string, string>) => {
      const sessionOnly = mode === 'CHAT';
      const activated = await api.activateNode(
        run.id,
        node.instance_key,
        sessionOnly ? {} : nextBindings,
        sessionOnly ? [] : gates,
        {},
        mode,
        sessionOnly ? undefined : agentPreset,
      );
      if (sessionOnly) return { created: activated, openDraft: true };
      const created = await waitForStartGate(run.id, activated);
      const attempt = created.attempts.at(-1);
      if (!attempt || attempt.state !== 'WAITING_START_CONFIRMATION') return { created };
      const started = await api.confirmStart(attempt.id, attempt.state_version, { startup_mode: 'PROMPT', prompt });
      return { created: { ...created, attempts: created.attempts.map(item => item.id === started.id ? started : item) } };
    },
    onSuccess: result => { onActivated(result.created); refresh(); if (result.openDraft) { const attempt = result.created.attempts.at(-1); if (attempt) openNodeSession(run.id, result.created.id, attempt.id); } },
  });
  const invalidMode = mode === 'PROMPT' && !prompt.trim();
  const invalidGates = mode === 'PROMPT' && gates.some(gate => !String(gate.config.prompt || '').trim());
  const missingInputs = node.asset.inputs.some(field => !bindings[field.field_key]);
  const nodeRuns = run.node_runs.filter(item => item.flow_node_snapshot_key === node.instance_key);
  const visits = nodeRuns.length;
  const selectTopTab = (next: 'PROMPT' | 'CHAT') => { setTopTab(next); setMode(next); };
  const runAction = <button className="primary node-run-button" disabled={terminal || invalidMode || invalidGates || mutation.isPending} onClick={() => mode === 'PROMPT' && missingInputs ? setPromptTab('inputs') : mutation.mutate(bindings)}><Play size={15}/>{mutation.isPending ? '正在创建…' : mode === 'PROMPT' && missingInputs ? '请先填写节点输入' : mode === 'CHAT' ? '启动节点会话' : '开始节点执行'}</button>;
  const history = <NodeExecutionHistory run={run} node={node} onSelectExecution={onSelectExecution}/>;
  return <><NodeConfigurationPanel title={node.alias || node.asset.name} subtitle={`节点控制台 · 已执行 ${visits} 次`} mode={topTab} onModeChange={selectTopTab} promptTab={promptTab} onPromptTabChange={setPromptTab} action={runAction} promptContent={<><InputSummary fields={node.asset.inputs} bindings={bindings} artifacts={inputArtifacts}/>{node.asset.inputs.length > 0 && <button className="secondary full" onClick={() => setInputDialogOpen(true)}><Upload size={14}/>填写节点输入</button>}<StartupPromptSummary prompt={prompt} freezeHint="本次执行创建后会冻结。" onEdit={() => setPromptDialogOpen(true)}/></>} agentContent={<AgentPresetEditor preset={agentPreset} nodeContext={node.asset.executor?.context_prompt ?? ''} onChange={setAgentPreset}/>} gateContent={<GateDraftEditor gates={gates} onChange={setGates}/>} historyContent={history} chatContent={history} belowContent={<>{invalidGates && <p className="error">每个门禁都需要填写判定提示词。</p>}{terminal && <p className="field-hint">流程已结束，不能创建新的节点执行。</p>}{mutation.error && <p className="error"><AlertTriangle size={14}/>{mutation.error.message}</p>}</>}/>{inputDialogOpen && <NodeInputDialog run={{ ...run, artifacts: inputArtifacts }} node={node} initialBindings={bindings} onClose={() => setInputDialogOpen(false)} onSubmit={({ bindings: nextBindings, artifacts }) => { setBindings(nextBindings); setInputArtifacts(current => mergeArtifacts(current, artifacts)); setInputDialogOpen(false); }}/>} {promptDialogOpen && <StartupPromptDialog prompt={prompt} onChange={setPrompt} onClose={() => setPromptDialogOpen(false)}/>}</>;
}

function AttemptPanel({ run, nodeRun, attempt, refresh, navigate }: { run: FlowRun; nodeRun: NodeRun; attempt: NodeAttempt; refresh: () => void; navigate: (result: unknown, kind: string) => void }) {
  const dialog = useProductDialog();
  const [text, setText] = useState('');
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [inputArtifacts, setInputArtifacts] = useState<ArtifactVersion[]>(run.artifacts);
  const [tab, setTab] = useState<'overview' | 'gates' | 'outputs'>('overview');
  const [inputDialogOpen, setInputDialogOpen] = useState(false);
  const [manualOutputs, setManualOutputs] = useState<Record<string, string>>({});
  const attemptSnapshot = run.snapshots.find(item => item.id === attempt.snapshot_id);
  const activeSnapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  // Older attempts can reference snapshots that were compacted from the run
  // payload. Their contracts are still best represented by the active frozen
  // run snapshot, rather than an empty result panel.
  const attemptNode = (attemptSnapshot ?? activeSnapshot)?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
  useEffect(() => { setTab('overview'); setInputDialogOpen(false); setManualOutputs({}); }, [attempt.id]);
  useEffect(() => setInputArtifacts(current => mergeArtifacts(current, run.artifacts)), [run.artifacts]);
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const currentBinding = (field: string) => bindings[field] ?? attempt.input_bindings.find(item => item.input_field_key === field)?.artifact_version_id ?? '';
  const mutation = useMutation({ mutationFn: async ({ kind, body }: { kind: string; body?: unknown }) => {
    if (kind === 'accept') return api.acceptAttempt(attempt.id, attempt.state_version);
    if (kind === 'reject') return api.rejectAttempt(attempt.id, String((body as { reason: string }).reason), attempt.state_version);
    if (kind === 'human') return api.humanInput(attempt.id, String((body as { content: string }).content), attempt.state_version);
    if (kind === 'manual-outputs') return api.submitManualOutputs(
      attempt.id,
      attempt.state_version,
      body as Record<string, { artifact_type: 'URL'; uri: string } | { artifact_type: 'FILE'; path: string }>,
    );
    if (kind === 'retry') return api.retryGates(attempt.id, attempt.state_version);
    if (kind === 'retry-cancel') return api.retryRuntimeCancel(attempt.id, attempt.state_version, 'RECONCILE_PARENT');
    if (kind === 'delete-runtime') return api.retryRuntimeCancel(attempt.id, attempt.state_version, 'DELETE_MANAGED_RUNTIME');
    if (kind === 'bind') return api.bindInputs(attempt.id, body as Record<string, string>, attempt.state_version);
    return api.cancelAttempt(attempt.id, attempt.state_version);
  }, onSuccess: (result, variables) => {
    setText('');
    navigate(result, variables.kind);
    refresh();
  } });
  const act = (kind: string, body?: unknown) => mutation.mutate({ kind, body });
  const attemptTerminal = attempt.state === 'ACCEPTED' || attempt.state === 'REJECTED' || attempt.state === 'CANCELLED';
  const editableInputs = attempt.state === 'WAITING_INPUT' || attempt.state === 'START_BLOCKED';
  const nodeInputBindings = Object.fromEntries((attemptNode?.asset.inputs ?? []).map(field => [field.field_key, currentBinding(field.field_key)]).filter(([, value]) => value));
  // Attempts created before runtime gate freezing retain their legacy gates in
  // the immutable snapshot. Prefer the frozen Attempt contract, but render the
  // snapshot contract as a read-only historical fallback so users still see
  // every configured gate before an evaluation has been produced.
  const gatePolicies = attempt.gate_policies?.length ? attempt.gate_policies : (attemptNode?.gates ?? []);
  const automaticAttempt = run.run_mode === 'AUTOMATIC';
  const runtimeFailed = attempt.error_code === 'RUNTIME_FAILED' || attempt.runtime_phase === 'FAILED';
  const manualOutputFields = attemptNode?.asset.outputs ?? [];
  const manualOutputsReady = manualOutputFields.every(field => Boolean(manualOutputs[field.field_key]?.trim()));
  const retryableAutomaticFailure = !attempt.error_code || [
    'GATE_CONFIG_INVALID',
    'AUTOMATIC_GATE_DELIVERY_FAILED',
    'AUTOMATIC_START_DELIVERY_FAILED',
    'AUTOMATIC_TRANSITION_DELIVERY_FAILED',
    'AUTOMATIC_TRANSITION_INVALID',
  ].includes(attempt.error_code);
  return <aside className="action-panel attempt-control"><header><div><b>{nodeRunName(run, nodeRun)}</b><small>第 {nodeVisitNumber(run, nodeRun)} 次执行 / 第 {attempt.attempt_no} 轮</small></div></header><nav className="attempt-detail-tabs" aria-label="执行详情"><button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>概览</button><button className={tab === 'gates' ? 'active' : ''} onClick={() => setTab('gates')}>门禁结果</button><button className={tab === 'outputs' ? 'active' : ''} onClick={() => setTab('outputs')}>输出</button></nav><div className="action-content">{tab === 'overview' && <><div className="state-banner"><span>当前轮次状态</span><b>{runtimeFailed ? '节点执行失败' : ATTEMPT_STATE_LABELS[attempt.state] ?? attempt.state}</b><small><span data-testid="attempt-state">{attempt.state}</span> · 状态版本 {attempt.state_version}</small></div>{attemptNode && <NodeContextSummary node={attemptNode} contextIds={attempt.context_ids ?? null} mode={attempt.startup_mode === 'CHAT' ? 'CHAT' : 'PROMPT'}/>}<InputSummary fields={attemptNode?.asset.inputs ?? []} bindings={nodeInputBindings} artifacts={inputArtifacts}/>{editableInputs && <button className="secondary full" onClick={() => setInputDialogOpen(true)}><Play size={14}/>编辑本轮输入</button>}{!editableInputs && <p className="field-hint">输入已随本轮启动冻结，仅供查看。</p>}
    {attempt.runtime_phase === 'CANCEL_FAILED' && <section className="terminal-run-panel"><h4>Agent 停止状态未确认</h4><p>{attempt.error_detail || '运行时停止失败，需要重新对账。FlowRun Runtime 的健康、替换和诊断入口位于会话工作台。'}</p>{attempt.runtime_cancel_recovery_modes.includes('RECONCILE_PARENT') && <button className="secondary full" disabled={mutation.isPending} onClick={() => act('retry-cancel')}>重新对账并重试停止</button>}</section>}
    <button className="secondary full node-session-entry" onClick={() => openNodeSession(run.id, nodeRun.id, attempt.id)}><Send size={15}/>进入节点会话</button>
    {nodeRun.attempts.length > 1 && <section className="attempt-switcher"><h4>修订轮次</h4><div>{nodeRun.attempts.map(item => <button key={item.id} className={item.id === attempt.id ? 'active' : ''} onClick={() => useWorkbenchStore.getState().selectAttempt(item.id)}>第 {item.attempt_no} 轮</button>)}</div></section>}
    {terminal ? <section className="terminal-run-panel"><h4>{run.state === 'CANCELLED' ? '流程已取消' : '流程已完成'}</h4><p>运行已进入只读终态，历史记录继续保留。流程级操作位于上方“流程运行态管理”。</p></section> : <>
      {attempt.startup_mode === 'CHAT' && attempt.state === 'WAITING_START_CONFIRMATION' && <section className="manual-session-outputs"><h4>提交会话产出</h4><p>会话回复不会自动成为节点输出。请按冻结输出合同填写 URL 或共享工作区文件路径，平台校验并复制为候选产物后再运行完成门禁。</p>{manualOutputFields.map(field => <label key={field.field_key}>{field.display_name || field.field_key} · {field.data_type}<input aria-label={`提交输出 ${field.display_name || field.field_key}`} value={manualOutputs[field.field_key] ?? ''} onChange={event => setManualOutputs(current => ({ ...current, [field.field_key]: event.target.value }))} placeholder={field.data_type === 'FILE' ? '/runtime/workspace/project/...' : 'https://...'}/></label>)}<button className="primary full" disabled={!manualOutputsReady || mutation.isPending} onClick={() => act('manual-outputs', Object.fromEntries(manualOutputFields.map(field => [field.field_key, field.data_type === 'FILE' ? { artifact_type: 'FILE' as const, path: manualOutputs[field.field_key].trim() } : { artifact_type: 'URL' as const, uri: manualOutputs[field.field_key].trim() }]))) }>{mutation.isPending ? '提交中…' : '提交候选输出并运行完成门禁'}</button></section>}
      {attempt.state === 'WAITING_HUMAN' && <><label>人工输入<textarea value={text} onChange={event => setText(event.target.value)}/></label><button className="primary full" disabled={!text} onClick={() => act('human', { content: text })}>提交并继续</button></>}
      {attempt.state === 'WAITING_CONFIRMATION' && <RuntimeConfirmationPanel attempt={attempt} onResolved={refresh}/>}
      {attempt.state === 'WAITING_ACCEPTANCE' && (automaticAttempt ? <section className="terminal-run-panel"><h4>等待平台自动流转</h4><p>完成门禁已通过。平台正在按冻结拓扑和端口映射验收产物并选择后继节点，无需进入会话推动。</p></section> : <><label>验收意见<textarea value={text} onChange={event => setText(event.target.value)} placeholder="退回时填写修改要求"/></label><button className="primary full" onClick={() => act('accept')}>完成节点并流转</button><button className="secondary full" disabled={!text} onClick={() => act('reject', { reason: text })}>退回修改</button></>)}
      {(attempt.state === 'START_BLOCKED' || attempt.state === 'END_BLOCKED') && <section className="terminal-run-panel"><h4>{runtimeFailed ? '节点执行失败' : automaticAttempt ? '自动运行需要人工处理' : '门禁未通过'}</h4><p>{attempt.error_detail || (runtimeFailed ? '模型或运行时执行失败，尚未生成正式输出。请检查模型配置和运行日志后重新执行节点。' : '请查看门禁结果，修正问题后重试。')}</p>{runtimeFailed && <small>失败发生在 Runtime 执行阶段，不是完成门禁拒绝；当前没有可流转的正式 Artifact。</small>}{(!automaticAttempt || retryableAutomaticFailure) && !runtimeFailed && <button className="secondary full" onClick={() => act('retry')}>重试当前阶段</button>}</section>}
      {!automaticAttempt && !attemptTerminal && <button className="danger full cancel-attempt-button" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '取消当前节点的本轮执行？', message: '只会取消这个节点的当前轮次，其他节点执行和整个流程不会被取消。', confirmLabel: '取消本轮执行', tone: 'danger' }).then(ok => ok && act('cancel'))}><StopCircle size={15}/>取消本轮节点执行</button>}
    </>}</>}{tab === 'gates' && <section className="attempt-side-section"><h4>门禁结果</h4><GateList evaluations={attempt.gate_evaluations} policies={gatePolicies}/></section>}{tab === 'outputs' && <section className="attempt-side-section attempt-side-artifacts"><ArtifactList artifacts={attempt.artifacts} expectedFields={attemptNode?.asset.outputs ?? []}/></section>}{mutation.error && <p className="error">{mutation.error.message}</p>}</div>{inputDialogOpen && attemptNode && <NodeInputDialog run={{ ...run, artifacts: inputArtifacts }} node={attemptNode} initialBindings={nodeInputBindings} onClose={() => setInputDialogOpen(false)} onSubmit={({ bindings: nextBindings, artifacts }) => { setInputDialogOpen(false); setBindings(nextBindings); setInputArtifacts(current => mergeArtifacts(current, artifacts)); act('bind', nextBindings); }}/>}</aside>;
}

function SnapshotSync({ run, currentVersion, onSynced }: { run: FlowRun; currentVersion?: number; onSynced: (run: FlowRun) => void }) {
  const active = run.snapshots.find(item => item.id === run.active_snapshot_id);
  const changed = currentVersion !== undefined && active?.definition.row_version !== currentVersion;
  const mutation = useMutation({ mutationFn: () => api.syncSnapshot(run.id, run.active_snapshot_version), onSuccess: onSynced });
  if (!changed) return null;
  return <section className="snapshot-sync" data-testid="snapshot-sync"><span><b>发现流程配置更新</b><small>运行快照 v{run.active_snapshot_version} 使用定义版本 {active?.definition.row_version}，当前流程为 v{currentVersion}。历史执行轮次与产物不会改变。</small></span><button className="secondary" onClick={() => mutation.mutate()} disabled={mutation.isPending}><RefreshCw size={14}/>{mutation.isPending ? '同步中…' : '同步最新配置'}</button>{mutation.error && <p className="error">{mutation.error.message}</p>}</section>;
}

const emptyAutomaticNodePlan = (node: SnapshotFlowNode): AutomaticNodePlan => ({
  startup_prompt: node.asset.executor?.startup_prompt ?? '',
  agent_preset: {
    capability_version_ids: [],
    node_context_enabled: false,
    node_context_prompt: node.asset.executor?.context_prompt ?? '',
  },
  gates: [], artifact_ids: {}, input_urls: {},
});

function AutomaticRecordDialog({ run, onClose, onCreated }: { run: FlowRun; onClose: () => void; onCreated: (record: FlowRunAutomaticRecord) => void }) {
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const nodes = snapshot?.definition.nodes ?? [];
  const [name, setName] = useState('');
  const [startNodeKey, setStartNodeKey] = useState(nodes[0]?.instance_key ?? '');
  const mutation = useMutation({
    mutationFn: () => {
      if (!run.environment_version_id) throw new Error('当前流程运行未绑定运行环境。');
      return api.createAutomaticRecord(run.id, {
        name: name.trim() || undefined,
        environment_version_id: run.environment_version_id,
        start_node_key: startNodeKey, node_plans: {},
      });
    },
    onSuccess: onCreated,
  });
  useEscapeClose(onClose);
  return <div className="modal-backdrop"><section className="modal automatic-record-dialog" role="dialog" aria-modal="true" aria-label="新增自动运行"><header><div><span className="eyebrow">AUTOMATIC RUN</span><h2>新增自动运行</h2><p>记录归属于当前流程运行，并使用当前冻结快照与运行环境。</p></div><button type="button" className="ghost" aria-label="关闭新增自动运行" onClick={onClose}><X size={17}/></button></header><label>名称<input aria-label="自动运行名称" value={name} onChange={event => setName(event.target.value)} placeholder={`${run.name} · 自动运行`}/></label><label>起始节点<select aria-label="自动运行起始节点" value={startNodeKey} onChange={event => setStartNodeKey(event.target.value)}>{nodes.map(node => <option key={node.instance_key} value={node.instance_key}>{node.alias || node.asset.name}</option>)}</select></label>{mutation.error && <p className="error">{mutation.error.message}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button type="button" className="primary" disabled={!startNodeKey || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? '创建中…' : '创建草稿'}</button></footer></section></div>;
}

function AutomaticRecordEditor({ parent, record, selectedKey, onDraft, onSaved }: { parent: FlowRun; record: FlowRunAutomaticRecord; selectedKey?: string; onDraft: (record: FlowRunAutomaticRecord) => void; onSaved: (record: FlowRunAutomaticRecord) => void }) {
  const snapshot = parent.snapshots.find(item => item.id === parent.active_snapshot_id) ?? parent.snapshots.at(-1);
  const [name, setName] = useState(record.name);
  const [plans, setPlans] = useState(record.node_plans);
  const [inputArtifacts, setInputArtifacts] = useState(record.artifacts);
  const [promptTab, setPromptTab] = useState<PromptConfigurationTab>('inputs');
  const [inputDialogOpen, setInputDialogOpen] = useState(false);
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [saveFeedback, setSaveFeedback] = useState('');
  // The parent keeps unsaved automatic edits in an overlay, so polling cannot
  // replace them with an older server projection. Mirror that effective record
  // whenever the overlay or a successful save changes it.
  useEffect(() => { setName(record.name); setPlans(record.node_plans); setInputArtifacts(record.artifacts); }, [record.artifacts, record.name, record.node_plans]);
  useEffect(() => { setPromptTab('inputs'); setInputDialogOpen(false); setPromptDialogOpen(false); setSaveFeedback(''); }, [record.id, selectedKey]);
  const activeKey = selectedKey;
  const node = snapshot?.definition.nodes.find(item => item.instance_key === activeKey);
  const plan = node && activeKey ? plans[activeKey] ?? emptyAutomaticNodePlan(node) : undefined;
  const editable = record.state === 'DRAFT';
  const patch = (value: Partial<AutomaticNodePlan>) => {
    if (!plan || !activeKey) return;
    const nextPlans = { ...plans, [activeKey]: { ...plan, ...value } };
    setPlans(nextPlans);
    onDraft({ ...record, node_plans: nextPlans, artifacts: inputArtifacts });
  };
  const save = useMutation({
    mutationFn: () => {
      const nodePlans = activeKey && plan ? { ...plans, [activeKey]: plan } : plans;
      return api.updateAutomaticRecord(parent.id, record.id, {
        expected_row_version: record.row_version, name: name.trim() || record.name,
        start_node_key: record.start_node_key, node_plans: nodePlans,
      });
    },
    onMutate: () => setSaveFeedback(''),
    onSuccess: saved => {
      onSaved(saved);
      setPlans(saved.node_plans);
      setSaveFeedback(saved.readiness.ready
        ? '配置已保存，自动运行已就绪。'
        : `配置已保存，仍有 ${saved.readiness.issues.length} 项待补齐：${saved.readiness.issues.map(issue => issue.message).join('；')}`);
    },
  });
  if (!node || !plan) return <aside className="action-panel automatic-record-editor"><div className="action-content automatic-empty">请在流程图中选择一个可配置节点。</div></aside>;
  const feedback = save.error ? `保存失败：${save.error.message}` : saveFeedback;
  const history = <NodeExecutionHistory run={record} node={node}/>;
  return <><NodeConfigurationPanel className="automatic-record-editor" title={node.alias || node.asset.name} subtitle={`${record.name} · ${editable ? '自动运行草稿配置' : '自动运行配置（只读）'}`} mode="PROMPT" onModeChange={() => undefined} chatEnabled={false} promptTab={promptTab} onPromptTabChange={setPromptTab} feedback={feedback && <div className={`automatic-save-feedback-banner ${save.error ? 'error' : 'success'}`} role={save.error ? 'alert' : 'status'}><b>{save.error ? '保存失败' : '已保存'}</b><span>{feedback}</span></div>} action={editable && <button className="primary node-run-button" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存配置'}</button>} promptContent={<><InputSummary fields={node.asset.inputs} bindings={plan.artifact_ids} artifacts={inputArtifacts}/>{editable && node.asset.inputs.length > 0 && <button className="secondary full" onClick={() => setInputDialogOpen(true)}><Upload size={14}/>填写节点输入</button>}<StartupPromptSummary prompt={plan.startup_prompt} freezeHint="自动运行启动时会随记录冻结。" editable={editable} onEdit={() => setPromptDialogOpen(true)}/></>} agentContent={editable ? <AgentPresetEditor preset={plan.agent_preset} nodeContext={node.asset.executor?.context_prompt ?? ''} onChange={agent_preset => patch({ agent_preset })}/> : <section className="attempt-side-section"><h4>首会话 Agent 配置</h4><p>{plan.agent_preset.model_name || '工作区默认模型'} · {plan.agent_preset.capability_version_ids.length} 项能力</p></section>} gateContent={editable ? <GateDraftEditor gates={plan.gates} onChange={gates => patch({ gates })}/> : <GateList evaluations={[]} policies={plan.gates}/>} historyContent={history} chatContent={history}/>{inputDialogOpen && <NodeInputDialog run={{ ...record, artifacts: inputArtifacts }} node={node} initialBindings={plan.artifact_ids} onClose={() => setInputDialogOpen(false)} onSubmit={({ bindings: artifact_ids, artifacts }) => {
    const nextArtifacts = mergeArtifacts(inputArtifacts, artifacts);
    const nextPlans = { ...plans, [node.instance_key]: { ...plan, artifact_ids } };
    setPlans(nextPlans);
    setInputArtifacts(nextArtifacts);
    onDraft({ ...record, node_plans: nextPlans, artifacts: nextArtifacts });
    setInputDialogOpen(false);
  }}/>} {promptDialogOpen && <StartupPromptDialog prompt={plan.startup_prompt} label={`自动启动提示词 ${activeKey}`} onChange={startup_prompt => patch({ startup_prompt })} onClose={() => setPromptDialogOpen(false)}/>}</>;
}

export function WorkbenchPage() {
  const qc = useQueryClient();
  const dialog = useProductDialog();
  const { selectedRunId, selectedNodeRunId, selectedAttemptId, selectAttempt, selectExecution, setView } = useWorkbenchStore();
  const [selectedNodeKey, setSelectedNodeKey] = useState<string>();
  const [mode, setMode] = useState<WorkbenchMode>('MANUAL');
  const [selectedAutomaticId, setSelectedAutomaticId] = useState<string>();
  const [automaticDrafts, setAutomaticDrafts] = useState<Record<string, FlowRunAutomaticRecord>>({});
  const [automaticDialogOpen, setAutomaticDialogOpen] = useState(false);
  const [automaticBusyId, setAutomaticBusyId] = useState<string>();
  const [manualBusyId, setManualBusyId] = useState<string>();
  const [sidePanelWidth, setSidePanelWidth] = useState(390);
  const query = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const flowId = query.data?.flow_definition_id;
  const flow = useQuery({ queryKey: ['flow', flowId], queryFn: () => api.flow(flowId!), enabled: Boolean(flowId), refetchInterval: 5000 });
  const automatic = useQuery({ queryKey: ['flow-run-automatic-records', selectedRunId], queryFn: () => api.automaticRecords(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const refresh = useCallback(() => { if (selectedRunId) void qc.invalidateQueries({ queryKey: ['flow-run', selectedRunId] }); if (flowId) void qc.invalidateQueries({ queryKey: ['flow', flowId] }); void qc.invalidateQueries({ queryKey: ['runs'] }); }, [flowId, qc, selectedRunId]);
  useEffect(() => { setSelectedNodeKey(undefined); setAutomaticDrafts({}); }, [selectedRunId]);
  useEffect(() => selectedRunId ? subscribeToRun(selectedRunId, refresh) : undefined, [selectedRunId, refresh]);
  useEffect(() => {
    const run = query.data;
    if (!run) return;
    const selectedNode = run.node_runs.find(item => item.id === selectedNodeRunId);
    if (selectedNodeRunId && !selectedNode) {
      useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined });
      return;
    }
    if (selectedNode && !selectedAttemptId) {
      const latestAttempt = selectedNode.attempts.at(-1);
      if (latestAttempt) selectAttempt(latestAttempt.id);
      return;
    }
  }, [query.data, selectAttempt, selectedAttemptId, selectedNodeRunId]);
  const returnToRuns = () => setView('runs');
  if (!selectedRunId) return <div className="empty workbench-fallback"><b>未选择运行</b><button className="secondary" onClick={returnToRuns}><ArrowLeft size={14}/>返回运行列表</button></div>;
  const run = query.data;
  if (query.isError) return <div className="empty workbench-fallback"><b>运行详情加载失败</b><span>{query.error.message}</span><button className="secondary" onClick={returnToRuns}><ArrowLeft size={14}/>返回运行列表</button></div>;
  if (!run) return <div className="empty workbench-fallback"><span>加载运行状态…</span><button className="secondary" onClick={returnToRuns}><ArrowLeft size={14}/>返回运行列表</button></div>;
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const selectedNode = snapshot?.definition.nodes.find(item => item.instance_key === selectedNodeKey);
  const nodeRun = run.node_runs.find(item => item.id === selectedNodeRunId);
  const attempt = nodeRun?.attempts.find(item => item.id === selectedAttemptId) ?? nodeRun?.attempts.at(-1);
  const automaticRecords = (automatic.data ?? []).map(record => automaticDrafts[record.id] ?? record);
  const selectedAutomatic = automaticRecords.find(item => item.id === selectedAutomaticId);
  const selectedAutomaticNodeRun = selectedAutomatic?.state === 'DRAFT' ? undefined : [...(selectedAutomatic?.node_runs ?? [])].reverse().find(item => item.flow_node_snapshot_key === selectedNodeKey);
  const selectedAutomaticAttempt = selectedAutomaticNodeRun?.attempts.at(-1);
  const clearSelection = () => {
    setSelectedNodeKey(undefined);
    setSelectedAutomaticId(undefined);
    useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined });
  };
  const navigate = (result: unknown, kind: string) => {
    if (kind === 'delete') {
      qc.removeQueries({ queryKey: ['flow-run', selectedRunId] });
      void qc.invalidateQueries({ queryKey: ['runs'] });
      useWorkbenchStore.setState({ view: 'runs', selectedRunId: undefined, selectedNodeRunId: undefined, selectedAttemptId: undefined });
      return;
    }
    if (kind === 'reject' && result && typeof result === 'object' && 'id' in result && nodeRun) {
      const nextAttempt = result as NodeAttempt;
      qc.setQueryData<FlowRun>(['flow-run', selectedRunId], current => current ? { ...current, node_runs: current.node_runs.map(item => item.id === nodeRun.id ? { ...item, attempts: [...item.attempts.map(existing => existing.id === attempt?.id ? { ...existing, state: 'REJECTED' as const } : existing), nextAttempt] } : item) } : current);
      selectExecution(nodeRun.id, nextAttempt.id);
    }
    if (kind === 'cancel' && result && typeof result === 'object' && 'node_run_id' in result) {
      const cancelled = result as NodeAttempt;
      qc.setQueryData<FlowRun>(['flow-run', selectedRunId], current => current ? {
        ...current,
        state: 'ACTIVE',
        completion_mode: null,
        finished_at: null,
        node_runs: current.node_runs.map(item => item.id === cancelled.node_run_id ? {
          ...item,
          state: 'CANCELLED',
          attempts: item.attempts.map(existing => existing.id === cancelled.id ? cancelled : existing),
        } : item),
      } : current);
      clearSelection();
    }
    if ((kind === 'complete' || kind === 'cancel') && result && typeof result === 'object' && 'node_runs' in result) {
      qc.setQueryData(['flow-run', selectedRunId], result as FlowRun);
    }
    if (kind === 'sync' && result && typeof result === 'object' && 'node_runs' in result) {
      qc.setQueryData(['flow-run', selectedRunId], result as FlowRun);
    }
    if (kind === 'accept' && result && typeof result === 'object' && 'node_runs' in result) {
      const updated = result as FlowRun;
      qc.setQueryData(['flow-run', selectedRunId], updated);
      const next = [...updated.node_runs].reverse().find(item => item.state === 'ACTIVE') ?? updated.node_runs.at(-1);
      if (next) selectExecution(next.id, next.attempts.at(-1)?.id);
    }
    if (kind === 'activate' && result && typeof result === 'object' && 'attempts' in result) {
      const created = result as NodeRun;
      qc.setQueryData<FlowRun>(['flow-run', selectedRunId], current => current ? {
        ...current,
        node_runs: current.node_runs.some(item => item.id === created.id)
          ? current.node_runs.map(item => item.id === created.id ? created : item)
          : [...current.node_runs, created],
        progress: { ...current.progress, active: current.node_runs.some(item => item.id === created.id) ? current.progress.active : current.progress.active + 1 },
      } : current);
      selectExecution(created.id, created.attempts.at(-1)?.id);
    }
  };
  const automaticNeutralView = mode === 'AUTOMATIC' && !selectedAutomatic;
  const graphReachableKeys = mode === 'AUTOMATIC' && selectedAutomatic
      ? selectedAutomatic.reachable_node_keys
      : reachableNodeKeys(run);
  // A new manual run still begins from the graph. Once an execution is selected,
  // however, clicking a never-run node must not discard that selection.
  const graphSelectableKeys = mode === 'AUTOMATIC' && selectedAutomatic
    ? selectedAutomatic.state === 'DRAFT'
      ? unlockedAutomaticNodeKeys(run, selectedAutomatic)
      : new Set(selectedAutomatic.node_runs.map(item => item.flow_node_snapshot_key))
    : graphReachableKeys;
  const configuredAutomaticPlanKeys = mode === 'AUTOMATIC' && selectedAutomatic?.state === 'DRAFT'
    ? readyAutomaticPlanKeys(selectedAutomatic) : new Set<string>();
  const missingAutomaticPlanKeys = mode === 'AUTOMATIC' && selectedAutomatic?.state === 'DRAFT'
    ? selectedAutomatic.readiness.issues.map(issue => issue.node_key) : [];
  const graphSelectedKey = selectedNodeKey ?? (mode === 'MANUAL' ? nodeRun?.flow_node_snapshot_key : undefined);
  const showExecutionState = mode === 'MANUAL'
    ? Boolean(nodeRun)
    : Boolean(selectedAutomatic && selectedAutomatic.state !== 'DRAFT');
  const neutralGraphHelp = mode === 'MANUAL'
    ? '未选择运行记录，当前显示中性流程定义；点击节点可新建单节点运行。'
    : selectedAutomatic?.state === 'DRAFT'
      ? '当前显示自动运行草稿配置；启动后将按持久节点执行事实更新状态。'
      : selectedAutomatic
        ? '当前显示该自动运行记录的持久执行状态；灰色节点尚未激活。'
      : '未选择自动运行记录，当前显示中性流程定义；点击节点可新建单节点运行。';
  const hasPanel = Boolean(selectedAutomatic || (nodeRun && attempt) || selectedNode);
  const selectGraphNode = (key: string) => {
    if (mode === 'AUTOMATIC' && selectedAutomatic) {
      setSelectedNodeKey(key);
      useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined });
      return;
    }
    if (mode === 'MANUAL') {
      const latest = [...run.node_runs].reverse().find(item => item.flow_node_snapshot_key === key && item.state !== 'CANCELLED');
      if (latest) {
        setSelectedNodeKey(key);
        selectExecution(latest.id, latest.attempts.at(-1)?.id);
      }
      // Preserve an existing execution selection when an unrelated, never-run
      // node is clicked. With no selection, retain the initial graph-to-console
      // flow used to create the first execution.
      if (nodeRun || selectedNodeRunId) return;
    }
    setSelectedNodeKey(key);
    useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined });
  };
  const selectHistory = (id: string) => {
    if (useWorkbenchStore.getState().selectedNodeRunId === id) {
      clearSelection();
      return;
    }
    const item = run.node_runs.find(candidate => candidate.id === id);
    setSelectedNodeKey(item?.flow_node_snapshot_key);
    selectExecution(id, item?.attempts.at(-1)?.id);
  };
  const selectAutomaticHistory = (id: string) => {
    if (selectedAutomaticId === id) {
      clearSelection();
      return;
    }
    const record = automaticRecords.find(item => item.id === id);
    setSelectedAutomaticId(id);
    const activeNode = record?.node_runs.find(item => item.state === 'ACTIVE') ?? record?.node_runs.at(-1);
    setSelectedNodeKey(record?.state === 'DRAFT' ? record.start_node_key : activeNode?.flow_node_snapshot_key);
  };
  const beginSideResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const move = (moveEvent: PointerEvent) => setSidePanelWidth(Math.max(320, Math.min(680, window.innerWidth - moveEvent.clientX)));
    const stop = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', stop); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  };
  const retainAutomaticDraft = (record: FlowRunAutomaticRecord) => setAutomaticDrafts(current => ({ ...current, [record.id]: record }));
  const replaceAutomatic = (record: FlowRunAutomaticRecord) => {
    setAutomaticDrafts(current => {
      const next = { ...current };
      delete next[record.id];
      return next;
    });
    qc.setQueryData<FlowRunAutomaticRecord[]>(['flow-run-automatic-records', run.id], current => (current ?? []).map(item => item.id === record.id ? record : item));
  };
  const rail = <RunRail run={run} mode={mode} automaticRecords={automaticRecords} selected={mode === 'MANUAL' ? nodeRun?.id : undefined} selectedNodeDeletable={Boolean(nodeRun && nodeRun.state === 'CANCELLED' && nodeRun.attempts.at(-1)?.runtime_phase === 'CANCELLED')} manualBusyId={manualBusyId} selectedAutomaticId={selectedAutomaticId} automaticBusyId={automaticBusyId} onModeChange={next => { setMode(next); clearSelection(); }} onSelect={selectHistory} onDeleteNode={() => { if (!nodeRun) return; void dialog.confirm({ title: '删除这条单节点运行？', message: '节点执行记录与产物将被永久删除；FlowRun、共享 Runtime 和 OpenHands 状态继续保留。', confirmLabel: '删除', tone: 'danger' }).then(async ok => { if (!ok) return; setManualBusyId(nodeRun.id); try { await api.deleteNodeRun(run.id, nodeRun.id); clearSelection(); await query.refetch(); } finally { setManualBusyId(undefined); } }); }} onSelectAutomatic={selectAutomaticHistory} onClearSelection={clearSelection} onCreateAutomatic={() => setAutomaticDialogOpen(true)} onDeleteAutomatic={() => { if (!selectedAutomatic) return; void dialog.confirm({ title: '删除自动运行记录？', message: '该记录的计划、执行历史与产物将被永久删除。', confirmLabel: '删除', tone: 'danger' }).then(async ok => { if (!ok) return; setAutomaticBusyId(selectedAutomatic.id); try { await api.deleteAutomaticRecord(run.id, selectedAutomatic.id); clearSelection(); await automatic.refetch(); } finally { setAutomaticBusyId(undefined); } }); }} onStartAutomatic={record => { setAutomaticBusyId(record.id); void api.startAutomaticRecord(run.id, record.id, record.row_version).then(replaceAutomatic).finally(() => setAutomaticBusyId(undefined)); }}/>;
  return <><section className="workbench-page flow-run-inner-workbench" style={hasPanel ? { gridTemplateColumns: `250px minmax(500px, 1fr) ${sidePanelWidth}px` } : { gridTemplateColumns: '250px minmax(500px, 1fr)' }}>{rail}<main className="run-main"><button className="back" onClick={returnToRuns}><ArrowLeft size={14}/>返回运行列表</button><header className="run-title" onClick={event => { if (!isInteractiveClick(event.target)) clearSelection(); }}><div><span className="eyebrow">第 {run.run_no} 次流程运行</span><h1>{run.name}</h1><p>{mode === 'AUTOMATIC' && selectedAutomatic ? `自动记录 · ${selectedAutomatic.name}` : `流程快照 v${run.active_snapshot_version} · ${run.progress.accepted}/${run.node_runs.length} 次节点执行已验收`}</p></div><div className="run-title-actions"><span className={`run-state ${(selectedAutomatic?.state ?? run.state).toLowerCase()}`}>{FLOW_STATE_LABELS[selectedAutomatic?.state ?? run.state] ?? selectedAutomatic?.state ?? run.state}</span>{(run.state === 'COMPLETED' || run.state === 'CANCELLED') && <TerminalRunDelete run={run} onDeleted={() => navigate(undefined, 'delete')}/>}</div></header>{mode === 'MANUAL' && run.state !== 'COMPLETED' && run.state !== 'CANCELLED' && <SnapshotSync run={run} currentVersion={flow.data?.row_version} onSynced={updated => navigate(updated, 'sync')}/>}<SnapshotGraph run={run} selectedKey={graphSelectedKey} reachableKeys={graphReachableKeys} selectableKeys={graphSelectableKeys} configuredPlanKeys={configuredAutomaticPlanKeys} executionRun={mode === 'AUTOMATIC' ? selectedAutomatic : run} missingPlanKeys={missingAutomaticPlanKeys} showExecutionState={showExecutionState} neutralHelp={neutralGraphHelp} neutralView={automaticNeutralView} onClearSelection={clearSelection} onSelect={selectGraphNode}/></main>{hasPanel && <aside className="run-side-panel"><div className="run-side-resizer" role="separator" aria-label="调整右侧栏宽度" aria-orientation="vertical" onPointerDown={beginSideResize}/>{mode === 'AUTOMATIC' && selectedAutomatic ? selectedAutomatic.state === 'DRAFT' ? <AutomaticRecordEditor key={selectedAutomatic.id} parent={run} record={selectedAutomatic} selectedKey={selectedNodeKey} onDraft={retainAutomaticDraft} onSaved={replaceAutomatic}/> : selectedAutomaticNodeRun && selectedAutomaticAttempt ? <AttemptPanel run={selectedAutomatic} nodeRun={selectedAutomaticNodeRun} attempt={selectedAutomaticAttempt} refresh={() => { void automatic.refetch(); }} navigate={() => { void automatic.refetch(); }}/> : <aside className="action-panel"><div className="action-content automatic-empty">该节点尚未激活。自动调度到达后会在这里显示执行、门禁和人工处理入口。</div></aside> : nodeRun && attempt ? <AttemptPanel run={run} nodeRun={nodeRun} attempt={attempt} refresh={refresh} navigate={navigate}/> : selectedNode ? <NodeConsole run={run} node={selectedNode} refresh={refresh} onActivated={created => { setSelectedNodeKey(undefined); navigate(created, 'activate'); }} onSelectExecution={item => { setSelectedNodeKey(item.flow_node_snapshot_key); selectExecution(item.id, item.attempts.at(-1)?.id); }}/> : null}</aside>}</section>{automaticDialogOpen && <AutomaticRecordDialog run={run} onClose={() => setAutomaticDialogOpen(false)} onCreated={record => { setAutomaticDialogOpen(false); qc.setQueryData<FlowRunAutomaticRecord[]>(['flow-run-automatic-records', run.id], current => [record, ...(current ?? [])]); setSelectedAutomaticId(record.id); setSelectedNodeKey(record.start_node_key); }}/>}</>;
}
