import { Check, ChevronDown, ChevronRight } from 'lucide-react';
import { createPortal } from 'react-dom';
import { useEffect, useRef, useState } from 'react';
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
  const popover = useRef<HTMLElement>(null);
  const sidePanel = useRef<HTMLElement>(null);
  const [sidePosition, setSidePosition] = useState<{ left: number; top: number }>();
  useEffect(() => {
    if (disabled) { setOpen(false); setPanel('root'); }
  }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target) && !sidePanel.current?.contains(event.target)) close();
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, [open]);
  const close = () => { setOpen(false); setPanel('root'); };
  const selectProvider = (value: string) => { onProviderChange(value); close(); };
  const selectModel = (value: string) => { onModelChange(value); close(); };
  const selectEffort = (value: string) => { onEffortChange(value); close(); };
  const currentProvider = providers.find(provider => provider.id === providerId)?.name ?? '选择供应商';
  const choices = [effort, ...efforts].filter((value, index, all): value is string => Boolean(value) && all.indexOf(value) === index);
  const rootRow = (key: 'provider' | 'model' | 'effort', label: string, value: string, unavailable = false) => <button type="button" className="agent-model-picker-row" aria-label={`${label} ${value}`} disabled={disabled || unavailable} onClick={() => setPanel(key)}><span>{label}</span><em>{value}</em><ChevronRight size={15}/></button>;
  const option = (key: string, label: string, selected: boolean, action: () => void) => <button type="button" key={key} className={`agent-model-picker-option${selected ? ' selected' : ''}`} onClick={action}><span>{label}</span>{selected && <Check size={15}/>}</button>;
  const panelTitle = panel === 'provider' ? '选择供应商' : panel === 'model' ? '选择模型' : '选择思考程度';
  useEffect(() => {
    if (!open || panel === 'root') { setSidePosition(undefined); return; }
    const place = () => {
      const anchor = popover.current?.getBoundingClientRect();
      if (!anchor) return;
      const width = Math.min(320, Math.max(220, window.innerWidth - anchor.right - 20));
      setSidePosition({ left: Math.min(anchor.right + 8, window.innerWidth - width - 12), top: Math.max(12, anchor.top) });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open, panel]);
  const pickerPanel = panel !== 'root' && sidePosition && <section ref={sidePanel} className="agent-model-picker-side-panel agent-model-picker-side-panel-portal" aria-label={panelTitle} style={sidePosition}>
    <header><b>{panelTitle}</b></header>
    <div className="agent-model-picker-options">
      {panel === 'provider' && providers.map(provider => option(provider.id, provider.name, provider.id === providerId, () => selectProvider(provider.id)))}
      {panel === 'model' && models.map(model => option(model.model_name, model.model_name, model.model_name === modelName, () => selectModel(model.model_name)))}
      {panel === 'effort' && choices.map(value => option(value, reasoningEffortLabel(value), value === effort, () => selectEffort(value)))}
    </div>
  </section>;
  return <div ref={menu} className="agent-composer-model-menu" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); close(); } }}>
    <button type="button" className="agent-composer-model-trigger" aria-label="打开模型与推理设置" aria-expanded={open} disabled={disabled} onClick={() => { setOpen(current => !current); setPanel('root'); }}><span className="agent-composer-model-summary"><span>{modelName || '选择模型'}</span>{effort && <em>{reasoningEffortLabel(effort)}</em>}</span><ChevronDown size={14}/></button>
    {open && <div className={`agent-composer-model-flyout${panel === 'root' ? '' : ' has-side-panel'}`}>
      <section ref={popover} className="agent-model-picker-popover" aria-label="模型与推理设置">
        {rootRow('provider', '供应商', currentProvider)}
        {rootRow('model', '模型', modelName || '选择模型', !providerId)}
        {efforts.length > 0 && rootRow('effort', '思考程度', effort ? reasoningEffortLabel(effort) : '默认', !providerId)}
        <p className="agent-model-picker-note">设置仅作用于当前会话。</p>
      </section>
    </div>}
    {pickerPanel && createPortal(pickerPanel, document.body)}
  </div>;
}

function reasoningEffortLabel(value: string): string {
  return {
    low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', ultra: '极高',
  }[value] ?? value;
}
