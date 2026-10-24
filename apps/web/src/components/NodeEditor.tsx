import { useQuery } from '@tanstack/react-query';
import { Check, ChevronDown, ChevronRight, Maximize2, Plus, Trash2, X } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api/client';
import type { IOField, NodeAsset, NodeAssetWrite, NodeDirectory } from '../types';
import { useEscapeClose } from './useEscapeClose';

const emptyField = (direction: 'input' | 'output', index: number): IOField => ({
  field_key: `${direction}_${index + 1}`, display_name: '', data_type: 'URL', description: '',
});

const emptyNode = (): NodeAssetWrite => ({
  directory_id: null, name: '', description: '', icon_kind: 'LUCIDE', icon_value: 'bot',
  row_version: 1, inputs: [], outputs: [],
  executor: {
    startup_prompt: '读取流程输入和节点上下文，按当前任务要求完成工作。',
    context_prompt: '优先读取流程上游产物与人工输入，引用证据时标注来源。',
    context_capability_ids: [],
  },
});

type FieldDirection = 'inputs' | 'outputs';
type PromptKey = 'startup_prompt' | 'context_prompt';
type EditorTarget = FieldDirection | PromptKey;
const promptLabels: Record<PromptKey, string> = { startup_prompt: '启动触发提示词', context_prompt: '上下文提示词' };

function DirectoryPicker({ value, directories, onChange }: {
  value: string | null; directories: NodeDirectory[]; onChange: (value: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const optionsId = useId();
  const options = [{ id: '', name: '未分类' }, ...directories];
  const [activeIndex, setActiveIndex] = useState(0);
  useEscapeClose(() => setOpen(false), open);
  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => { if (!host.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, [open]);
  const choose = (id: string) => { onChange(id || null); setOpen(false); trigger.current?.focus(); };
  return <div className="node-directory-picker" ref={host} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <button ref={trigger} type="button" role="combobox" aria-label="所属目录" aria-haspopup="listbox" aria-expanded={open} aria-controls={optionsId} aria-activedescendant={open ? `${optionsId}-${activeIndex}` : undefined} onClick={() => { setActiveIndex(Math.max(0, options.findIndex(item => item.id === (value ?? '')))); setOpen(current => !current); }} onKeyDown={event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const selected = Math.max(0, options.findIndex(item => item.id === (value ?? '')));
        setActiveIndex(current => open ? (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length : selected);
        setOpen(true);
      } else if (open && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); choose(options[activeIndex].id); }
    }}><span>{options.find(item => item.id === (value ?? ''))?.name ?? '未分类'}</span><ChevronDown size={15}/></button>
    {open && <div className="node-directory-options" id={optionsId} role="listbox" aria-label="所属目录">{options.map((item, index) => <button id={`${optionsId}-${index}`} type="button" role="option" aria-selected={item.id === (value ?? '')} className={index === activeIndex ? 'active' : ''} key={item.id} onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(item.id)}><span>{item.name}</span>{item.id === (value ?? '') && <Check size={14}/>}</button>)}</div>}
  </div>;
}

function ConfigDialog({ title, description, children, onApply, onClose }: {
  title: string; description: string; children: ReactNode; onApply: (event: FormEvent<HTMLFormElement>) => void; onClose: () => void;
}) {
  const titleId = useId();
  const form = useRef<HTMLFormElement>(null);
  useEscapeClose(onClose);
  useEffect(() => {
    const previous = document.activeElement;
    (form.current?.querySelector<HTMLElement>('textarea, input') ?? form.current?.querySelector<HTMLElement>('button'))?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  return createPortal(<div className="modal-backdrop node-config-backdrop"><form ref={form} className="modal node-config-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} onSubmit={onApply} onKeyDown={event => {
    if (event.key !== 'Tab') return;
    const items = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]')];
    const target = event.shiftKey ? items.at(-1) : items[0];
    if (document.activeElement === (event.shiftKey ? items[0] : items.at(-1))) { event.preventDefault(); target?.focus(); }
  }}><header><div><h2 id={titleId}>{title}</h2><p>{description}</p></div><button type="button" className="ghost" aria-label={`关闭${title}`} onClick={onClose}><X size={18}/></button></header>{children}<footer><button type="button" className="secondary" onClick={onClose}>取消</button><button type="submit" className="primary">应用</button></footer></form></div>, document.body);
}

