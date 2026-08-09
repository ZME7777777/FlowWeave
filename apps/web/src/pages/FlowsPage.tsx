import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { CheckSquare, GitBranch, LayoutDashboard, Plus, Save, Search, Trash2 } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState, type DragEvent } from 'react';
import { api, ApiError, randomId } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import type {
  FlowDefinition,
  FlowEdge,
  FlowPortMapping,
  FlowWrite,
  GatePolicy,
  ModelProvider,
  NodeAsset,
  NodeDirectory,
} from '../types';

type FlowNodeData = {
  label: string;
  assetName: string;
  assetId: string;
  inputs: NodeAsset['inputs'];
  outputs: NodeAsset['outputs'];
  gates: GatePolicy[];
  alias: string;
  linkMode?: 'flow' | 'data';
  onDelete?: (nodeId: string) => void;
};

const defaultGates = (): GatePolicy[] => [];
const nodeTypes = { flowAsset: FlowAssetNode };

function validLarkWikiUrl(value: string): boolean {
  try {
    const url = new URL(value.trim());
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    const validHost = ['feishu.cn', 'larksuite.com', 'larkoffice.com']
      .some(suffix => host === suffix || host.endsWith(`.${suffix}`));
    return url.protocol === 'https:'
      && validHost
      && /^\/wiki\/[^/]+/.test(url.pathname);
  } catch {
    return false;
  }
}

function flowSaveError(reason: Error): string {
  if (reason instanceof ApiError && reason.code === 'INVALID_COMMAND') {
    const errors = reason.details.errors;
    if (Array.isArray(errors) && errors.some(item => {
      if (!item || typeof item !== 'object' || !('loc' in item)) return false;
      return Array.isArray(item.loc) && item.loc.includes('lark_root_folder_url');
    })) return '飞书 Wiki 根节点链接无效。请复制完整的飞书 Wiki 节点链接，格式为 https://<租户>.feishu.cn/wiki/<节点Token>。';
  }
  return reason.message;
}

function FlowAssetNode({ id, data, selected }: NodeProps<Node<FlowNodeData>>) {
  const startCount = data.gates.filter(item => item.stage === 'START').length;
  const endCount = data.gates.filter(item => item.stage === 'END').length;
  return <article className={`flow-asset-node ${selected ? 'selected' : ''}`}>
    <Handle id="flow-target" className="flow-direction-handle" type="target" position={Position.Left} isConnectable={data.linkMode === 'flow'}/>
    <div className="flow-node-head"><span className="flow-node-kind">AGENT</span><button type="button" className="flow-node-delete nodrag nopan" aria-label={`删除节点 ${data.label}`} title="删除节点" onClick={event => { event.stopPropagation(); data.onDelete?.(id); }}><Trash2 size={13}/></button></div>
    <strong>{data.label}</strong>
    <small>标准端口来自节点资产</small>
    <div className="flow-port-groups"><section><span>INPUTS</span>{data.inputs.map(field => <div className="flow-port-row flow-port-input" key={`input-${field.field_key}`}><Handle id={`input:${field.field_key}`} className="data-port-handle" type="target" position={Position.Left} isConnectable={data.linkMode === 'data'}/><b>{field.field_key}</b><small>{field.data_type}</small></div>)}</section><section><span>OUTPUTS</span>{data.outputs.map(field => <div className="flow-port-row flow-port-output" key={`output-${field.field_key}`}><b>{field.field_key}</b><small>{field.data_type}</small><Handle id={`output:${field.field_key}`} className="data-port-handle" type="source" position={Position.Right} isConnectable={data.linkMode === 'data'}/></div>)}</section></div>
    <div className="flow-node-gates"><span>START {startCount}</span><span>END {endCount}</span></div>
    <Handle id="flow-source" className="flow-direction-handle" type="source" position={Position.Right} isConnectable={data.linkMode === 'flow'}/>
  </article>;
}

function nodeData(asset: NodeAsset, alias = '', gates: GatePolicy[] = []): FlowNodeData {
  return {
    label: alias || asset.name,
    assetName: asset.name,
    assetId: asset.id,
    inputs: asset.inputs,
    outputs: asset.outputs,
    gates,
    alias,
  };
}

