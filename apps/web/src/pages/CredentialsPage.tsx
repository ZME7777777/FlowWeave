import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, Globe2, KeyRound, Pencil, Plus, ShieldCheck, Trash2, UserRound } from 'lucide-react';
import { FormEvent, KeyboardEvent, useEffect, useId, useRef, useState } from 'react';
import { api } from '../api/client';
import { Pagination } from '../components/Pagination';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { WebsiteCredential, WebsiteCredentialWrite } from '../types';

const empty = (): WebsiteCredentialWrite => ({ name: '', target_host: '', include_subdomains: false, auth_type: 'USERNAME_PASSWORD', username: '', secret: '' });

function credentialPageSize() {
  const columns = window.innerWidth >= 1720 ? 4 : window.innerWidth >= 1280 ? 3 : window.innerWidth >= 760 ? 2 : 1;
  const rows = window.innerHeight >= 920 ? 4 : 3;
  return columns * rows;
}

const authTypeOptions = [
  { value: 'USERNAME_PASSWORD', label: '用户名与密码' },
  { value: 'BEARER_TOKEN', label: 'Bearer Token' },
] as const;

type AuthType = WebsiteCredentialWrite['auth_type'];

function AuthTypeSelect({ value, onChange }: { value: AuthType; onChange: (value: AuthType) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();
  const selected = authTypeOptions.find(option => option.value === value) ?? authTypeOptions[0];

  useEffect(() => {
    const closeIfOutside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', closeIfOutside);
    return () => document.removeEventListener('mousedown', closeIfOutside);
  }, []);

  const select = (next: AuthType) => {
    onChange(next);
    setOpen(false);
  };
  const move = (event: KeyboardEvent<HTMLButtonElement>, offset: number) => {
    event.preventDefault();
    const current = authTypeOptions.findIndex(option => option.value === value);
    const next = (current + offset + authTypeOptions.length) % authTypeOptions.length;
    select(authTypeOptions[next].value);
  };

  return <div className="credential-auth-select" ref={root}>
    <button
      type="button"
      className="credential-auth-trigger"
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls={listId}
      onClick={() => setOpen(current => !current)}
      onKeyDown={event => {
        if (event.key === 'ArrowDown') move(event, 1);
        else if (event.key === 'ArrowUp') move(event, -1);
        else if (event.key === 'Escape') setOpen(false);
      }}
    >
      <span>{selected.label}</span><ChevronDown size={16} aria-hidden="true"/>
    </button>
    {open && <div id={listId} className="credential-auth-options" role="listbox" aria-label="认证方式">
      {authTypeOptions.map(option => <button
        key={option.value}
        type="button"
        role="option"
        aria-selected={option.value === value}
        onClick={() => select(option.value)}
      >{option.label}</button>)}
    </div>}
  </div>;
}

function CredentialEditor({ credential, onClose }: { credential?: WebsiteCredential; onClose: () => void }) {
  useEscapeClose(onClose);
  const client = useQueryClient();
  const [form, setForm] = useState<WebsiteCredentialWrite>(empty());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // Existing secrets are deliberately never hydrated.  Keep separate evidence of a
  // user edit so a password manager's DOM-only (or synthetic) fill cannot become an
  // update request merely because the form is submitted.
  const editedUsername = useRef(false);
  const editedSecret = useRef(false);
  useEffect(() => {
    editedUsername.current = false;
    editedSecret.current = false;
    setForm(credential ? { name: credential.name, target_host: credential.target_host, include_subdomains: credential.include_subdomains, auth_type: credential.auth_type, username: '', secret: '', row_version: credential.row_version } : empty());
  }, [credential]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try {
      // On an edit, blank means retain the server-side value.  In particular, do not
      // transmit a value the browser may have filled into an existing credential form.
      const write = credential ? { ...form, username: editedUsername.current ? form.username : '', secret: editedSecret.current ? form.secret : '' } : form;
      if (credential) await api.updateWebsiteCredential(credential.id, write);
      else await api.createWebsiteCredential(form);
      await client.invalidateQueries({ queryKey: ['website-credentials'] }); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存认证信息失败'); }
    finally { setBusy(false); }
  };
  const usernamePassword = form.auth_type === 'USERNAME_PASSWORD';
  return <div className="modal-backdrop"><form className="modal credential-editor" autoComplete={credential ? 'off' : 'on'} onSubmit={submit}><header><div><span className="eyebrow">WEBSITE AUTHENTICATION</span><h2>{credential ? '编辑认证条目' : '新增认证条目'}</h2><p>Secret 会加密保存；保存后不再返回浏览器，也不会进入终端镜像。</p></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header><div className="form-grid"><label>名称<input required value={form.name} placeholder="例如：内部检索只读账号" onChange={event => setForm({ ...form, name: event.target.value })}/></label><label>认证方式<AuthTypeSelect value={form.auth_type} onChange={auth_type => setForm({ ...form, auth_type, username: '' })}/></label><label className="wide">目标域名<input required value={form.target_host} placeholder="search.example.com" autoCapitalize="none" onChange={event => setForm({ ...form, target_host: event.target.value })}/><small>填写主机名，不要包含 http(s)、路径、端口或用户名。</small></label><label className="wide credential-subdomain"><input type="checkbox" checked={form.include_subdomains} onChange={event => setForm({ ...form, include_subdomains: event.target.checked })}/><span><b>允许匹配子域名</b><small>仅开启后，api.search.example.com 才可使用 search.example.com 的条目。</small></span></label>{usernamePassword && <label>用户名<input required={!credential || Boolean(form.username)} name={credential ? 'website-credential-username' : 'username'} autoComplete={credential ? 'off' : 'username'} value={form.username ?? ''} placeholder={credential?.has_username ? '留空保留现有用户名' : '输入用户名'} onBeforeInput={() => { editedUsername.current = true; }} onChange={event => setForm({ ...form, username: event.target.value })}/></label>}<label className={usernamePassword ? '' : 'wide'}>{usernamePassword ? '密码' : 'Token'}<input type="password" required={!credential} name={credential ? 'website-credential-replacement-secret' : 'password'} autoComplete={credential ? 'new-password' : 'current-password'} value={form.secret ?? ''} placeholder={credential ? `留空保留现有 Secret ${credential.secret_hint ? `••••${credential.secret_hint}` : ''}` : usernamePassword ? '输入密码' : '输入 Token'} onBeforeInput={() => { editedSecret.current = true; }} onChange={event => setForm({ ...form, secret: event.target.value })}/></label>{error && <p className="error wide">{error}</p>}</div><footer><button type="button" className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy}>{busy ? '保存中…' : '保存认证信息'}</button></footer></form></div>;
}

export function CredentialsPage() {
  const dialog = useProductDialog(); const client = useQueryClient();
  const { data: credentials = [], isLoading } = useQuery({ queryKey: ['website-credentials'], queryFn: api.websiteCredentials });
  const [editing, setEditing] = useState<WebsiteCredential | null | undefined>(); const [error, setError] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(credentialPageSize);
  const pagedCredentials = credentials.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => { if (page > Math.max(1, Math.ceil(credentials.length / pageSize))) setPage(1); }, [credentials.length, page, pageSize]);
  useEffect(() => {
    const updatePageSize = () => setPageSize(credentialPageSize());
    window.addEventListener('resize', updatePageSize);
    return () => window.removeEventListener('resize', updatePageSize);
  }, []);
  const remove = async (credential: WebsiteCredential) => {
    if (!await dialog.confirm({ title: `删除“${credential.name}”？`, message: '加密保存的认证信息会被永久删除；之后新建会话将无法使用它。', confirmLabel: '确认删除', tone: 'danger' })) return;
    try { await api.deleteWebsiteCredential(credential.id); await client.invalidateQueries({ queryKey: ['website-credentials'] }); } catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败'); }
  };
  return <section className="page credentials-page">
    <div className="credential-fixed-tools">
      <div><span className="eyebrow">AUTHENTICATION MANAGEMENT</span><b>认证管理</b><small>网站认证仅在新建 Agent 会话时以 Secret 注入。</small></div>
      <div><span>共 {credentials.length} 个认证</span><button className="primary" onClick={() => setEditing(null)}><Plus size={16}/>新增认证</button></div>
    </div>
    <div className="credentials-page-scroll">
      <div className="credential-policy"><ShieldCheck size={17}/><span>默认精确匹配域名；勾选后才匹配子域名。Secret 不会返回浏览器或写入镜像。</span></div>
      {error && <p className="notice error">{error}</p>}
      {isLoading ? <div className="empty">加载中…</div> : credentials.length === 0 ? <div className="empty credential-empty"><KeyRound size={26}/><b>尚未保存认证信息</b><span>添加站点认证后，新创建的 Agent 会话可在匹配的站点访问中使用它。</span></div> : <>
        <div className="credential-grid compact-credential-list">{pagedCredentials.map(credential => <article className="credential-card" key={credential.id}>
          <span className="credential-icon">{credential.auth_type === 'USERNAME_PASSWORD' ? <UserRound size={17}/> : <KeyRound size={17}/>}</span>
          <div className="credential-summary"><h3>{credential.name}</h3><small><Globe2 size={11}/>{credential.target_host}{credential.include_subdomains ? ' · 包含子域' : ' · 仅此主机'}</small></div>
          <span className="credential-type">{credential.auth_type === 'USERNAME_PASSWORD' ? '用户名与密码' : 'Bearer Token'}</span>
          <div className="card-actions"><button title="编辑" aria-label={`编辑 ${credential.name}`} onClick={() => setEditing(credential)}><Pencil size={16}/></button><button className="danger" title="删除" aria-label={`删除 ${credential.name}`} onClick={() => void remove(credential)}><Trash2 size={16}/></button></div>
        </article>)}</div>
        <Pagination page={page} pageSize={pageSize} total={credentials.length} onPageChange={setPage}/>
      </>}
    </div>
    {editing !== undefined && <CredentialEditor credential={editing ?? undefined} onClose={() => setEditing(undefined)}/>}
  </section>;
}
