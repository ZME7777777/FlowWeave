import { Background, Controls, ReactFlow, type Edge, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { AlertTriangle, ArrowLeft, ExternalLink, Link2, Play, Plus, RefreshCw, Send, StopCircle, Trash2 } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, subscribeToRun } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { RuntimeConfirmationPanel } from '../components/RuntimeConfirmationPanel';
import { useWorkbenchStore } from '../store/workbench';
import type { ArtifactVersion, AttemptState, FlowRun, GateEvaluation, NodeAttempt, NodeRun, SnapshotFlowNode } from '../types';

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
  window.history.pushState({ flowweaveNodeSession: true }, '', bindingId ? `${base}/${encodeURIComponent(bindingId)}` : base);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

function RunRail({ run, selected, onSelect }: { run: FlowRun; selected?: string; onSelect: (id: string) => void }) {
  const active = run.node_runs.filter(item => item.state === 'ACTIVE').length;
  return <aside className="run-rail"><span className="eyebrow">流程运行</span><h2>{run.name}</h2><span data-testid="flow-run-state" className={`run-state ${run.state.toLowerCase()}`}>{FLOW_STATE_LABELS[run.state] ?? run.state}</span><div className="run-progress"><span>{run.progress.accepted} 已验收</span><span>{run.progress.terminal} 已结束</span><span>{active} 进行中</span></div><div className="run-history-title"><b>节点执行记录</b><small>节点和修订轮次只引用 OpenHands Conversation；全部会话归属于当前 FlowRun Runtime。</small></div><div className="timeline">{run.node_runs.map(item => <button key={item.id} className={selected === item.id ? 'active' : ''} onClick={() => onSelect(item.id)}><i className={String(attemptState(item)).toLowerCase()}/><span><b>{nodeRunName(run, item)}</b><small>第 {nodeVisitNumber(run, item)} 次执行 · {attemptStateLabel(item)}</small></span></button>)}</div></aside>;
}

