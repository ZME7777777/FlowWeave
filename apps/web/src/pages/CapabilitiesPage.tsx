import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckSquare, FileArchive, Pencil, PlugZap, Search, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { BlockedCapabilityDelete, CapabilityAsset, CapabilityRef } from '../types';

const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
const CONFIG_MAX_BYTES = 1024 * 1024;
const DEPENDENCY_EXAMPLE = `dependencies:
  python:
    requests: 2.32.3
  node:
    lodash: 4.17.21
  cli:
    lark-cli: 1.0.84`;

interface CapabilityLineage {
  id: string;
  latest: CapabilityAsset;
  versions: CapabilityAsset[];
}

function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  const chunks: string[] = [];
  for (let start = 0; start < bytes.length; start += 0x8000) chunks.push(String.fromCharCode(...bytes.subarray(start, start + 0x8000)));
  return btoa(chunks.join(''));
}
function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}
function typeLabel(type: CapabilityRef['capability_type']): string { return type === 'SKILL' ? 'Skill' : type; }
function errorMessage(reason: unknown): string {
  if (reason instanceof ApiError && reason.code === 'CAPABILITY_IN_USE') {
    const blocked = reason.details.blocked;
    if (Array.isArray(blocked)) return blockedCapabilityMessage(blocked as BlockedCapabilityDelete[]);
    return '能力仍被节点引用，请先从相关节点移除后再删除。';
  }
  return reason instanceof Error ? reason.message : '操作失败';
}
function blockedCapabilityMessage(blocked: BlockedCapabilityDelete[]): string {
  return `以下能力仍有关联，已跳过：${blocked.map(item => `“${item.name}”绑定节点 ${item.nodes.map(node => `“${node.name}”`).join('、')}`).join('；')}。`;
}
function groupCapabilities(capabilities: CapabilityAsset[]): CapabilityLineage[] {
  const groups = new Map<string, CapabilityAsset[]>();
  capabilities.forEach(item => groups.set(item.lineage_id, [...(groups.get(item.lineage_id) ?? []), item]));
  return [...groups.entries()].map(([id, items]) => {
    const versions = [...items].sort((left, right) => right.revision_number - left.revision_number);
    return { id, versions, latest: versions.find(item => item.is_latest) ?? versions[0] };
  }).sort((left, right) => right.latest.created_at.localeCompare(left.latest.created_at));
}

