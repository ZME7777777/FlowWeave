import { useQuery } from '@tanstack/react-query';
import { Layers3, Plus, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useEscapeClose } from './useEscapeClose';
import type { CapabilityAsset, CapabilityCollection, CapabilityRef, IOField, NodeAsset, NodeAssetWrite } from '../types';
const emptyField = (direction: 'input' | 'output', index: number): IOField => ({
  field_key: `${direction}_${index + 1}`,
  display_name: '',
  data_type: 'URL',
  description: '',
  template_url: '',
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
    model_provider_id: null,
    model_name: null,
    startup_prompt: '读取流程输入和节点上下文，按当前任务要求完成工作。',
    context_prompt: '优先读取流程上游产物与人工输入，引用证据时标注来源。',
    timeout_seconds: 900,
    max_iterations: 100,
    confirmation_policy: 'ALWAYS',
    condenser: {
      kind: 'NO_OP', model_provider_id: null, model_name: null,
      max_size: 240, max_tokens: null, keep_first: 2, minimum_progress: 0.1,
      hard_context_reset_max_retries: 5, hard_context_reset_context_scaling: 0.8,
    },
  },
  capabilities: [],
  environment_version_id: null,
});
interface Props {
  node?: NodeAsset;
  onSave: (data: NodeAssetWrite) => Promise<void>;
  onClose: () => void;
}
const tabs = ['基础信息', '模型与提示词', '能力引用', '输入输出定义'];

