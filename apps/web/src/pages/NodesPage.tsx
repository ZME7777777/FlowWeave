import { ChevronDown, ChevronRight, Copy, Folder, FolderOpen, Plus, Search, Trash2 } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import { NodeEditor } from '../components/NodeEditor';
import { useProductDialog } from '../components/ProductDialogContext';
import { useWorkbenchStore } from '../store/workbench';
import type { BlockedNodeDelete, NodeAsset, NodeDirectory } from '../types';

type BlockedAsset = BlockedNodeDelete;

function DeleteBlockedDialog({ assets, onClose, onOpenFlows }: { assets: BlockedAsset[]; onClose: () => void; onOpenFlows: () => void }) {
  return <div className="modal-backdrop"><section className="modal node-delete-blocked" role="dialog" aria-modal="true" aria-label="节点正在被流程引用">
    <header><div><span className="eyebrow">DELETE BLOCKED</span><h2>无法删除节点</h2></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <p>以下节点仍被有效流程引用。请先在流程编排中移除或替换节点，再重新删除。</p>
    <div className="node-reference-list">{assets.map(asset => <section key={asset.id}><b>{asset.name}</b>{asset.flows.map(flow => <div key={flow.id}><span>{flow.name}</span><small>引用 {flow.reference_count} 次</small></div>)}</section>)}</div>
    <footer><button className="secondary" onClick={onClose}>取消</button><button className="primary" onClick={onOpenFlows}>前往流程编排</button></footer>
  </section></div>;
}

function directoryDescendantIds(id: string, directories: NodeDirectory[]): string[] {
  const children = directories.filter(item => item.parent_id === id);
  return [id, ...children.flatMap(child => directoryDescendantIds(child.id, directories))];
}