function FieldsDialog({ direction, fields, onApply, onClose }: {
  direction: FieldDirection; fields: IOField[]; onApply: (fields: IOField[]) => void; onClose: () => void;
}) {
  const [draft, setDraft] = useState(() => fields.map(field => ({ ...field })));
  const [error, setError] = useState('');
  const input = direction === 'inputs';
  const label = input ? '输入' : '输出';
  const update = (index: number, patch: Partial<IOField>) => { setError(''); setDraft(current => current.map((field, i) => i === index ? { ...field, ...patch } : field)); };
  return <ConfigDialog title={`${label}定义`} description={input ? '配置运行时的输入字段。应用后，点击“保存节点”保存配置。' : '配置 Agent 返回的链接或工作区文件。应用后，点击“保存节点”保存配置。'} onClose={onClose} onApply={event => {
    event.preventDefault();
    if (new Set(draft.map(field => field.field_key)).size !== draft.length) { setError('字段标识不能重复。'); return; }
    onApply(draft);
  }}><div className="node-config-body"><div className="node-fields-toolbar"><span>{draft.length} 个{label}字段</span><button type="button" className="secondary" onClick={() => {
    const prefix = input ? 'input' : 'output';
    let index = 0;
    while (draft.some(field => field.field_key === `${prefix}_${index + 1}`)) index += 1;
    setDraft(current => [...current, emptyField(prefix, index)]);
  }}><Plus size={14}/>添加{label}</button></div><div className="node-config-fields">{draft.map((field, index) => <section className="node-config-field" key={index} aria-label={`${label}字段 ${index + 1}`}><header><b>{label}字段 {index + 1}</b><button type="button" className="ghost node-field-remove" aria-label={`移除 ${direction} 字段 ${field.field_key}`} onClick={() => { setError(''); setDraft(current => current.filter((_, i) => i !== index)); }}><Trash2 size={14}/></button></header><div className="node-config-field-grid"><label>字段标识<input required pattern="[A-Za-z][A-Za-z0-9_]{0,99}" title="以字母开头，只能包含字母、数字和下划线" aria-label={`${direction} key ${index}`} value={field.field_key} onChange={event => update(index, { field_key: event.target.value })}/></label><label>展示名称<input aria-label={`${direction} name ${index}`} value={field.display_name} onChange={event => update(index, { display_name: event.target.value })}/></label><div className="node-config-field-type"><span>类型</span><div className="io-type-toggle" role="radiogroup" aria-label={`${direction} type ${index}`}><button type="button" role="radio" aria-checked={field.data_type === 'URL'} className={field.data_type === 'URL' ? 'active' : ''} onClick={() => update(index, { data_type: 'URL' })}>URL</button><button type="button" role="radio" aria-checked={field.data_type === 'FILE'} className={field.data_type === 'FILE' ? 'active' : ''} onClick={() => update(index, { data_type: 'FILE' })}>文件</button></div></div><label className="node-config-field-description">说明<textarea aria-label={`${direction} description ${index}`} value={field.description} onChange={event => update(index, { description: event.target.value })}/></label></div></section>)}</div>{!draft.length && <p className="node-fields-empty">暂未定义{label}，点击“添加{label}”开始配置。</p>}{error && <p role="alert" className="error">{error}</p>}</div></ConfigDialog>;
}

function PromptDialog({ title, value, onApply, onClose }: { title: string; value: string; onApply: (value: string) => void; onClose: () => void }) {
  const [draft, setDraft] = useState(value);
  return <ConfigDialog title={title} description="查看完整内容并编辑，应用后点击“保存节点”保存。" onClose={onClose} onApply={event => { event.preventDefault(); onApply(draft); }}><div className="node-config-body node-prompt-dialog-body"><textarea aria-label={title} value={draft} onChange={event => setDraft(event.target.value)} spellCheck={false}/></div></ConfigDialog>;
}

interface Props {
  node?: NodeAsset; initialDirectoryId?: string | null; embedded?: boolean;
  onSave: (data: NodeAssetWrite) => Promise<unknown>; onClose: () => void;
}

