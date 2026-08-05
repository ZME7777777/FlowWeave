import { CheckSquare, ChevronRight, Copy, Folder, Pencil, Plus, Search, Trash2, X } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type MouseEvent } from 'react';
import { api } from '../api/client';
import { NodeEditor } from '../components/NodeEditor';
import type { ModelProvider, NodeAsset, NodeAssetWrite, NodeDirectory } from '../types';

interface FolderRowProps {
  item: NodeDirectory;
  directories: NodeDirectory[];
  nodes: NodeAsset[];
  selected: string;
  depth: number;
  onSelect: (id: string) => void;
}

function descendantIds(id: string, directories: NodeDirectory[]): string[] {
  const children = directories.filter(item => item.parent_id === id);
  return [id, ...children.flatMap(child => descendantIds(child.id, directories))];
}

function FolderRow({ item, directories, nodes, selected, depth, onSelect }: FolderRowProps) {
  const children = directories.filter(child => child.parent_id === item.id);
  const ids = descendantIds(item.id, directories);
  const count = nodes.filter(node => node.directory_id && ids.includes(node.directory_id)).length;
  return <>
    <button className={`folder-row ${selected === item.id ? 'active' : ''}`} style={{ paddingLeft: 9 + depth * 16 }} onClick={() => onSelect(item.id)}>
      {children.length ? <ChevronRight size={12}/> : <span/>}<Folder size={14}/><span>{item.name}</span><small>{count}</small>
    </button>
    {children.map(child => <FolderRow key={child.id} item={child} directories={directories} nodes={nodes} selected={selected} depth={depth + 1} onSelect={onSelect}/>)}
  </>;
}

function NodeDetail({ node, directories, providers, onClose, onEdit }: { node: NodeAsset; directories: NodeDirectory[]; providers: ModelProvider[]; onClose: () => void; onEdit: () => void }) {
  const provider = providers.find(item => item.id === node.executor?.model_provider_id);
  const capabilities = (type: 'SKILL' | 'MCP' | 'HOOK') => node.capabilities.filter(item => item.capability_type === type);
  return <div className="detail-drawer-backdrop" onClick={onClose}><aside className="node-detail-drawer" role="dialog" aria-modal="true" aria-label={`节点详情 ${node.name}`} onClick={event => event.stopPropagation()}>
    <header><span className="node-icon">{node.icon_value.slice(0, 2).toUpperCase()}</span><div><h2>{node.name}</h2><small>节点资产 · AGENT</small></div><button className="ghost" aria-label="关闭节点详情" onClick={onClose}><X size={17}/></button></header>
    <section><h3>资产摘要</h3><p>{node.description || '暂无说明'}</p><dl><dt>分类</dt><dd>{directories.find(item => item.id === node.directory_id)?.name || '未分类'}</dd><dt>最近更新</dt><dd>{new Date(node.updated_at).toLocaleString()}</dd></dl></section>
    <section><h3>Agent 能力</h3><dl><dt>模型</dt><dd>{provider?.name || '未配置'} · {node.executor?.model_name || '服务默认'}</dd><dt>默认 Skill</dt><dd>{node.default_skill_ref}</dd></dl>
      {(['SKILL', 'MCP', 'HOOK'] as const).map(type => <div className="detail-capability" key={type}><b>{type === 'SKILL' ? 'Skills' : type}</b><span>{capabilities(type).length} 项</span><small>{capabilities(type).map(item => item.capability_key).join('、') || '无'}</small></div>)}
    </section>
    <section><h3>提示词上下文</h3><p><b>启动入口：</b>{node.executor?.startup_prompt || '未配置'}</p><p><b>上下文：</b>{node.executor?.context_prompt || '未配置'}</p></section>
    <section><h3>输入输出定义</h3><div className="detail-capability"><b>输入定义</b><span>{node.inputs.length} 项</span><small>{node.inputs.map(item => `${item.field_key} · ${item.data_type}`).join('、') || '无'}</small></div><div className="detail-capability"><b>输出定义</b><span>{node.outputs.length} 项</span><small>{node.outputs.map(item => `${item.field_key} · ${item.data_type}`).join('、') || '无'}</small></div></section>
    <footer><button className="secondary" onClick={onClose}>关闭</button><button className="primary" onClick={onEdit}><Pencil size={14}/>编辑节点</button></footer>
  </aside></div>;
}

