import { CheckCircle2, CheckSquare, ExternalLink, KeyRound, Link2, LoaderCircle, LogIn, LogOut, Pencil, Plus, Server, ShieldCheck, ShieldX, Trash2 } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormEvent, useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import { Pagination } from '../components/Pagination';
import type { CodexDeviceAuthorization, CodexOAuthStatus, ModelProvider, ModelProviderWrite, ProviderModel } from '../types';

const blank = (): ModelProviderWrite => ({ name: '', auth_type: 'API_KEY', base_url: '', api_key: '', models: [{ model_name: '', enabled: true, is_default: true }] });
const CONNECTION_STATE_LABELS: Record<string, string> = { UNTESTED: '未测试', AUTHORIZING: '等待登录', CONNECTED: '已连接', FAILED: '连接失败' };
type TestFeedback = { state: 'testing' | 'success' | 'error'; message: string };
function ProviderEditor({ provider, onClose }: { provider?: ModelProvider; onClose: () => void }) {
  useEscapeClose(onClose);
  const qc = useQueryClient();
  const [form, setForm] = useState<ModelProviderWrite>(blank());
  const [discovered, setDiscovered] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const applyDiscovery = useCallback((result: { models: string[]; provider?: ModelProvider }) => {
    setDiscovered(result.models);
    if (result.provider) {
      const synchronized = result.provider;
      setForm(old => ({
        ...old,
        row_version: synchronized.row_version,
        models: synchronized.models.map(({ model_name, enabled, is_default }) => ({ model_name, enabled, is_default })),
      }));
      qc.setQueryData<ModelProvider[]>(['providers'], current => current?.map(item => item.id === synchronized.id ? synchronized : item));
      return;
    }
    setForm(old => {
      const models = old.models.filter(item => item.model_name.trim());
      return { ...old, models };
    });
  }, [qc]);
  useEffect(() => {
    setForm(provider ? { name: provider.name, auth_type: provider.auth_type, base_url: provider.base_url, api_key: '', row_version: provider.row_version, models: provider.models.map(({ model_name, enabled, is_default }) => ({ model_name, enabled, is_default })) } : blank());
    setDiscovered([]); setError('');
  }, [applyDiscovery, provider]);
  useEffect(() => {
    if (!provider || provider.auth_type !== 'CODEX_OAUTH' || !provider.oauth_connected) return;
    let active = true;
    setBusy(true);
    void api.discoverProviderModels(provider.id).then(result => {
      if (!active) return;
      applyDiscovery(result);
    }).catch(reason => {
      if (active) setError(reason instanceof Error ? reason.message : '自动拉取 Codex 模型失败');
    }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [applyDiscovery, provider]);
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const saved = provider ? await api.updateProvider(provider.id, form) : await api.createProvider(form);
      qc.setQueryData<ModelProvider[]>(['providers'], current => {
        const providers = current ?? [];
        const exists = providers.some(item => item.id === saved.id);
        return exists ? providers.map(item => item.id === saved.id ? saved : item) : [saved, ...providers];
      });
      void qc.invalidateQueries({ queryKey: ['providers'] });
      onClose();
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { setBusy(false); }
  };
  const discover = async () => {
    setBusy(true); setError('');
    try {
      const result = form.auth_type === 'API_KEY'
        ? await api.previewProviderModels({ base_url: form.base_url, api_key: form.api_key, provider_id: provider?.id })
        : await api.discoverProviderModels(provider!.id);
      applyDiscovery(result);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : '发现模型失败'); }
    finally { setBusy(false); }
  };
  const toggle = (name: string) => setForm(old => {
    const exists = old.models.find(item => item.model_name === name);
    if (!exists) {
      const hasDefault = old.models.some(item => item.enabled && item.is_default);
      return { ...old, models: [...old.models, { model_name: name, enabled: true, is_default: !hasDefault }] };
    }
    const models = old.models.filter(item => item.model_name !== name);
    if (exists.is_default && !models.some(item => item.enabled && item.is_default)) {
      const firstEnabled = models.findIndex(item => item.enabled);
      if (firstEnabled >= 0) models[firstEnabled] = { ...models[firstEnabled], is_default: true };
    }
    return { ...old, models };
  });
  const updateModel = (index: number, patch: Partial<ProviderModel>) => setForm(old => ({
    ...old,
    models: old.models.map((item, itemIndex) => {
      if (itemIndex === index) {
        const next = { ...item, ...patch };
        if (patch.enabled === false && item.is_default) next.is_default = false;
        if (patch.is_default) next.enabled = true;
        return next;
      }
      return patch.is_default ? { ...item, is_default: false } : item;
    }),
  }));
  const removeModel = (index: number) => setForm(old => {
    const models = old.models.filter((_, itemIndex) => itemIndex !== index);
    if (models.length && !models.some(item => item.is_default && item.enabled)) {
      const firstEnabled = models.findIndex(item => item.enabled);
      if (firstEnabled >= 0) models[firstEnabled] = { ...models[firstEnabled], is_default: true };
    }
    return { ...old, models };
  });
  const changeAuthType = (auth_type: ModelProviderWrite['auth_type']) => setForm(old => ({
    ...old,
    auth_type,
    base_url: auth_type === 'CODEX_OAUTH' ? '' : old.base_url,
    api_key: '',
    models: auth_type === 'CODEX_OAUTH' ? [] : old.models.length ? old.models : [{ model_name: '', enabled: true, is_default: true }],
  }));
  return <div className="modal-backdrop"><form className="modal model-editor" onSubmit={save}><header><div><span className="eyebrow">MODEL PROVIDER</span><h2>{provider ? '编辑模型服务' : '新增模型服务'}</h2><p>API Key 与 Codex OAuth 凭据均加密保存且永不返回浏览器。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <div className="form-grid"><label>服务名称<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}/></label><label>认证方式<select value={form.auth_type} onChange={e => changeAuthType(e.target.value as ModelProviderWrite['auth_type'])}><option value="API_KEY">Codex / OpenAI 兼容</option><option value="CODEX_OAUTH">Codex OAuth（ChatGPT 订阅）</option></select></label>{form.auth_type === 'API_KEY' ? <><label>Base URL<input required value={form.base_url} placeholder="https://api.example.com/v1" onChange={e => setForm({ ...form, base_url: e.target.value })}/></label><label>API Key<input type="password" value={form.api_key ?? ''} placeholder={provider?.has_api_key ? `留空保留现有密钥 ${provider.api_key_hint ?? ''}` : '输入 API Key'} onChange={e => setForm({ ...form, api_key: e.target.value })}/></label></> : <p className="startpoint wide">保存后在服务卡片点击“登录 Codex”，使用设备码连接 ChatGPT 订阅。OAuth 服务仅用于 Agent 节点，不用于 Prompt Gate。</p>}</div>
    <div className="model-discovery-head"><div><b>可用模型</b><small>{form.auth_type === 'API_KEY' ? '填写连接信息后拉取模型，再选择启用项和默认模型' : provider?.oauth_connected ? '已按当前登录账号自动拉取；也可手动刷新' : '登录 Codex 后可按账号自动拉取模型'}</small></div>{(form.auth_type === 'API_KEY' || (provider && provider.oauth_connected)) && <button type="button" className="secondary" disabled={busy || (form.auth_type === 'API_KEY' && !form.base_url.trim())} onClick={() => void discover()}>{busy ? '拉取中…' : form.auth_type === 'CODEX_OAUTH' ? '刷新模型' : '拉取模型'}</button>}</div>
    {form.auth_type === 'API_KEY' && discovered.length > 0 && <div className="model-tags discovery-tags">{discovered.map(name => { const selected = form.models.some(item => item.model_name === name); return <button type="button" key={name} className={selected ? 'selected' : ''} aria-pressed={selected} onClick={() => toggle(name)}>{name}</button>; })}</div>}
    <div className={`provider-model-list ${form.auth_type === 'CODEX_OAUTH' ? 'oauth-model-list' : ''}`}>{form.models.map((model, index) => <div className="provider-model-row" key={model.model_name || index}>{form.auth_type === 'CODEX_OAUTH' ? <span className="synced-model-name"><b>{model.model_name}</b><small>由当前 Codex 账号同步{model.supported_reasoning_efforts?.length ? ` · 支持 ${model.supported_reasoning_efforts.join(' / ')}` : ''}</small></span> : <input aria-label={`模型 ${index + 1}`} required value={model.model_name} placeholder="模型标识" onChange={e => updateModel(index, { model_name: e.target.value })}/>}<label><input type="checkbox" checked={model.enabled} onChange={e => updateModel(index, { enabled: e.target.checked })}/>启用</label><label><input type="radio" name="default-model" checked={model.is_default} onChange={() => updateModel(index, { is_default: true, enabled: true })}/>默认</label>{form.auth_type === 'API_KEY' && <button type="button" className="danger model-remove-button" aria-label={`移除模型 ${model.model_name}`} onClick={() => removeModel(index)}><Trash2 size={16}/>删除</button>}</div>)}</div>
    {form.auth_type === 'API_KEY' && <button type="button" className="ghost" onClick={() => setForm(old => ({ ...old, models: [...old.models, { model_name: '', enabled: true, is_default: old.models.length === 0 }] }))}><Plus size={13}/>手动添加模型</button>}
    {form.auth_type === 'CODEX_OAUTH' && provider?.oauth_connected && !busy && !form.models.length && <p className="model-discovery-empty">当前账号没有返回可用模型，请点击“刷新模型”重试。</p>}
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || (form.auth_type === 'API_KEY' && !form.models.length)}>{busy ? '处理中…' : '保存模型服务'}</button></footer>
  </form></div>;
}
function CodexLoginDialog({ provider, authorization, onClose, onConnected }: { provider: ModelProvider; authorization: CodexDeviceAuthorization; onClose: () => void; onConnected: (status: CodexOAuthStatus) => void }) {
  useEscapeClose(onClose);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');
  const check = async () => {
    setChecking(true); setError('');
    try {
      const status = await api.pollCodexOAuth(provider.id);
      if (status.connected) onConnected(status);
      else setError('尚未完成授权。请在 Codex 页面输入设备码并确认后重试。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '检查授权状态失败'); }
    finally { setChecking(false); }
  };
  return <div className="modal-backdrop"><div className="modal"><header><div><span className="eyebrow">CODEX OAUTH</span><h2>登录 {provider.name}</h2><p>设备码是敏感的一次性凭据，请勿分享给他人。</p></div><button className="ghost" onClick={onClose}>关闭</button></header><div className="form-grid"><div className="wide"><b>1. 打开 Codex 登录页面</b><p><a className="secondary" href={authorization.verification_url} target="_blank" rel="noreferrer"><ExternalLink size={14}/> 打开登录页面</a></p></div><div className="wide"><b>2. 输入设备码</b><p><code>{authorization.user_code}</code></p><small>有效期至 {new Date(authorization.expires_at).toLocaleString()}</small></div>{error && <p className="error wide">{error}</p>}</div><footer><button className="ghost" onClick={onClose}>稍后完成</button><button className="primary" disabled={checking} onClick={() => void check()}>{checking ? <LoaderCircle className="spin" size={14}/> : <CheckCircle2 size={14}/>}我已完成授权</button></footer></div></div>;
}
export function ModelsPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const { data: providers = [], isLoading } = useQuery({ queryKey: ['providers'], queryFn: api.providers });
  const [editing, setEditing] = useState<ModelProvider | null | undefined>();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [testFeedback, setTestFeedback] = useState<Record<string, TestFeedback>>({});
  const [codexLogin, setCodexLogin] = useState<{ provider: ModelProvider; authorization: CodexDeviceAuthorization }>();
  const [page, setPage] = useState(1);
  const allSelected = providers.length > 0 && providers.every(provider => selectedIds.has(provider.id));
  const pageSize = 10;
  const pagedProviders = providers.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => { if (page > Math.max(1, Math.ceil(providers.length / pageSize))) setPage(1); }, [page, providers.length]);
  const test = useMutation({
    mutationFn: api.testProvider,
    onMutate: providerId => {
      setTestFeedback(old => ({ ...old, [providerId]: { state: 'testing', message: '正在请求服务的模型列表…' } }));
    },
    onSuccess: (result, providerId) => {
      setTestFeedback(old => ({ ...old, [providerId]: { state: 'success', message: `连接成功，服务返回 ${result.model_count} 个模型。` } }));
    },
    onError: (reason, providerId) => {
      const message = reason instanceof Error ? reason.message : '连接请求失败';
      setTestFeedback(old => ({ ...old, [providerId]: { state: 'error', message: `连接失败：${message}` } }));
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['providers'] }),
  });
  const toggle = (id: string) => setSelectedIds(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleAll = () => setSelectedIds(allSelected ? new Set() : new Set(providers.map(provider => provider.id)));
  const startCodexLogin = async (provider: ModelProvider) => {
    setError('');
    try {
      const authorization = await api.startCodexOAuth(provider.id);
      setCodexLogin({ provider, authorization });
      window.open(authorization.verification_url, '_blank', 'noopener,noreferrer');
      await qc.invalidateQueries({ queryKey: ['providers'] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法启动 Codex 登录'); }
  };
  const disconnectCodex = async (provider: ModelProvider) => {
    if (!await dialog.confirm({ title: `断开“${provider.name}”？`, message: '加密保存的 Codex OAuth token 将被永久删除，节点将无法继续使用该服务。', confirmLabel: '断开连接', tone: 'danger' })) return;
    try { await api.disconnectCodexOAuth(provider.id); await qc.invalidateQueries({ queryKey: ['providers'] }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '断开失败'); }
  };
  const removeMany = async (ids: string[], label: string) => {
    if (!ids.length) return;
    if (!await dialog.confirm({ title: `删除${label}？`, message: '模型服务、密钥和模型配置将被永久删除。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setDeleting(true); setError(''); setNotice('');
    try {
      const result = await api.deleteProviders(ids);
      setSelectedIds(new Set(result.blocked.map(item => item.id)));
      if (result.deleted_ids.length) setNotice(`已删除 ${result.deleted_ids.length} 个模型服务。`);
      if (result.blocked.length) setError(`以下模型服务仍有关联，已跳过：${result.blocked.map(item => `“${item.name}”绑定节点 ${item.nodes.map(node => `“${node.name}”`).join('、')}`).join('；')}。`);
      await qc.invalidateQueries({ queryKey: ['providers'] });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };
  return <section className="page models-page"><div className="model-config-tools model-config-fixed-tools"><span>共 {providers.length} 个模型服务</span><div className="bulk-actions"><button className="secondary" disabled={!providers.length || deleting} onClick={toggleAll}><CheckSquare size={14}/>{allSelected ? '取消全选' : '全选'}</button><button className="danger" disabled={!selectedIds.size || deleting} onClick={() => void removeMany([...selectedIds], `选中的 ${selectedIds.size} 个模型服务`)}><Trash2 size={14}/>{deleting ? '删除中…' : `批量删除 (${selectedIds.size})`}</button><button className="primary" onClick={() => setEditing(null)}><Plus size={16}/>新增模型服务</button></div></div>
    <div className="models-page-scroll">
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    {isLoading ? <div className="empty">加载中…</div> : <><div className="model-config-grid compact-model-list">{pagedProviders.map(provider => { const feedback = testFeedback[provider.id]; const testing = feedback?.state === 'testing'; const oauth = provider.auth_type === 'CODEX_OAUTH'; return <article className={`model-config-card ${selectedIds.has(provider.id) ? 'selected' : ''}`} key={provider.id}><header><label className="model-provider-select resource-check"><input type="checkbox" aria-label={`选择模型服务 ${provider.name}`} checked={selectedIds.has(provider.id)} onChange={() => toggle(provider.id)}/><span className="model-server-icon"><Server size={18}/></span></label><div><h3>{provider.name}</h3><small>{oauth ? 'Codex OAuth · ChatGPT 订阅' : provider.base_url}</small></div><div className="card-actions model-card-actions"><button title="编辑" aria-label={`编辑模型服务 ${provider.name}`} onClick={() => setEditing(provider)}><Pencil size={16}/></button><button className="danger card-delete-button" title="删除" aria-label={`删除模型服务 ${provider.name}`} onClick={() => void removeMany([provider.id], `模型服务“${provider.name}”`)}><Trash2 size={16}/>删除</button></div></header><div className="model-provider-state"><span className={provider.available_for_nodes ? 'available' : 'unavailable'}>{provider.available_for_nodes ? <ShieldCheck size={13}/> : <ShieldX size={13}/>} {provider.available_for_nodes ? '可用于节点' : oauth ? (provider.oauth_connected ? '暂无可用默认模型' : '需要登录 Codex') : '暂无可用默认模型'}</span><span><Link2 size={12}/>{provider.reference_node_count} 个引用节点</span></div><div className="model-provider-meta"><span>连接状态</span><b className={provider.connection_state === 'CONNECTED' ? 'good' : provider.connection_state === 'FAILED' ? 'bad' : ''}>{CONNECTION_STATE_LABELS[provider.connection_state] ?? provider.connection_state}</b><span>{oauth ? 'Codex 账号' : 'API Key'}</span><b><KeyRound size={12}/>{oauth ? (provider.oauth_connected ? provider.oauth_account_email ?? '已登录' : '未登录') : (provider.has_api_key ? `已配置 ${provider.api_key_hint ?? ''}` : '未配置')}</b><span>默认模型</span><b>{provider.models.find(item => item.is_default && item.enabled)?.model_name ?? '未设置'}</b><span>启用模型</span><b>{provider.models.filter(item => item.enabled).length} 个</b></div><div className="model-tags">{provider.models.filter(item => item.enabled).map(item => <span key={item.id}>{item.model_name}</span>)}</div>{oauth && !provider.oauth_connected ? <button className="secondary full" onClick={() => void startCodexLogin(provider)}><LogIn size={14}/> {provider.oauth_device_pending ? '重新开始登录' : '登录 Codex'}</button> : <button className="secondary full" disabled={deleting || test.isPending} onClick={() => test.mutate(provider.id)}>{testing ? <LoaderCircle className="spin" size={14}/> : <CheckCircle2 size={14}/>} {testing ? '测试中…' : '测试连接'}</button>}{oauth && provider.oauth_connected && <button className="ghost full" onClick={() => void disconnectCodex(provider)}><LogOut size={14}/>断开 Codex</button>}{feedback && <p className={`model-test-result ${feedback.state}`} role={feedback.state === 'error' ? 'alert' : 'status'}>{feedback.message}</p>}</article>; })}</div><Pagination page={page} pageSize={pageSize} total={providers.length} onPageChange={setPage}/></>}
    {!isLoading && !providers.length && <div className="empty"><Server size={28}/><b>还没有模型服务</b></div>}
    </div>
    {editing !== undefined && <ProviderEditor provider={editing ?? undefined} onClose={() => setEditing(undefined)}/>}
    {codexLogin && <CodexLoginDialog provider={codexLogin.provider} authorization={codexLogin.authorization} onClose={() => setCodexLogin(undefined)} onConnected={status => { setCodexLogin(undefined); if (status.model_sync_error) setError(`Codex 登录成功，但模型同步失败：${status.model_sync_error}`); else setNotice(`Codex 登录成功，已自动同步 ${status.model_count ?? 0} 个模型。`); void qc.invalidateQueries({ queryKey: ['providers'] }); }}/>}
  </section>;
}
