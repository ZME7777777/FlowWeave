import { ChevronRight, ArrowLeft, Pencil, Plus, Trash2, X } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { IOField, NodeAsset, NodeAssetWrite } from '../types';
import { useEscapeClose } from './useEscapeClose';

const emptyField = (): IOField => ({
  field_key: '', display_name: '', data_type: 'URL', description: '',
});

const emptyNode = (): NodeAssetWrite => ({
  directory_id: null, name: '', description: '', icon_kind: 'LUCIDE', icon_value: 'bot',
  row_version: 1, inputs: [], outputs: [],
  executor: {
    startup_prompt: '',
    context_prompt: '',
    context_capability_ids: [],
  },
});

const nodeWrite = (node: NodeAsset): NodeAssetWrite => ({
  directory_id: node.directory_id ?? null, name: node.name, description: node.description,
  icon_kind: node.icon_kind, icon_value: node.icon_value, row_version: node.row_version,
  inputs: node.inputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
  outputs: node.outputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
  executor: node.executor ? { ...node.executor, context_capability_ids: [] } : emptyNode().executor,
});

type FieldDirection = 'inputs' | 'outputs';
type PromptKey = 'startup_prompt' | 'context_prompt';
type EditorTarget = FieldDirection | PromptKey | 'description';
const promptLabels: Record<PromptKey, string> = { startup_prompt: '启动触发提示词', context_prompt: '上下文提示词' };

