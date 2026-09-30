import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Building2,
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
import type { LdapOrganization, LdapUser } from '../types';

type OrganizationNode = LdapOrganization & {
  children: OrganizationNode[];
  directUsers: number;
  totalUsers: number;
  enabledUsers: number;
};

const EMPTY_USERS: LdapUser[] = [];
const EMPTY_ORGANIZATIONS: LdapOrganization[] = [];

function buildOrganizationTree(organizations: LdapOrganization[], users: LdapUser[]) {
  const nodes = new Map<string, OrganizationNode>();
  for (const item of organizations) {
    nodes.set(item.id, { ...item, children: [], directUsers: 0, totalUsers: 0, enabledUsers: 0 });
  }
  for (const user of users) {
    const node = user.organization_id ? nodes.get(user.organization_id) : undefined;
    if (node) {
      node.directUsers += 1;
      node.totalUsers += 1;
      if (user.enabled) node.enabledUsers += 1;
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
}: {
  node: OrganizationNode;
  depth: number;
  selectedId: string | null;
  expanded: Set<string>;
  onSelect: (id: string) => void;
  onToggle: (id: string) => void;
}) {
  const hasChildren = node.children.length > 0;
  const isExpanded = expanded.has(node.id);
  return <li>
    <div className={`ldap-org-row${selectedId === node.id ? ' selected' : ''}`} style={{ paddingLeft: 8 + depth * 16 }}>
      {hasChildren ? <button className="ldap-org-expander" type="button" aria-label={`${isExpanded ? '收起' : '展开'} ${node.name}`} aria-expanded={isExpanded} onClick={() => onToggle(node.id)}>{isExpanded ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button> : <span className="ldap-org-expander-spacer"/>}
      <button className="ldap-org-select" type="button" onClick={() => onSelect(node.id)} title={node.name}><Building2 size={14}/><span>{node.name}</span><small>{node.totalUsers}</small>{node.enabledUsers > 0 && <i>{node.enabledUsers}</i>}</button>
    </div>
    {hasChildren && isExpanded && <ul>{node.children.map(child => <OrganizationTreeNode key={child.id} node={child} depth={depth + 1} selectedId={selectedId} expanded={expanded} onSelect={onSelect} onToggle={onToggle}/>)}</ul>}
  </li>;
}

export function LdapUsersPage() {
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const directory = useQuery({ queryKey: ['ldap-directory'], queryFn: api.ldapDirectory });
  const update = useMutation({
    mutationFn: ({ externalSubject, enabled }: { externalSubject: string; enabled: boolean }) =>
      api.setLdapUserEnabled(externalSubject, enabled),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['ldap-directory'] }); },
  });
  const users = directory.data?.users ?? EMPTY_USERS;
  const organizations = directory.data?.organizations ?? EMPTY_ORGANIZATIONS;
  const tree = useMemo(() => buildOrganizationTree(organizations, users), [organizations, users]);

  useEffect(() => {
    if (tree.roots.length === 0) return;
    setExpanded(current => current.size > 0 ? current : new Set(tree.roots.map(item => item.id)));
  }, [tree.roots]);
  useEffect(() => {
    if (selectedOrganizationId && !tree.nodes.has(selectedOrganizationId)) setSelectedOrganizationId(null);
  }, [selectedOrganizationId, tree.nodes]);

  const descendantIds = useMemo(() => {
    if (!selectedOrganizationId) return null;
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
    return users.filter(item => {
      if (descendantIds && (!item.organization_id || !descendantIds.has(item.organization_id))) return false;
      return !term || [item.username, item.display_name, item.email ?? '', item.organization_id ? organizationPaths.get(item.organization_id) ?? '' : ''].some(value => value.toLocaleLowerCase().includes(term));
    });
  }, [descendantIds, organizationPaths, query, users]);
  const enabledCount = users.filter(item => item.enabled).length;
  const selectedOrganization = selectedOrganizationId ? tree.nodes.get(selectedOrganizationId) : undefined;
  const toggleExpanded = (id: string) => setExpanded(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return <section className="page ldap-users-page">
    <header className="ldap-users-header">
      <div><span className="eyebrow">LDAP USER ACCESS</span><h1>用户管理</h1><p>按 LDAP 组织架构浏览用户。仅已授权用户可使用公司账号登录 FlowWeave。</p></div>
      <div className="ldap-users-metrics"><span><ShieldCheck size={15}/>{enabledCount} 个已授权</span><button className="secondary" disabled={directory.isFetching} onClick={() => void directory.refetch()}><RefreshCw size={14} className={directory.isFetching ? 'spin' : ''}/>刷新目录</button></div>
    </header>
    <div className="ldap-users-notice"><ShieldCheck size={17}/><span>登录仍由 LDAP 验证；本页面只管理 FlowWeave 的访问授权。取消授权会立即撤销该用户现有会话。</span></div>
    {directory.isLoading ? <div className="empty">正在读取 LDAP 组织目录…</div> : directory.isError ? <div className="empty error">{directory.error.message}</div> : <div className="ldap-directory-layout">
      <aside className="ldap-org-panel">
        <div className="ldap-org-panel-title"><FolderTree size={16}/><b>组织架构</b><span>{organizations.length}</span></div>
        <div className={`ldap-org-row ldap-org-all${selectedOrganizationId === null ? ' selected' : ''}`}><span className="ldap-org-expander-spacer"/><button className="ldap-org-select" type="button" onClick={() => setSelectedOrganizationId(null)}><UsersRound size={14}/><span>全部用户</span><small>{users.length}</small>{enabledCount > 0 && <i>{enabledCount}</i>}</button></div>
        <div className="ldap-org-tree-scroll"><ul className="ldap-org-tree">{tree.roots.map(node => <OrganizationTreeNode key={node.id} node={node} depth={0} selectedId={selectedOrganizationId} expanded={expanded} onSelect={setSelectedOrganizationId} onToggle={toggleExpanded}/>)}</ul></div>
      </aside>
      <div className="ldap-user-panel">
        <div className="ldap-users-toolbar"><div><b>{selectedOrganization?.name ?? '全部用户'}</b><span>{selectedOrganization ? `包含下级组织 · ${selectedOrganization.totalUsers} 人` : `${users.length} 个目录用户`}</span></div><label>搜索目录用户<input value={query} onChange={event => setQuery(event.target.value)} placeholder="按账号、姓名、邮箱或组织搜索"/></label></div>
        <div className="ldap-user-table-wrap"><table className="ldap-user-table"><thead><tr><th>允许登录</th><th>账号</th><th>姓名</th><th>组织</th><th>邮箱</th><th>状态</th></tr></thead><tbody>{visible.map(item => {
          const changing = update.isPending && update.variables?.externalSubject === item.external_subject;
          return <tr key={item.external_subject}><td><label className="ldap-user-toggle"><input type="checkbox" aria-label={`允许 ${item.username} 登录`} checked={item.enabled} disabled={changing} onChange={event => update.mutate({ externalSubject: item.external_subject, enabled: event.target.checked })}/><span/></label></td><td><b>{item.username}</b></td><td>{item.display_name}</td><td className="ldap-user-organization" title={item.organization_id ? organizationPaths.get(item.organization_id) : undefined}>{item.organization_id ? organizationPaths.get(item.organization_id) ?? '未归类' : '未归类'}</td><td>{item.email ?? '—'}</td><td><span className={item.enabled ? 'ldap-access granted' : 'ldap-access'}>{item.enabled ? <><CheckCircle2 size={13}/>已授权</> : '未授权'}</span></td></tr>;
        })}</tbody></table>{visible.length === 0 && <div className="empty compact"><UsersRound size={24}/><b>当前组织没有匹配用户</b><span>选择其他组织或调整搜索条件。</span></div>}</div>
      </div>
    </div>}
    {update.isError && <p className="notice error">{update.error.message}</p>}
  </section>;
}
