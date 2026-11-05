import { CheckCircle2, CheckSquare, ChevronDown, ExternalLink, KeyRound, Link2, LoaderCircle, LogIn, LogOut, Pencil, Plus, Server, ShieldCheck, ShieldX, Trash2 } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormEvent, KeyboardEvent, useCallback, useEffect, useId, useRef, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import { SettingsManagementLayout, SettingsResourceRow } from '../components/SettingsManagementLayout';
import { Pagination } from '../components/Pagination';
import type { CodexDeviceAuthorization, CodexOAuthStatus, ModelProtocol, ModelProvider, ModelProviderUsage, ModelProviderWrite, ProviderModel, ProviderUsageQuery } from '../types';
import { Toast } from '../components/Toast';

const blank = (): ModelProviderWrite => ({ name: '', auth_type: 'API_KEY', base_url: '', api_key: '', models: [{ model_name: '', enabled: true, is_default: true }] });
const CONNECTION_STATE_LABELS: Record<string, string> = { UNTESTED: '未测试', AUTHORIZING: '等待登录', CONNECTED: '已连接', FAILED: '连接失败' };
type TestFeedback = { state: 'testing' | 'success' | 'error'; message: string };
type ProviderAuthType = ModelProviderWrite['auth_type'];
const providerAuthOptions: ReadonlyArray<{ value: ProviderAuthType; label: string }> = [
  { value: 'API_KEY', label: 'API Key（自动适配 GPT / Claude 等协议）' },
  { value: 'CODEX_OAUTH', label: 'Codex OAuth（ChatGPT 订阅）' },
];
const isApiKeyProvider = (authType: ProviderAuthType) => authType !== 'CODEX_OAUTH';
const budgetNumber = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 4 });

function ProviderAuthSelect({ value, onChange }: { value: ProviderAuthType; onChange: (value: ProviderAuthType) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();
  const selected = providerAuthOptions.find(option => option.value === value) ?? providerAuthOptions[0];

  useEffect(() => {
    const closeIfOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', closeIfOutside);
    return () => document.removeEventListener('mousedown', closeIfOutside);
  }, []);

  const select = (next: ProviderAuthType) => {
    onChange(next);
    setOpen(false);
  };
  const move = (event: KeyboardEvent<HTMLButtonElement>, offset: number) => {
    event.preventDefault();
    const current = providerAuthOptions.findIndex(option => option.value === value);
    const next = (current + offset + providerAuthOptions.length) % providerAuthOptions.length;
    select(providerAuthOptions[next].value);
  };

  return <div className="credential-auth-select" ref={root}>
    <button type="button" className="credential-auth-trigger" aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} onClick={() => setOpen(current => !current)} onKeyDown={event => {
      if (event.key === 'ArrowDown') move(event, 1);
      else if (event.key === 'ArrowUp') move(event, -1);
      else if (event.key === 'Escape') setOpen(false);
    }}><span>{selected.label}</span><ChevronDown size={16} aria-hidden="true"/></button>
    {open && <div id={listId} className="credential-auth-options" role="listbox" aria-label="认证方式">{providerAuthOptions.map(option => <button key={option.value} type="button" role="option" aria-selected={option.value === value} onClick={() => select(option.value)}>{option.label}</button>)}</div>}
  </div>;
}
const PROTOCOL_LABELS: Record<ModelProtocol, string> = { CHAT_COMPLETIONS: 'Chat Completions', RESPONSES: 'Responses', ANTHROPIC: 'Anthropic Messages' };
const protocolSummary = (protocols: ModelProtocol[]) => protocols.length ? protocols.map(protocol => PROTOCOL_LABELS[protocol]).join(' · ') : '—';
// Mirrors the server's name-based inference so the editor can preview it before saving.
const inferProtocol = (name: string): ModelProtocol => /(^|[/:._-])claude/i.test(name) ? 'ANTHROPIC' : /(^|[/:._-])(gpt-?[5-9]|gpt-?\d{2,}|codex|o[1-9](?![a-z]))/i.test(name) ? 'RESPONSES' : 'CHAT_COMPLETIONS';
const effectiveProtocol = (model: { model_name: string; protocol?: ModelProtocol | null }) => model.protocol ?? inferProtocol(model.model_name);
const protocolChoices: ReadonlyArray<ModelProtocol | null> = [null, 'CHAT_COMPLETIONS', 'RESPONSES', 'ANTHROPIC'];

