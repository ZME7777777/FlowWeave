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
import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { api, randomId } from '../api/client';
import { flowMappingEdgeTypes, withMappingLabelOffsets } from '../components/flowMappingEdgeLayout';
import { useProductDialog } from '../components/ProductDialogContext';
import type {
  FlowDefinition,
  FlowEdge,
  FlowPortMapping,
  FlowWrite,
  NodeAsset,
  NodeDirectory,
} from '../types';

type FlowNodeData = {
  label: string;
  assetName: string;
  assetId: string;
  inputs: NodeAsset['inputs'];
  outputs: NodeAsset['outputs'];
  alias: string;
  linkMode?: 'flow' | 'data';
  onDelete?: (nodeId: string) => void;
};

const nodeTypes = { flowAsset: FlowAssetNode };
const emptyNodeAssets: NodeAsset[] = [];
const emptyNodeDirectories: NodeDirectory[] = [];
const emptyFlows: FlowDefinition[] = [];

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : [];
}

function ioFields(value: unknown): NodeAsset['inputs'] {
  return asArray<NodeAsset['inputs'][number]>(value).filter(field =>
    field && typeof field.field_key === 'string' && typeof field.data_type === 'string',
  );
}

function portLabel(field: NodeAsset['inputs'][number] | undefined, fallback = ''): string {
  return field?.display_name?.trim() || field?.field_key || fallback;
}

function flowSaveError(reason: Error): string {
  return reason.message;
}

function FlowAssetNode({ id, data, selected }: NodeProps<Node<FlowNodeData>>) {
  const inputs = ioFields(data.inputs);
  const outputs = ioFields(data.outputs);
  return <article className={`flow-asset-node ${selected ? 'selected' : ''}`}>
    <Handle id="flow-target" className="flow-direction-handle" type="target" position={Position.Left} isConnectable={data.linkMode === 'flow'}/>
    <div className="flow-node-head"><span className="flow-node-kind">AGENT</span><button type="button" className="flow-node-delete nodrag nopan" aria-label={`删除节点 ${data.label}`} title="删除节点" onClick={event => { event.stopPropagation(); data.onDelete?.(id); }}><Trash2 size={13}/></button></div>
    <strong>{data.label}</strong>
    <small>标准端口来自节点资产</small>
    <div className="flow-port-groups"><section><span>INPUTS</span>{inputs.map(field => <div className="flow-port-row flow-port-input" key={`input-${field.field_key}`}><Handle id={`input:${field.field_key}`} className="data-port-handle" type="target" position={Position.Left} isConnectable={data.linkMode === 'data'}/><b>{portLabel(field)}</b><small>{field.data_type}</small></div>)}</section><section><span>OUTPUTS</span>{outputs.map(field => <div className="flow-port-row flow-port-output" key={`output-${field.field_key}`}><b>{portLabel(field)}</b><small>{field.data_type}</small><Handle id={`output:${field.field_key}`} className="data-port-handle" type="source" position={Position.Right} isConnectable={data.linkMode === 'data'}/></div>)}</section></div>
    <Handle id="flow-source" className="flow-direction-handle" type="source" position={Position.Right} isConnectable={data.linkMode === 'flow'}/>
  </article>;
}

function nodeData(asset: NodeAsset): FlowNodeData {
  return {
    label: asset.name,
    assetName: asset.name,
    assetId: asset.id,
    inputs: ioFields(asset.inputs),
    outputs: ioFields(asset.outputs),
    alias: '',
  };
}

