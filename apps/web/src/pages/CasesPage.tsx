import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ArrowDown, Bot, Boxes, Check, ChevronDown, ChevronRight, CirclePause, CirclePlay, ClipboardList, Clock3, ExternalLink, FileText, GitBranch, GripVertical, Copy, Download, Info, Link2, ListChecks, LoaderCircle, Pencil, Play, Plus, RotateCcw, Search, Settings2, Trash2, Upload, X } from 'lucide-react';
import { api, caseMaterialFileUrl } from '../api/client';
import type { Case, CaseBasics, CaseMaterial, CaseStage, CaseStageExecution } from '../types';
import { CapabilityManager } from '../components/agent-session/AgentSessionWorkbench';
import { CaseSessionPage } from './CaseSessionPage';
import { useEscapeClose } from '../components/useEscapeClose';
import { useProductDialog } from '../components/ProductDialogContext';
import { CaseChoice } from './CaseChoice';
import { withoutDeploymentBase } from '../deploymentPath';
import './cases.css';

const CASE_STATE: Record<Case['state'], string> = { DRAFT: '草稿', ACTIVE: '进行中', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVED: '已归档' };
const STAGE_STATE: Record<CaseStage['state'], string> = { PENDING: '待启动', ACTIVE: '待启动', REVIEW_PENDING: '等待 Agent 复核', COMPLETED: '已完成', SKIPPED: '已跳过' };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '保存失败，请重试。';
const stageExecutionLabel = (stage: CaseStage) => {
  if (stage.state === 'REVIEW_PENDING') return '等待 Agent 复核';
  if (!stage.current_execution_flow_run_id) return stage.state === 'ACTIVE' ? '当前阶段' : STAGE_STATE[stage.state];
  const state = stage.execution?.state;
  if (state === 'COMPLETED') return '执行完成';
  if (state === 'FAILED') return '执行失败';
  if (state === 'CANCELLED') return '已停止';
  if (state === 'MISSING') return '记录不可用';
  if (state === 'DRAFT') return '待启动';
  if (state === 'WAITING_HUMAN' || stage.execution?.automatic_block) return '等待处理';
  return state ? '运行中' : '同步中';
};

const nodeStateLabel = (state: string) => ({
  PENDING: '待启动', WAITING_INPUT: '待补充', WAITING_START_CONFIRMATION: '待启动',
  EXECUTING: '执行中', WAITING_HUMAN: '待处理', WAITING_CONFIRMATION: '待确认',
  WAITING_ACCEPTANCE: '待验收', ACCEPTED: '已完成', REJECTED: '已退回',
  CANCELLED: '已取消', START_BLOCKED: '启动受阻', END_BLOCKED: '完成受阻',
  FAILED: '执行失败', ACTIVE: '执行中',
})[state] ?? state;

function openFlowRunFromCase(path: string, onNavigate: (path: string, replace?: boolean) => void) {
  const state = { ...(window.history.state ?? {}), flowweavePendingReturn: { path: `${withoutDeploymentBase(window.location.pathname)}${window.location.search}${window.location.hash}`, view: 'cases' } };
  window.history.replaceState(state, '', window.location.href);
  onNavigate(path);
}

/** Lay the stage's nodes out as a left-to-right layered DAG (longest-path layering). */
function layoutStageGraph(nodes: NonNullable<CaseStageExecution['nodes']>, edges: NonNullable<CaseStageExecution['edges']>) {
  const keys = new Set(nodes.map(node => node.node_key));
  const valid = edges.filter(edge => keys.has(edge.source) && keys.has(edge.target) && edge.source !== edge.target);
  const layer = new Map<string, number>(nodes.map(node => [node.node_key, 0]));
  // The graph is acyclic by contract; the bound only protects the UI from malformed data.
  for (let pass = 0; pass < nodes.length; pass += 1) {
    let changed = false;
    for (const edge of valid) {
      const next = (layer.get(edge.source) ?? 0) + 1;
      if (next > (layer.get(edge.target) ?? 0)) { layer.set(edge.target, next); changed = true; }
    }
    if (!changed) break;
  }
  const columns: string[][] = [];
  for (const node of nodes) { const column = layer.get(node.node_key) ?? 0; (columns[column] ??= []).push(node.node_key); }
  const NODE_W = 132, NODE_H = 34, GAP_X = 34, GAP_Y = 12;
  const position = new Map<string, { x: number; y: number }>();
  columns.forEach((column, x) => column?.forEach((key, y) => position.set(key, { x: x * (NODE_W + GAP_X), y: y * (NODE_H + GAP_Y) })));
  const width = Math.max(1, columns.length) * (NODE_W + GAP_X) - GAP_X;
  const height = Math.max(1, ...columns.map(column => column?.length ?? 0)) * (NODE_H + GAP_Y) - GAP_Y;
  return { edges: valid, position, width, height, NODE_W, NODE_H };
}

function StageFlowGraph({ stage }: { stage: CaseStage }) {
  const nodes = stage.execution?.nodes ?? [];
  const graph = useMemo(() => layoutStageGraph(nodes, stage.execution?.edges ?? []), [nodes, stage.execution?.edges]);
  if (!nodes.length) return null;
  return <div className="case-stage-graph" role="img" aria-label={`${stage.name} 流程图：${nodes.map(node => `${node.node_name} ${nodeStateLabel(node.state)}`).join('，')}`}>
    <div className="case-stage-graph-canvas" style={{ width: graph.width, height: graph.height }}>
      <svg width={graph.width} height={graph.height} aria-hidden="true">
        <defs><marker id={`case-arrow-${stage.id}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="#9fb4a6"/></marker></defs>
        {graph.edges.map(edge => {
          const from = graph.position.get(edge.source)!, to = graph.position.get(edge.target)!;
          const x1 = from.x + graph.NODE_W, y1 = from.y + graph.NODE_H / 2, x2 = to.x, y2 = to.y + graph.NODE_H / 2, mid = (x1 + x2) / 2;
          return <path key={`${edge.source}>${edge.target}`} d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2 - 1} ${y2}`} fill="none" stroke="#b4c6ba" strokeWidth="1.4" markerEnd={`url(#case-arrow-${stage.id})`}/>;
        })}
      </svg>
      {nodes.map(node => { const at = graph.position.get(node.node_key)!; return <span key={node.node_key} className={`case-flow-node ${node.active ? 'active' : ''} ${node.state.toLowerCase()}`} style={{ left: at.x, top: at.y, width: graph.NODE_W, height: graph.NODE_H }} title={`${node.node_name}：${nodeStateLabel(node.state)}`}>
        <i aria-hidden="true"/><b>{node.node_name}</b><small>{nodeStateLabel(node.state)}</small>
      </span>; })}
    </div>
  </div>;
}

function MaterialName({ name, file, onRename }: { name: string; file: boolean; onRename: (name: string) => void }) {
  const [editing, setEditing] = useState(false);
  // Keep the file type stable so renamed uploads retain preview and download behavior.
  const extension = file ? name.match(/\.[^.]+$/)?.[0] ?? '' : '';
  const [draft, setDraft] = useState('');
  const valid = Boolean(draft.trim()) && (!file || (!/[\\/]/.test(draft) && !draft.includes('\0')));
  const apply = () => { if (valid) { onRename(draft.trim() + extension); setEditing(false); } };
  return editing ? <span className="case-material-rename">
    <input aria-label={`资料名称 ${name}`} autoFocus value={draft} maxLength={220 - extension.length}
      onChange={event => setDraft(event.target.value)} onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === 'Enter') { event.preventDefault(); apply(); }
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setEditing(false); }
      }}/>{extension && <small>{extension}</small>}
    <button className="ghost" type="button" aria-label="确认改名" disabled={!valid} onClick={apply}><Check size={14}/></button>
    <button className="ghost" type="button" aria-label="取消改名" onClick={() => setEditing(false)}><X size={14}/></button>
  </span> : <button className="ghost case-material-rename-button" type="button" aria-label={`重命名 ${name}`} onClick={() => { setDraft(extension ? name.slice(0, -extension.length) : name); setEditing(true); }}><Pencil size={14}/></button>;
}

function MaterialPreview({ material, caseId, onClose }: {
  material: CaseMaterial; caseId: string; onClose: () => void;
}) {
  useEscapeClose(onClose);
  const text = /\.(txt|md|json|csv|yaml|yml|toml|log|py|ts|js|tsx|css|svg|html)$/i.test(material.name);
  const preview = useQuery({
    queryKey: ['case-material-preview', caseId, material.value],
    queryFn: () => api.caseMaterialPreview(caseId, material.value),
    enabled: text,
  });
  const url = caseMaterialFileUrl(caseId, material.value);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="modal scenario-material-preview" role="dialog" aria-modal="true" aria-label="资料预览">
      <header><h2>{material.name}</h2><button className="ghost" aria-label="关闭预览" onClick={onClose}><X size={17}/></button></header>
      {text ? <pre>{preview.isLoading ? '正在加载…' : preview.error ? errorMessage(preview.error) : preview.data}</pre>
        : /\.(png|jpe?g|gif|webp|avif)$/i.test(material.name) ? <img src={url} alt={material.name}/>
          : /\.pdf$/i.test(material.name) ? <iframe title={material.name} src={url} sandbox=""/> : <p>此文件可下载后查看。</p>}
      <footer><a className="secondary" href={caseMaterialFileUrl(caseId, material.value, true)}>下载文件</a></footer>
    </section>
  </div>;
}

