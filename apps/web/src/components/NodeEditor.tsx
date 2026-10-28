import { Maximize2, Plus, Trash2, X } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import type { IOField, NodeAsset, NodeAssetWrite } from '../types';
import { ConversationMarkdown } from './ConversationMarkdown';
import { useEscapeClose } from './useEscapeClose';
import './node-editor.css';

type PromptKey = 'startup_prompt' | 'context_prompt';
type EditorTab = PromptKey | 'fields';
type Direction = 'inputs' | 'outputs';
type DraftField = IOField & { draftId: string };
type NodeDraft = Omit<NodeAssetWrite, Direction> & Record<Direction, DraftField[]>;
export interface NodeDraftState { dirty: boolean; saving: boolean }
const promptLabels: Record<PromptKey, string> = { startup_prompt: '启动触发提示词', context_prompt: '上下文提示词' };

function initialDraft(node?: NodeAsset, directoryId: string | null = null): NodeDraft {
  return {
    directory_id: node?.directory_id ?? directoryId, name: node?.name ?? '', description: node?.description ?? '',
    icon_kind: node?.icon_kind ?? 'LUCIDE', icon_value: node?.icon_value ?? 'bot', row_version: node?.row_version ?? 1,
    inputs: (node?.inputs ?? []).map(field => ({ ...field, draftId: crypto.randomUUID() })),
    outputs: (node?.outputs ?? []).map(field => ({ ...field, draftId: crypto.randomUUID() })),
    executor: { startup_prompt: node?.executor?.startup_prompt ?? '', context_prompt: node?.executor?.context_prompt ?? '', context_capability_ids: [] },
  };
}
function writeDraft(draft: NodeDraft): NodeAssetWrite {
  const fields = (values: DraftField[]) => values.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description }));
  return { ...draft, inputs: fields(draft.inputs), outputs: fields(draft.outputs) };
}

function PromptContent({ promptKey, value, preview, disabled, onChange }: { promptKey: PromptKey; value: string; preview: boolean; disabled: boolean; onChange: (value: string) => void }) {
  return <div className="node-prompt-content">
    <textarea hidden={preview} aria-label={promptLabels[promptKey]} value={value} readOnly={disabled} placeholder={promptKey === 'startup_prompt' ? '描述节点要完成的任务、执行步骤和验收标准…' : '补充背景、约束与需要始终遵守的规则…'} spellCheck={false} onChange={event => onChange(event.target.value)}/>
    <div hidden={!preview} className="node-prompt-preview" aria-label={`${promptLabels[promptKey]}预览`}>{value ? <ConversationMarkdown>{value}</ConversationMarkdown> : <p className="node-prompt-empty">暂无内容，切换到编辑后添加。</p>}</div>
  </div>;
}
function PromptMode({ preview, onChange }: { preview: boolean; onChange: (preview: boolean) => void }) {
  return <div className="node-prompt-mode" role="group" aria-label="正文显示方式"><button type="button" aria-pressed={!preview} onClick={() => onChange(false)}>编辑</button><button type="button" aria-pressed={preview} onClick={() => onChange(true)}>预览</button></div>;
}
function ExpandedPrompt({ promptKey, value, disabled, error, onChange, onClose, onCancel, onSave }: { onCancel: () => void; error: string; promptKey: PromptKey; value: string; disabled: boolean; onChange: (value: string) => void; onClose: () => void; onSave: (event: KeyboardEvent) => void }) {
  const panel = useRef<HTMLElement>(null);
  const [preview, setPreview] = useState(false);
  const title = useId();
  useEscapeClose(() => { onCancel(); onClose(); });
  useEffect(() => {
    const previous = document.activeElement;
    panel.current?.querySelector('textarea')?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  return createPortal(<div className="modal-backdrop node-expanded-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section ref={panel} className="node-expanded-prompt" role="dialog" aria-modal="true" aria-labelledby={title} onKeyDown={event => {
    onSave(event);
    if (event.key !== 'Tab') return;
    const items = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not([hidden])')];
    if (document.activeElement === (event.shiftKey ? items[0] : items.at(-1))) { event.preventDefault(); (event.shiftKey ? items.at(-1) : items[0])?.focus(); }
  }}><header><b id={title}>{promptLabels[promptKey]}</b><span>修改与节点草稿同步</span><PromptMode preview={preview} onChange={setPreview}/><button type="button" className="ghost" aria-label="退出放大编辑" onClick={onClose}><X size={17}/></button></header><PromptContent promptKey={promptKey} value={value} preview={preview} disabled={disabled} onChange={onChange}/>{error && <p className="error node-save-error" role="alert">{error}</p>}<footer><small>⌘ / Ctrl + S 保存节点</small><button type="button" className="secondary" onClick={onClose}>完成</button></footer></section></div>, document.body);
}