function SnapshotGraph({ run, selectedKey, onSelect }: { run: FlowRun; selectedKey?: string; onSelect: (key: string) => void }) {
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const [nodes, edges] = useMemo(() => {
    const graphNodes: Node[] = (snapshot?.definition.nodes ?? []).map(item => {
      const visits = run.node_runs.filter(nodeRun => nodeRun.flow_node_snapshot_key === item.instance_key);
      const status = visits.at(-1)?.state.toLowerCase() ?? 'pending';
      return { id: item.instance_key, position: { x: item.position_x, y: item.position_y }, data: { label: <span><b>{item.alias || item.asset.name}</b><small>{visits.length ? `运行 ${visits.length} 次` : '未运行'}</small></span> }, className: `run-graph-node ${status}${selectedKey === item.instance_key ? ' snapshot-selected' : ''}` };
    });
    const directionEdges: Edge[] = (snapshot?.definition.edges ?? []).map((item, index) => ({ id: `flow-${item.id ?? index}`, source: item.source_instance_key, target: item.target_instance_key, className: 'run-direction-edge' }));
    const mappingEdges: Edge[] = (snapshot?.definition.port_mappings ?? []).map((item, index) => ({ id: `mapping-${item.id ?? index}`, source: item.source_instance_key, target: item.target_instance_key, label: `${item.source_output_key} → ${item.target_input_key}`, className: 'run-mapping-edge' }));
    const graphEdges = [...directionEdges, ...mappingEdges];
    return [graphNodes, graphEdges] as const;
  }, [run.node_runs, selectedKey, snapshot]);
  return <section className="run-graph"><header><div><h3>运行快照 v{snapshot?.version ?? '-'}</h3><small>点击任意节点，在右侧配置输入并开始一次独立执行</small></div><span>定义 Hash {snapshot?.definition_hash.slice(0, 8)}</span></header><div className="run-graph-canvas"><ReactFlow key={selectedKey ?? 'node-run'} nodes={nodes} edges={edges} nodesDraggable={false} nodesConnectable={false} fitView onNodeClick={(_, node) => onSelect(node.id)}><Background/><Controls showInteractive={false}/></ReactFlow></div></section>;
}
function GateList({ evaluations }: { evaluations: GateEvaluation[] }) {
  return <div className="gate-results">{evaluations.length ? evaluations.map(item => <div className="gate-result" key={item.id}><span><b>{item.stage} · #{item.policy_position + 1}</b><small>{String(item.result.summary ?? '')}</small></span><strong className={item.decision === 'PASS' ? 'good' : 'bad'}>{item.decision}</strong></div>) : <div className="empty compact">此阶段没有门禁记录。</div>}</div>;
}

function ArtifactList({ artifacts }: { artifacts: ArtifactVersion[] }) {
  return <section className="artifacts"><h3>本轮输出</h3>{artifacts.length ? artifacts.map(item => <article key={item.id}><span className="artifact-version">v{item.version_no}</span><div><b>{item.field_key} · 飞书文档</b><small>{item.content_hash.slice(0, 10)} · URL</small><p>{item.uri}</p><div className="artifact-actions">{item.uri && <a className="ghost" href={item.uri} target="_blank" rel="noreferrer"><ExternalLink size={13}/>打开飞书文档</a>}</div></div></article>) : <div className="empty compact">本轮尚无输出。</div>}</section>;
}

function artifactOptions(run: FlowRun, dataType: string) {
  return run.artifacts.filter(item => item.artifact_type === dataType);
}

function artifactLabel(item: ArtifactVersion) {
  const name = typeof item.metadata?.display_name === 'string' && item.metadata.display_name.trim()
    ? item.metadata.display_name.trim()
    : item.field_key;
  return `${name} · v${item.version_no}`;
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

function ArtifactCreator({ run, field, refresh, onCreated }: { run: FlowRun; field: SnapshotFlowNode['asset']['inputs'][number]; refresh: () => void; onCreated: (artifact: ArtifactVersion) => void }) {
  const [name, setName] = useState(field.display_name || field.field_key);
  const [url, setUrl] = useState('');
  const mutation = useMutation({
    mutationFn: () => api.addArtifact(run.id, {
      field_key: field.field_key, artifact_type: 'URL', uri: url.trim(), mime_type: 'text/uri-list',
      metadata: { source: 'HUMAN_INPUT', display_name: name.trim() || field.display_name || field.field_key },
    }),
    onSuccess: artifact => { setUrl(''); onCreated(artifact); refresh(); },
  });
  return <details className="node-artifact-creator"><summary><Plus size={13}/>新建输入产物</summary><div><label>产物名称<input aria-label={`新建产物名称 ${field.field_key}`} value={name} onChange={event => setName(event.target.value)} placeholder="例如：需求文档（最终版）"/></label><label>实际内容 URL<input type="url" aria-label={`新建产物 URL ${field.field_key}`} value={url} onChange={event => setUrl(event.target.value)} placeholder="https://tenant.feishu.cn/docx/..."/></label><button className="secondary full" disabled={!name.trim() || !url.trim() || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? '保存中…' : '保存到产物池'}</button>{mutation.error && <p className="error">{mutation.error.message}</p>}</div></details>;
}

function FlowRunControls({ run, refresh, navigate }: { run: FlowRun; refresh: () => void; navigate: (result: unknown, kind: string) => void }) {
  const dialog = useProductDialog();
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const mutation = useMutation<unknown, Error, 'complete' | 'cancel' | 'delete'>({
    mutationFn: (kind: 'complete' | 'cancel' | 'delete') => {
      if (kind === 'complete') return api.completeRun(run.id);
      if (kind === 'cancel') return api.cancelRun(run.id);
      return api.deleteRun(run.id);
    },
    onSuccess: (result, kind) => {
      navigate(result, kind);
      if (kind !== 'delete') refresh();
    },
  });
  return <div className="flow-run-management" aria-label="流程运行态管理"><span>流程运行态管理</span><div>{terminal ? <button className="danger" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '永久删除这个流程运行？', message: '相关记录都会被清理且不可恢复。', confirmLabel: '永久删除', tone: 'danger' }).then(ok => ok && mutation.mutate('delete'))}><Trash2 size={14}/>永久删除此运行</button> : <><button className="ghost" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '人工结束整个流程？', message: '未结束的执行会被取消。', confirmLabel: '结束流程' }).then(ok => ok && mutation.mutate('complete'))}>人工结束流程</button><button className="danger" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '取消整个流程？', message: '未结束的执行会被取消。', confirmLabel: '取消流程', tone: 'danger' }).then(ok => ok && mutation.mutate('cancel'))}><StopCircle size={14}/>取消整个流程</button></>}</div>{mutation.error && <p className="error">{mutation.error.message}</p>}</div>;
}