function ConfigDialog({ title, description, children, onApply, onClose, actions, busy = false, compact = false }: {
  title: string; description?: string; children: ReactNode; onApply: (event: FormEvent<HTMLFormElement>) => void; onClose: () => void; actions?: ReactNode; busy?: boolean; compact?: boolean;
}) {
  const titleId = useId();
  const form = useRef<HTMLFormElement>(null);
  useEscapeClose(() => { if (!busy) onClose(); });
  useEffect(() => {
    const previous = document.activeElement;
    (form.current?.querySelector<HTMLElement>('textarea, input') ?? form.current?.querySelector<HTMLElement>('button'))?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  return createPortal(<div className="modal-backdrop node-config-backdrop"><form ref={form} className={`modal node-config-dialog${compact ? ' node-fields-dialog' : ''}`} role="dialog" aria-modal="true" aria-labelledby={titleId} onSubmit={onApply} onKeyDown={event => {
    if (event.key !== 'Tab') return;
    const items = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]')];
    const target = event.shiftKey ? items.at(-1) : items[0];
    if (document.activeElement === (event.shiftKey ? items[0] : items.at(-1))) { event.preventDefault(); target?.focus(); }
  }}><header><div><h2 id={titleId}>{title}</h2>{description && <p>{description}</p>}</div><button type="button" className="ghost" aria-label={`关闭${title}`} disabled={busy} onClick={onClose}><X size={18}/></button></header>{children}{actions === undefined ? <footer><button type="button" className="secondary" onClick={onClose}>取消</button><button type="submit" className="primary">应用</button></footer> : actions}</form></div>, document.body);
}

function FieldsDialog({ direction, fields, onPersist, onClose }: {
  direction: FieldDirection; fields: IOField[]; onPersist: (fields: IOField[]) => Promise<void>; onClose: () => void;
}) {
  const [editing, setEditing] = useState<{ index: number; draft: IOField; creating: boolean }>();
  const [busyKey, setBusyKey] = useState<string>();
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const label = direction === 'inputs' ? '输入' : '输出';
  const update = (patch: Partial<IOField>) => { setError(''); setEditing(current => current ? { ...current, draft: { ...current.draft, ...patch } } : current); };
  const createField = () => {
    setError('');
    setEditing({ index: fields.length, draft: emptyField(), creating: true });
  };
  const persist = async (next: IOField[], key: string) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusyKey(key); setError('');
    try { await onPersist(next); setEditing(undefined); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败，请重试。'); }
    finally { inFlight.current = false; setBusyKey(undefined); }
  };
  const saveRecord = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editing || inFlight.current) return;
    const draft = { ...editing.draft, display_name: editing.draft.display_name.trim() };
    const next = editing.creating ? [...fields, draft] : fields.map((field, index) => index === editing.index ? draft : field);
    void persist(next, draft.field_key);
  };
  return <ConfigDialog title={editing ? `${editing.creating ? '添加' : '编辑'}${label}字段` : `${label}定义`} compact description={editing ? undefined : `点击一条${label}查看和编辑配置。`} onClose={onClose} busy={Boolean(busyKey)} onApply={saveRecord} actions={null}>
    <div className="node-config-body node-fields-body">
      {editing ? <>
        <button type="button" className="node-field-back" disabled={Boolean(busyKey)} onClick={() => { setEditing(undefined); setError(''); }}><ArrowLeft size={14}/>返回{label}列表</button>
        <fieldset className="node-field-editor" disabled={Boolean(busyKey)}>
          <div className="node-field-name-and-type"><label>名称<input required autoFocus autoComplete="off" aria-label={`${label}名称`} value={editing.draft.display_name} onChange={event => update({ display_name: event.target.value })}/></label><div className="node-config-field-type"><span>类型</span><div className="io-type-toggle" role="radiogroup" aria-label={`${label}类型`}><button type="button" role="radio" aria-checked={editing.draft.data_type === 'URL'} className={editing.draft.data_type === 'URL' ? 'active' : ''} onClick={() => update({ data_type: 'URL' })}>URL</button><button type="button" role="radio" aria-checked={editing.draft.data_type === 'FILE'} className={editing.draft.data_type === 'FILE' ? 'active' : ''} onClick={() => update({ data_type: 'FILE' })}>文件</button></div></div></div>
          <label className="node-field-description">说明<textarea aria-label={`${label}说明`} value={editing.draft.description} onChange={event => update({ description: event.target.value })}/></label>
          <div className="node-field-save"><button type="button" className="secondary" disabled={Boolean(busyKey)} onClick={() => { setEditing(undefined); setError(''); }}>取消</button><button type="submit" className="primary">{busyKey ? '正在保存…' : editing.creating ? '创建字段' : '保存修改'}</button></div>
        </fieldset>
      </> : <>
        <div className="node-fields-toolbar"><span>{fields.length} 个{label}字段</span></div>
        <div className="node-fields-table" role="table" aria-label={`${label}字段列表`}>
          <div className="node-fields-table-head" role="row"><span role="columnheader">名称</span><span role="columnheader">说明</span><span role="columnheader">操作</span></div>
          {fields.map((field, index) => {
            const name = field.display_name || '未命名字段';
            return <div className="node-field-record" role="row" key={field.field_key} aria-busy={busyKey === field.field_key}>
              <button type="button" className="node-field-open" disabled={Boolean(busyKey)} aria-label={`配置${label} ${name}`} onClick={() => { setError(''); setEditing({ index, draft: { ...field }, creating: false }); }}><b role="cell" title={name}>{name}</b><span role="cell" title={field.description}>{field.description || '暂无说明'}</span></button>
              <div className="node-field-actions" role="cell"><button type="button" disabled={Boolean(busyKey)} aria-label={`修改${label} ${name}`} onClick={() => { setError(''); setEditing({ index, draft: { ...field }, creating: false }); }}><Pencil size={14}/></button><button type="button" disabled={Boolean(busyKey)} className="node-field-delete" aria-label={`删除${label} ${name}`} onClick={() => void persist(fields.filter((_, i) => i !== index), field.field_key)}>{busyKey === field.field_key ? <span>…</span> : <Trash2 size={14}/>}</button></div>
            </div>;
          })}
        </div>
        <button type="button" className="node-field-create" disabled={Boolean(busyKey)} onClick={createField}><Plus size={14}/>新增{label}</button>
        {!fields.length && <p className="node-fields-empty">暂未定义{label}，请在下方新增。</p>}
      </>}
      {error && <p role="alert" className="error node-field-error">{error}</p>}
    </div>
  </ConfigDialog>;
}

function PromptDialog({ title, value, onApply, onClose }: { title: string; value: string; onApply: (value: string) => void; onClose: () => void }) {
  const [draft, setDraft] = useState(value);
  return <ConfigDialog title={title} description="查看完整内容并编辑，应用后点击“保存节点”保存。" onClose={onClose} onApply={event => { event.preventDefault(); onApply(draft); }}><div className="node-config-body node-prompt-dialog-body"><textarea aria-label={title} value={draft} onChange={event => setDraft(event.target.value)} spellCheck={false}/></div></ConfigDialog>;
}

function NodeDescriptionDialog({ value, onApply, onClose }: { value: string; onApply: (value: string) => void; onClose: () => void }) {
  const [draft, setDraft] = useState(value);
  return <ConfigDialog title="节点说明" description="查看完整内容并编辑，应用后点击“保存节点”保存。" onClose={onClose} onApply={event => { event.preventDefault(); onApply(draft); }}><div className="node-config-body node-prompt-dialog-body"><textarea aria-label="节点说明全文" value={draft} onChange={event => setDraft(event.target.value)}/></div></ConfigDialog>;
}

