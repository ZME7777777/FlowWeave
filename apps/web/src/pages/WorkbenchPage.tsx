import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { AlertTriangle, ArrowLeft, Download, ExternalLink, Eye, FileText, Play, Plus, RefreshCw, Send, StopCircle, Trash2, Upload, X } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { api, artifactContentUrl, subscribeToRun } from '../api/client';
import { flowMappingEdgeTypes, withMappingLabelOffsets } from '../components/FlowMappingEdge';
import { useProductDialog } from '../components/ProductDialogContext';
import { RuntimeConfirmationPanel } from '../components/RuntimeConfirmationPanel';
import { useEscapeClose } from '../components/useEscapeClose';
import { useWorkbenchStore } from '../store/workbench';
import type { ArtifactVersion, AttemptState, FlowRun, GateEvaluation, GatePolicy, NodeAttempt, NodeRun, SnapshotFlowNode } from '../types';

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
const GRAPH_RENDER_REVISION = '2026-08-31.2';

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

type SnapshotGraphNodeData = {
  label: string;
  status: string;
  visits: number;
  inputs: SnapshotFlowNode['asset']['inputs'];
  outputs: SnapshotFlowNode['asset']['outputs'];
};

function SnapshotGraphNode({ data, selected }: NodeProps<Node<SnapshotGraphNodeData>>) {
  return <article className={`run-graph-node ${data.status}${selected ? ' snapshot-selected' : ''}`}>
    <Handle id="flow-target" className="run-flow-handle" type="target" position={Position.Left} style={{ top: 18 }}/>
    <Handle id="flow-source" className="run-flow-handle" type="source" position={Position.Right} style={{ top: 18 }}/>
    <header><b>{data.label}</b><small>{data.visits ? `运行 ${data.visits} 次` : '未运行'}</small></header>
    <div className="run-node-contract">
      <section aria-label="输入端口"><span>输入</span>{data.inputs.length ? data.inputs.map(field => <div key={field.field_key}><Handle id={`input:${field.field_key}`} className="run-data-handle input" type="target" position={Position.Left} style={{ top: '50%' }}/><b>{field.display_name || field.field_key}</b><small>{field.data_type}</small></div>) : <em>无输入</em>}</section>
      <section aria-label="输出端口"><span>输出</span>{data.outputs.length ? data.outputs.map(field => <div key={field.field_key}><b>{field.display_name || field.field_key}</b><small>{field.data_type}</small><Handle id={`output:${field.field_key}`} className="run-data-handle output" type="source" position={Position.Right} style={{ top: '50%' }}/></div>) : <em>无输出</em>}</section>
    </div>
  </article>;
}

const runSnapshotNodeTypes = { snapshotNode: SnapshotGraphNode };