function NodeConsole({ run, node, refresh, onActivated, onSelectExecution }: { run: FlowRun; node: SnapshotFlowNode; refresh: () => void; onActivated: (nodeRun: NodeRun) => void; onSelectExecution: (nodeRun: NodeRun) => void }) {
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<'PROMPT' | 'CHAT'>('CHAT');
  const [prompt, setPrompt] = useState(node.asset.executor?.startup_prompt ?? '');
  useEffect(() => {
    setBindings({}); setMode('CHAT'); setPrompt(node.asset.executor?.startup_prompt ?? '');
  }, [node.instance_key, node.asset.executor?.startup_prompt]);
  const mutation = useMutation({
    mutationFn: async () => {
      const activated = await api.activateNode(run.id, node.instance_key, bindings);
      const created = await waitForStartGate(run.id, activated);
      const attempt = created.attempts.at(-1);
      if (!attempt || attempt.state !== 'WAITING_START_CONFIRMATION') return { created };
      if (mode === 'CHAT') {
        return { created, openDraft: true };
      }
      const started = await api.confirmStart(attempt.id, attempt.state_version, { startup_mode: 'PROMPT', prompt });
      return {
        created: { ...created, attempts: created.attempts.map(item => item.id === started.id ? started : item) },
      };
    },
    onSuccess: result => {
      onActivated(result.created);
      refresh();
      if (result.openDraft) {
        const attempt = result.created.attempts.at(-1);
        if (attempt) openNodeSession(run.id, result.created.id, attempt.id);
      }
    },
  });
  const missing = node.asset.inputs.some(field => !bindings[field.field_key]);
  const invalidMode = mode === 'PROMPT' && !prompt.trim();
  const nodeRuns = run.node_runs.filter(item => item.flow_node_snapshot_key === node.instance_key);
  const visits = nodeRuns.length;
  return <aside className="action-panel node-console"><header><div><b>{node.alias || node.asset.name}</b><small>节点控制台 · 已执行 {visits} 次</small></div></header><div className="action-content">
    <section className="node-console-intro"><span className="eyebrow">ARTIFACT-DRIVEN</span><h3>创建一次独立执行</h3><p>先绑定本次实际输入，再选择启动方式。执行记录引用 FlowRun 内的 OpenHands 会话，不再拥有独立 Runtime。</p></section>
    {nodeRuns.length > 0 && <section className="node-execution-history"><header><h4>已有执行记录</h4><small>可随时查看，且不影响再次启动</small></header>{nodeRuns.map(item => <button key={item.id} onClick={() => onSelectExecution(item)}><span><b>第 {nodeVisitNumber(run, item)} 次执行</b><small>{item.attempts.length} 个轮次 · {attemptStateLabel(item)}</small></span><ExternalLink size={13}/></button>)}</section>}
    <section className="node-console-inputs"><h4>本次输入</h4>{node.asset.inputs.length ? node.asset.inputs.map(field => { const options = artifactOptions(run, field.data_type); const selected = options.find(item => item.id === bindings[field.field_key]); return <article key={field.field_key}><header><span><b>{field.display_name || field.field_key}</b><small>{field.description || field.data_type}</small></span><code>{field.field_key}</code></header><select aria-label={`节点输入 ${field.field_key}`} value={bindings[field.field_key] ?? ''} onChange={event => setBindings(old => ({ ...old, [field.field_key]: event.target.value }))}><option value="">选择已有产物</option>{options.map(item => <option key={item.id} value={item.id}>{artifactLabel(item)} · {item.uri}</option>)}</select>{selected && <div className="selected-artifact"><span><Link2 size={13}/><b>{artifactLabel(selected)}</b></span><a href={selected.uri ?? undefined} target="_blank" rel="noreferrer">{selected.uri || '无外部 URL'}</a></div>}<ArtifactCreator run={run} field={field} refresh={refresh} onCreated={artifact => setBindings(old => ({ ...old, [field.field_key]: artifact.id }))}/></article>; }) : <div className="empty compact">该节点无需输入，可以直接启动。</div>}</section>
    <section className="attempt-startup node-console-start"><h4>启动方式</h4><div className="startup-mode-options"><label><input type="radio" checked={mode === 'PROMPT'} onChange={() => setMode('PROMPT')}/><span><b>发送启动提示词</b><small>创建后立即自动执行</small></span></label><label><input type="radio" checked={mode === 'CHAT'} onChange={() => setMode('CHAT')}/><span><b>仅创建会话启动</b><small>不发送首条任务消息</small></span></label></div>{mode === 'PROMPT' && <label>启动提示词<textarea aria-label="节点启动提示词" value={prompt} onChange={event => setPrompt(event.target.value)}/></label>}</section>
    <button className="primary full node-run-button" disabled={terminal || missing || invalidMode || mutation.isPending} onClick={() => mutation.mutate()}><Play size={15}/>{mutation.isPending ? '正在创建…' : mode === 'CHAT' ? '启动节点会话' : `开始第 ${visits + 1} 次执行`}</button>{terminal && <p className="field-hint">流程已结束，不能创建新的节点执行。</p>}{mutation.error && <p className="error"><AlertTriangle size={14}/>{mutation.error.message}</p>}
  </div></aside>;
}