function toCanvas(flow?: FlowDefinition, assets: NodeAsset[] = []): [Node<FlowNodeData>[], Edge[], Edge[]] {
  if (!flow) return [[], [], []];
  const canvasNodes = asArray<FlowDefinition['nodes'][number]>(flow.nodes).filter(item =>
    item && typeof item.instance_key === 'string' && typeof item.node_asset_id === 'string',
  ).map(item => {
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
      context_capabilities: [],
      created_at: '',
      updated_at: '',
    };
    return {
      id: item.instance_key,
      type: 'flowAsset',
      position: {
        x: Number.isFinite(item.position_x) ? item.position_x : 0,
        y: Number.isFinite(item.position_y) ? item.position_y : 0,
      },
      data: nodeData(asset ?? fallback),
    };
  });
  return [
    canvasNodes,
    asArray<FlowDefinition['edges'][number]>(flow.edges).filter(item =>
      item && typeof item.source_instance_key === 'string' && typeof item.target_instance_key === 'string',
    ).map(item => ({
      id: `flow:${item.id ?? randomId()}`,
      source: item.source_instance_key,
      target: item.target_instance_key,
      sourceHandle: 'flow-source',
      targetHandle: 'flow-target',
      type: 'bezier',
      className: 'flow-direction-edge',
    })),
    asArray<FlowDefinition['port_mappings'][number]>(flow.port_mappings).filter(item =>
      item && typeof item.source_instance_key === 'string' && typeof item.target_instance_key === 'string'
        && typeof item.source_output_key === 'string' && typeof item.target_input_key === 'string',
    ).map(item => {
      const source = canvasNodes.find(node => node.id === item.source_instance_key)?.data.outputs
        .find(field => field.field_key === item.source_output_key);
      const target = canvasNodes.find(node => node.id === item.target_instance_key)?.data.inputs
        .find(field => field.field_key === item.target_input_key);
      return {
        id: `mapping:${item.id ?? randomId()}`,
        source: item.source_instance_key,
        sourceHandle: `output:${item.source_output_key}`,
        target: item.target_instance_key,
        targetHandle: `input:${item.target_input_key}`,
        type: 'bezier',
        className: 'flow-mapping-edge',
        label: `${portLabel(source, item.source_output_key)} → ${portLabel(target, item.target_input_key)}`,
      };
    }),
  ];
}

