import { useQuery } from '@tanstack/react-query';
import { Plus, Trash2, Upload } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type { ArtifactDataType, CapabilityRef, IOField, NodeAsset, NodeAssetWrite } from '../types';

const DATA_TYPES: { value: ArtifactDataType; label: string }[] = [
  { value: 'TEXT', label: 'Text' },
  { value: 'MARKDOWN', label: 'Markdown' },
  { value: 'JSON_OBJECT', label: 'JSON Object' },
  { value: 'JSON_ARRAY', label: 'JSON Array' },
  { value: 'FILE', label: 'File' },
  { value: 'FILE_COLLECTION', label: 'File Collection' },
  { value: 'DOCUMENT', label: 'Document' },
  { value: 'URL', label: 'URL' },
  { value: 'REPOSITORY_REF', label: 'Repository Ref' },
];
const emptyField = (direction: 'input' | 'output', index: number): IOField => ({
  field_key: `${direction}_${index + 1}`,
  display_name: direction === 'input' ? '输入' : '输出',
  data_type: 'DOCUMENT',
  description: '',
});
const emptyNode = (): NodeAssetWrite => ({
  directory_id: null,
  name: '',
  description: '',
  icon_kind: 'LUCIDE',
  icon_value: 'bot',
  default_skill_ref: '',
  row_version: 1,
  inputs: [emptyField('input', 0)],
  outputs: [emptyField('output', 0)],
  executor: {
    model_provider_id: null,
    model_name: null,
    startup_prompt: '使用默认 Skill 执行本节点任务。先读取流程输入，再按 Skill 定义完成工作。',
    context_prompt: '优先读取流程上游产物与人工输入，引用证据时标注来源。',
    timeout_seconds: 900,
    max_iterations: 100,
  },
  capabilities: [],
});
interface Props {
  node?: NodeAsset;
  onSave: (data: NodeAssetWrite) => Promise<void>;
  onClose: () => void;
}
const tabs = ['基础信息', '模型与提示词', '能力导入', '输入输出定义'];