function toCanvas(flow?: FlowDefinition, assets: NodeAsset[] = []): [Node<FlowNodeData>[], Edge[], Edge[]] {
  if (!flow) return [[], [], []];
  return [
    flow.nodes.map(item => {
      const asset = assets.find(candidate => candidate.id === item.node_asset_id);
      const fallback: NodeAsset = {
        id: item.node_asset_id,
        name: item.instance_key,
        description: '',
        icon_kind: 'LUCIDE',
        icon_value: 'bot',
        row_version: 1,
        inputs: [],
        outputs: [],
        executor: null,
        capabilities: [],
        created_at: '',
        updated_at: '',
      };
      return {
        id: item.instance_key,
        type: 'flowAsset',
        position: { x: item.position_x, y: item.position_y },
        data: nodeData(asset ?? fallback, item.alias ?? '', item.gates),
      };
    }),
    flow.edges.map(item => ({
      id: `flow:${item.id ?? randomId()}`,
      source: item.source_instance_key,
      target: item.target_instance_key,
      sourceHandle: 'flow-source',
      targetHandle: 'flow-target',
      type: 'smoothstep',
      className: 'flow-direction-edge',
    })),
    flow.port_mappings.map(item => ({
      id: `mapping:${item.id ?? randomId()}`,
      source: item.source_instance_key,
      sourceHandle: `output:${item.source_output_key}`,
      target: item.target_instance_key,
      targetHandle: `input:${item.target_input_key}`,
      type: 'smoothstep',
      className: 'flow-mapping-edge',
      label: `${item.source_output_key} → ${item.target_input_key}`,
    })),
  ];
}

const executableGate = (stage: 'START' | 'END', position: number): GatePolicy => ({
  stage,
  position,
  gate_type: 'JAVASCRIPT',
  enabled: true,
  timeout_seconds: 30,
  config: {
    code: "return {decision: 'PASS', summary: '检查通过', reasons: [], evidence: [], details: {}};",
  },
});