function directoryLabel(directoryId: string | null | undefined, directories: NodeDirectory[]): string {
  if (!directoryId) return '未分类';
  const names: string[] = [];
  const visited = new Set<string>();
  let current = directories.find(item => item.id === directoryId);
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    names.unshift(current.name);
    current = directories.find(item => item.id === current?.parent_id);
  }
  return names.length ? names.join(' / ') : '未分类';
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
  const { data: assets = emptyNodeAssets } = useQuery({ queryKey: ['nodes'], queryFn: () => api.nodes() });
  const { data: directories = emptyNodeDirectories } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: flows = emptyFlows } = useQuery({ queryKey: ['flows'], queryFn: api.flows });
  const [selected, setSelected] = useState<FlowDefinition>();
  const [isNew, setIsNew] = useState(false);
  const [selectedNode, setSelectedNode] = useState<string>();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [assetSearch, setAssetSearch] = useState('');
  const [flowSearch, setFlowSearch] = useState('');
  const [linkMode, setLinkMode] = useState<'flow' | 'data'>('flow');
  const [selectedFlowIds, setSelectedFlowIds] = useState<Set<string>>(new Set());
  const [deletingFlows, setDeletingFlows] = useState(false);
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance<Node<FlowNodeData>, Edge>>();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([]);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const [directionEdges, setDirectionEdges] = useEdgesState<Edge>([]);
  const [portMappings, setPortMappings] = useEdgesState<Edge>([]);

  useEffect(() => {
    const [canvasNodes, canvasEdges, canvasMappings] = toCanvas(selected, assets);
    setNodes(canvasNodes);
    setDirectionEdges(canvasEdges);
    setPortMappings(canvasMappings);
    setName(selected?.name ?? '');
    setDescription(selected?.description ?? '');
    setSelectedNode(canvasNodes[0]?.id);
    setNotice('');
  }, [selected, assets, setNodes, setDirectionEdges, setPortMappings]);

  const payload = (): FlowWrite => ({
    name,
    description,
    default_entry_key: null,
    row_version: selected?.row_version,
    nodes: nodes.map(item => ({
      instance_key: item.id,
      node_asset_id: item.data.assetId,
      alias: null,
      position_x: Math.round(item.position.x),
      position_y: Math.round(item.position.y),
      config_override: {},
      // Gates belong to an individual execution, not a reusable flow template.
      // Saving a definition also clears any legacy template gates.
      gates: [],
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
    mutationFn: () => selected && !isNew ? api.updateFlow(selected.id, payload()) : api.createFlow(payload()),
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
    const currentNodes = nodesRef.current;
    const count = currentNodes.filter(item => item.data.assetId === asset.id).length;
    const id = `node_${count + 1}_${randomId().replaceAll('-', '').slice(0, 8)}`;
    const nextNodes = [...currentNodes, {
      id,
      type: 'flowAsset',
      position: position ?? { x: 100 + currentNodes.length * 220, y: 180 },
      data: nodeData(asset),
    }];
    nodesRef.current = nextNodes;
    setNodes(nextNodes);
    setSelectedNode(id);
    setNotice(count ? `已再次添加“${asset.name}”。` : `已添加“${asset.name}”。`);
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
        type: 'bezier',
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
      type: 'bezier',
      className: 'flow-mapping-edge',
      label: `${portLabel(source)} → ${portLabel(target)}`,
    }]);
    setError('');
    setNotice(`已连接 ${sourceNode?.data.label}.${portLabel(source)} → ${targetNode?.data.label}.${portLabel(target)}。`);
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
    setSelectedNode(current => current === nodeId ? remaining[0]?.id : current);
    setNotice(`已删除节点“${removed.data.label}”及其关联连线。`);
  };
  const displayEdges = useMemo(() => [
    ...directionEdges.map(edge => ({ ...edge, selectable: linkMode === 'flow', deletable: linkMode === 'flow', style: { opacity: linkMode === 'flow' ? 1 : 0.16 } })),
    ...withMappingLabelOffsets(portMappings).map(edge => ({ ...edge, selectable: linkMode === 'data', deletable: linkMode === 'data', style: { opacity: linkMode === 'data' ? 1 : 0.16 } })),
  ], [directionEdges, portMappings, linkMode]);
  const changeEdges = (changes: EdgeChange[]) => {
    const edgeId = (change: EdgeChange) => change.type === 'add' ? change.item.id : change.id;
    const flowChanges = changes.filter(change => edgeId(change).startsWith('flow:'));
    const mappingChanges = changes.filter(change => edgeId(change).startsWith('mapping:'));
    if (flowChanges.length) setDirectionEdges(old => applyEdgeChanges(flowChanges, old));
    if (mappingChanges.length) setPortMappings(old => applyEdgeChanges(mappingChanges, old));
  };
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
  const startNewFlow = () => {
    setSelected(undefined);
    setIsNew(true);
    setNodes([]);
    setDirectionEdges([]);
    setPortMappings([]);
    setSelectedNode(undefined);
    setName('新流程');
    setDescription('');
    setError('');
    setNotice('');
  };
  const removeFlows = async (ids: string[], label: string) => {
    if (!ids.length || !await dialog.confirm({ title: `永久删除${label}？`, message: '流程定义将从数据库永久删除且不可恢复；如仍有关联运行，系统会阻止删除并提示先清理运行记录。', confirmLabel: '永久删除', tone: 'danger' })) return;
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
      setNotice(`已永久删除 ${succeeded} 个流程定义。`);
    }
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['flows'] }),
      qc.invalidateQueries({ queryKey: ['runs'] }),
    ]);
    setDeletingFlows(false);
  };
  const filteredAssets = useMemo(() => assets.filter(asset => `${asset.name ?? ''} ${asset.description ?? ''}`.toLowerCase().includes(assetSearch.toLowerCase())), [assets, assetSearch]);
  const groupedAssets = useMemo(() => {
    const groups = new Map<string, NodeAsset[]>();
    filteredAssets.forEach(asset => {
      const label = directoryLabel(asset.directory_id, directories);
      groups.set(label, [...(groups.get(label) ?? []), asset]);
    });
    return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [filteredAssets, directories]);

  return <section className="page flow-page">
    <div className="flow-product-layout inspector-free" style={{ gridTemplateColumns: '230px minmax(0, 1fr)' }}>
      <aside className="flow-library" data-testid="flow-library"><h3>流程</h3><label className="flow-library-search"><Search size={13}/><input aria-label="搜索流程" placeholder="搜索流程" value={flowSearch} onChange={event => setFlowSearch(event.target.value)}/></label><div className="flow-list-actions"><button type="button" className="secondary" disabled={!filteredFlows.length || deletingFlows} onClick={toggleVisibleFlows}><CheckSquare size={13}/>{allVisibleFlowsSelected ? '取消全选' : '全选'}</button><button type="button" className="danger" disabled={!selectedFlowIds.size || deletingFlows} onClick={() => void removeFlows([...selectedFlowIds], `选中的 ${selectedFlowIds.size} 个流程`)}><Trash2 size={13}/>{deletingFlows ? '删除中' : `删除 (${selectedFlowIds.size})`}</button><button type="button" className="flow-create-action" aria-label="新建流程" onClick={startNewFlow}><Plus size={13}/>新建</button></div><div className="flow-definition-list">{filteredFlows.map(flow => <div className={`flow-definition-row ${selected?.id === flow.id ? 'active' : ''}`} key={flow.id}><label className="resource-check"><input type="checkbox" aria-label={`选择流程 ${flow.name}`} checked={selectedFlowIds.has(flow.id)} onChange={() => toggleFlow(flow.id)}/></label><button className="flow-select" onClick={() => { setSelected(flow); setIsNew(false); }}>{flow.name}</button><button type="button" className="flow-definition-delete" aria-label={`删除流程 ${flow.name}`} title="删除流程" onClick={() => void removeFlows([flow.id], `流程“${flow.name}”`)}><Trash2 size={13}/></button></div>)}</div>{!filteredFlows.length && <div className="flow-list-empty">没有匹配流程</div>}<h3>节点资产目录</h3><label className="flow-library-search"><Search size={13}/><input aria-label="搜索节点资产" placeholder="搜索当前资产库" value={assetSearch} onChange={event => setAssetSearch(event.target.value)}/></label>{groupedAssets.map(([directory, items]) => <section className="flow-asset-group" key={directory}><h4>{directory}</h4>{items.map(asset => <button draggable key={asset.id} aria-label={asset.name} title="拖入画布或点击添加" onDragStart={event => { event.dataTransfer.effectAllowed = 'copy'; event.dataTransfer.setData('application/flowweave-node-asset', asset.id); }} onClick={() => addAsset(asset)}><span className="flow-library-icon">{(asset.icon_value || 'AG').slice(0, 2).toUpperCase()}</span><span><b>{asset.name}</b><small>{ioFields(asset.inputs).length} 输入 · {ioFields(asset.outputs).length} 输出</small></span></button>)}</section>)}</aside>
      <main className="flow-designer" data-testid="flow-designer" data-link-mode={linkMode} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }} onDrop={dropAsset}>
        <div className="designer-toolbar"><input aria-label="流程名称" value={name} placeholder="流程名称" onChange={event => setName(event.target.value)}/><input aria-label="流程说明" value={description} placeholder="说明" onChange={event => setDescription(event.target.value)}/><div className="flow-link-mode" aria-label="连线模式"><button type="button" className={linkMode === 'flow' ? 'active' : ''} aria-pressed={linkMode === 'flow'} onClick={() => setLinkMode('flow')}>流程走向</button><button type="button" className={linkMode === 'data' ? 'active' : ''} aria-pressed={linkMode === 'data'} onClick={() => setLinkMode('data')}>产物流转</button></div><button className="secondary" aria-label="自动布局" onClick={() => { setNodes(old => autoLayout(old, directionEdges)); window.setTimeout(() => void flowInstance?.fitView({ padding: 0.2 }), 0); }}><LayoutDashboard size={14}/>自动布局</button><button className="primary" onClick={() => save.mutate()} disabled={!name.trim() || !nodes.length}><Save size={14}/>保存流程</button></div>
        {error && <div className="canvas-error">{error}</div>}{notice && <div className="canvas-notice" role="status">{notice}</div>}
        <ReactFlow nodeTypes={nodeTypes} edgeTypes={flowMappingEdgeTypes} nodes={nodes.map(item => ({ ...item, selected: item.id === selectedNode, data: { ...item.data, linkMode, onDelete: removeNode },  }))} edges={displayEdges} onInit={setFlowInstance} onNodesChange={onNodesChange} onEdgesChange={changeEdges} onConnect={connect} onNodeClick={(_, node) => setSelectedNode(node.id)} fitView><Background/><Controls/></ReactFlow>
        <small className="canvas-help"><GitBranch size={12}/>{linkMode === 'flow' ? '流程走向模式：流程连线高亮；端口映射弱化显示。' : '产物流转模式：端口映射高亮；流程走向弱化显示。'}</small>
      </main>
    </div>
  </section>;
}
