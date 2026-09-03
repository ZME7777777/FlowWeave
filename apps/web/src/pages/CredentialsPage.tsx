import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Globe2, KeyRound, Pencil, Plus, ShieldCheck, Trash2, UserRound } from 'lucide-react';
import { FormEvent, useEffect, useState } from 'react';
import { api } from '../api/client';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { WebsiteCredential, WebsiteCredentialWrite } from '../types';

const empty = (): WebsiteCredentialWrite => ({ name: '', target_host: '', include_subdomains: false, auth_type: 'USERNAME_PASSWORD', username: '', secret: '' });

function CredentialEditor({ credential, onClose }: { credential?: WebsiteCredential; onClose: () => void }) {
  useEscapeClose(onClose);
  const client = useQueryClient();
  const [form, setForm] = useState<WebsiteCredentialWrite>(empty());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => setForm(credential ? { name: credential.name, target_host: credential.target_host, include_subdomains: credential.include_subdomains, auth_type: credential.auth_type, username: '', secret: '', row_version: credential.row_version } : empty()), [credential]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (credential) await api.updateWebsiteCredential(credential.id, form);
      else await api.createWebsiteCredential(form);
      await client.invalidateQueries({ queryKey: ['website-credentials'] }); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存认证信息失败'); }
    finally { setBusy(false); }
  };
  const usernamePassword = form.auth_type === 'USERNAME_PASSWORD';
  return <div className="modal-backdrop"><form className="modal credential-editor" onSubmit={submit}><header><div><span className="eyebrow">WEBSITE AUTHENTICATION</span><h2>{credential ? '编辑认证条目' : '新增认证条目'}</h2><p>Secret 会加密保存；保存后不再返回浏览器，也不会进入终端镜像。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header><div className="form-grid"><label>名称<input required value={form.name} placeholder="例如：内部检索只读账号" onChange={event => setForm({ ...form, name: event.target.value })}/></label><label>认证方式<select value={form.auth_type} onChange={event => setForm({ ...form, auth_type: event.target.value as WebsiteCredentialWrite['auth_type'], username: '' })}><option value="USERNAME_PASSWORD">用户名与密码</option><option value="BEARER_TOKEN">Bearer Token</option></select></label><label className="wide">目标域名<input required value={form.target_host} placeholder="search.example.com" autoCapitalize="none" onChange={event => setForm({ ...form, target_host: event.target.value })}/><small>填写主机名，不要包含 http(s)、路径、端口或用户名。</small></label><label className="wide credential-subdomain"><input type="checkbox" checked={form.include_subdomains} onChange={event => setForm({ ...form, include_subdomains: event.target.checked })}/><span><b>允许匹配子域名</b><small>仅开启后，api.search.example.com 才可使用 search.example.com 的条目。</small></span></label>{usernamePassword && <label>用户名<input required={!credential || Boolean(form.username)} value={form.username ?? ''} placeholder={credential?.has_username ? '留空保留现有用户名' : '输入用户名'} onChange={event => setForm({ ...form, username: event.target.value })}/></label>}<label className={usernamePassword ? '' : 'wide'}>{usernamePassword ? '密码' : 'Token'}<input type="password" required={!credential} value={form.secret ?? ''} placeholder={credential ? `留空保留现有 Secret ${credential.secret_hint ? `••••${credential.secret_hint}` : ''}` : usernamePassword ? '输入密码' : '输入 Token'} onChange={event => setForm({ ...form, secret: event.target.value })}/></label>{error && <p className="error wide">{error}</p>}</div><footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy}>{busy ? '保存中…' : '保存认证信息'}</button></footer></form></div>;
}

export function CredentialsPage() {
  const dialog = useProductDialog(); const client = useQueryClient();
  const { data: credentials = [], isLoading } = useQuery({ queryKey: ['website-credentials'], queryFn: api.websiteCredentials });
  const [editing, setEditing] = useState<WebsiteCredential | null | undefined>(); const [error, setError] = useState('');
  const remove = async (credential: WebsiteCredential) => {
    if (!await dialog.confirm({ title: `删除“${credential.name}”？`, message: '加密保存的认证信息会被永久删除；之后新建会话将无法使用它。', confirmLabel: '确认删除', tone: 'danger' })) return;
    try { await api.deleteWebsiteCredential(credential.id); await client.invalidateQueries({ queryKey: ['website-credentials'] }); } catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败'); }
  };
  return <section className="page credentials-page"><div className="page-head"><div><span className="eyebrow">AUTHENTICATION MANAGEMENT</span><h1>认证管理</h1><p>为网站访问保存受控认证信息。创建 Agent 会话时以 OpenHands Secret 注入，不会写入镜像。</p></div><button className="primary" onClick={() => setEditing(null)}><Plus size={16}/>新增认证</button></div><div className="credential-policy"><ShieldCheck size={19}/><div><b>域名匹配策略</b><span>默认仅精确匹配主机名；子域匹配必须由该条目显式开启。模型看到域名与变量名，不会看到 Secret 明文。</span></div></div>{error && <p className="notice error">{error}</p>}{isLoading ? <div className="empty">加载中…</div> : credentials.length === 0 ? <div className="empty credential-empty"><KeyRound size={26}/><b>尚未保存认证信息</b><span>添加站点认证后，新创建的 Agent 会话可在匹配的站点访问中使用它。</span></div> : <div className="credential-grid">{credentials.map(credential => <article className="credential-card" key={credential.id}><header><span className="credential-icon">{credential.auth_type === 'USERNAME_PASSWORD' ? <UserRound size={18}/> : <KeyRound size={18}/>}</span><div><h3>{credential.name}</h3><small><Globe2 size={11}/>{credential.target_host}{credential.include_subdomains ? ' · 包含子域' : ' · 仅此主机'}</small></div><div className="card-actions"><button title="编辑" aria-label={`编辑 ${credential.name}`} onClick={() => setEditing(credential)}><Pencil size={16}/></button><button className="danger" title="删除" aria-label={`删除 ${credential.name}`} onClick={() => void remove(credential)}><Trash2 size={16}/></button></div></header><dl><div><dt>认证方式</dt><dd>{credential.auth_type === 'USERNAME_PASSWORD' ? '用户名与密码' : 'Bearer Token'}</dd></div><div><dt>会话变量</dt><dd><code>{Object.values(credential.environment_names).join(' · ')}</code></dd></div><div><dt>Secret</dt><dd>{credential.secret_hint ? `已保存 ••••${credential.secret_hint}` : '已加密保存'}</dd></div></dl></article>)}</div>}{editing !== undefined && <CredentialEditor credential={editing ?? undefined} onClose={() => setEditing(undefined)}/>}</section>;
}
