import { CheckCircle2, CheckSquare, KeyRound, Link2, LoaderCircle, Pencil, Plus, Server, ShieldCheck, ShieldX, Trash2 } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormEvent, useEffect, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { ModelProvider, ModelProviderWrite, ProviderModel } from '../types';

const blank = (): ModelProviderWrite => ({ name: '', base_url: '', api_key: '', models: [{ model_name: '', enabled: true, is_default: true }] });
const CONNECTION_STATE_LABELS: Record<string, string> = { UNTESTED: '未测试', CONNECTED: '已连接', FAILED: '连接失败' };
type TestFeedback = { state: 'testing' | 'success' | 'error'; message: string };
function ProviderEditor({ provider, onClose }: { provider?: ModelProvider; onClose: () => void }) {
  useEscapeClose(onClose);
  const qc = useQueryClient();
  const [form, setForm] = useState<ModelProviderWrite>(blank());
  const [discovered, setDiscovered] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setForm(provider ? { name: provider.name, base_url: provider.base_url, api_key: '', row_version: provider.row_version, models: provider.models.map(({ model_name, enabled, is_default }) => ({ model_name, enabled, is_default })) } : blank());
    setDiscovered([]); setError('');
  }, [provider]);
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
    if (!provider) { setError('请先保存服务，再执行模型发现'); return; }
    setBusy(true); setError('');
    try { const result = await api.discoverProviderModels(provider.id); setDiscovered(result.models); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '发现模型失败'); }
    finally { setBusy(false); }
  };
  const toggle = (name: string) => setForm(old => {
    const exists = old.models.find(item => item.model_name === name);
    return { ...old, models: exists ? old.models.filter(item => item.model_name !== name) : [...old.models, { model_name: name, enabled: true, is_default: old.models.length === 0 }] };
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
  return <div className="modal-backdrop"><form className="modal model-editor" onSubmit={save}><header><div><span className="eyebrow">MODEL PROVIDER</span><h2>{provider ? '编辑模型服务' : '新增模型服务'}</h2><p>密钥加密保存且永不返回浏览器。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <div className="form-grid"><label>服务名称<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}/></label><label>Base URL<input required value={form.base_url} placeholder="https://api.example.com/v1" onChange={e => setForm({ ...form, base_url: e.target.value })}/></label><label className="wide">API Key<input type="password" value={form.api_key ?? ''} placeholder={provider?.has_api_key ? `留空保留现有密钥 ${provider.api_key_hint ?? ''}` : '输入 API Key'} onChange={e => setForm({ ...form, api_key: e.target.value })}/></label></div>
    <div className="model-discovery-head"><div><b>可用模型</b><small>发现模型后选择启用项和默认模型</small></div><button type="button" className="secondary" onClick={() => void discover()}>发现模型</button></div>
    {discovered.length > 0 && <div className="model-tags discovery-tags">{discovered.map(name => <button type="button" key={name} className={form.models.some(item => item.model_name === name) ? 'selected' : ''} onClick={() => toggle(name)}>{name}</button>)}</div>}
    <div className="provider-model-list">{form.models.map((model, index) => <div className="provider-model-row" key={index}><input aria-label={`模型 ${index + 1}`} required value={model.model_name} placeholder="模型标识" onChange={e => updateModel(index, { model_name: e.target.value })}/><label><input type="checkbox" checked={model.enabled} onChange={e => updateModel(index, { enabled: e.target.checked })}/>启用</label><label><input type="radio" name="default-model" checked={model.is_default} onChange={() => updateModel(index, { is_default: true, enabled: true })}/>默认</label><button type="button" className="ghost" aria-label={`移除模型 ${model.model_name}`} onClick={() => removeModel(index)}><Trash2 size={14}/></button></div>)}</div>
    <button type="button" className="ghost" onClick={() => setForm(old => ({ ...old, models: [...old.models, { model_name: '', enabled: true, is_default: old.models.length === 0 }] }))}><Plus size={13}/>手动添加模型</button>
    {error && <p className="error">{error}</p>}<footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !form.models.length}>{busy ? '处理中…' : '保存模型服务'}</button></footer>
  </form></div>;
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
  const allSelected = providers.length > 0 && providers.every(provider => selectedIds.has(provider.id));
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
  return <section className="page models-page"><div className="page-head"><div><span className="eyebrow">MODEL PROVIDERS</span><h1>大模型配置</h1><p>统一维护模型服务、连接状态、启用模型和默认模型；节点不接触 API Key。</p></div><button className="primary" onClick={() => setEditing(null)}><Plus size={16}/>新增模型服务</button></div>
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    {!isLoading && providers.length > 0 && <div className="model-config-tools"><span>共 {providers.length} 个模型服务</span><div className="bulk-actions"><button className="secondary" disabled={deleting} onClick={toggleAll}><CheckSquare size={14}/>{allSelected ? '取消全选' : '全选'}</button><button className="danger" disabled={!selectedIds.size || deleting} onClick={() => void removeMany([...selectedIds], `选中的 ${selectedIds.size} 个模型服务`)}><Trash2 size={14}/>{deleting ? '删除中…' : `批量删除 (${selectedIds.size})`}</button></div></div>}
    {isLoading ? <div className="empty">加载中…</div> : <div className="model-config-grid">{providers.map(provider => { const feedback = testFeedback[provider.id]; const testing = feedback?.state === 'testing'; return <article className={`model-config-card ${selectedIds.has(provider.id) ? 'selected' : ''}`} key={provider.id}><header><label className="model-provider-select resource-check"><input type="checkbox" aria-label={`选择模型服务 ${provider.name}`} checked={selectedIds.has(provider.id)} onChange={() => toggle(provider.id)}/><span className="model-server-icon"><Server size={18}/></span></label><div><h3>{provider.name}</h3><small>{provider.base_url}</small></div><div className="card-actions"><button title="编辑" aria-label={`编辑模型服务 ${provider.name}`} onClick={() => setEditing(provider)}><Pencil size={15}/></button><button title="删除" aria-label={`删除模型服务 ${provider.name}`} onClick={() => void removeMany([provider.id], `模型服务“${provider.name}”`)}><Trash2 size={15}/></button></div></header><div className="model-provider-state"><span className={provider.available_for_nodes ? 'available' : 'unavailable'}>{provider.available_for_nodes ? <ShieldCheck size={13}/> : <ShieldX size={13}/>} {provider.available_for_nodes ? '可用于节点' : '暂无可用默认模型'}</span><span><Link2 size={12}/>{provider.reference_node_count} 个引用节点</span></div><div className="model-provider-meta"><span>连接状态</span><b className={provider.connection_state === 'CONNECTED' ? 'good' : provider.connection_state === 'FAILED' ? 'bad' : ''}>{CONNECTION_STATE_LABELS[provider.connection_state] ?? provider.connection_state}</b><span>API Key</span><b><KeyRound size={12}/>{provider.has_api_key ? `已配置 ${provider.api_key_hint ?? ''}` : '未配置'}</b><span>默认模型</span><b>{provider.models.find(item => item.is_default && item.enabled)?.model_name ?? '未设置'}</b><span>启用模型</span><b>{provider.models.filter(item => item.enabled).length} 个</b></div><div className="model-tags">{provider.models.filter(item => item.enabled).map(item => <span key={item.id}>{item.model_name}</span>)}</div><button className="secondary full" disabled={deleting || test.isPending} onClick={() => test.mutate(provider.id)}>{testing ? <LoaderCircle className="spin" size={14}/> : <CheckCircle2 size={14}/>} {testing ? '测试中…' : '测试连接'}</button>{feedback && <p className={`model-test-result ${feedback.state}`} role={feedback.state === 'error' ? 'alert' : 'status'}>{feedback.message}</p>}</article>; })}</div>}
    {!isLoading && !providers.length && <div className="empty"><Server size={28}/><b>还没有模型服务</b></div>}
    {editing !== undefined && <ProviderEditor provider={editing ?? undefined} onClose={() => setEditing(undefined)}/>}</section>;
}
