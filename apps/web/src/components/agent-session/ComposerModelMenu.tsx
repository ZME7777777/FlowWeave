import { Check, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { ViewportPopover } from '../ViewportPopover';
import { useEscapeClose } from '../useEscapeClose';
import type { ModelProvider, ProviderModel } from '../../types';

export function ComposerModelMenu({
  providers, providerId, modelName, models, efforts, effort, disabled, onSelect, onEffortChange,
}: {
  providers: ModelProvider[]; providerId: string; modelName: string; models: ProviderModel[]; efforts: string[];
  effort: string; disabled: boolean; onSelect: (providerId: string, modelName: string) => void;
  onEffortChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<'root' | 'effort'>('root');
  // Hover/focus only reveals a provider's models; clicking a model applies it at once.
  const [hoverProviderId, setHoverProviderId] = useState('');
  const hoverTimer = useRef<number | undefined>(undefined);
  const menu = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const cancelHover = () => { window.clearTimeout(hoverTimer.current); hoverTimer.current = undefined; };
  // Hover switches providers only after a short dwell so a diagonal move toward the models column does not flip them.
  const hoverProvider = (id: string) => { cancelHover(); hoverTimer.current = window.setTimeout(() => setHoverProviderId(id), 220); };
  useEffect(() => cancelHover, []);
  const reset = () => { cancelHover(); setPanel('root'); setHoverProviderId(''); };
  const close = () => { setOpen(false); reset(); trigger.current?.focus(); };
  useEscapeClose(close, open);
  useEffect(() => {
    if (disabled) { setOpen(false); reset(); }
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
  const currentProvider = providers.find(provider => provider.id === providerId)?.name ?? '选择供应商';
  const choices = [effort, ...efforts].filter((value, index, all): value is string => Boolean(value) && all.indexOf(value) === index);
  const compactSummary = [currentProvider, modelName || '选择模型', effort ? reasoningEffortLabel(effort) : '默认'].join(' ');
  const shownProviderId = hoverProviderId || providerId;
  const shownProvider = providers.find(provider => provider.id === shownProviderId);
  // Models of the current provider come from the caller (they may be frozen to the session); others from the catalog.
  const shownModels = (shownProviderId === providerId && models.length ? models : shownProvider?.models ?? []).filter(model => model.enabled);
  const target = { providerId, modelName };
  const choose = (nextProviderId: string, nextModelName: string) => {
    if (nextProviderId !== providerId || nextModelName !== modelName) onSelect(nextProviderId, nextModelName);
    close();
  };
  const selectEffort = (value: string) => { onEffortChange(value); close(); };
  const effortOptions = choices.map(value => <button type="button" role="option" aria-selected={value === effort} key={value} className={`agent-model-picker-option${value === effort ? ' selected' : ''}`} onClick={() => selectEffort(value)}><span>{reasoningEffortLabel(value)}</span>{value === effort && <Check size={15}/>}</button>);
  return <div ref={menu} className="agent-composer-model-menu">
    <button ref={trigger} type="button" className="agent-composer-model-trigger" aria-label={`打开模型与推理设置：${compactSummary}`} aria-expanded={open} disabled={disabled} onClick={() => { setOpen(current => !current); reset(); }}><span className="agent-composer-model-summary"><span>{compactSummary}</span></span><ChevronDown size={14}/></button>
    {open && <ViewportPopover ref={popover} anchor={() => trigger.current} align="end" placement="top" heightLimit={360} className={`agent-composer-model-popover${panel === 'root' ? ' cascade' : ''}`} aria-label={panel === 'root' ? '模型与推理设置' : '选择思考程度'} onKeyDown={event => {
      const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : event.key === 'ArrowDown' ? (index + 1) % buttons.length : event.key === 'ArrowUp' ? (index - 1 + buttons.length) % buttons.length : undefined;
      if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
      if (event.key === 'Tab') setOpen(false);
    }}>{panel === 'root' ? <>
      <div className="agent-model-cascade" onMouseLeave={() => { cancelHover(); setHoverProviderId(''); }}>
        <div className="agent-model-cascade-providers" role="listbox" aria-label="供应商">{providers.map(provider => {
          const active = provider.id === shownProviderId;
          return <button type="button" role="option" key={provider.id} aria-selected={provider.id === target.providerId} aria-label={`供应商 ${provider.name}`} className={`agent-model-picker-option${active ? ' hovered' : ''}${provider.id === target.providerId ? ' selected' : ''}`} onMouseEnter={() => hoverProvider(provider.id)} onFocus={() => { cancelHover(); setHoverProviderId(provider.id); }} onClick={() => { cancelHover(); setHoverProviderId(provider.id); }}><span>{provider.name}{provider.is_primary && <small className="agent-model-primary">主要</small>}</span><ChevronRight size={14}/></button>;
        })}{!providers.length && <p className="agent-model-picker-empty">暂无可用供应商</p>}</div>
        <div className="agent-model-cascade-models" role="listbox" aria-label="模型" onMouseEnter={cancelHover}>{shownModels.map(model => {
          const selected = target.providerId === shownProviderId && target.modelName === model.model_name;
          return <button type="button" role="option" key={model.model_name} aria-selected={selected} aria-label={`模型 ${model.model_name}`} className={`agent-model-picker-option${selected ? ' selected' : ''}`} onClick={() => choose(shownProviderId, model.model_name)}><span>{model.model_name}</span>{selected && <Check size={15}/>}</button>;
        })}{shownProvider && !shownModels.length && <p className="agent-model-picker-empty">该供应商没有可用模型</p>}</div>
      </div>
      {efforts.length > 0 && <button type="button" className="agent-model-picker-row" aria-label={`思考程度 ${effort ? reasoningEffortLabel(effort) : '默认'}`} disabled={disabled || !providerId} onClick={() => setPanel('effort')}><span>思考程度</span><em>{effort ? reasoningEffortLabel(effort) : '默认'}</em><ChevronRight size={15}/></button>}
      <footer className="agent-model-picker-footer"><span>设置仅作用于当前会话。</span></footer>
    </> : <><header><button type="button" aria-label="返回模型设置" onClick={() => setPanel('root')}><ChevronLeft size={16}/></button><b>选择思考程度</b></header><div className="agent-model-picker-options" role="listbox" aria-label="选择思考程度">{effortOptions}</div></>}</ViewportPopover>}
  </div>;
}

function reasoningEffortLabel(value: string): string {
  return {
    low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', ultra: '极高',
  }[value] ?? value;
}
