import { Background, Controls, ReactFlow, type Edge, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { AlertTriangle, ArrowLeft, CheckCircle2, Download, Eye, FileText, Play, RefreshCw, Send, StopCircle, Trash2, Wrench, X } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, artifactContentUrl, subscribeToRun } from '../api/client';
import { useWorkbenchStore } from '../store/workbench';
import type { ArtifactVersion, AttemptState, FlowRun, GateEvaluation, NodeAttempt, NodeRun, RunEvent, SnapshotFlowNode } from '../types';

const attemptState = (run: NodeRun) => run.attempts.at(-1)?.state ?? run.state;

const ATTEMPT_STATE_LABELS: Record<AttemptState, string> = {
  WAITING_INPUT: '等待补充输入',
  START_GATES: '正在检查启动条件',
  START_BLOCKED: '启动条件未通过',
  WAITING_START_CONFIRMATION: '等待确认开始',
  EXECUTING: '正在执行',
  WAITING_HUMAN: '等待人工回复',
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
  const state = attemptState(run);
  return state in ATTEMPT_STATE_LABELS ? ATTEMPT_STATE_LABELS[state as AttemptState] : state;
};

function RunRail({ run, selected, onSelect }: { run: FlowRun; selected?: string; onSelect: (id: string) => void }) {
  const active = run.node_runs.filter(item => item.state === 'ACTIVE').length;
  return <aside className="run-rail"><span className="eyebrow">流程运行</span><h2>{run.name}</h2><span data-testid="flow-run-state" className={`run-state ${run.state.toLowerCase()}`}>{FLOW_STATE_LABELS[run.state] ?? run.state}</span><div className="run-progress"><span>{run.progress.accepted} 已验收</span><span>{run.progress.terminal} 已结束</span><span>{active} 进行中</span></div><div className="run-history-title"><b>节点执行记录</b><small>每条代表节点的一次独立执行；退回修改会在该记录内增加新轮次，每轮都保留自己的 Agent 对话。</small></div><div className="timeline">{run.node_runs.map(item => <button key={item.id} className={selected === item.id ? 'active' : ''} onClick={() => onSelect(item.id)}><i className={String(attemptState(item)).toLowerCase()}/><span><b>{nodeRunName(run, item)}</b><small>第 {nodeVisitNumber(run, item)} 次执行 · {attemptStateLabel(item)}</small></span></button>)}</div></aside>;
}

