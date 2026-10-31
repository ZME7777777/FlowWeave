import { Search } from 'lucide-react';
import type { ReactNode } from 'react';
import './settings-management.css';

interface Props {
  title: string;
  count: number;
  query: string;
  onQueryChange: (query: string) => void;
  actions: ReactNode;
  selection?: ReactNode;
  list: ReactNode;
  listKey: string;
  pagination?: ReactNode;
  children: ReactNode;
  overlays?: ReactNode;
  className?: string;
}

export function SettingsManagementLayout({ title, count, query, onQueryChange, actions, selection, list, listKey, pagination, children, overlays, className = '' }: Props) {
  return <section className={`page settings-management-page ${className}`}>
    <aside className="settings-management-list" aria-label={`${title}列表`}>
      <header><h1>{title}</h1><span>{count} 项</span><div>{actions}</div></header>
      <label className="settings-management-search"><Search size={14}/><input aria-label={`搜索${title}`} placeholder="搜索名称或相关信息…" value={query} onChange={event => onQueryChange(event.target.value)}/></label>
      {selection && <div className="settings-management-selection">{selection}</div>}
      <div className="settings-management-list-scroll" key={listKey}>{list}</div>
      {pagination && <footer className="settings-management-pagination">{pagination}</footer>}
    </aside>
    <main className="settings-management-detail" aria-label={`${title}详情`}>{children}</main>
    {overlays}
  </section>;
}

export function SettingsResourceRow({ name, description, meta, active, onSelect, checked, onToggle }: {
  name: string; description: string; meta?: ReactNode; active: boolean; onSelect: () => void;
  checked?: boolean; onToggle?: () => void;
}) {
  const selectable = Boolean(onToggle);
  return <div className={`settings-resource-row${active ? ' active' : ''}${checked ? ' checked' : ''}${selectable ? ' selectable' : ''}`} onClick={selectable ? onToggle : undefined}>
    {onToggle && <input type="checkbox" aria-label={`选择 ${name}`} checked={checked} onClick={event => event.stopPropagation()} onChange={onToggle}/>}
    <div className="settings-resource-copy"><button type="button" className="settings-resource-name" aria-label={`查看 ${name}`} aria-pressed={active} onClick={event => { event.stopPropagation(); onSelect(); }}><b title={name}>{name}</b></button><small title={description}>{description}</small>{meta && <span className="settings-resource-meta">{meta}</span>}</div>
  </div>;
}