interface Props {
  node?: NodeAsset; initialDirectoryId?: string | null; embedded?: boolean;
  onSave: (data: NodeAssetWrite) => Promise<NodeAsset>; onClose: () => void;
}

export function NodeEditor({ node, initialDirectoryId = null, embedded = false, onSave, onClose }: Props) {
  useEscapeClose(onClose);
  const [form, setForm] = useState<NodeAssetWrite>(() => node ? nodeWrite(node) : { ...emptyNode(), directory_id: initialDirectoryId });
  const persisted = useRef(node);
  const [activeEditor, setActiveEditor] = useState<EditorTarget>();
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);

  const persistFields = async (direction: FieldDirection, fields: IOField[]) => {
    if (!persisted.current && !form.name.trim()) {
      setForm(current => ({ ...current, [direction]: fields }));
      return;
    }
    const base = persisted.current ? nodeWrite(persisted.current) : form;
    const saved = await onSave({ ...base, [direction]: fields });
    persisted.current = saved;
    setForm(current => ({ ...current, [direction]: saved[direction], row_version: saved.row_version }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setSaving(true); setError('');
    try {
      const saved = await onSave({
        ...form,
        inputs: form.inputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
        outputs: form.outputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
        executor: { ...form.executor, context_capability_ids: [] },
      });
      persisted.current = saved;
      setForm(nodeWrite(saved));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { submitting.current = false; setSaving(false); }
  };

  const editor = <form className={`modal editor asset-editor node-form${embedded ? ' node-detail-editor' : ''}`} onSubmit={submit}>
    <header className="asset-editor-header"><div className="node-detail-toolbar-label"><span className="eyebrow">NODE FORM</span><b>{node ? node.name : '新建节点'}</b></div><div className="node-detail-toolbar-actions"><button type="button" className="ghost" onClick={onClose}>取消</button><button type="submit" className="primary" disabled={saving}>{saving ? '保存中…' : '保存节点'}</button></div></header>
    <div className="node-form-body"><section className="node-form-basics"><h3>基础信息</h3><div className="node-form-grid"><label>节点名称<input aria-label="节点名称" required value={form.name} onChange={event => setForm(current => ({ ...current, name: event.target.value }))}/></label><article className="node-form-description"><header><span>节点说明</span><button type="button" aria-label="查看节点说明全文" aria-haspopup="dialog" onClick={() => setActiveEditor('description')}>查看全文<ChevronRight size={13}/></button></header><p>{form.description || '暂无内容'}</p></article></div></section>
    <section className="node-form-io"><h3>输入与输出</h3>{(['inputs', 'outputs'] as const).map(direction => <button type="button" className="node-fields-summary" key={direction} aria-label={`配置${direction === 'inputs' ? '输入' : '输出'}定义`} onClick={() => setActiveEditor(direction)}><b>{direction === 'inputs' ? '输入定义' : '输出定义'}</b><span>{form[direction].length ? form[direction].map(field => field.display_name || '未命名字段').join('、') : '暂未配置'}</span><small>{form[direction].length} 个字段</small><ChevronRight size={16}/></button>)}</section>
    <section className="node-form-prompts"><h3>提示词与上下文</h3>{(['startup_prompt', 'context_prompt'] as const).map(key => <article className="node-prompt-field" key={key}><header><span>{promptLabels[key]}</span><button type="button" aria-label={`查看${promptLabels[key]}全文`} aria-haspopup="dialog" onClick={() => setActiveEditor(key)}>查看全文<ChevronRight size={13}/></button></header><p>{form.executor[key] || '暂无内容'}</p></article>)}</section>
    {error && <p className="error" role="alert">{error}</p>}</div>
  </form>;

  return <>{embedded ? editor : <div className="modal-backdrop">{editor}</div>}{(activeEditor === 'inputs' || activeEditor === 'outputs') && <FieldsDialog key={activeEditor} direction={activeEditor} fields={form[activeEditor]} onClose={() => setActiveEditor(undefined)} onPersist={fields => persistFields(activeEditor, fields)}/>} {activeEditor === 'description' && <NodeDescriptionDialog value={form.description} onClose={() => setActiveEditor(undefined)} onApply={value => { setForm(current => ({ ...current, description: value })); setActiveEditor(undefined); }}/>} {(activeEditor === 'startup_prompt' || activeEditor === 'context_prompt') && <PromptDialog key={activeEditor} title={promptLabels[activeEditor]} value={form.executor[activeEditor]} onClose={() => setActiveEditor(undefined)} onApply={value => { setForm(current => ({ ...current, executor: { ...current.executor, [activeEditor]: value } })); setActiveEditor(undefined); }}/>}</>;
}
