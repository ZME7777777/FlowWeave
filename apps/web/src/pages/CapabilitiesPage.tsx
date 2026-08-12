import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Braces, CheckSquare, FileArchive, Layers3, Pencil, PlugZap, Search, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import { HookEditorDialog, type HookScriptAsset } from '../components/HookEditorDialog';
import { SkillCollectionEditorDialog } from '../components/SkillCollectionEditorDialog';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { BlockedCapabilityDelete, CapabilityAsset, CapabilityAssetType, SkillCollection, SkillCollectionWrite } from '../types';

const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
const MCP_JSON_MAX_BYTES = 1024 * 1024;
const MCP_SCRIPT_MAX_FILES = 20;
const MCP_SCRIPT_MAX_FILE_BYTES = 1024 * 1024;
const MCP_SCRIPT_MAX_TOTAL_BYTES = 10 * 1024 * 1024;
const MCP_SCRIPT_EXTENSIONS = new Set(['.py', '.js', '.mjs', '.cjs', '.sh', '.json', '.yaml', '.yml', '.toml', '.txt']);
const MCP_JSON_EXAMPLE = JSON.stringify({
  mcpServers: {
    docs: {
      url: 'https://mcp.example.com',
      transport: 'streamable-http',
      description: '查询团队文档',
      timeout: 30,
    },
    localTools: {
      command: 'mcp-tool-server',
      args: ['--stdio'],
      transport: 'stdio',
      description: '调用终端环境中已安装的 MCP CLI',
    },
  },
}, null, 2);
const HOOK_JSON_EXAMPLE = JSON.stringify({
  name: 'protect-dangerous-tools',
  description: '在工具执行前检查高风险操作',
  hooks: {
    PreToolUse: [{
      matcher: 'terminal',
      hooks: [{ type: 'prompt', name: 'review-command', prompt: '检查本次工具调用是否安全；不安全时阻止执行。', timeout: 60 }],
    }],
  },
}, null, 2);
const DEPENDENCY_EXAMPLE = `dependencies:
  python:
    requests: 2.32.3
  node:
    lodash: 4.17.21
  cli:
    lark-cli: 1.0.84`;

interface CapabilityLineage {
  id: string;
  latest: CapabilityAsset;
  versions: CapabilityAsset[];
}

type McpEditorMode = 'FORM' | 'JSON';
type McpTransport = 'http' | 'streamable-http' | 'sse' | 'stdio';
type McpServerConfig = Record<string, unknown>;
interface McpDocument { mcpServers: Record<string, McpServerConfig> }
interface McpScriptAsset { server: string; filename: string; contentBase64: string; byteSize: number }

function parseMcpDocument(text: string): McpDocument {
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new Error('JSON 语法无效，请修正后再切换到表单。'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('MCP JSON 根节点必须是对象。');
  const root = value as Record<string, unknown>;
  const rawServers = root.mcpServers ?? root.servers;
  if (!rawServers || typeof rawServers !== 'object' || Array.isArray(rawServers)) throw new Error('MCP JSON 必须包含 mcpServers 或 servers 对象。');
  const servers: Record<string, McpServerConfig> = {};
  Object.entries(rawServers as Record<string, unknown>).forEach(([name, server]) => {
    if (!server || typeof server !== 'object' || Array.isArray(server)) throw new Error(`Server“${name}”必须是对象。`);
    servers[name] = { ...(server as McpServerConfig) };
  });
  return { mcpServers: servers };
}

function serializeMcpDocument(document: McpDocument): string {
  return JSON.stringify(document, null, 2);
}

function effectiveTransport(server: McpServerConfig): McpTransport {
  const transport = server.transport ?? server.type;
  if (transport === 'stdio' || transport === 'sse' || transport === 'http') return transport;
  return 'streamable-http';
}

function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  const chunks: string[] = [];
  for (let start = 0; start < bytes.length; start += 0x8000) chunks.push(String.fromCharCode(...bytes.subarray(start, start + 0x8000)));
  return btoa(chunks.join(''));
}
function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}
function typeLabel(type: CapabilityAssetType): string { return type === 'SKILL' ? 'Skill' : type; }
function errorMessage(reason: unknown): string {
  if (reason instanceof ApiError && reason.code === 'CAPABILITY_IN_USE') {
    const blocked = reason.details.blocked;
    if (Array.isArray(blocked)) return blockedCapabilityMessage(blocked as BlockedCapabilityDelete[]);
    return '能力仍被节点引用，请先从相关节点移除后再删除。';
  }
  if (reason instanceof ApiError && reason.code === 'IMPORT_REJECTED') {
    const filename = typeof reason.details.filename === 'string' ? `（${reason.details.filename}）` : '';
    const actual = typeof reason.details.actual_entries === 'number' ? reason.details.actual_entries : undefined;
    const maximum = typeof reason.details.max_entries === 'number' ? reason.details.max_entries : undefined;
    const fields = Array.isArray(reason.details.fields) ? reason.details.fields.map(String).join('、') : '';
    if (actual !== undefined && maximum !== undefined) return `压缩包条目过多：${actual} 项，最多允许 ${maximum} 项。`;
    if (filename) return `能力包包含不支持或不安全的文件${filename}。`;
    if (fields) return `能力配置包含不支持的字段：${fields}。`;
    return reason.message || '能力配置未通过安全校验。';
  }
  return reason instanceof Error ? reason.message : '操作失败';
}
function blockedCapabilityMessage(blocked: BlockedCapabilityDelete[]): string {
  return `以下能力仍有关联，已跳过：${blocked.map(item => {
    const nodes = item.nodes.length ? `节点 ${item.nodes.map(node => `“${node.name}”`).join('、')}` : '';
    const collections = item.collections?.length ? `Skill 组合 ${item.collections.map(collection => `“${collection.name}”`).join('、')}` : '';
    return `“${item.name}”被${[nodes, collections].filter(Boolean).join('、')}引用`;
  }).join('；')}。`;
}
function groupCapabilities(capabilities: CapabilityAsset[]): CapabilityLineage[] {
  const groups = new Map<string, CapabilityAsset[]>();
  capabilities.forEach(item => groups.set(item.lineage_id, [...(groups.get(item.lineage_id) ?? []), item]));
  return [...groups.entries()].map(([id, items]) => {
    const versions = [...items].sort((left, right) => right.revision_number - left.revision_number);
    return { id, versions, latest: versions.find(item => item.is_latest) ?? versions[0] };
  }).sort((left, right) => right.latest.created_at.localeCompare(left.latest.created_at));
}