function ModelProtocolSelect({ model, onChange }: { model: { model_name: string; protocol?: ModelProtocol | null }; onChange: (value: ModelProtocol | null) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();
  const label = (value: ModelProtocol | null) => value ? PROTOCOL_LABELS[value] : `自动 · ${PROTOCOL_LABELS[inferProtocol(model.model_name)]}`;

  useEffect(() => {
    if (!open) return;
    const closeIfOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false); }
    };
    document.addEventListener('mousedown', closeIfOutside);
    document.addEventListener('keydown', closeOnEscape, true);
    return () => { document.removeEventListener('mousedown', closeIfOutside); document.removeEventListener('keydown', closeOnEscape, true); };
  }, [open]);

  const select = (next: ModelProtocol | null) => { onChange(next); setOpen(false); };
  const move = (event: KeyboardEvent<HTMLButtonElement>, offset: number) => {
    event.preventDefault();
    const current = protocolChoices.indexOf(model.protocol ?? null);
    select(protocolChoices[(current + offset + protocolChoices.length) % protocolChoices.length]);
  };

  return <div className="provider-protocol-select model-protocol-select" ref={root}>
    <button type="button" className="provider-protocol-trigger" aria-label={`模型 ${model.model_name || ''} 协议`} aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} onClick={() => setOpen(current => !current)} onKeyDown={event => {
      if (event.key === 'ArrowDown') move(event, 1);
      else if (event.key === 'ArrowUp') move(event, -1);
    }}><span>{label(model.protocol ?? null)}</span><ChevronDown size={14} aria-hidden="true"/></button>
    {open && <div id={listId} className="provider-protocol-options" role="listbox" aria-label="调用协议">{protocolChoices.map(choice => <button key={choice ?? 'auto'} type="button" role="option" aria-selected={(model.protocol ?? null) === choice} onClick={() => select(choice)}>{label(choice)}</button>)}</div>}
  </div>;
}

function ProviderUsagePanel({ provider, value, loading, onRefresh }: { provider: ModelProvider; value?: ModelProviderUsage; loading: boolean; onRefresh: () => void }) {
  if (provider.auth_type !== 'API_KEY' && !provider.usage_query) return <div className="provider-usage unavailable"><div><span>上游 Key 预算</span><b>该认证方式不支持查询</b></div></div>;
  const detail = value?.status === 'AVAILABLE'
    ? <>已用 {budgetNumber(value.spend ?? 0)} / 上限 {budgetNumber(value.max_budget ?? 0)} <strong>剩余 {budgetNumber(value.remaining ?? 0)}</strong></>
    : value?.status === 'UNLIMITED'
      ? <>已用 {budgetNumber(value.spend ?? 0)}；上游未设置预算上限</>
      : value?.status === 'UNAVAILABLE'
        ? <>{value.reason ?? '上游未提供预算查询'}</>
        : <>{provider.usage_query ? '按当前配置查询服务余额' : '按需查询上游 API Key 的共享预算'}</>;
  return <div className={`provider-usage ${value?.status?.toLowerCase() ?? ''}`}><div><span>{provider.usage_query ? '自定义余额查询' : '上游 Key 预算'}</span>{value?.metrics ? <ProviderUsageMetrics metrics={value.metrics}/> : <b>{detail}</b>}</div><button className="ghost" disabled={loading || !provider.has_api_key} onClick={onRefresh}>{loading ? '查询中…' : provider.usage_query ? (value ? '刷新余额' : '查询余额') : value ? '刷新用量' : '查询用量'}</button></div>;
}
function ProviderUsageMetrics({ metrics }: { metrics: NonNullable<ModelProviderUsage['metrics']> }) {
  return <div className="provider-usage-metrics" role="status">{metrics.map((metric, index) => <div className="provider-usage-metric" key={index}>
    <span>{metric.label}</span><b>{metric.value === null ? '—' : `${metric.currency ? '$' : ''}${budgetNumber(metric.value)}${metric.percentage ? '%' : ''}`}</b>
    {metric.percentage && metric.value !== null && <progress aria-label={metric.label} max={100} value={Math.max(0, Math.min(100, metric.value))}/>}
  </div>)}</div>;
}

