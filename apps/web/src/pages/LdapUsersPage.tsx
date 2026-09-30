import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, RefreshCw, ShieldCheck, UsersRound } from 'lucide-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';

export function LdapUsersPage() {
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const users = useQuery({ queryKey: ['ldap-users'], queryFn: api.ldapUsers });
  const update = useMutation({
    mutationFn: ({ externalSubject, enabled }: { externalSubject: string; enabled: boolean }) =>
      api.setLdapUserEnabled(externalSubject, enabled),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['ldap-users'] }); },
  });
  const visible = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    if (!term) return users.data ?? [];
    return (users.data ?? []).filter(item =>
      [item.username, item.display_name, item.email ?? ''].some(value => value.toLocaleLowerCase().includes(term)),
    );
  }, [query, users.data]);
  const enabledCount = (users.data ?? []).filter(item => item.enabled).length;

  return <section className="page ldap-users-page">
    <header className="ldap-users-header">
      <div><span className="eyebrow">LDAP USER ACCESS</span><h1>用户管理</h1><p>从 LDAP 目录读取用户。仅已勾选的用户可使用公司账号登录 FlowWeave。</p></div>
      <div className="ldap-users-metrics"><span><ShieldCheck size={15}/>{enabledCount} 个已授权</span><button className="secondary" disabled={users.isFetching} onClick={() => void users.refetch()}><RefreshCw size={14} className={users.isFetching ? 'spin' : ''}/>刷新目录</button></div>
    </header>
    <div className="ldap-users-notice"><ShieldCheck size={17}/><span>登录仍由 LDAP 验证；本页面只管理 FlowWeave 的访问授权。取消勾选会立即撤销该用户现有会话。</span></div>
    <div className="ldap-users-toolbar"><label>搜索目录用户<input value={query} onChange={event => setQuery(event.target.value)} placeholder="按账号、姓名或邮箱搜索"/></label><span>{users.data?.length ?? 0} 个目录用户</span></div>
    {users.isLoading ? <div className="empty">正在读取 LDAP 用户目录…</div> : users.isError ? <div className="empty error">{users.error.message}</div> : <div className="ldap-user-table-wrap"><table className="ldap-user-table"><thead><tr><th>允许登录</th><th>账号</th><th>姓名</th><th>邮箱</th><th>状态</th></tr></thead><tbody>{visible.map(item => {
      const changing = update.isPending && update.variables?.externalSubject === item.external_subject;
      return <tr key={item.external_subject}><td><label className="ldap-user-toggle"><input type="checkbox" aria-label={`允许 ${item.username} 登录`} checked={item.enabled} disabled={changing} onChange={event => update.mutate({ externalSubject: item.external_subject, enabled: event.target.checked })}/><span/></label></td><td><b>{item.username}</b></td><td>{item.display_name}</td><td>{item.email ?? '—'}</td><td><span className={item.enabled ? 'ldap-access granted' : 'ldap-access'}>{item.enabled ? <><CheckCircle2 size={13}/>已授权</> : '未授权'}</span></td></tr>;
    })}</tbody></table>{visible.length === 0 && <div className="empty compact"><UsersRound size={24}/><b>没有匹配的目录用户</b><span>调整搜索条件后重试。</span></div>}</div>}
    {update.isError && <p className="notice error">{update.error.message}</p>}
  </section>;
}