export function CapabilitiesPage() {
  const dialog = useProductDialog();
  const qc = useQueryClient();
  const { data: capabilities = [], isLoading } = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const { data: skillCollections = [], isLoading: collectionsLoading } = useQuery({ queryKey: ['skill-collections'], queryFn: api.skillCollections });
  const [type, setType] = useState<'ALL' | CapabilityAssetType>('ALL');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<CapabilityAsset>();
  useEscapeClose(() => setEditing(undefined), Boolean(editing));
  const [editingCollection, setEditingCollection] = useState<SkillCollection | null | undefined>();
  const [mcpOpen, setMcpOpen] = useState(false);
  useEscapeClose(() => setMcpOpen(false), mcpOpen);
  const [hookOpen, setHookOpen] = useState(false);
  useEscapeClose(() => setHookOpen(false), hookOpen);
  const [source, setSource] = useState('');
  const [mcpMode, setMcpMode] = useState<McpEditorMode>('FORM');
  const [mcpJson, setMcpJson] = useState(MCP_JSON_EXAMPLE);
  const [mcpSelectedServer, setMcpSelectedServer] = useState('docs');
  const [mcpJsonError, setMcpJsonError] = useState('');
  const [mcpScripts, setMcpScripts] = useState<McpScriptAsset[]>([]);
  const [hookJson, setHookJson] = useState(HOOK_JSON_EXAMPLE);
  const [hookScripts, setHookScripts] = useState<HookScriptAsset[]>([]);
  const [busy, setBusy] = useState(false);
  const [importingSkill, setImportingSkill] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const lineages = useMemo(() => groupCapabilities(capabilities), [capabilities]);
  const visible = useMemo(() => lineages.filter(group =>
    (type === 'ALL' || group.latest.capability_type === type)
    && (!search || group.versions.some(item => `${item.capability_key} ${item.description} ${item.filename}`.toLowerCase().includes(search.toLowerCase()))),
  ), [lineages, type, search]);
  const allVisibleSelected = visible.length > 0 && visible.every(item => selected.has(item.id));
  const selectedIds = lineages.filter(item => selected.has(item.id)).flatMap(item => item.versions.map(record => record.id));
  const parsedMcp = useMemo(() => {
    try { return { document: parseMcpDocument(mcpJson), error: '' }; }
    catch (reason) { return { document: undefined, error: reason instanceof Error ? reason.message : 'MCP JSON 无效。' }; }
  }, [mcpJson]);
  const mcpServerNames = parsedMcp.document ? Object.keys(parsedMcp.document.mcpServers) : [];
  const activeMcpServerName = mcpServerNames.includes(mcpSelectedServer) ? mcpSelectedServer : (mcpServerNames[0] ?? '');
  const activeMcpServer = parsedMcp.document?.mcpServers[activeMcpServerName] ?? {};
  const activeMcpTransport = effectiveTransport(activeMcpServer);

  const saveMcpDocument = (document: McpDocument, selectedServer?: string) => {
    setMcpJson(serializeMcpDocument(document));
    setMcpJsonError('');
    if (selectedServer !== undefined) setMcpSelectedServer(selectedServer);
  };
  const updateMcpServer = (patch: McpServerConfig, removed: string[] = []) => {
    if (!parsedMcp.document || !Object.hasOwn(parsedMcp.document.mcpServers, activeMcpServerName)) return;
    const next = { ...activeMcpServer, ...patch };
    removed.forEach(key => delete next[key]);
    saveMcpDocument({ mcpServers: { ...parsedMcp.document.mcpServers, [activeMcpServerName]: next } });
  };
  const renameMcpServer = (nextName: string) => {
    if (!parsedMcp.document || !Object.hasOwn(parsedMcp.document.mcpServers, activeMcpServerName) || nextName === activeMcpServerName) return;
    if (nextName && Object.hasOwn(parsedMcp.document.mcpServers, nextName)) {
      setMcpJsonError(`Server“${nextName}”已存在。`);
      return;
    }
    const entries = Object.entries(parsedMcp.document.mcpServers).map(([name, server]) => name === activeMcpServerName ? [nextName, server] : [name, server]);
    setMcpScripts(old => old.map(script => script.server === activeMcpServerName ? { ...script, server: nextName } : script));
    saveMcpDocument({ mcpServers: Object.fromEntries(entries) }, nextName);
  };
  const addMcpServer = () => {
    if (!parsedMcp.document) return;
    let index = mcpServerNames.length + 1;
    let name = `server${index}`;
    while (Object.hasOwn(parsedMcp.document.mcpServers, name)) name = `server${++index}`;
    saveMcpDocument({ mcpServers: { ...parsedMcp.document.mcpServers, [name]: { transport: 'streamable-http', url: '' } } }, name);
  };
  const removeMcpServer = () => {
    if (!parsedMcp.document || !Object.hasOwn(parsedMcp.document.mcpServers, activeMcpServerName)) return;
    const servers = { ...parsedMcp.document.mcpServers };
    delete servers[activeMcpServerName];
    setMcpScripts(old => old.filter(script => script.server !== activeMcpServerName));
    const nextName = Object.keys(servers)[0] ?? '';
    saveMcpDocument({ mcpServers: servers }, nextName);
  };
  const switchMcpMode = (mode: McpEditorMode) => {
    if (mode === 'FORM') {
      if (!parsedMcp.document) { setMcpJsonError(parsedMcp.error); return; }
      saveMcpDocument(parsedMcp.document, activeMcpServerName);
    }
    setMcpMode(mode);
  };
  const addMcpScripts = async (server: string, files: File[]) => {
    const current = mcpScripts.filter(script => script.server === server);
    const incomingNames = new Set<string>();
    if (mcpScripts.length + files.length > MCP_SCRIPT_MAX_FILES) throw new Error(`一个 MCP 配置最多上传 ${MCP_SCRIPT_MAX_FILES} 个脚本文件。`);
    const nextTotal = mcpScripts.reduce((total, script) => total + script.byteSize, 0) + files.reduce((total, file) => total + file.size, 0);
    if (nextTotal > MCP_SCRIPT_MAX_TOTAL_BYTES) throw new Error('MCP 脚本总大小不能超过 10 MiB。');
    const additions: McpScriptAsset[] = [];
    for (const file of files) {
      const extension = file.name.includes('.') ? `.${file.name.split('.').pop()?.toLowerCase()}` : '';
      if (!MCP_SCRIPT_EXTENSIONS.has(extension)) throw new Error(`不支持脚本文件“${file.name}”。`);
      if (file.size > MCP_SCRIPT_MAX_FILE_BYTES) throw new Error(`脚本“${file.name}”不能超过 1 MiB。`);
      if (current.some(script => script.filename === file.name) || incomingNames.has(file.name)) throw new Error(`Server“${server}”中已存在脚本“${file.name}”。`);
      incomingNames.add(file.name);
      additions.push({ server, filename: file.name, byteSize: file.size, contentBase64: toBase64(await file.arrayBuffer()) });
    }
    setMcpScripts(old => [...old, ...additions]);
  };
  const removeMcpScript = (server: string, filename: string) => setMcpScripts(old => old.filter(script => script.server !== server || script.filename !== filename));
  const clearMcpScripts = (server: string) => setMcpScripts(old => old.filter(script => script.server !== server));

  const refresh = async () => { setSelected(new Set()); await qc.invalidateQueries({ queryKey: ['capabilities'] }); };
  const importSkill = async (file: File) => {
    setImportingSkill(true); setError(''); setNotice('');
    try {
      if (file.size > SKILL_ZIP_MAX_BYTES) throw new Error('Skill ZIP 不能超过 25 MiB。');
      const validated = await api.validateCapability({ capability_type: 'SKILL', filename: file.name, content_base64: toBase64(await file.arrayBuffer()) });
      const preview = validated.preview;
      const capabilityCount = preview.capabilities?.length ?? 0;
      const message = `识别到 ${capabilityCount} 个 Skill、${preview.file_count ?? 0} 个有效文件。${preview.ignored_entry_count ? `已忽略 ${preview.ignored_entry_count} 个 macOS 元数据条目。` : ''}`;
      const confirmed = await dialog.confirm({ title: `确认导入 ${file.name}`, message, confirmLabel: `导入 ${capabilityCount} 项能力` });
      if (!confirmed) return;
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); setNotice(`已从 ${file.name} 导入 ${committed.capabilities.length} 项 Skill 能力。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImportingSkill(false); }
  };
  const createMcp = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      const document = parseMcpDocument(mcpJson);
      if (!Object.keys(document.mcpServers).length) throw new Error('至少需要配置一个 MCP Server。');
      const bytes = new TextEncoder().encode(mcpJson);
      if (bytes.byteLength > MCP_JSON_MAX_BYTES) throw new Error('MCP JSON 不能超过 1 MiB。');
      for (const script of mcpScripts) {
        const server = document.mcpServers[script.server];
        if (!server || effectiveTransport(server) !== 'stdio') throw new Error(`脚本“${script.filename}”绑定的 Server“${script.server}”不存在或已不是本地 stdio Server。`);
      }
      const validated = await api.validateCapability({
        capability_type: 'MCP', filename: 'mcp.json', content_base64: toBase64(bytes.buffer),
        mcp_scripts: mcpScripts.map(script => ({ server: script.server, filename: script.filename, content_base64: script.contentBase64 })),
      });
      const capabilityCount = validated.preview.capabilities?.length ?? 0;
      if (!capabilityCount) throw new Error('JSON 中没有可用的 MCP Server。');
      const committed = await api.commitCapability(validated.import_token);
      setMcpOpen(false); setMcpMode('FORM'); setMcpSelectedServer('docs'); setMcpJsonError(''); setMcpJson(MCP_JSON_EXAMPLE); setMcpScripts([]);
      await refresh(); setNotice(`已创建 ${committed.capabilities.length} 项 MCP 能力。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const createHook = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      const bytes = new TextEncoder().encode(hookJson);
      if (bytes.byteLength > MCP_JSON_MAX_BYTES) throw new Error('Hook JSON 不能超过 1 MiB。');
      const validated = await api.validateCapability({
        capability_type: 'HOOK', filename: 'hook.json', content_base64: toBase64(bytes.buffer),
        hook_scripts: hookScripts.map(script => ({ filename: script.filename, content_base64: script.contentBase64 })),
      });
      const capabilityCount = validated.preview.capabilities?.length ?? 0;
      if (!capabilityCount) throw new Error('JSON 中没有可用的 Hook 策略。');
      const committed = await api.commitCapability(validated.import_token);
      setHookOpen(false); setHookJson(HOOK_JSON_EXAMPLE); setHookScripts([]);
      await refresh(); setNotice('已创建 ' + committed.capabilities.length + ' 项 Hook 能力。');
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const remove = async (ids: string[], capabilityCount: number) => {
    if (!ids.length || !await dialog.confirm({ title: `删除所选的 ${capabilityCount} 项能力？`, message: '有关联的记录会保留，其余记录会直接删除。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.deleteCapabilities(ids);
      const blockedIds = new Set(result.blocked.map(item => item.id));
      setSelected(new Set(lineages.filter(group => group.versions.some(item => blockedIds.has(item.id))).map(group => group.id)));
      await qc.invalidateQueries({ queryKey: ['capabilities'] });
      if (result.deleted_ids.length) setNotice(`已删除 ${result.deleted_ids.length} 条无关联能力记录。`);
      if (result.blocked.length) setError(blockedCapabilityMessage(result.blocked));
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const openEditor = async (item: CapabilityAsset) => {
    setBusy(true); setError('');
    try { const loaded = await api.capabilitySource(item.id); setSource(loaded.content); setEditing(item); }
    catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const saveSource = async () => {
    if (!editing) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api.updateCapabilitySource(editing.id, source);
      setEditing(undefined); await refresh(); setNotice(`已保存 ${editing.capability_key}，使用该能力的节点已同步更新。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const saveCollection = async (payload: SkillCollectionWrite) => {
    setBusy(true); setError(''); setNotice('');
    try {
      if (editingCollection) await api.updateSkillCollection(editingCollection.id, payload);
      else await api.createSkillCollection(payload);
      setEditingCollection(undefined);
      await qc.invalidateQueries({ queryKey: ['skill-collections'] });
      setNotice(`已${editingCollection ? '更新' : '创建'} Skill 组合“${payload.name}”；节点添加时会展开为 ${payload.capability_ids.length} 个真实 Skill。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const removeCollection = async (collection: SkillCollection) => {
    if (!await dialog.confirm({ title: `删除 Skill 组合“${collection.name}”？`, message: '只删除虚拟组合，不会删除 Skill，也不会影响已经展开到节点中的真实 Skill 引用。', confirmLabel: '删除组合', tone: 'danger' })) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api.deleteSkillCollection(collection.id);
      await qc.invalidateQueries({ queryKey: ['skill-collections'] });
      setNotice(`已删除 Skill 组合“${collection.name}”，已有节点不受影响。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const toggle = (id: string) => setSelected(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleVisible = () => setSelected(old => {
    const next = new Set(old);
    if (allVisibleSelected) visible.forEach(item => next.delete(item.id));
    else visible.forEach(item => next.add(item.id));
    return next;
  });
  const count = (capabilityType: CapabilityAssetType) => lineages.filter(item => item.latest.capability_type === capabilityType).length;

  return <section className="page capabilities-page">
    <div className="page-head"><div><span className="eyebrow">CAPABILITY REPOSITORY</span><h1>能力仓库</h1><p>统一管理 Skill、Skill 组合、MCP 与 Hook；组合仅用于批量选择真实 Skill。</p></div><div className="capability-import-actions"><label className="primary file-button"><Upload size={15}/>{importingSkill ? '上传中…' : '上传 Skill ZIP'}<input type="file" disabled={importingSkill || busy} accept=".zip" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importSkill(file); }}/></label><button className="secondary" disabled={importingSkill || busy || !capabilities.some(item => item.capability_type === 'SKILL')} onClick={() => { setError(''); setEditingCollection(null); }}><Layers3 size={15}/>新建 Skill 组合</button><button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setMcpOpen(true); }}><Braces size={15}/>新建 MCP</button><button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setHookOpen(true); }}><ShieldCheck size={15}/>新建 Hook</button></div></div>
    {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    <section className="capability-guidance"><ShieldCheck size={20}/><div><b>安全导入与环境隔离</b><span>Skill 会完整物化到节点工作区；MCP 与 Hook 可携带受校验的脚本附件。命令型 Server 与 Command Hook 所需 CLI 请先在终端环境安装并发布。</span></div></section>
    <section className="skill-collection-section"><header><div><Layers3 size={19}/><span><b>Skill 组合</b><small>虚拟的批量选择模板；固定引用具体 Skill 版本，不进入节点或 Runtime。</small></span></div><em>{skillCollections.length} 个组合</em></header>{collectionsLoading ? <div className="empty compact">加载 Skill 组合…</div> : skillCollections.length ? <div className="skill-collection-grid">{skillCollections.map(collection => <article key={collection.id}><header><span>{collection.category || '未分类'}</span><em>{collection.members.length} 项</em></header><h3>{collection.name}</h3><p>{collection.description || '暂无说明'}</p><div>{collection.members.map(member => <code key={member.id}>{member.capability_key}<small>rev {member.revision_number}</small></code>)}</div><footer><button className="secondary" onClick={() => setEditingCollection(collection)}><Pencil size={12}/>编辑</button><button className="ghost" onClick={() => void removeCollection(collection)}><Trash2 size={12}/>删除</button></footer></article>)}</div> : <div className="skill-collection-empty"><span>还没有组合。选择固定版本的 Skill 后，节点可一键批量添加。</span><button className="secondary" disabled={!capabilities.some(item => item.capability_type === 'SKILL')} onClick={() => setEditingCollection(null)}>创建第一个组合</button></div>}</section>
    <div className="capability-tools"><div className="capability-type-tabs">{(['ALL', 'SKILL', 'MCP', 'HOOK'] as const).map(item => <button key={item} className={type === item ? 'active' : ''} onClick={() => setType(item)}>{item === 'ALL' ? '全部' : typeLabel(item)} <span>{item === 'ALL' ? lineages.length : count(item)}</span></button>)}</div><label><Search size={15}/><input aria-label="搜索能力仓库" value={search} placeholder="搜索名称、说明或来源文件" onChange={event => setSearch(event.target.value)}/></label><button className="secondary" disabled={!visible.length} onClick={toggleVisible}><CheckSquare size={14}/>{allVisibleSelected ? '取消全选' : `全选当前结果（${visible.length}）`}</button>{selected.size > 0 && <button className="danger" disabled={busy} onClick={() => void remove(selectedIds, selected.size)}><Trash2 size={14}/>删除所选能力（{selected.size}）</button>}</div>
    {isLoading ? <div className="empty">加载能力仓库…</div> : visible.length ? <div className="capability-card-grid">{visible.map(group => <CapabilityCard key={group.id} group={group} selected={selected.has(group.id)} onToggle={() => toggle(group.id)} onEdit={() => void openEditor(group.latest)} onDelete={() => void remove(group.versions.map(item => item.id), 1)}/>)}</div> : <div className="empty"><FileArchive size={30}/><b>{lineages.length ? '没有匹配能力' : '能力仓库尚为空'}</b><span>{lineages.length ? '调整类型或搜索条件。' : '从右上角上传 Skill ZIP，或新建 MCP / Hook。'}</span></div>}
    {editing && <div className="modal-backdrop"><section className="modal capability-source-editor" role="dialog" aria-label={`编辑 Skill ${editing.capability_key}`}><header><div><span className="eyebrow">EDIT SKILL</span><h2>编辑 {editing.capability_key}</h2></div><button className="ghost" onClick={() => setEditing(undefined)}>关闭</button></header><p>保存会直接更新当前 Skill，并同步到正在使用该能力的节点，不会生成新的能力记录。</p><textarea aria-label="Skill 源码" value={source} onChange={event => setSource(event.target.value)}/><div className="dependency-policy"><b>声明依赖（写入 SKILL.md frontmatter）</b><code>{DEPENDENCY_EXAMPLE}</code><span>所有版本必须精确固定。CLI 必须在平台白名单中；不接受终端命令。</span></div><footer><button className="ghost" onClick={() => setEditing(undefined)}>取消</button><button className="primary" disabled={busy} onClick={() => void saveSource()}>{busy ? '保存中…' : '保存'}</button></footer></section></div>}
    {editingCollection !== undefined && <SkillCollectionEditorDialog collection={editingCollection ?? undefined} skills={capabilities} busy={busy} onClose={() => setEditingCollection(undefined)} onSave={payload => void saveCollection(payload)}/>}
    {mcpOpen && <McpEditorDialog mode={mcpMode} json={mcpJson} jsonError={mcpJsonError || parsedMcp.error} serverNames={mcpServerNames} selectedServer={activeMcpServerName} server={activeMcpServer} transport={activeMcpTransport} scripts={mcpScripts.filter(script => script.server === activeMcpServerName)} busy={busy} onModeChange={switchMcpMode} onJsonChange={value => { setMcpJson(value); setMcpJsonError(''); }} onSelectServer={setMcpSelectedServer} onAddServer={addMcpServer} onRemoveServer={removeMcpServer} onRenameServer={renameMcpServer} onUpdateServer={updateMcpServer} onAddScripts={(server, files) => void addMcpScripts(server, files).catch(reason => setMcpJsonError(errorMessage(reason)))} onRemoveScript={removeMcpScript} onClearScripts={clearMcpScripts} onClose={() => setMcpOpen(false)} onSave={() => void createMcp()}/>}
    {hookOpen && <HookEditorDialog json={hookJson} scripts={hookScripts} busy={busy} onJsonChange={setHookJson} onScriptsChange={setHookScripts} onClose={() => setHookOpen(false)} onSave={() => void createHook()}/>}
  </section>;
}

interface McpEditorDialogProps {
  mode: McpEditorMode;
  json: string;
  jsonError: string;
  serverNames: string[];
  selectedServer: string;
  server: McpServerConfig;
  transport: McpTransport;
  scripts: McpScriptAsset[];
  busy: boolean;
  onModeChange: (mode: McpEditorMode) => void;
  onJsonChange: (value: string) => void;
  onSelectServer: (name: string) => void;
  onAddServer: () => void;
  onRemoveServer: () => void;
  onRenameServer: (name: string) => void;
  onUpdateServer: (patch: McpServerConfig, removed?: string[]) => void;
  onAddScripts: (server: string, files: File[]) => void;
  onRemoveScript: (server: string, filename: string) => void;
  onClearScripts: (server: string) => void;
  onClose: () => void;
  onSave: () => void;
}

function McpEditorDialog(props: McpEditorDialogProps) {
  const {
    mode, json, jsonError, serverNames, selectedServer, server, transport, scripts, busy,
    onModeChange, onJsonChange, onSelectServer, onAddServer, onRemoveServer,
    onRenameServer, onUpdateServer, onAddScripts, onRemoveScript, onClearScripts, onClose, onSave,
  } = props;
  const args = Array.isArray(server.args) ? server.args.filter((item): item is string => typeof item === 'string').join('\n') : '';
  const timeout = typeof server.timeout === 'number' ? String(server.timeout) : '';
  const stringValue = (key: string) => typeof server[key] === 'string' ? String(server[key]) : '';
  const isExecutableScript = (filename: string) => /\.(?:py|js|mjs|cjs|sh)$/i.test(filename);
  const updateTransport = (next: McpTransport) => {
    if (next === 'stdio') onUpdateServer({ transport: next, command: stringValue('command') }, ['type', 'url']);
    else { onClearScripts(selectedServer); onUpdateServer({ transport: next, url: stringValue('url') }, ['type', 'command', 'args', 'cwd']); }
  };
  const setScriptAsEntry = (filename: string) => {
    const extension = filename.split('.').pop()?.toLowerCase();
    const command = extension === 'py' ? 'python' : extension === 'sh' ? 'sh' : 'node';
    onUpdateServer({ command, args: [`scripts/${filename}`] });
  };
  return <div className="modal-backdrop"><section className="modal capability-source-editor mcp-editor" role="dialog" aria-modal="true" aria-label="新建 MCP">
    <header><div><span className="eyebrow">NEW MCP</span><h2>新建 MCP Server</h2></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <p>表单和 JSON 是同一份配置的两种视图。任一视图的修改都会同步到另一视图；表单未展示的高级字段会原样保留。</p>
    <div className="mcp-mode-tabs" role="tablist"><button type="button" role="tab" aria-selected={mode === 'FORM'} className={mode === 'FORM' ? 'active' : ''} onClick={() => onModeChange('FORM')}>表单配置</button><button type="button" role="tab" aria-selected={mode === 'JSON'} className={mode === 'JSON' ? 'active' : ''} onClick={() => onModeChange('JSON')}>JSON 配置</button></div>
    {mode === 'FORM' ? <div className="mcp-form-view">
      <aside className="mcp-server-list"><header><b>Servers</b><button type="button" className="secondary" onClick={onAddServer}>新增</button></header>{serverNames.map(name => <button type="button" key={name} className={selectedServer === name ? 'active' : ''} onClick={() => onSelectServer(name)}><b>{name || '未命名 Server'}</b></button>)}{!serverNames.length && <span>暂无 Server，请点击新增。</span>}</aside>
      <div className="mcp-form">{selectedServer !== '' || serverNames.includes('') ? <>
        <label><span>Server 名称 *</span><input aria-label="Server 名称" value={selectedServer} placeholder="例如 docs" onChange={event => onRenameServer(event.target.value)}/><small>作为能力名和 OpenHands mcp_config 中的唯一键。</small></label>
        <label><span>连接方式 *</span><select aria-label="连接方式" value={transport} onChange={event => updateTransport(event.target.value as McpTransport)}><option value="streamable-http">远程 Streamable HTTP（推荐）</option><option value="http">远程 HTTP</option><option value="sse">远程 SSE</option><option value="stdio">本地命令（stdio）</option></select><small>优先使用 Streamable HTTP；仅按服务端要求选择其他方式。</small></label>
        {transport === 'stdio' ? <><label className="full-row"><span>CLI 命令 *</span><input aria-label="CLI 命令" value={stringValue('command')} placeholder="例如 python、node 或 mcp-tool-server" onChange={event => onUpdateServer({ command: event.target.value })}/><small>可使用终端环境内已安装的 CLI，或把下方上传的脚本设为入口。</small></label><label><span>参数</span><textarea aria-label="CLI 参数" value={args} placeholder={'每行一个参数，例如：\nscripts/server.py\n--readonly'} onChange={event => { const values = event.target.value.split('\n').map(item => item.trim()).filter(Boolean); onUpdateServer(values.length ? { args: values } : {}, values.length ? [] : ['args']); }}/></label><label><span>工作目录</span><input aria-label="工作目录" value={stringValue('cwd')} placeholder="留空则使用 MCP 工作目录" onChange={event => onUpdateServer(event.target.value ? { cwd: event.target.value } : {}, event.target.value ? [] : ['cwd'])}/></label><section className="mcp-script-panel full-row"><header><div><b>脚本资源</b><span>随当前 Server 持久化并解包到 <code>scripts/</code></span></div><label className="secondary file-button">上传脚本<input type="file" multiple accept=".py,.js,.mjs,.cjs,.sh,.json,.yaml,.yml,.toml,.txt" onChange={event => { const files = [...(event.target.files ?? [])]; event.target.value = ''; if (files.length) onAddScripts(selectedServer, files); }}/></label></header>{scripts.length ? <div className="mcp-script-list">{scripts.map(script => <article key={script.filename}><div><b>{script.filename}</b><small>{formatBytes(script.byteSize)} · scripts/{script.filename}</small></div>{isExecutableScript(script.filename) && <button type="button" className="secondary" onClick={() => setScriptAsEntry(script.filename)}>设为入口</button>}<button type="button" className="ghost" onClick={() => onRemoveScript(selectedServer, script.filename)}>移除</button></article>)}</div> : <div className="mcp-script-empty">可上传多个入口脚本和辅助配置；单文件 1 MiB，合计 10 MiB，最多 20 个。</div>}</section></> : <label className="full-row"><span>Server URL *</span><input aria-label="Server URL" type="url" value={stringValue('url')} placeholder="https://mcp.example.com/mcp" onChange={event => onUpdateServer({ url: event.target.value })}/><small>填写 Runtime 容器网络可访问的 HTTP(S) 地址。</small></label>}
        <label><span>超时（秒）</span><input aria-label="超时" type="number" min="1" value={timeout} onChange={event => onUpdateServer(event.target.value ? { timeout: Number(event.target.value) } : {}, event.target.value ? [] : ['timeout'])}/></label>
        <label><span>说明</span><input aria-label="MCP 说明" value={stringValue('description')} placeholder="说明该 Server 提供什么工具" onChange={event => onUpdateServer(event.target.value ? { description: event.target.value } : {}, event.target.value ? [] : ['description'])}/></label>
        <div className="mcp-form-actions full-row"><span>高级字段请切换到 JSON 编辑；表单修改不会删除它们。</span><button type="button" className="danger" onClick={onRemoveServer}>删除当前 Server</button></div>
      </> : <div className="empty compact">请先新增一个 MCP Server。</div>}</div>
    </div> : <div className="mcp-json-editor"><textarea aria-label="MCP JSON" value={json} spellCheck={false} onChange={event => onJsonChange(event.target.value)}/><div className="mcp-config-help"><b>FlowWeave JSON 结构</b><span>根节点使用 <code>mcpServers</code>（兼容输入 <code>servers</code>）。</span><span>远程：<code>url</code> + <code>transport</code>。</span><span>本地：<code>command</code> + <code>args</code> + <code>transport: stdio</code>。</span><span>支持的高级字段：env、headers、auth、enabled、keep_alive、icon 等。</span></div></div>}
    {jsonError && <p className="error" role="alert">{jsonError}</p>}
    <div className="mcp-security-note"><b>凭据与运行环境</b><span>配置禁止包含 token、secret、password、Authorization 等敏感字段。CLI 和依赖来自节点绑定并已发布的终端环境，不会根据配置自动安装。</span></div>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !json.trim() || Boolean(jsonError)} onClick={onSave}>{busy ? '保存中…' : '校验并保存'}</button></footer>
  </section></div>;
}

interface CardProps { group: CapabilityLineage; selected: boolean; onToggle: () => void; onEdit: () => void; onDelete: () => void }
function CapabilityCard({ group, selected, onToggle, onEdit, onDelete }: CardProps) {
  const item = group.latest;
  const dependencyLabel = item.dependency_build_state === 'READY' ? '依赖可用' : item.dependency_build_state === 'PENDING' ? '依赖构建中' : item.dependency_build_state === 'FAILED' ? '依赖构建失败' : '无需额外依赖';
  const totalReferences = group.versions.reduce((total, version) => total + version.reference_count, 0);
  return <article className={`capability-card ${selected ? 'selected' : ''}`}><header><label className="capability-select"><input type="checkbox" aria-label={`选择能力 ${item.capability_key}`} checked={selected} onChange={onToggle}/></label><span className={`capability-card-icon ${item.capability_type.toLowerCase()}`}>{item.capability_type === 'SKILL' ? <FileArchive size={18}/> : <PlugZap size={18}/>}</span><span className="cap-type">{typeLabel(item.capability_type)}</span></header><h3>{item.capability_key}</h3><p>{item.description || '暂无能力说明'}</p><div className={`dependency-state ${item.dependency_build_state.toLowerCase()}`} title={item.dependency_build_error || ''}>{dependencyLabel}{item.dependency_build_error ? `：${item.dependency_build_error}` : ''}</div><dl><dt>来源文件</dt><dd>{item.filename}</dd><dt>文件大小</dt><dd>{formatBytes(item.byte_size)}</dd><dt>更新时间</dt><dd>{new Date(item.created_at).toLocaleString()}</dd><dt>节点引用</dt><dd>{totalReferences} 个</dd></dl><footer>{item.capability_type === 'SKILL' && <button className="secondary" onClick={onEdit}><Pencil size={13}/>编辑</button>}<button className="ghost" title={totalReferences > 0 ? '有关联的记录会保留并说明绑定节点，其余记录直接删除' : '删除能力'} onClick={onDelete}><Trash2 size={13}/>删除能力</button></footer></article>;
}