function materialHref(value: string): string | undefined {
  try {
    const protocol = new URL(value, window.location.href).protocol;
    return ['javascript:', 'data:', 'vbscript:'].includes(protocol) ? undefined : value;
  } catch { return undefined; }
}

function AddMaterialDialog({ onImport, onDiscard, onApply, onClose }: {
  onImport: (file: File) => Promise<CaseMaterial>;
  onDiscard: (material: CaseMaterial) => Promise<void>;
  onApply: (material: CaseMaterial) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [imported, setImported] = useState<CaseMaterial>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const perform = async (action: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError('');
    try { await action(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { pending.current = false; setBusy(false); }
  };
  const cancel = () => void perform(async () => {
    if (imported) await onDiscard(imported);
    onClose();
  });
  useEscapeClose(cancel);
  return createPortal(<div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) cancel(); }}><form className="modal case-add-material-dialog" role="dialog" aria-modal="true" aria-label="添加资料" aria-busy={busy} onKeyDown={event => {
    if (event.key !== 'Tab') return;
    const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled):not([hidden])')].filter(element => !element.closest('fieldset:disabled'));
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }} onSubmit={event => {
    event.preventDefault(); event.stopPropagation();
    if (!name.trim() || !value.trim()) return;
    void perform(async () => {
      const file = imported && value === imported.value ? imported : undefined;
      if (imported && !file) await onDiscard(imported);
      onApply({ kind: file ? 'FILE' : 'LINK', name: name.trim(), value: file ? file.value : value.trim() });
      onClose();
    });
  }}><header><div><h2>添加资料</h2><p>填写资料地址，或导入文件到场景工作区。</p></div><button type="button" className="ghost" aria-label="关闭添加资料" disabled={busy} onClick={cancel}><X size={18}/></button></header>
    <fieldset disabled={busy}><label>资料名称<input autoFocus required aria-label="资料名称" value={name} maxLength={220} placeholder="为这份资料起个名称" onChange={event => setName(event.target.value)}/></label>
      <label>资料地址<span className="case-material-address"><input required aria-label="资料地址" value={value} maxLength={2000} placeholder="输入链接或导入文件" onChange={event => setValue(event.target.value)}/><button className="secondary" type="button" onClick={() => fileInput.current?.click()}><Upload size={15}/>{busy ? '导入中…' : '导入'}</button></span></label>
      <input hidden ref={fileInput} type="file" aria-label="导入资料文件" onChange={event => {
        const file = event.target.files?.[0]; event.target.value = '';
        if (!file) return;
        void perform(async () => {
          if (imported) { await onDiscard(imported); setImported(undefined); setValue(''); }
          const result = await onImport(file);
          setImported(result); setValue(result.value); setName(current => current || file.name);
        });
      }}/>
      {imported && value === imported.value && <p className="case-material-path-note">文件已导入，显示的是相对于工作区的路径。</p>}
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
    <footer><button className="secondary" type="button" disabled={busy} onClick={cancel}>取消</button><button className="primary" type="submit" disabled={busy || !name.trim() || !value.trim()}>{busy ? '处理中…' : '添加'}</button></footer>
  </form></div>, document.body);
}

function CompactTextField({ label, value, placeholder, onChange, readOnly = false }: {
  label: string; value: string; placeholder: string; readOnly?: boolean; onChange: (value: string) => void;
}) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    const measure = () => setTruncated(element.scrollHeight > element.clientHeight + 1);
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, [value]);
  return <div className="case-compact-text-field">
    <span><label htmlFor={`case-${label}`}>{label}</label>{truncated && <button type="button" className="ghost" aria-haspopup="dialog" onClick={() => dialog.current?.showModal()}>查看原文<ChevronRight size={13}/></button>}</span>
    <textarea ref={textarea} id={`case-${label}`} aria-label={label} value={value} readOnly={readOnly} maxLength={20_000} onChange={event => onChange(event.target.value)} placeholder={placeholder}/>
    {createPortal(<dialog ref={dialog} className="case-text-dialog" aria-label={`${label}全文`} onClick={event => { if (event.target === event.currentTarget) dialog.current?.close(); }}>
      <section><header><h3>{label}</h3><button type="button" className="ghost" aria-label={`关闭${label}全文`} onClick={() => dialog.current?.close()}><X size={17}/></button></header><p>{value || '暂无内容'}</p></section>
    </dialog>, document.body)}
  </div>;
}


function BasicsForm({ item, workspaceId, onSaved, onCancel, formId, showActions = true }: {
  item?: Case; workspaceId: string; onSaved: (item: Case) => void; onCancel?: () => void; formId?: string; showActions?: boolean;
}) {
  const directories = useQuery({ queryKey: ['agent-work-directories', workspaceId], queryFn: () => api.agentWorkDirectories(workspaceId), enabled: Boolean(workspaceId) });
  const [name, setName] = useState(item?.name ?? '');
  const [description, setDescription] = useState(item?.description ?? '');
  const [objective, setObjective] = useState(item?.objective ?? '');
  const [directoryId, setDirectoryId] = useState(item?.work_directory_id ?? '');
  const [materials, setMaterials] = useState<CaseMaterial[]>(item?.materials ?? []);
  const [materialOpen, setMaterialOpen] = useState(false);
  const materialButton = useRef<HTMLButtonElement>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<CaseMaterial>();
  // Preserve the newest server version while managing uploaded materials.
  const saved = useRef(item);
  const importMaterial = async (file: File): Promise<CaseMaterial> => {
    if (!name.trim() || !workspaceId) throw new Error('请先填写场景名称，再导入文件。');
    if (materials.length >= 100) throw new Error('最多添加 100 项资料。');
    if (file.size > 64 * 1024 * 1024) throw new Error('单个文件不能超过 64 MB。');
    setBusy(true); setNotice('');
    try {
      const current = saved.current && (!item || saved.current.row_version > item.row_version) ? saved.current : item;
      if (!current) throw new Error('请先创建场景，再添加资料。');
      const known = new Set((current.materials ?? []).map(material => material.value));
      const result = await api.uploadCaseMaterial(current.id, current.row_version, file);
      saved.current = result;
      const material = result.materials?.find(material => material.kind === 'FILE' && !known.has(material.value));
      if (!material) throw new Error('上传未返回文件路径，请重新读取场景资料。');
      return material;
    } finally { setBusy(false); }
  };
  const discardImport = async (material: CaseMaterial) => {
    const current = saved.current;
    if (!current) return;
    setBusy(true);
    try {
      saved.current = await api.updateCase(current.id, {
        expected_row_version: current.row_version,
        name: current.name, description: current.description ?? '', objective: current.objective,
        work_directory_id: current.work_directory_id ?? null,
        materials: (current.materials ?? []).filter(entry => entry.value !== material.value),
      });
    } finally { setBusy(false); }
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !name.trim() || !workspaceId) return;
    if (materials.length > 100) { setError('最多添加 100 项资料。'); return; }
    setBusy(true); setError(''); setNotice('');
    const basics: CaseBasics = { name: name.trim(), description, objective, materials, work_directory_id: directoryId || null };
    try {
      const current = saved.current && (!item || saved.current.row_version > item.row_version) ? saved.current : item;
      const result = current
        ? await api.updateCase(current.id, { ...basics, name: current.name, description: current.description ?? '', objective: current.objective, work_directory_id: current.work_directory_id ?? null, expected_row_version: current.row_version })
        : await api.createCase(basics);
      saved.current = result;
      setNotice('已保存'); onSaved(result);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };
  return <form id={formId} className="scenario-overview scenario-basics-form" onSubmit={event => void save(event)}>
    <fieldset disabled={busy || materialOpen}><div className="case-basics-fields">
      <label>名称<input autoFocus={!item} value={name} readOnly={Boolean(item || saved.current)} maxLength={220} required onChange={event => { setName(event.target.value); setNotice(''); }} placeholder="为这个 Case 起个名称"/></label>
      <div className="case-field"><span>工作区 <span className="scenario-optional">可选</span></span><CaseChoice label="工作区" value={directoryId} disabled={busy || Boolean(item || saved.current)} onChange={value => { setDirectoryId(value); setNotice(''); }} options={[
        { value: '', label: '默认工作区', detail: '自动创建 Case 专属工作区' },
        ...(directoryId && !directories.data?.items.some(directory => directory.id === directoryId) ? [{ value: directoryId, label: '当前工作区（暂不可用）' }] : []),
        ...(directories.data?.items.filter(directory => directory.id !== item?.control_work_directory_id).map(directory => ({ value: directory.id, label: directory.display_name })) ?? []),
      ]}/><small>{item || saved.current ? '基础信息创建后固定。' : '基础信息创建后不可修改。'}</small>{directories.error && <p className="error">{errorMessage(directories.error)}</p>}</div>
      <CompactTextField label="说明" readOnly={Boolean(item)} value={description} placeholder="介绍当前场景的背景。" onChange={value => { setDescription(value); setNotice(''); }}/>
      <CompactTextField label="目标" readOnly={Boolean(item)} value={objective} placeholder="说明需要达成的结果。" onChange={value => { setObjective(value); setNotice(''); }}/>
      </div>
      {item && <section className="scenario-materials" aria-label="资料"><header><span><b>资料</b></span><button ref={materialButton} className="secondary" type="button" disabled={materials.length >= 100} onClick={() => setMaterialOpen(true)}><Plus size={14}/>添加资料</button></header>
        {!materials.length && <p className="scenario-materials-empty">尚未添加资料</p>}
        <ul>{materials.map((material, index) => <li key={`${index}-${material.value}`}>{material.kind === 'LINK' ? <Link2 size={15}/> : <FileText size={15}/>}{material.kind === 'LINK' ? <a href={materialHref(material.value)} target="_blank" rel="noreferrer">{material.name}</a> : <button className="ghost case-material-preview-button" type="button" onClick={() => setPreview(material)}>{material.name}</button>}
          <span className={`case-material-kind ${material.kind.toLowerCase()}`}>{material.kind === 'LINK' ? '链接' : '文件'}</span><MaterialName name={material.name} file={material.kind === 'FILE'} onRename={name => { setMaterials(current => current.map((entry, i) => i === index ? { ...entry, name } : entry)); setNotice(''); }}/>
          <button className="ghost" type="button" aria-label={`移除资料 ${material.name}`} onClick={() => { setMaterials(current => current.filter((_, i) => i !== index)); setNotice(''); }}><X size={14}/></button></li>)}
          </ul>
      </section>}
      {showActions && <footer className="scenario-form-actions">{onCancel && <button className="secondary" type="button" onClick={onCancel}>取消</button>}<button className="primary" disabled={!name.trim() || !workspaceId}>{busy ? '保存中…' : '保存'}</button><span role="status">{notice}</span></footer>}
    </fieldset>
    {!showActions && <span className="case-form-status" role="status">{notice}</span>}
    {error && <p className="error" role="alert">{error}</p>}
    {materialOpen && <AddMaterialDialog onImport={importMaterial} onDiscard={discardImport} onApply={material => { setMaterials(current => [...current, material]); setNotice(''); }} onClose={() => { setMaterialOpen(false); window.requestAnimationFrame(() => materialButton.current?.focus()); }}/>}
    {preview && <MaterialPreview material={preview} caseId={saved.current?.id ?? item?.id ?? ''} onClose={() => setPreview(undefined)}/>}
  </form>;
}