export function CapabilitiesPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const { data: capabilities = [], isLoading } = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const [type, setType] = useState<'ALL' | CapabilityRef['capability_type']>('ALL');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<CapabilityAsset>();
  useEscapeClose(() => setEditing(undefined), Boolean(editing));
  const [source, setSource] = useState('');
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState<CapabilityRef['capability_type']>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const lineages = useMemo(() => groupCapabilities(capabilities), [capabilities]);
  const visible = useMemo(() => lineages.filter(group =>
    (type === 'ALL' || group.latest.capability_type === type)
    && (!search || group.versions.some(item => `${item.capability_key} ${item.description} ${item.filename}`.toLowerCase().includes(search.toLowerCase()))),
  ), [lineages, type, search]);
  const allVisibleSelected = visible.length > 0 && visible.every(item => selected.has(item.id));
  const selectedIds = lineages.filter(item => selected.has(item.id)).flatMap(item => item.versions.map(record => record.id));

  const refresh = async () => { setSelected(new Set()); await qc.invalidateQueries({ queryKey: ['capabilities'] }); };
  const importFile = async (file: File, capabilityType: CapabilityRef['capability_type']) => {
    setImporting(capabilityType); setError(''); setNotice('');
    try {
      const maxBytes = capabilityType === 'SKILL' ? SKILL_ZIP_MAX_BYTES : CONFIG_MAX_BYTES;
      if (file.size > maxBytes) throw new Error(`${capabilityType === 'SKILL' ? 'Skill ZIP' : '配置文件'}不能超过 ${capabilityType === 'SKILL' ? '25 MiB' : '1 MiB'}。`);
      const validated = await api.validateCapability({ capability_type: capabilityType, filename: file.name, content_base64: toBase64(await file.arrayBuffer()) });
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); setNotice(`已从 ${file.name} 导入 ${committed.capabilities.length} 项 ${typeLabel(capabilityType)} 能力。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImporting(undefined); }
  };
  const remove = async (ids: string[], capabilityCount: number) => {
    if (!ids.length || !await dialog.confirm({ title: `删除所选的 ${capabilityCount} 项能力？`, message: '有关联的记录会保留，其余记录会直接删除。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.deleteCapabilities(ids);
      const blockedIds = new Set(result.blocked.map(item => item.id));
      setSelected(new Set(lineages.filter(group => group.versions.some(item => blockedIds.has(item.id))).map(group => group.id)));
      await qc.invalidateQueries({ queryKey: ['capabilities'] });
      if (result.deleted_ids.length) setNotice(`已删除 ${result.deleted_ids.length} 条无关联能力记录。`);
      if (result.blocked.length) setError(blockedCapabilityMessage(result.blocked));
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const openEditor = async (item: CapabilityAsset) => {
    setBusy(true); setError('');
    try { const loaded = await api.capabilitySource(item.id); setSource(loaded.content); setEditing(item); }
    catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const saveSource = async () => {
    if (!editing) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api.updateCapabilitySource(editing.id, source);
      setEditing(undefined); await refresh(); setNotice(`已保存 ${editing.capability_key}，使用该能力的节点已同步更新。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const toggle = (id: string) => setSelected(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleVisible = () => setSelected(old => {
    const next = new Set(old);
    if (allVisibleSelected) visible.forEach(item => next.delete(item.id));
    else visible.forEach(item => next.add(item.id));
    return next;
  });
  const count = (capabilityType: CapabilityRef['capability_type']) => lineages.filter(item => item.latest.capability_type === capabilityType).length;

  return <section className="page capabilities-page">
    <div className="page-head"><div><span className="eyebrow">CAPABILITY REPOSITORY</span><h1>能力仓库</h1><p>统一上传和管理 Skill、MCP 与 Hook；Skill 可直接编辑并保存，不会额外生成一份。</p></div><div className="capability-import-actions">{(['SKILL', 'MCP', 'HOOK'] as const).map(item => <label className="primary file-button" key={item}><Upload size={15}/>{importing === item ? '上传中…' : item === 'SKILL' ? '上传 Skill ZIP' : `上传 ${item}`}<input type="file" disabled={Boolean(importing)} accept={item === 'SKILL' ? '.zip' : '.json,.yaml,.yml'} onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFile(file, item); }}/></label>)}</div></div>
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    <section className="capability-guidance"><ShieldCheck size={20}/><div><b>安全导入、直接保存与隔离依赖</b><span>编辑会原地更新当前 Skill，并同步使用它的节点；不会发布新版或创建重复能力。</span></div></section>
    <div className="capability-tools"><div className="capability-type-tabs">{(['ALL', 'SKILL', 'MCP', 'HOOK'] as const).map(item => <button key={item} className={type === item ? 'active' : ''} onClick={() => setType(item)}>{item === 'ALL' ? '全部' : typeLabel(item)} <span>{item === 'ALL' ? lineages.length : count(item)}</span></button>)}</div><label><Search size={15}/><input aria-label="搜索能力仓库" value={search} placeholder="搜索名称、说明或来源文件" onChange={event => setSearch(event.target.value)}/></label><button className="secondary" disabled={!visible.length} onClick={toggleVisible}><CheckSquare size={14}/>{allVisibleSelected ? '取消全选' : `全选当前结果（${visible.length}）`}</button>{selected.size > 0 && <button className="danger" disabled={busy} onClick={() => void remove(selectedIds, selected.size)}><Trash2 size={14}/>删除所选能力（{selected.size}）</button>}</div>
    {isLoading ? <div className="empty">加载能力仓库…</div> : visible.length ? <div className="capability-card-grid">{visible.map(group => <CapabilityCard key={group.id} group={group} selected={selected.has(group.id)} onToggle={() => toggle(group.id)} onEdit={() => void openEditor(group.latest)} onDelete={() => void remove(group.versions.map(item => item.id), 1)}/>)}</div> : <div className="empty"><FileArchive size={30}/><b>{lineages.length ? '没有匹配能力' : '能力仓库尚为空'}</b><span>{lineages.length ? '调整类型或搜索条件。' : '从右上角上传 Skill ZIP、MCP 或 Hook 配置。'}</span></div>}
    {editing && <div className="modal-backdrop"><section className="modal capability-source-editor" role="dialog" aria-label={`编辑 Skill ${editing.capability_key}`}><header><div><span className="eyebrow">EDIT SKILL</span><h2>编辑 {editing.capability_key}</h2></div><button className="ghost" onClick={() => setEditing(undefined)}>关闭</button></header><p>保存会直接更新当前 Skill，并同步到正在使用该能力的节点，不会生成新的能力记录。</p><textarea aria-label="Skill 源码" value={source} onChange={event => setSource(event.target.value)}/><div className="dependency-policy"><b>声明依赖（写入 SKILL.md frontmatter）</b><code>{DEPENDENCY_EXAMPLE}</code><span>所有版本必须精确固定。CLI 必须在平台白名单中；不接受终端命令。</span></div><footer><button className="ghost" onClick={() => setEditing(undefined)}>取消</button><button className="primary" disabled={busy} onClick={() => void saveSource()}>{busy ? '保存中…' : '保存'}</button></footer></section></div>}
  </section>;
}

