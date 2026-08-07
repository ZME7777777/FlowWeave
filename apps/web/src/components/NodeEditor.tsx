import { useQuery } from '@tanstack/react-query';
import { Plus, Trash2, Upload } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { ArtifactDataType, CapabilityRef, IOField, NodeAsset, NodeAssetWrite } from '../types';

const DATA_TYPES: { value: ArtifactDataType; label: string }[] = [
  { value: 'TEXT', label: '纯文本' },
  { value: 'MARKDOWN', label: 'Markdown' },
  { value: 'JSON_OBJECT', label: 'JSON 对象' },
  { value: 'JSON_ARRAY', label: 'JSON 数组' },
  { value: 'FILE', label: '单个文件' },
  { value: 'FILE_COLLECTION', label: '文件集合' },
  { value: 'DOCUMENT', label: '文档' },
  { value: 'URL', label: 'URL' },
  { value: 'REPOSITORY_REF', label: '代码仓库引用' },
];
const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
const SKILL_ZIP_MAX_ENTRIES = 1000;
const CONFIG_MAX_BYTES = 1024 * 1024;
const toBase64 = (buffer: ArrayBuffer) => {
  const bytes = new Uint8Array(buffer);
  const chunks: string[] = [];
  for (let start = 0; start < bytes.length; start += 0x8000) {
    chunks.push(String.fromCharCode(...bytes.subarray(start, start + 0x8000)));
  }
  return btoa(chunks.join(''));
};
const emptyField = (direction: 'input' | 'output', index: number): IOField => ({
  field_key: `${direction}_${index + 1}`,
  display_name: '',
  data_type: 'DOCUMENT',
  description: '',
});
const emptyNode = (): NodeAssetWrite => ({
  directory_id: null,
  name: '',
  description: '',
  icon_kind: 'LUCIDE',
  icon_value: 'bot',
  default_skill_ref: null,
  row_version: 1,
  inputs: [],
  outputs: [],
  executor: {
    model_provider_id: null,
    model_name: null,
    startup_prompt: '读取流程输入和节点上下文，按当前任务要求完成工作。',
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
      default_skill_ref: node.default_skill_ref ?? null,
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
      default_skill_ref: removed.capability_key === old.default_skill_ref ? null : old.default_skill_ref,
    };
  });
  const importFile = async (file: File, type: CapabilityRef['capability_type']) => {
    setImporting(true); setError(''); setNotice('');
    try {
      const maxBytes = type === 'SKILL' ? SKILL_ZIP_MAX_BYTES : CONFIG_MAX_BYTES;
      if (file.size > maxBytes) throw new Error(`${type === 'SKILL' ? 'Skill ZIP' : '配置文件'}不能超过 ${type === 'SKILL' ? '25 MiB' : '1 MiB'}。`);
      const validated = await api.validateCapability({ capability_type: type, filename: file.name, content_base64: toBase64(await file.arrayBuffer()) });
      const preview = validated.preview as { capabilities?: Array<{ capability_key?: string }> };
      const incomingKeys = (preview.capabilities ?? []).map(item => item.capability_key).filter((key): key is string => Boolean(key));
      const existingKeys = new Set(form.capabilities.filter(item => item.capability_type === type).map(item => item.capability_key));
      const conflicts = incomingKeys.filter(key => existingKeys.has(key));
      if (conflicts.length) throw new Error(`以下 ${type} 已存在：${[...new Set(conflicts)].join('、')}`);
      const committed = await api.commitCapability(validated.import_token);
      setForm(old => {
        const capabilities = [...old.capabilities, ...committed.capabilities];
        return { ...old, capabilities };
      });
      setNotice(type === 'SKILL'
        ? `已从 ${file.name} 导入 ${committed.capabilities.length} 个 Skill。`
        : `已从 ${file.name} 导入 ${committed.capabilities.length} 项 ${type}。`);
    } catch (reason) {
      if (reason instanceof ApiError) {
        const maxEntries = typeof reason.details.max_entries === 'number' ? reason.details.max_entries : undefined;
        const actualEntries = typeof reason.details.actual_entries === 'number' ? reason.details.actual_entries : undefined;
        if (maxEntries !== undefined) {
          setError(`Skill ZIP 包含 ${actualEntries ?? '过多'} 个归档条目，最多允许 ${maxEntries} 个（文件和目录都会计数）。`);
          return;
        }
        const filename = typeof reason.details.filename === 'string' ? `：${reason.details.filename}` : '';
        setError(`${reason.message}${filename}`);
      } else setError(reason instanceof Error ? reason.message : '导入失败');
    }
    finally { setImporting(false); }
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    if (form.default_skill_ref && !skills.some(item => item.capability_key === form.default_skill_ref)) {
      setTab(2); setError('默认 Skill 已不存在，请重新选择或设为不指定。'); return;
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
      <div className="capability-toolbar">{(['SKILL', 'MCP', 'HOOK'] as const).map(type => <label className="secondary file-button" key={type}><Upload size={14}/>{importing ? '导入中…' : type === 'SKILL' ? '导入 Skill ZIP（支持批量）' : `导入 ${type}`}<input type="file" disabled={importing} accept={type === 'SKILL' ? '.zip' : '.json,.yaml,.yml'} onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void importFile(file, type); }}/></label>)}</div>
      <div className="capability-help"><b>Skill ZIP 最大 25 MiB、最多 {SKILL_ZIP_MAX_ENTRIES} 个归档条目</b><span>条目数包含文件和目录。该上限用于防止 ZIP 炸弹或误打包的海量小文件耗尽解压、扫描资源；请勿打包 <code>node_modules</code>、缓存或构建产物。</span><span>单个 Skill：可将 <code>SKILL.md</code>、<code>scripts/</code> 等直接放在 ZIP 根目录，也可以外包一层目录。</span><span>批量导入：每个 Skill 使用独立目录，例如 <code>skill-a/SKILL.md</code>、<code>skill-b/SKILL.md</code>。</span></div>
      {notice && <div className="notice success" role="status">{notice}</div>}
      {skills.length > 0 && <label className="default-skill-control">默认 Skill（可选）<select aria-label="默认 Skill" value={form.default_skill_ref ?? ''} onChange={e => setForm({ ...form, default_skill_ref: e.target.value || null })}><option value="">不指定，按提示词或 $ 引用能力</option>{skills.map(item => <option key={item.capability_key}>{item.capability_key}</option>)}</select><small>仅在节点始终优先使用某个 Skill 时设置；不影响会话中通过 $ 手动引用能力。</small></label>}
      <div className="capability-list">{form.capabilities.map((item, index) => <article key={`${item.capability_type}-${index}`}><span className="cap-type">{item.capability_type}</span><b data-testid="capability-key">{item.capability_key}</b><code>{JSON.stringify(item.normalized_config).slice(0, 120)}</code><button type="button" className="ghost" aria-label={`移除能力 ${item.capability_key}`} onClick={() => removeCapability(index)}><Trash2 size={14}/></button></article>)}</div>
    </section>}
    {tab === 3 && <section className="form-pane io-editor"><div className="io-intro"><b>定义节点与流程之间的数据契约</b><span>字段标识用于流程连线和产物绑定，展示名称面向使用者，使用说明会提供给 Agent。输入/输出方向由下方分区决定，不作为重复字段展示。</span></div>{(['inputs', 'outputs'] as const).map(direction => { const input = direction === 'inputs'; return <section className={`io-section ${input ? 'input' : 'output'}`} key={direction}><header><div><span>{input ? 'INPUT' : 'OUTPUT'}</span><h3>{input ? '输入字段' : '输出字段'}</h3><p>{input ? '声明本节点运行前可以接收的数据；没有输入时可保持为空。' : '声明本节点完成时应产出的数据；没有结构化产物时可保持为空。'}</p></div><button type="button" className="secondary" onClick={() => setForm(old => ({ ...old, [direction]: [...old[direction], emptyField(input ? 'input' : 'output', old[direction].length)] }))}><Plus size={13}/>添加{input ? '输入' : '输出'}</button></header>{form[direction].length > 0 && <div className="io-column-head" aria-hidden="true"><span>字段标识</span><span>展示名称</span><span>数据类型</span><span>使用说明</span><span/></div>}{form[direction].map((field, index) => <div className="io-row" key={index}><input required pattern="[A-Za-z][A-Za-z0-9_]{0,99}" aria-label={`${direction} key ${index}`} value={field.field_key} placeholder={input ? '例如 requirement' : '例如 result'} title="以字母开头，只能包含字母、数字和下划线" onChange={e => updateField(direction, index, { field_key: e.target.value })}/><input required aria-label={`${direction} name ${index}`} value={field.display_name} placeholder={input ? '例如需求文档' : '例如分析结果'} onChange={e => updateField(direction, index, { display_name: e.target.value })}/><select aria-label={`${direction} type ${index}`} value={field.data_type} onChange={e => updateField(direction, index, { data_type: e.target.value as ArtifactDataType })}>{DATA_TYPES.map(type => <option key={type.value} value={type.value}>{type.label}</option>)}</select><input aria-label={`${direction} description ${index}`} value={field.description} placeholder={input ? '说明 Agent 如何使用该输入' : '说明输出内容和验收要求'} onChange={e => updateField(direction, index, { description: e.target.value })}/><button type="button" className="ghost" aria-label={`移除 ${direction} 字段 ${field.field_key}`} onClick={() => removeField(direction, index)}><Trash2 size={14}/></button></div>)}{form[direction].length === 0 && <div className="io-empty">暂未定义{input ? '输入' : '输出'}；需要时点击右上角添加。</div>}</section>; })}</section>}
    {error && <p className="error">{error}</p>}
    <footer><button type="button" className="ghost" onClick={onClose}>取消</button>{tab > 0 && <button type="button" className="secondary" onClick={() => setTab(tab - 1)}>上一步</button>}{tab < 3 ? <button type="button" className="primary" onClick={() => setTab(tab + 1)}>下一步</button> : <button type="button" className="primary" disabled={saving} onClick={event => event.currentTarget.form?.requestSubmit()}>{saving ? '保存中…' : '保存节点资产'}</button>}</footer>
  </form></div>;
}