export function NodesPage() {
  const dialog = useProductDialog();
  const queryClient = useQueryClient();
  const setView = useWorkbenchStore(state => state.setView);
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: nodes = [], isLoading } = useQuery({ queryKey: ['nodes'], queryFn: () => api.nodes() });
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const [copyingNodeId, setCopyingNodeId] = useState<string>();
  const [creatingInDirectory, setCreatingInDirectory] = useState<string | null>();
  const [expandedDirectoryIds, setExpandedDirectoryIds] = useState<Set<string>>(() => new Set());
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deletingNodeId, setDeletingNodeId] = useState<string>();
  const [blockedAssets, setBlockedAssets] = useState<BlockedAsset[]>([]);
  const selectedNode = nodes.find(node => node.id === selectedNodeId);
  const normalizedSearch = search.trim().toLowerCase();
  const visibleNodes = useMemo(() => normalizedSearch ? nodes.filter(node => `${node.name} ${node.description}`.toLowerCase().includes(normalizedSearch)) : nodes, [nodes, normalizedSearch]);
  const roots = directories.filter(directory => !directory.parent_id);

  const refreshAssets = async () => {
    await Promise.all([queryClient.invalidateQueries({ queryKey: ['nodes'] }), queryClient.invalidateQueries({ queryKey: ['directories'] })]);
  };
  const toggleDirectory = (directoryId: string) => setExpandedDirectoryIds(current => {
    const next = new Set(current);
    if (next.has(directoryId)) next.delete(directoryId); else next.add(directoryId);
    return next;
  });
  const createDirectory = async (parentId: string | null) => {
    const directoryName = await dialog.prompt({ title: parentId ? '新建子目录' : '新建节点目录', message: '创建一个用于整理节点资产的目录。', inputLabel: '目录名称', placeholder: '例如：产品研发', confirmLabel: '创建目录' });
    if (!directoryName) return;
    setError('');
    try {
      await api.createDirectory({ name: directoryName, parent_id: parentId, position: directories.filter(item => item.parent_id === parentId).length });
      await refreshAssets();
      if (parentId) setExpandedDirectoryIds(current => new Set(current).add(parentId));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建目录失败'); }
  };
  const removeDirectory = async (directory: NodeDirectory) => {
    if (!await dialog.confirm({ title: `删除目录“${directory.name}”？`, message: '目录中的节点与直接子目录会提升至上级目录；同名冲突会拒绝删除。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setError('');
    try {
      const deletedIds = new Set(directoryDescendantIds(directory.id, directories));
      await api.deleteDirectory(directory.id);
      if (selectedNode?.directory_id && deletedIds.has(selectedNode.directory_id)) setSelectedNodeId(undefined);
      await refreshAssets();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '删除目录失败'); }
  };
  const removeNode = async (node: NodeAsset) => {
    if (!await dialog.confirm({ title: `删除节点“${node.name}”？`, message: '删除后将不再出现在节点目录中。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setDeletingNodeId(node.id); setError(''); setNotice('');
    try {
      const result = await api.deleteNodes([node.id]);
      setBlockedAssets(result.blocked);
      if (result.deleted_ids.includes(node.id)) { setSelectedNodeId(undefined); setNotice('节点已删除。'); }
      if (result.blocked.length) setError('该节点仍绑定有效流程，暂时无法删除。');
      await queryClient.invalidateQueries({ queryKey: ['nodes'] });
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'NODE_ASSET_IN_USE' && Array.isArray(reason.details.assets)) setBlockedAssets(reason.details.assets as BlockedAsset[]);
      else setError(reason instanceof Error ? reason.message : '删除节点失败');
    } finally { setDeletingNodeId(undefined); }
  };
  const saveNode = async (data: Parameters<typeof api.createNode>[0]) => {
    const saved = selectedNode ? await api.updateNode(selectedNode.id, data) : await api.createNode(data);
    setSelectedNodeId(saved.id); setCreatingInDirectory(undefined);
    await queryClient.invalidateQueries({ queryKey: ['nodes'] });
    return saved;
  };
  const copyNode = async (node: NodeAsset) => {
    if (copyingNodeId) return;
    setCopyingNodeId(node.id); setError(''); setNotice('');
    const names = new Set(nodes.filter(item => item.directory_id === node.directory_id).map(item => item.name));
    const baseName = `${node.name} 副本`;
    let name = baseName;
    let copyIndex = 2;
    while (names.has(name)) name = `${baseName} ${copyIndex++}`;
    try {
      const copied = await api.createNode({
        directory_id: node.directory_id ?? null,
        name,
        description: node.description,
        icon_kind: node.icon_kind,
        icon_value: node.icon_value,
        inputs: node.inputs,
        outputs: node.outputs,
        executor: node.executor ?? { startup_prompt: '', context_prompt: '', context_capability_ids: [] },
      });
      setNotice(`节点“${copied.name}”已复制。`);
      await queryClient.invalidateQueries({ queryKey: ['nodes'] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '复制节点失败'); }
    finally { setCopyingNodeId(undefined); }
  };
  const renderNode = (node: NodeAsset) => <div className={`node-tree-record${selectedNodeId === node.id ? ' selected' : ''}`} key={node.id}>
    <button type="button" className="node-tree-select" data-testid="node-card" aria-current={selectedNodeId === node.id ? 'true' : undefined} onClick={() => { setSelectedNodeId(node.id); setCreatingInDirectory(undefined); }}><span className="node-icon">{node.icon_value.slice(0, 2).toUpperCase()}</span><span><b>{node.name}</b><small>{node.description || '暂无说明'}</small></span></button>
    <div className="node-record-actions"><button type="button" title="复制节点" aria-label={`复制节点 ${node.name}`} disabled={copyingNodeId === node.id} onClick={() => void copyNode(node)}><Copy size={14}/></button><button type="button" title="删除节点" aria-label={`删除节点 ${node.name}`} disabled={deletingNodeId === node.id} onClick={() => void removeNode(node)}><Trash2 size={14}/></button></div>
  </div>;
  const renderDirectory = (directory: NodeDirectory, depth: number): React.ReactNode => {
    const children = directories.filter(item => item.parent_id === directory.id);
    const directoryNodes = visibleNodes.filter(node => node.directory_id === directory.id);
    const hasVisibleDescendant = directoryDescendantIds(directory.id, directories).some(id => visibleNodes.some(node => node.directory_id === id));
    if (normalizedSearch && !hasVisibleDescendant) return null;
    const expanded = Boolean(normalizedSearch || expandedDirectoryIds.has(directory.id));
    return <section className="node-directory-group" key={directory.id} style={{ '--node-depth': depth } as React.CSSProperties}>
      <div className="node-directory-row">
        <button type="button" className="node-directory-toggle" aria-label={`${expanded ? '收起' : '展开'}目录 ${directory.name}`} aria-expanded={expanded} disabled={Boolean(normalizedSearch)} onClick={() => toggleDirectory(directory.id)}>{children.length || directoryNodes.length ? (expanded ? <ChevronDown size={14}/> : <ChevronRight size={14}/>) : <span/>}</button>
        <button type="button" className="node-directory-select" onClick={() => toggleDirectory(directory.id)}>{expanded ? <FolderOpen size={15}/> : <Folder size={15}/>}<span><b>{directory.name}</b><small>{directoryNodes.length} 个节点</small></span></button>
        <div className="node-record-actions"><button type="button" title="在此目录新建节点" aria-label={`在 ${directory.name} 新建节点`} onClick={() => { setSelectedNodeId(undefined); setCreatingInDirectory(directory.id); }}><Plus size={14}/></button><button type="button" title="新建子目录" aria-label={`在 ${directory.name} 新建子目录`} onClick={() => void createDirectory(directory.id)}><Folder size={14}/></button><button type="button" title="删除目录" aria-label={`删除目录 ${directory.name}`} onClick={() => void removeDirectory(directory)}><Trash2 size={14}/></button></div>
      </div>
      {expanded && <div className="node-tree-children">{children.map(child => renderDirectory(child, depth + 1))}{directoryNodes.map(renderNode)}</div>}
    </section>;
  };
  const uncategorizedNodes = visibleNodes.filter(node => !node.directory_id);
  const isCreating = creatingInDirectory !== undefined;

  return <section className="page node-assets-page">
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    <div className="node-center-layout">
      <aside className="node-center-rail" aria-label="节点目录">
        <header className="node-center-list-head"><div><span className="eyebrow">NODE CENTER</span><b>节点目录</b><small>{nodes.length} 个节点</small></div><button type="button" className="secondary" onClick={() => void createDirectory(null)}><Plus size={14}/>新建目录</button></header>
        <label className="node-center-search"><Search size={14}/><input aria-label="搜索节点" value={search} placeholder="搜索节点" onChange={event => setSearch(event.target.value)}/></label>
        <div className="node-center-tree">{isLoading ? <p className="node-center-empty">正在加载…</p> : <>{roots.map(directory => renderDirectory(directory, 0))}<section className="node-directory-group node-uncategorized-group"><div className="node-directory-row"><span className="node-directory-toggle"><Folder size={14}/></span><span className="node-directory-select"><span><b>未分类</b><small>{uncategorizedNodes.length} 个节点</small></span></span><div className="node-record-actions"><button type="button" title="新建未分类节点" aria-label="新建未分类节点" onClick={() => { setSelectedNodeId(undefined); setCreatingInDirectory(null); }}><Plus size={14}/></button></div></div><div className="node-tree-children">{uncategorizedNodes.map(renderNode)}</div></section>{!visibleNodes.length && <p className="node-center-empty">没有匹配的节点。</p>}</>}</div>
      </aside>
      <main className="node-center-detail">{selectedNode || isCreating ? <NodeEditor key={selectedNode?.id ?? `new-${creatingInDirectory ?? 'uncategorized'}`} embedded node={selectedNode} initialDirectoryId={creatingInDirectory} onClose={() => { setSelectedNodeId(undefined); setCreatingInDirectory(undefined); }} onSave={saveNode}/> : <div className="node-center-detail-empty"><Folder size={32}/><b>选择或新建一个节点</b><span>从左侧目录中选择节点，即可在此查看和修改完整配置。</span></div>}</main>
    </div>
    {blockedAssets.length > 0 && <DeleteBlockedDialog assets={blockedAssets} onClose={() => setBlockedAssets([])} onOpenFlows={() => { setBlockedAssets([]); setView('flows'); }}/>}
  </section>;
}