function ConversationTab({ item, workspaceId, onUpdated }: { item: Case; workspaceId: string; onUpdated: (item: Case) => void }) {
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const [providerId, setProviderId] = useState(item.session_config?.model_provider_id ?? '');
  const [modelName, setModelName] = useState(item.session_config?.model_name ?? '');
  const [effort, setEffort] = useState(item.session_config?.reasoning_effort ?? '');
  const [capabilities, setCapabilities] = useState(item.session_config?.capability_version_ids ?? []);
  // Undefined means this Case has no credential preference yet; [] is an explicit opt-out.
  const [credentialSelection, setCredentialSelection] = useState<string[] | undefined>(item.session_config?.credential_ids);
  const credentialCatalog = useQuery({ queryKey: ['website-credentials'], queryFn: api.websiteCredentials, enabled: credentialSelection === undefined });
  const credentials = credentialSelection ?? credentialCatalog.data?.map(credential => credential.id) ?? [];
  const credentialsReady = credentialSelection !== undefined || credentialCatalog.isSuccess;
  const [configOpen, setConfigOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const availableProviders = (providers.data ?? []).filter(provider => provider.available_for_nodes);
  const provider = availableProviders.find(candidate => candidate.id === providerId);
  const models = provider?.models.filter(model => model.enabled) ?? [];
  const model = models.find(candidate => candidate.model_name === modelName);
  const save = useMutation({
    mutationFn: () => api.saveCaseSessionConfig(item.id, item.row_version, { model_provider_id: providerId, model_name: modelName, reasoning_effort: effort || null, capability_version_ids: capabilities, credential_ids: credentials }),
    onSuccess: result => { onUpdated(result); setNotice('已保存'); },
  });
  return <section className="scenario-config">
    <div className="scenario-config-grid case-session-grid"><div className="case-field"><span>模型供应商</span><CaseChoice label="模型供应商" value={providerId} disabled={save.isPending} onChange={value => { setProviderId(value); setModelName(''); setEffort(''); setNotice(''); }} options={availableProviders.map(candidate => ({ value: candidate.id, label: candidate.name }))}/></div>
      <div className="case-field"><span>模型名称</span><CaseChoice label="模型名称" value={modelName} disabled={!provider || save.isPending} placeholder={provider ? '选择模型' : '请先选择供应商'} onChange={value => { setModelName(value); setEffort(''); setNotice(''); }} options={models.map(candidate => ({ value: candidate.model_name, label: candidate.model_name }))}/></div>
      <div className="case-field"><span>思考程度</span><CaseChoice label="思考程度" value={effort} disabled={!model || save.isPending} onChange={value => { setEffort(value); setNotice(''); }} options={[{ value: '', label: '使用模型默认值' }, ...(model?.supported_reasoning_efforts?.map(value => ({ value, label: value })) ?? [])]}/></div>
      <div className="case-field"><span>能力配置</span><button className="secondary case-capability-choice" type="button" aria-label="配置会话" disabled={!workspaceId || save.isPending || !credentialsReady} onClick={() => setConfigOpen(true)}><Settings2 size={15}/><span>配置会话</span><small>{credentialsReady ? `已选 ${capabilities.length + credentials.length} 项` : '正在读取认证…'}</small><ChevronRight size={15}/></button></div></div>
    {providers.error && <p className="error">{errorMessage(providers.error)}</p>}{save.error && <p className="error" role="alert">{errorMessage(save.error)}</p>}
    {credentialSelection === undefined && credentialCatalog.error && <p className="error" role="alert">{errorMessage(credentialCatalog.error)}<button type="button" className="ghost" onClick={() => void credentialCatalog.refetch()}>重新读取认证</button></p>}
    <footer className="case-config-heading"><span className="case-form-status" role="status">{notice}</span><button className="primary" disabled={!model || save.isPending || !credentialsReady} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存配置'}</button></footer>
    {configOpen && <CapabilityManager configurationPreset workspaceId={workspaceId} draftCapabilityIds={capabilities} draftCredentialIds={credentials} onClose={() => setConfigOpen(false)} onCreateEnhancedConversation={ids => { setCapabilities(ids); setNotice(''); }} onDraftCredentialIdsChange={ids => { setCredentialSelection(ids); setNotice(''); }}/>}
  </section>;
}

function PathTab({ item, artifactCounts, onUpdated, onOpenRecord }: { item: Case; artifactCounts: Record<string, number>; onUpdated: (item: Case) => void; onOpenRecord: (stage: CaseStage) => void }) {
  const [adding, setAdding] = useState(false);
  const [parentId, setParentId] = useState('');
  const [mode, setMode] = useState('');
  const [recordId, setRecordId] = useState('');
  const defaultStage = item.stages.find(stage => stage.state === 'ACTIVE') ?? item.stages.find(stage => stage.current_execution_flow_run_id) ?? item.stages[0];
  const [selectedStageId, setSelectedStageId] = useState(defaultStage?.id ?? '');
  const selectedStage = item.stages.find(stage => stage.id === selectedStageId) ?? defaultStage;
  const [draggingId, setDraggingId] = useState('');
  const [dropTarget, setDropTarget] = useState<{ id: string; after: boolean }>();
  const [status, setStatus] = useState('');
  const templates = useQuery({ queryKey: ['case-stage-templates', item.id], queryFn: () => api.caseStageTemplates(item.id), enabled: adding });
  const runs = useQuery({ queryKey: ['case-running-flow-runs'], queryFn: api.runs, enabled: adding, staleTime: 0, refetchOnMount: 'always', refetchInterval: adding ? 3000 : false });
  const runningRuns = (runs.data ?? []).filter(run => run.state === 'ACTIVE' && run.runtime_status === 'ACTIVE' && run.runtime_write_available);
  const selectedParent = runningRuns.find(run => run.id === parentId);
  // The API returns top-level FlowRuns; eligible records carry their formal owner ID.
  const ownedTemplates = (templates.data ?? []).filter(record => selectedParent && (record.parent_flow_run_id || record.id) === selectedParent.id);
  const records = ownedTemplates.filter(record => record.execution_mode === mode);
  const selectedRecord = records.find(record => record.id === recordId);
  const update = useMutation({
    mutationFn: (cards: Array<{ id: string } | { flow_run_id: string }>) => api.saveCasePath(item.id, item.row_version, cards),
    onSuccess: (result, cards) => {
      onUpdated(result); setStatus('路径已保存');
      if (cards.some(card => 'flow_run_id' in card)) { setAdding(false); setParentId(''); setMode(''); setRecordId(''); }
      if (adding) void templates.refetch();
    },
  });
  const rerun = useMutation({
    mutationFn: (stage: CaseStage) => api.rerunCaseStage(item.id, stage.id),
    onSuccess: result => { onUpdated(result); setStatus('已重置阶段。点击工具栏“再次启动”，由 Agent 重新准备并启动。'); },
  });
  const reorder = (source: string, target: string, after: boolean) => {
    const from = item.stages.findIndex(stage => stage.id === source);
    const to = item.stages.findIndex(stage => stage.id === target);
    if (from < 0 || to < 0 || from === to || update.isPending) return;
    const cards = item.stages.map(stage => ({ id: stage.id }));
    const [card] = cards.splice(from, 1);
    cards.splice(to - (from < to ? 1 : 0) + (after ? 1 : 0), 0, card);
    if (cards.every((card, index) => card.id === item.stages[index].id)) return;
    setStatus('正在保存路径顺序…'); update.mutate(cards);
  };
  const pathLocked = item.state === 'COMPLETED' || item.state === 'ARCHIVED';
  useEscapeClose(() => setAdding(false), adding && !update.isPending);
  const endDrag = () => { setDraggingId(''); setDropTarget(undefined); };
  return <div className={`case-path-workspace${selectedStage ? ' has-stage-info' : ''}`}>
    <section className={`scenario-path scenario-path-editor case-connected-path${!item.stages.length ? ' is-empty' : ''}`} aria-label="Case 路径卡片">
    <ol>{item.stages.map((stage, index) => <li key={stage.id} data-stage-id={stage.id} className={`${stage.state.toLowerCase()}${selectedStage?.id === stage.id ? ' is-selected' : ''}${draggingId === stage.id ? ' is-dragging' : ''}${dropTarget?.id === stage.id ? dropTarget.after ? ' drop-after' : ' drop-before' : ''}`}
      onDragOver={event => {
        if (!draggingId || update.isPending || pathLocked) return;
        event.preventDefault(); event.dataTransfer.dropEffect = 'move';
        const bounds = event.currentTarget.getBoundingClientRect();
        setDropTarget({ id: stage.id, after: event.clientY > bounds.top + bounds.height / 2 });
      }}
      onDrop={event => {
        event.preventDefault();
        if (draggingId && event.dataTransfer.getData('application/x-flowweave-case-stage') === draggingId) {
          const bounds = event.currentTarget.getBoundingClientRect();
          reorder(draggingId, stage.id, event.clientY > bounds.top + bounds.height / 2);
        }
        endDrag();
      }}>
      <div className="scenario-path-rail"><span>{index + 1}</span><i><ArrowDown size={12}/></i></div>
      <article draggable={!pathLocked && !update.isPending && !adding} aria-label={`路径卡片 ${stage.name}`} aria-current={selectedStage?.id === stage.id ? 'true' : undefined} tabIndex={0}
        onClick={() => setSelectedStageId(stage.id)} onFocus={() => setSelectedStageId(stage.id)}
        onKeyDown={event => { if ((event.key === 'Enter' || event.key === ' ') && event.target === event.currentTarget) { event.preventDefault(); setSelectedStageId(stage.id); } }}
        onDragStart={event => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('application/x-flowweave-case-stage', stage.id); setDraggingId(stage.id); setStatus(''); }} onDragEnd={endDrag}>
        <header><div className="case-card-title"><span className={`case-stage-state ${stage.current_execution_flow_run_id ? 'executing' : stage.state.toLowerCase()}`}>{stageExecutionLabel(stage)}</span><h3>{stage.name}</h3></div>
          <div className="scenario-card-actions"><button className="ghost case-drag-handle" aria-label={`调整顺序 ${stage.name}`} title="拖拽调整顺序，也可聚焦后按上下方向键" disabled={pathLocked || update.isPending || adding} onKeyDown={event => {
            if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
            event.preventDefault(); const to = index + (event.key === 'ArrowDown' ? 1 : -1);
            if (item.stages[to]) reorder(stage.id, item.stages[to].id, to > index);
          }}><GripVertical size={17}/></button><button className="ghost" aria-label={`删除卡片 ${stage.name}`} disabled={pathLocked || Boolean(stage.current_execution_flow_run_id) || update.isPending} onClick={() => { setStatus(''); update.mutate(item.stages.filter(candidate => candidate.id !== stage.id).map(candidate => ({ id: candidate.id }))); }}><Trash2 size={15}/></button></div>
        </header>
        <StageFlowGraph stage={stage}/>


      </article>
    </li>)}</ol>
    <div className="case-path-tail"><div className="scenario-path-rail"><span><Plus size={14}/></span></div>
      {adding ? <form className="scenario-add-card case-path-picker" onSubmit={event => { event.preventDefault(); if (selectedRecord && !update.isPending) { setStatus(''); update.mutate([...item.stages.map(stage => ({ id: stage.id })), { flow_run_id: selectedRecord.id }]); } }}>
        <div className="case-path-selection"><div className="case-field"><span>FlowRun</span><CaseChoice label="FlowRun" value={parentId} disabled={update.isPending || runs.isLoading} emptyText="暂无正在运行的 FlowRun" onChange={value => { setParentId(value); setMode(''); setRecordId(''); }} options={runningRuns.map(run => ({ value: run.id, label: run.name, detail: `Run #${run.run_no}` }))}/></div>
          <div className="case-field"><span>运行方式</span><CaseChoice label="运行方式" value={selectedParent ? mode : ''} disabled={!selectedParent || update.isPending || templates.isLoading} placeholder={selectedParent ? '选择运行方式' : '请先选择 FlowRun'} onChange={value => { setMode(value); setRecordId(''); }} options={[{ value: 'STEPWISE', label: '逐步运行' }, { value: 'CONTINUOUS', label: '连续运行' }].map(option => ({ ...option, detail: `${ownedTemplates.filter(record => record.execution_mode === option.value).length} 个可用记录` }))}/></div>
          <div className="case-field"><span>运行记录</span><CaseChoice label="运行记录" value={selectedParent ? recordId : ''} disabled={!selectedParent || !mode || update.isPending} placeholder={selectedParent && mode ? '选择运行记录' : '请先选择运行方式'} emptyText="暂无完整配置的可用记录" onChange={setRecordId} options={records.map(record => ({ value: record.id, label: record.name, detail: `${record.reachable_node_keys.length} 个节点 · ${record.id}` }))}/></div><footer><button type="button" className="ghost" aria-label="取消添加卡片" disabled={update.isPending} onClick={() => setAdding(false)}>取消</button><button className="primary" disabled={!selectedRecord || update.isPending}>{update.isPending ? '添加中…' : '添加'}</button></footer></div>
        {parentId && !selectedParent && !runs.isFetching && !runs.error && <p>所选 FlowRun 已停止运行，请重新选择。</p>}
        {(templates.isLoading || runs.isLoading) && <p>正在加载运行记录…</p>}
        {(templates.error || runs.error) && <p className="error" role="alert">{errorMessage(templates.error || runs.error)}</p>}
        {!templates.isLoading && !templates.error && selectedParent && mode && !records.length && <p>当前 FlowRun 下暂无可添加的{mode === 'STEPWISE' ? '逐步' : '连续'}运行记录，请先完成记录配置。</p>}
      </form> : <button className="scenario-add-path-card" disabled={pathLocked || update.isPending || item.stages.length >= 50} onClick={() => { setAdding(true); update.reset(); setStatus(''); }}><span className="case-add-icon"><Plus size={20}/></span><span><b>{item.stages.length ? '添加下一流程' : '添加首个流程'}</b></span></button>}
    </div>
    <p className="case-path-status" role="status">{update.error ? '' : status}</p>
    {(update.error || rerun.error) && <p className="error" role="alert">{errorMessage(update.error || rerun.error)}</p>}
    </section>
    {selectedStage && <aside className="case-stage-info" aria-label="流程信息">
      <header><span className="case-stage-info-icon"><GitBranch size={17}/></span><span><small>流程信息</small><h2 title={selectedStage.name}>{selectedStage.name}</h2></span></header>
      <div className="case-stage-info-badges"><span className={`case-stage-state ${selectedStage.current_execution_flow_run_id ? 'executing' : selectedStage.state.toLowerCase()}`}>{stageExecutionLabel(selectedStage)}</span><span>{selectedStage.execution_mode === 'STEPWISE' ? '逐步运行' : '连续运行'}</span></div>
      {selectedStage.objective && <p className="case-stage-info-objective">{selectedStage.objective}</p>}
      <dl>
        <div><dt>阶段位置</dt><dd>{item.stages.findIndex(stage => stage.id === selectedStage.id) + 1} / {item.stages.length}</dd></div>
        <div><dt>执行方式</dt><dd>{selectedStage.execution_mode === 'STEPWISE' ? '逐步运行' : '连续运行'}</dd></div>
        <div><dt>当前节点</dt><dd>{selectedStage.execution?.active_node?.node_name ?? (selectedStage.current_execution_flow_run_id ? '暂无活动节点' : '尚未启动')}</dd></div>
        <div><dt>执行进度</dt><dd>{selectedStage.execution?.progress ? `${selectedStage.execution.progress.accepted} 已接受 · ${selectedStage.execution.progress.active} 活动` : '暂无执行数据'}</dd></div>
        {selectedStage.execution?.automatic_block && <div><dt>待补全</dt><dd>{selectedStage.execution.automatic_block.gates.length} 项门禁配置</dd></div>}
        <div><dt>正式产物</dt><dd>{artifactCounts[selectedStage.stage_key] ?? 0} 项</dd></div>
      </dl>
      <div className="case-stage-info-actions">
        {selectedStage.current_execution_flow_run_id ? <button className="secondary case-record-link" onClick={() => onOpenRecord(selectedStage)}><ListChecks size={14}/>查看流程记录<ExternalLink size={12}/></button> : <p><Info size={13}/>启动后可从这里进入对应的流程记录。</p>}
        {selectedStage.current_execution_flow_run_id && item.state !== 'ARCHIVED' && ['COMPLETED', 'FAILED', 'CANCELLED'].includes(selectedStage.execution?.state ?? '') && !item.stages.some(other => other.position > selectedStage.position && other.current_execution_flow_run_id) && <button className="ghost case-rerun" disabled={rerun.isPending} onClick={() => { setStatus(''); rerun.mutate(selectedStage); }}><RotateCcw size={13}/>{rerun.isPending ? '准备中…' : '重新运行此流程'}</button>}
      </div>
    </aside>}
  </div>;
}

