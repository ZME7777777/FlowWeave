import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, Check, ChevronRight, ClipboardList, ExternalLink, FileText, GripVertical, Link2, MessageSquareCode, Pencil, Plus, Settings2, Trash2, Upload, X } from 'lucide-react';
import { api, caseMaterialFileUrl } from '../api/client';
import type { Case, CaseBasics, CaseMaterial, CaseSessionConfig, CaseStage } from '../types';
import { CapabilityManager } from '../components/agent-session/AgentSessionWorkbench';
import { useEscapeClose } from '../components/useEscapeClose';
import { useProductDialog } from '../components/ProductDialogContext';
import { CaseChoice } from './CaseChoice';
import './cases.css';

const CASE_STATE: Record<Case['state'], string> = { DRAFT: '草稿', ACTIVE: '进行中', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVED: '已归档' };
const STAGE_STATE: Record<CaseStage['state'], string> = { PENDING: '待启动', ACTIVE: '待启动', COMPLETED: '已完成', SKIPPED: '已跳过' };
type Tab = 'overview' | 'path' | 'conversation';
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '保存失败，请重试。';
const stageExecutionLabel = (stage: CaseStage) => {
  if (!stage.current_execution_flow_run_id) return STAGE_STATE[stage.state];
  if (stage.execution?.state === 'COMPLETED') return '已完成';
  if (stage.execution?.state === 'FAILED') return '执行失败';
  return '运行中';
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

function BasicsForm({ item, workspaceId, onSaved, onCancel }: {
  item?: Case; workspaceId: string; onSaved: (item: Case) => void; onCancel?: () => void;
}) {
  const directories = useQuery({ queryKey: ['agent-work-directories', workspaceId], queryFn: () => api.agentWorkDirectories(workspaceId), enabled: Boolean(workspaceId) });
  const [name, setName] = useState(item?.name ?? '');
  const [description, setDescription] = useState(item?.description ?? '');
  const [objective, setObjective] = useState(item?.objective ?? '');
  const [directoryId, setDirectoryId] = useState(item?.work_directory_id ?? '');
  const [materials, setMaterials] = useState<CaseMaterial[]>(item?.materials ?? []);
  const [files, setFiles] = useState<File[]>([]);
  const [linkOpen, setLinkOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [linkName, setLinkName] = useState('');
  const [linkUrl, setLinkUrl] = useState('');
  const [fileName, setFileName] = useState('');
  const [selectedFile, setSelectedFile] = useState<File>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [toast, setToast] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<CaseMaterial>();
  // Keep the created identity on a partial upload failure so retry never creates another Case.
  const saved = useRef(item);
  const fileInput = useRef<HTMLInputElement>(null);
  const showToast = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(current => current === message ? '' : current), 3000);
  };
  const addLink = () => {
    try { const url = new URL(linkUrl); if (!['https:', 'http:'].includes(url.protocol)) throw new Error(); }
    catch { showToast('请输入有效的 HTTP 或 HTTPS 链接。'); return; }
    setMaterials(current => [...current, { kind: 'LINK', name: linkName.trim() || linkUrl, value: linkUrl }]);
    setLinkName(''); setLinkUrl(''); setLinkOpen(false); setError(''); setNotice('');
  };
  const addFile = () => {
    if (!selectedFile) { showToast('请选择要添加的文件。'); return; }
    const renamed = fileName.trim() ? new File([selectedFile], fileName.trim(), { type: selectedFile.type, lastModified: selectedFile.lastModified }) : selectedFile;
    setFiles(current => [...current, renamed]);
    setFileName(''); setSelectedFile(undefined); setUploadOpen(false); setNotice('');
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !name.trim() || !workspaceId) return;
    if (materials.length + files.length > 100) { setError('最多添加 100 项资料。'); return; }
    if (files.some(file => file.size > 64 * 1024 * 1024)) { setError('单个文件不能超过 64 MB。'); return; }
    setBusy(true); setError(''); setNotice('');
    const basics: CaseBasics = { name: name.trim(), description, objective, materials, work_directory_id: directoryId || null };
    try {
      const current = saved.current && (!item || saved.current.row_version > item.row_version) ? saved.current : item;
      let result = current
        ? await api.updateCase(current.id, { ...basics, expected_row_version: current.row_version })
        : await api.createCase(basics);
      saved.current = result;
      for (const file of files) {
        result = await api.uploadCaseMaterial(result.id, result.row_version, file);
        saved.current = result;
        setMaterials(result.materials ?? []);
        setFiles(current => current.filter(candidate => candidate !== file));
      }
      setNotice('已保存'); onSaved(result);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };
  return <form className="scenario-overview scenario-basics-form" onSubmit={event => void save(event)}>
    <fieldset disabled={busy}><div className="case-basics-fields">
      <label>名称<input autoFocus={!item} value={name} maxLength={220} required onChange={event => { setName(event.target.value); setNotice(''); }} placeholder="为这个 Case 起个名称"/></label>
      <div className="case-field"><span>工作区 <span className="scenario-optional">可选</span></span><CaseChoice label="工作区" value={directoryId} disabled={busy || Boolean(item?.control_conversation_binding_id)} onChange={value => { setDirectoryId(value); setNotice(''); }} options={[
        { value: '', label: '默认工作区', detail: '自动创建 Case 专属工作区' },
        ...(directoryId && !directories.data?.items.some(directory => directory.id === directoryId) ? [{ value: directoryId, label: '当前工作区（暂不可用）' }] : []),
        ...(directories.data?.items.filter(directory => directory.id !== item?.control_work_directory_id).map(directory => ({ value: directory.id, label: directory.display_name })) ?? []),
      ]}/><small>{item?.control_conversation_binding_id ? '会话已创建，工作区已固定。' : '输入名称搜索已有工作区，或选择默认工作区。'}</small>{directories.error && <p className="error">{errorMessage(directories.error)}</p>}</div>
      <label>说明<textarea value={description} maxLength={20_000} onChange={event => { setDescription(event.target.value); setNotice(''); }} placeholder="介绍当前场景的背景。"/></label>
      <label>目标<textarea value={objective} maxLength={20_000} onChange={event => { setObjective(event.target.value); setNotice(''); }} placeholder="说明需要达成的结果。"/></label>
      </div>
      <section className="scenario-materials" aria-label="资料"><header><span><b>资料</b><small>添加完成当前场景所需的基础资料。</small></span><div><button className="secondary" type="button" onClick={() => { setLinkOpen(current => !current); setUploadOpen(false); }}><Link2 size={14}/>添加链接</button><button className="secondary" type="button" onClick={() => { setUploadOpen(current => !current); setLinkOpen(false); }}><Upload size={14}/>添加文件</button></div></header>
        <input hidden ref={fileInput} type="file" aria-label="选择资料文件" onChange={event => { const file = event.target.files?.[0]; if (file) { setSelectedFile(file); setFileName(current => current || file.name); } event.target.value = ''; }}/>
        {linkOpen && <div className="scenario-link-editor"><label>链接名称<input value={linkName} maxLength={220} onChange={event => setLinkName(event.target.value)} placeholder="选填"/></label><label>链接地址<input value={linkUrl} type="url" onChange={event => setLinkUrl(event.target.value)} placeholder="https://"/></label><button className="secondary scenario-material-confirm" type="button" disabled={!linkUrl.trim()} onClick={addLink}><Check size={15}/><span className="sr-only">添加链接</span></button></div>}
        {uploadOpen && <div className="scenario-upload-editor"><label>资料名称<input value={fileName} maxLength={220} onChange={event => setFileName(event.target.value)} placeholder="为这份资料起个名称"/></label><div className="scenario-file-picker"><span>{selectedFile ? selectedFile.name : '尚未选择文件'}</span><button className="secondary" type="button" onClick={() => fileInput.current?.click()}><Upload size={14}/>选择文件</button></div><button className="secondary scenario-material-confirm" type="button" disabled={!selectedFile} onClick={addFile}><Check size={15}/><span className="sr-only">添加文件</span></button></div>}
        {!materials.length && !files.length && !linkOpen && !uploadOpen && <p className="scenario-materials-empty">尚未添加资料</p>}
        <ul>{materials.map((material, index) => <li key={`${index}-${material.value}`}>{material.kind === 'LINK' ? <Link2 size={15}/> : <FileText size={15}/>}{material.kind === 'LINK' ? <a href={material.value} target="_blank" rel="noreferrer">{material.name}</a> : <button className="ghost case-material-preview-button" type="button" onClick={() => setPreview(material)}>{material.name}</button>}
          <span className={`case-material-kind ${material.kind.toLowerCase()}`}>{material.kind === 'LINK' ? '链接' : '文件'}</span><MaterialName name={material.name} file={material.kind === 'FILE'} onRename={name => { setMaterials(current => current.map((entry, i) => i === index ? { ...entry, name } : entry)); setNotice(''); }}/>
          <button className="ghost" type="button" aria-label={`移除资料 ${material.name}`} onClick={() => { setMaterials(current => current.filter((_, i) => i !== index)); setNotice(''); }}><X size={14}/></button></li>)}
          {files.map((file, index) => <li key={index}><FileText size={15}/><span>{file.name}<small>{file.type || '未知类型'} · 保存时上传</small></span><span className="case-material-kind file">文件</span>
            <MaterialName name={file.name} file onRename={name => { setFiles(current => current.map((entry, i) => i === index ? new File([entry], name, { type: entry.type, lastModified: entry.lastModified }) : entry)); setNotice(''); }}/>
            <button className="ghost" type="button" aria-label={`移除文件 ${file.name}`} onClick={() => setFiles(current => current.filter((_, i) => i !== index))}><X size={14}/></button></li>)}</ul>
      </section>
      <footer className="scenario-form-actions">{onCancel && <button className="secondary" type="button" onClick={onCancel}>取消</button>}<button className="primary" disabled={!name.trim() || !workspaceId}>{busy ? '保存中…' : '保存'}</button><span role="status">{notice}</span></footer>
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
    {toast && <p className="case-form-toast" role="status">{toast}</p>}
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
    <footer className="scenario-form-actions"><button className="primary" disabled={!model || save.isPending || !credentialsReady} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存配置'}</button><span role="status">{notice}</span></footer>
    {configOpen && <CapabilityManager configurationPreset workspaceId={workspaceId} draftCapabilityIds={capabilities} draftCredentialIds={credentials} onClose={() => setConfigOpen(false)} onCreateEnhancedConversation={ids => { setCapabilities(ids); setNotice(''); }} onDraftCredentialIdsChange={ids => { setCredentialSelection(ids); setNotice(''); }}/>}
  </section>;
}

