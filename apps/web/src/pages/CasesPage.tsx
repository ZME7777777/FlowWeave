import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, Bot, Clock3, Search, Info, Play, Check, ChevronRight, ClipboardList, ExternalLink, FileText, GripVertical, Link2, MessageSquareCode, Pencil, Plus, Settings2, Trash2, Upload, X } from 'lucide-react';
import { api, caseMaterialFileUrl } from '../api/client';
import type { Case, CaseBasics, CaseMaterial, CaseSessionConfig, CaseStage } from '../types';
import { CapabilityManager } from '../components/agent-session/AgentSessionWorkbench';
import { useEscapeClose } from '../components/useEscapeClose';
import { useProductDialog } from '../components/ProductDialogContext';
import { CaseChoice } from './CaseChoice';
import './cases.css';

const CASE_STATE: Record<Case['state'], string> = { DRAFT: '草稿', ACTIVE: '进行中', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVED: '已归档' };
const STAGE_STATE: Record<CaseStage['state'], string> = { PENDING: '待启动', ACTIVE: '待启动', COMPLETED: '已完成', SKIPPED: '已跳过' };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '保存失败，请重试。';
const stageExecutionLabel = (stage: CaseStage) => {
  if (!stage.current_execution_flow_run_id) return stage.state === 'ACTIVE' ? '当前阶段' : STAGE_STATE[stage.state];
  const state = stage.execution?.state;
  if (state === 'COMPLETED') return '已完成';
  if (state === 'FAILED') return '执行失败';
  if (state === 'CANCELLED') return '已停止';
  if (state === 'MISSING') return '记录不可用';
  if (state === 'DRAFT') return '待启动';
  if (state === 'WAITING_HUMAN' || stage.execution?.automatic_block) return '等待处理';
  return state ? '运行中' : '同步中';
};

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
  return createPortal(<div className="modal-backdrop"><form className="modal case-add-material-dialog" role="dialog" aria-modal="true" aria-label="添加资料" aria-busy={busy} onKeyDown={event => {
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

function PathTab({ item, onUpdated, onOpenRun }: { item: Case; onUpdated: (item: Case) => void; onOpenRun: (id: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [parentId, setParentId] = useState('');
  const [mode, setMode] = useState('');
  const [recordId, setRecordId] = useState('');
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
  const execute = useMutation({
    mutationFn: async (stage: CaseStage) => {
      if (!stage.current_execution_flow_run_id) await api.launchCaseStage(item.id, stage.id);
      await api.startCaseStageExecution(item.id, stage.id);
      return api.case(item.id);
    },
    onError: () => { void api.case(item.id).then(onUpdated).catch(() => undefined); },
    onSuccess: result => { onUpdated(result); setStatus('已创建并启动独立执行记录'); },
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
  return <section className={`scenario-path scenario-path-editor case-connected-path${!item.stages.length ? ' is-empty' : ''}`} aria-label="Case 路径卡片">
    <ol>{item.stages.map((stage, index) => <li key={stage.id} data-stage-id={stage.id} className={`${stage.state.toLowerCase()}${draggingId === stage.id ? ' is-dragging' : ''}${dropTarget?.id === stage.id ? dropTarget.after ? ' drop-after' : ' drop-before' : ''}`}
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
      <article draggable={!pathLocked && !update.isPending && !adding && !execute.isPending} aria-label={`路径卡片 ${stage.name}`}
        onDragStart={event => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('application/x-flowweave-case-stage', stage.id); setDraggingId(stage.id); setStatus(''); }} onDragEnd={endDrag}>
        <header><div className="case-card-title"><span className={`case-stage-state ${stage.current_execution_flow_run_id ? 'executing' : stage.state.toLowerCase()}`}>{stageExecutionLabel(stage)}</span><span>{stage.execution_mode === 'STEPWISE' ? '逐步运行' : '连续运行'}</span><h3>{stage.name}</h3></div>
          <div className="scenario-card-actions"><button className="ghost case-drag-handle" aria-label={`调整顺序 ${stage.name}`} title="拖拽调整顺序，也可聚焦后按上下方向键" disabled={pathLocked || update.isPending || adding || execute.isPending} onKeyDown={event => {
            if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
            event.preventDefault(); const to = index + (event.key === 'ArrowDown' ? 1 : -1);
            if (item.stages[to]) reorder(stage.id, item.stages[to].id, to > index);
          }}><GripVertical size={17}/></button><button className="ghost" aria-label={`删除卡片 ${stage.name}`} disabled={pathLocked || Boolean(stage.current_execution_flow_run_id) || update.isPending || execute.isPending} onClick={() => { setStatus(''); update.mutate(item.stages.filter(candidate => candidate.id !== stage.id).map(candidate => ({ id: candidate.id }))); }}><Trash2 size={15}/></button></div>
        </header>
        {stage.objective && <p className="case-stage-objective">{stage.objective}</p>}
        <div className="case-stage-progress">{stage.execution?.active_node && <span>当前节点 <b>{stage.execution.active_node.node_name}</b></span>}{stage.execution?.progress && <span>已接受 {stage.execution.progress.accepted} · 活动 {stage.execution.progress.active}</span>}{stage.execution?.automatic_block && <span className="error">待补全 {stage.execution.automatic_block.gates.length} 项门禁配置</span>}</div>
        <footer>{stage.flow_run_id && <button className="ghost" onClick={() => onOpenRun(stage.flow_run_id!)}><ExternalLink size={12}/>流程模板</button>}{stage.current_execution_flow_run_id && <button className="ghost" onClick={() => onOpenRun(stage.current_execution_flow_run_id!)}><ExternalLink size={12}/>查看执行记录</button>}{!pathLocked && stage.state === 'ACTIVE' && (!stage.current_execution_flow_run_id || stage.execution?.state === 'DRAFT') && <button className="ghost case-manual-launch" disabled={execute.isPending} onClick={() => { setStatus(''); execute.mutate(stage); }}>{execute.isPending ? '启动中…' : '手动启动'}</button>}</footer>

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
    {(update.error || execute.error) && <p className="error" role="alert">{errorMessage(update.error || execute.error)}</p>}
  </section>;
}

function CaseActivity({ item, onOpenRun }: { item: Case; onOpenRun: (id: string) => void }) {
  const events = [
    ...item.control_plans.map(plan => ({ id: `plan-${plan.id}`, kind: '控制动作', action: ({ CREATE_EXECUTION_COPY: '创建执行副本', START_EXECUTION: '启动流程', START_STEPWISE_NODE: '启动逐步节点', STOP_EXECUTION: '停止执行', RERUN_STEPWISE_NODE: '重跑节点' })[plan.action], state: plan.state, reason: plan.reason, error: plan.error_code, runId: plan.execution_flow_run_id, created: plan.created_at })),
    ...item.decision_proposals.map(proposal => ({ id: `decision-${proposal.id}`, kind: '执行决策', action: ({ START_STAGE_EXECUTION: '启动阶段', CONTINUE_STEPWISE: '继续逐步运行', STOP_FLOW_RUN: '停止流程', RERUN_STEPWISE_NODE: '重跑节点', COPY_AUTOMATIC_RUN: '复制连续运行' })[proposal.action], state: proposal.state, reason: proposal.reason, error: '', runId: proposal.flow_run_id, created: proposal.created_at })),
    ...item.tasks.map(task => ({ id: `task-${task.id}`, kind: '观察任务', action: ({ OBSERVE: '观察结果', PROPOSE_REVISION: '建议修订', REVIEW: '复核' })[task.task_type], state: task.state, reason: '', error: '', runId: task.flow_run_id, created: task.created_at })),
  ].sort((left, right) => right.created.localeCompare(left.created));
  const states: Record<string, string> = { PENDING: '待处理', VALIDATED: '已校验', EXECUTING: '执行中', RUNNING: '执行中', SUCCEEDED: '成功', FAILED: '失败', CANCELLED: '已取消', PROPOSED: '已提出', EXECUTED: '已执行', SUPERSEDED: '已替代' };
  return <section className="case-activity-list">{events.length ? events.map(event => <article key={event.id}><header><b>{event.action}</b><span className={`case-activity-state ${event.state.toLowerCase()}`}>{states[event.state] ?? event.state}</span></header><small>{event.kind} · {new Date(event.created).toLocaleString()}</small>{event.reason && <p>{event.reason}</p>}{event.error && <p className="error">{event.error}</p>}{event.runId && <button className="ghost" onClick={() => onOpenRun(event.runId!)}><ExternalLink size={12}/>查看执行记录</button>}</article>) : <div className="empty"><Clock3 size={24}/><span>暂无操作记录</span></div>}</section>;
}

function ScenarioWorkspace({ item, workspaceId, onUpdated, onDelete, onOpenConversation, onOpenRun }: { item: Case; workspaceId: string; onUpdated: (item: Case) => void; onDelete: () => void; onOpenConversation: (id: string) => void; onOpenRun: (id: string) => void }) {
  const dialog = useProductDialog();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const [panel, setPanel] = useState<'basics' | 'agent' | 'activity'>();
  useEscapeClose(() => setPanel(undefined), Boolean(panel));
  const observing = item.state !== 'COMPLETED' && item.state !== 'ARCHIVED' && Boolean(item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id));
  const live = useQuery({ queryKey: ['case-live', item.id], queryFn: () => api.case(item.id), enabled: observing, refetchInterval: observing ? 3000 : false, retry: 1 });
  useEffect(() => { if (live.data) onUpdated(live.data); }, [live.data, onUpdated]);
  const startKey = useRef<string | undefined>(undefined);
  const start = useMutation({
    mutationFn: () => {
      startKey.current ??= crypto.randomUUID();
      return api.createCaseControlConversation(item.id, item.session_config as CaseSessionConfig, startKey.current);
    },
    onSuccess: result => { onUpdated(result); if (result.control_conversation_binding_id) onOpenConversation(result.control_conversation_binding_id); },
  });
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
  const startBlock = item.control_conversation_binding_id ? '' : terminal ? '场景已结束' : !workspaceId ? '正在读取工作区' : missing.length ? `请先${missing.join('、')}` : '';
  const activityCount = item.control_plans.length + item.decision_proposals.length + item.tasks.length;
  const openOrStart = () => {
    if (item.control_conversation_binding_id) { onOpenConversation(item.control_conversation_binding_id); return; }
    if (startBlock || start.isPending) return;
    setError(''); start.mutate();
  };
  const active = item.stages.find(stage => stage.state === 'ACTIVE');
  const completed = item.stages.filter(stage => stage.state === 'COMPLETED').length;
  const stateLabel = item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id) ? CASE_STATE[item.state] : terminal ? CASE_STATE[item.state] : '待启动';
  const panelTitle = panel === 'basics' ? '场景信息' : panel === 'agent' ? 'Agent 配置' : '操作记录';
  return <main className="scenario-workspace case-single-workspace"><div className="scenario-workspace-inner">
    <header className="case-workspace-toolbar"><div className="case-workspace-title"><h1 title={item.name}>{item.name}</h1><span className={`case-workspace-state ${item.state.toLowerCase()}`}>{stateLabel}</span><small>{completed}/{item.stages.length} 阶段</small>{active && <small className="case-current-stage" title={`当前阶段：${active.name}`}>{active.name}</small>}</div><div className="scenario-case-actions"><button className="ghost" aria-pressed={panel === 'basics'} onClick={() => setPanel('basics')}><Info size={14}/>场景信息</button><button className="ghost" aria-pressed={panel === 'agent'} onClick={() => setPanel('agent')}><Settings2 size={14}/>Agent 配置</button><button className="primary" disabled={start.isPending || Boolean(startBlock)} title={startBlock || undefined} aria-description={startBlock || undefined} onClick={openOrStart}>{item.control_conversation_binding_id ? <Bot size={14}/> : <Play size={13}/>} {start.isPending ? '启动中…' : item.control_conversation_binding_id ? '打开 Agent' : '启动 Agent'}</button><button className="ghost case-delete-action" type="button" aria-label="删除场景" title="删除场景" disabled={deleting} onClick={() => void deleteCase()}><Trash2 size={14}/></button></div></header>
    {(error || start.error || live.error) && <p className="error scenario-delete-error" role="alert">{error || errorMessage(start.error || live.error)}</p>}

    <div className="case-single-layout"><section className="case-path-panel"><PathTab item={item} onUpdated={onUpdated} onOpenRun={onOpenRun}/></section></div>
    {panel && createPortal(<div className="modal-backdrop case-inspector-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setPanel(undefined); }}><section className={`modal case-inspector scenario-workspace case-inspector-${panel}`} role="dialog" aria-modal="true" aria-label={panelTitle} onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href]')];
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}><header className="case-inspector-heading"><h2>{panelTitle}</h2><button className="ghost" autoFocus aria-label="关闭场景面板" onClick={() => setPanel(undefined)}><X size={17}/></button></header><div className="case-inspector-body" key={panel}>{panel === 'basics' ? <><BasicsForm item={item} workspaceId={workspaceId} onSaved={onUpdated}/>{activityCount > 0 && <button className="ghost case-audit-entry" title="查看平台、手动与会话控制动作的结果" aria-label="查看操作记录" onClick={() => setPanel('activity')}><Clock3 size={13}/>操作记录<small>{activityCount}</small><ChevronRight size={13}/></button>}</> : panel === 'agent' ? <ConversationTab item={item} workspaceId={workspaceId} onUpdated={onUpdated}/> : <CaseActivity item={item} onOpenRun={onOpenRun}/>}</div></section></div>, document.body)}
  </div></main>;
}

export function CasesPage({ onNavigate }: { onNavigate: (path: string) => void }) {
  const qc = useQueryClient();
  const cases = useQuery({ queryKey: ['cases'], queryFn: api.cases });
  const workspace = useQuery({ queryKey: ['default-agent-workspace'], queryFn: () => api.defaultAgentWorkspace() });
  const [selectedId, setSelectedId] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState('');
  const selected = useMemo(() => cases.data?.find(item => item.id === selectedId) ?? cases.data?.[0], [cases.data, selectedId]);
  const replace = useCallback((item: Case) => { qc.setQueryData<Case[]>(['cases'], current => current?.some(candidate => candidate.id === item.id) ? current.map(candidate => candidate.id === item.id && candidate.row_version <= item.row_version ? item : candidate) : [item, ...(current ?? [])]); }, [qc]);
  const remove = (id: string) => { qc.setQueryData<Case[]>(['cases'], current => current?.filter(candidate => candidate.id !== id) ?? []); qc.removeQueries({ queryKey: ['case-live', id] }); setSelectedId(current => current === id ? undefined : current); };
  return <main className="scenario-space"><aside><header className="scenario-side-head"><b>场景列表</b><button className="primary" aria-label="新建场景" onClick={() => setCreating(true)}><Plus size={14}/><span>新建</span></button></header>
    <label className="case-sidebar-search"><Search size={13}/><input aria-label="搜索场景" placeholder="搜索场景…" value={query} onChange={event => setQuery(event.target.value)}/></label>
    {cases.isLoading ? <p className="scenario-empty">正在加载场景…</p> : cases.error ? <p className="error">{cases.error.message}</p> : cases.data?.length ? <div className="scenario-tree">{cases.data.filter(item => !query.trim() || `${item.name} ${item.objective} ${item.description ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).map(item => {
      const isSelected = !creating && selected?.id === item.id;
      const completed = item.stages.filter(stage => stage.state === 'COMPLETED').length;
      return <section className="scenario-case-wrap" key={item.id}><button className={`scenario-case-head ${isSelected ? 'active' : ''}`} onClick={() => { setSelectedId(item.id); setCreating(false); }}><i className={item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id) ? item.state.toLowerCase() : 'draft'}/><span><b>{item.name}</b><small>{item.control_conversation_binding_id || item.stages.some(stage => stage.current_execution_flow_run_id) || item.state === 'COMPLETED' || item.state === 'ARCHIVED' ? CASE_STATE[item.state] : '待启动'} · {completed}/{item.stages.length} 阶段</small></span><ChevronRight size={15}/></button>{isSelected && item.control_conversation_binding_id && <button className="scenario-session" onClick={() => onNavigate(`/cases/${encodeURIComponent(item.id)}/conversations/${encodeURIComponent(item.control_conversation_binding_id!)}`)}><MessageSquareCode size={13}/><span><b>Case Agent</b></span><em>打开</em></button>}</section>;
    })}</div> : <div className="scenario-empty"><ClipboardList size={21}/><b>暂无场景</b></div>}</aside>
    {creating ? <main className="scenario-workspace case-basics-workspace"><div className="scenario-workspace-inner"><header className="scenario-casebar"><h1>新建 Case</h1></header><div className="scenario-workspace-content">{workspace.error && <p className="error">{errorMessage(workspace.error)}</p>}<BasicsForm key="new-case" workspaceId={workspace.data?.id ?? ''} onCancel={() => { setCreating(false); void cases.refetch(); }} onSaved={item => { replace(item); setSelectedId(item.id); setCreating(false); }}/></div></div></main>
      : selected ? <ScenarioWorkspace key={selected.id} item={selected} workspaceId={workspace.data?.id ?? ''} onUpdated={replace} onDelete={() => remove(selected.id)} onOpenConversation={bindingId => onNavigate(`/cases/${encodeURIComponent(selected.id)}/conversations/${encodeURIComponent(bindingId)}`)} onOpenRun={id => onNavigate(`/flow-runs/${encodeURIComponent(id)}`)}/>
        : <section className="scenario-workspace scenario-workspace-empty"><ClipboardList size={28}/><h2>选择或创建一个场景</h2></section>}
  </main>;
}