function CaseActivity({ item, onOpenRun, onRetry, onOpenConversation }: { item: Case; onOpenRun: (id: string) => void; onRetry?: (id: string) => void; onOpenConversation?: (planId?: string) => void }) {
  const labels: Record<Case['control_plans'][number]['action'], string> = { CREATE_EXECUTION_COPY: '创建执行副本', CONFIGURE_EXECUTION: '修改执行配置', START_EXECUTION: '启动流程', START_STEPWISE_NODE: '启动逐步节点', ACCEPT_STEPWISE_ATTEMPT: '接受节点结果', REJECT_STEPWISE_ATTEMPT: '拒绝节点结果', CANCEL_STEPWISE_ATTEMPT: '取消投递失败的节点尝试', STOP_EXECUTION: '停止执行', RERUN_STEPWISE_NODE: '重跑节点', COPY_CONTINUOUS_EXECUTION: '复制连续运行', ADVANCE_CASE_STAGE: '进入下一阶段', COMPLETE_CASE: '完成场景', CONCLUDE_CASE: '提前结束场景', ASK_HUMAN: '向人工提问' };
  const events = [
    ...item.control_plans.map(plan => ({ id: `plan-${plan.id}`, planId: plan.id, kind: '控制动作', action: labels[plan.action], state: plan.state, reason: plan.reason, error: plan.error_code, runId: plan.execution_flow_run_id, created: plan.created_at })),
    ...item.decision_proposals.map(proposal => ({ id: `decision-${proposal.id}`, planId: undefined, kind: '执行决策', action: ({ START_STAGE_EXECUTION: '启动阶段', CONTINUE_STEPWISE: '继续逐步运行', STOP_FLOW_RUN: '停止流程', RERUN_STEPWISE_NODE: '重跑节点', COPY_AUTOMATIC_RUN: '复制连续运行' })[proposal.action], state: proposal.state, reason: proposal.reason, error: '', runId: proposal.flow_run_id, created: proposal.created_at })),
    ...item.tasks.map(task => ({ id: `task-${task.id}`, planId: undefined, kind: '观察任务', action: ({ OBSERVE: '观察结果', PROPOSE_REVISION: '建议修订', REVIEW: '复核' })[task.task_type], state: task.state, reason: '', error: '', runId: task.flow_run_id, created: task.created_at })),
  ].sort((left, right) => right.created.localeCompare(left.created));
  const states: Record<string, string> = { PENDING: '待处理', VALIDATED: '已校验', EXECUTING: '执行中', RUNNING: '执行中', SUCCEEDED: '成功', FAILED: '失败', CANCELLED: '已取消', PROPOSED: '已提出', EXECUTED: '已执行', SUPERSEDED: '已替代' };
  return <section className="case-activity-list">{events.length ? events.map(event => <article key={event.id}><b>{event.action}</b><span className={`case-activity-state ${event.state.toLowerCase()}`}>{states[event.state] ?? event.state}</span><small>{new Date(event.created).toLocaleString()}</small>{event.error && <em className="error" title={event.error}>{event.error}</em>}<footer>{onOpenConversation && event.reason && <button className="ghost" title="在 Agent 会话中查看理由" aria-label="在会话中查看" onClick={() => onOpenConversation(event.planId)}><Bot size={13}/>会话</button>}{event.runId && <button className="ghost" title="查看执行记录" aria-label="查看执行记录" onClick={() => onOpenRun(event.runId!)}><ExternalLink size={13}/>记录</button>}{onRetry && event.planId && event.state === 'FAILED' && <button className="secondary" onClick={() => onRetry(event.planId!)}>重试</button>}</footer></article>) : <div className="empty"><Clock3 size={24}/><span>暂无 Agent 动作</span></div>}</section>;
}