function SnapshotNodeDialog({ node, visits, onClose, onSelect }: { node: SnapshotFlowNode; visits: NodeRun[]; onClose: () => void; onSelect: (id: string) => void }) {
  const latest = visits.at(-1);
  const executor = node.asset.executor;
  const startGates = node.gates.filter(gate => gate.stage === 'START');
  const endGates = node.gates.filter(gate => gate.stage === 'END');
  return <div className="modal-backdrop"><section className="modal run-node-dialog" role="dialog" aria-label={`运行节点详情 ${node.alias || node.asset.name}`}><header><div><span className="eyebrow">SNAPSHOT NODE</span><h2>{node.alias || node.asset.name}</h2><p>{node.asset.name} · {node.instance_key}</p></div><button className="ghost" aria-label="关闭运行节点详情" onClick={onClose}><X size={16}/></button></header>
    <div className="run-node-summary"><span><small>运行次数</small><b>{visits.length}</b></span><span><small>最新状态</small><b>{latest ? attemptState(latest) : '未激活'}</b></span><span><small>START / END</small><b>{startGates.length} / {endGates.length}</b></span></div>
    <section><h3>执行配置</h3><dl><dt>模型服务</dt><dd>{executor?.model_provider_id || '未配置'}</dd><dt>模型</dt><dd>{executor?.model_name || '服务默认'}</dd><dt>默认 Skill</dt><dd>{node.asset.default_skill_ref}</dd><dt>超时 / 迭代</dt><dd>{executor?.timeout_seconds ?? '-'} 秒 / {executor?.max_iterations ?? '-'}</dd></dl></section>
    <section className="run-node-io"><div><h3>输入</h3>{node.asset.inputs.length ? node.asset.inputs.map(field => <span key={field.field_key}><b>{field.display_name}</b><small>{field.field_key} · {field.data_type}</small></span>) : <p>无输入</p>}</div><div><h3>输出</h3>{node.asset.outputs.length ? node.asset.outputs.map(field => <span key={field.field_key}><b>{field.display_name}</b><small>{field.field_key} · {field.data_type}</small></span>) : <p>无输出</p>}</div></section>
    <section><h3>门禁策略</h3>{node.gates.length ? node.gates.map(gate => <div className="run-node-gate" key={`${gate.stage}-${gate.position}`}><b>{gate.stage} · #{gate.position + 1}</b><span>{gate.gate_type}</span><small>{gate.enabled ? '启用' : '停用'}</small></div>) : <p>无门禁策略</p>}</section>
    <section><h3>执行历史</h3>{visits.length ? visits.map((visit, index) => <button className="run-node-visit" key={visit.id} onClick={() => { onSelect(visit.id); onClose(); }}><span><b>第 {index + 1} 次执行</b><small>{new Date(visit.activated_at).toLocaleString()} · {visit.created_from === 'RUN_START' ? '流程启动' : '人工启动'}</small></span><strong>{attemptStateLabel(visit)}</strong><small>{visit.attempts.length} 轮</small></button>) : <p>此快照节点尚未执行，可从右侧人工控制台选择“从任意节点重新运行”。</p>}</section>
    <footer><button className="secondary" onClick={onClose}>关闭</button>{latest && <button className="primary" onClick={() => { onSelect(latest.id); onClose(); }}>查看最新运行</button>}</footer>
  </section></div>;
}

function SnapshotGraph({ run, onSelect }: { run: FlowRun; onSelect: (id: string) => void }) {
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const [detailKey, setDetailKey] = useState<string>();
  const [nodes, edges] = useMemo(() => {
    const graphNodes: Node[] = (snapshot?.definition.nodes ?? []).map(item => {
      const visits = run.node_runs.filter(nodeRun => nodeRun.flow_node_snapshot_key === item.instance_key);
      const status = visits.at(-1)?.state.toLowerCase() ?? 'pending';
      return { id: item.instance_key, position: { x: item.position_x, y: item.position_y }, data: { label: <span><b>{item.alias || item.asset.name}</b><small>{visits.length ? `运行 ${visits.length} 次` : '未激活'}</small></span> }, className: `run-graph-node ${status}` };
    });
    const graphEdges: Edge[] = (snapshot?.definition.edges ?? []).map((item, index) => ({ id: item.id ?? `edge-${index}`, source: item.source_instance_key, target: item.target_instance_key, label: item.mappings.map(mapping => `${mapping.source_output_key} → ${mapping.target_input_key}`).join(', ') }));
    return [graphNodes, graphEdges] as const;
  }, [run.node_runs, snapshot]);
  const detailNode = snapshot?.definition.nodes.find(item => item.instance_key === detailKey);
  const detailVisits = detailNode ? run.node_runs.filter(item => item.flow_node_snapshot_key === detailNode.instance_key) : [];
  return <><section className="run-graph"><header><div><h3>运行快照 v{snapshot?.version ?? '-'}</h3><small>点击节点查看不可变快照配置与运行历史</small></div><span>定义 Hash {snapshot?.definition_hash.slice(0, 8)}</span></header><div className="run-graph-canvas"><ReactFlow nodes={nodes} edges={edges} nodesDraggable={false} nodesConnectable={false} fitView onNodeClick={(_, node) => setDetailKey(node.id)}><Background/><Controls showInteractive={false}/></ReactFlow></div></section>{detailNode && <SnapshotNodeDialog node={detailNode} visits={detailVisits} onClose={() => setDetailKey(undefined)} onSelect={onSelect}/>}</>;
}
function GateList({ evaluations }: { evaluations: GateEvaluation[] }) {
  return <div className="gate-results">{evaluations.length ? evaluations.map(item => <div className="gate-result" key={item.id}><span><b>{item.stage} · #{item.policy_position + 1}</b><small>{String(item.result.summary ?? '')}</small></span><strong className={item.decision === 'PASS' ? 'good' : 'bad'}>{item.decision}</strong></div>) : <div className="empty compact">此阶段没有门禁记录。</div>}</div>;
}

function EventTimeline({ events }: { events: RunEvent[] }) {
  return <section className="event-history"><h3>运行事件</h3>{events.map(event => <div key={event.cursor}><Wrench size={13}/><span><b>{event.event_type}</b><small>{new Date(event.occurred_at).toLocaleTimeString()}</small></span><code>{JSON.stringify(event.payload)}</code></div>)}</section>;
}

function ArtifactList({ artifacts }: { artifacts: ArtifactVersion[] }) {
  const [preview, setPreview] = useState<{ artifact: ArtifactVersion; content: string }>();
  const previewMutation = useMutation({ mutationFn: async (artifact: ArtifactVersion) => ({ artifact, content: await api.artifactContent(artifact.id) }), onSuccess: setPreview });
  return <><section className="artifacts"><h3>本轮输出</h3>{artifacts.length ? artifacts.map(item => <article key={item.id}><span className="artifact-version">v{item.version_no}</span><div><b>{item.field_key} · {item.artifact_type}</b><small>{item.source} · {item.content_hash.slice(0, 10)} · {item.byte_size} bytes</small><p>{item.inline_content}</p><div className="artifact-actions"><button className="ghost" onClick={() => previewMutation.mutate(item)}><Eye size={13}/>预览</button><a className="ghost" href={artifactContentUrl(item.id, true)}><Download size={13}/>下载</a></div></div></article>) : <div className="empty compact">本轮尚无输出。</div>}{previewMutation.error && <p className="error">{previewMutation.error.message}</p>}</section>{preview && <div className="modal-backdrop"><section className="modal artifact-preview" role="dialog" aria-label="产物预览"><header><div><span className="eyebrow">ARTIFACT v{preview.artifact.version_no}</span><h2>{preview.artifact.field_key}</h2></div><button className="ghost" aria-label="关闭产物预览" onClick={() => setPreview(undefined)}><X size={16}/></button></header><pre>{preview.content}</pre><footer><a className="primary" href={artifactContentUrl(preview.artifact.id, true)}><Download size={14}/>下载产物</a></footer></section></div>}</>;
}

function artifactOptions(run: FlowRun, dataType: string) {
  return run.artifacts.filter(item => item.artifact_type === dataType);
}

function AttemptPanel({ run, nodeRun, attempt, refresh, navigate }: { run: FlowRun; nodeRun: NodeRun; attempt: NodeAttempt; refresh: () => void; navigate: (result: unknown, kind: string) => void }) {
  const [text, setText] = useState('');
  const [artifact, setArtifact] = useState('');
  const [fieldKey, setFieldKey] = useState('manual_input');
  const [artifactType, setArtifactType] = useState('DOCUMENT');
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const activeSnapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const attemptSnapshot = run.snapshots.find(item => item.id === attempt.snapshot_id);
  const attemptNode = attemptSnapshot?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
  const [activationKey, setActivationKey] = useState(nodeRun.flow_node_snapshot_key);
  const [activationBindings, setActivationBindings] = useState<Record<string, string>>({});
  const activationNode = activeSnapshot?.definition.nodes.find(item => item.instance_key === activationKey);
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const currentBinding = (field: string) => bindings[field] ?? attempt.input_bindings.find(item => item.input_field_key === field)?.artifact_version_id ?? '';
  const mutation = useMutation({ mutationFn: async ({ kind, body }: { kind: string; body?: unknown }) => {
    if (kind === 'confirm') return api.confirmStart(attempt.id, attempt.state_version);
    if (kind === 'accept') return api.acceptAttempt(attempt.id, attempt.state_version);
    if (kind === 'reject') return api.rejectAttempt(attempt.id, String((body as { reason: string }).reason), attempt.state_version);
    if (kind === 'human') return api.humanInput(attempt.id, String((body as { content: string }).content), attempt.state_version);
    if (kind === 'retry') return api.retryGates(attempt.id, attempt.state_version);
    if (kind === 'artifact') return api.createArtifact(run.id, body as Parameters<typeof api.createArtifact>[1]);
    if (kind === 'bind') return api.bindInputs(attempt.id, body as Record<string, string>, attempt.state_version);
    if (kind === 'activate') return api.activateNode(run.id, activationKey, body as Record<string, string>);
    if (kind === 'complete') return api.completeRun(run.id);
    if (kind === 'delete') return api.deleteRun(run.id);
    return api.cancelRun(run.id);
  }, onSuccess: (result, variables) => { setText(''); setArtifact(''); navigate(result, variables.kind); if (variables.kind !== 'delete') refresh(); } });
  const act = (kind: string, body?: unknown) => mutation.mutate({ kind, body });
  const bindingPayload = Object.fromEntries((attemptNode?.asset.inputs ?? []).map(field => [field.field_key, currentBinding(field.field_key)]).filter(([, value]) => value));
  const activationPayload = Object.fromEntries(Object.entries(activationBindings).filter(([, value]) => value));
  return <aside className="action-panel"><header><div><b>人工控制台</b><small>第 {attempt.attempt_no} 轮 · 流程快照 v{attemptSnapshot?.version}</small></div></header><div className="action-content"><div className="state-banner"><span>当前轮次状态</span><b>{ATTEMPT_STATE_LABELS[attempt.state] ?? attempt.state}</b><small><span data-testid="attempt-state">{attempt.state}</span> · 状态版本 {attempt.state_version}</small></div>
    <button className="secondary full agent-chat-entry" onClick={() => useWorkbenchStore.getState().openAgentChat(run.id, nodeRun.id, attempt.id)}><Send size={15}/>进入 Agent 对话</button>
    {terminal ? <section className="terminal-run-panel"><h4>{run.state === 'CANCELLED' ? '流程已取消' : '流程已完成'}</h4><p>运行已进入只读终态。节点执行、修订轮次、对话、事件和产物会继续保留用于审计；如不再需要，可永久删除整个运行记录。</p><button className="danger full" disabled={mutation.isPending} onClick={() => { if (window.confirm('确定永久删除这个流程运行吗？相关节点执行、修订轮次、对话、事件和产物都会被清理，且不可恢复。')) act('delete'); }}><Trash2 size={15}/>{mutation.isPending ? '删除中…' : '永久删除此运行'}</button></section> : <>
      {attempt.state === 'WAITING_START_CONFIRMATION' && <button className="primary full" onClick={() => act('confirm')}><Play size={15}/>确认开始执行</button>}
      {attempt.state === 'WAITING_HUMAN' && <><label>人工输入<textarea value={text} onChange={e => setText(e.target.value)}/></label><button className="primary full" disabled={!text} onClick={() => act('human', { content: text })}><Send size={15}/>提交并继续</button></>}
      {attempt.state === 'WAITING_ACCEPTANCE' && <><label>验收意见<textarea aria-label="验收意见" value={text} onChange={e => setText(e.target.value)} placeholder="退回时填写修改要求"/></label><button className="primary full" onClick={() => act('accept')}><CheckCircle2 size={15}/>确认完成</button><button className="secondary full" disabled={!text} onClick={() => act('reject', { reason: text })}><RefreshCw size={15}/>退回修改并进入第 {attempt.attempt_no + 1} 轮</button></>}
      {(attempt.state === 'WAITING_INPUT' || attempt.state === 'START_BLOCKED') && <section className="input-binding-editor"><h4>提供或替换输入产物</h4>{attemptNode?.asset.inputs.map(field => <label key={field.field_key}>{field.display_name}<select aria-label={`绑定输入 ${field.field_key}`} value={currentBinding(field.field_key)} onChange={event => setBindings(old => ({ ...old, [field.field_key]: event.target.value }))}><option value="">未绑定</option>{artifactOptions(run, field.data_type).map(item => <option key={item.id} value={item.id}>{item.field_key} · v{item.version_no} · {item.source}</option>)}</select></label>)}<button className="secondary full" disabled={!Object.keys(bindingPayload).length} onClick={() => act('bind', bindingPayload)}>保存输入绑定</button>{attempt.state === 'START_BLOCKED' && <button className="secondary full" onClick={() => act('retry')}><RefreshCw size={15}/>重试门禁</button>}</section>}
      <section className="node-activation"><h4>从任意节点重新运行</h4><select aria-label="重新运行节点" value={activationKey} onChange={event => { setActivationKey(event.target.value); setActivationBindings({}); }}>{activeSnapshot?.definition.nodes.map(item => <option key={item.instance_key} value={item.instance_key}>{item.alias || item.asset.name}</option>)}</select>{activationNode?.asset.inputs.map(field => <label key={field.field_key}>{field.display_name}<select aria-label={`重新运行输入 ${field.field_key}`} value={activationBindings[field.field_key] ?? ''} onChange={event => setActivationBindings(old => ({ ...old, [field.field_key]: event.target.value }))}><option value="">稍后提供</option>{artifactOptions(run, field.data_type).map(item => <option key={item.id} value={item.id}>{item.field_key} · v{item.version_no}</option>)}</select></label>)}<button className="secondary full" onClick={() => act('activate', activationPayload)}><Play size={14}/>从此节点运行</button></section>
      <div className="manual-artifact"><h4>提供人工产物</h4><input aria-label="产物字段" value={fieldKey} onChange={e => setFieldKey(e.target.value)}/><input aria-label="产物类型" value={artifactType} onChange={e => setArtifactType(e.target.value)}/><textarea aria-label="产物内容" value={artifact} onChange={e => setArtifact(e.target.value)}/><button className="secondary full" disabled={!artifact} onClick={() => act('artifact', { field_key: fieldKey, artifact_type: artifactType, inline_content: artifact })}><FileText size={15}/>登记不可变产物版本</button></div>
      <button className="ghost full" onClick={() => { if (window.confirm('确定人工结束整个流程吗？所有尚未结束的节点运行都会被取消，已验收结果和产物历史会保留。')) act('complete'); }}>人工结束流程</button><button className="danger full" onClick={() => { if (window.confirm('确定取消整个流程吗？所有尚未结束的节点运行都会被取消，已验收结果和产物历史会保留。')) act('cancel'); }}><StopCircle size={15}/>取消整个流程</button>
    </>}{mutation.error && <p className="error"><AlertTriangle size={14}/>{mutation.error.message}</p>}</div></aside>;
}

function SnapshotSync({ run, currentVersion, onSynced }: { run: FlowRun; currentVersion?: number; onSynced: (run: FlowRun) => void }) {
  const active = run.snapshots.find(item => item.id === run.active_snapshot_id);
  const changed = currentVersion !== undefined && active?.definition.row_version !== currentVersion;
  const mutation = useMutation({ mutationFn: () => api.syncSnapshot(run.id, run.active_snapshot_version), onSuccess: onSynced });
  if (!changed) return null;
  return <section className="snapshot-sync" data-testid="snapshot-sync"><span><b>发现流程配置更新</b><small>运行快照 v{run.active_snapshot_version} 使用定义版本 {active?.definition.row_version}，当前流程为 v{currentVersion}。历史执行轮次与产物不会改变。</small></span><button className="secondary" onClick={() => mutation.mutate()} disabled={mutation.isPending}><RefreshCw size={14}/>{mutation.isPending ? '同步中…' : '同步最新配置'}</button>{mutation.error && <p className="error">{mutation.error.message}</p>}</section>;
}

export function WorkbenchPage() {
  const qc = useQueryClient();
  const { selectedRunId, selectedNodeRunId, selectedAttemptId, selectNodeRun, selectAttempt, selectExecution, setView } = useWorkbenchStore();
  const query = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const events = useQuery({ queryKey: ['run-events', selectedRunId], queryFn: () => api.flowEvents(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const flowId = query.data?.flow_definition_id;
  const flow = useQuery({ queryKey: ['flow', flowId], queryFn: () => api.flow(flowId!), enabled: Boolean(flowId), refetchInterval: 5000 });
  const refresh = useCallback(() => { if (selectedRunId) { void qc.invalidateQueries({ queryKey: ['flow-run', selectedRunId] }); void qc.invalidateQueries({ queryKey: ['run-events', selectedRunId] }); } if (flowId) void qc.invalidateQueries({ queryKey: ['flow', flowId] }); void qc.invalidateQueries({ queryKey: ['runs'] }); }, [flowId, qc, selectedRunId]);
  useEffect(() => selectedRunId ? subscribeToRun(selectedRunId, refresh) : undefined, [selectedRunId, refresh]);
  useEffect(() => {
    const run = query.data;
    if (!run) return;
    const selectedNode = run.node_runs.find(item => item.id === selectedNodeRunId);
    if (!selectedNode) {
      const latestNode = run.node_runs.at(-1);
      if (latestNode) selectExecution(latestNode.id, latestNode.attempts.at(-1)?.id);
      return;
    }
    if (!selectedAttemptId) {
      const latestAttempt = selectedNode.attempts.at(-1);
      if (latestAttempt) selectAttempt(latestAttempt.id);
    }
  }, [query.data, selectAttempt, selectExecution, selectedAttemptId, selectedNodeRunId]);
  if (!selectedRunId) return <div className="empty"><b>未选择运行</b><button className="secondary" onClick={() => setView('runs')}>返回运行列表</button></div>;
  const run = query.data;
  if (!run) return <div className="empty">加载运行状态…</div>;
  const nodeRun = run.node_runs.find(item => item.id === selectedNodeRunId) ?? run.node_runs.at(-1);
  const attempt = nodeRun?.attempts.find(item => item.id === selectedAttemptId) ?? nodeRun?.attempts.at(-1);
  const navigate = (result: unknown, kind: string) => {
    if (kind === 'delete') {
      qc.removeQueries({ queryKey: ['flow-run', selectedRunId] });
      qc.removeQueries({ queryKey: ['run-events', selectedRunId] });
      void qc.invalidateQueries({ queryKey: ['runs'] });
      useWorkbenchStore.setState({ view: 'runs', selectedRunId: undefined, selectedNodeRunId: undefined, selectedAttemptId: undefined, selectedConversationId: undefined });
      return;
    }
    if (kind === 'reject' && result && typeof result === 'object' && 'id' in result && nodeRun) {
      const nextAttempt = result as NodeAttempt;
      qc.setQueryData<FlowRun>(['flow-run', selectedRunId], current => current ? { ...current, node_runs: current.node_runs.map(item => item.id === nodeRun.id ? { ...item, attempts: [...item.attempts.map(existing => existing.id === attempt?.id ? { ...existing, state: 'REJECTED' as const } : existing), nextAttempt] } : item) } : current);
      selectExecution(nodeRun.id, nextAttempt.id);
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
        node_runs: [...current.node_runs, created],
        progress: { ...current.progress, active: current.progress.active + 1 },
      } : current);
      selectExecution(created.id, created.attempts.at(-1)?.id);
    }
  };
  return <section className="workbench-page"><RunRail run={run} selected={nodeRun?.id} onSelect={id => { const node = run.node_runs.find(item => item.id === id); selectExecution(id, node?.attempts.at(-1)?.id); }}/><main className="run-main"><button className="back" onClick={() => setView('runs')}><ArrowLeft size={14}/>返回运行列表</button><header className="run-title"><div><span className="eyebrow">第 {run.run_no} 次流程运行</span><h1>{run.name}</h1><p>流程快照 v{run.active_snapshot_version} · {run.progress.accepted}/{run.node_runs.length} 次节点执行已验收</p></div><span className={`run-state ${run.state.toLowerCase()}`}>{FLOW_STATE_LABELS[run.state] ?? run.state}</span></header>{run.state !== 'COMPLETED' && run.state !== 'CANCELLED' && <SnapshotSync run={run} currentVersion={flow.data?.row_version} onSynced={updated => navigate(updated, 'sync')}/>}<SnapshotGraph run={run} onSelect={selectNodeRun}/>
    {nodeRun && attempt ? <><section className="attempt-tabs"><header><div><h3>{nodeRunName(run, nodeRun)}</h3><small>第 {nodeVisitNumber(run, nodeRun)} 次节点执行 · {nodeRun.created_from === 'RUN_START' ? '流程启动' : '人工重跑'}</small></div><div>{nodeRun.attempts.map(item => <button className={item.id === attempt.id ? 'active' : ''} key={item.id} onClick={() => selectAttempt(item.id)}>第 {item.attempt_no} 轮</button>)}</div></header><div className="attempt-grid"><section><h4>输入绑定</h4>{attempt.input_bindings.map(item => <div className="binding-row" key={item.id}><b>{item.input_field_key}</b><span>{run.artifacts.find(artifactItem => artifactItem.id === item.artifact_version_id)?.field_key} v{run.artifacts.find(artifactItem => artifactItem.id === item.artifact_version_id)?.version_no}</span><small>{item.binding_source}</small></div>)}</section><section><h4>门禁结果</h4><GateList evaluations={attempt.gate_evaluations}/></section></div></section><ArtifactList artifacts={attempt.artifacts}/></> : <div className="empty compact">此流程运行尚未执行节点。</div>}<EventTimeline events={events.data ?? []}/></main>{nodeRun && attempt && <AttemptPanel run={run} nodeRun={nodeRun} attempt={attempt} refresh={refresh} navigate={navigate}/>}</section>;
}