export function NodeEditor({ node, onSave, onClose }: Props) {
  useEscapeClose(onClose);
  const [form, setForm] = useState<NodeAssetWrite>(emptyNode());
  const [tab, setTab] = useState(0);
  const [error, setError] = useState('');
  const [collectionNotice, setCollectionNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const { data: directories = [] } = useQuery({ queryKey: ['directories'], queryFn: api.directories });
  const { data: providers = [] } = useQuery({ queryKey: ['providers'], queryFn: api.providers });
  const { data: capabilityPool = [], isLoading: capabilitiesLoading } = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const { data: capabilityCollections = [], isLoading: collectionsLoading } = useQuery({ queryKey: ['capability-collections'], queryFn: api.capabilityCollections });
  const { data: environments = [] } = useQuery({ queryKey: ['terminal-environments'], queryFn: api.terminalEnvironments });

  useEffect(() => {
    if (!node) { setForm(emptyNode()); return; }
    setForm({
      directory_id: node.directory_id,
      name: node.name,
      description: node.description,
      icon_kind: node.icon_kind,
      icon_value: node.icon_value,
      environment_version_id: node.environment_version_id ?? null,
      row_version: node.row_version,
      inputs: node.inputs.map(({ field_key, display_name, data_type, description, template_url }) => ({ field_key, display_name, data_type, description, template_url })),
      outputs: node.outputs.map(({ field_key, display_name, data_type, description, template_url }) => ({ field_key, display_name, data_type, description, template_url })),
      executor: node.executor ?? emptyNode().executor,
      capabilities: node.capabilities.map(({ capability_id, capability_type, capability_key, normalized_config }) => ({ capability_id, capability_type, capability_key, normalized_config })),
    });
  }, [node]);

  const provider = providers.find(item => item.id === form.executor.model_provider_id);
  const condenserProvider = providers.find(item => item.id === (form.executor.condenser.model_provider_id ?? form.executor.model_provider_id));
  const updateField = (direction: 'inputs' | 'outputs', index: number, patch: Partial<IOField>) => setForm(old => ({
    ...old,
    [direction]: old[direction].map((item, i) => i === index ? { ...item, ...patch } : item),
  }));
  const removeField = (direction: 'inputs' | 'outputs', index: number) => setForm(old => ({
    ...old, [direction]: old[direction].filter((_, i) => i !== index),
  }));
  const selectedCapabilityIds = new Set(form.capabilities.map(item => item.capability_id).filter((id): id is string => Boolean(id)));
  const selectableCapabilities = capabilityPool.filter(item => item.is_latest || selectedCapabilityIds.has(item.id));
  const selectableToolPolicies = selectableCapabilities.filter(item => item.capability_type === 'TOOL_POLICY');
  const selectedToolPolicy = form.capabilities.find(item => item.capability_type === 'TOOL_POLICY');
  const selectToolPolicy = (capabilityId: string) => setForm(old => {
    const policy = selectableToolPolicies.find(item => item.id === capabilityId);
    return {
      ...old,
      capabilities: [
        ...old.capabilities.filter(item => item.capability_type !== 'TOOL_POLICY'),
        ...(policy ? [{
          capability_id: policy.id,
          capability_type: 'TOOL_POLICY' as const,
          capability_key: policy.capability_key,
          normalized_config: {},
        }] : []),
      ],
    };
  });
  const toggleCapability = (asset: CapabilityAsset) => {
    const capabilityType: CapabilityRef['capability_type'] = asset.capability_type;
    setForm(old => {
      if (old.capabilities.some(item => item.capability_id === asset.id)) {
        return { ...old, capabilities: old.capabilities.filter(item => item.capability_id !== asset.id) };
      }
      const next: CapabilityRef = {
        capability_id: asset.id,
        capability_type: capabilityType,
        capability_key: asset.capability_key,
        normalized_config: {},
      };
      return {
        ...old,
        capabilities: [
          ...old.capabilities.filter(item => !(item.capability_type === capabilityType && item.capability_key === asset.capability_key)),
          next,
        ],
      };
    });
  };
  const addCapabilityCollection = (collection: CapabilityCollection) => {
    const unavailable = collection.members.filter(item => item.dependency_build_state !== 'NOT_REQUIRED' && item.dependency_build_state !== 'READY');
    if (unavailable.length) {
      setCollectionNotice(`组合“${collection.name}”包含依赖未就绪的能力：${unavailable.map(item => `${item.capability_type}:${item.capability_key}`).join('、')}。`);
      return;
    }
    setForm(old => {
      const identity = (type: string, key: string) => `${type}:${key}`;
      const memberKeys = new Set(collection.members.map(item => identity(item.capability_type, item.capability_key)));
      const currentByKey = new Map(old.capabilities.map(item => [identity(item.capability_type, item.capability_key), item]));
      const added = collection.members.filter(item => !currentByKey.has(identity(item.capability_type, item.capability_key))).length;
      const replaced = collection.members.filter(item => {
        const current = currentByKey.get(identity(item.capability_type, item.capability_key));
        return current && current.capability_id !== item.id;
      }).length;
      const unchanged = collection.members.length - added - replaced;
      const expanded: CapabilityRef[] = collection.members.map(item => ({
        capability_id: item.id,
        capability_type: item.capability_type,
        capability_key: item.capability_key,
        normalized_config: {},
      }));
      setCollectionNotice(`已展开“${collection.name}”：新增 ${added} 项、替换 ${replaced} 项、已有 ${unchanged} 项。节点保存的仍是 ${expanded.length} 个真实能力版本引用。`);
      return {
        ...old,
        capabilities: [
          ...old.capabilities.filter(item => !memberKeys.has(identity(item.capability_type, item.capability_key))),
          ...expanded,
        ],
      };
    });
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setSaving(true); setError('');
    try {
      await onSave({
        ...form,
        inputs: form.inputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
        outputs: form.outputs.map(field => ({ ...field, display_name: field.display_name.trim() })),
      });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败'); }
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
      <label className="wide">运行环境<select aria-label="运行环境" value={form.environment_version_id ?? ''} onChange={e => setForm({ ...form, environment_version_id: e.target.value || null })}><option value="">平台默认环境</option>{environments.flatMap(environment => environment.versions.filter(version => version.state === 'READY').map(version => <option key={version.id} value={version.id}>{environment.name} · v{version.version_no}</option>))}</select><small>节点保存具体的不可变环境版本；后续发布新版本不会改变已开始的运行。</small></label>
    </section>}
    {tab === 1 && <section className="form-grid form-pane">
      <label>模型服务<select aria-label="模型服务" value={form.executor.model_provider_id ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, model_provider_id: e.target.value || null, model_name: null } })}><option value="">未配置</option>{providers.filter(item => item.available_for_nodes).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>模型<select aria-label="模型" value={form.executor.model_name ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, model_name: e.target.value || null } })}><option value="">服务默认</option>{provider?.models.filter(item => item.enabled).map(item => <option key={item.model_name}>{item.model_name}</option>)}</select></label>
      <label className="wide">启动触发提示词<textarea aria-label="启动触发提示词" value={form.executor.startup_prompt} onChange={e => setForm({ ...form, executor: { ...form.executor, startup_prompt: e.target.value } })}/></label>
      <label className="wide">上下文提示词<textarea aria-label="上下文提示词" value={form.executor.context_prompt} onChange={e => setForm({ ...form, executor: { ...form.executor, context_prompt: e.target.value } })}/></label>
      <label>超时秒数<input type="number" min="1" value={form.executor.timeout_seconds} onChange={e => setForm({ ...form, executor: { ...form.executor, timeout_seconds: Number(e.target.value) } })}/></label>
      <label>最大迭代<input type="number" min="1" value={form.executor.max_iterations} onChange={e => setForm({ ...form, executor: { ...form.executor, max_iterations: Number(e.target.value) } })}/></label>
      <label className="wide">工具确认策略<select aria-label="工具确认策略" value={form.executor.confirmation_policy} onChange={e => setForm({ ...form, executor: { ...form.executor, confirmation_policy: e.target.value as 'ALWAYS' | 'NEVER' } })}><option value="ALWAYS">每个 OpenHands 工具批次均需人工确认</option><option value="NEVER">无需人工确认</option></select><small>策略在 Attempt 启动时冻结；“每批次确认”使用 OpenHands 原生批次确认，不支持伪逐 Action 审批。</small></label>
      <label className="wide">上下文压缩策略<select aria-label="上下文压缩策略" value={form.executor.condenser.kind} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, kind: e.target.value as 'NO_OP' | 'LLM_SUMMARIZING', model_provider_id: e.target.value === 'NO_OP' ? null : form.executor.condenser.model_provider_id, model_name: e.target.value === 'NO_OP' ? null : form.executor.condenser.model_name } } })}><option value="NO_OP">禁用（显式 NoOpCondenser）</option><option value="LLM_SUMMARIZING">LLM 摘要压缩</option></select><small>策略随 Run Snapshot 冻结；不会依赖 OpenHands 将来的默认值。</small></label>
      {form.executor.condenser.kind === 'LLM_SUMMARIZING' && <>
        <label>摘要模型服务<select aria-label="摘要模型服务" value={form.executor.condenser.model_provider_id ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, model_provider_id: e.target.value || null, model_name: null } } })}><option value="">继承节点模型服务</option>{providers.filter(item => item.available_for_nodes).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label>摘要模型<select aria-label="摘要模型" value={form.executor.condenser.model_name ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, model_name: e.target.value || null } } })}><option value="">服务默认</option>{condenserProvider?.models.filter(item => item.enabled).map(item => <option key={item.model_name}>{item.model_name}</option>)}</select></label>
        <label>最大事件数<input aria-label="压缩最大事件数" type="number" min="20" max="10000" value={form.executor.condenser.max_size} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, max_size: Number(e.target.value) } } })}/></label>
        <label>最大 Token（可选）<input aria-label="压缩最大 Token" type="number" min="1" value={form.executor.condenser.max_tokens ?? ''} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, max_tokens: e.target.value ? Number(e.target.value) : null } } })}/></label>
        <label>保留开头事件<input aria-label="压缩保留开头事件" type="number" min="0" value={form.executor.condenser.keep_first} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, keep_first: Number(e.target.value) } } })}/></label>
        <label>最小压缩比例<input aria-label="最小压缩比例" type="number" min="0.01" max="0.99" step="0.01" value={form.executor.condenser.minimum_progress} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, minimum_progress: Number(e.target.value) } } })}/></label>
        <label>硬重置重试次数<input aria-label="硬重置重试次数" type="number" min="1" max="100" value={form.executor.condenser.hard_context_reset_max_retries} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, hard_context_reset_max_retries: Number(e.target.value) } } })}/></label>
        <label>硬重置缩放系数<input aria-label="硬重置缩放系数" type="number" min="0.01" max="0.99" step="0.01" value={form.executor.condenser.hard_context_reset_context_scaling} onChange={e => setForm({ ...form, executor: { ...form.executor, condenser: { ...form.executor.condenser, hard_context_reset_context_scaling: Number(e.target.value) } } })}/></label>
      </>}
    </section>}
    {tab === 2 && <section className="form-pane">
      <div className="capability-help"><b>从公共能力仓库选择当前节点可用的能力</b><span>Tool Policy 独立单选；Skill、Plugin、MCP、Hook 与 Agent Definition 可多选。节点只保存不可变能力版本引用。</span><span>绑定 Agent Definition 时，Tool Policy 必须显式允许 task_tool_set，并覆盖定义内声明的全部 Tool。</span></div>
      <section className="node-tool-policy"><header><div><b>Tool Policy</b><small>固定 OpenHands 1.40.0 Tool 名和参数；未知或未治理 Tool 默认拒绝。</small></div><em>单选不可变版本</em></header><label><span>运行工具策略</span><select aria-label="运行工具策略" value={selectedToolPolicy?.capability_id ?? ''} onChange={event => selectToolPolicy(event.target.value)}><option value="">平台默认策略（保存时冻结）</option>{selectableToolPolicies.map(item => <option key={item.id} value={item.id}>{item.capability_key} · rev {item.revision_number} · {item.content_hash.slice(0, 10)}</option>)}</select><small>{selectedToolPolicy ? `已绑定 ${selectedToolPolicy.capability_key} 的固定版本 ${selectedToolPolicy.capability_id}` : '未显式选择时，后端将在保存节点时绑定内置默认 Tool Policy 的固定版本。'}</small></label></section>
      <section className="node-capability-collections"><header><div><Layers3 size={16}/><span><b>按能力组合批量添加</b><small>组合仅用于选择；点击后立即展开为固定版本的真实能力。</small></span></div><em>{capabilityCollections.length} 个组合</em></header>{collectionsLoading ? <div className="empty compact">加载能力组合…</div> : capabilityCollections.length ? <div>{capabilityCollections.map(collection => { const unavailable = collection.members.some(item => item.dependency_build_state !== 'NOT_REQUIRED' && item.dependency_build_state !== 'READY'); return <article key={collection.id}><div><span>{collection.category || '未分类'}</span><b>{collection.name}</b><small>{collection.description || collection.members.map(item => `${item.capability_type}:${item.capability_key}`).join('、')}</small><p>{collection.members.map(item => <code key={item.id}>{item.capability_type} · {item.capability_key} · rev {item.revision_number}</code>)}</p></div><button type="button" className="secondary" disabled={unavailable} title={unavailable ? '组合中有依赖未就绪的能力' : `添加 ${collection.members.length} 个真实能力版本`} onClick={() => addCapabilityCollection(collection)}>添加 {collection.members.length} 项</button></article>; })}</div> : <p className="node-capability-collections-empty">尚未创建能力组合；仍可在下方逐项选择。</p>}{collectionNotice && <div className="node-collection-notice" role="status">{collectionNotice}</div>}</section>
      {capabilitiesLoading ? <div className="empty compact">加载能力仓库…</div> : selectableCapabilities.some(item => item.capability_type !== 'TOOL_POLICY') ? <div className="capability-picker">{(['SKILL', 'PLUGIN', 'MCP', 'HOOK', 'AGENT_DEFINITION'] as const).map(type => { const items = selectableCapabilities.filter(item => item.capability_type === type); return <section key={type}><header><b>{type === 'SKILL' ? 'Skills' : type === 'AGENT_DEFINITION' ? 'Agent Definitions' : type}</b><span>{items.filter(item => item.is_latest).length} 项能力</span></header>{items.length ? items.map(item => { const ready = item.dependency_build_state === 'NOT_REQUIRED' || item.dependency_build_state === 'READY'; const selected = selectedCapabilityIds.has(item.id); return <label key={item.id} className={`${selected ? 'selected' : ''} ${ready ? '' : 'unavailable'}`} title={ready ? '' : item.dependency_build_state === 'FAILED' ? item.dependency_build_error || '依赖构建失败' : '依赖正在隔离构建中'}><input type="checkbox" aria-label={`选择能力 ${item.capability_key}`} checked={selected} disabled={!ready && !selected} onChange={() => toggleCapability(item)}/><span><b data-testid="capability-key">{item.capability_key}</b><small>{item.description || item.filename}</small></span><em>{!ready ? item.dependency_build_state === 'FAILED' ? '依赖失败' : '依赖构建中' : `${item.reference_count} 个节点引用`}</em></label>; }) : <p>能力仓库中暂无 {type === 'AGENT_DEFINITION' ? 'Agent Definition' : type}</p>}</section>; })}</div> : <div className="capability-pool-empty"><b>运行能力仓库尚为空</b><span>请关闭编辑器，前往顶部“能力仓库”发布运行能力。</span></div>}
    </section>}
    {tab === 3 && <section className="form-pane io-editor"><div className="io-intro"><b>定义输入输出</b><span>这里只定义数据槽位，不填写运行时的具体飞书文档。模板为可选项：有模板时仅参考其格式和结构；留空时不参考模板，按说明和任务要求处理。</span></div>{(['inputs', 'outputs'] as const).map(direction => { const input = direction === 'inputs'; return <section className={`io-section ${input ? 'input' : 'output'}`} key={direction}><header><div><span>{input ? 'INPUT' : 'OUTPUT'}</span><h3>{input ? '输入定义' : '输出定义'}</h3><p>{input ? '运行时由人工指定实际输入，或自动接收上游输出。' : '运行时在本次运行目录创建文档；有模板则复制模板，无模板则创建空白文档。'}</p></div><button type="button" className="secondary" onClick={() => setForm(old => ({ ...old, [direction]: [...old[direction], emptyField(input ? 'input' : 'output', old[direction].length)] }))}><Plus size={13}/>添加{input ? '输入' : '输出'}</button></header>{form[direction].length > 0 && <div className="io-column-head lark-doc-head" aria-hidden="true"><span>字段标识</span><span>展示名称</span><span>模板（可选）</span><span>说明</span><span>操作</span></div>}{form[direction].map((field, index) => <div className="io-row lark-doc-row" key={index}><input required pattern="[A-Za-z][A-Za-z0-9_]{0,99}" aria-label={`${direction} key ${index}`} value={field.field_key} placeholder={input ? 'requirement' : 'result'} title="以字母开头，只能包含字母、数字和下划线" onChange={e => updateField(direction, index, { field_key: e.target.value })}/><input aria-label={`${direction} name ${index}`} value={field.display_name} placeholder="" onChange={e => updateField(direction, index, { display_name: e.target.value })}/><input type="url" pattern="https://.*/docx/[^/]+.*" aria-label={`${direction} template ${index}`} value={field.template_url} placeholder="可选：飞书 Docx 模板 URL" onChange={e => updateField(direction, index, { template_url: e.target.value })}/><input aria-label={`${direction} description ${index}`} value={field.description} placeholder={input ? '说明 Agent 如何使用输入' : '说明输出内容与验收要求'} onChange={e => updateField(direction, index, { description: e.target.value })}/><button type="button" className="ghost" aria-label={`移除 ${direction} 字段 ${field.field_key}`} onClick={() => removeField(direction, index)}><Trash2 size={14}/></button></div>)}{form[direction].length === 0 && <div className="io-empty">暂未定义{input ? '输入' : '输出'}。</div>}</section>; })}</section>}
    {error && <p className="error">{error}</p>}
    <footer><button type="button" className="ghost" onClick={onClose}>取消</button>{tab > 0 && <button type="button" className="secondary" onClick={() => setTab(tab - 1)}>上一步</button>}{tab < 3 ? <button type="button" className="primary" onClick={() => setTab(tab + 1)}>下一步</button> : <button type="button" className="primary" disabled={saving} onClick={event => event.currentTarget.form?.requestSubmit()}>{saving ? '保存中…' : '保存节点资产'}</button>}</footer>
  </form></div>;
}
