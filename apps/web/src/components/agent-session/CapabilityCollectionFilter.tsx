import { Check, ChevronDown, Search } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { ViewportPopover } from '../ViewportPopover';
import { useEscapeClose } from '../useEscapeClose';

/** Collection membership narrows the catalog; it never enables a capability. */
export function CapabilityCollectionFilter({ collections, selectedIds, onChange }: {
  collections: Array<{ id: string; name: string }>; selectedIds: string[]; onChange: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const id = useId();
  const close = () => { setOpen(false); trigger.current?.focus(); };
  useEscapeClose(close, open);
  useEffect(() => { if (open) search.current?.focus(); }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !trigger.current?.contains(event.target) && !menu.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const visible = collections.filter(item => item.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <>
    <button ref={trigger} type="button" className={`session-collection-filter${selectedIds.length ? ' active' : ''}`} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} aria-label={selectedIds.length ? `Skill 组合筛选（${selectedIds.length}）` : 'Skill 组合筛选'} onClick={() => setOpen(current => !current)} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); } }}>Skill 组合{selectedIds.length > 0 && <b>{selectedIds.length}</b>}<ChevronDown size={12}/></button>
    {open && <ViewportPopover id={id} ref={menu} anchor={() => trigger.current} align="start" heightLimit={320} className="session-collection-popover" role="dialog" aria-label="筛选 Skill 组合" onKeyDown={event => {
      const options = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role=checkbox]')];
      const index = options.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'ArrowDown' ? (index + 1) % options.length : event.key === 'ArrowUp' ? (index - 1 + options.length) % options.length : event.key === 'Home' && index >= 0 ? 0 : event.key === 'End' && index >= 0 ? options.length - 1 : undefined;
      if (next !== undefined) { event.preventDefault(); options[next]?.focus(); }
      if (event.key === 'Tab') {
        const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),input')];
        const current = controls.indexOf(document.activeElement as HTMLElement);
        if (event.shiftKey && current === 0) { event.preventDefault(); controls.at(-1)?.focus(); }
        else if (!event.shiftKey && current === controls.length - 1) { event.preventDefault(); controls[0]?.focus(); }
      }
    }}>
      <label className="session-collection-search"><Search size={14}/><input ref={search} value={query} aria-label="搜索 Skill 组合" placeholder="搜索组合…" onChange={event => setQuery(event.target.value)}/></label>
      <div className="session-collection-options">{visible.length ? visible.map(item => {
        const checked = selectedIds.includes(item.id);
        return <button type="button" role="checkbox" aria-checked={checked} key={item.id} onClick={() => onChange(checked ? selectedIds.filter(id => id !== item.id) : [...selectedIds, item.id])}><i className={checked ? 'selected' : ''} aria-hidden="true">{checked && <Check size={12}/>}</i><span>{item.name}</span></button>;
      }) : <p>没有匹配的组合</p>}</div>
      <footer><button type="button" disabled={!selectedIds.length} onClick={() => onChange([])}>清除筛选</button><button type="button" onClick={close}>完成</button></footer>
    </ViewportPopover>}
  </>;
}
