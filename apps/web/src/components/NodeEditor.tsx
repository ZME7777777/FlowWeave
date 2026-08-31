import { useQuery } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { CapabilityAsset, IOField, NodeAsset, NodeAssetWrite } from '../types';
import { useEscapeClose } from './useEscapeClose';

const emptyField = (direction: 'input' | 'output', index: number): IOField => ({
  field_key: `${direction}_${index + 1}`,
  display_name: '',
  data_type: 'URL',
  description: '',
});

const emptyNode = (): NodeAssetWrite => ({
  directory_id: null,
  name: '',
  description: '',
  icon_kind: 'LUCIDE',
  icon_value: 'bot',
  row_version: 1,
  inputs: [],
  outputs: [],
  executor: {
    startup_prompt: '读取流程输入和节点上下文，按当前任务要求完成工作。',
    context_prompt: '优先读取流程上游产物与人工输入，引用证据时标注来源。',
    context_capability_ids: [],
  },
});

interface Props {
  node?: NodeAsset;
  onSave: (data: NodeAssetWrite) => Promise<void>;
  onClose: () => void;
}

const tabs = ['基础信息', '提示词', '输入输出定义'];

export function NodeEditor({ node, onSave, onClose }: Props) {
  useEscapeClose(onClose);
  const [form, setForm] = useState<NodeAssetWrite>(emptyNode());
  const [tab, setTab] = useState(0);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: capabilities = [] } = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const contexts = capabilities.filter((item: CapabilityAsset) => item.capability_type === 'CONTEXT' && item.is_latest);

  useEffect(() => {
    if (!node) {
      setForm(emptyNode());
      return;
    }
    setForm({
      directory_id: node.directory_id,
      name: node.name,
      description: node.description,
      icon_kind: node.icon_kind,
      icon_value: node.icon_value,
      row_version: node.row_version,
      inputs: node.inputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
      outputs: node.outputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
      executor: node.executor
        ? { ...node.executor, context_capability_ids: node.executor.context_capability_ids ?? [] }
        : emptyNode().executor,
    });
  }, [node]);

  const updateField = (direction: 'inputs' | 'outputs', index: number, patch: Partial<IOField>) => setForm(old => ({
    ...old,
    [direction]: old[direction].map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item),
  }));
  const removeField = (direction: 'inputs' | 'outputs', index: number) => setForm(old => ({
    ...old,
    [direction]: old[direction].filter((_, itemIndex) => itemIndex !== index),
  }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setSaving(true);
    setError('');
    try {
      await onSave({
        ...form,
        inputs: form.inputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
        outputs: form.outputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败');
    } finally {
      submitting.current = false;
      setSaving(false);
    }
  };

  return <div className="modal-backdrop"><form className="modal editor asset-editor" onSubmit={submit}>
    <header><div><span className="eyebrow">NODE ASSET</span><h2>{node ? '编辑节点资产' : '新建节点资产'}</h2></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <div className="step-tabs">{tabs.map((label, index) => <button type="button" key={label} className={tab === index ? 'active' : ''} onClick={() => setTab(index)}><span>{index + 1}</span>{label}</button>)}</div>
    {tab === 0 && <section className="form-grid form-pane">
      <label>节点名称<input aria-label="节点名称" required value={form.name} onChange={event => setForm({ ...form, name: event.target.value })}/></label>
      <label>所属目录<select aria-label="所属目录" value={form.directory_id ?? ''} onChange={event => setForm({ ...form, directory_id: event.target.value || null })}><option value="">未分类</option>{directories.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label className="wide">节点说明<textarea aria-label="节点说明" value={form.description} onChange={event => setForm({ ...form, description: event.target.value })}/></label>
    </section>}
    {tab === 1 && <section className="form-grid form-pane">
      <label className="wide">启动触发提示词<textarea aria-label="启动触发提示词" value={form.executor.startup_prompt} onChange={event => setForm({ ...form, executor: { ...form.executor, startup_prompt: event.target.value } })}/></label>
      <label className="wide">上下文提示词<textarea aria-label="上下文提示词" value={form.executor.context_prompt} onChange={event => setForm({ ...form, executor: { ...form.executor, context_prompt: event.target.value } })}/></label>
      <label className="wide">Context 能力（可多选）<select aria-label="Context 能力" multiple value={form.executor.context_capability_ids} onChange={event => setForm({ ...form, executor: { ...form.executor, context_capability_ids: [...event.currentTarget.selectedOptions].map(option => option.value) } })}>{contexts.map(item => <option key={item.id} value={item.id}>{item.capability_key} · rev {item.revision_number}</option>)}</select><small>与上方自由文本一起作为 OpenHands 的系统级会话上下文冻结；不作为普通用户消息发送。</small></label>
    </section>}
    {tab === 2 && <section className="form-pane io-editor"><div className="io-intro"><b>定义输入输出</b><span>这里只定义运行时表单的数据槽位和类型。URL 接受安全 HTTP(S) 链接，文件以平台附件样式上传。</span></div>{(['inputs', 'outputs'] as const).map(direction => {
      const input = direction === 'inputs';
      return <section className={`io-section ${input ? 'input' : 'output'}`} key={direction}><header><div><span>{input ? 'INPUT' : 'OUTPUT'}</span><h3>{input ? '输入定义' : '输出定义'}</h3><p>{input ? '运行时严格按这里的字段生成一对一输入表单。' : 'Agent 按类型返回 URL，或返回本次工作区内创建的文件。'}</p></div><button type="button" className="secondary" onClick={() => setForm(old => ({ ...old, [direction]: [...old[direction], emptyField(input ? 'input' : 'output', old[direction].length)] }))}><Plus size={13}/>添加{input ? '输入' : '输出'}</button></header>{form[direction].length > 0 && <div className="io-column-head lark-doc-head" aria-hidden="true"><span>字段标识</span><span>展示名称</span><span>类型</span><span>说明</span><span>操作</span></div>}{form[direction].map((field, index) => <div className="io-row lark-doc-row" key={index}><input required pattern="[A-Za-z][A-Za-z0-9_]{0,99}" aria-label={`${direction} key ${index}`} value={field.field_key} placeholder={input ? 'requirement' : 'result'} title="以字母开头，只能包含字母、数字和下划线" onChange={event => updateField(direction, index, { field_key: event.target.value })}/><input aria-label={`${direction} name ${index}`} value={field.display_name} onChange={event => updateField(direction, index, { display_name: event.target.value })}/><div className="io-type-toggle" role="radiogroup" aria-label={`${direction} type ${index}`}><button type="button" role="radio" aria-checked={field.data_type === 'URL'} className={field.data_type === 'URL' ? 'active' : ''} onClick={() => updateField(direction, index, { data_type: 'URL' })}>URL</button><button type="button" role="radio" aria-checked={field.data_type === 'FILE'} className={field.data_type === 'FILE' ? 'active' : ''} onClick={() => updateField(direction, index, { data_type: 'FILE' })}>文件</button></div><input aria-label={`${direction} description ${index}`} value={field.description} placeholder={input ? '说明 Agent 如何使用输入' : '说明输出内容与验收要求'} onChange={event => updateField(direction, index, { description: event.target.value })}/><button type="button" className="ghost" aria-label={`移除 ${direction} 字段 ${field.field_key}`} onClick={() => removeField(direction, index)}><Trash2 size={14}/></button></div>)}{form[direction].length === 0 && <div className="io-empty">暂未定义{input ? '输入' : '输出'}。</div>}</section>;
    })}</section>}
    {error && <p className="error">{error}</p>}
    <footer><button type="button" className="ghost" onClick={onClose}>取消</button>{tab > 0 && <button type="button" className="secondary" onClick={() => setTab(tab - 1)}>上一步</button>}{tab < tabs.length - 1 ? <button type="button" className="primary" onClick={event => {
      // Moving onto the final step replaces this keyed position with the
      // submit button during the same click dispatch. Prevent the browser's
      // post-dispatch default action from submitting that reused DOM node.
      event.preventDefault();
      setTab(current => current + 1);
    }}>下一步</button> : <button type="submit" className="primary" disabled={saving}>{saving ? '保存中…' : '保存节点资产'}</button>}</footer>
  </form></div>;
}