function SnapshotGraph({ run, selectedKey, onSelect }: { run: FlowRun; selectedKey?: string; onSelect: (key: string) => void }) {
  const [linkMode, setLinkMode] = useState<'flow' | 'data'>('flow');
  const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  const [nodes, edges] = useMemo(() => {
    const graphNodes: Node<SnapshotGraphNodeData>[] = (snapshot?.definition.nodes ?? []).map(item => {
      const visits = run.node_runs.filter(nodeRun => nodeRun.flow_node_snapshot_key === item.instance_key);
      const status = visits.at(-1)?.state.toLowerCase() ?? 'pending';
      return { id: item.instance_key, type: 'snapshotNode', selected: item.instance_key === selectedKey, position: { x: item.position_x, y: item.position_y }, data: { label: item.alias || item.asset.name, status, visits: visits.length, inputs: item.asset.inputs, outputs: item.asset.outputs } };
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
  }, [linkMode, run.node_runs, selectedKey, snapshot]);
  const graphKey = `${GRAPH_RENDER_REVISION}:${snapshot?.id ?? 'snapshot'}:${snapshot?.definition_hash ?? ''}:${selectedKey ?? 'node-run'}`;
  return <section className="run-graph" data-graph-render-revision={GRAPH_RENDER_REVISION}><header><div><h3>运行快照 v{snapshot?.version ?? '-'}</h3><small>实线表示流程走向；蓝色虚线表示冻结的输出 → 输入映射。</small></div><div className="flow-link-mode run-link-mode" aria-label="运行图连线模式"><button type="button" className={linkMode === 'flow' ? 'active' : ''} aria-pressed={linkMode === 'flow'} onClick={() => setLinkMode('flow')}>流程走向</button><button type="button" className={linkMode === 'data' ? 'active' : ''} aria-pressed={linkMode === 'data'} onClick={() => setLinkMode('data')}>产物流转</button></div><span>定义 Hash {snapshot?.definition_hash.slice(0, 8)}</span></header><div className="run-graph-canvas"><ReactFlow key={graphKey} nodeTypes={runSnapshotNodeTypes} edgeTypes={flowMappingEdgeTypes} nodes={nodes} edges={edges} nodesDraggable={false} nodesConnectable={false} fitView onNodeClick={(_, node) => onSelect(node.id)}><Background/><Controls showInteractive={false}/></ReactFlow></div></section>;
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

function NodeInputDialog({ run, node, initialBindings = {}, onClose, onSubmit }: { run: FlowRun; node: SnapshotFlowNode; initialBindings?: Record<string, string>; onClose: () => void; onSubmit: (bindings: Record<string, string>) => void }) {
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
      for (const field of fields) {
        const retained = initialBindings[field.field_key];
        if (field.data_type === 'URL') {
          const value = urls[field.field_key]?.trim();
          if (!value && retained) { bindings[field.field_key] = retained; continue; }
          if (!value) throw new Error(`请填写“${field.display_name || field.field_key}”。`);
          const artifact = await api.addNodeInputArtifact(run.id, node.instance_key, {
            field_key: field.field_key, artifact_type: 'URL', uri: value, mime_type: 'text/uri-list',
            metadata: { display_name: field.display_name || field.field_key },
          });
          bindings[field.field_key] = artifact.id;
          continue;
        }
        const file = files[field.field_key];
        if (!file && retained) { bindings[field.field_key] = retained; continue; }
        if (!file) throw new Error(`请上传“${field.display_name || field.field_key}”。`);
        const artifact = await api.uploadNodeInputArtifact(run.id, node.instance_key, field.field_key, field.display_name || field.field_key, file);
        bindings[field.field_key] = artifact.id;
      }
      return bindings;
    },
    onSuccess: bindings => onSubmit(bindings),
    onError: reason => setError(reason instanceof Error ? reason.message : '保存输入失败'),
  });
  return <div className="modal-backdrop"><section className="modal node-input-dialog" role="dialog" aria-modal="true" aria-label="填写节点输入"><header><div><span className="eyebrow">NODE INPUTS</span><h2>填写 {node.alias || node.asset.name} 的输入</h2><p>字段由节点定义冻结生成；每个值只绑定当前节点，不会进入可复用产物池。</p></div><button type="button" className="ghost" aria-label="关闭输入表单" onClick={onClose}><X size={17}/></button></header><div className="node-input-dialog-body">{fields.map(field => {
    const current = run.artifacts.find(item => item.id === initialBindings[field.field_key]);
    return <label className="node-input-control" key={field.field_key}><span><b>{field.display_name || field.field_key}</b><code>{field.field_key} · {field.data_type}</code><small>{field.description || '请按节点定义填写此字段。'}</small></span>{field.data_type === 'URL' ? <input aria-label={`填写输入 ${field.field_key}`} type="url" value={urls[field.field_key] ?? ''} onChange={event => setUrls(old => ({ ...old, [field.field_key]: event.target.value }))} placeholder="https://example.com/resource"/> : <span className="platform-file-upload"><Upload size={16}/><span><b>{files[field.field_key]?.name || (typeof current?.metadata?.filename === 'string' ? current.metadata.filename : '选择文件')}</b><small>{files[field.field_key] ? `${files[field.field_key]?.size} B` : '图片、PDF、文档、压缩包等，最大 25 MiB'}</small></span><input aria-label={`上传输入文件 ${field.field_key}`} type="file" onChange={event => setFiles(old => ({ ...old, [field.field_key]: event.target.files?.[0] }))}/></span>}{current && <a className="current-input" href={artifactHref(current)} target="_blank" rel="noreferrer">当前绑定：{artifactLabel(current)}</a>}</label>; })}{!fields.length && <div className="empty compact">该节点没有定义输入，可直接创建执行。</div>}</div>{error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? '保存输入中…' : '保存输入并继续'}</button></footer></section></div>;
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

type GateDraftDialog = { index?: number; gate: GatePolicy; readOnly: boolean };

const gateText = (gate: GatePolicy) => String(gate.gate_type === 'PYTHON' ? gate.config.code || '' : gate.config.prompt || '');

function GateDraftDialog({ draft, onClose, onSave }: { draft: GateDraftDialog; onClose: () => void; onSave: (gate: GatePolicy) => void }) {
  const [gate, setGate] = useState(draft.gate);
  const [error, setError] = useState('');
  useEscapeClose(onClose);
  const editable = !draft.readOnly;
  const contentLabel = gate.gate_type === 'PYTHON' ? 'Python 脚本' : '判定提示词';
  const accept = gate.gate_type === 'PYTHON' ? '.py,text/x-python' : '.md,.txt,text/markdown,text/plain';
  const importFile = async (file?: File) => {
    if (!file) return;
    const extension = file.name.toLowerCase().split('.').at(-1);
    const supported = gate.gate_type === 'PYTHON' ? extension === 'py' : extension === 'md' || extension === 'txt';
    if (!supported) { setError(gate.gate_type === 'PYTHON' ? 'Python 门禁仅支持导入 .py 文件。' : '提示词门禁仅支持导入 .md 或 .txt 文件。'); return; }
    const text = await file.text();
    setGate(current => ({ ...current, config: gate.gate_type === 'PYTHON' ? { ...current.config, code: text, script_filename: file.name } : { ...current.config, prompt: text, source_filename: file.name } }));
    setError('');
  };
  const save = () => {
    if (!gateText(gate).trim()) { setError(`请填写或导入${contentLabel}。`); return; }
    onSave(gate);
  };
  return <div className="modal-backdrop"><section className="modal gate-draft-dialog" role="dialog" aria-modal="true" aria-label={`${editable ? '编辑' : '查看'}${contentLabel}`}><header><div><span className="eyebrow">{gate.stage === 'START' ? 'START GATE' : 'END GATE'}</span><h2>{editable ? `编辑${contentLabel}` : `查看${contentLabel}`}</h2></div><button type="button" className="ghost" aria-label="关闭门禁弹窗" onClick={onClose}><X size={17}/></button></header><div className="gate-draft-dialog-body"><label>门禁类型<select aria-label="门禁类型" value={gate.gate_type} disabled={!editable} onChange={event => { const gateType = event.target.value as GatePolicy['gate_type']; setGate(current => ({ ...current, gate_type: gateType, config: gateType === 'PYTHON' ? { code: '', script_filename: '' } : { prompt: '' } })); setError(''); }}><option value="PROMPT">Prompt 模型判断</option><option value="PYTHON">Python 脚本</option></select></label><label>超时（秒）<input type="number" min="1" max="300" disabled={!editable} value={gate.timeout_seconds} onChange={event => setGate(current => ({ ...current, timeout_seconds: Number(event.target.value) || 30 }))}/></label>{editable && <label className="gate-file-import"><span><Upload size={15}/>从文件导入</span><small>{gate.gate_type === 'PYTHON' ? '仅支持 .py 文件' : '仅支持 .md 或 .txt 文件'}</small><input aria-label={`导入${contentLabel}文件`} type="file" accept={accept} onChange={event => void importFile(event.target.files?.[0])}/></label>}<label className="gate-content-field">{contentLabel}<textarea aria-label={contentLabel} className={gate.gate_type === 'PYTHON' ? 'code' : ''} readOnly={!editable} value={gateText(gate)} onChange={event => setGate(current => ({ ...current, config: current.gate_type === 'PYTHON' ? { ...current.config, code: event.target.value } : { ...current.config, prompt: event.target.value } }))}/></label></div>{error && <p className="error">{error}</p>}<footer>{editable ? <><button type="button" className="ghost" onClick={onClose}>取消</button><button type="button" className="primary" onClick={save}>保存门禁</button></> : <button type="button" className="primary" onClick={onClose}>完成</button>}</footer></section></div>;
}

function GateDraftEditor({ gates, onChange }: { gates: GatePolicy[]; onChange: (gates: GatePolicy[]) => void }) {
  const [dialog, setDialog] = useState<GateDraftDialog>();
  useEscapeClose(() => setDialog(undefined), Boolean(dialog));
  const renumber = (items: GatePolicy[]) => items.map((gate, index, all) => ({ ...gate, position: all.slice(0, index).filter(candidate => candidate.stage === gate.stage).length }));
  const openNew = (stage: 'START' | 'END') => setDialog({ gate: { stage, position: gates.filter(gate => gate.stage === stage).length, gate_type: 'PROMPT', enabled: true, timeout_seconds: 30, config: { prompt: '' } }, readOnly: false });
  const save = (gate: GatePolicy) => {
    onChange(dialog?.index === undefined ? [...gates, gate] : gates.map((item, index) => index === dialog.index ? gate : item));
    setDialog(undefined);
  };
  const remove = (index: number) => onChange(renumber(gates.filter((_, itemIndex) => itemIndex !== index)));
  return <section className="attempt-side-section gate-draft-editor"><p className="field-hint">门禁只应用于即将创建的这一次执行；创建后将冻结为本轮的门禁结果依据。</p>{(['START', 'END'] as const).map(stage => <section key={stage} className="gate-draft-stage"><header><h4>{stage === 'START' ? '开始门禁' : '结束门禁'}</h4><button type="button" className="secondary" onClick={() => openNew(stage)}><Plus size={13}/>添加门禁</button></header><div className="gate-draft-list">{gates.filter(gate => gate.stage === stage).map(gate => {
    const index = gates.indexOf(gate); const content = gateText(gate).trim();
    return <article key={`${stage}-${gate.position}`}><button type="button" className="gate-draft-open" onClick={() => setDialog({ index, gate, readOnly: true })}><span><b>{gate.gate_type === 'PYTHON' ? 'Python 脚本' : 'Prompt 模型判断'}</b><small>{content ? content.replace(/\s+/g, ' ').slice(0, 72) : '尚未填写内容'}</small></span><Eye size={14}/></button><div><button type="button" className="ghost" onClick={() => setDialog({ index, gate, readOnly: false })}>编辑</button><button type="button" className="ghost gate-delete" onClick={() => remove(index)}><Trash2 size={14}/>删除</button></div></article>;
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
  useEscapeClose(() => setViewing(undefined));
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

function NodeConsole({ run, node, refresh, onActivated, onSelectExecution }: { run: FlowRun; node: SnapshotFlowNode; refresh: () => void; onActivated: (nodeRun: NodeRun) => void; onSelectExecution: (nodeRun: NodeRun) => void }) {
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const [topTab, setTopTab] = useState<'PROMPT' | 'CHAT'>('PROMPT');
  const [promptTab, setPromptTab] = useState<'inputs' | 'gates' | 'history'>('inputs');
  const [inputDialogOpen, setInputDialogOpen] = useState(false);
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [gates, setGates] = useState<GatePolicy[]>([]);
  const [contextIds, setContextIds] = useState<string[]>([]);
  const [mode, setMode] = useState<'PROMPT' | 'CHAT'>('PROMPT');
  const [prompt, setPrompt] = useState(node.asset.executor?.startup_prompt ?? '');
  useEffect(() => { setTopTab('PROMPT'); setPromptTab('inputs'); setInputDialogOpen(false); setPromptDialogOpen(false); setBindings({}); setGates([]); setContextIds([]); setMode('PROMPT'); setPrompt(node.asset.executor?.startup_prompt ?? ''); }, [node.instance_key, node.asset.executor?.startup_prompt]);
  useEscapeClose(() => setPromptDialogOpen(false), promptDialogOpen);
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
        sessionOnly ? [] : contextIds,
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
  const invalidGates = mode === 'PROMPT' && gates.some(gate => gate.gate_type === 'PYTHON' && !String(gate.config.code || '').trim());
  const missingInputs = node.asset.inputs.some(field => !bindings[field.field_key]);
  const nodeRuns = run.node_runs.filter(item => item.flow_node_snapshot_key === node.instance_key);
  const visits = nodeRuns.length;
  const selectTopTab = (next: 'PROMPT' | 'CHAT') => { setTopTab(next); setMode(next); };
  const runAction = <button className="primary node-run-button" disabled={terminal || invalidMode || invalidGates || mutation.isPending} onClick={() => mode === 'PROMPT' && missingInputs ? setPromptTab('inputs') : mutation.mutate(bindings)}><Play size={15}/>{mutation.isPending ? '正在创建…' : mode === 'PROMPT' && missingInputs ? '请先填写节点输入' : mode === 'CHAT' ? '启动节点会话' : '开始节点执行'}</button>;
  const history = <section className="node-execution-history"><header><h4>执行记录</h4><small>可随时查看，且不影响再次启动</small></header>{nodeRuns.length ? nodeRuns.map(item => <button key={item.id} onClick={() => onSelectExecution(item)}><span><b>第 {nodeVisitNumber(run, item)} 次执行</b><small>{item.attempts.length} 个轮次 · {attemptStateLabel(item)}</small></span><ExternalLink size={13}/></button>) : <p className="field-hint">还没有执行记录。</p>}</section>;
  return <aside className="action-panel node-console"><header><div><b>{node.alias || node.asset.name}</b><small>节点控制台 · 已执行 {visits} 次</small></div></header><div className="node-console-mode-bar"><nav className="node-console-mode-tabs" aria-label="启动方式"><button className={topTab === 'PROMPT' ? 'active' : ''} onClick={() => selectTopTab('PROMPT')}><span>提示词执行</span><small>按节点配置自动执行</small></button><button className={topTab === 'CHAT' ? 'active' : ''} onClick={() => selectTopTab('CHAT')}><span>会话启动</span><small>人工进入会话引导</small></button></nav>{runAction}</div><div className="action-content">
    {topTab === 'PROMPT' && <><section className="node-console-mode-notice"><b>提示词执行</b><small>输入、门禁和上下文只作用于即将创建的这一次执行。</small></section><nav className="attempt-detail-tabs node-console-subtabs" aria-label="提示词执行配置"><button className={promptTab === 'inputs' ? 'active' : ''} onClick={() => setPromptTab('inputs')}>输入与上下文</button><button className={promptTab === 'gates' ? 'active' : ''} onClick={() => setPromptTab('gates')}>门禁配置</button><button className={promptTab === 'history' ? 'active' : ''} onClick={() => setPromptTab('history')}>执行记录</button></nav>{promptTab === 'inputs' && <><InputSummary fields={node.asset.inputs} bindings={bindings} artifacts={run.artifacts}/>{node.asset.inputs.length > 0 && <button className="secondary full" onClick={() => setInputDialogOpen(true)}><Upload size={14}/>填写节点输入</button>}<section className="attempt-side-section startup-prompt-summary"><header><div><h4>启动提示词</h4><small>本次执行创建后会冻结。</small></div></header><p title={prompt.trim() || undefined}>{prompt.trim() || '尚未填写启动提示词。'}</p><footer><button type="button" className="secondary" onClick={() => setPromptDialogOpen(true)}>编辑</button></footer></section><NodeContextSummary node={node} contextIds={contextIds} editable onChange={setContextIds} mode="PROMPT"/></>}{promptTab === 'gates' && <GateDraftEditor gates={gates} onChange={setGates}/>} {promptTab === 'history' && history}</>}
    {topTab === 'CHAT' && <><section className="node-console-mode-notice chat"><b>仅创建会话启动</b><small>不应用节点输入、门禁、流程输出或端口映射；进入会话后由你自行引导 AI。</small></section>{history}</>}
    {invalidGates && <p className="error">每个 Python 门禁都需要填写脚本。</p>}{terminal && <p className="field-hint">流程已结束，不能创建新的节点执行。</p>}{mutation.error && <p className="error"><AlertTriangle size={14}/>{mutation.error.message}</p>}
  </div>{inputDialogOpen && <NodeInputDialog run={run} node={node} initialBindings={bindings} onClose={() => setInputDialogOpen(false)} onSubmit={nextBindings => { setBindings(nextBindings); setInputDialogOpen(false); }}/>} {promptDialogOpen && <div className="modal-backdrop"><section className="modal startup-prompt-dialog" role="dialog" aria-modal="true" aria-label="编辑启动提示词"><header><div><span className="eyebrow">STARTUP PROMPT</span><h2>编辑启动提示词</h2></div><button type="button" className="ghost" aria-label="关闭启动提示词编辑" onClick={() => setPromptDialogOpen(false)}><X size={17}/></button></header><label>启动提示词<textarea aria-label="节点启动提示词" value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="输入发送给 AI 的启动提示词"/></label><footer><button type="button" className="ghost" onClick={() => setPromptDialogOpen(false)}>取消</button><button type="button" className="primary" onClick={() => setPromptDialogOpen(false)}>完成</button></footer></section></div>}</aside>;
}

function AttemptPanel({ run, nodeRun, attempt, refresh, navigate, onCreateNew }: { run: FlowRun; nodeRun: NodeRun; attempt: NodeAttempt; refresh: () => void; navigate: (result: unknown, kind: string) => void; onCreateNew: () => void }) {
  const dialog = useProductDialog();
  const [text, setText] = useState('');
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [tab, setTab] = useState<'overview' | 'gates' | 'outputs'>('overview');
  const [inputDialogOpen, setInputDialogOpen] = useState(false);
  const attemptSnapshot = run.snapshots.find(item => item.id === attempt.snapshot_id);
  const activeSnapshot = run.snapshots.find(item => item.id === run.active_snapshot_id) ?? run.snapshots.at(-1);
  // Older attempts can reference snapshots that were compacted from the run
  // payload. Their contracts are still best represented by the active frozen
  // run snapshot, rather than an empty result panel.
  const attemptNode = (attemptSnapshot ?? activeSnapshot)?.definition.nodes.find(item => item.instance_key === nodeRun.flow_node_snapshot_key);
  useEffect(() => { setTab('overview'); setInputDialogOpen(false); }, [attempt.id]);
  const terminal = run.state === 'COMPLETED' || run.state === 'CANCELLED';
  const currentBinding = (field: string) => bindings[field] ?? attempt.input_bindings.find(item => item.input_field_key === field)?.artifact_version_id ?? '';
  const mutation = useMutation({ mutationFn: async ({ kind, body }: { kind: string; body?: unknown }) => {
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
  return <aside className="action-panel attempt-control"><header><div><b>{nodeRunName(run, nodeRun)}</b><small>第 {nodeVisitNumber(run, nodeRun)} 次执行 / 第 {attempt.attempt_no} 轮</small></div></header><nav className="attempt-detail-tabs" aria-label="执行详情"><button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>概览</button><button className={tab === 'gates' ? 'active' : ''} onClick={() => setTab('gates')}>门禁结果</button><button className={tab === 'outputs' ? 'active' : ''} onClick={() => setTab('outputs')}>输出</button></nav><div className="action-content">{tab === 'overview' && <>{!terminal && <button className="primary full create-another-run" onClick={onCreateNew}><Plus size={15}/>创建新的独立执行</button>}<div className="state-banner"><span>当前轮次状态</span><b>{ATTEMPT_STATE_LABELS[attempt.state] ?? attempt.state}</b><small><span data-testid="attempt-state">{attempt.state}</span> · 状态版本 {attempt.state_version}</small></div>{attemptNode && <NodeContextSummary node={attemptNode} contextIds={attempt.context_ids ?? null} mode={attempt.startup_mode === 'CHAT' ? 'CHAT' : 'PROMPT'}/>}<InputSummary fields={attemptNode?.asset.inputs ?? []} bindings={nodeInputBindings} artifacts={run.artifacts}/>{editableInputs && <button className="secondary full" onClick={() => setInputDialogOpen(true)}><Play size={14}/>编辑本轮输入</button>}{!editableInputs && <p className="field-hint">输入已随本轮启动冻结，仅供查看。</p>}
    {attempt.runtime_phase === 'CANCEL_FAILED' && <section className="terminal-run-panel"><h4>Agent 停止状态未确认</h4><p>{attempt.error_detail || '运行时停止失败，需要重新对账。FlowRun Runtime 的健康、替换和诊断入口位于会话工作台。'}</p>{attempt.runtime_cancel_recovery_modes.includes('RECONCILE_PARENT') && <button className="secondary full" disabled={mutation.isPending} onClick={() => act('retry-cancel')}>重新对账并重试停止</button>}</section>}
    <button className="secondary full node-session-entry" onClick={() => openNodeSession(run.id, nodeRun.id, attempt.id)}><Send size={15}/>进入节点会话</button>
    {nodeRun.attempts.length > 1 && <section className="attempt-switcher"><h4>修订轮次</h4><div>{nodeRun.attempts.map(item => <button key={item.id} className={item.id === attempt.id ? 'active' : ''} onClick={() => useWorkbenchStore.getState().selectAttempt(item.id)}>第 {item.attempt_no} 轮</button>)}</div></section>}
    {terminal ? <section className="terminal-run-panel"><h4>{run.state === 'CANCELLED' ? '流程已取消' : '流程已完成'}</h4><p>运行已进入只读终态，历史记录继续保留。流程级操作位于上方“流程运行态管理”。</p></section> : <>
      {attempt.state === 'WAITING_HUMAN' && <><label>人工输入<textarea value={text} onChange={event => setText(event.target.value)}/></label><button className="primary full" disabled={!text} onClick={() => act('human', { content: text })}>提交并继续</button></>}
      {attempt.state === 'WAITING_CONFIRMATION' && <RuntimeConfirmationPanel attempt={attempt} onResolved={refresh}/>}
      {attempt.state === 'WAITING_ACCEPTANCE' && <><label>验收意见<textarea value={text} onChange={event => setText(event.target.value)} placeholder="退回时填写修改要求"/></label><button className="primary full" onClick={() => act('accept')}>确认完成</button><button className="secondary full" disabled={!text} onClick={() => act('reject', { reason: text })}>退回修改</button></>}
      {attempt.state === 'START_BLOCKED' && <button className="secondary full" onClick={() => act('retry')}>重试门禁</button>}
      {!attemptTerminal && <button className="danger full cancel-attempt-button" disabled={mutation.isPending} onClick={() => void dialog.confirm({ title: '取消当前节点的本轮执行？', message: '只会取消这个节点的当前轮次，其他节点执行和整个流程不会被取消。', confirmLabel: '取消本轮执行', tone: 'danger' }).then(ok => ok && act('cancel'))}><StopCircle size={15}/>取消本轮节点执行</button>}
    </>}</>}{tab === 'gates' && <section className="attempt-side-section"><h4>门禁结果</h4><GateList evaluations={attempt.gate_evaluations} policies={gatePolicies}/></section>}{tab === 'outputs' && <section className="attempt-side-section attempt-side-artifacts"><ArtifactList artifacts={attempt.artifacts} expectedFields={attemptNode?.asset.outputs ?? []}/></section>}{mutation.error && <p className="error">{mutation.error.message}</p>}</div>{inputDialogOpen && attemptNode && <NodeInputDialog run={run} node={attemptNode} initialBindings={nodeInputBindings} onClose={() => setInputDialogOpen(false)} onSubmit={nextBindings => { setInputDialogOpen(false); setBindings(nextBindings); act('bind', nextBindings); }}/>}</aside>;
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
  const [sidePanelWidth, setSidePanelWidth] = useState(390);
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
      return;
    }
    // Opening a Run from the list has no persisted node selection.  Treat the
    // first node in its active snapshot as selected, rather than using the
    // canvas-only state that hides both workbench sidebars until a click.
    // An explicitly selected execution always wins (for deep links and Back).
    if (!selectedNodeRunId && !selectedNodeKey) {
      const snapshot = run.snapshots.find(item => item.id === run.active_snapshot_id)
        ?? run.snapshots.at(-1);
      const firstNode = snapshot?.definition.nodes.at(0);
      if (firstNode) {
        setSelectedNodeKey(firstNode.instance_key);
        return;
      }
      const firstExecution = run.node_runs.at(0);
      if (firstExecution) selectExecution(firstExecution.id, firstExecution.attempts.at(-1)?.id);
    }
  }, [query.data, selectAttempt, selectExecution, selectedAttemptId, selectedNodeKey, selectedNodeRunId]);
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
  const beginSideResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const move = (moveEvent: PointerEvent) => setSidePanelWidth(Math.max(320, Math.min(680, window.innerWidth - moveEvent.clientX)));
    const stop = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', stop); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  };
  return <section className={`workbench-page${hasPanel ? '' : ' no-action-panel'}`} style={hasPanel ? { gridTemplateColumns: `250px minmax(500px, 1fr) ${sidePanelWidth}px` } : undefined}><RunRail run={run} selected={nodeRun?.id} onSelect={selectHistory}/><main className="run-main"><button className="back" onClick={returnToRuns}><ArrowLeft size={14}/>返回运行列表</button><header className="run-title"><div><span className="eyebrow">第 {run.run_no} 次流程运行</span><h1>{run.name}</h1><p>流程快照 v{run.active_snapshot_version} · {run.progress.accepted}/{run.node_runs.length} 次节点执行已验收</p></div><div className="run-title-actions"><span className={`run-state ${run.state.toLowerCase()}`}>{FLOW_STATE_LABELS[run.state] ?? run.state}</span><FlowRunControls run={run} refresh={refresh} navigate={navigate}/></div></header>{run.state !== 'COMPLETED' && run.state !== 'CANCELLED' && <SnapshotSync run={run} currentVersion={flow.data?.row_version} onSynced={updated => navigate(updated, 'sync')}/>}<SnapshotGraph run={run} selectedKey={selectedNodeKey} onSelect={selectGraphNode}/>
    </main>{hasPanel && <aside className="run-side-panel"><div className="run-side-resizer" role="separator" aria-label="调整右侧栏宽度" aria-orientation="vertical" onPointerDown={beginSideResize}/>{nodeRun && attempt ? <AttemptPanel run={run} nodeRun={nodeRun} attempt={attempt} refresh={refresh} navigate={navigate} onCreateNew={() => { setSelectedNodeKey(nodeRun.flow_node_snapshot_key); useWorkbenchStore.setState({ selectedNodeRunId: undefined, selectedAttemptId: undefined }); }}/> : selectedNode ? <NodeConsole run={run} node={selectedNode} refresh={refresh} onActivated={created => { setSelectedNodeKey(undefined); navigate(created, 'activate'); }} onSelectExecution={item => { setSelectedNodeKey(undefined); selectExecution(item.id, item.attempts.at(-1)?.id); }}/> : null}</aside>}</section>;
}
