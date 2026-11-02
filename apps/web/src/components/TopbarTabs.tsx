import { Check, ChevronDown, ChevronsLeft, ChevronsRight, X, type LucideIcon } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type CSSProperties } from 'react';
import { ViewportPopover } from './ViewportPopover';
import { useEscapeClose } from './useEscapeClose';
import './topbar-tabs.css';

type Tab = { id: string; label: string; icon: LucideIcon; active: boolean; title?: string; onSelect: () => void };
export type TemporaryTab = Tab & { onClose: () => void };
const TAB_MAX_WIDTH = 168;
const TAB_MIN_WIDTH = 92;
// icon + gaps + close button; labels are mostly CJK at 13px.
const tabWidth = (label: string) => {
  let text = 0;
  for (const char of label) text += char.charCodeAt(0) > 255 ? 13 : 7.2;
  return Math.min(TAB_MAX_WIDTH, Math.max(TAB_MIN_WIDTH, Math.ceil(text + 62)));
};
const TAB_GAP = 2;
const OVERFLOW_WIDTH = 28;
const preferenceKey = (userId: string) => `flowweave.topbar.fixed-collapsed.v1:${userId}`;

function TabMenu({ label, items, children, hover = false, overflow = false, className = '', onSelect }: {
  label: string; items: Tab[]; children: ReactNode; hover?: boolean; overflow?: boolean; className?: string;
  onSelect?: (item: Tab) => void;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const focusMenu = useRef(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cancelClose = () => { clearTimeout(closeTimer.current); };
  const close = () => { cancelClose(); setOpen(false); };
  // Grace period long enough to cross the trigger-to-menu gap; entering either surface cancels it.
  const enter = () => { cancelClose(); if (hover) setOpen(true); };
  const leave = () => { if (hover) { cancelClose(); closeTimer.current = setTimeout(() => setOpen(false), 280); } };
  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEscapeClose(() => { close(); trigger.current?.focus(); }, open);
  useLayoutEffect(() => {
    if (open && focusMenu.current) {
      focusMenu.current = false;
      (popover.current?.querySelector<HTMLButtonElement>('[aria-current="page"]') ?? popover.current?.querySelector<HTMLButtonElement>('[role="menuitem"]'))?.focus();
    }
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !trigger.current?.contains(event.target) && !popover.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  return <div className={`topbar-tab-menu ${className}`}>
    <button ref={trigger} type="button" className="topbar-tab-menu-trigger" onPointerEnter={enter} onPointerLeave={leave} aria-label={label} title={label} aria-haspopup="menu" aria-expanded={open} onKeyDown={event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); cancelClose(); focusMenu.current = true; setOpen(true); if (open) { (popover.current?.querySelector<HTMLButtonElement>('[aria-current="page"]') ?? popover.current?.querySelector<HTMLButtonElement>('[role="menuitem"]'))?.focus(); focusMenu.current = false; } }
    }} onClick={event => { focusMenu.current = event.detail === 0; cancelClose(); if (hover && !open) setOpen(true); else if (event.detail === 0 && open && hover) popover.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus(); else setOpen(current => !current); }}>{children}</button>
    {open && <ViewportPopover ref={popover} anchor={() => trigger.current} align={overflow ? 'end' : 'start'} gap={overflow ? 4 : 0} matchAnchorWidth={!overflow} heightLimit={360} className={`topbar-tab-popover${overflow ? ' topbar-context-overflow-popover' : ' topbar-fixed-popover'}`} role="menu" aria-label={label} onPointerEnter={enter} onPointerLeave={leave} onKeyDown={event => {
      const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : event.key === 'ArrowDown' ? (index + 1) % buttons.length : event.key === 'ArrowUp' ? (index - 1 + buttons.length) % buttons.length : undefined;
      if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
      if (event.key === 'Tab') close();
    }}>{items.map(item => <button type="button" role="menuitem" key={item.id} aria-current={item.active ? 'page' : undefined} title={item.title ?? item.label} onClick={() => { close(); trigger.current?.focus(); if (onSelect) onSelect(item); else item.onSelect(); }}><i className="topbar-tab-icon"><item.icon size={14}/></i><span>{item.label}</span>{item.active && <Check size={12}/>}</button>)}</ViewportPopover>}
  </div>;
}

export function TopbarTabs({ userId, fixedTabs, temporaryTabs, onPromote }: {
  userId: string; fixedTabs: Tab[]; temporaryTabs: TemporaryTab[]; onPromote: (id: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(preferenceKey(userId)) === 'true'; } catch { return false; }
  });
  const [fixedWidth, setFixedWidth] = useState(760);
  const [availableWidth, setAvailableWidth] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const fixedNav = useRef<HTMLElement>(null);
  const contextRegion = useRef<HTMLDivElement>(null);
  const tabButtons = useRef(new Map<string, HTMLButtonElement>());
  const focusTab = useRef<string | undefined>(undefined);
  const lastFixed = useRef(fixedTabs[0]?.id);
  const activeFixed = fixedTabs.find(tab => tab.active);
  const fixedSelection = activeFixed ?? fixedTabs.find(tab => tab.id === lastFixed.current) ?? fixedTabs[0];
  useEffect(() => { if (activeFixed) lastFixed.current = activeFixed.id; }, [activeFixed]);
  useEffect(() => {
    try { localStorage.setItem(preferenceKey(userId), String(collapsed)); } catch { /* presentation preference only */ }
  }, [collapsed, userId]);
  useLayoutEffect(() => {
    const nav = fixedNav.current;
    if (!nav) return;
    const measure = () => setFixedWidth(nav.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure); observer.observe(nav);
    return () => observer.disconnect();
  }, []);
  const hasTemporary = temporaryTabs.length > 0;
  useLayoutEffect(() => {
    const region = contextRegion.current;
    if (!region) return;
    const measure = () => setAvailableWidth(region.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure); observer.observe(region);
    return () => observer.disconnect();
  }, [hasTemporary]);
  const fit = (room: number) => {
    let used = 0; let count = 0;
    for (const tab of temporaryTabs) {
      const next = used + (count ? TAB_GAP : 0) + tabWidth(tab.label);
      if (next > room && count > 0) break;
      used = next; count += 1;
    }
    return Math.max(1, count);
  };
  const overflow = temporaryTabs.length > fit(availableWidth);
  const capacity = overflow ? fit(availableWidth - OVERFLOW_WIDTH) : temporaryTabs.length;
  const visible = temporaryTabs.slice(0, capacity);
  const visibleWidth = visible.reduce((width, tab, index) => width + tabWidth(tab.label) + (index ? TAB_GAP : 0), 0);
  const hidden = temporaryTabs.slice(capacity);
  const activeTemporaryId = temporaryTabs.find(tab => tab.active)?.id;
  useLayoutEffect(() => {
    if (availableWidth > 0 && activeTemporaryId && !visible.some(tab => tab.id === activeTemporaryId)) onPromote(activeTemporaryId);
    const target = focusTab.current && tabButtons.current.get(focusTab.current);
    if (target) { target.focus(); focusTab.current = undefined; }
  }, [activeTemporaryId, availableWidth, onPromote, visible]);
  return <div ref={root} className={`topbar-tabs${hasTemporary ? ' has-context-tabs' : ''}${collapsed ? ' fixed-collapsed' : ''}`} style={{ '--fixed-nav-width': `${fixedWidth}px` } as CSSProperties}>
    <div className="topbar-fixed-region">
      <div className="topbar-fixed-scroll" inert={collapsed} aria-hidden={collapsed}><nav ref={fixedNav} aria-label="主导航">{fixedTabs.map(item => <button type="button" key={item.id} className={item.active ? 'active' : ''} aria-current={item.active ? 'page' : undefined} onClick={item.onSelect}><item.icon size={15}/>{item.label}</button>)}</nav></div>
      {collapsed && <TabMenu className={`topbar-fixed-menu${activeFixed ? ' active' : ''}`} hover label="选择固定页面" items={fixedTabs}>{fixedSelection && <><fixedSelection.icon size={15}/><span>{fixedSelection.label}</span></>}<ChevronDown size={11}/></TabMenu>}
    </div>
    <button type="button" className="topbar-nav-boundary" aria-label={collapsed ? '展开固定导航' : '收起固定导航'} title={collapsed ? '展开固定导航' : '收起固定导航'} aria-expanded={!collapsed} onClick={() => setCollapsed(current => !current)}>{collapsed ? <ChevronsRight size={12}/> : <ChevronsLeft size={12}/>}</button>
    {hasTemporary && <div className="topbar-context-region" ref={contextRegion}>
      <div className="topbar-context-content" style={{ width: hidden.length ? visibleWidth + OVERFLOW_WIDTH : visibleWidth }}>
        <div className="context-tabs" aria-label="临时管理标签">{visible.map(tab => <div className={`context-tab${tab.active ? ' active' : ''}`} key={tab.id} data-tab-id={tab.id} style={{ width: tabWidth(tab.label) }}>
        <button ref={element => { if (element) tabButtons.current.set(tab.id, element); else tabButtons.current.delete(tab.id); }} type="button" className="context-tab-select" aria-current={tab.active ? 'page' : undefined} title={tab.title ?? tab.label} onClick={tab.onSelect}><tab.icon size={14}/><span>{tab.label}</span></button>
        <button type="button" className="context-tab-close" aria-label={`关闭${tab.label}`} title={`关闭${tab.label}`} onClick={event => {
          if (event.detail === 0) {
            const index = temporaryTabs.indexOf(tab);
            focusTab.current = (temporaryTabs[index + 1] ?? temporaryTabs[index - 1])?.id;
            if (!focusTab.current) (root.current?.querySelector<HTMLButtonElement>('.topbar-fixed-menu>button') ?? fixedNav.current?.querySelector<HTMLButtonElement>('[aria-current="page"],button'))?.focus();
          }
          tab.onClose();
        }}><X size={12}/></button>
        </div>)}</div>
        {hidden.length > 0 && <TabMenu className="topbar-context-overflow" hover overflow label={`其他临时标签（${hidden.length}）`} items={hidden} onSelect={item => { focusTab.current = item.id; onPromote(item.id); item.onSelect(); }}><ChevronsRight size={13}/></TabMenu>}
      </div>
    </div>}
  </div>;
}