interface CardProps { group: CapabilityLineage; selected: boolean; onToggle: () => void; onEdit: () => void; onDelete: () => void }
function CapabilityCard({ group, selected, onToggle, onEdit, onDelete }: CardProps) {
  const item = group.latest;
  const dependencyLabel = item.dependency_build_state === 'READY' ? '依赖可用' : item.dependency_build_state === 'PENDING' ? '依赖构建中' : item.dependency_build_state === 'FAILED' ? '依赖构建失败' : '无需额外依赖';
  const totalReferences = group.versions.reduce((total, version) => total + version.reference_count, 0);
  return <article className={`capability-card ${selected ? 'selected' : ''}`}><header><label className="capability-select"><input type="checkbox" aria-label={`选择能力 ${item.capability_key}`} checked={selected} onChange={onToggle}/></label><span className={`capability-card-icon ${item.capability_type.toLowerCase()}`}>{item.capability_type === 'SKILL' ? <FileArchive size={18}/> : <PlugZap size={18}/>}</span><span className="cap-type">{typeLabel(item.capability_type)}</span></header><h3>{item.capability_key}</h3><p>{item.description || '暂无能力说明'}</p><div className={`dependency-state ${item.dependency_build_state.toLowerCase()}`} title={item.dependency_build_error || ''}>{dependencyLabel}{item.dependency_build_error ? `：${item.dependency_build_error}` : ''}</div><dl><dt>来源文件</dt><dd>{item.filename}</dd><dt>文件大小</dt><dd>{formatBytes(item.byte_size)}</dd><dt>更新时间</dt><dd>{new Date(item.created_at).toLocaleString()}</dd><dt>节点引用</dt><dd>{totalReferences} 个</dd></dl><footer>{item.capability_type === 'SKILL' && <button className="secondary" onClick={onEdit}><Pencil size={13}/>编辑</button>}<button className="ghost" title={totalReferences > 0 ? '有关联的记录会保留并说明绑定节点，其余记录直接删除' : '删除能力'} onClick={onDelete}><Trash2 size={13}/>删除能力</button></footer></article>;
}
