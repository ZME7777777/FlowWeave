import { Check, Copy, Search, ShieldAlert, Wrench } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { ToolPolicyCatalog, ToolPolicyCatalogItem } from '../types';

type Mode = 'FORM' | 'JSON';
type Filter = 'ALL' | 'ENABLED' | 'READ_ONLY' | 'MUTATING' | 'CONTROL' | 'DISABLED';

const DEFAULT_TOOLS = ['terminal', 'file_editor', 'task_tracker'];
const TOOL_HELP: Record<string, { label: string; description: string }> = {
  terminal: { label: '终端', description: '在受管工作区执行 Shell 命令。' },
  file_editor: { label: '文件编辑器', description: '读取、创建和修改工作区文件。' },
  task_tracker: { label: '任务跟踪', description: '维护 Agent 的结构化任务计划。' },
  task_tool_set: { label: '子 Agent', description: '通过 OpenHands 原生 Task Tool Set 委派任务。' },
  workflow_tool_set: { label: '工作流', description: '调用 OpenHands 原生 Workflow Tool Set。' },
  edit: { label: '精确编辑', description: '使用 Gemini 兼容编辑工具修改文件。' },
  list_directory: { label: '目录浏览', description: '只读列出目录内容。' },
  read_file: { label: '读取文件', description: '只读获取文件内容。' },
  write_file: { label: '写入文件', description: '创建或覆盖工作区文件。' },
  glob: { label: '文件匹配', description: '按 glob 模式只读查找文件。' },
  grep: { label: '内容搜索', description: '只读搜索工作区文本。' },
  planning_file_editor: { label: '计划编辑器', description: '维护指定路径的计划文件。' },
  browser_tool_set: { label: '浏览器', description: '访问开放网络并操作网页。' },
  task: { label: '底层 Task', description: 'OpenHands 内部 Task 工具。' },
  workflow: { label: '底层 Workflow', description: 'OpenHands 内部 Workflow 工具。' },
};
const ACCESS_LABEL = { READ_ONLY: '只读', READ_WRITE: '读写', CONTROL: '控制', OPEN_WORLD: '开放网络' };
const CONCURRENCY_LABEL = { READ_ONLY: '可并行', RESOURCE_LOCKED: '资源锁保护', SERIAL_ONLY: '仅串行' };
const DISABLED_REASON: Record<string, string> = {
  'requires an OpenHands-internal TaskExecutor; use task_tool_set': '依赖 OpenHands 内部 TaskExecutor，请改用“子 Agent”。',
  'requires an OpenHands-internal WorkflowExecutor; use workflow_tool_set': '依赖 OpenHands 内部 WorkflowExecutor，请改用“工作流”。',
  'browser network, credential, artifact, and SSRF controls are not installed': '尚未安装网络、凭据、产物与 SSRF 安全控制。',
};

interface Props {
  catalog?: ToolPolicyCatalog; loading: boolean; loadError?: string; busy: boolean;
  onClose: () => void; onSave: (json: string) => void;
}

function help(tool: ToolPolicyCatalogItem) {
  return TOOL_HELP[tool.name] ?? { label: tool.name, description: `OpenHands 工具 ${tool.name}` };
}

