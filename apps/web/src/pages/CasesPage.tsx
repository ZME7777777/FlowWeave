import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, ChevronRight, ClipboardList, ExternalLink, FileText, Link2, MessageSquareCode, Plus, Settings2, Trash2, Upload, X } from 'lucide-react';
import { api, caseMaterialFileUrl } from '../api/client';
import type { Case, CaseBasics, CaseMaterial, CaseSessionConfig, CaseStage } from '../types';
import { CapabilityManager } from '../components/agent-session/AgentSessionWorkbench';
import { useEscapeClose } from '../components/useEscapeClose';
import { useProductDialog } from '../components/ProductDialogContext';

const CASE_STATE: Record<Case['state'], string> = { DRAFT: '草稿', ACTIVE: '进行中', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVED: '已归档' };
const STAGE_STATE: Record<CaseStage['state'], string> = { PENDING: '待处理', ACTIVE: '进行中', COMPLETED: '已完成', SKIPPED: '已跳过' };
type Tab = 'overview' | 'path' | 'conversation';
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '保存失败，请重试。';

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
  const [linkName, setLinkName] = useState('');
  const [linkUrl, setLinkUrl] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<CaseMaterial>();
  // Keep the created identity on a partial upload failure so retry never creates another Case.
  const saved = useRef(item);
  const fileInput = useRef<HTMLInputElement>(null);
  const addLink = () => {
    try { const url = new URL(linkUrl); if (!['https:', 'http:'].includes(url.protocol)) throw new Error(); }
    catch { setError('请输入有效的 HTTP 或 HTTPS 链接。'); return; }
    setMaterials(current => [...current, { kind: 'LINK', name: linkName.trim() || linkUrl, value: linkUrl }]);
    setLinkName(''); setLinkUrl(''); setLinkOpen(false); setError(''); setNotice('');
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
    <fieldset disabled={busy}>
      <label>名称<input autoFocus={!item} value={name} maxLength={220} required onChange={event => { setName(event.target.value); setNotice(''); }} placeholder="为这个 Case 起个名称"/></label>
      <label>说明<textarea value={description} maxLength={20_000} onChange={event => { setDescription(event.target.value); setNotice(''); }} placeholder="介绍当前场景的背景。"/></label>
      <label>目标<textarea value={objective} maxLength={20_000} onChange={event => { setObjective(event.target.value); setNotice(''); }} placeholder="说明需要达成的结果。"/></label>
      <label><span>工作区 <span className="scenario-optional">可选</span></span><select aria-label="工作区" value={directoryId} disabled={Boolean(item?.control_conversation_binding_id)} onChange={event => { setDirectoryId(event.target.value); setNotice(''); }}>
        <option value="">默认工作区（自动创建 Case 专属工作区）</option>
        {directoryId && !directories.data?.items.some(directory => directory.id === directoryId) && <option value={directoryId}>当前工作区（暂不可用）</option>}
        {directories.data?.items.filter(directory => directory.id !== item?.control_work_directory_id).map(directory => <option key={directory.id} value={directory.id}>{directory.display_name}</option>)}
      </select><small>{item?.control_conversation_binding_id ? '会话已创建，工作区已固定。' : '可选择自由对话中的已有工作区；留空将自动创建专属工作区。'}</small></label>
      {directories.error && <p className="error">{errorMessage(directories.error)}</p>}
      <section className="scenario-materials" aria-label="资料"><header><span><b>资料</b><small>添加完成当前场景所需的基础资料。</small></span><div><button className="secondary" type="button" onClick={() => setLinkOpen(current => !current)}><Link2 size={14}/>添加链接</button><button className="secondary" type="button" onClick={() => fileInput.current?.click()}><Upload size={14}/>上传文件</button></div></header>
        <input hidden ref={fileInput} type="file" multiple aria-label="上传资料文件" onChange={event => { const selectedFiles = Array.from(event.target.files ?? []); setFiles(current => [...current, ...selectedFiles]); event.target.value = ''; setNotice(''); }}/>
        {linkOpen && <div className="scenario-link-editor"><label>链接名称<input value={linkName} maxLength={220} onChange={event => setLinkName(event.target.value)} placeholder="选填"/></label><label>链接地址<input value={linkUrl} type="url" onChange={event => setLinkUrl(event.target.value)} placeholder="https://"/></label><button className="secondary" type="button" disabled={!linkUrl.trim()} onClick={addLink}>添加</button></div>}
        {!materials.length && !files.length && <p className="scenario-materials-empty">尚未添加资料</p>}
        <ul>{materials.map((material, index) => <li key={`${index}-${material.value}`}>{material.kind === 'LINK' ? <Link2 size={15}/> : <FileText size={15}/>}{material.kind === 'LINK' ? <a href={material.value} target="_blank" rel="noreferrer">{material.name}</a> : <button className="ghost" type="button" onClick={() => setPreview(material)}>{material.name}</button>}<button className="ghost" type="button" aria-label={`移除资料 ${material.name}`} onClick={() => { setMaterials(current => current.filter((_, i) => i !== index)); setNotice(''); }}><X size={14}/></button></li>)}
          {files.map((file, index) => <li key={`${index}-${file.name}`}><FileText size={15}/><span>{file.name}<small>保存时上传</small></span><button className="ghost" type="button" aria-label={`移除文件 ${file.name}`} onClick={() => setFiles(current => current.filter((_, i) => i !== index))}><X size={14}/></button></li>)}</ul>
      </section>
      <footer className="scenario-form-actions">{onCancel && <button className="secondary" type="button" onClick={onCancel}>取消</button>}<button className="primary" disabled={!name.trim() || !workspaceId}>{busy ? '保存中…' : '保存'}</button><span role="status">{notice}</span></footer>
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
    {preview && <MaterialPreview material={preview} caseId={saved.current?.id ?? item?.id ?? ''} onClose={() => setPreview(undefined)}/>}
  </form>;
}