function ScenarioWorkspace({ item, workspaceId, onUpdated, onDelete, onOpenConversation, onOpenRun, onOpenRecord }: { item: Case; workspaceId: string; onUpdated: (item: Case) => void; onDelete: () => void; onOpenConversation: (id: string, planId?: string) => void; onOpenRun: (id: string) => void; onOpenRecord: (stage: CaseStage) => void }) {
  const dialog = useProductDialog();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const [panel, setPanel] = useState<'basics' | 'agent' | 'takeover' | 'actions'>();
  useEscapeClose(() => setPanel(undefined), Boolean(panel));
  const currentRound = item.control_rounds?.find(round => round.id === item.current_control_round_id);
  const [openingRound, setOpeningRound] = useState<{ id: string; rowVersion: number }>();
  const observing = currentRound?.state === 'PENDING' || item.state !== 'COMPLETED' && item.state !== 'ARCHIVED' && Boolean(item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id));
  const live = useQuery({ queryKey: ['case-live', item.id], queryFn: () => api.case(item.id), enabled: observing, refetchInterval: observing ? 3000 : false, retry: 1 });
  const materials = useQuery({ queryKey: ['case-artifact-materials', item.id], queryFn: () => api.caseArtifactMaterials(item.id), enabled: Boolean(item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id)) });
  const artifactCounts = useMemo(() => (materials.data?.items ?? []).reduce<Record<string, number>>((counts, material) => { if (material.state === 'ACCEPTED') counts[material.stage_key] = (counts[material.stage_key] ?? 0) + 1; return counts; }, {}), [materials.data]);
  // Only unresolved problems of a running Case count as exceptions. A failed action that a later retry
  // already completed, and any leftovers once the Case is finished, stay in the operation log only.
  const caseFinished = item.state === 'COMPLETED' || item.state === 'ARCHIVED';
  const failedDeliveries = caseFinished ? [] : item.feedback_deliveries?.filter(delivery => delivery.state === 'FAILED' || delivery.state === 'AMBIGUOUS') ?? [];
  const failedPlans = caseFinished ? [] : item.control_plans.filter(plan => plan.state === 'FAILED' && !item.control_plans.some(other => other.retry_of_plan_id === plan.id && other.state === 'SUCCEEDED'));
  const takeoverPlans = item.control_plans.filter(plan => plan.state === 'FAILED' || Boolean(plan.retry_of_plan_id));
  const coordination = useMutation({
    mutationFn: (state: 'ACTIVE' | 'PAUSED') => api.setCaseCoordinationState(item.id, item.row_version, state),
    onSuccess: onUpdated,
  });
  const [rejectNote, setRejectNote] = useState('');
  const concludeDecision = useMutation({
    mutationFn: (decision: 'CONFIRM' | 'REJECT') => decision === 'CONFIRM' ? api.confirmCaseConclusion(item.id, item.row_version) : api.rejectCaseConclusion(item.id, item.row_version, rejectNote),
    onSuccess: result => { setRejectNote(''); onUpdated(result); },
  });
  const retryPlan = useMutation({
    mutationFn: (planId: string) => api.retryCaseControlPlan(item.id, planId),
    onSuccess: () => void api.case(item.id).then(onUpdated),
  });

  useEffect(() => { if (live.data) onUpdated(live.data); }, [live.data, onUpdated]);
  const startKey = useRef<string | undefined>(undefined);
  const [startMenuOpen, setStartMenuOpen] = useState(false);
  const startSplit = useRef<HTMLDivElement>(null);
  useEscapeClose(() => setStartMenuOpen(false), startMenuOpen);
  useEffect(() => {
    if (!startMenuOpen) return;
    const outside = (event: PointerEvent) => { if (!startSplit.current?.contains(event.target as Node)) setStartMenuOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [startMenuOpen]);
  const start = useMutation({
    mutationFn: (session: 'CURRENT' | 'NEW') => {
      if (session === 'CURRENT' && currentRound?.state === 'FAILED') return api.retryCaseControlRound(item.id, currentRound.id);
      startKey.current ??= crypto.randomUUID();
      return api.startCase(item.id, { session, expected_row_version: item.row_version }, startKey.current);
    },
    onSuccess: result => {
      startKey.current = undefined;
      setStartMenuOpen(false);
      onUpdated(result);
      const round = result.control_rounds?.find(entry => entry.id === result.current_control_round_id);
      if (round?.state === 'PENDING') setOpeningRound({ id: round.id, rowVersion: result.row_version });
    },
    onError: () => { startKey.current = undefined; },
  });
  useEffect(() => {
    if (!openingRound || item.row_version < openingRound.rowVersion) return;
    const round = item.control_rounds?.find(entry => entry.id === openingRound.id);
    if (round?.state === 'STARTED' && round.binding_id) {
      setOpeningRound(undefined); onOpenConversation(round.binding_id);
    } else if (round?.state === 'FAILED') setOpeningRound(undefined);
  }, [item.control_rounds, item.row_version, openingRound, onOpenConversation]);
  const deleteCase = async () => {
    if (!await dialog.confirm({ title: `删除 Case“${item.name}”？`, message: '会永久删除 Case、其阶段控制元数据、协调会话及专属工作目录，无法恢复。关联 FlowRun 历史会保留。进行中的执行、运行中会话或未完成受控计划会阻止删除。', confirmLabel: '删除 Case', tone: 'danger' })) return;
    setDeleting(true); setError('');
    try { await api.deleteCase(item.id); onDelete(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setDeleting(false); }
  };
  const terminal = item.state === 'COMPLETED' || item.state === 'ARCHIVED';
  const missing = [
    ...(!item.stages.length || item.stages.some(stage => !stage.flow_run_id) ? ['添加完整的流程路径'] : []),
    ...(!item.session_config?.model_provider_id || !item.session_config.model_name ? ['保存 Agent 配置'] : []),
  ];
  const pendingRound = currentRound?.state === 'PENDING';
  const startBlock = pendingRound ? '协调会话正在创建' : terminal ? '场景已结束' : item.state === 'PAUSED' ? '场景已暂停' : !workspaceId ? '正在读取工作区' : missing.length ? `请先${missing.join('、')}` : '';
  const hasRun = item.stages.some(stage => stage.current_execution_flow_run_id);
  const active = item.stages.find(stage => stage.state === 'ACTIVE' || stage.state === 'REVIEW_PENDING');
  const completed = item.stages.filter(stage => stage.state === 'COMPLETED').length;
  const stateLabel = item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id) ? CASE_STATE[item.state] : terminal ? CASE_STATE[item.state] : '待启动';
  const agentStatus = item.state === 'PAUSED' ? '协调已暂停，后台不会执行新的自主动作' : active?.state === 'REVIEW_PENDING' ? '结果已就绪，等待 Agent 判断' : active?.execution?.active_node ? `Agent 正在跟进：${active.execution.active_node.node_name}` : failedPlans.length || failedDeliveries.length ? `有 ${failedPlans.length + failedDeliveries.length} 项 Agent 动作未成功，需要重试或人工处理` : item.control_conversation_binding_id ? 'Agent 正在监控当前场景' : '启动 Agent 后将持续协调此路径';
  const panelTitle = panel === 'basics' ? '场景信息' : panel === 'agent' ? 'Agent 配置' : panel === 'actions' ? 'Agent 动作' : '未解决的问题';
  // The Agent parks the Case with ASK_HUMAN; surface its question until the Case is resumed.
  const latestPlan = [...item.control_plans].sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const pendingQuestion = item.state === 'PAUSED' && item.pause_reason === 'WAITING_HUMAN' && latestPlan?.action === 'ASK_HUMAN' && latestPlan.state === 'SUCCEEDED' ? latestPlan : undefined;
  const conclusionProposed = item.state === 'PAUSED' && item.pause_reason === 'CONCLUSION_PROPOSED';
  const conclusionProposal = item.state === 'PAUSED' && item.pause_reason === 'CONCLUSION_PROPOSED' ? (item.pause_note || '') : undefined;
  return <main className="scenario-workspace case-single-workspace"><div className="scenario-workspace-inner">
    <header className="case-workspace-toolbar"><div className="case-workspace-title"><h1 title={item.name}>{item.name}</h1><span className={`case-workspace-state ${item.state.toLowerCase()}`} title={agentStatus}>{stateLabel}</span><small>{completed}/{item.stages.length} 阶段</small>{Boolean(failedPlans.length || failedDeliveries.length) && <button type="button" className="case-issue-chip" title="有 Agent 动作未成功，点击查看并重试" onClick={() => setPanel('takeover')}><AlertTriangle size={12}/>{failedPlans.length + failedDeliveries.length} 项待处理</button>}</div><div className="scenario-case-actions"><button className="ghost" aria-label="场景信息" title="查看并编辑场景名称、目标和资料" aria-pressed={panel === 'basics'} onClick={() => setPanel('basics')}><Info size={14}/>场景信息</button><button className="ghost" aria-label="Agent 动作" title="查看协调 Agent 做过的全部动作及理由" aria-pressed={panel === 'actions'} onClick={() => setPanel('actions')}><ClipboardList size={14}/>Agent 动作{item.control_plans.length > 0 && <span className="case-action-count">{item.control_plans.length}</span>}</button><button className="ghost" aria-label="Agent 配置" title="协调 Agent 使用的模型与能力" aria-pressed={panel === 'agent'} onClick={() => setPanel('agent')}><Settings2 size={14}/>Agent 配置</button>{item.control_conversation_binding_id && !terminal && <button className="ghost" aria-label={item.state === 'PAUSED' ? '继续协调' : '暂停协调'} title={conclusionProposal !== undefined ? '请先在上方处理 Agent 的结束提议' : item.state === 'PAUSED' ? 'Agent 恢复自主动作' : 'Agent 不再执行新的自主动作，已启动的流程不受影响'} disabled={coordination.isPending || conclusionProposed} onClick={() => coordination.mutate(item.state === 'PAUSED' ? 'ACTIVE' : 'PAUSED')}>{item.state === 'PAUSED' ? <CirclePlay size={14}/> : <CirclePause size={14}/>}{item.state === 'PAUSED' ? '继续' : '暂停'}</button>}<div ref={startSplit} className="case-start-split"><button className="primary case-start-main" aria-label={hasRun ? '再次启动' : '启动'} title={startBlock || (hasRun ? '默认在当前 Agent 会话里继续；点右侧箭头可新建会话' : '向协调 Agent 发送启动事件，由 Agent 判断并启动流程')} disabled={start.isPending || Boolean(startBlock)} onClick={() => { setError(''); start.mutate('CURRENT'); }}>{start.isPending || pendingRound ? <LoaderCircle size={14} className="spin"/> : hasRun ? <RotateCcw size={14}/> : <Play size={14}/>}{start.isPending || pendingRound ? '提交中' : hasRun ? '再次启动' : '启动'}</button>{item.control_conversation_binding_id && <button className="primary case-start-more" aria-label="选择启动方式" title="选择启动方式" aria-haspopup="menu" aria-expanded={startMenuOpen} disabled={start.isPending || Boolean(startBlock)} onClick={() => setStartMenuOpen(open => !open)}><ChevronDown size={14}/></button>}{startMenuOpen && <div className="case-start-menu" role="menu"><button role="menuitem" onClick={() => start.mutate('CURRENT')}>基于当前会话<small>默认 · 在原对话里继续</small></button><button role="menuitem" onClick={() => start.mutate('NEW')}>新建会话<small>开始一段新对话，历史保留</small></button></div>}</div><button className="ghost case-delete-action" type="button" aria-label="删除场景" title="删除场景" disabled={deleting} onClick={() => void deleteCase()}><Trash2 size={14}/>删除</button></div></header>
    {(error || start.error || live.error || coordination.error || concludeDecision.error || retryPlan.error || materials.error) && <p className="error scenario-delete-error" role="alert">{error || errorMessage(start.error || live.error || coordination.error || concludeDecision.error || retryPlan.error || materials.error)}</p>}

    {conclusionProposal !== undefined && <section className="case-question case-conclusion" role="status" aria-label="Agent 提议结束场景"><div><b>Agent 提议结束场景</b><p>{conclusionProposal}</p><input className="case-conclusion-note" aria-label="继续的原因（可选）" placeholder="选择“继续”时可告诉 Agent 原因（可选）" maxLength={2000} value={rejectNote} onChange={event => setRejectNote(event.target.value)}/></div><div className="case-question-actions">{item.control_conversation_binding_id && <button className="ghost" onClick={() => onOpenConversation(item.control_conversation_binding_id!)}><Bot size={13}/>查看会话</button>}<button className="ghost" disabled={concludeDecision.isPending} onClick={() => concludeDecision.mutate('REJECT')}><CirclePlay size={13}/>继续</button><button className="primary danger" disabled={concludeDecision.isPending} onClick={async () => { if (await dialog.confirm({ title: '确认结束场景？', message: '会停止仍在运行的执行并跳过后续阶段，场景将变为已完成且无法恢复。', confirmLabel: '确认结束', tone: 'danger' })) concludeDecision.mutate('CONFIRM'); }}>确认结束</button></div></section>}
    {pendingQuestion && <section className="case-question" role="status" aria-label="Agent 正在等待你的回答"><div><b>Agent 在等你回答</b><p>{pendingQuestion.reason}</p></div><div className="case-question-actions">{item.control_conversation_binding_id && <button className="ghost" onClick={() => onOpenConversation(item.control_conversation_binding_id!)}><Bot size={13}/>去会话回答</button>}<button className="primary" disabled={coordination.isPending} onClick={() => coordination.mutate('ACTIVE')}><CirclePlay size={13}/>已回答，继续</button></div></section>}
    {currentRound?.state === 'FAILED' && <p className="error scenario-delete-error" role="alert">协调会话创建失败：{currentRound.error_code}。重试会复用本轮配置和会话身份。</p>}
    
    
    <div className="case-single-layout"><section className="case-path-panel"><PathTab item={item} artifactCounts={artifactCounts} onUpdated={onUpdated} onOpenRecord={onOpenRecord}/></section></div>
    {panel && createPortal(<div className={`modal-backdrop case-inspector-backdrop`} onMouseDown={event => { if (event.target === event.currentTarget) setPanel(undefined); }}><section className={`modal case-inspector scenario-workspace case-inspector-${panel}`} role="dialog" aria-modal="true" aria-label={panelTitle} onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href]')];
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}><header className="case-inspector-heading"><h2>{panelTitle}</h2><button className="ghost" autoFocus aria-label="关闭场景面板" onClick={() => setPanel(undefined)}><X size={17}/></button></header><div className="case-inspector-body" key={panel}>{panel === 'basics' ? <><BasicsForm item={item} workspaceId={workspaceId} onSaved={onUpdated}/></> : panel === 'agent' ? <ConversationTab item={item} workspaceId={workspaceId} onUpdated={onUpdated}/> : panel === 'actions' ? <section className="case-takeover"><p>Agent 的每个决策都会记录在这里，只列出动作与结果；Agent 给出的理由和细节请在 Agent 会话中查看。</p><CaseActivity item={{ ...item, decision_proposals: [], tasks: [] }} onOpenRun={id => { setPanel(undefined); onOpenRun(id); }} onRetry={planId => retryPlan.mutate(planId)} onOpenConversation={item.control_conversation_binding_id ? planId => { setPanel(undefined); onOpenConversation(item.control_conversation_binding_id!, planId); } : undefined}/></section> : panel === 'takeover' ? <section className="case-takeover"><p>这些动作没有成功，也没有被后续重试补上。可以重试，或打开 Agent 会话人工判断。</p>{failedDeliveries.map(delivery => <article key={delivery.id}><b>Agent 回访异常</b><small>{delivery.error_code ?? delivery.state}</small></article>)}<CaseActivity item={{ ...item, decision_proposals: [], tasks: [], control_plans: takeoverPlans }} onOpenRun={onOpenRun} onRetry={planId => retryPlan.mutate(planId)}/>
{item.control_conversation_binding_id && <button className="primary" onClick={() => onOpenConversation(item.control_conversation_binding_id!)}><Bot size={14}/>打开 Agent 会话</button>}</section> : null}</div></section></div>, document.body)}
  </div></main>;
}