function AttemptPanel({ run, nodeRun, attempt, refresh, navigate, onCreateNew }: { run: FlowRun; nodeRun: NodeRun; attempt: NodeAttempt; refresh: () => void; navigate: (result: unknown, kind: string) => void; onCreateNew: () => void }) {
  const dialog = useProductDialog();
  const [text, setText] = useState('');
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [startupMode, setStartupMode] = useState<'PROMPT' | 'CHAT'>('PROMPT');
  const attemptSnapshot = run.snapshots.find(item => item.id === attempt.snapshot_id);
  const attemptNode = attemptSnapshot?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
  const [startupPrompt, setStartupPrompt] = useState(attemptNode?.asset.executor?.startup_prompt ?? '');
  useEffect(() => { setStartupMode('PROMPT'); setStartupPrompt(attemptNode?.asset.executor?.startup_prompt ?? ''); }, [attempt.id, attemptNode?.asset.executor?.startup_prompt]);
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const currentBinding = (field: string) => bindings[field] ?? attempt.input_bindings.find(item => item.input_field_key === field)?.artifact_version_id ?? '';
  const mutation = useMutation({ mutationFn: async ({ kind, body }: { kind: string; body?: unknown }) => {
    if (kind === 'confirm') return api.confirmStart(attempt.id, attempt.state_version, body as { startup_mode: 'PROMPT'; prompt?: string });
    if (kind === 'chat') return { openDraft: true };
    if (kind === 'accept') return api.acceptAttempt(attempt.id, attempt.state_version);
    if (kind === 'reject') return api.rejectAttempt(attempt.id, String((body as { reason: string }).reason), attempt.state_version);
    if (kind === 'human') return api.humanInput(attempt.id, String((body as { content: string }).content), attempt.state_version);
    if (kind === 'retry') return api.retryGates(attempt.id, attempt.state_version);
    if (kind === 'retry-cancel') return api.retryRuntimeCancel(attempt.id, attempt.state_version, 'RECONCILE_PARENT');
    if (kind === 'delete-runtime') return api.retryRuntimeCancel(attempt.id, attempt.state_version, 'DELETE_MANAGED_RUNTIME');
    if (kind === 'bind') return api.bindInputs(attempt.id, body as Record<string, string>, attempt.state_version);
    return api.cancelAttempt(attempt.id, attempt.state_version);
  }, onSuccess: (result, variables) => {
    setText('');
    if (variables.kind === 'chat') {
      openNodeSession(run.id, nodeRun.id, attempt.id);
    } else navigate(result, variables.kind);
    refresh();
  } });
  const act = (kind: string, body?: unknown) => mutation.mutate({ kind, body });
  const bindingPayload = Object.fromEntries((attemptNode?.asset.inputs ?? []).map(field => [field.field_key, currentBinding(field.field_key)]).filter(([, value]) => value));
  const attemptTerminal = attempt.state === 'ACCEPTED' || attempt.state === 'REJECTED' || attempt.state === 'CANCELLED';
  return <aside className="action-panel attempt-control"><header><div><b>{nodeRunName(run, nodeRun)}</b><small>第 {nodeVisitNumber(run, nodeRun)} 次执行 / 第 {attempt.attempt_no} 轮</small></div></header><div className="action-content">{!terminal && <button className="primary full create-another-run" onClick={onCreateNew}><Plus size={15}/>创建新的独立执行</button>}<div className="state-banner"><span>当前轮次状态</span><b>{ATTEMPT_STATE_LABELS[attempt.state] ?? attempt.state}</b><small><span data-testid="attempt-state">{attempt.state}</span> · 状态版本 {attempt.state_version}</small></div>
    {attempt.runtime_phase === 'CANCEL_FAILED' && <section className="terminal-run-panel"><h4>Agent 停止状态未确认</h4><p>{attempt.error_detail || '运行时停止失败，需要重新对账。FlowRun Runtime 的健康、替换和诊断入口位于会话工作台。'}</p>{attempt.runtime_cancel_recovery_modes.includes('RECONCILE_PARENT') && <button className="secondary full" disabled={mutation.isPending} onClick={() => act('retry-cancel')}>重新对账并重试停止</button>}</section>}
    <button className="secondary full node-session-entry" onClick={() => openNodeSession(run.id, nodeRun.id, attempt.id)}><Send size={15}/>进入节点会话</button>
    {nodeRun.attempts.length > 1 && <section className="attempt-switcher"><h4>修订轮次</h4><div>{nodeRun.attempts.map(item => <button key={item.id} className={item.id === attempt.id ? 'active' : ''} onClick={() => useWorkbenchStore.getState().selectAttempt(item.id)}>第 {item.attempt_no} 轮</button>)}</div></section>}
    <section className="attempt-side-section"><h4>本轮冻结输入</h4>{attempt.input_bindings.length ? attempt.input_bindings.map(item => { const boundArtifact = run.artifacts.find(artifactItem => artifactItem.id === item.artifact_version_id); const contract = attemptNode?.asset.inputs.find(field => field.field_key === item.input_field_key); return <article className="attempt-input-card" key={item.id}><header><span><b>{contract?.display_name || item.input_field_key}</b><small>{item.input_field_key}</small></span><span className="artifact-version">{boundArtifact ? `v${boundArtifact.version_no}` : '失效'}</span></header><strong>{boundArtifact ? artifactLabel(boundArtifact) : '产物不可用'}</strong><a href={boundArtifact?.uri ?? undefined} target="_blank" rel="noreferrer">{boundArtifact?.uri || '无外部 URL'}</a></article>; }) : <div className="empty compact">本轮没有输入绑定。</div>}</section>
    <section className="attempt-side-section"><h4>门禁结果</h4><GateList evaluations={attempt.gate_evaluations}/></section>
    <section className="attempt-side-section attempt-side-artifacts"><ArtifactList artifacts={attempt.artifacts}/></section>
    {terminal ? <section className="terminal-run-panel"><h4>{run.state === 'CANCELLED' ? '流程已取消' : '流程已完成'}</h4><p>运行已进入只读终态，历史记录继续保留。流程级操作位于上方“流程运行态管理”。</p></section> : <>
      {attempt.state === 'WAITING_START_CONFIRMATION' && <section className="attempt-startup"><h4>启动这条执行记录</h4><p>会话归属于 FlowRun；选择“仅创建会话”会显式新建一个 OpenHands Conversation。</p><div className="startup-mode-options"><label><input type="radio" checked={startupMode === 'PROMPT'} onChange={() => setStartupMode('PROMPT')}/><span><b>发送启动提示词</b></span></label><label><input type="radio" checked={startupMode === 'CHAT'} onChange={() => setStartupMode('CHAT')}/><span><b>仅创建会话启动</b></span></label></div>{startupMode === 'PROMPT' && <label>启动提示词<textarea value={startupPrompt} onChange={event => setStartupPrompt(event.target.value)}/></label>}<button className="primary full" disabled={mutation.isPending || (startupMode === 'PROMPT' && !startupPrompt.trim())} onClick={() => { if (startupMode === 'CHAT') act('chat'); else act('confirm', { startup_mode: 'PROMPT', prompt: startupPrompt }); }}>确认启动</button></section>}
      {attempt.state === 'WAITING_HUMAN' && <><label>人工输入<textarea value={text} onChange={event => setText(event.target.value)}/></label><button className="primary full" disabled={!text} onClick={() => act('human', { content: text })}>提交并继续</button></>}
      {attempt.state === 'WAITING_CONFIRMATION' && <RuntimeConfirmationPanel attempt={attempt} onResolved={refresh}/>}
      {attempt.state === 'WAITING_ACCEPTANCE' && <><label>验收意见<textarea value={text} onChange={event => setText(event.target.value)} placeholder="退回时填写修改要求"/></label><button className="primary full" onClick={() => act('accept')}>确认完成</button><button className="secondary full" disabled={!text} onClick={() => act('reject', { reason: text })}>退回修改</button></>}
      {(attempt.state === 'WAITING_INPUT' || attempt.state === 'START_BLOCKED') && <section className="input-binding-editor"><h4>修正本轮输入</h4>{attemptNode?.asset.inputs.map(field => <label key={field.field_key}>{field.display_name}<select value={currentBinding(field.field_key)} onChange={event => setBindings(old => ({ ...old, [field.field_key]: event.target.value }))}><option value="">未绑定</option>{artifactOptions(run, field.data_type).map(item => <option key={item.id} value={item.id}>{artifactLabel(item)} · {item.uri}</option>)}</select></label>)}<button className="secondary full" disabled={!Object.keys(bindingPayload).length} onClick={() => act('bind', bindingPayload)}>保存输入绑定</button>{attempt.state === 'START_BLOCKED' && <button className="secondary full" onClick={() => act('retry')}>重试门禁</button>}</section>}
      {!attemptTerminal && <button className="danger full cancel-attempt-button" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '取消当前节点的本轮执行？', message: '只会取消这个节点的当前轮次，其他节点执行和整个流程不会被取消。', confirmLabel: '取消本轮执行', tone: 'danger' }).then(ok => ok && act('cancel'))}><StopCircle size={15}/>取消本轮节点执行</button>}
    </>}{mutation.error && <p className="error">{mutation.error.message}</p>}</div></aside>;
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
  const { selectedRunId, selectedNodeRunId, selectedAttemptId, selectAttempt, selectExecution, setView } = useWorkbenchStore();
  const [selectedNodeKey, setSelectedNodeKey] = useState<string>();
  const query = useQuery({ queryKey: ['flow-run', selectedRunId], queryFn: () => api.flowRun(selectedRunId!), enabled: Boolean(selectedRunId), refetchInterval: 5000 });
  const flowId = query.data?.flow_definition_id;
  const flow = useQuery({ queryKey: ['flow', flowId], queryFn: () => api.flow(flowId!), enabled: Boolean(flowId), refetchInterval: 5000 });
  const refresh = useCallback(() => { if (selectedRunId) void qc.invalidateQueries({ queryKey: ['flow-run', selectedRunId] }); if (flowId) void qc.invalidateQueries({ queryKey: ['flow', flowId] }); void qc.invalidateQueries({ queryKey: ['runs'] }); }, [flowId, qc, selectedRunId]);
  useEffect(() => setSelectedNodeKey(undefined), [selectedRunId]);
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
  const hasPanel = Boolean((nodeRun && attempt) || selectedNode);
  const selectGraphNode = (key: string) => {
    setSelectedNodeKey(key);
    useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined });
  };
  const selectHistory = (id: string) => {
    const item = run.node_runs.find(candidate => candidate.id === id);
    setSelectedNodeKey(undefined);
    selectExecution(id, item?.attempts.at(-1)?.id);
  };
  return <section className={`workbench-page${hasPanel ? '' : ' no-action-panel'}`}><RunRail run={run} selected={nodeRun?.id} onSelect={selectHistory}/><main className="run-main"><button className="back" onClick={returnToRuns}><ArrowLeft size={14}/>返回运行列表</button><header className="run-title"><div><span className="eyebrow">第 {run.run_no} 次流程运行</span><h1>{run.name}</h1><p>流程快照 v{run.active_snapshot_version} · {run.progress.accepted}/{run.node_runs.length} 次节点执行已验收</p></div><div className="run-title-actions"><span className={`run-state ${run.state.toLowerCase()}`}>{FLOW_STATE_LABELS[run.state] ?? run.state}</span><FlowRunControls run={run} refresh={refresh} navigate={navigate}/></div></header>{run.state !== 'COMPLETED' && run.state !== 'CANCELLED' && <SnapshotSync run={run} currentVersion={flow.data?.row_version} onSynced={updated => navigate(updated, 'sync')}/>}<SnapshotGraph run={run} selectedKey={selectedNodeKey} onSelect={selectGraphNode}/>
    </main>{nodeRun && attempt ? <AttemptPanel run={run} nodeRun={nodeRun} attempt={attempt} refresh={refresh} navigate={navigate} onCreateNew={() => { setSelectedNodeKey(nodeRun.flow_node_snapshot_key); useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined }); }}/> : selectedNode ? <NodeConsole run={run} node={selectedNode} refresh={refresh} onActivated={created => { setSelectedNodeKey(undefined); navigate(created, 'activate'); }} onSelectExecution={item => { setSelectedNodeKey(undefined); selectExecution(item.id, item.attempts.at(-1)?.id); }}/> : null}</section>;
}