export function ToolPolicyEditorDialog({ catalog, loading, loadError, busy, onClose, onSave }: Props) {
  const [mode, setMode] = useState<Mode>('FORM');
  const [name, setName] = useState('safe-default-tools');
  const [description, setDescription] = useState('节点允许使用的 OpenHands 工具');
  const [selected, setSelected] = useState(() => new Set(DEFAULT_TOOLS));
  const [params, setParams] = useState<Record<string, Record<string, string>>>({});
  const [concurrency, setConcurrency] = useState(1);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<Filter>('ALL');
  const [copied, setCopied] = useState(false);

  const selectedTools = useMemo(
    () => catalog?.tools.filter(tool => selected.has(tool.name) && tool.policy_enabled) ?? [],
    [catalog, selected],
  );
  const selectableTools = useMemo(
    () => catalog?.tools.filter(tool => tool.policy_enabled) ?? [],
    [catalog],
  );
  const allSelectableSelected = selectableTools.length > 0
    && selectableTools.every(tool => selected.has(tool.name));
  const forcesSerial = selectedTools.some(tool => tool.concurrency === 'SERIAL_ONLY');
  const effectiveConcurrency = forcesSerial ? 1 : concurrency;
  const document = useMemo(() => ({
    name: name.trim(),
    description: description.trim(),
    tool_concurrency_limit: effectiveConcurrency,
    tools: selectedTools.map(tool => ({
      name: tool.name,
      params: Object.fromEntries(Object.entries(params[tool.name] ?? {}).flatMap(([key, value]) => {
        if (value === '') return [];
        return [[key, tool.params[key]?.type === 'integer' ? Number(value) : value]];
      })),
    })),
  }), [description, effectiveConcurrency, name, params, selectedTools]);
  const json = JSON.stringify(document, null, 2);
  const visible = catalog?.tools.filter(tool => {
    const text = `${tool.name} ${help(tool).label} ${help(tool).description}`.toLowerCase();
    if (search && !text.includes(search.toLowerCase())) return false;
    if (filter === 'ENABLED') return tool.policy_enabled;
    if (filter === 'DISABLED') return !tool.policy_enabled;
    if (filter === 'READ_ONLY') return tool.access === 'READ_ONLY';
    if (filter === 'MUTATING') return tool.access === 'READ_WRITE';
    if (filter === 'CONTROL') return tool.access === 'CONTROL';
    return true;
  }) ?? [];

  const toggle = (tool: ToolPolicyCatalogItem) => {
    if (!tool.policy_enabled) return;
    setSelected(old => {
      const next = new Set(old);
      if (next.has(tool.name)) next.delete(tool.name); else next.add(tool.name);
      return next;
    });
    if (tool.concurrency === 'SERIAL_ONLY') setConcurrency(1);
  };
  const toggleAll = () => {
    setSelected(old => {
      const next = new Set(old);
      if (allSelectableSelected) {
        selectableTools.forEach(tool => next.delete(tool.name));
      } else {
        selectableTools.forEach(tool => next.add(tool.name));
      }
      return next;
    });
  };
  const updateParam = (tool: string, key: string, value: string) => setParams(old => ({
    ...old, [tool]: { ...(old[tool] ?? {}), [key]: value },
  }));
  const copyJson = async () => {
    await navigator.clipboard.writeText(json);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return <div className="modal-backdrop"><section className="modal tool-policy-dialog" role="dialog" aria-modal="true" aria-label="新建 Tool Policy">
    <header className="tool-policy-dialog-head"><div><span className="eyebrow">NEW TOOL POLICY</span><h2>配置并发布 Tool Policy</h2><p>从当前受治理的 OpenHands 工具中选择，保存后生成不可变策略版本。</p></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <div className="tool-policy-mode-tabs"><button className={mode === 'FORM' ? 'active' : ''} onClick={() => setMode('FORM')}>可视化配置</button><button className={mode === 'JSON' ? 'active' : ''} onClick={() => setMode('JSON')}>JSON 预览</button></div>
    {loading ? <div className="tool-policy-loading">正在读取当前 OpenHands Tool Catalog…</div> : loadError ? <div className="notice error" role="alert">{loadError}</div> : !catalog ? <div className="notice error">Tool Catalog 不可用。</div> : mode === 'FORM' ? <div className="tool-policy-form-layout">
      <section className="tool-policy-basics">
        <header><span className="eyebrow">POLICY SETTINGS</span><h3>基础设置</h3><p>设置策略标识与执行并发规则。</p></header>
        <label><span>策略名称</span><input aria-label="策略名称" value={name} maxLength={200} onChange={event => setName(event.target.value)}/></label>
        <label><span>说明</span><textarea aria-label="策略说明" value={description} maxLength={2000} onChange={event => setDescription(event.target.value)}/></label>
        <label><span>并发上限</span><select aria-label="工具并发上限" value={effectiveConcurrency} disabled={forcesSerial} onChange={event => setConcurrency(Number(event.target.value))}>{Array.from({ length: catalog.max_tool_concurrency }, (_, index) => index + 1).map(value => <option key={value} value={value}>{value}</option>)}</select><small>{forcesSerial ? '所选工具包含仅串行工具，已锁定为 1。' : '仅对具备只读或资源锁契约的工具生效。'}</small></label>
      </section>
      <section className="tool-policy-catalog"><header><div><b>选择工具</b><span>从 {catalog.tools.length} 项当前目录中选择策略允许使用的工具</span></div><code>OpenHands {catalog.openhands_version}</code></header><div className="tool-policy-toolbar"><label className="tool-policy-search"><Search size={15} aria-hidden="true"/><input aria-label="搜索 OpenHands 工具" placeholder="搜索名称或用途" value={search} onChange={event => setSearch(event.target.value)}/></label><div><button className="tool-policy-select-all" onClick={toggleAll}>{allSelectableSelected ? '取消全选' : '全选'}</button>{([['ALL', '全部'], ['ENABLED', '可用'], ['READ_ONLY', '只读'], ['MUTATING', '读写'], ['CONTROL', '控制'], ['DISABLED', '不可用']] as Array<[Filter, string]>).map(([value, label]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div></div>
        <div className="tool-policy-tool-grid">{visible.map(tool => { const info = help(tool); const checked = selected.has(tool.name); return <article key={tool.name} className={`${checked ? 'selected' : ''} ${tool.policy_enabled ? '' : 'disabled'}`}><label title={tool.policy_enabled ? `选择 ${tool.name}` : (tool.disabled_reason ?? '当前策略不可用')}><input type="checkbox" aria-label={`选择工具 ${tool.name}`} checked={checked} disabled={!tool.policy_enabled} onChange={() => toggle(tool)}/><span className="tool-policy-tool-icon">{tool.policy_enabled ? checked ? <Check size={16}/> : <Wrench size={16}/> : <ShieldAlert size={16}/>}</span><span><b>{info.label}</b><code>{tool.name}</code></span></label><p>{info.description}</p><div className="tool-policy-badges"><span className={tool.access.toLowerCase()}>{ACCESS_LABEL[tool.access]}</span><span>{tool.confirmation === 'REQUIRED' ? '需确认' : '免确认'}</span><span>{CONCURRENCY_LABEL[tool.concurrency]}</span></div>{!tool.policy_enabled && <small className="tool-policy-disabled-reason">{DISABLED_REASON[tool.disabled_reason ?? ''] ?? tool.disabled_reason ?? '尚未通过 FlowWeave 治理。'}</small>}{checked && Object.keys(tool.params).length > 0 && <div className="tool-policy-params">{Object.entries(tool.params).map(([key, schema]) => <label key={key}><span>{key}<small>可选</small></span>{schema.enum ? <select value={params[tool.name]?.[key] ?? ''} onChange={event => updateParam(tool.name, key, event.target.value)}><option value="">使用 OpenHands 默认值</option>{schema.enum.map(value => <option key={value} value={value}>{value}</option>)}</select> : <input type={schema.type === 'integer' ? 'number' : 'text'} min={schema.minimum} max={schema.maximum} maxLength={schema.max_length} value={params[tool.name]?.[key] ?? ''} placeholder="使用 OpenHands 默认值" onChange={event => updateParam(tool.name, key, event.target.value)}/>}</label>)}</div>}</article>; })}</div>
      </section>
    </div> : <section className="tool-policy-json-preview"><header><div><b>将要发布的规范 JSON</b><span>元数据与安全分类由后端校验并补全。</span></div><button className="secondary" onClick={() => void copyJson()}><Copy size={13}/>{copied ? '已复制' : '复制 JSON'}</button></header><pre>{json}</pre></section>}
    <footer><div className="tool-policy-provenance"><span>Catalog digest</span><code title={catalog?.catalog_digest}>{catalog?.catalog_digest.slice(0, 12) ?? '—'}</code></div><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || loading || !catalog || !name.trim() || selectedTools.length === 0} onClick={() => onSave(json)}>{busy ? '校验中…' : `保存并发布（${selectedTools.length} 项）`}</button></footer>
  </section></div>;
}