function PathTab({ item, onUpdated, onOpenRun, onDelete, deleting }: { item: Case; onUpdated: (item: Case) => void; onOpenRun: (id: string) => void; onDelete: () => void; deleting: boolean }) {
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
    mutationFn: async (stageId: string) => {
      const result = await api.launchCaseStage(item.id, stageId);
      await api.startCaseStageExecution(item.id, stageId);
      return result;
    },
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
  const endDrag = () => { setDraggingId(''); setDropTarget(undefined); };
  return <section className={`scenario-path scenario-path-editor case-connected-path${!item.stages.length ? ' is-empty' : ''}`} aria-label="Case 路径卡片">
    <header className="case-path-heading">{item.stages.length > 0 && <span><b>运行路径</b><small>{item.stages.length} 个运行记录，按顺序串联</small></span>}<div>{item.stages.length > 0 && <small>拖拽卡片调整顺序</small>}<button className="danger" type="button" disabled={deleting} onClick={onDelete}><Trash2 size={14}/>{deleting ? '删除中…' : '删除场景'}</button></div></header>
    <ol>{item.stages.map((stage, index) => <li key={stage.id} data-stage-id={stage.id} className={`${stage.state.toLowerCase()}${draggingId === stage.id ? ' is-dragging' : ''}${dropTarget?.id === stage.id ? dropTarget.after ? ' drop-after' : ' drop-before' : ''}`}
      onDragOver={event => {
        if (!draggingId || update.isPending) return;
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
      <article draggable={!update.isPending && !adding && !execute.isPending} aria-label={`路径卡片 ${stage.name}`}
        onDragStart={event => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('application/x-flowweave-case-stage', stage.id); setDraggingId(stage.id); setStatus(''); }} onDragEnd={endDrag}>
        <header><div className="case-card-title"><span className={`case-stage-state ${stage.current_execution_flow_run_id ? 'executing' : stage.state.toLowerCase()}`}>{stageExecutionLabel(stage)}</span><span>{stage.execution_mode === 'STEPWISE' ? '逐步运行' : '连续运行'}</span><h3>{stage.name}</h3></div>
          <div className="scenario-card-actions"><button className="ghost case-drag-handle" aria-label={`调整顺序 ${stage.name}`} title="拖拽调整顺序，也可聚焦后按上下方向键" disabled={update.isPending || adding || execute.isPending} onKeyDown={event => {
            if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
            event.preventDefault(); const to = index + (event.key === 'ArrowDown' ? 1 : -1);
            if (item.stages[to]) reorder(stage.id, item.stages[to].id, to > index);
          }}><GripVertical size={17}/></button><button className="ghost" aria-label={`删除卡片 ${stage.name}`} disabled={update.isPending || execute.isPending} onClick={() => { setStatus(''); update.mutate(item.stages.filter(candidate => candidate.id !== stage.id).map(candidate => ({ id: candidate.id }))); }}><Trash2 size={15}/></button></div>
        </header>
        <footer>{stage.current_execution_flow_run_id ? <button className="ghost" onClick={() => onOpenRun(stage.current_execution_flow_run_id!)}><ExternalLink size={13}/>查看执行记录</button> : <button className="ghost" disabled={stage.state !== 'ACTIVE' || execute.isPending} onClick={() => { setStatus(''); execute.mutate(stage.id); }}>{execute.isPending ? '启动中…' : '启动执行'}</button>}</footer>
      </article>
    </li>)}</ol>
    <div className="case-path-tail"><div className="scenario-path-rail"><span><Plus size={14}/></span></div>
      {adding ? <form className="scenario-add-card case-path-picker" onSubmit={event => { event.preventDefault(); if (selectedRecord && !update.isPending) { setStatus(''); update.mutate([...item.stages.map(stage => ({ id: stage.id })), { flow_run_id: selectedRecord.id }]); } }}>
        <header><span><b>添加运行记录</b><small>先选 FlowRun，再选择运行方式和具体记录</small></span><button className="ghost" type="button" aria-label="取消添加卡片" disabled={update.isPending} onClick={() => setAdding(false)}><X size={16}/></button></header>
        <div className="case-path-selection"><div className="case-field"><span><em>1</em>FlowRun</span><CaseChoice label="FlowRun" value={parentId} disabled={update.isPending || runs.isLoading} emptyText="暂无正在运行的 FlowRun" onChange={value => { setParentId(value); setMode(''); setRecordId(''); }} options={runningRuns.map(run => ({ value: run.id, label: run.name, detail: `Run #${run.run_no}` }))}/></div>
          <div className="case-field"><span><em>2</em>运行方式</span><CaseChoice label="运行方式" value={selectedParent ? mode : ''} disabled={!selectedParent || update.isPending || templates.isLoading} placeholder={selectedParent ? '选择运行方式' : '请先选择 FlowRun'} onChange={value => { setMode(value); setRecordId(''); }} options={[{ value: 'STEPWISE', label: '逐步运行' }, { value: 'CONTINUOUS', label: '连续运行' }].map(option => ({ ...option, detail: `${ownedTemplates.filter(record => record.execution_mode === option.value).length} 个可用记录` }))}/></div>
          <div className="case-field"><span><em>3</em>运行记录</span><CaseChoice label="运行记录" value={selectedParent ? recordId : ''} disabled={!selectedParent || !mode || update.isPending} placeholder={selectedParent && mode ? '选择运行记录' : '请先选择运行方式'} emptyText="暂无完整配置的可用记录" onChange={setRecordId} options={records.map(record => ({ value: record.id, label: record.name, detail: `${record.reachable_node_keys.length} 个节点` }))}/></div></div>
        {parentId && !selectedParent && !runs.isFetching && !runs.error && <p>所选 FlowRun 已停止运行，请重新选择。</p>}
        {(templates.isLoading || runs.isLoading) && <p>正在加载运行记录…</p>}
        {(templates.error || runs.error) && <p className="error" role="alert">{errorMessage(templates.error || runs.error)}</p>}
        {!templates.isLoading && !templates.error && selectedParent && mode && !records.length && <p>当前 FlowRun 下暂无可添加的{mode === 'STEPWISE' ? '逐步' : '连续'}运行记录，请先完成记录配置。</p>}
        <footer><button type="button" className="secondary" disabled={update.isPending} onClick={() => setAdding(false)}>取消</button><button className="primary" disabled={!selectedRecord || update.isPending}>{update.isPending ? '添加中…' : '添加'}</button></footer>
      </form> : <button className="scenario-add-path-card" disabled={update.isPending || item.stages.length >= 50} onClick={() => { setAdding(true); update.reset(); setStatus(''); }}><span className="case-add-icon"><Plus size={20}/></span><span><b>{item.stages.length ? '添加下一张卡片' : '添加第一张卡片'}</b><small>选择已有运行记录，串联 Case 路径</small></span></button>}
    </div>
    <p className="case-path-status" role="status">{update.error ? '' : status}</p>
    {update.error && <p className="error" role="alert">{errorMessage(update.error)}</p>}
  </section>;
}

function ScenarioWorkspace({ item, workspaceId, onUpdated, onDelete, onOpenConversation, onOpenRun }: { item: Case; workspaceId: string; onUpdated: (item: Case) => void; onDelete: () => void; onOpenConversation: (id: string) => void; onOpenRun: (id: string) => void }) {
  const [tab, setTab] = useState<Tab>('overview');
  const dialog = useProductDialog();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const start = useMutation({
    mutationFn: () => api.createCaseControlConversation(item.id, item.session_config as CaseSessionConfig),
    onSuccess: result => { onUpdated(result); if (result.control_conversation_binding_id) onOpenConversation(result.control_conversation_binding_id); },
  });
  const deleteCase = async () => {
    if (!await dialog.confirm({ title: `删除 Case“${item.name}”？`, message: '会永久删除 Case、其阶段控制元数据、协调会话及专属工作目录，无法恢复。关联 FlowRun 历史会保留。进行中的执行、运行中会话或未完成受控计划会阻止删除。', confirmLabel: '删除 Case', tone: 'danger' })) return;
    setDeleting(true); setError('');
    try { await api.deleteCase(item.id); onDelete(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setDeleting(false); }
  };
  return <main className={`scenario-workspace${tab === 'overview' ? ' case-basics-workspace' : ''}`}><div className="scenario-workspace-inner">
    <header className="scenario-casebar"><div><h1>{item.name}</h1></div><div className="scenario-case-actions"><button className="primary" disabled={start.isPending} onClick={() => {
      if (item.control_conversation_binding_id) onOpenConversation(item.control_conversation_binding_id);
      else if (!item.session_config?.model_provider_id) { setTab('conversation'); setError('请先保存会话配置。'); }
      else { setError(''); start.mutate(); }
    }}>{start.isPending ? '启动中…' : '启动'}</button></div></header>
    <nav aria-label="场景工作区">{([['overview', '基础配置'], ['path', 'Case 路径'], ['conversation', '会话配置']] as const).map(([value, label]) => <button key={value} className={tab === value ? 'active' : ''} onClick={() => { setTab(value); setError(''); }}>{label}</button>)}</nav>
    {(error || start.error) && <p className="error scenario-delete-error" role="alert">{error || errorMessage(start.error)}</p>}
    <div className="scenario-workspace-content"><div hidden={tab !== 'overview'}><BasicsForm item={item} workspaceId={workspaceId} onSaved={onUpdated}/></div>{tab === 'path' && <PathTab item={item} onUpdated={onUpdated} onOpenRun={onOpenRun} onDelete={() => void deleteCase()} deleting={deleting}/>} {tab === 'conversation' && <ConversationTab item={item} workspaceId={workspaceId} onUpdated={onUpdated}/>}</div>
  </div></main>;
}

export function CasesPage({ onNavigate }: { onNavigate: (path: string) => void }) {
  const qc = useQueryClient();
  const cases = useQuery({ queryKey: ['cases'], queryFn: api.cases });
  const workspace = useQuery({ queryKey: ['default-agent-workspace'], queryFn: () => api.defaultAgentWorkspace() });
  const [selectedId, setSelectedId] = useState<string>();
  const [creating, setCreating] = useState(false);
  const selected = useMemo(() => cases.data?.find(item => item.id === selectedId) ?? cases.data?.[0], [cases.data, selectedId]);
  const replace = (item: Case) => { qc.setQueryData<Case[]>(['cases'], current => [item, ...(current ?? []).filter(candidate => candidate.id !== item.id)]); setSelectedId(item.id); };
  const remove = (id: string) => { qc.setQueryData<Case[]>(['cases'], current => current?.filter(candidate => candidate.id !== id) ?? []); setSelectedId(current => current === id ? undefined : current); };
  return <main className="scenario-space"><aside><header className="scenario-side-head"><b>Case</b><button className="primary" aria-label="新建场景" onClick={() => setCreating(true)}><Plus size={14}/><span>新建</span></button></header>
    {cases.isLoading ? <p className="scenario-empty">正在加载场景…</p> : cases.error ? <p className="error">{cases.error.message}</p> : cases.data?.length ? <div className="scenario-tree">{cases.data.map(item => {
      const isSelected = !creating && selected?.id === item.id;
      const completed = item.stages.filter(stage => stage.state === 'COMPLETED').length;
      return <section className="scenario-case-wrap" key={item.id}><button className={`scenario-case-head ${isSelected ? 'active' : ''}`} onClick={() => { setSelectedId(item.id); setCreating(false); }}><i className={item.state.toLowerCase()}/><span><b>{item.name}</b><small>{CASE_STATE[item.state]} · {completed}/{item.stages.length} 阶段</small></span><ChevronRight size={15}/></button>{isSelected && item.control_conversation_binding_id && <button className="scenario-session" onClick={() => onNavigate(`/cases/${encodeURIComponent(item.id)}/conversations/${encodeURIComponent(item.control_conversation_binding_id!)}`)}><MessageSquareCode size={13}/><span><b>Case 助手</b><small>协调会话</small></span><em>打开</em></button>}</section>;
    })}</div> : <div className="scenario-empty"><ClipboardList size={21}/><b>暂无场景</b><span>创建一个场景来组织跨流程目标。</span></div>}</aside>
    {creating ? <main className="scenario-workspace case-basics-workspace"><div className="scenario-workspace-inner"><header className="scenario-casebar"><h1>新建 Case</h1></header><div className="scenario-workspace-content">{workspace.error && <p className="error">{errorMessage(workspace.error)}</p>}<BasicsForm key="new-case" workspaceId={workspace.data?.id ?? ''} onCancel={() => { setCreating(false); void cases.refetch(); }} onSaved={item => { replace(item); setCreating(false); }}/></div></div></main>
      : selected ? <ScenarioWorkspace key={selected.id} item={selected} workspaceId={workspace.data?.id ?? ''} onUpdated={replace} onDelete={() => remove(selected.id)} onOpenConversation={bindingId => onNavigate(`/cases/${encodeURIComponent(selected.id)}/conversations/${encodeURIComponent(bindingId)}`)} onOpenRun={id => onNavigate(`/flow-runs/${encodeURIComponent(id)}`)}/>
        : <section className="scenario-workspace scenario-workspace-empty"><ClipboardList size={28}/><h2>选择或创建一个场景</h2><p>场景工作区将组织阶段路径和协调会话。</p></section>}
  </main>;
}