function ConversationTab({ item, workspaceId, onUpdated }: { item: Case; workspaceId: string; onUpdated: (item: Case) => void }) {
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const [providerId, setProviderId] = useState(item.session_config?.model_provider_id ?? '');
  const [modelName, setModelName] = useState(item.session_config?.model_name ?? '');
  const [effort, setEffort] = useState(item.session_config?.reasoning_effort ?? '');
  const [capabilities, setCapabilities] = useState(item.session_config?.capability_version_ids ?? []);
  const [credentials, setCredentials] = useState(item.session_config?.credential_ids ?? []);
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
    <div className="scenario-config-grid"><label>模型供应商<select aria-label="模型供应商" value={providerId} onChange={event => { setProviderId(event.target.value); setModelName(''); setEffort(''); setNotice(''); }}><option value="">选择模型服务</option>{availableProviders.map(candidate => <option value={candidate.id} key={candidate.id}>{candidate.name}</option>)}</select></label>
      <label>模型名称<select aria-label="模型名称" value={modelName} onChange={event => { setModelName(event.target.value); setEffort(''); setNotice(''); }}><option value="">选择模型</option>{models.map(candidate => <option value={candidate.model_name} key={candidate.model_name}>{candidate.model_name}</option>)}</select></label>
      <label>思考程度<select aria-label="思考程度" value={effort} onChange={event => { setEffort(event.target.value); setNotice(''); }}><option value="">使用模型默认值</option>{model?.supported_reasoning_efforts?.map(value => <option value={value} key={value}>{value}</option>)}</select></label></div>
    <section className="scenario-config-box"><span><b>能力配置</b><small>选择会话加载的能力与认证 · 已选 {capabilities.length + credentials.length} 项</small></span><button className="secondary" type="button" disabled={!workspaceId} onClick={() => setConfigOpen(true)}><Settings2 size={14}/>配置会话</button></section>
    {providers.error && <p className="error">{errorMessage(providers.error)}</p>}{save.error && <p className="error" role="alert">{errorMessage(save.error)}</p>}
    <footer className="scenario-form-actions"><button className="primary" disabled={!model || save.isPending} onClick={() => save.mutate()}>{save.isPending ? '保存中…' : '保存配置'}</button><span role="status">{notice}</span></footer>
    {configOpen && <CapabilityManager configurationPreset workspaceId={workspaceId} draftCapabilityIds={capabilities} draftCredentialIds={credentials} onClose={() => setConfigOpen(false)} onCreateEnhancedConversation={ids => { setCapabilities(ids); setNotice(''); }} onDraftCredentialIdsChange={ids => { setCredentials(ids); setNotice(''); }}/>}
  </section>;
}

