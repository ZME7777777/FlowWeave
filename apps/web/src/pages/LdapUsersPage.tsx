import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Building2,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FolderTree,
  RefreshCw,
  ShieldCheck,
  UsersRound,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { PlatformUserProfileDialog } from '../components/PlatformUserProfileDialog';
import type { LdapOrganization, LdapUser, PlatformUser } from '../types';

type OrganizationNode = LdapOrganization & {
  children: OrganizationNode[];
  directUsers: number;
  totalUsers: number;
  enabledUsers: number;
  agentSessionUsers: number;
};

const PLATFORM_DIRECTORY = '__flowweave_platform__';
const EMPTY_PLATFORM_USERS: PlatformUser[] = [];
type ManagedUser = LdapUser | PlatformUser;
const isPlatformUser = (user: ManagedUser): user is PlatformUser => 'id' in user;
const EMPTY_USERS: LdapUser[] = [];
const EMPTY_ORGANIZATIONS: LdapOrganization[] = [];

function buildOrganizationTree(organizations: LdapOrganization[], users: LdapUser[]) {
  const nodes = new Map<string, OrganizationNode>();
  for (const item of organizations) {
    nodes.set(item.id, { ...item, children: [], directUsers: 0, totalUsers: 0, enabledUsers: 0, agentSessionUsers: 0 });
  }
  for (const user of users) {
    const node = user.organization_id ? nodes.get(user.organization_id) : undefined;
    if (node) {
      node.directUsers += 1;
      node.totalUsers += 1;
      if (user.enabled) node.enabledUsers += 1;
      if (user.agent_session_access) node.agentSessionUsers += 1;
    }
  }
  const roots: OrganizationNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parent_id ? nodes.get(node.parent_id) : undefined;
    if (parent && parent.id !== node.id) parent.children.push(node);
    else roots.push(node);
  }
  const order = (left: OrganizationNode, right: OrganizationNode) =>
    left.name.localeCompare(right.name, 'zh-CN', { sensitivity: 'base' });
  const aggregate = (node: OrganizationNode, path: Set<string>) => {
    if (path.has(node.id)) return;
    const nextPath = new Set(path).add(node.id);
    node.children.sort(order);
    for (const child of node.children) {
      aggregate(child, nextPath);
      node.totalUsers += child.totalUsers;
      node.enabledUsers += child.enabledUsers;
      node.agentSessionUsers += child.agentSessionUsers;
    }
  };
  roots.sort(order);
  for (const root of roots) aggregate(root, new Set());
  return { roots, nodes };
}

