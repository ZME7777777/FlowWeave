import { ViewportPopover } from './ViewportPopover';
import { Check, ChevronDown } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { useEscapeClose } from './useEscapeClose';

type LaunchMenuOption = { value: string; label: string };

export function LaunchOptionMenu({ label, value, options, disabled = false, placeholder = '未选择', onChange }: { label: string; value: string; options: LaunchMenuOption[]; disabled?: boolean; placeholder?: string; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  useEscapeClose(() => { setOpen(false); trigger.current?.focus(); }, open);
  useEffect(() => {
    if (open) (popover.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]') ?? popover.current?.querySelector<HTMLButtonElement>('[role="option"]'))?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (event.target instanceof Node && !host.current?.contains(event.target) && !popover.current?.contains(event.target)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  // Empty is not a valid choice. Showing the first provider for an empty value
  // made the fallback dialog look configured although no provider was selected.
  const selected = options.find(item => item.value === value)?.label ?? placeholder;
  return <div ref={host} className="launch-option-menu"><button ref={trigger} type="button" aria-haspopup="listbox" aria-controls={open ? listId : undefined} onKeyDown={event => { if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setOpen(true); } }} aria-label={label} aria-expanded={open} disabled={disabled} onClick={() => setOpen(current => !current)}><span>{selected}</span><ChevronDown size={15}/></button>{open && <ViewportPopover ref={popover} anchor={() => trigger.current} matchAnchorWidth heightLimit={230} id={listId} className="launch-option-popover" onKeyDown={event => {
    const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]')];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : event.key === 'ArrowDown' ? (index + 1) % buttons.length : event.key === 'ArrowUp' ? (index - 1 + buttons.length) % buttons.length : undefined;
    if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
    if (event.key === 'Tab') setOpen(false);
  }} role="listbox" aria-label={label}>{options.map(item => <button type="button" role="option" aria-selected={item.value === value} key={item.value} onClick={() => { onChange(item.value); setOpen(false); trigger.current?.focus(); }}><span>{item.label}</span>{item.value === value && <Check size={14}/>}</button>)}</ViewportPopover>}</div>;
}