export function NodeEditor({ node, onSave, onClose }: Props) {
  const [form, setForm] = useState<NodeAssetWrite>(emptyNode());
  const [tab, setTab] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const submitting = useRef(false);
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: providers = [] } = useQuery({ queryKey: ['providers'], queryFn: api.providers });

  useEffect(() => {
    if (!node) { setForm(emptyNode()); return; }
    setForm({
      directory_id: node.directory_id,
      name: node.name,
      description: node.description,
      icon_kind: node.icon_kind,
      icon_value: node.icon_value,
      default_skill_ref: node.default_skill_ref ?? '',
      row_version: node.row_version,
      inputs: node.inputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
      outputs: node.outputs.map(({ field_key, display_name, data_type, description }) => ({ field_key, display_name, data_type, description })),
      executor: node.executor ?? emptyNode().executor,
      capabilities: node.capabilities.map(({ capability_type, capability_key, normalized_config }) => ({ capability_type, capability_key, normalized_config })),
    });
  }, [node]);

  const provider = providers.find(item => item.id === form.executor.model_provider_id);
  const skills = useMemo(() => form.capabilities.filter(item => item.capability_type === 'SKILL'), [form.capabilities]);
  const updateField = (direction: 'inputs' | 'outputs', index: number, patch: Partial<IOField>) => setForm(old => ({
    ...old,
    [direction]: old[direction].map((item, i) => i === index ? { ...item, ...patch } : item),
  }));
  const removeField = (direction: 'inputs' | 'outputs', index: number) => setForm(old => ({
    ...old, [direction]: old[direction].filter((_, i) => i !== index),
  }));
  const removeCapability = (index: number) => setForm(old => {
    const removed = old.capabilities[index];
    return {
      ...old,
      capabilities: old.capabilities.filter((_, i) => i !== index),
      default_skill_ref: removed.capability_key === old.default_skill_ref ? '' : old.default_skill_ref,
    };
  });
  const importFile = async (file: File, type: CapabilityRef['capability_type']) => {
    setImporting(true); setError(''); setNotice('');
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = ''; bytes.forEach(byte => { binary += String.fromCharCode(byte); });
      const validated = await api.validateCapability({ capability_type: type, filename: file.name, content_base64: btoa(binary) });
      const preview = validated.preview as { capabilities?: Array<{ capability_key?: string }> };
      const incomingKeys = (preview.capabilities ?? []).map(item => item.capability_key).filter((key): key is string => Boolean(key));
      const existingKeys = new Set(form.capabilities.filter(item => item.capability_type === type).map(item => item.capability_key));
      const conflicts = incomingKeys.filter(key => existingKeys.has(key));
      if (conflicts.length) throw new Error(`以下 ${type} 已存在：${[...new Set(conflicts)].join('、')}`);
      const committed = await api.commitCapability(validated.import_token);
      setForm(old => {
        const capabilities = [...old.capabilities, ...committed.capabilities];
        const firstImportedSkill = committed.capabilities.find(item => item.capability_type === 'SKILL');
        return {
          ...old,
          capabilities,
          default_skill_ref: old.default_skill_ref || firstImportedSkill?.capability_key || '',
        };
      });
      setNotice(type === 'SKILL'
        ? `已从 ${file.name} 批量导入 ${committed.capabilities.length} 个 Skill。`
        : `已从 ${file.name} 导入 ${committed.capabilities.length} 项 ${type}。`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '导入失败'); }
    finally { setImporting(false); }
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    if (!skills.length) { setTab(2); setError('至少导入一个 Skill 后才能保存节点资产。'); return; }
    if (!form.default_skill_ref || !skills.some(item => item.capability_key === form.default_skill_ref)) {
      setTab(2); setError('请选择一个已导入的 Skill 作为默认 Skill。'); return;
    }
    submitting.current = true; setSaving(true); setError('');
    try { await onSave(form); } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { submitting.current = false; setSaving(false); }
  };

  return <div className="modal-backdrop"><form className="modal editor asset-editor" onSubmit={submit}>
    <header><div><span className="eyebrow">NODE ASSET</span><h2>{node ? '编辑节点资产' : '新建节点资产'}</h2></div><button type="button" className="ghost" onClick={onClose}>关闭</button></header>
    <div className="step-tabs">{tabs.map((label, index) => <button type="button" key={label} className={tab === index ? 'active' : ''} onClick={() => setTab(index)}><span>{index + 1}</span>{label}</button>)}</div>
    {tab === 0 && <section className="form-grid form-pane">
      <label>节点名称<input aria-label="节点名称" required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}/></label>
      <label>所属目录<select aria-label="所属目录" value={form.directory_id ?? ''} onChange={e => setForm({ ...form, directory_id: e.target.value || null })}><option value="">未分类</option>{directories.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label className="wide">节点说明<textarea aria-label="节点说明" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })}/></label>
      <label>图标<input aria-label="图标" value={form.icon_value} onChange={e => setForm({ ...form, icon_value: e.target.value })}/></label>
      <label>默认 Skill<select aria-label="默认 Skill" required value={form.default_skill_ref} onChange={e => setForm({ ...form, default_skill_ref: e.target.value })}><option value="">请先导入 Skill</option>{skills.map(item => <option key={item.capability_key}>{item.capability_key}</option>)}</select></label>
    </section>}
    {tab === 1 && <section className="form-grid form-pane">
      <label>模型服务<select aria-label="模型服务" value={form.executor.model_provider_id ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, model_provider_id: e.target.value || null, model_name: null } })}><option value="">未配置</option>{providers.filter(item => item.available_for_nodes).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>模型<select aria-label="模型" value={form.executor.model_name ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, model_name: e.target.value || null } })}><option value="">服务默认</option>{provider?.models.filter(item => item.enabled).map(item => <option key={item.model_name}>{item.model_name}</option>)}</select></label>
      <label className="wide">启动触发提示词<textarea aria-label="启动触发提示词" value={form.executor.startup_prompt} onChange={e => setForm({ ...form, executor: { ...form.executor, startup_prompt: e.target.value } })}/></label>
      <label className="wide">上下文提示词<textarea aria-label="上下文提示词" value={form.executor.context_prompt} onChange={e => setForm({ ...form, executor: { ...form.executor, context_prompt: e.target.value } })}/></label>
      <label>超时秒数<input type="number" min="1" value={form.executor.timeout_seconds} onChange={e => setForm({ ...form, executor: { ...form.executor, timeout_seconds: Number(e.target.value) } })}/></label>
      <label>最大迭代<input type="number" min="1" value={form.executor.max_iterations} onChange={e => setForm({ ...form, executor: { ...form.executor, max_iterations: Number(e.target.value) } })}/></label>
    </section>}
    {tab === 2 && <section className="form-pane">
      <div className="capability-toolbar">{(['SKILL', 'MCP', 'HOOK'] as const).map(type => <label className="secondary file-button" key={type}><Upload size={14}/>{importing ? '导入中…' : type === 'SKILL' ? '批量导入 Skill ZIP' : `导入 ${type}`}<input type="file" disabled={importing} accept={type === 'SKILL' ? '.zip' : '.json,.yaml,.yml'} onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void importFile(file, type); }}/></label>)}</div>
      <p className="capability-help">一个 ZIP 可包含多个 Skill；每个 Skill 放在独立目录中并包含 <code>SKILL.md</code>，例如 ZIP 内的 <code>skill-a/SKILL.md</code>。导入后请选择默认 Skill。</p>
      {notice && <div className="notice success" role="status">{notice}</div>}
      {skills.length > 0 && <label className="default-skill-control">默认 Skill<select aria-label="默认 Skill" required value={form.default_skill_ref} onChange={e => setForm({ ...form, default_skill_ref: e.target.value })}>{skills.map(item => <option key={item.capability_key}>{item.capability_key}</option>)}</select></label>}
      <div className="capability-list">{form.capabilities.map((item, index) => <article key={`${item.capability_type}-${index}`}><span className="cap-type">{item.capability_type}</span><b data-testid="capability-key">{item.capability_key}</b><code>{JSON.stringify(item.normalized_config).slice(0, 120)}</code><button type="button" className="ghost" aria-label={`移除能力 ${item.capability_key}`} onClick={() => removeCapability(index)}><Trash2 size={14}/></button></article>)}</div>
    </section>}
    {tab === 3 && <section className="form-pane io-editor">{(['inputs', 'outputs'] as const).map(direction => <div key={direction}><header><h3>{direction === 'inputs' ? '输入定义' : '输出定义'}</h3><button type="button" className="secondary" onClick={() => setForm(old => ({ ...old, [direction]: [...old[direction], emptyField(direction === 'inputs' ? 'input' : 'output', old[direction].length)] }))}><Plus size={13}/>添加字段</button></header>{form[direction].map((field, index) => <div className="io-row" key={index}><input aria-label={`${direction} key ${index}`} value={field.field_key} onChange={e => updateField(direction, index, { field_key: e.target.value })}/><input aria-label={`${direction} name ${index}`} value={field.display_name} onChange={e => updateField(direction, index, { display_name: e.target.value })}/><select aria-label={`${direction} type ${index}`} value={field.data_type} onChange={e => updateField(direction, index, { data_type: e.target.value as ArtifactDataType })}>{DATA_TYPES.map(type => <option key={type.value} value={type.value}>{type.label}</option>)}</select><input aria-label={`${direction} description ${index}`} value={field.description} placeholder="说明" onChange={e => updateField(direction, index, { description: e.target.value })}/><button type="button" className="ghost" aria-label={`移除 ${direction} 字段 ${field.field_key}`} onClick={() => removeField(direction, index)}><Trash2 size={14}/></button></div>)}</div>)}</section>}
    {error && <p className="error">{error}</p>}
    <footer><button type="button" className="ghost" onClick={onClose}>取消</button>{tab > 0 && <button type="button" className="secondary" onClick={() => setTab(tab - 1)}>上一步</button>}{tab < 3 ? <button type="button" className="primary" onClick={() => setTab(tab + 1)}>下一步</button> : <button type="button" className="primary" disabled={saving} onClick={event => event.currentTarget.form?.requestSubmit()}>{saving ? '保存中…' : '保存节点资产'}</button>}</footer>
  </form></div>;
}
