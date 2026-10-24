import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Check, ChevronDown, Search } from 'lucide-react';
import { useEscapeClose } from '../components/useEscapeClose';

type ChoiceOption = { value: string; label: string; detail?: string };

/** Search stays local; a typed value never becomes a saved selection. */
export function CaseChoice({ label, value, options, onChange, placeholder, disabled = false, emptyText = '没有匹配的选项' }: {
  label: string; value: string; options: ChoiceOption[]; onChange: (value: string) => void;
  placeholder?: string; disabled?: boolean; emptyText?: string;
}) {
  const id = useId();
  const host = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const selected = options.find(option => option.value === value);
  const filtered = options.filter(option => `${option.label} ${option.detail ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const expanded = open && !disabled;
  const activeIndex = Math.min(active, filtered.length - 1);
  const close = () => setOpen(false);
  useEscapeClose(close, expanded);
  useEffect(() => {
    if (!expanded) return;
    const outside = (event: PointerEvent) => { if (!host.current?.contains(event.target as Node)) close(); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [expanded]);
  useEffect(() => {
    if (expanded) host.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, expanded, query]);
  const reveal = () => { setOpen(true); setQuery(''); setActive(Math.max(0, options.findIndex(option => option.value === value))); };
  const choose = (next: string) => { onChange(next); close(); input.current?.focus(); };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!expanded) reveal();
      else setActive(current => Math.max(0, Math.min(filtered.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1))));
    } else if (expanded && (event.key === 'Home' || event.key === 'End')) {
      event.preventDefault(); setActive(event.key === 'Home' ? 0 : filtered.length - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (!expanded) reveal();
      else if (filtered[activeIndex]) choose(filtered[activeIndex].value);
    }
  };
  return <div className={`case-choice${expanded ? ' is-open' : ''}`} ref={host} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(); }}>
    <div className="case-choice-control">
      {expanded && <Search size={14} aria-hidden="true"/>}
      <input ref={input} role="combobox" aria-label={label} aria-expanded={expanded} aria-controls={`${id}-list`} aria-autocomplete="list"
        aria-activedescendant={expanded && filtered[activeIndex] ? `${id}-${activeIndex}` : undefined}
        disabled={disabled} autoComplete="off" value={expanded ? query : selected?.label ?? ''}
        placeholder={expanded ? `搜索${label}` : placeholder ?? `选择${label}`}
        onFocus={reveal} onClick={() => { if (!expanded) reveal(); }} onKeyDown={onKeyDown}
        onChange={event => { setQuery(event.target.value); setActive(0); setOpen(true); }}/>
      <button type="button" tabIndex={-1} disabled={disabled} aria-label={`展开${label}`} onMouseDown={event => event.preventDefault()} onClick={() => { if (expanded) close(); else { input.current?.focus(); reveal(); } }}><ChevronDown size={15} aria-hidden="true"/></button>
    </div>
    {expanded && <div className="case-choice-options" role="listbox" id={`${id}-list`} aria-label={label}>
      {filtered.map((option, index) => <button key={option.value} id={`${id}-${index}`} type="button" tabIndex={-1} role="option" aria-label={option.label} aria-selected={option.value === value} data-active={index === activeIndex}
        onMouseDown={event => event.preventDefault()} onMouseMove={() => setActive(index)} onClick={() => choose(option.value)}>
        <span><b>{option.label}</b>{option.detail && <small>{option.detail}</small>}</span>{option.value === value && <Check size={14} aria-hidden="true"/>}
      </button>)}
      {!filtered.length && <p>{emptyText}</p>}
    </div>}
  </div>;
}