function PathTab({ item, onUpdated, onOpenRun }: { item: Case; onUpdated: (item: Case) => void; onOpenRun: (id: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [flowRunId, setFlowRunId] = useState('');
  const templates = useQuery({ queryKey: ['case-stage-templates', item.id], queryFn: () => api.caseStageTemplates(item.id), enabled: adding });
  const update = useMutation({
    mutationFn: (cards: Array<{ id: string } | { flow_run_id: string }>) => api.saveCasePath(item.id, item.row_version, cards),
    onSuccess: result => { onUpdated(result); setAdding(false); setFlowRunId(''); void templates.refetch(); },
  });
  const move = (from: number, to: number) => {
    const cards = item.stages.map(stage => ({ id: stage.id }));
    [cards[from], cards[to]] = [cards[to], cards[from]];
    update.mutate(cards);
  };
  return <section className={`scenario-path scenario-path-editor${!item.stages.length ? ' is-empty' : ''}`}>
    <ol>{item.stages.map((stage, index) => <li key={stage.id} className={stage.state.toLowerCase()}><div className="scenario-path-rail"><span>{index + 1}</span>{index < item.stages.length - 1 && <i/>}</div><article><header><div><span>{STAGE_STATE[stage.state]} · {stage.execution_mode === 'STEPWISE' ? '逐步运行' : '连续运行'}</span><h3>{stage.name}</h3></div><div className="scenario-card-actions"><button className="ghost" aria-label={`上移 ${stage.name}`} disabled={index === 0 || update.isPending} onClick={() => move(index, index - 1)}><ArrowUp size={15}/></button><button className="ghost" aria-label={`下移 ${stage.name}`} disabled={index === item.stages.length - 1 || update.isPending} onClick={() => move(index, index + 1)}><ArrowDown size={15}/></button><button className="ghost" aria-label={`删除卡片 ${stage.name}`} disabled={update.isPending} onClick={() => update.mutate(item.stages.filter(candidate => candidate.id !== stage.id).map(candidate => ({ id: candidate.id })))}><Trash2 size={15}/></button></div></header><footer>{stage.flow_run_id && <button className="ghost" onClick={() => onOpenRun(stage.flow_run_id!)}><ExternalLink size={13}/>查看运行记录</button>}{stage.current_execution_flow_run_id && <button className="ghost" onClick={() => onOpenRun(stage.current_execution_flow_run_id!)}><ExternalLink size={13}/>当前执行</button>}</footer></article></li>)}</ol>
    {adding ? <form className="scenario-add-card" onSubmit={event => { event.preventDefault(); if (flowRunId) update.mutate([...item.stages.map(stage => ({ id: stage.id })), { flow_run_id: flowRunId }]); }}><label>流程运行记录<select aria-label="流程运行记录" autoFocus required value={flowRunId} onChange={event => setFlowRunId(event.target.value)}><option value="">选择已有流程运行记录</option>{templates.data?.map(template => <option key={template.id} value={template.id}>{template.name} · {template.execution_mode === 'STEPWISE' ? '逐步运行' : '连续运行'}</option>)}</select></label>{templates.isLoading && <p>正在加载…</p>}{templates.error && <p className="error">{errorMessage(templates.error)}</p>}{!templates.isLoading && !templates.error && !templates.data?.length && <p>暂无可添加的运行记录，请先在流程运行中配置完整的逐步或连续运行记录。</p>}<footer><button type="button" className="secondary" disabled={update.isPending} onClick={() => setAdding(false)}>取消</button><button className="primary" disabled={!flowRunId || update.isPending}>{update.isPending ? '添加中…' : '添加'}</button></footer></form>
      : <button className="scenario-add-path-card" disabled={update.isPending || item.stages.length >= 50} onClick={() => setAdding(true)}><Plus size={24}/><b>添加卡片</b><span>选择一个已有流程运行记录</span></button>}
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
  const directories = useQuery({ queryKey: ['agent-work-directories', workspaceId], queryFn: () => api.agentWorkDirectories(workspaceId), enabled: Boolean(workspaceId) });
  const workspaceName = directories.data?.items.find(directory => directory.id === item.work_directory_id)?.display_name || 'Case 专属工作区';
  return <main className="scenario-workspace"><div className="scenario-workspace-inner">
    <header className="scenario-casebar"><div><h1>{item.name}</h1><small>工作区：{workspaceName}</small></div><div className="scenario-case-actions"><button className="primary" disabled={start.isPending} onClick={() => {
      if (item.control_conversation_binding_id) onOpenConversation(item.control_conversation_binding_id);
      else if (!item.session_config?.model_provider_id) { setTab('conversation'); setError('请先保存会话配置。'); }
      else { setError(''); start.mutate(); }
    }}>{start.isPending ? '启动中…' : '启动'}</button><button className="danger" disabled={deleting} onClick={() => void deleteCase()}><Trash2 size={14}/>{deleting ? '删除中…' : '删除'}</button></div></header>
    <nav aria-label="场景工作区">{([['overview', '基础配置'], ['path', 'Case 路径'], ['conversation', '会话配置']] as const).map(([value, label]) => <button key={value} className={tab === value ? 'active' : ''} onClick={() => { setTab(value); setError(''); }}>{label}</button>)}</nav>
    {(error || start.error) && <p className="error scenario-delete-error" role="alert">{error || errorMessage(start.error)}</p>}
    <div className="scenario-workspace-content"><div hidden={tab !== 'overview'}><BasicsForm item={item} workspaceId={workspaceId} onSaved={onUpdated}/></div>{tab === 'path' && <PathTab item={item} onUpdated={onUpdated} onOpenRun={onOpenRun}/>} {tab === 'conversation' && <ConversationTab item={item} workspaceId={workspaceId} onUpdated={onUpdated}/>}</div>
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
    {creating ? <main className="scenario-workspace"><div className="scenario-workspace-inner"><header className="scenario-casebar"><h1>新建 Case</h1></header><div className="scenario-workspace-content">{workspace.error && <p className="error">{errorMessage(workspace.error)}</p>}<BasicsForm key="new-case" workspaceId={workspace.data?.id ?? ''} onCancel={() => { setCreating(false); void cases.refetch(); }} onSaved={item => { replace(item); setCreating(false); }}/></div></div></main>
      : selected ? <ScenarioWorkspace key={selected.id} item={selected} workspaceId={workspace.data?.id ?? ''} onUpdated={replace} onDelete={() => remove(selected.id)} onOpenConversation={bindingId => onNavigate(`/cases/${encodeURIComponent(selected.id)}/conversations/${encodeURIComponent(bindingId)}`)} onOpenRun={id => onNavigate(`/flow-runs/${encodeURIComponent(id)}`)}/>
        : <section className="scenario-workspace scenario-workspace-empty"><ClipboardList size={28}/><h2>选择或创建一个场景</h2><p>场景工作区将组织阶段路径和协调会话。</p></section>}
  </main>;
}