function GateEditor({
  node,
  providers,
  portMappings,
  onChange,
  onDelete,
}: {
  node: Node<FlowNodeData>;
  providers: ModelProvider[];
  portMappings: Edge[];
  onChange: (data: FlowNodeData) => void;
  onDelete: () => void;
}) {
  const add = (stage: 'START' | 'END') => {
    const position = node.data.gates.filter(item => item.stage === stage).length;
    onChange({ ...node.data, gates: [...node.data.gates, executableGate(stage, position)] });
  };
  const update = (index: number, patch: Partial<GatePolicy>) => onChange({
    ...node.data,
    gates: node.data.gates.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item),
  });
  const changeType = (index: number, gateType: GatePolicy['gate_type']) => {
    const config = gateType === 'PROMPT'
      ? { model_provider_id: providers[0]?.id ?? '', model_name: null, prompt: '' }
      : {
          code: gateType === 'PYTHON'
            ? "result = {'decision': 'PASS', 'summary': '检查通过', 'reasons': [], 'evidence': [], 'details': {}}"
            : "return {decision: 'PASS', summary: '检查通过', reasons: [], evidence: [], details: {}};",
        };
    update(index, { gate_type: gateType, config });
  };
  const remove = (index: number) => {
    const remaining = node.data.gates.filter((_, itemIndex) => itemIndex !== index);
    onChange({
      ...node.data,
      gates: remaining.map((item, itemIndex, all) => ({
        ...item,
        position: all.slice(0, itemIndex).filter(candidate => candidate.stage === item.stage).length,
      })),
    });
  };

  return <aside className="flow-inspector"><header><div><b>{node.data.label}</b><small>{node.id}</small></div><button type="button" className="danger" aria-label={`删除节点 ${node.data.label}`} onClick={onDelete}><Trash2 size={13}/>删除节点</button></header>
    <label>节点别名<input value={node.data.alias} onChange={event => onChange({ ...node.data, alias: event.target.value, label: event.target.value || node.data.assetName })}/></label>
    <div className="flow-contract-summary"><b>端口连接</b><small>输入输出来自节点资产；这里仅显示流程中的自动流转关系。</small>{node.data.inputs.map(input => { const mapping = portMappings.find(edge => edge.target === node.id && edge.targetHandle === `input:${input.field_key}`); return <div key={input.field_key}><span><b>{input.field_key}</b><small>{input.data_type} · 必填</small></span><em className={mapping ? 'connected' : ''}>{mapping ? `${mapping.source}.${mapping.sourceHandle?.replace('output:', '')}` : '未连接 · 运行时补充'}</em></div>; })}</div>
    {(['START', 'END'] as const).map(stage => <section key={stage}><div className="inspector-title"><b>{stage === 'START' ? '开始门禁' : '结束门禁'}</b><button type="button" aria-label={stage === 'START' ? '添加开始门禁' : '添加结束门禁'} onClick={() => add(stage)}><Plus size={13}/></button></div>
      {node.data.gates.map((gate, index) => gate.stage === stage && <article className="gate-editor" key={`${stage}-${gate.position}`}><div className="gate-row"><select aria-label={`${stage} 门禁类型 ${gate.position + 1}`} value={gate.gate_type} onChange={event => changeType(index, event.target.value as GatePolicy['gate_type'])}><option>PROMPT</option><option>PYTHON</option><option>JAVASCRIPT</option></select><label>超时（秒）<input aria-label={`${stage} 门禁超时 ${gate.position + 1}`} type="number" min="1" max="300" value={gate.timeout_seconds} onChange={event => update(index, { timeout_seconds: Number(event.target.value) })}/></label><button type="button" aria-label={`删除${stage === 'START' ? '开始' : '结束'}门禁 ${gate.position + 1}`} className="ghost" onClick={() => remove(index)}><Trash2 size={13}/></button></div>
        {gate.gate_type === 'PROMPT' ? <><label>模型服务<select aria-label={`${stage} 门禁模型服务 ${gate.position + 1}`} value={String(gate.config.model_provider_id ?? '')} onChange={event => update(index, { config: { ...gate.config, model_provider_id: event.target.value, model_name: null } })}><option value="">选择模型服务</option>{providers.filter(provider => provider.available_for_nodes).map(provider => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></label><label>模型<select aria-label={`${stage} 门禁模型 ${gate.position + 1}`} value={String(gate.config.model_name ?? '')} onChange={event => update(index, { config: { ...gate.config, model_name: event.target.value || null } })}><option value="">服务默认</option>{providers.find(provider => provider.id === gate.config.model_provider_id)?.models.filter(model => model.enabled).map(model => <option key={model.model_name}>{model.model_name}</option>)}</select></label><label>判定提示词<textarea aria-label={`${stage} 门禁提示词 ${gate.position + 1}`} value={String(gate.config.prompt ?? '')} onChange={event => update(index, { config: { ...gate.config, prompt: event.target.value } })}/></label></> : <label>{gate.gate_type === 'PYTHON' ? 'Python 代码（赋值 result）' : 'JavaScript 代码（return 结果）'}<textarea className="gate-code" aria-label={`${stage} 门禁代码 ${gate.position + 1}`} value={String(gate.config.code ?? '')} onChange={event => update(index, { config: { code: event.target.value } })}/></label>}
      </article>)}
    </section>)}
  </aside>;
}

function directoryLabel(directoryId: string | null | undefined, directories: NodeDirectory[]): string {
  if (!directoryId) return '未分类';
  const names: string[] = [];
  let current = directories.find(item => item.id === directoryId);
  while (current) {
    names.unshift(current.name);
    current = directories.find(item => item.id === current?.parent_id);
  }
  return names.join(' / ');
}

function autoLayout(nodes: Node<FlowNodeData>[], edges: Edge[]): Node<FlowNodeData>[] {
  const incoming = new Map(nodes.map(node => [node.id, 0]));
  const outgoing = new Map(nodes.map(node => [node.id, [] as string[]]));
  edges.forEach(edge => {
    if (!incoming.has(edge.source) || !incoming.has(edge.target)) return;
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
    outgoing.get(edge.source)?.push(edge.target);
  });
  const depth = new Map<string, number>();
  const queue = nodes.filter(node => incoming.get(node.id) === 0).map(node => node.id);
  queue.forEach(id => depth.set(id, 0));
  while (queue.length) {
    const id = queue.shift()!;
    for (const target of outgoing.get(id) ?? []) {
      depth.set(target, Math.max(depth.get(target) ?? 0, (depth.get(id) ?? 0) + 1));
      incoming.set(target, (incoming.get(target) ?? 1) - 1);
      if (incoming.get(target) === 0) queue.push(target);
    }
  }
  nodes.forEach(node => { if (!depth.has(node.id)) depth.set(node.id, 0); });
  const rowByDepth = new Map<number, number>();
  return nodes.map(node => {
    const column = depth.get(node.id) ?? 0;
    const row = rowByDepth.get(column) ?? 0;
    rowByDepth.set(column, row + 1);
    return { ...node, position: { x: 70 + column * 280, y: 70 + row * 180 } };
  });
}

export function FlowsPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const { data: assets = [] } = useQuery({ queryKey: ['nodes'], queryFn: () => api.nodes() });
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: flows = [] } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const { data: providers = [] } = useQuery({ queryKey: ['providers'], queryFn: api.providers });
  const [selected, setSelected] = useState<FlowDefinition>();
  const [isNew, setIsNew] = useState(false);
  const [selectedNode, setSelectedNode] = useState<string>();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [larkRootFolderUrl, setLarkRootFolderUrl] = useState('');
  const [entry, setEntry] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [assetSearch, setAssetSearch] = useState('');
  const [flowSearch, setFlowSearch] = useState('');
  const [linkMode, setLinkMode] = useState<'flow' | 'data'>('data');
  const [selectedFlowIds, setSelectedFlowIds] = useState<Set<string>>(new Set());
  const [deletingFlows, setDeletingFlows] = useState(false);
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance<Node<FlowNodeData>, Edge>>();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([]);
  const [directionEdges, setDirectionEdges] = useEdgesState<Edge>([]);
  const [portMappings, setPortMappings] = useEdgesState<Edge>([]);

  useEffect(() => {
    const [canvasNodes, canvasEdges, canvasMappings] = toCanvas(selected, assets);
    setNodes(canvasNodes);
    setDirectionEdges(canvasEdges);
    setPortMappings(canvasMappings);
    setName(selected?.name ?? '');
    setDescription(selected?.description ?? '');
    setLarkRootFolderUrl(selected?.lark_root_folder_url ?? '');
    setEntry(selected?.default_entry_key ?? '');
    setSelectedNode(undefined);
    setNotice('');
  }, [selected, assets, setNodes, setDirectionEdges, setPortMappings]);

  const currentNode = nodes.find(item => item.id === selectedNode);
  const payload = (): FlowWrite => ({
    name,
    description,
    lark_root_folder_url: larkRootFolderUrl,
    default_entry_key: entry || null,
    row_version: selected?.row_version,
    nodes: nodes.map(item => ({
      instance_key: item.id,
      node_asset_id: item.data.assetId,
      alias: item.data.alias || null,
      position_x: Math.round(item.position.x),
      position_y: Math.round(item.position.y),
      config_override: {},
      gates: item.data.gates.map(gate => ({
        stage: gate.stage,
        position: gate.position,
        gate_type: gate.gate_type,
        enabled: gate.enabled,
        timeout_seconds: gate.timeout_seconds,
        config: gate.config,
      })),
    })),
    edges: directionEdges.map((item, position): FlowEdge => ({
      source_instance_key: item.source,
      target_instance_key: item.target,
      position,
    })),
    port_mappings: portMappings.map((item): FlowPortMapping => ({
      source_instance_key: item.source,
      source_output_key: item.sourceHandle?.replace('output:', '') ?? '',
      target_instance_key: item.target,
      target_input_key: item.targetHandle?.replace('input:', '') ?? '',
    })),
  });
  const save = useMutation({
    mutationFn: () => {
      if (!validLarkWikiUrl(larkRootFolderUrl)) {
        throw new Error('飞书 Wiki 根节点链接无效。请复制完整的飞书 Wiki 节点链接，格式为 https://<租户>.feishu.cn/wiki/<节点Token>。');
      }
      return selected && !isNew ? api.updateFlow(selected.id, payload()) : api.createFlow(payload());
    },
    onSuccess: flow => {
      setSelected(flow);
      setIsNew(false);
      setError('');
      setNotice('流程已保存');
      void qc.invalidateQueries({ queryKey: ['flows'] });
    },
    onError: reason => setError(flowSaveError(reason)),
  });

  const addAsset = (asset: NodeAsset, position?: { x: number; y: number }) => {
    const count = nodes.filter(item => item.data.assetId === asset.id).length;
    const id = `node_${count + 1}_${randomId().replaceAll('-', '').slice(0, 8)}`;
    setNodes(old => [...old, {
      id,
      type: 'flowAsset',
      position: position ?? { x: 100 + old.length * 220, y: 180 },
      data: nodeData(asset, '', defaultGates()),
    }]);
    setSelectedNode(id);
    setNotice(count ? `已再次添加“${asset.name}”，新实例可独立配置门禁。` : `已添加“${asset.name}”。`);
  };
  const connect = (connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) {
      setError('连线必须连接两个不同的节点。');
      return;
    }
    if (linkMode === 'flow') {
      if (directionEdges.some(edge => edge.source === connection.source && edge.target === connection.target)) {
        setError('这两个节点之间已经存在流程走向。');
        return;
      }
      setDirectionEdges(old => addEdge({
        ...connection,
        id: `flow:${randomId()}`,
        sourceHandle: 'flow-source',
        targetHandle: 'flow-target',
        type: 'smoothstep',
        className: 'flow-direction-edge',
      }, old));
      setError('');
      setNotice('已添加节点流程走向。');
      return;
    }
    const sourceKey = connection.sourceHandle?.replace('output:', '');
    const targetKey = connection.targetHandle?.replace('input:', '');
    const sourceNode = nodes.find(item => item.id === connection.source);
    const targetNode = nodes.find(item => item.id === connection.target);
    const source = sourceNode?.data.outputs.find(field => field.field_key === sourceKey);
    const target = targetNode?.data.inputs.find(field => field.field_key === targetKey);
    if (!source || !target) {
      setError('产物流转只能从输出端口连接到输入端口。');
      return;
    }
    if (source.data_type !== target.data_type) {
      setError(`端口类型不兼容：${source.data_type} 不能写入 ${target.data_type}。`);
      return;
    }
    setPortMappings(old => [...old.filter(edge => !(edge.target === connection.target && edge.targetHandle === connection.targetHandle)), {
      ...connection,
      id: `mapping:${randomId()}`,
      type: 'smoothstep',
      className: 'flow-mapping-edge',
      label: `${source.field_key} → ${target.field_key}`,
    }]);
    setError('');
    setNotice(`已连接 ${sourceNode?.data.label}.${source.field_key} → ${targetNode?.data.label}.${target.field_key}。`);
  };
  const dropAsset = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    const asset = assets.find(item => item.id === event.dataTransfer.getData('application/flowweave-node-asset'));
    if (!asset || !flowInstance) return;
    addAsset(asset, flowInstance.screenToFlowPosition({ x: event.clientX, y: event.clientY }));
  };
  const removeNode = (nodeId: string) => {
    const removed = nodes.find(item => item.id === nodeId);
    if (!removed) return;
    const remaining = nodes.filter(item => item.id !== nodeId);
    setNodes(remaining);
    setDirectionEdges(old => old.filter(edge => edge.source !== nodeId && edge.target !== nodeId));
    setPortMappings(old => old.filter(edge => edge.source !== nodeId && edge.target !== nodeId));
    setSelectedNode(current => current === nodeId ? undefined : current);
    if (entry === nodeId) setEntry('');
    setNotice(`已删除节点“${removed.data.label}”及其关联连线。`);
  };
  const displayEdges = useMemo(() => [
    ...directionEdges.map(edge => ({ ...edge, selectable: linkMode === 'flow', deletable: linkMode === 'flow', style: { opacity: linkMode === 'flow' ? 1 : 0.18 } })),
    ...portMappings.map(edge => ({ ...edge, selectable: linkMode === 'data', deletable: linkMode === 'data', style: { opacity: linkMode === 'data' ? 1 : 0.18 } })),
  ], [directionEdges, portMappings, linkMode]);
  const changeEdges = (changes: EdgeChange[]) => {
    const edgeId = (change: EdgeChange) => change.type === 'add' ? change.item.id : change.id;
    const flowChanges = changes.filter(change => edgeId(change).startsWith('flow:'));
    const mappingChanges = changes.filter(change => edgeId(change).startsWith('mapping:'));
    if (flowChanges.length) setDirectionEdges(old => applyEdgeChanges(flowChanges, old));
    if (mappingChanges.length) setPortMappings(old => applyEdgeChanges(mappingChanges, old));
  };
  const updateCurrent = (data: FlowNodeData) => setNodes(old => old.map(item => item.id === selectedNode ? { ...item, data } : item));
  const filteredFlows = useMemo(() => {
    const term = flowSearch.trim().toLowerCase();
    return flows.filter(flow => !term || `${flow.name} ${flow.description}`.toLowerCase().includes(term));
  }, [flows, flowSearch]);
  const visibleFlowIds = filteredFlows.map(flow => flow.id);
  const allVisibleFlowsSelected = visibleFlowIds.length > 0 && visibleFlowIds.every(id => selectedFlowIds.has(id));
  const toggleFlow = (id: string) => setSelectedFlowIds(old => {
    const next = new Set(old);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const toggleVisibleFlows = () => setSelectedFlowIds(old => {
    const next = new Set(old);
    if (allVisibleFlowsSelected) visibleFlowIds.forEach(id => next.delete(id));
    else visibleFlowIds.forEach(id => next.add(id));
    return next;
  });
  const removeFlows = async (ids: string[], label: string) => {
    if (!ids.length || !await dialog.confirm({ title: `删除${label}？`, message: '流程定义将从编排列表移除，已有运行及其快照仍会保留。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setDeletingFlows(true);
    setError('');
    setNotice('');
    const results = await Promise.allSettled(ids.map(id => api.deleteFlow(id)));
    const failed = ids.filter((_, index) => results[index].status === 'rejected');
    const succeeded = ids.length - failed.length;
    setSelectedFlowIds(new Set(failed));
    if (selected && ids.includes(selected.id) && !failed.includes(selected.id)) {
      setSelected(undefined);
      setIsNew(false);
    }
    if (failed.length) {
      const reason = results.find(item => item.status === 'rejected') as PromiseRejectedResult | undefined;
      setError(`已删除 ${succeeded} 个流程，${failed.length} 个失败：${reason?.reason instanceof Error ? reason.reason.message : '请求失败'}`);
    } else {
      setNotice(`已删除 ${succeeded} 个流程定义；历史运行仍可在流程运行中查看。`);
    }
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['flows'] }),
      qc.invalidateQueries({ queryKey: ['runs'] }),
    ]);
    setDeletingFlows(false);
  };
  const filteredAssets = useMemo(() => assets.filter(asset => `${asset.name} ${asset.description}`.toLowerCase().includes(assetSearch.toLowerCase())), [assets, assetSearch]);
  const groupedAssets = useMemo(() => {
    const groups = new Map<string, NodeAsset[]>();
    filteredAssets.forEach(asset => {
      const label = directoryLabel(asset.directory_id, directories);
      groups.set(label, [...(groups.get(label) ?? []), asset]);
    });
    return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [filteredAssets, directories]);

  return <section className="page flow-page">
    <div className="page-head"><div><span className="eyebrow">FLOW DESIGN</span><h1>流程编排</h1><p>流程走向支持一对多和多对一；产物流转连接节点资产的具体输出端口与兼容输入端口。</p></div><button className="primary" onClick={() => { setSelected(undefined); setIsNew(true); setNodes([]); setDirectionEdges([]); setPortMappings([]); setName('新流程'); setDescription(''); setLarkRootFolderUrl(''); setEntry(''); setNotice(''); }}><Plus size={16}/>新建流程</button></div>
    <div className="flow-product-layout">
      <aside className="flow-library" data-testid="flow-library"><h3>流程</h3><label className="flow-library-search"><Search size={13}/><input aria-label="搜索流程" placeholder="搜索流程" value={flowSearch} onChange={event => setFlowSearch(event.target.value)}/></label><div className="flow-list-actions"><button type="button" className="secondary" disabled={!filteredFlows.length || deletingFlows} onClick={toggleVisibleFlows}><CheckSquare size={13}/>{allVisibleFlowsSelected ? '取消全选' : '全选'}</button><button type="button" className="danger" disabled={!selectedFlowIds.size || deletingFlows} onClick={() => void removeFlows([...selectedFlowIds], `选中的 ${selectedFlowIds.size} 个流程`)}><Trash2 size={13}/>{deletingFlows ? '删除中' : `删除 (${selectedFlowIds.size})`}</button></div><div className="flow-definition-list">{filteredFlows.map(flow => <div className={`flow-definition-row ${selected?.id === flow.id ? 'active' : ''}`} key={flow.id}><label className="resource-check"><input type="checkbox" aria-label={`选择流程 ${flow.name}`} checked={selectedFlowIds.has(flow.id)} onChange={() => toggleFlow(flow.id)}/></label><button className="flow-select" onClick={() => { setSelected(flow); setIsNew(false); }}>{flow.name}</button><button type="button" className="flow-definition-delete" aria-label={`删除流程 ${flow.name}`} title="删除流程" onClick={() => void removeFlows([flow.id], `流程“${flow.name}”`)}><Trash2 size={13}/></button></div>)}</div>{!filteredFlows.length && <div className="flow-list-empty">没有匹配流程</div>}<h3>节点资产目录</h3><label className="flow-library-search"><Search size={13}/><input aria-label="搜索节点资产" placeholder="搜索当前资产库" value={assetSearch} onChange={event => setAssetSearch(event.target.value)}/></label>{groupedAssets.map(([directory, items]) => <section className="flow-asset-group" key={directory}><h4>{directory}</h4>{items.map(asset => <button draggable key={asset.id} aria-label={asset.name} title="拖入画布或点击添加" onDragStart={event => { event.dataTransfer.effectAllowed = 'copy'; event.dataTransfer.setData('application/flowweave-node-asset', asset.id); }} onClick={() => addAsset(asset)}><span className="flow-library-icon">{asset.icon_value.slice(0, 2).toUpperCase()}</span><span><b>{asset.name}</b><small>{asset.inputs.length} 输入 · {asset.outputs.length} 输出</small></span></button>)}</section>)}</aside>
      <main className="flow-designer" data-testid="flow-designer" data-link-mode={linkMode} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }} onDrop={dropAsset}>
        <div className="designer-toolbar"><input aria-label="流程名称" value={name} placeholder="流程名称" onChange={event => setName(event.target.value)}/><input aria-label="流程说明" value={description} placeholder="说明" onChange={event => setDescription(event.target.value)}/><input required type="url" pattern="https://.*/wiki/[^/]+.*" aria-label="飞书 Wiki 根节点" value={larkRootFolderUrl} placeholder="飞书 Wiki 根节点 URL" title="请输入 https://.../wiki/... 格式的飞书 Wiki 节点链接" onChange={event => setLarkRootFolderUrl(event.target.value)}/><select aria-label="默认入口" value={entry} onChange={event => setEntry(event.target.value)}><option value="">无默认入口</option>{nodes.map(item => <option key={item.id} value={item.id}>{item.data.label}</option>)}</select><div className="flow-link-mode" aria-label="连线模式"><button type="button" className={linkMode === 'flow' ? 'active' : ''} aria-pressed={linkMode === 'flow'} onClick={() => setLinkMode('flow')}>流程走向</button><button type="button" className={linkMode === 'data' ? 'active' : ''} aria-pressed={linkMode === 'data'} onClick={() => setLinkMode('data')}>产物流转</button></div><button className="secondary" aria-label="自动布局" onClick={() => { setNodes(old => autoLayout(old, directionEdges)); window.setTimeout(() => void flowInstance?.fitView({ padding: 0.2 }), 0); }}><LayoutDashboard size={14}/>自动布局</button><button className="primary" onClick={() => save.mutate()} disabled={!name.trim() || !larkRootFolderUrl.trim() || !nodes.length}><Save size={14}/>保存流程</button></div>
        {error && <div className="canvas-error">{error}</div>}{notice && <div className="canvas-notice" role="status">{notice}</div>}
        <ReactFlow nodeTypes={nodeTypes} nodes={nodes.map(item => ({ ...item, data: { ...item.data, linkMode, onDelete: removeNode }, className: item.id === entry ? 'start-flow-node' : '' }))} edges={displayEdges} onInit={setFlowInstance} onNodesChange={onNodesChange} onEdgesChange={changeEdges} onConnect={connect} onNodeClick={(_, node) => setSelectedNode(node.id)} fitView><Background/><Controls/></ReactFlow>
        <small className="canvas-help"><GitBranch size={12}/>{linkMode === 'flow' ? '流程走向模式：连接节点两侧主端点；端口映射显示为灰色。' : '产物流转模式：连接具体输出与输入端口；节点走向显示为灰色。'}</small>
      </main>
      {currentNode ? <GateEditor node={currentNode} providers={providers.filter(provider => provider.available_for_nodes)} portMappings={portMappings} onChange={updateCurrent} onDelete={() => removeNode(currentNode.id)}/> : <aside className="flow-inspector empty compact">选择画布节点以查看端口连接并配置门禁。</aside>}
    </div>
  </section>;
}