function ProviderEditor({ provider, onClose }: { provider?: ModelProvider; onClose: () => void }) {
  useEscapeClose(onClose);
  const qc = useQueryClient();
  const [form, setForm] = useState<ModelProviderWrite>(blank());
  const [discovered, setDiscovered] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [usageHeaders, setUsageHeaders] = useState('{}');
  const [usagePreview, setUsagePreview] = useState<ModelProviderUsage>();
  const [usagePreviewError, setUsagePreviewError] = useState('');
  const [previewingUsage, setPreviewingUsage] = useState(false);
  const usageRequest = useRef<AbortController | undefined>(undefined);
  useEffect(() => {
    setUsagePreview(undefined); setUsagePreviewError('');
    usageRequest.current?.abort(); setPreviewingUsage(false);
    return () => usageRequest.current?.abort();
  }, [form, usageHeaders]);
  const usageQuery = (): ProviderUsageQuery | null => {
    if (!form.usage_query || form.auth_type === 'CODEX_OAUTH') return null;
    let headers: unknown;
    try { headers = JSON.parse(usageHeaders); }
    catch { throw new Error('查询请求头必须是 JSON 对象'); }
    if (!headers || Array.isArray(headers) || typeof headers !== 'object' || Object.values(headers).some(value => typeof value !== 'string')) throw new Error('查询请求头必须是字符串键值组成的 JSON 对象');
    return { ...form.usage_query, headers: headers as Record<string, string> };
  };
  const previewUsage = async () => {
    setUsagePreview(undefined); setUsagePreviewError('');
    const controller = new AbortController();
    usageRequest.current?.abort(); usageRequest.current = controller;
    try {
      if (form.auth_type === 'CODEX_OAUTH') return;
      const query = usageQuery();
      setPreviewingUsage(true);
      const result = await api.previewProviderUsage({ provider_id: provider?.id, base_url: form.base_url, api_key: form.api_key, usage_query: query }, controller.signal);
      if (!controller.signal.aborted) setUsagePreview(result);
    } catch (reason) {
      if (!controller.signal.aborted) setUsagePreviewError(reason instanceof Error ? reason.message : '余额查询失败');
    } finally { if (!controller.signal.aborted) setPreviewingUsage(false); }
  };
  const applyDiscovery = useCallback((result: { models: string[]; provider?: ModelProvider }) => {
    setDiscovered(result.models);
    if (result.provider) {
      const synchronized = result.provider;
      setForm(old => ({
        ...old,
        row_version: synchronized.row_version,
        models: synchronized.models.map(({ model_name, enabled, is_default, protocol }) => ({ model_name, enabled, is_default, protocol: protocol ?? null })),
      }));
      qc.setQueryData<ModelProvider[]>(['model-providers'], current => current?.map(item => item.id === synchronized.id ? synchronized : item));
      return;
    }
    setForm(old => {
      const models = old.models.filter(item => item.model_name.trim());
      return { ...old, models };
    });
  }, [qc]);
  useEffect(() => {
    setForm(provider ? { name: provider.name, auth_type: provider.auth_type, base_url: provider.base_url, api_key: '', usage_query: provider.usage_query ?? null, row_version: provider.row_version, models: provider.models.map(({ model_name, enabled, is_default, protocol }) => ({ model_name, enabled, is_default, protocol: protocol ?? null })) } : blank());
    setUsageHeaders(JSON.stringify(provider?.usage_query?.headers ?? {}, null, 2));
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
      const payload = { ...form, models: form.models.map(model => ({ ...model, protocol: model.protocol ?? null })), usage_query: usageQuery() };
      const saved = provider ? await api.updateProvider(provider.id, payload) : await api.createProvider(payload);
      qc.setQueryData<ModelProvider[]>(['model-providers'], current => {
        const providers = current ?? [];
        const exists = providers.some(item => item.id === saved.id);
        return exists ? providers.map(item => item.id === saved.id ? saved : item) : [saved, ...providers];
      });
      void qc.invalidateQueries({ queryKey: ['model-providers'] });
      onClose();
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { setBusy(false); }
  };
  const discover = async () => {
    setBusy(true); setError('');
    try {
      const result = form.auth_type === 'CODEX_OAUTH'
        ? await api.discoverProviderModels(provider!.id)
        : await api.previewProviderModels({ base_url: form.base_url, api_key: form.api_key, provider_id: provider?.id });
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
  const changeAuthType = (auth_type: ProviderAuthType) => setForm(old => ({
    ...old,
    auth_type,
    base_url: auth_type === 'CODEX_OAUTH' ? '' : old.base_url,
    api_key: '',
    usage_query: auth_type === 'CODEX_OAUTH' ? null : old.usage_query,
    models: auth_type === 'CODEX_OAUTH' ? [] : old.models.length ? old.models : [{ model_name: '', enabled: true, is_default: true }],
  }));
  return <div className="modal-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}><form className="modal model-editor" onSubmit={save}><header><div><span className="eyebrow">MODEL PROVIDER</span><h2>{provider ? '编辑供应商' : '新增供应商'}</h2><p>API Key 与 Codex OAuth 凭据均加密保存且永不返回浏览器。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <div className="form-grid"><label>服务名称<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}/></label><label>认证方式<ProviderAuthSelect value={form.auth_type} onChange={changeAuthType}/></label>{form.auth_type === 'CODEX_OAUTH' ? <p className="startpoint wide">保存后在服务详情点击“登录 Codex”，使用设备码连接 ChatGPT 订阅。OAuth 服务仅用于 Agent 节点，不用于 Prompt Gate。</p> : <><label>Base URL<input required value={form.base_url} placeholder="https://api.example.com" onChange={e => setForm({ ...form, base_url: e.target.value })}/></label><label className="wide">API Key<input type="password" value={form.api_key ?? ''} placeholder={provider?.has_api_key ? `留空保留现有密钥 ${provider.api_key_hint ?? ''}` : '输入 API Key'} onChange={e => setForm({ ...form, api_key: e.target.value })}/></label><small className="wide">只需填写一个地址，FlowWeave 按模型自动选择 Claude / GPT 等协议与端点。</small></>}</div>
    {isApiKeyProvider(form.auth_type) && <section className="model-usage-config" aria-label="余额查询配置">
      <div className="model-usage-config-heading"><label className="model-usage-toggle"><input type="checkbox" checked={Boolean(form.usage_query)} onChange={event => setForm(old => ({ ...old, usage_query: event.target.checked ? { method: 'GET', url: `${old.base_url.replace(/\/v1\/?$/, '').replace(/\/$/, '')}/key/info`, expression: '$info.max_budget - info.spend', headers: {} } : null }))}/>自定义余额查询</label><small>{form.usage_query ? '按接口返回的 JSON 计算余额' : '使用默认上游 Key 预算查询'}</small></div>
      {form.usage_query && <>
        <div className="model-usage-request"><div className="model-usage-method" role="group" aria-label="查询请求方法">{(['GET', 'POST'] as const).map(method => <button type="button" key={method} aria-pressed={form.usage_query?.method === method} onClick={() => setForm(old => ({ ...old, usage_query: { ...old.usage_query!, method } }))}>{method}</button>)}</div><label>余额查询地址<input required type="url" value={form.usage_query.url} placeholder="https://api.example.com/key/info" onChange={event => setForm(old => ({ ...old, usage_query: { ...old.usage_query!, url: event.target.value } }))}/></label></div>
        <label>余额字段<input required value={form.usage_query.expression} placeholder="$info.max_budget - info.spend" onChange={event => setForm(old => ({ ...old, usage_query: { ...old.usage_query!, expression: event.target.value } }))}/></label>
        <small>支持 JSON 字段路径、+ - * / 与括号。前加 $ 显示金额，后加 % 将比例显示为百分比。多个余额用分号分隔，可加标签，如「剩余: $data.balance」。</small>
        <details className="model-usage-advanced"><summary>请求头与数组汇总</summary><label>查询请求头（JSON）<textarea className="model-usage-headers" value={usageHeaders} onChange={event => setUsageHeaders(event.target.value)} placeholder={'{"Authorization": "Bearer {key}"}'}/></label><small>默认复用服务认证，地址和请求头中的 {'{key}'} 使用当前 API Key。密钥请使用占位符。</small><small>数组可写 a.*.used、a[status=active].used，通过 sum()、min()、max() 或 count() 汇总，如「sum(a.*.used)」。取不到的余额显示 —。</small></details>
      </>}
      {(form.auth_type === 'API_KEY' || form.usage_query) && <div className="model-usage-preview"><button type="button" className="secondary" disabled={busy || previewingUsage || !form.base_url.trim() || (!form.api_key && !provider?.has_api_key)} onClick={() => void previewUsage()}>{previewingUsage ? <LoaderCircle className="spin" size={13}/> : <CheckCircle2 size={13}/>} {previewingUsage ? '查询中…' : '查询余额'}</button>{usagePreview && (usagePreview.metrics ? <ProviderUsageMetrics metrics={usagePreview.metrics}/> : <span role="status">{usagePreview.status === 'UNAVAILABLE' ? usagePreview.reason : usagePreview.status === 'UNLIMITED' ? `已用 ${budgetNumber(usagePreview.spend ?? 0)}，上游未设置预算上限` : `剩余 ${budgetNumber(usagePreview.remaining ?? 0)}`}</span>)}</div>}
      {usagePreviewError && <p className="error" role="alert">{usagePreviewError}</p>}
    </section>}
    <div className="model-discovery-head"><div><b>可用模型</b><small>{form.auth_type === 'CODEX_OAUTH' ? provider?.oauth_connected ? '已按当前登录账号自动拉取；也可手动刷新' : '登录 Codex 后可按账号自动拉取模型' : '填写连接信息后拉取模型，再选择启用项和默认模型；协议按模型自动判断，可逐个调整'}</small></div>{(isApiKeyProvider(form.auth_type) || (form.auth_type === 'CODEX_OAUTH' && provider?.oauth_connected)) && <button type="button" className="secondary" disabled={busy || !form.base_url.trim() && isApiKeyProvider(form.auth_type)} onClick={() => void discover()}>{busy ? '拉取中…' : form.auth_type === 'CODEX_OAUTH' ? '刷新模型' : '拉取模型'}</button>}</div>
    {isApiKeyProvider(form.auth_type) && discovered.length > 0 && <div className="model-tags discovery-tags">{discovered.map(name => { const selected = form.models.some(item => item.model_name === name); return <button type="button" key={name} className={selected ? 'selected' : ''} aria-pressed={selected} onClick={() => toggle(name)}>{name}</button>; })}</div>}
    <div className={`provider-model-list ${form.auth_type === 'CODEX_OAUTH' ? 'oauth-model-list' : 'with-protocol'}`}>{form.models.map((model, index) => <div className="provider-model-row" key={model.model_name || index}>{form.auth_type === 'CODEX_OAUTH' ? <span className="synced-model-name"><b>{model.model_name}</b><small>由当前 Codex 账号同步{model.supported_reasoning_efforts?.length ? ` · 支持 ${model.supported_reasoning_efforts.join(' / ')}` : ''}</small></span> : <input aria-label={`模型 ${index + 1}`} required value={model.model_name} placeholder="模型标识" onChange={e => updateModel(index, { model_name: e.target.value })}/>}{isApiKeyProvider(form.auth_type) && <ModelProtocolSelect model={model} onChange={protocol => updateModel(index, { protocol })}/>}<label><input type="checkbox" checked={model.enabled} onChange={e => updateModel(index, { enabled: e.target.checked })}/>启用</label><label><input type="radio" name="default-model" checked={model.is_default} onChange={() => updateModel(index, { is_default: true, enabled: true })}/>默认</label>{isApiKeyProvider(form.auth_type) && <button type="button" className="danger model-remove-button" aria-label={`移除模型 ${model.model_name}`} onClick={() => removeModel(index)}><Trash2 size={16}/>删除</button>}</div>)}</div>
    {isApiKeyProvider(form.auth_type) && <button type="button" className="ghost" onClick={() => setForm(old => ({ ...old, models: [...old.models, { model_name: '', enabled: true, is_default: old.models.length === 0 }] }))}><Plus size={13}/>手动添加模型</button>}
    {form.auth_type === 'CODEX_OAUTH' && provider?.oauth_connected && !busy && !form.models.length && <p className="model-discovery-empty">当前账号没有返回可用模型，请点击“刷新模型”重试。</p>}
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || (isApiKeyProvider(form.auth_type) && !form.models.length)}>{busy ? '处理中…' : '保存供应商'}</button></footer>
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
  const { data: providers = [], isLoading, error: loadError, refetch } = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const [editing, setEditing] = useState<ModelProvider | null | undefined>();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [testFeedback, setTestFeedback] = useState<Record<string, TestFeedback>>({});
  const [usage, setUsage] = useState<Record<string, { value: ModelProviderUsage; rowVersion: number }>>({});
  const [codexLogin, setCodexLogin] = useState<{ provider: ModelProvider; authorization: CodexDeviceAuthorization }>();
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState<string>();
  const filteredProviders = providers.filter(item => !query.trim() || `${item.name} ${item.base_url} ${item.models.map(model => model.model_name).join(' ')}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const allSelected = providers.length > 0 && providers.every(provider => selectedIds.has(provider.id));
  const pagedProviders = filteredProviders.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => { if (page > Math.max(1, Math.ceil(filteredProviders.length / pageSize))) setPage(1); }, [page, pageSize, filteredProviders.length]);
  const activeProvider = pagedProviders.find(item => item.id === activeId) ?? pagedProviders[0];
  useEffect(() => { setSelectedIds(current => { const next = new Set([...current].filter(id => providers.some(item => item.id === id))); return next.size === current.size ? current : next; }); }, [providers]);
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
    onSettled: () => void qc.invalidateQueries({ queryKey: ['model-providers'] }),
  });
  const readUsage = useMutation({
    mutationFn: (provider: ModelProvider) => api.providerUsage(provider.id),
    onSuccess: (value, provider) => setUsage(old => ({ ...old, [provider.id]: { value, rowVersion: provider.row_version } })),
    onError: (_reason, provider) => setUsage(old => ({ ...old, [provider.id]: { value: { status: 'UNAVAILABLE', reason: '暂时无法读取上游用量' }, rowVersion: provider.row_version } })),
  });
  const toggle = (id: string) => setSelectedIds(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleAll = () => setSelectedIds(allSelected ? new Set() : new Set(providers.map(provider => provider.id)));
  const startCodexLogin = async (provider: ModelProvider) => {
    setError('');
    try {
      const authorization = await api.startCodexOAuth(provider.id);
      setCodexLogin({ provider, authorization });
      window.open(authorization.verification_url, '_blank', 'noopener,noreferrer');
      await qc.invalidateQueries({ queryKey: ['model-providers'] });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法启动 Codex 登录'); }
  };
  const disconnectCodex = async (provider: ModelProvider) => {
    if (!await dialog.confirm({ title: `断开“${provider.name}”？`, message: '加密保存的 Codex OAuth token 将被永久删除，节点将无法继续使用该服务。', confirmLabel: '断开连接', tone: 'danger' })) return;
    try { await api.disconnectCodexOAuth(provider.id); await qc.invalidateQueries({ queryKey: ['model-providers'] }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '断开失败'); }
  };
  const removeMany = async (ids: string[], label: string) => {
    if (!ids.length) return;
    if (!await dialog.confirm({ title: `删除${label}？`, message: '供应商、密钥和模型配置将被永久删除。已有会话会保留，但下次发送前需要选择其他模型；自动运行会按名称顺序选择其他已连接服务的默认模型。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setDeleting(true); setError(''); setNotice('');
    try {
      const result = await api.deleteProviders(ids);
      setSelectedIds(new Set());
      const outcomes = [`已删除 ${result.deleted_ids.length} 个供应商。`];
      if (result.session_reconfigured) outcomes.push(`${result.session_reconfigured} 个会话需要在下次发送前选择其他模型。`);
      if (result.automatic_reconfigured) outcomes.push(`${result.automatic_reconfigured} 个自动运行已改用其他已连接服务的默认模型。`);
      if (result.automatic_needs_model_configuration) outcomes.push(`${result.automatic_needs_model_configuration} 个自动运行没有可用替代服务，需要重新配置模型。`);
      if (result.deleted_ids.length) setNotice(outcomes.join(' '));
      await qc.invalidateQueries({ queryKey: ['model-providers'] });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };
  return <SettingsManagementLayout title="供应商配置" count={providers.length} query={query} onQueryChange={value => { setQuery(value); setPage(1); }} className="models-page"
    actions={<button className="primary" onClick={() => setEditing(null)}><Plus size={14}/>新增供应商</button>}
    selection={<><button className="ghost" disabled={!providers.length || deleting} onClick={toggleAll}><CheckSquare size={13}/>{allSelected ? '取消全选' : '全选'}</button><button className="danger" disabled={!selectedIds.size || deleting} onClick={() => void removeMany([...selectedIds], `选中的 ${selectedIds.size} 个供应商`)}><Trash2 size={13}/>{deleting ? '删除中…' : `批量删除 (${selectedIds.size})`}</button></>}
    listKey={`${query}-${page}`}
    list={isLoading ? <div className="empty compact">加载中…</div> : loadError ? <div className="empty compact error"><span>{loadError.message}</span><button className="secondary" onClick={() => void refetch()}>重试</button></div> : pagedProviders.length ? pagedProviders.map(provider => <SettingsResourceRow key={provider.id} name={provider.name} description={provider.models.find(item => item.is_default && item.enabled)?.model_name ?? '未设置默认模型'} meta={<><em className={provider.connection_state === 'FAILED' ? 'failed' : ''}>{CONNECTION_STATE_LABELS[provider.connection_state] ?? provider.connection_state}</em><span>{provider.auth_type === 'CODEX_OAUTH' ? 'Codex OAuth' : 'API Key'}</span></>} active={activeProvider?.id === provider.id} onSelect={() => setActiveId(provider.id)} checked={selectedIds.has(provider.id)} onToggle={() => toggle(provider.id)}/>) : <div className="empty compact">{query ? '没有匹配的供应商' : '还没有供应商'}</div>}
    pagination={<Pagination page={page} pageSize={pageSize} total={filteredProviders.length} onPageChange={setPage}/>}
    overlays={<>    {editing !== undefined && <ProviderEditor provider={editing ?? undefined} onClose={() => setEditing(undefined)}/>}
    {codexLogin && <CodexLoginDialog provider={codexLogin.provider} authorization={codexLogin.authorization} onClose={() => setCodexLogin(undefined)} onConnected={status => { setCodexLogin(undefined); if (status.model_sync_error) setError(`Codex 登录成功，但模型同步失败：${status.model_sync_error}`); else setNotice(`Codex 登录成功，已自动同步 ${status.model_count ?? 0} 个模型。`); void qc.invalidateQueries({ queryKey: ['model-providers'] }); }}/>}
</>}>
    <Toast kind="error" message={error} onDismiss={() => setError('')}/><Toast message={notice} onDismiss={() => setNotice('')}/>
    {activeProvider ? [activeProvider].map(provider => {
      const feedback = testFeedback[provider.id];
      const testing = feedback?.state === 'testing';
      const oauth = provider.auth_type === 'CODEX_OAUTH';
      const readingUsage = readUsage.isPending && readUsage.variables?.id === provider.id;
      const enabledCount = provider.models.filter(item => item.enabled).length;
      const protocol = oauth ? 'Codex Responses' : protocolSummary(provider.protocols);
      return <div className="settings-selected-provider" key={provider.id}>
        <header className="settings-detail-header">
          <div><h2>{provider.name}</h2><small title={provider.base_url}>{oauth ? 'Codex OAuth · ChatGPT 订阅' : provider.base_url}</small></div>
          <div className="settings-detail-actions">
            {oauth && !provider.oauth_connected
              ? <button className="secondary" onClick={() => void startCodexLogin(provider)}><LogIn size={14}/>{provider.oauth_device_pending ? '重新开始登录' : '登录 Codex'}</button>
              : <button className="secondary" disabled={deleting || test.isPending} onClick={() => test.mutate(provider.id)}>{testing ? <LoaderCircle className="spin" size={14}/> : <CheckCircle2 size={14}/>} {testing ? '测试中…' : '测试连接'}</button>}
            {oauth && provider.oauth_connected && <button className="ghost" onClick={() => void disconnectCodex(provider)}><LogOut size={14}/>断开 Codex</button>}
            <button className="secondary" aria-label={`编辑供应商 ${provider.name}`} onClick={() => setEditing(provider)}><Pencil size={14}/>编辑</button>
            <button className="danger" disabled={deleting} aria-label={`删除供应商 ${provider.name}`} onClick={() => void removeMany([provider.id], `供应商“${provider.name}”`)}><Trash2 size={14}/>删除</button>
          </div>
        </header>
        <div className="settings-detail-body">
          <article className="model-config-card settings-model-detail">
            {feedback && <p className={`model-test-result ${feedback.state}`} role={feedback.state === 'error' ? 'alert' : 'status'}>{feedback.message}</p>}
            <div className="model-provider-state">
              <span className={provider.available_for_nodes ? 'available' : 'unavailable'}>{provider.available_for_nodes ? <ShieldCheck size={13}/> : <ShieldX size={13}/>} {provider.available_for_nodes ? '可用于节点' : oauth ? (provider.oauth_connected ? '暂无可用默认模型' : '需要登录 Codex') : '暂无可用默认模型'}</span>
              <span><Link2 size={12}/>{provider.reference_node_count} 个引用节点</span>
            </div>
            <div className="settings-model-overview">
              <section className="settings-model-connection" aria-label="连接配置">
                <h3>连接配置</h3>
                <dl className="settings-detail-facts">
                  <dt>连接状态</dt><dd className={provider.connection_state === 'CONNECTED' ? 'good' : provider.connection_state === 'FAILED' ? 'bad' : ''}>{CONNECTION_STATE_LABELS[provider.connection_state] ?? provider.connection_state}</dd>
                  <dt>认证方式</dt><dd>{providerAuthOptions.find(option => option.value === provider.auth_type)?.label}</dd>
                  <dt>支持协议</dt><dd>{protocol}</dd>
                  <dt>{oauth ? 'Codex 账号' : 'API Key'}</dt><dd className="settings-model-key"><KeyRound size={12}/>{oauth ? (provider.oauth_connected ? provider.oauth_account_email ?? '已登录' : '未登录') : (provider.has_api_key ? `已配置 ${provider.api_key_hint ?? ''}` : '未配置')}</dd>
                  <dt>默认模型</dt><dd>{provider.models.find(item => item.is_default && item.enabled)?.model_name ?? '未设置'}</dd>
                </dl>
              </section>
              <section className="settings-model-service" aria-label="用量与服务信息">
                <ProviderUsagePanel provider={provider} value={usage[provider.id]?.rowVersion === provider.row_version ? usage[provider.id].value : undefined} loading={readingUsage} onRefresh={() => readUsage.mutate(provider)}/>
                <dl className="settings-detail-facts settings-model-timestamps">
                  <dt>创建时间</dt><dd>{new Date(provider.created_at).toLocaleString('zh-CN')}</dd>
                  <dt>最近更新</dt><dd>{new Date(provider.updated_at).toLocaleString('zh-CN')}</dd>
                </dl>
              </section>
            </div>
            <div className="settings-model-table-heading"><h3>模型列表（{provider.models.length}）</h3><span>已启用 {enabledCount} · 已停用 {provider.models.length - enabledCount}</span></div>
            <div className="settings-model-table-scroll">
              <table className="settings-data-table settings-model-table">
                <thead><tr><th>模型名称</th><th>协议</th><th>启用状态</th><th>推理档位</th><th>上下文窗口</th><th>默认模型</th></tr></thead>
                <tbody>{provider.models.map(item => <tr key={item.id ?? item.model_name}>
                  <td><b title={item.model_name}>{item.model_name}</b></td>
                  <td>{PROTOCOL_LABELS[item.effective_protocol ?? effectiveProtocol(item)]}{!oauth && item.protocol && <small>手动指定</small>}</td>
                  <td><span className={item.enabled ? 'good' : ''}>{item.enabled ? '已启用' : '已停用'}</span></td>
                  <td>{item.supported_reasoning_efforts?.length ? item.supported_reasoning_efforts.join(' / ') : '未提供'}{item.default_reasoning_effort && <small>默认 {item.default_reasoning_effort}</small>}</td>
                  <td>{item.context_window ? `${item.context_window.toLocaleString()} tokens` : '未提供'}</td>
                  <td>{item.is_default && item.enabled ? <span className="settings-model-default">默认</span> : '—'}</td>
                </tr>)}</tbody>
              </table>
              {!provider.models.length && <div className="empty compact">暂无模型，请通过编辑配置模型。</div>}
            </div>
          </article>
        </div>
      </div>;
    }) : <div className="empty"><Server size={28}/><b>{query ? '没有匹配的供应商' : '还没有供应商'}</b><span>在左侧新增或选择供应商，查看连接与模型配置。</span></div>}

  </SettingsManagementLayout>;
}