interface Props {
  node?: NodeAsset; initialDirectoryId?: string | null; embedded?: boolean;
  onSave: (data: NodeAssetWrite) => Promise<NodeAsset>;
  onDraftStateChange?: (state: NodeDraftState) => void;
}
export function NodeEditor({ node, initialDirectoryId = null, embedded = false, onSave, onDraftStateChange }: Props) {
  const [form, setForm] = useState(() => initialDraft(node, initialDirectoryId));
  const [baseline, setBaseline] = useState(() => structuredClone(form));
  const [activeTab, setActiveTab] = useState<EditorTab>('startup_prompt');
  const [previews, setPreviews] = useState<Record<PromptKey, boolean>>({ startup_prompt: false, context_prompt: false });
  const [isCreated, setIsCreated] = useState(Boolean(node));
  const [expandedPrompt, setExpandedPrompt] = useState<PromptKey>();
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const formElement = useRef<HTMLFormElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const tabsId = useId();
  const dirty = JSON.stringify(writeDraft(form)) !== JSON.stringify(writeDraft(baseline));
  useLayoutEffect(() => { onDraftStateChange?.({ dirty, saving }); }, [dirty, saving, onDraftStateChange]);
  const cancelChanges = () => {
    if (submitting.current || !dirty) return;
    setForm(structuredClone(baseline)); setError('');
  };
  useEscapeClose(cancelChanges);
  useEffect(() => {
    if (!dirty && !saving) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, saving]);
  const updatePrompt = (key: PromptKey, value: string) => setForm(current => ({ ...current, executor: { ...current.executor, [key]: value } }));
  const updateField = (direction: Direction, id: string, patch: Partial<IOField>) => setForm(current => ({ ...current, [direction]: current[direction].map(field => field.draftId === id ? { ...field, ...patch } : field) }));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    if (!form.name.trim()) { setError('请填写节点名称。'); setExpandedPrompt(undefined); window.requestAnimationFrame(() => nameInput.current?.focus()); return; }
    if (form.name.length > 200) { setError('节点名称不能超过 200 个字符。'); nameInput.current?.focus(); return; }
    for (const direction of ['inputs', 'outputs'] as const) {
      const index = form[direction].findIndex(field => !field.display_name.trim() || field.display_name.length > 160);
      if (index !== -1) { setExpandedPrompt(undefined); setActiveTab('fields'); setError(`请填写${direction === 'inputs' ? '输入' : '输出'}字段名称，最多 160 个字符。`); window.requestAnimationFrame(() => formElement.current?.querySelector<HTMLInputElement>(`[data-field-direction="${direction}"] [data-field-name="${index}"]`)?.focus()); return; }
    }
    submitting.current = true; setSaving(true); setError('');
    try {
      const payload = writeDraft(form);
      payload.inputs.forEach(field => { field.display_name = field.display_name.trim(); });
      payload.outputs.forEach(field => { field.display_name = field.display_name.trim(); });
      const result = initialDraft(await onSave(payload));
      setForm(result); setBaseline(structuredClone(result)); setIsCreated(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败，请重试。'); }
    finally { submitting.current = false; setSaving(false); }
  };
  const shortcut = (event: KeyboardEvent) => {
    if (event.defaultPrevented || event.nativeEvent.isComposing || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 's') return;
    event.preventDefault(); formElement.current?.requestSubmit();
  };
  const fieldList = (direction: Direction) => {
    const label = direction === 'inputs' ? '输入' : '输出';
    return <section className="node-draft-fields" data-field-direction={direction} aria-label={`${label}定义`}><header><h3>{label}定义 <small>{form[direction].length}</small></h3><button type="button" className="secondary" disabled={saving} onClick={() => setForm(current => ({ ...current, [direction]: [...current[direction], { draftId: crypto.randomUUID(), field_key: '', display_name: '', data_type: 'URL', description: '' }] }))}><Plus size={13}/>新增{label}</button></header>
      {form[direction].length ? <div className="node-draft-field-list"><div className="node-draft-field-head" aria-hidden="true"><span>名称</span><span>类型</span><span>说明</span><span/></div>{form[direction].map((field, index) => <div className="node-draft-field-row" key={field.draftId}><input data-field-name={index} aria-label={`${label}名称 ${index + 1}`} value={field.display_name} maxLength={160} disabled={saving} placeholder="字段名称" onChange={event => updateField(direction, field.draftId, { display_name: event.target.value })}/><div className="node-field-type" role="group" aria-label={`${label}类型 ${index + 1}`}>{(['URL', 'FILE'] as const).map(type => <button key={type} type="button" aria-pressed={field.data_type === type} disabled={saving} onClick={() => updateField(direction, field.draftId, { data_type: type })}>{type === 'FILE' ? '文件' : 'URL'}</button>)}</div><textarea rows={1} aria-label={`${label}说明 ${index + 1}`} value={field.description} disabled={saving} placeholder="用途与要求" onChange={event => updateField(direction, field.draftId, { description: event.target.value })}/><button type="button" className="node-draft-field-delete" aria-label={`删除${label} ${field.display_name || `第 ${index + 1} 项`}`} disabled={saving} onClick={() => setForm(current => ({ ...current, [direction]: current[direction].filter(item => item.draftId !== field.draftId) }))}><Trash2 size={14}/></button></div>)}</div> : <p className="node-draft-fields-empty">暂无{label}字段</p>}
    </section>;
  };
  const editor = <form ref={formElement} className={`modal editor asset-editor node-form node-edit-workspace${embedded ? ' node-detail-editor' : ''}`} noValidate onSubmit={submit} onKeyDown={shortcut}>
    <header className="asset-editor-header"><div className={`node-detail-toolbar-label${isCreated ? ' node-fixed-metadata' : ''}`}>
      {isCreated ? <><h2 title={form.name}>{form.name}</h2>{form.description && <p title={form.description}>{form.description}</p>}</> : <input ref={nameInput} aria-label="节点名称" title="节点名称" size={Math.max(12, Math.min(36, form.name.length * 2 + 2))} value={form.name} maxLength={200} disabled={saving} placeholder="新建节点 · 输入名称" onChange={event => setForm(current => ({ ...current, name: event.target.value }))}/>}
    </div><div className="node-detail-toolbar-actions"><button type="button" className="secondary node-cancel-changes" disabled={!dirty || saving} onClick={cancelChanges} title="取消本次修改（Esc）">取消</button><button type="submit" className="primary" disabled={saving} title="⌘ / Ctrl + S">{saving ? '保存中…' : '保存节点'}</button></div></header>
    {!isCreated && <label className="node-creation-description">节点说明<textarea rows={2} aria-label="节点说明" value={form.description} disabled={saving} placeholder="简述节点用途，创建后名称与说明将固定。" onChange={event => setForm(current => ({ ...current, description: event.target.value }))}/></label>}
    <div className="node-editor-tabs" role="tablist" aria-label="节点配置">{(['startup_prompt', 'context_prompt', 'fields'] as const).map((tab, index, tabs) => <button key={tab} type="button" role="tab" id={`${tabsId}-${tab}`} aria-controls={`${tabsId}-panel-${tab}`} aria-selected={activeTab === tab} tabIndex={activeTab === tab ? 0 : -1} onClick={() => setActiveTab(tab)} onKeyDown={event => {
      const next = event.key === 'ArrowRight' ? tabs[(index + 1) % tabs.length] : event.key === 'ArrowLeft' ? tabs[(index + tabs.length - 1) % tabs.length] : event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : undefined;
      if (next) { event.preventDefault(); setActiveTab(next); document.getElementById(`${tabsId}-${next}`)?.focus(); }
    }}>{tab === 'fields' ? <>输入与输出 <small>{form.inputs.length} / {form.outputs.length}</small></> : tab === 'startup_prompt' ? '启动提示词' : '上下文'}</button>)}</div>
    {error && <p className="error node-save-error" role="alert">{error}</p>}
    {(['startup_prompt', 'context_prompt'] as const).map(key => <section key={key} hidden={activeTab !== key} className="node-editor-panel node-text-panel" role="tabpanel" id={`${tabsId}-panel-${key}`} aria-labelledby={`${tabsId}-${key}`}><div className="node-prompt-toolbar"><small>{key === 'startup_prompt' ? '任务、步骤与验收标准' : '背景信息与执行约束'}</small><span>{form.executor[key].length.toLocaleString()} 字符</span><PromptMode preview={previews[key]} onChange={value => setPreviews(current => ({ ...current, [key]: value }))}/><button type="button" className="node-prompt-expand" aria-label={`放大编辑${promptLabels[key]}`} onClick={() => setExpandedPrompt(key)}><Maximize2 size={13}/><span>放大</span></button></div><PromptContent promptKey={key} value={form.executor[key]} preview={previews[key]} disabled={saving} onChange={value => updatePrompt(key, value)}/></section>)}
    <section hidden={activeTab !== 'fields'} className="node-editor-panel node-fields-panel" role="tabpanel" id={`${tabsId}-panel-fields`} aria-labelledby={`${tabsId}-fields`}>{fieldList('inputs')}{fieldList('outputs')}<p className="node-fields-draft-note">字段修改随节点统一保存。</p></section>
  </form>;
  return <>{embedded ? editor : <div className="modal-backdrop">{editor}</div>}{expandedPrompt && <ExpandedPrompt promptKey={expandedPrompt} value={form.executor[expandedPrompt]} disabled={saving} error={error} onCancel={cancelChanges} onChange={value => updatePrompt(expandedPrompt, value)} onSave={shortcut} onClose={() => setExpandedPrompt(undefined)}/>}</>;
}
