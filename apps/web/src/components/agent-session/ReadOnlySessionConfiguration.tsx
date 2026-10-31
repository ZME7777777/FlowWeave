import { ArrowLeft, Check, ChevronRight, Eye, EyeOff, FileText, LockKeyhole, Search, X } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useEscapeClose } from '../useEscapeClose';
import { capabilityTypeFilters, capabilityTypeIcons, capabilityTypeLabels, type AgentCapabilityType } from './capabilityPresentation';
import '../../pages/agent-workbench.css';
import './read-only-session-configuration.css';

export interface FrozenSessionConfigurationItem {
  id: string;
  capability_key: string;
  capability_type: string;
  digest?: string;
  text?: string;
  source: string;
  applied: boolean;
}

/** Pure snapshot viewer: never loads the current catalog or invokes Runtime mutations. */
export function ReadOnlySessionConfiguration({ items, onClose }: { items: FrozenSessionConfigurationItem[]; onClose: () => void }) {
  const [kind, setKind] = useState<AgentCapabilityType | 'ALL'>('ALL');
  const [query, setQuery] = useState('');
  const [appliedOnly, setAppliedOnly] = useState(false);
  const [viewing, setViewing] = useState<FrozenSessionConfigurationItem>();
  const dialog = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const opener = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const detailOpener = useRef<string | undefined>(undefined);
  const heading = useRef<HTMLHeadingElement>(null);
  const id = useId();
  useEffect(() => {
    const element = opener.current;
    close.current?.focus();
    return () => element?.focus();
  }, []);
  useEffect(() => {
    if (viewing) heading.current?.focus();
    else [...(dialog.current?.querySelectorAll<HTMLButtonElement>('[data-frozen-item-id]') ?? [])].find(item => item.dataset.frozenItemId === detailOpener.current)?.focus();
  }, [viewing]);
  useEscapeClose(onClose);
  const needle = query.trim().toLocaleLowerCase();
  const visible = items.filter(item => (kind === 'ALL' || item.capability_type === kind)
    && (!appliedOnly || item.applied)
    && (!needle || `${item.capability_key} ${item.capability_type} ${item.source} ${item.digest ?? ''}`.toLocaleLowerCase().includes(needle)));
  const label = (item: FrozenSessionConfigurationItem) => capabilityTypeLabels[item.capability_type as AgentCapabilityType] ?? item.capability_type;
  return createPortal(<div className="agent-capability-backdrop" role="presentation" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialog} className="agent-capability-manager frozen-session-configuration" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const controls = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input, [tabindex="0"]') ?? [])].filter(item => item.offsetParent !== null);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}>
      <header><div><h2 id={`${id}-title`}>会话配置 <span className="frozen-session-badge"><LockKeyhole size={11}/>只读</span></h2><p>本轮执行冻结的上下文与能力</p></div><button ref={close} type="button" aria-label="关闭会话配置" onClick={onClose}><X size={18}/></button></header>
      {viewing ? <section className="frozen-session-detail" aria-label={`查看 ${label(viewing)} ${viewing.capability_key}`}>
        <button type="button" className="ghost" onClick={() => setViewing(undefined)}><ArrowLeft size={14}/>返回列表</button>
        <h3 ref={heading} tabIndex={-1}>{viewing.capability_key}</h3>
        <dl><dt>类型</dt><dd>{label(viewing)}</dd><dt>本轮状态</dt><dd>{viewing.applied ? '已装配 · 只读' : '未应用 · 只读'}</dd><dt>来源</dt><dd>{viewing.source}</dd><dt>{viewing.id === '__node_context_prompt__' ? '配置标识' : '版本 ID'}</dt><dd>{viewing.id}</dd>{viewing.digest && <><dt>内容摘要</dt><dd>{viewing.digest}</dd></>}</dl>
        {viewing.text !== undefined ? <><h4>冻结内容</h4><pre>{viewing.text || '未填写内容。'}</pre></> : <p className="frozen-session-detail-note">执行快照未包含此能力正文。</p>}
      </section> : <>
        <div className="agent-capability-toolbar"><nav className="agent-capability-tabs" aria-label="能力类型">{capabilityTypeFilters.map(([value, title]) => <button type="button" key={value} className={kind === value ? 'active' : ''} aria-pressed={kind === value} onClick={() => setKind(value)}>{title}</button>)}</nav><div className="agent-capability-toolbar-actions"><button type="button" className={`agent-capability-show-selected${appliedOnly ? ' active' : ''}`} aria-label={appliedOnly ? '显示全部冻结项' : '只看已装配'} title={appliedOnly ? '显示全部冻结项' : '只看已装配'} aria-pressed={appliedOnly} onClick={() => setAppliedOnly(current => !current)}>{appliedOnly ? <Eye size={16}/> : <EyeOff size={16}/>}</button><label className="agent-capability-search"><Search size={15}/><input aria-label="搜索冻结能力" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、类型或版本…"/></label></div></div>
        <div className="agent-capability-summary"><span>本轮已装配 <b>{items.filter(item => item.applied).length}</b> 项</span><span>当前显示 {visible.length} 项 · 执行快照</span></div>
        <div className="agent-capability-list" aria-label="冻结上下文与能力">{visible.length ? visible.map(item => {
          const Icon = capabilityTypeIcons[item.capability_type as AgentCapabilityType] ?? FileText;
          return <button type="button" key={`${item.capability_type}:${item.id}`} className={`frozen-capability-row${item.applied ? ' selected' : ' not-applied'}`} aria-label={`查看 ${item.capability_key}`} data-frozen-item-id={`${item.capability_type}:${item.id}`} onClick={() => { detailOpener.current = `${item.capability_type}:${item.id}`; setViewing(item); }}>
            <i className="agent-selection-checkbox" aria-hidden="true">{item.applied && <Check size={12}/>}</i>
            <span className={`agent-capability-icon ${item.capability_type.toLowerCase()}`} aria-hidden="true"><Icon size={16}/></span>
            <span className="agent-capability-copy"><span className="agent-capability-name"><b>{item.capability_key}</b>{item.capability_type === 'MCP' && item.capability_key === 'flowweave-reference-history' && <em className="agent-capability-builtin-badge">内置</em>}</span><small>{label(item)} · {item.applied ? '本轮已装配' : '本轮未应用'}{item.digest ? ` · ${item.digest.slice(0, 12)}` : ` · ${item.source}`}</small></span>
            <ChevronRight size={14} className="frozen-capability-details"/>
          </button>;
        }) : <p>{items.length ? '没有匹配的冻结配置。' : '本轮未记录上下文或能力。'}</p>}</div>
      </>}
      <footer><span><LockKeyhole size={12}/>冻结配置仅供查看</span><button type="button" className="secondary" onClick={onClose}>关闭</button></footer>
    </section>
  </div>, document.body);
}