export function NodeEditor({ node, initialDirectoryId = null, embedded = false, onSave, onClose }: Props) {
  useEscapeClose(onClose);
  const [form, setForm] = useState<NodeAssetWrite>(emptyNode());
  const [activeEditor, setActiveEditor] = useState<EditorTarget>();
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });

  useEffect(() => {
    setActiveEditor(undefined);
    if (!node) { setForm({ ...emptyNode(), directory_id: initialDirectoryId }); return; }
    setForm({
      directory_id: node.directory_id, name: node.name, description: node.description,
      icon_kind: node.icon_kind, icon_value: node.icon_value, row_version: node.row_version,
      inputs: node.inputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
      outputs: node.outputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
      executor: node.executor ? { ...node.executor, context_capability_ids: [] } : emptyNode().executor,
    });
  }, [initialDirectoryId, node]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setSaving(true); setError('');
    try {
      await onSave({
        ...form,
        inputs: form.inputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
        outputs: form.outputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
        executor: { ...form.executor, context_capability_ids: [] },
      });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { submitting.current = false; setSaving(false); }
  };

  const editor = <form className={`modal editor asset-editor node-form${embedded ? ' node-detail-editor' : ''}`} onSubmit={submit}>
    <header className="asset-editor-header"><div className="node-detail-toolbar-label"><span className="eyebrow">NODE FORM</span><b>{node ? node.name : '新建节点'}</b></div><div className="node-detail-toolbar-actions"><button type="button" className="ghost" onClick={onClose}>取消</button><button type="submit" className="primary" disabled={saving}>{saving ? '保存中…' : '保存节点'}</button></div></header>
    <div className="node-form-body"><section className="node-form-basics"><h3>基础信息</h3><div className="node-form-grid"><label>节点名称<input aria-label="节点名称" required value={form.name} onChange={event => setForm(current => ({ ...current, name: event.target.value }))}/></label><div className="node-form-directory"><span>所属目录</span><DirectoryPicker value={form.directory_id ?? null} directories={directories} onChange={value => setForm(current => ({ ...current, directory_id: value }))}/></div><label className="node-form-description">节点说明<textarea aria-label="节点说明" value={form.description} onChange={event => setForm(current => ({ ...current, description: event.target.value }))}/></label></div></section>
    <section className="node-form-io"><h3>输入与输出</h3>{(['inputs', 'outputs'] as const).map(direction => <button type="button" className="node-fields-summary" key={direction} aria-label={`配置${direction === 'inputs' ? '输入' : '输出'}定义`} onClick={() => setActiveEditor(direction)}><b>{direction === 'inputs' ? '输入定义' : '输出定义'}</b><span>{form[direction].length ? form[direction].map(field => field.display_name || field.field_key).join('、') : '暂未配置'}</span><small>{form[direction].length} 个字段</small><ChevronRight size={16}/></button>)}</section>
    <section className="node-form-prompts"><h3>提示词与上下文</h3>{(['startup_prompt', 'context_prompt'] as const).map(key => <div className="node-prompt-field" key={key}><span>{promptLabels[key]}</span><button type="button" className="node-prompt-preview" aria-label={`查看和编辑${promptLabels[key]}`} onClick={() => setActiveEditor(key)}><span>{form.executor[key] || '暂无内容，点击编辑'}</span><Maximize2 size={14} aria-hidden="true"/></button></div>)}</section>
    {error && <p className="error" role="alert">{error}</p>}</div>
  </form>;

  return <>{embedded ? editor : <div className="modal-backdrop">{editor}</div>}{(activeEditor === 'inputs' || activeEditor === 'outputs') && <FieldsDialog key={activeEditor} direction={activeEditor} fields={form[activeEditor]} onClose={() => setActiveEditor(undefined)} onApply={fields => { setForm(current => ({ ...current, [activeEditor]: fields })); setActiveEditor(undefined); }}/>} {(activeEditor === 'startup_prompt' || activeEditor === 'context_prompt') && <PromptDialog key={activeEditor} title={promptLabels[activeEditor]} value={form.executor[activeEditor]} onClose={() => setActiveEditor(undefined)} onApply={value => { setForm(current => ({ ...current, executor: { ...current.executor, [activeEditor]: value } })); setActiveEditor(undefined); }}/>}</>;
}