function OrganizationTreeNode({
  node,
  depth,
  selectedId,
  expanded,
  onSelect,
  onToggle,
  onAgentSessionAccessChange,
  agentSessionAccessPending,
}: {
  node: OrganizationNode;
  depth: number;
  selectedId: string | null;
  expanded: Set<string>;
  onSelect: (id: string) => void;
  onToggle: (id: string) => void;
  onAgentSessionAccessChange: (id: string, enabled: boolean) => void;
  agentSessionAccessPending: boolean;
}) {
  const hasChildren = node.children.length > 0;
  const isExpanded = expanded.has(node.id);
  return <li>
    <div className={`ldap-org-row${selectedId === node.id ? ' selected' : ''}`} style={{ paddingLeft: 8 + depth * 16 }}>
      {hasChildren ? <button className="ldap-org-expander" type="button" aria-label={`${isExpanded ? '收起' : '展开'} ${node.name}`} aria-expanded={isExpanded} onClick={() => onToggle(node.id)}>{isExpanded ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button> : <span className="ldap-org-expander-spacer"/>}
      <button className="ldap-org-select" type="button" onClick={() => onSelect(node.id)} title={node.name}><Building2 size={14}/><span>{node.name}</span><small>{node.totalUsers}</small>{node.agent_session_access && !node.agent_session_direct_access && <em>继承</em>}</button>
      <label className="ldap-org-agent-toggle" title={`允许 ${node.name} 及其下级组织使用 Agent 会话`}><Bot size={12}/><input type="checkbox" aria-label={`允许 ${node.name} 组织使用 Agent 会话`} checked={node.agent_session_direct_access} disabled={agentSessionAccessPending} onChange={event => onAgentSessionAccessChange(node.id, event.target.checked)}/><span/></label>
    </div>
    {hasChildren && isExpanded && <ul>{node.children.map(child => <OrganizationTreeNode key={child.id} node={child} depth={depth + 1} selectedId={selectedId} expanded={expanded} onSelect={onSelect} onToggle={onToggle} onAgentSessionAccessChange={onAgentSessionAccessChange} agentSessionAccessPending={agentSessionAccessPending}/>)}</ul>}
  </li>;
}

export function LdapUsersPage() {
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const [editingUser, setEditingUser] = useState<PlatformUser | null>(null);
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const directory = useQuery({ queryKey: ['ldap-directory'], queryFn: api.ldapDirectory });
  const loginUpdate = useMutation({
    mutationFn: ({ externalSubject, enabled }: { externalSubject: string; enabled: boolean }) =>
      api.setLdapUserEnabled(externalSubject, enabled),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['ldap-directory'] }); },
  });
  const userAgentSessionUpdate = useMutation({
    mutationFn: ({ externalSubject, enabled }: { externalSubject: string; enabled: boolean }) =>
      api.setLdapUserAgentSessionAccess(externalSubject, enabled),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['ldap-directory'] }); },
  });
  const organizationAgentSessionUpdate = useMutation({
    mutationFn: ({ organizationId, enabled }: { organizationId: string; enabled: boolean }) =>
      api.setLdapOrganizationAgentSessionAccess(organizationId, enabled),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['ldap-directory'] }); },
  });
  const users = directory.data?.users ?? EMPTY_USERS;
  const platformUsers = directory.data?.platform_users ?? EMPTY_PLATFORM_USERS;
  const allUsers = useMemo<ManagedUser[]>(() => [...users, ...platformUsers], [users, platformUsers]);
  const organizations = directory.data?.organizations ?? EMPTY_ORGANIZATIONS;
  const tree = useMemo(() => buildOrganizationTree(organizations, users), [organizations, users]);

  useEffect(() => {
    if (tree.roots.length === 0) return;
    setExpanded(current => current.size > 0 ? current : new Set(tree.roots.map(item => item.id)));
  }, [tree.roots]);
  useEffect(() => {
    if (selectedOrganizationId && selectedOrganizationId !== PLATFORM_DIRECTORY && !tree.nodes.has(selectedOrganizationId)) setSelectedOrganizationId(null);
  }, [selectedOrganizationId, tree.nodes]);

  const descendantIds = useMemo(() => {
    if (!selectedOrganizationId || selectedOrganizationId === PLATFORM_DIRECTORY) return null;
    const result = new Set<string>();
    const visit = (node: OrganizationNode) => {
      if (result.has(node.id)) return;
      result.add(node.id);
      node.children.forEach(visit);
    };
    const selected = tree.nodes.get(selectedOrganizationId);
    if (selected) visit(selected);
    return result;
  }, [selectedOrganizationId, tree.nodes]);
  const organizationPaths = useMemo(() => {
    const result = new Map<string, string>();
    const organizationsById = new Map(organizations.map(item => [item.id, item]));
    for (const organization of organizations) {
      const path: string[] = [];
      const visited = new Set<string>();
      let current: LdapOrganization | undefined = organization;
      while (current && !visited.has(current.id)) {
        visited.add(current.id);
        path.unshift(current.name);
        current = current.parent_id ? organizationsById.get(current.parent_id) : undefined;
      }
      result.set(organization.id, path.join(' / '));
    }
    return result;
  }, [organizations]);
  const visible = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return allUsers.filter(item => {
      const platform = isPlatformUser(item);
      if (selectedOrganizationId === PLATFORM_DIRECTORY && !platform) return false;
      if (descendantIds && (platform || !item.organization_id || !descendantIds.has(item.organization_id))) return false;
      return !term || [item.username, item.display_name ?? '', item.email ?? '', platform ? 'FlowWeave 平台' : item.organization_id ? organizationPaths.get(item.organization_id) ?? '' : ''].some(value => value.toLocaleLowerCase().includes(term));
    });
  }, [allUsers, descendantIds, organizationPaths, query, selectedOrganizationId]);
  const enabledCount = allUsers.filter(item => item.enabled).length;
  const agentSessionCount = allUsers.filter(item => item.enabled && item.agent_session_access).length;
  const selectedOrganization = selectedOrganizationId ? tree.nodes.get(selectedOrganizationId) : undefined;
  const toggleExpanded = (id: string) => setExpanded(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return <section className="page ldap-users-page">
    <header className="ldap-users-header">
      <div><span className="eyebrow">USER DIRECTORY</span><h1>用户管理</h1><p>管理 LDAP 用户授权和平台账户资料。</p></div>
      <div className="ldap-users-metrics"><span><ShieldCheck size={15}/>{enabledCount} 个可登录</span><span><Bot size={15}/>{agentSessionCount} 个可用 Agent</span><button className="secondary" disabled={directory.isFetching} onClick={() => void directory.refetch()}><RefreshCw size={14} className={directory.isFetching ? 'spin' : ''}/>刷新目录</button></div>
    </header>
    <div className="ldap-users-notice"><ShieldCheck size={17}/><span>LDAP 用户可按账号或组织授权；FlowWeave 平台账户按既有角色访问，可编辑姓名和邮箱。</span></div>
    {directory.isLoading ? <div className="empty">正在读取用户目录…</div> : directory.isError ? <div className="empty error">{directory.error.message}</div> : <div className="ldap-directory-layout">
      <aside className="ldap-org-panel">
        <div className="ldap-org-panel-title"><FolderTree size={16}/><b>用户目录</b><small>右侧开关授权 Agent</small><span>{organizations.length + 1}</span></div>
        <div className={`ldap-org-row ldap-org-all${selectedOrganizationId === null ? ' selected' : ''}`}><span className="ldap-org-expander-spacer"/><button className="ldap-org-select" type="button" onClick={() => setSelectedOrganizationId(null)}><UsersRound size={14}/><span>全部用户</span><small>{allUsers.length}</small>{enabledCount > 0 && <i>{enabledCount}</i>}</button></div>
        <div className="ldap-org-tree-scroll"><ul className="ldap-org-tree">{tree.roots.map(node => <OrganizationTreeNode key={node.id} node={node} depth={0} selectedId={selectedOrganizationId} expanded={expanded} onSelect={setSelectedOrganizationId} onToggle={toggleExpanded} onAgentSessionAccessChange={(organizationId, enabled) => organizationAgentSessionUpdate.mutate({ organizationId, enabled })} agentSessionAccessPending={organizationAgentSessionUpdate.isPending}/>)}<li><div className={`ldap-org-row${selectedOrganizationId === PLATFORM_DIRECTORY ? ' selected' : ''}`}><span className="ldap-org-expander-spacer"/><button type="button" className="ldap-org-select" title="FlowWeave 平台" onClick={() => setSelectedOrganizationId(PLATFORM_DIRECTORY)}><Building2 size={14}/><span>FlowWeave 平台</span><small>{platformUsers.length}</small></button></div></li></ul></div>
      </aside>
      <div className="ldap-user-panel">
        <div className="ldap-users-toolbar"><div><b>{selectedOrganizationId === PLATFORM_DIRECTORY ? 'FlowWeave 平台' : selectedOrganization?.name ?? '全部用户'}</b><span>{selectedOrganizationId === PLATFORM_DIRECTORY ? `${platformUsers.length} 个平台账户` : selectedOrganization ? `包含下级组织 · ${selectedOrganization.totalUsers} 人` : `${allUsers.length} 个用户`}</span></div><label>搜索用户<input value={query} onChange={event => setQuery(event.target.value)} placeholder="按账号、姓名、邮箱或组织搜索"/></label></div>
        <div className="ldap-user-table-wrap"><table className="ldap-user-table"><thead><tr><th>允许登录</th><th>Agent 会话</th><th>账号</th><th>姓名</th><th>组织</th><th>邮箱</th><th>状态</th><th>操作</th></tr></thead><tbody>{visible.map(item => {
          if (isPlatformUser(item)) return <tr key={`platform-${item.id}`}>
            <td><span className="ldap-access granted">{item.enabled ? item.role === 'SUPER_ADMIN' ? '默认允许' : '允许' : '未启用'}</span></td>
            <td><span className={item.agent_session_access ? 'ldap-access granted' : 'ldap-access'}>{item.agent_session_access ? item.role === 'SUPER_ADMIN' ? '默认允许' : '允许' : '未授权'}</span></td>
            <td><b>{item.username}</b></td><td>{item.display_name || '—'}</td><td>FlowWeave 平台</td><td>{item.email || '—'}</td><td><span className="ldap-access">平台账户</span></td>
            <td><button className="secondary" type="button" aria-label={`编辑 ${item.username} 的资料`} onClick={() => setEditingUser(item)}>编辑资料</button></td>
          </tr>;
          const loginChanging = loginUpdate.isPending && loginUpdate.variables?.externalSubject === item.external_subject;
          const agentChanging = userAgentSessionUpdate.isPending && userAgentSessionUpdate.variables?.externalSubject === item.external_subject;
          return <tr key={item.external_subject}><td><label className="ldap-user-toggle"><input type="checkbox" aria-label={`允许 ${item.username} 登录`} checked={item.enabled} disabled={loginChanging} onChange={event => loginUpdate.mutate({ externalSubject: item.external_subject, enabled: event.target.checked })}/><span/></label></td><td><div className="ldap-agent-access-cell"><label className="ldap-user-toggle"><input type="checkbox" aria-label={`单独允许 ${item.username} 使用 Agent 会话`} checked={item.agent_session_direct_access} disabled={agentChanging} onChange={event => userAgentSessionUpdate.mutate({ externalSubject: item.external_subject, enabled: event.target.checked })}/><span/></label><small>{item.agent_session_direct_access && item.agent_session_inherited_access ? '单独＋组织' : item.agent_session_inherited_access ? '组织授权' : item.agent_session_access ? '单独授权' : '未授权'}</small></div></td><td><b>{item.username}</b></td><td>{item.display_name}</td><td className="ldap-user-organization" title={item.organization_id ? organizationPaths.get(item.organization_id) : undefined}>{item.organization_id ? organizationPaths.get(item.organization_id) ?? '未归类' : '未归类'}</td><td>{item.email ?? '—'}</td><td><span className={item.enabled ? 'ldap-access granted' : 'ldap-access'}>{item.enabled ? <><CheckCircle2 size={13}/>可登录</> : '未开通'}</span></td><td><small>LDAP 同步</small></td></tr>;
        })}</tbody></table>{visible.length === 0 && <div className="empty compact"><UsersRound size={24}/><b>当前组织没有匹配用户</b><span>选择其他组织或调整搜索条件。</span></div>}</div>
      </div>
    </div>}
    {directory.data?.ldap_warning && <p className="notice">{directory.data.ldap_warning}</p>}
    {editingUser && <PlatformUserProfileDialog key={editingUser.id} user={editingUser} onClose={() => setEditingUser(null)}/>}
    {(loginUpdate.isError || userAgentSessionUpdate.isError || organizationAgentSessionUpdate.isError) && <p className="notice error">{loginUpdate.error?.message ?? userAgentSessionUpdate.error?.message ?? organizationAgentSessionUpdate.error?.message}</p>}
  </section>;
}
