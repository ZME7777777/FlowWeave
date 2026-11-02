import { Check, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { ViewportPopover } from '../ViewportPopover';
import { useEscapeClose } from '../useEscapeClose';
import type { ModelProvider, ProviderModel } from '../../types';

export function ComposerModelMenu({
  providers, providerId, modelName, models, efforts, effort, disabled, onProviderChange, onModelChange, onEffortChange,
}: {
  providers: ModelProvider[]; providerId: string; modelName: string; models: ProviderModel[]; efforts: string[];
  effort: string; disabled: boolean; onProviderChange: (value: string) => void; onModelChange: (value: string) => void;
  onEffortChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<'root' | 'provider' | 'model' | 'effort'>('root');
  const menu = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const close = () => { setOpen(false); setPanel('root'); trigger.current?.focus(); };
  useEscapeClose(close, open);
  useEffect(() => {
    if (disabled) { setOpen(false); setPanel('root'); }
  }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target) && !popover.current?.contains(event.target)) close();
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const selected = popover.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]');
    (selected ?? popover.current?.querySelector<HTMLButtonElement>('button:not(:disabled)'))?.focus();
  }, [open, panel]);
  const selectProvider = (value: string) => { onProviderChange(value); close(); };
  const selectModel = (value: string) => { onModelChange(value); close(); };
  const selectEffort = (value: string) => { onEffortChange(value); close(); };
  const currentProvider = providers.find(provider => provider.id === providerId)?.name ?? '选择供应商';
  const choices = [effort, ...efforts].filter((value, index, all): value is string => Boolean(value) && all.indexOf(value) === index);
  const compactSummary = [currentProvider, modelName || '选择模型', effort ? reasoningEffortLabel(effort) : '默认'].join(' ');
  const rootRow = (key: 'provider' | 'model' | 'effort', label: string, value: string, unavailable = false) => <button type="button" className="agent-model-picker-row" aria-label={`${label} ${value}`} disabled={disabled || unavailable} onClick={() => setPanel(key)}><span>{label}</span><em>{value}</em><ChevronRight size={15}/></button>;
  const option = (key: string, label: string, selected: boolean, action: () => void) => <button type="button" role="option" aria-selected={selected} key={key} className={`agent-model-picker-option${selected ? ' selected' : ''}`} onClick={action}><span>{label}</span>{selected && <Check size={15}/>}</button>;
  const panelTitle = panel === 'provider' ? '选择供应商' : panel === 'model' ? '选择模型' : '选择思考程度';
  const options = panel === 'provider'
    ? providers.map(provider => option(provider.id, provider.name, provider.id === providerId, () => selectProvider(provider.id)))
    : panel === 'model'
      ? models.map(model => option(model.model_name, model.model_name, model.model_name === modelName, () => selectModel(model.model_name)))
      : choices.map(value => option(value, reasoningEffortLabel(value), value === effort, () => selectEffort(value)));
  return <div ref={menu} className="agent-composer-model-menu">
    <button ref={trigger} type="button" className="agent-composer-model-trigger" aria-label={`打开模型与推理设置：${compactSummary}`} aria-expanded={open} disabled={disabled} onClick={() => { setOpen(current => !current); setPanel('root'); }}><span className="agent-composer-model-summary"><span>{compactSummary}</span></span><ChevronDown size={14}/></button>
    {open && <ViewportPopover ref={popover} anchor={() => trigger.current} align="end" placement="top" heightLimit={360} className="agent-composer-model-popover" aria-label={panel === 'root' ? '模型与推理设置' : panelTitle} onKeyDown={event => {
      const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : event.key === 'ArrowDown' ? (index + 1) % buttons.length : event.key === 'ArrowUp' ? (index - 1 + buttons.length) % buttons.length : undefined;
      if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
      if (event.key === 'Tab') setOpen(false);
    }}>{panel === 'root' ? <>
      {rootRow('provider', '供应商', currentProvider)}
      {rootRow('model', '模型', modelName || '选择模型', !providerId)}
      {efforts.length > 0 && rootRow('effort', '思考程度', effort ? reasoningEffortLabel(effort) : '默认', !providerId)}
      <p className="agent-model-picker-note">设置仅作用于当前会话。</p>
    </> : <><header><button type="button" aria-label="返回模型设置" onClick={() => setPanel('root')}><ChevronLeft size={16}/></button><b>{panelTitle}</b></header><div className="agent-model-picker-options" role="listbox" aria-label={panelTitle}>{options}</div></>}</ViewportPopover>}
  </div>;
}

function reasoningEffortLabel(value: string): string {
  return {
    low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', ultra: '极高',
  }[value] ?? value;
}
