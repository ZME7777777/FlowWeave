import { useQuery } from '@tanstack/react-query';
import { Braces, Check, ChevronDown, ChevronRight, Download, File, FileCode2, Folder, Layers3, LoaderCircle, Save, X } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { api, capabilityPackageUrl } from '../api/client';
import type { CapabilityAsset, CapabilityPackageComparison } from '../types';
import { FileDiff } from './FileDiff';
import { filePreviewLanguage, highlightedCode } from './fileDiffModel';
import { useEscapeClose } from './useEscapeClose';
import { useProductDialog } from './ProductDialogContext';
import './capability-package.css';

const MANIFEST = '\0manifest';
const statusLabels = { added: '新增', deleted: '删除', modified: '修改', unchanged: '未变更' };
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : '能力包暂时无法读取';
const sizeText = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(1)} MiB`;

type FileItem = { path: string; status?: keyof typeof statusLabels };
type TreeNode = { name: string; path: string; children: TreeNode[]; file?: FileItem };
function fileTree(files: FileItem[]): TreeNode[] {
  const root: TreeNode[] = [];
  files.forEach(file => {
    if (!file.path) return;
    let nodes = root;
    const parts = file.path.split('/');
    parts.forEach((name, index) => {
      const path = parts.slice(0, index + 1).join('/');
      let node = nodes.find(item => item.name === name);
      if (!node) { node = { name, path, children: [] }; nodes.push(node); }
      if (index === parts.length - 1) node.file = file;
      nodes = node.children;
    });
  });
  const sort = (nodes: TreeNode[]) => { nodes.sort((a, b) => Number(Boolean(a.file)) - Number(Boolean(b.file)) || a.name.localeCompare(b.name)); nodes.forEach(node => sort(node.children)); };
  sort(root);
  return root;
}
function PackageTree({ files, selected, drafts, onSelect, compiled, manifest }: { files: FileItem[]; selected: string; drafts: Record<string, string>; onSelect: (path: string) => void; compiled: boolean; manifest: boolean }) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const tree = useMemo(() => fileTree(files), [files]);
  const render = (nodes: TreeNode[], depth = 0): ReactNode => nodes.map(node => node.file
    ? <button type="button" role="treeitem" aria-selected={selected === node.path} className={`capability-package-tree-file ${selected === node.path ? 'active' : ''}`} style={{ paddingLeft: 12 + depth * 14 }} key={node.path} title={node.path} onClick={() => onSelect(node.path)}><FileCode2 size={14}/><span>{node.name}</span>{Object.hasOwn(drafts, node.path) && <i className="capability-package-draft" aria-label="未发布修改"/>}{node.file.status && <small className={node.file.status}>{statusLabels[node.file.status]}</small>}</button>
    : <div role="treeitem" aria-expanded={!collapsed.has(node.path)} key={node.path}><button type="button" className="capability-package-tree-folder" style={{ paddingLeft: 10 + depth * 14 }} onClick={() => setCollapsed(current => { const next = new Set(current); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); return next; })}>{collapsed.has(node.path) ? <ChevronRight size={12}/> : <ChevronDown size={12}/>}<Folder size={14}/><span>{node.name}</span></button>{!collapsed.has(node.path) && <div role="group">{render(node.children, depth + 1)}</div>}</div>);
  return <nav className="capability-package-tree" aria-label="包文件"><div role="tree" aria-label="文件目录">{compiled && <button type="button" role="treeitem" aria-selected={selected === ''} className={`capability-package-tree-file compiled ${selected === '' ? 'active' : ''}`} onClick={() => onSelect('')}><Layers3 size={14}/><span>总内容</span>{files.find(file => file.path === '')?.status && <small className={files.find(file => file.path === '')!.status}>{statusLabels[files.find(file => file.path === '')!.status!]}</small>}</button>}{manifest && <button type="button" role="treeitem" aria-selected={selected === MANIFEST} className={`capability-package-tree-file ${selected === MANIFEST ? 'active' : ''}`} onClick={() => onSelect(MANIFEST)}><Braces size={14}/><span>包配置</span></button>}{render(tree)}</div></nav>;
}

export function CapabilityPackageDialog({ item, comparison = false, onClose, onPublished }: { item: CapabilityAsset; comparison?: boolean; onClose: () => void; onPublished: () => Promise<void> }) {
  const productDialog = useProductDialog();
  const [selected, setSelected] = useState<string | undefined>(comparison ? undefined : item.capability_type === 'CONTEXT' ? '' : 'SKILL.md');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [showUnchanged, setShowUnchanged] = useState(false);
  const close = async () => {
    if (saving) return;
    if (Object.keys(drafts).length && !await productDialog.confirm({ title: '放弃未发布的修改？', message: '当前包内的文件修改尚未发布。', confirmLabel: '放弃修改', tone: 'danger' })) return;
    onClose();
  };
  useEscapeClose(() => { void close(); }, true);
  const filesQuery = useQuery({ queryKey: ['capability-package-files', item.id], queryFn: ({ signal }) => api.capabilityFiles(item.id, signal), enabled: !comparison });
  const comparisonQuery = useQuery({ queryKey: ['capability-package-comparison', item.id], queryFn: ({ signal }) => api.capabilityComparison(item.id, signal), enabled: comparison });
  const packageInfo = filesQuery.data;
  const compare = comparisonQuery.data;
  const compareFiles = compare?.files ?? [];
  const changed = compareFiles.filter(file => file.status !== 'unchanged');
  const visibleFiles = comparison ? (showUnchanged || !changed.length ? compareFiles : changed) : packageInfo?.files ?? [];
  // Choose a changed file on first open, while keeping each later explicit selection stable.
  const preferred = item.capability_type === 'CONTEXT' && compareFiles.some(file => file.path === '') ? '' : changed[0]?.path ?? compareFiles[0]?.path ?? '';
  const path = comparison ? selected !== undefined && visibleFiles.some(file => file.path === selected) ? selected : visibleFiles.some(file => file.path === preferred) ? preferred : visibleFiles[0]?.path ?? '' : selected ?? '';
  const fileQuery = useQuery({ queryKey: ['capability-package-file', item.id, path], queryFn: ({ signal }) => api.capabilityFile(item.id, path, signal), enabled: !comparison && Boolean(packageInfo) && path !== '' && path !== MANIFEST });
  const diffQuery = useQuery({ queryKey: ['capability-package-file-diff', item.id, path], queryFn: ({ signal }) => api.capabilityFileDiff(item.id, path, signal), enabled: comparison && Boolean(compare) && compareFiles.some(file => file.path === path) });
  const file = fileQuery.data;
  const text = path === '' ? packageInfo?.compiled_content : path === MANIFEST ? JSON.stringify({ manifest: packageInfo?.manifest, dependencies: item.dependencies, digest: packageInfo?.digest, filename: packageInfo?.filename, version_no: packageInfo?.version_no }, null, 2) : file?.content;
  const editable = !comparison && path !== MANIFEST && Boolean(file?.editable);
  const failure = saveError || errorTextOrEmpty(comparisonQuery.error ?? filesQuery.error ?? fileQuery.error ?? diffQuery.error);
  const loading = comparison ? comparisonQuery.isPending || Boolean(compare && diffQuery.isPending) : filesQuery.isPending || (path !== '' && path !== MANIFEST && fileQuery.isPending);
  const save = async () => {
    if (!packageInfo || !Object.keys(drafts).length) return;
    setSaving(true); setSaveError('');
    try { await api.updateCapabilityFiles(item.id, packageInfo.digest, drafts); await onPublished(); onClose(); }
    catch (reason) { setSaveError(errorText(reason)); }
    finally { setSaving(false); }
  };
  const retry = () => { void filesQuery.refetch(); if (comparison) void comparisonQuery.refetch(); else if (path && path !== MANIFEST) void fileQuery.refetch(); if (comparison && compare) void diffQuery.refetch(); };
  return <div className="modal-backdrop capability-package-backdrop"><section className="modal capability-package-dialog" role="dialog" aria-modal="true" aria-label={`${comparison ? '版本对比' : item.capability_type === 'SKILL' ? '编辑 Skill' : '查看 Context'} ${item.capability_key}`}>
    <header className="capability-package-header"><div><span>{comparison ? '版本对比' : item.capability_type}</span><h2 title={item.capability_key}>{item.capability_key}</h2></div><div className="capability-package-header-actions">{compare ? <ComparisonDownloads comparison={compare}/> : <a href={capabilityPackageUrl(item.id)} download aria-label={`下载 ${item.capability_type === 'SKILL' ? 'Skill' : 'Context'} ${item.capability_key}`}><Download size={15}/><span>下载</span></a>}<button type="button" aria-label="关闭" title="关闭" onClick={() => void close()} disabled={saving}><X size={18}/></button></div></header>
    <div className="capability-package-body"><aside><div className="capability-package-directory-heading"><span>{comparison ? `${changed.length} 个文件变更` : `${packageInfo?.files.length ?? '…'} 个文件`}</span>{comparison && <label><input type="checkbox" checked={showUnchanged} onChange={event => setShowUnchanged(event.target.checked)}/>未变更</label>}</div><PackageTree files={visibleFiles} selected={path} drafts={drafts} onSelect={setSelected} compiled={item.capability_type === 'CONTEXT' && (!comparison || visibleFiles.some(file => file.path === ''))} manifest={!comparison} /></aside>
      <main className="capability-package-content"><div className="capability-package-file-heading"><File size={14}/><span title={path}>{path === '' ? '总内容' : path === MANIFEST ? '包配置' : path}</span>{!comparison && file && path && path !== MANIFEST && <small>{sizeText(file.byte_size)}{editable ? ' · 可编辑' : ''}</small>}{Object.hasOwn(drafts, path) && <small>已修改</small>}</div>
        {loading ? <div className="capability-package-empty" role="status"><LoaderCircle size={18} className="spinning"/>正在读取文件…</div> : comparison && diffQuery.data ? <FileDiff key={path} path={path || 'compiled.txt'} patch={diffQuery.data.diff} unavailable={diffQuery.data.preview_unavailable}/> : editable && typeof text === 'string' ? <textarea spellCheck={false} aria-label={`编辑 ${path}`} value={drafts[path] ?? text} disabled={saving} onChange={event => setDrafts(current => { const next = { ...current }; if (event.target.value === text) delete next[path]; else next[path] = event.target.value; return next; })}/> : typeof text === 'string' ? <pre className="capability-package-source"><code dangerouslySetInnerHTML={{ __html: highlightedCode(text, path === MANIFEST ? 'json' : filePreviewLanguage(path)) }}/></pre> : file?.image ? <div className="capability-package-image"><img src={file.image} alt={path}/></div> : !failure ? <div className="capability-package-empty">该文件为二进制或超过 1 MiB，请下载完整包查看。</div> : null}
      </main></div>
    {failure && <div className="capability-package-error" role="alert"><span>{failure}</span>{!saveError && <button type="button" onClick={retry}>重试</button>}</div>}
    <footer><span>{comparison ? '上一版本 → 当前版本' : item.capability_type === 'SKILL' ? '发布保留两版；已有会话继续使用冻结版本。' : '已冻结的上下文 · 默认展示总内容'}</span>{!comparison && item.capability_type === 'SKILL' ? <button type="button" className="primary" disabled={saving || !Object.keys(drafts).length} onClick={() => void save()}>{saving ? <LoaderCircle size={14} className="spinning"/> : <Save size={14}/>} {saving ? '发布中…' : `发布新版本${Object.keys(drafts).length ? `（${Object.keys(drafts).length}）` : ''}`}</button> : <button type="button" className="ghost" onClick={() => void close()}><Check size={14}/>完成</button>}</footer>
  </section></div>;
}
function errorTextOrEmpty(reason: unknown) { return reason ? errorText(reason) : ''; }
function ComparisonDownloads({ comparison }: { comparison: CapabilityPackageComparison }) {
  return <>{(['previous', 'current'] as const).map(version => <a key={version} href={capabilityPackageUrl(comparison[version].id)} download aria-label={`下载${version === 'previous' ? '上一' : '当前'}版本`}><Download size={14}/><span>{version === 'previous' ? '上一版' : '当前版'}</span></a>)}</>;
}