export function NodesPage() {
  const qc = useQueryClient();
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: providers = [] } = useQuery({ queryKey: ['providers'], queryFn: api.providers });
  const { data: nodes = [], isLoading } = useQuery({ queryKey: ['nodes'], queryFn: () => api.nodes() });
  const [directory, setDirectory] = useState('all');
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<NodeAsset | null | undefined>();
  const [detail, setDetail] = useState<NodeAsset>();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleting, setDeleting] = useState(false);
  const visible = useMemo(() => nodes.filter(item => (directory === 'all' || item.directory_id === directory) && (!search || `${item.name} ${item.description}`.toLowerCase().includes(search.toLowerCase()))), [nodes, directory, search]);
  const visibleIds = visible.map(item => item.id);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));

  const save = useMutation({
    mutationFn: ({ node, data }: { node?: NodeAsset; data: NodeAssetWrite }) => node ? api.updateNode(node.id, data) : api.createNode(data),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['nodes'] }); setEditing(undefined); setDetail(undefined); },
  });
  const action = (event: MouseEvent, callback: () => void) => { event.stopPropagation(); callback(); };
  const toggle = (id: string) => setSelectedIds(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleVisible = () => setSelectedIds(old => {
    const next = new Set(old);
    if (allVisibleSelected) visibleIds.forEach(id => next.delete(id));
    else visibleIds.forEach(id => next.add(id));
    return next;
  });
  const removeMany = async (ids: string[], label: string) => {
    if (!ids.length || !window.confirm(`确定删除${label}吗？删除后将不再出现在资产目录中。`)) return;
    setDeleting(true); setError(''); setNotice('');
    const results = await Promise.allSettled(ids.map(id => api.deleteNode(id)));
    const failed = ids.filter((_, index) => results[index].status === 'rejected');
    const succeeded = ids.length - failed.length;
    setSelectedIds(new Set(failed));
    if (detail && ids.includes(detail.id) && !failed.includes(detail.id)) setDetail(undefined);
    if (failed.length) setError(`已删除 ${succeeded} 个节点，${failed.length} 个失败：${results.find(item => item.status === 'rejected')?.reason instanceof Error ? (results.find(item => item.status === 'rejected') as PromiseRejectedResult).reason.message : '请求失败'}`);
    else setNotice(`已删除 ${succeeded} 个节点资产。`);
    await qc.invalidateQueries({ queryKey: ['nodes'] });
    setDeleting(false);
  };
  const createDirectory = async () => {
    const directoryName = prompt('目录名称'); if (!directoryName) return;
    try { await api.createDirectory({ name: directoryName, parent_id: directory === 'all' ? null : directory, position: directories.length }); await qc.invalidateQueries({ queryKey: ['directories'] }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '创建目录失败'); }
  };
  const currentDirectory = directories.find(item => item.id === directory)?.name ?? '全部节点';
  const roots = directories.filter(item => !item.parent_id);

  return <section className="page node-assets-page"><div className="page-head"><div><span className="eyebrow">NODE ASSETS</span><h1>节点资产</h1><p>按目录维护可复用 Agent 节点、能力、提示词和输入输出契约。</p></div><button className="primary" onClick={() => setEditing(null)}><Plus size={16}/>新建节点</button></div>
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    <div className="asset-layout"><aside className="directory-panel"><header><b>节点目录</b><button title="新建目录" onClick={() => void createDirectory()}><Plus size={14}/></button></header><button className={`folder-row all ${directory === 'all' ? 'active' : ''}`} onClick={() => setDirectory('all')}><span/><Folder size={14}/><span>全部节点</span><small>{nodes.length}</small></button>{roots.map(item => <FolderRow key={item.id} item={item} directories={directories} nodes={nodes} selected={directory} depth={0} onSelect={setDirectory}/>)}</aside>
      <main className="asset-content"><div className="asset-tools"><div><span>节点资产</span><strong>{currentDirectory}</strong><small>{visible.length} 个节点</small></div><div className="bulk-actions"><button className="secondary" disabled={!visible.length || deleting} onClick={toggleVisible}><CheckSquare size={14}/>{allVisibleSelected ? '取消全选' : '全选当前结果'}</button><button className="danger" disabled={!selectedIds.size || deleting} onClick={() => void removeMany([...selectedIds], `选中的 ${selectedIds.size} 个节点`)}><Trash2 size={14}/>{deleting ? '删除中…' : `批量删除 (${selectedIds.size})`}</button></div><label><Search size={14}/><input aria-label="搜索节点" value={search} placeholder={`搜索 ${currentDirectory}`} onChange={event => setSearch(event.target.value)}/></label></div>
        {isLoading ? <div className="empty">加载中…</div> : <div className="card-grid">{visible.map(node => <article tabIndex={0} className={`node-card product-card ${selectedIds.has(node.id) ? 'selected' : ''}`} data-testid="node-card" key={node.id} onClick={() => setDetail(node)} onKeyDown={event => { if (event.key === 'Enter') setDetail(node); }}><div className="node-card-head"><label className="resource-check" onClick={event => event.stopPropagation()}><input type="checkbox" aria-label={`选择节点 ${node.name}`} checked={selectedIds.has(node.id)} onChange={() => toggle(node.id)}/><span className="node-icon">{node.icon_value.slice(0, 2).toUpperCase()}</span></label><div className="card-actions"><button title="复制" onClick={event => action(event, () => setEditing({ ...node, id: '', name: `${node.name} 副本`, row_version: 1 }))}><Copy size={15}/></button><button title="编辑" onClick={event => action(event, () => setEditing(node))}><Pencil size={15}/></button><button title="删除" aria-label={`删除节点资产 ${node.name}`} onClick={event => action(event, () => void removeMany([node.id], `节点“${node.name}”`))}><Trash2 size={15}/></button></div></div><h3>{node.name}</h3><p>{node.description || '暂无说明'}</p><div className="chip-row"><span>Skills {node.capabilities.filter(item => item.capability_type === 'SKILL').length}</span><span>MCP {node.capabilities.filter(item => item.capability_type === 'MCP').length}</span><span>Hooks {node.capabilities.filter(item => item.capability_type === 'HOOK').length}</span></div><dl><div><dt>默认</dt><dd>{node.default_skill_ref}</dd></div><div><dt>输入 / 输出</dt><dd>{node.inputs.length} / {node.outputs.length}</dd></div></dl></article>)}</div>}
        {!isLoading && !visible.length && <div className="empty">当前目录没有匹配节点。</div>}
      </main></div>
    {detail && <NodeDetail node={detail} directories={directories} providers={providers} onClose={() => setDetail(undefined)} onEdit={() => { setEditing(detail); setDetail(undefined); }}/>} {editing !== undefined && <NodeEditor node={editing?.id ? editing : undefined} onClose={() => setEditing(undefined)} onSave={data => save.mutateAsync({ node: editing?.id ? editing : undefined, data }).then(() => undefined)}/>} </section>;
}