export function CasesPage({ onNavigate, sessionRoute }: { onNavigate: (path: string, replace?: boolean) => void; sessionRoute?: { caseId: string; bindingId: string } }) {
  const qc = useQueryClient();
  const cases = useQuery({ queryKey: ['cases'], queryFn: api.cases });
  const workspace = useQuery({ queryKey: ['default-agent-workspace'], queryFn: () => api.defaultAgentWorkspace() });
  const [selectedId, setSelectedId] = useState<string>();
  const [sessionConfigOpen, setSessionConfigOpen] = useState(false);
  const sessionWorkspaceId = workspace.data?.id;
  const sessionBindingId = sessionRoute?.bindingId;
  const sessionConversation = useQuery({
    queryKey: ['case-session-conversation', sessionWorkspaceId, sessionBindingId],
    queryFn: () => api.agentConversation(sessionWorkspaceId!, sessionBindingId!),
    enabled: Boolean(sessionWorkspaceId && sessionBindingId),
  });
  const [creating, setCreating] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const toggleExpanded = useCallback((id: string) => setExpandedIds(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }), []);
  const [query, setQuery] = useState('');
  const summary = useMemo(() => cases.data?.find(item => item.id === (sessionRoute?.caseId ?? selectedId)) ?? cases.data?.[0], [cases.data, selectedId, sessionRoute?.caseId]);
  // The list endpoint returns lightweight summaries; the open case loads its full history separately.
  const detail = useQuery({ queryKey: ['case-detail', summary?.id], queryFn: () => api.case(summary!.id), enabled: Boolean(summary), staleTime: 5000 });
  const selected = useMemo(() => summary && detail.data?.id === summary.id && detail.data.row_version >= summary.row_version ? detail.data : summary, [summary, detail.data]);
  const replace = useCallback((item: Case) => { qc.setQueryData<Case>(['case-detail', item.id], current => current && current.row_version > item.row_version ? current : item); qc.setQueryData<Case[]>(['cases'], current => current?.some(candidate => candidate.id === item.id) ? current.map(candidate => candidate.id === item.id && candidate.row_version <= item.row_version ? item : candidate) : [item, ...(current ?? [])]); }, [qc]);
  const routedCaseId = sessionRoute?.caseId;
  useEffect(() => { if (routedCaseId) setExpandedIds(current => current.has(routedCaseId) ? current : new Set(current).add(routedCaseId)); }, [routedCaseId]);
  const remove = (id: string) => { qc.setQueryData<Case[]>(['cases'], current => current?.filter(candidate => candidate.id !== id) ?? []); qc.removeQueries({ queryKey: ['case-live', id] }); setSelectedId(current => current === id ? undefined : current); };
  const importInput = useRef<HTMLInputElement>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(() => new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchNotice, setBatchNotice] = useState('');
  const batchDialog = useProductDialog();
  const visibleCases = useMemo(() => (cases.data ?? []).filter(item => !query.trim() || `${item.name} ${item.objective} ${item.description ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [cases.data, query]);
  const checkedCases = visibleCases.filter(item => checkedIds.has(item.id));
  const toggleChecked = useCallback((id: string) => setCheckedIds(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }), []);
  const exitBatch = useCallback(() => { setCheckedIds(new Set()); setBatchNotice(''); }, []);
  const runBatch = async (label: string, work: (item: Case) => Promise<void>) => {
    setBatchBusy(true); setBatchNotice('');
    const failures: string[] = [];
    for (const item of checkedCases) { try { await work(item); } catch (error) { failures.push(`${item.name}：${error instanceof Error ? error.message : String(error)}`); } }
    setBatchBusy(false);
    await qc.invalidateQueries({ queryKey: ['cases'] });
    if (failures.length) setBatchNotice(`${label}：${checkedCases.length - failures.length} 成功，${failures.length} 失败 — ${failures.join('；')}`);
    else setCheckedIds(new Set());
  };
  const batchImport = async (file: File) => {
    setBatchBusy(true); setBatchNotice('');
    try {
      const parsed = JSON.parse(await file.text());
      const list: Array<Record<string, any>> = Array.isArray(parsed) ? parsed : [parsed];
      if (!list.length || list.some(entry => !entry || typeof entry.name !== 'string' || typeof entry.objective !== 'string' || !Array.isArray(entry.stages))) throw new Error('文件格式不正确，需要由“导出”生成的场景 JSON');
      const failures: string[] = [];
      for (const entry of list) {
        try {
          replace(await api.createCase({ name: String(entry.name).slice(0, 220), description: entry.description ?? '', objective: entry.objective, work_directory_id: null, materials: (entry.materials ?? []).filter((material: { kind?: string }) => material.kind === 'LINK'), stages: entry.stages.map((stage: Record<string, string>) => ({ stage_key: stage.stage_key, name: stage.name, objective: stage.objective })) } as never));
        } catch (error) { failures.push(`${entry.name}：${error instanceof Error ? error.message : String(error)}`); }
      }
      await qc.invalidateQueries({ queryKey: ['cases'] });
      setBatchNotice(failures.length ? `导入：${list.length - failures.length} 成功，${failures.length} 失败 — ${failures.join('；')}` : `已导入 ${list.length} 个场景`);
    } catch (error) { setBatchNotice(`导入失败：${error instanceof Error ? error.message : String(error)}`); }
    finally { setBatchBusy(false); }
  };
  const batchCopy = () => runBatch('批量复制', async item => {
    const full = await api.case(item.id);
    const created = await api.createCase({ name: `${full.name} 副本`.slice(0, 220), description: full.description ?? '', objective: full.objective, work_directory_id: full.work_directory_id ?? null, materials: (full.materials ?? []).filter(material => material.kind === 'LINK'), stages: full.stages.map(stage => ({ stage_key: stage.stage_key, name: stage.name, objective: stage.objective })) } as never);
    replace(created);
  });
  const batchExport = async () => {
    setBatchBusy(true); setBatchNotice('');
    try {
      const full = await Promise.all(checkedCases.map(item => api.case(item.id)));
      const payload = full.map(item => ({ name: item.name, description: item.description ?? '', objective: item.objective, state: item.state, materials: item.materials ?? [], stages: item.stages.map(stage => ({ stage_key: stage.stage_key, name: stage.name, objective: stage.objective, state: stage.state, execution_mode: stage.execution_mode })), exported_at: new Date().toISOString() }));
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = `cases-${payload.length}.json`; link.click(); URL.revokeObjectURL(url);
    } catch (error) { setBatchNotice(`批量导出失败：${error instanceof Error ? error.message : String(error)}`); }
    finally { setBatchBusy(false); }
  };
  const batchDelete = async () => {
    if (!checkedCases.length || !await batchDialog.confirm({ title: `删除 ${checkedCases.length} 个场景？`, message: '会永久删除所选 Case、阶段控制元数据、协调会话及专属工作目录，无法恢复。关联 FlowRun 历史会保留。进行中的执行、运行中会话或未完成受控计划会阻止对应场景删除。', confirmLabel: '批量删除', tone: 'danger' })) return;
    await runBatch('批量删除', async item => { await api.deleteCase(item.id); remove(item.id); });
  };
  return <main className="scenario-space"><aside><header className="scenario-side-head"><label className="case-sidebar-search"><Search size={13}/><input aria-label="搜索场景" placeholder="搜索场景…" value={query} onChange={event => setQuery(event.target.value)}/></label><button className="primary" aria-label="新建场景" onClick={() => setCreating(true)}><Plus size={14}/><span>新建</span></button></header>
    <div className="case-batch-bar"><label className="case-batch-all"><input type="checkbox" aria-label="全选场景" checked={visibleCases.length > 0 && checkedCases.length === visibleCases.length} onChange={event => setCheckedIds(event.target.checked ? new Set(visibleCases.map(item => item.id)) : new Set())}/><span>{checkedCases.length ? `已选 ${checkedCases.length}` : '全选'}</span></label><button type="button" aria-label="批量复制" title="批量复制" disabled={!checkedCases.length || batchBusy} onClick={batchCopy}><Copy size={13}/><span>复制</span></button><button type="button" aria-label="批量导出" title="批量导出" disabled={!checkedCases.length || batchBusy} onClick={batchExport}><Download size={13}/><span>导出</span></button><button type="button" aria-label="导入场景" title="导入场景（由导出生成的 JSON）" disabled={batchBusy} onClick={() => importInput.current?.click()}><Upload size={13}/><span>导入</span></button><input ref={importInput} type="file" accept="application/json,.json" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void batchImport(file); }}/><button type="button" className="danger" aria-label="批量删除" title="批量删除" disabled={!checkedCases.length || batchBusy} onClick={batchDelete}><Trash2 size={13}/><span>删除</span></button></div>{batchNotice && <p className="error case-batch-notice">{batchNotice}</p>}
    {cases.isLoading ? <p className="scenario-empty">正在加载场景…</p> : cases.error ? <p className="error">{cases.error.message}</p> : cases.data?.length ? <div className="scenario-tree">{visibleCases.map(item => {
      const isSelected = !creating && selected?.id === item.id;
      const completed = item.stages.filter(stage => stage.state === 'COMPLETED').length;
      const expanded = expandedIds.has(item.id);
      return <section className="scenario-case-wrap" key={item.id}><div className={`scenario-case-row${isSelected ? ' active' : ''}`}>{<input type="checkbox" className="scenario-case-check" aria-label={`选择场景 ${item.name}`} checked={checkedIds.has(item.id)} onChange={() => toggleChecked(item.id)}/>}<button className={`scenario-case-head ${isSelected ? 'active' : ''}`} onClick={() => { setSelectedId(item.id); setCreating(false); if (sessionRoute) onNavigate('/', true); }}><i className={item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id) ? item.state.toLowerCase() : 'draft'}/><span><b>{item.name}</b><small>{item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id) || item.state === 'COMPLETED' || item.state === 'ARCHIVED' ? CASE_STATE[item.state] : '待启动'} · {completed}/{item.stages.length} 阶段</small></span></button><button type="button" className={`scenario-case-toggle${expanded ? ' expanded' : ''}`} aria-label={`${expanded ? '收起' : '展开'}场景 ${item.name}`} aria-expanded={expanded} onClick={() => toggleExpanded(item.id)}><ChevronRight size={15}/></button></div>{expanded && item.control_conversation_binding_id && (() => {
        const round = item.control_rounds?.find(candidate => candidate.binding_id === item.control_conversation_binding_id);
        const title = round?.display_title || 'Case Agent';
        const active = sessionRoute?.caseId === item.id && sessionRoute.bindingId === item.control_conversation_binding_id;
        return <button className={`scenario-session${active ? ' active' : ''}`} title={title} aria-current={active ? 'page' : undefined} onClick={() => onNavigate(`/cases/${encodeURIComponent(item.id)}/conversations/${encodeURIComponent(item.control_conversation_binding_id!)}`)}><span><b>{title}</b></span></button>;
      })()}</section>;
    })}</div> : <div className="scenario-empty"><ClipboardList size={21}/><b>暂无场景</b></div>}{sessionRoute && !creating && workspace.data && <footer className="agent-workbench-rail-footer scenario-session-footer"><button type="button" aria-label="会话配置" title="会话配置" onClick={() => setSessionConfigOpen(true)}><Boxes size={13}/><span><b>会话配置</b></span><ChevronRight size={12}/></button></footer>}</aside>
    {sessionConfigOpen && sessionRoute && workspace.data && <CapabilityManager workspaceId={workspace.data.id} bindingId={sessionRoute.bindingId} conversationCapabilities={sessionConversation.data?.capabilities} onClose={() => setSessionConfigOpen(false)}/>}
    {sessionRoute && selected?.id === sessionRoute.caseId && !creating ? <main className="scenario-workspace scenario-conversation"><CaseSessionPage key={`${sessionRoute.caseId}:${sessionRoute.bindingId}`} caseId={sessionRoute.caseId} bindingId={sessionRoute.bindingId} onNavigate={onNavigate} onReturn={() => onNavigate('/', true)}/></main>
      : creating ? <main className="scenario-workspace case-basics-workspace"><div className="scenario-workspace-inner"><header className="scenario-casebar"><h1>新建 Case</h1></header><div className="scenario-workspace-content">{workspace.error && <p className="error">{errorMessage(workspace.error)}</p>}<BasicsForm key="new-case" workspaceId={workspace.data?.id ?? ''} onCancel={() => { setCreating(false); void cases.refetch(); }} onSaved={item => { replace(item); setSelectedId(item.id); setCreating(false); }}/></div></div></main>
      : selected ? <ScenarioWorkspace key={selected.id} item={selected} workspaceId={workspace.data?.id ?? ''} onUpdated={replace} onDelete={() => remove(selected.id)} onOpenConversation={(bindingId, planId) => onNavigate(`/cases/${encodeURIComponent(selected.id)}/conversations/${encodeURIComponent(bindingId)}${planId ? `?plan=${encodeURIComponent(planId)}` : ''}`)} onOpenRun={id => void api.flowRun(id).then(run => openFlowRunFromCase(run.parent_flow_run_id && run.run_mode ? `/flow-runs/${encodeURIComponent(run.parent_flow_run_id)}/records/${run.run_mode === 'AUTOMATIC' ? 'automatic' : 'stepwise'}/${encodeURIComponent(run.id)}` : `/flow-runs/${encodeURIComponent(id)}`, onNavigate)).catch(() => openFlowRunFromCase(`/flow-runs/${encodeURIComponent(id)}`, onNavigate))} onOpenRecord={stage => {
        const execution = stage.execution;
        const parentRunId = execution?.parent_flow_run_id;
        const mode = execution?.run_mode;
        const recordId = stage.current_execution_flow_run_id;
        const path = parentRunId && mode && recordId
          ? `/flow-runs/${encodeURIComponent(parentRunId)}/records/${mode === 'AUTOMATIC' ? 'automatic' : 'stepwise'}/${encodeURIComponent(recordId)}`
          : `/flow-runs/${encodeURIComponent(recordId!)}`;
        openFlowRunFromCase(path, onNavigate);
      }}/>
        : <section className="scenario-workspace scenario-workspace-empty"><ClipboardList size={28}/><h2>选择或创建一个场景</h2></section>}
  </main>;
}
