import { ViewportPopover } from '../components/ViewportPopover';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, ArrowUpDown, BookOpen, Box, Code2, Search, Webhook, Braces, CheckSquare, ChevronDown, Download, Ellipsis, FileArchive, GitCompareArrows, Layers3, LockKeyhole, Pencil, PlugZap, RotateCcw, Trash2, Upload, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, ApiError, capabilityPackageUrl } from '../api/client';
import { CapabilityPackageDialog } from '../components/CapabilityPackageDialog';
import { CapabilityCollectionEditorDialog } from '../components/CapabilityCollectionEditorDialog';
import { Pagination } from '../components/Pagination';
import { PluginMarketDialog } from '../components/PluginMarketDialog';
import { SkillMarketDialog } from '../components/SkillMarketDialog';
import { AgentProfileHistoryDialog } from '../components/AgentProfileHistoryDialog';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { BlockedCapabilityDelete, CapabilityAsset, CapabilityAssetType, CapabilityLineage, CapabilityCollection, CapabilityCollectionWrite, CapabilitySort, CapabilityCollectionSort, ContextBundleManifest } from '../types';

import './capability-repository.css';

const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
const MCP_JSON_MAX_BYTES = 1024 * 1024;
const MCP_SCRIPT_MAX_FILES = 20;
const MCP_SCRIPT_MAX_FILE_BYTES = 1024 * 1024;
const MCP_SCRIPT_MAX_TOTAL_BYTES = 10 * 1024 * 1024;
const MCP_SCRIPT_EXTENSIONS = new Set(['.py', '.js', '.mjs', '.cjs', '.sh', '.json', '.yaml', '.yml', '.toml', '.txt']);
const MCP_JSON_EXAMPLE = JSON.stringify({
  mcpServers: {
    remote: {
      url: 'https://mcp.example.com',
      transport: 'streamable-http',
      description: '查询团队文档',
      timeout: 30,
    },
  },
}, null, 2);
const CAPABILITY_MODULES: CapabilityAssetType[] = ['SKILL', 'PLUGIN', 'MCP', 'AGENT_DEFINITION', 'CONTEXT', 'HOOK'];
const HOOK_EVENTS = [
  { value: 'pre_tool_use', label: '工具调用前', native: 'PreToolUse', description: '在 OpenHands 执行匹配工具前检查；可阻断调用。', toolScoped: true },
  { value: 'post_tool_use', label: '工具调用后', native: 'PostToolUse', description: '在匹配工具返回后记录或检查；不会阻断既有调用。', toolScoped: true },
  { value: 'user_prompt_submit', label: '用户提交消息', native: 'UserPromptSubmit', description: '用户消息进入 Agent 前运行；可阻断该次提交。', toolScoped: false },
  { value: 'session_start', label: '会话启动', native: 'SessionStart', description: 'OpenHands 初始化会话时运行。', toolScoped: false },
  { value: 'session_end', label: '会话结束', native: 'SessionEnd', description: 'OpenHands 关闭会话时运行。', toolScoped: false },
  { value: 'stop', label: 'Agent 尝试结束', native: 'Stop', description: 'Agent 尝试结束时运行；可阻止结束。', toolScoped: false },
] as const;
type HookEvent = typeof HOOK_EVENTS[number]['value'];
type HookMode = 'PROMPT' | 'SCRIPT';

type McpEditorMode = 'FORM' | 'JSON';
type McpTransport = 'http' | 'streamable-http' | 'sse' | 'stdio';
type McpConnectionKind = 'REMOTE' | 'LOCAL';
type McpServerConfig = Record<string, unknown>;
interface McpDocument { mcpServers: Record<string, McpServerConfig> }
interface McpScriptAsset { server: string; filename: string; contentBase64: string; byteSize: number }

type ContextBundleDraft = Omit<ContextBundleManifest, 'documents'> & {
  documents: Array<{ path: string; title: string; content_sha256: string }>
};

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
  if (transport === 'shttp') return 'http';
  if (typeof server.command === 'string' && server.command.trim()) return 'stdio';
  return 'streamable-http';
}

function connectionKind(server: McpServerConfig): McpConnectionKind {
  return effectiveTransport(server) === 'stdio' ? 'LOCAL' : 'REMOTE';
}

function validateMcpDocumentContract(document: McpDocument): string {
  const entries = Object.entries(document.mcpServers);
  if (entries.length !== 1) return '新建 MCP 一次只能配置一个具名 Server。';
  const [name, server] = entries[0];
  if (!name.trim()) return 'Server 名称不能为空。';
  if (server.type !== undefined && server.transport !== undefined) return 'MCP Server 不能同时配置 type 和 transport。';
  const declaredTransport = server.transport ?? server.type;
  if (declaredTransport !== undefined && !['stdio', 'http', 'streamable-http', 'sse', 'shttp'].includes(String(declaredTransport))) return 'MCP 连接协议无效。';
  const kind = connectionKind(server);
  const transport = effectiveTransport(server);
  const forbidden = kind === 'LOCAL'
    ? ['url', 'headers', 'auth', 'keep_alive', 'sse_read_timeout']
    : ['command', 'args', 'env', 'cwd'];
  const mixedFields = forbidden.filter(field => Object.hasOwn(server, field));
  if (mixedFields.length) return `${kind === 'LOCAL' ? '本地' : '远程'} MCP 不能配置字段：${mixedFields.join('、')}。`;
  if (kind === 'LOCAL' && (typeof server.command !== 'string' || !server.command.trim())) return '本地 MCP 必须填写 CLI 命令。';
  if (kind === 'REMOTE' && (typeof server.url !== 'string' || !server.url.trim())) return '远程 MCP 必须填写 Server URL。';
  if (kind === 'REMOTE' && !['http', 'streamable-http', 'sse'].includes(transport)) return '远程 MCP 协议无效。';
  return '';
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
function typeLabel(type: CapabilityAssetType): string {
  if (type === 'SKILL') return 'Skill';
  if (type === 'PLUGIN') return 'Plugin';
  if (type === 'AGENT_DEFINITION') return 'Agent Definition';
  if (type === 'CONTEXT') return 'Context';
  if (type === 'HOOK') return 'Hook';
  return type;
}
function capabilityModuleDescription(type: CapabilityAssetType): string {
  if (type === 'SKILL') return '可复用指令与工作方法；选择后按具体版本冻结。';
  if (type === 'PLUGIN') return '以固定来源发布的 OpenHands 原生扩展包。';
  if (type === 'MCP') return '受治理的 MCP Server 配置，运行前会检测连接状态。';
  if (type === 'AGENT_DEFINITION') return '可委派的原生 Agent 定义与运行预算。';
  if (type === 'CONTEXT') return '可上传并冻结到节点 OpenHands 系统上下文的文本。';
  if (type === 'HOOK') return '选择原生回调事件，上传提示词或脚本后按会话冻结。';
  return '受治理、可追溯的不可变能力版本。';
}
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
  const unique = [...new Map(blocked.map(item => [`${item.relation}:${item.name}`, item])).values()];
  return `以下能力无法删除，已跳过：${unique.map(item => {
    if (item.relation === 'BUILTIN_CAPABILITY') return `“${item.name}”是系统内置能力，承担节点默认运行策略，不能删除`;
    const nodes = item.nodes?.length ? `节点 ${item.nodes.map(node => `“${node.name}”`).join('、')}` : '';
    const collections = item.collections?.length ? `Skill 组合 ${item.collections.map(collection => `“${collection.name}”`).join('、')}` : '';
    const workspaces = item.workspaces?.length ? `Agent 工作区 ${item.workspaces.map(workspace => `“${workspace.name}”`).join('、')}` : '';
    const conversations = item.conversations?.length ? `Agent 会话 ${item.conversations.map(conversation => `“${conversation.name}”`).join('、')}` : '';
    const governance = item.governance?.length ? '治理记录' : '';
    return `“${item.name}”被${[nodes, collections, workspaces, conversations, governance].filter(Boolean).join('、')}引用`;
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
  const [capabilities, setCapabilities] = useState<CapabilityAsset[]>([]);
  const [collectionsView, setCollectionsView] = useState(false);
  const [type, setType] = useState<CapabilityAssetType>('SKILL');
  const [editing, setEditing] = useState<CapabilityAsset>();
  const [skillComparison, setSkillComparison] = useState<CapabilityAsset>();
  const [contextOpen, setContextOpen] = useState(false);
  const [contextTitle, setContextTitle] = useState('');
  const [contextDescription, setContextDescription] = useState('');
  const [contextContent, setContextContent] = useState('');
  const [contextFile, setContextFile] = useState<File>();
  const [contextBundleManifest, setContextBundleManifest] = useState<ContextBundleDraft>();
  const [viewingContext, setViewingContext] = useState<CapabilityAsset>();
  useEscapeClose(() => setContextOpen(false), contextOpen);
  const [editingCollection, setEditingCollection] = useState<CapabilityCollection | null | undefined>();
  const [mcpOpen, setMcpOpen] = useState(false);
  const [editingMcp, setEditingMcp] = useState<CapabilityAsset>();
  useEscapeClose(() => setMcpOpen(false), mcpOpen);
  const [viewingAgentDefinition, setViewingAgentDefinition] = useState<CapabilityAsset>();
  useEscapeClose(() => setViewingAgentDefinition(undefined), Boolean(viewingAgentDefinition));
  const [hookOpen, setHookOpen] = useState(false);
  const [hookName, setHookName] = useState('');
  const [hookDescription, setHookDescription] = useState('');
  const [hookEvent, setHookEvent] = useState<HookEvent>('pre_tool_use');
  const [hookMatcher, setHookMatcher] = useState('*');
  const [hookMode, setHookMode] = useState<HookMode>('PROMPT');
  const [hookFile, setHookFile] = useState<File>();
  useEscapeClose(() => setHookOpen(false), hookOpen);
  const [skillMarketOpen, setSkillMarketOpen] = useState(false);
  const skillUpload = useRef<HTMLInputElement>(null);
  const [pluginMarketOpen, setPluginMarketOpen] = useState(false);
  const pluginUpload = useRef<HTMLInputElement>(null);
  const [profileHistory, setProfileHistory] = useState<CapabilityLineage>();
  const [mcpMode, setMcpMode] = useState<McpEditorMode>('FORM');
  const [mcpJson, setMcpJson] = useState(MCP_JSON_EXAMPLE);
  const [mcpSelectedServer, setMcpSelectedServer] = useState('remote');
  const [mcpJsonError, setMcpJsonError] = useState('');
  const [mcpScripts, setMcpScripts] = useState<McpScriptAsset[]>([]);
  const [busy, setBusy] = useState(false);
  const [importingSkill, setImportingSkill] = useState(false);
  const [selectedLineageIds, setSelectedLineageIds] = useState<Set<string>>(new Set());
  const [repositoryQuery, setRepositoryQuery] = useState('');
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [sort, setSort] = useState<CapabilitySort>('created_at');
  const [collectionSort, setCollectionSort] = useState<CapabilityCollectionSort>('created_at');
  const [direction, setDirection] = useState<'asc' | 'desc'>('desc');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const selectedGroups = useRef(new Map<string, CapabilityLineage>());
  const [allSelectionIds, setAllSelectionIds] = useState<string[]>([]);
  const directoryParams = { capability_type: type, q: repositoryQuery, page, page_size: pageSize, sort, direction };
  const directory = useQuery({ queryKey: ['capabilities', 'directory', directoryParams], queryFn: () => api.capabilityDirectory(directoryParams), enabled: !collectionsView });
  const collectionParams = { q: repositoryQuery, page, page_size: pageSize, sort: collectionSort, direction };
  const collectionDirectory = useQuery({ queryKey: ['capability-collections', 'directory', collectionParams], queryFn: () => api.capabilityCollectionDirectory(collectionParams), enabled: collectionsView });
  const [counts, setCounts] = useState<Partial<Record<CapabilityAssetType, number>>>({});
  const [collectionTotal, setCollectionTotal] = useState<number>();
  useEffect(() => { if (directory.data) { setCounts(Object.fromEntries(CAPABILITY_MODULES.map(item => [item, directory.data.counts[item] ?? 0]))); setCollectionTotal(directory.data.collection_total); } }, [directory.data]);
  useEffect(() => { if (collectionsView && collectionDirectory.data && !repositoryQuery) setCollectionTotal(collectionDirectory.data.total); }, [collectionsView, collectionDirectory.data, repositoryQuery]);
  const visible = directory.data?.items ?? [];
  const capabilityCollections = collectionDirectory.data?.items ?? [];
  const total = collectionsView ? collectionDirectory.data?.total ?? 0 : directory.data?.total ?? 0;
  const isLoading = collectionsView ? collectionDirectory.isLoading : directory.isLoading;
  const queryError = collectionsView ? collectionDirectory.error : directory.error;
  const selectedVisible = [...selectedLineageIds].map(id => selectedGroups.current.get(id)).filter((item): item is CapabilityLineage => Boolean(item));
  const allVisibleSelected = allSelectionIds.length > 0 && allSelectionIds.every(id => selectedLineageIds.has(id));
  const clearSelection = () => { setSelectedLineageIds(new Set()); selectedGroups.current.clear(); setAllSelectionIds([]); };
  const navigateModule = (next: CapabilityAssetType, collections = false) => {
    setType(next); setCollectionsView(collections); setPage(1); setRepositoryQuery('');
    setSort('created_at'); setCollectionSort('created_at'); setDirection('desc');
    clearSelection(); setError(''); setNotice('');
  };
  const changeSort = (column: CapabilitySort | CapabilityCollectionSort) => {
    const active = collectionsView ? collectionSort : sort;
    if (collectionsView) setCollectionSort(column as CapabilityCollectionSort); else setSort(column as CapabilitySort);
    setDirection(active === column && direction === 'asc' ? 'desc' : 'asc'); setPage(1);
  };
  useEffect(() => { if (page > Math.max(1, Math.ceil(total / pageSize)) && !isLoading && !queryError) setPage(1); }, [total, page, isLoading, queryError]);
  const openCollectionEditor = async (collection: CapabilityCollection | null) => {
    setBusy(true); setError('');
    try {
      const all = await qc.fetchQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
      setCapabilities(all); setEditingCollection(collection);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const parsedMcp = useMemo(() => {
    try { return { document: parseMcpDocument(mcpJson), error: '' }; }
    catch (reason) { return { document: undefined, error: reason instanceof Error ? reason.message : 'MCP JSON 无效。' }; }
  }, [mcpJson]);
  const mcpContractError = parsedMcp.document ? validateMcpDocumentContract(parsedMcp.document) : '';
  const mcpServerNames = parsedMcp.document ? Object.keys(parsedMcp.document.mcpServers) : [];
  const activeMcpServerName = mcpServerNames.includes(mcpSelectedServer) ? mcpSelectedServer : (mcpServerNames[0] ?? '');
  const activeMcpServer = parsedMcp.document?.mcpServers[activeMcpServerName] ?? {};
  const activeMcpTransport = effectiveTransport(activeMcpServer);
  const activeMcpConnectionKind = connectionKind(activeMcpServer);

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
  const switchMcpConnectionKind = (kind: McpConnectionKind) => {
    if (!parsedMcp.document || !Object.hasOwn(parsedMcp.document.mcpServers, activeMcpServerName) || kind === activeMcpConnectionKind) return;
    if (kind === 'LOCAL') {
      const next: McpServerConfig = { ...activeMcpServer, transport: 'stdio', command: '' };
      ['type', 'url', 'headers', 'auth', 'keep_alive', 'sse_read_timeout'].forEach(key => delete next[key]);
      saveMcpDocument({ mcpServers: { [activeMcpServerName]: next } }, activeMcpServerName);
      return;
    }
    setMcpScripts([]);
    const next: McpServerConfig = { ...activeMcpServer, transport: 'streamable-http', url: '' };
    ['type', 'command', 'args', 'env', 'cwd'].forEach(key => delete next[key]);
    saveMcpDocument({ mcpServers: { [activeMcpServerName]: next } }, activeMcpServerName);
  };
  const switchMcpMode = (mode: McpEditorMode) => {
    if (mode === 'FORM') {
      if (!parsedMcp.document) { setMcpJsonError(parsedMcp.error); return; }
      const contractError = validateMcpDocumentContract(parsedMcp.document);
      if (contractError) { setMcpJsonError(contractError); return; }
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

  const refresh = async () => { await qc.invalidateQueries({ queryKey: ['capabilities'] }); };
  const importFileCapability = async (file: File, capabilityType: 'SKILL' | 'PLUGIN' | 'CONTEXT' | 'AGENT_DEFINITION') => {
    setImportingSkill(true); setError(''); setNotice('');
    try {
      if ((capabilityType === 'SKILL' || capabilityType === 'PLUGIN') && !file.name.toLowerCase().endsWith('.zip')) throw new Error(`请选择 ${capabilityType === 'SKILL' ? 'Skill' : 'Plugin'} ZIP 文件。`);
      if (file.size > (capabilityType === 'CONTEXT' || capabilityType === 'AGENT_DEFINITION' ? MCP_JSON_MAX_BYTES : SKILL_ZIP_MAX_BYTES)) throw new Error(capabilityType === 'AGENT_DEFINITION' ? 'Agent Definition Markdown 不能超过 1 MiB。' : capabilityType === 'CONTEXT' ? 'Context 文本不能超过 1 MiB。' : `${capabilityType === 'SKILL' ? 'Skill' : 'Plugin'} ZIP 不能超过 25 MiB。`);
      const validated = await api.validateCapability({ capability_type: capabilityType, filename: file.name, content_base64: toBase64(await file.arrayBuffer()) });
      const preview = validated.preview;
      const capabilityCount = preview.capabilities?.length ?? 0;
      const label = capabilityType === 'SKILL' ? 'Skill' : capabilityType === 'PLUGIN' ? 'Plugin' : capabilityType === 'AGENT_DEFINITION' ? 'Agent Definition' : 'Context';
      const message = capabilityType === 'CONTEXT' ? `将 ${file.name} 冻结为 1 个 Context 版本，并在节点选择时作为 OpenHands 系统上下文。` : capabilityType === 'AGENT_DEFINITION' ? `将 ${file.name} 解析为 1 个 OpenHands Agent Definition，并冻结其 frontmatter 与系统提示词。` : `识别到 ${capabilityCount} 个 ${label}、${preview.file_count ?? 0} 个有效文件。${preview.ignored_entry_count ? `已忽略 ${preview.ignored_entry_count} 个 macOS 元数据条目。` : ''}`;
      const confirmed = await dialog.confirm({ title: `确认导入 ${file.name}`, message, confirmLabel: `导入 ${capabilityCount} 项能力` });
      if (!confirmed) return;
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); setNotice(`已从 ${file.name} 导入 ${committed.capabilities.length} 项 ${label} 能力。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImportingSkill(false); }
  };
  const closeContextForm = () => {
    setContextOpen(false); setContextTitle(''); setContextDescription(''); setContextContent(''); setContextFile(undefined); setContextBundleManifest(undefined);
  };
  const createContext = async () => {
    setImportingSkill(true); setError(''); setNotice('');
    try {
      const title = contextTitle.trim();
      const content = contextContent.trim();
      const sourceFile = contextFile ?? (content ? new File([content], 'context.txt', { type: 'text/plain;charset=utf-8' }) : undefined);
      if (!title) throw new Error('请填写 Context 标题。');
      if (!sourceFile) throw new Error('请直接编写内容，或选择一个 Context 文件或资料包。');
      if (sourceFile.size > MCP_JSON_MAX_BYTES) throw new Error('Context 文本或资料包 ZIP 不能超过 1 MiB。');
      const validated = await api.validateCapability({
        capability_type: 'CONTEXT', filename: sourceFile.name,
        content_base64: toBase64(await sourceFile.arrayBuffer()),
        context_title: title, context_description: contextDescription.trim(),
        context_bundle_manifest: contextBundleManifest && {
          entrypoint: contextBundleManifest.entrypoint,
          documents: contextBundleManifest.documents.map(({ path, title: documentTitle }) => ({ path, title: documentTitle })),
          conflict_policy: contextBundleManifest.conflict_policy,
        },
      });
      const normalized = validated.preview.capabilities?.[0]?.normalized_config;
      const suggestedManifest = normalized?.manifest as ContextBundleDraft | undefined;
      if (sourceFile.name.toLowerCase().endsWith('.zip') && !contextBundleManifest && suggestedManifest) {
        setContextBundleManifest(suggestedManifest);
        return;
      }
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); closeContextForm();
      setNotice(`已发布 Context“${committed.capabilities[0]?.capability_key ?? title}”。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImportingSkill(false); }
  };
  const closeHookForm = () => {
    setHookOpen(false); setHookName(''); setHookDescription(''); setHookEvent('pre_tool_use'); setHookMatcher('*'); setHookMode('PROMPT'); setHookFile(undefined);
  };
  const createHook = async () => {
    setImportingSkill(true); setError(''); setNotice('');
    try {
      const name = hookName.trim();
      if (!name) throw new Error('请填写 Hook 名称。');
      if (!hookFile) throw new Error('请上传一个提示词或脚本文件。');
      if (hookFile.size > MCP_JSON_MAX_BYTES) throw new Error('Hook 文件不能超过 1 MiB。');
      const event = HOOK_EVENTS.find(item => item.value === hookEvent);
      const matcher = event?.toolScoped ? hookMatcher.trim() || '*' : '*';
      const validated = await api.validateCapability({
        capability_type: 'HOOK', filename: hookFile.name, content_base64: toBase64(await hookFile.arrayBuffer()),
        hook_name: name, hook_description: hookDescription.trim(), hook_event: hookEvent, hook_matcher: matcher, hook_mode: hookMode,
      });
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); closeHookForm();
      setNotice(`已发布 Hook“${committed.capabilities[0]?.capability_key ?? name}”。它会在新建会话时冻结并由 OpenHands 原生执行。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImportingSkill(false); }
  };
  const openContextSource = (item: CapabilityAsset) => setViewingContext(item);
  const openMcpEditor = async (item?: CapabilityAsset) => {
    setBusy(true); setError(''); setNotice('');
    try {
      if (item) {
        const loaded = await api.mcpSource(item.id);
        setMcpJson(loaded.content);
        setMcpScripts(loaded.mcp_scripts.map(script => ({ ...script, contentBase64: script.content_base64, byteSize: Math.floor(script.content_base64.length * 3 / 4) })));
        setMcpSelectedServer(loaded.capability_key);
        setEditingMcp(item);
      } else {
        setMcpJson(MCP_JSON_EXAMPLE); setMcpScripts([]); setMcpSelectedServer('remote'); setEditingMcp(undefined);
      }
      setMcpMode('FORM'); setMcpJsonError(''); setMcpOpen(true);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const createMcp = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      const document = parseMcpDocument(mcpJson);
      const contractError = validateMcpDocumentContract(document);
      if (contractError) throw new Error(contractError);
      const bytes = new TextEncoder().encode(mcpJson);
      if (bytes.byteLength > MCP_JSON_MAX_BYTES) throw new Error('MCP JSON 不能超过 1 MiB。');
      for (const script of mcpScripts) {
        const server = document.mcpServers[script.server];
        if (!server || effectiveTransport(server) !== 'stdio') throw new Error(`脚本“${script.filename}”绑定的 Server“${script.server}”不存在或已不是本地 stdio Server。`);
      }
      const scripts = mcpScripts.map(script => ({ server: script.server, filename: script.filename, content_base64: script.contentBase64 }));
      if (editingMcp) await api.updateMcpSource(editingMcp.id, mcpJson, scripts);
      else {
        const validated = await api.validateCapability({ capability_type: 'MCP', filename: 'mcp.json', content_base64: toBase64(bytes.buffer), mcp_scripts: scripts });
        const capabilityCount = validated.preview.capabilities?.length ?? 0;
        if (!capabilityCount) throw new Error('JSON 中没有可用的 MCP Server。');
        await api.commitCapability(validated.import_token);
      }
      setMcpOpen(false); setMcpMode('FORM'); setMcpSelectedServer('remote'); setMcpJsonError(''); setMcpJson(MCP_JSON_EXAMPLE); setMcpScripts([]);
      setEditingMcp(undefined);
      await refresh(); setNotice(editingMcp ? `已发布 MCP“${editingMcp.capability_key}”的新版本。` : '已创建 MCP 能力。');
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const toggleLineage = (group: CapabilityLineage) => {
    selectedGroups.current.set(group.id, group);
    setSelectedLineageIds(old => { const next = new Set(old); if (next.has(group.id)) next.delete(group.id); else next.add(group.id); return next; });
  };
  const toggleVisible = async () => {
    if (allVisibleSelected) { clearSelection(); return; }
    setBusy(true); setError('');
    try {
      const all = await qc.fetchQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
      const needle = repositoryQuery.trim().toLocaleLowerCase();
      const groups = groupCapabilities(all).filter(group => group.latest.capability_type === type && !group.latest.is_builtin
        && (!needle || `${group.latest.capability_key} ${group.latest.description} ${group.latest.filename}`.toLocaleLowerCase().includes(needle)));
      groups.forEach(group => selectedGroups.current.set(group.id, group));
      const ids = groups.map(group => group.id); setAllSelectionIds(ids); setSelectedLineageIds(new Set(ids));
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const remove = async (groups: CapabilityLineage[], retainBlockedSelection = false) => {
    if (!groups.length || !await dialog.confirm({ title: `删除所选的 ${groups.length} 项能力？`, message: '将删除所选能力；已有会话或运行引用的冻结快照会被保留，确保历史记录仍可恢复。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const selectedIds = new Set(groups.map(group => group.id));
      groups = groupCapabilities(await api.capabilities()).filter(group => selectedIds.has(group.id));
      const ids = groups.flatMap(group => group.versions.map(item => item.id));
      const deletedIds: string[] = [];
      const blocked: BlockedCapabilityDelete[] = [];
      const updatedCollections = new Set<string>();
      const deletedCollections = new Set<string>();
      for (let start = 0; start < ids.length; start += 100) {
        const result = await api.deleteCapabilities(ids.slice(start, start + 100));
        deletedIds.push(...result.deleted_ids);
        blocked.push(...result.blocked);
        result.collection_changes.updated.forEach(name => updatedCollections.add(name));
        result.collection_changes.deleted.forEach(name => deletedCollections.add(name));
      }
      const blockedIds = new Set(blocked.map(item => item.id));
      setSelectedLineageIds(old => {
        const next = new Set(old);
        groups.forEach(group => next.delete(group.id));
        if (retainBlockedSelection) groups.filter(group => group.versions.some(item => blockedIds.has(item.id))).forEach(group => next.add(group.id));
        return next;
      });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['capabilities'] }),
        qc.invalidateQueries({ queryKey: ['capability-collections'] }),
      ]);
      if (deletedIds.length) {
        const collectionNote = [
          updatedCollections.size ? `已从 ${updatedCollections.size} 个 Skill 组合移除` : '',
          deletedCollections.size ? `已清理 ${deletedCollections.size} 个空 Skill 组合` : '',
        ].filter(Boolean).join('；');
        setNotice(`已删除 ${deletedIds.length} 条无关联能力记录。${collectionNote ? ` ${collectionNote}。` : ''}`);
      }
      if (blocked.length) setError(blockedCapabilityMessage(blocked));
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const openEditor = (item: CapabilityAsset) => setEditing(item);
  const compareSkillVersions = (item: CapabilityAsset) => setSkillComparison(item);
  const rollbackSkillVersion = async (item: CapabilityAsset) => {
    const confirmed = await dialog.confirm({
      title: `回退 ${item.capability_key}？`,
      message: '将移除当前版本，并恢复上一版本为当前版本。已经创建的会话和运行继续使用其冻结的能力快照。',
      confirmLabel: '确认回退', tone: 'danger',
    });
    if (!confirmed) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api.rollbackSkillVersion(item.id);
      await refresh();
      setNotice(`已回退 ${typeLabel(item.capability_type)}“${item.capability_key}”到上一版本。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const saveCollection = async (payload: CapabilityCollectionWrite) => {
    setBusy(true); setError(''); setNotice('');
    try {
      if (editingCollection) await api.updateCapabilityCollection(editingCollection.id, payload);
      else await api.createCapabilityCollection(payload);
      setEditingCollection(undefined);
      await qc.invalidateQueries({ queryKey: ['capability-collections'] });
      setNotice(`已${editingCollection ? '更新' : '创建'} Skill 组合“${payload.name}”；节点添加时会展开为 ${payload.capability_ids.length} 个真实 Skill 版本。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const removeCollection = async (collection: CapabilityCollection) => {
    if (!await dialog.confirm({ title: `删除 Skill 组合“${collection.name}”？`, message: '只删除选择模板，不会删除 Skill 版本，也不会影响已经展开到节点中的真实引用。', confirmLabel: '删除组合', tone: 'danger' })) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await api.deleteCapabilityCollection(collection.id);
      await qc.invalidateQueries({ queryKey: ['capability-collections'] });
      setNotice(`已删除 Skill 组合“${collection.name}”，已有节点不受影响。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const moduleIcon = (item: CapabilityAssetType) => item === 'SKILL' ? <BookOpen size={16}/> : item === 'PLUGIN' ? <Box size={16}/> : item === 'MCP' ? <PlugZap size={16}/> : item === 'HOOK' ? <Webhook size={16}/> : item === 'AGENT_DEFINITION' ? <Code2 size={16}/> : <FileArchive size={16}/>;
  const heading = collectionsView ? 'Skill 组合' : typeLabel(type);
  const sortHeader = (label: string, column: CapabilitySort | CapabilityCollectionSort) => {
    const active = (collectionsView ? collectionSort : sort) === column;
    return <th scope="col" aria-sort={active ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}><button onClick={() => changeSort(column)}>{label}{active ? direction === 'asc' ? <ArrowUp size={12}/> : <ArrowDown size={12}/> : <ArrowUpDown size={12}/>}</button></th>;
  };
  return <section className="page capabilities-page capability-repository">
    <nav className="capability-repository-sidebar" aria-label="能力模块"><header><Layers3 size={18}/><b>能力仓库</b></header>{CAPABILITY_MODULES.map(item => <div key={item}><button disabled={busy || importingSkill} className={!collectionsView && type === item ? 'active' : ''} aria-current={!collectionsView && type === item ? 'page' : undefined} title={capabilityModuleDescription(item)} onClick={() => navigateModule(item)}>{moduleIcon(item)}<span>{typeLabel(item)}</span><em>{counts[item] ?? '—'}</em></button>{item === 'SKILL' && <button disabled={busy || importingSkill} className={`collection-entry ${collectionsView ? 'active' : ''}`} aria-current={collectionsView ? 'page' : undefined} onClick={() => navigateModule('SKILL', true)}><Layers3 size={14}/><span>Skill 组合</span><em>{collectionTotal ?? '—'}</em></button>}</div>)}<footer>统一管理 · 按版本使用</footer></nav>
    {skillMarketOpen && <SkillMarketDialog onClose={() => setSkillMarketOpen(false)} onPublished={async () => { await refresh(); setNotice('Skill 已导入能力仓库。'); }}/>}
    {pluginMarketOpen && <PluginMarketDialog onClose={() => setPluginMarketOpen(false)} onPublished={async () => { await refresh(); setNotice('Plugin 已导入能力仓库。'); }}/>}
    <div className="capability-module-scroll capability-repository-main"><header className="capability-repository-heading"><h2>{heading}</h2><span>{total} 项</span><small>{collectionsView ? '组合用于批量选择 Skill，运行时仍按实际版本使用。' : capabilityModuleDescription(type)}</small></header><div className="capability-notice-layer">{notice && <div className="notice success capability-notice" role="status"><span>{notice}</span><button type="button" aria-label="关闭成功提示" title="关闭" onClick={() => setNotice('')}><X size={15}/></button></div>}</div>{error && <div className="notice error" role="alert">{error}</div>}
    <div className="capability-bulk-tools"><span>{repositoryQuery ? `找到 ${total} 项匹配的 ${heading}` : `共 ${total} 项 ${heading}`}{visible.some(group => group.latest.is_builtin) ? ` · ${visible.filter(group => group.latest.is_builtin).length} 项系统内置不可删除` : ''}</span><div className="capability-list-controls"><label className="capability-repository-search"><Search size={14}/><input aria-label="搜索当前能力模块" maxLength={200} disabled={busy} value={repositoryQuery} onChange={event => { setRepositoryQuery(event.target.value); setPage(1); clearSelection(); }} placeholder={`搜索 ${heading} 名称、说明或文件…`}/>{repositoryQuery && <button type="button" aria-label="清除搜索" onClick={() => { setRepositoryQuery(''); setPage(1); clearSelection(); }}>×</button>}</label>{collectionsView ? <button className="primary" disabled={busy} onClick={() => void openCollectionEditor(null)}><Layers3 size={14}/>新建 Skill 组合</button> : <>{type === 'SKILL' && <><input ref={skillUpload} aria-label="Skill ZIP 文件" type="file" hidden disabled={importingSkill || busy} accept=".zip" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFileCapability(file, 'SKILL'); }}/><button className="secondary" disabled={busy || importingSkill} onClick={() => setSkillMarketOpen(true)}>Skill 市场</button><button className="primary" title="导入 Skill ZIP 文件" disabled={busy || importingSkill} onClick={() => { setError(''); setNotice(''); skillUpload.current?.click(); }}><Upload size={15}/>{importingSkill ? '上传中…' : '导入 Skill'}</button></>}{type === 'PLUGIN' && <><input ref={pluginUpload} aria-label="Plugin ZIP 文件" type="file" hidden disabled={importingSkill || busy} accept=".zip" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFileCapability(file, 'PLUGIN'); }}/><button className="secondary" disabled={busy || importingSkill} onClick={() => setPluginMarketOpen(true)}>插件市场</button><button className="primary" title="导入 Plugin ZIP 文件" disabled={busy || importingSkill} onClick={() => { setError(''); setNotice(''); pluginUpload.current?.click(); }}><Upload size={14}/>{importingSkill ? '上传中…' : '导入 Plugin'}</button></>}{type === 'CONTEXT' && <button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setNotice(''); setContextOpen(true); }}><Upload size={15}/>新增 Context</button>}{type === 'MCP' && <button className="primary" disabled={importingSkill || busy} onClick={() => void openMcpEditor()}><Braces size={15}/>新建 MCP</button>}{type === 'HOOK' && <button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setNotice(''); setHookOpen(true); }}><Upload size={15}/>新建 Hook</button>}{type === 'AGENT_DEFINITION' && <label className="primary file-button"><Upload size={15}/>{importingSkill ? '上传中…' : '上传 Agent Definition Markdown'}<input type="file" disabled={importingSkill || busy} accept=".md,.markdown,text/markdown" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFileCapability(file, 'AGENT_DEFINITION'); }}/></label>}</>}</div>{!collectionsView && <div className="bulk-actions"><button className="secondary" disabled={!total || busy} onClick={() => void toggleVisible()}><CheckSquare size={14}/>{allVisibleSelected ? '取消全选' : '全选当前模块'}</button><button className="danger" disabled={!selectedVisible.length || busy} onClick={() => void remove(selectedVisible, true)}><Trash2 size={14}/>{busy ? '删除中…' : `批量删除 (${selectedVisible.length})`}</button></div>}</div>
    {queryError && <div className="notice error" role="alert">{errorMessage(queryError)}<button className="secondary" onClick={() => void (collectionsView ? collectionDirectory.refetch() : directory.refetch())}>重试</button></div>}
    <div className="capability-table-scroll" key={`${heading}-${page}-${sort}-${collectionSort}-${direction}`} aria-busy={isLoading}>
      <table className={`capability-table${collectionsView ? ' capability-collection-table' : ''}`}><thead><tr>{!collectionsView && <th scope="col" className="capability-check-column"><span className="sr-only">选择</span></th>}{sortHeader('名称', 'name')}{collectionsView ? <>{sortHeader('分类', 'category')}{sortHeader('Skill 数量', 'member_count')}{sortHeader('创建时间', 'created_at')}{sortHeader('更新时间', 'updated_at')}</> : <><th scope="col">来源文件</th>{sortHeader('大小', 'byte_size')}{sortHeader('版本', 'revision_number')}{sortHeader('上传时间', 'created_at')}<th scope="col">引用</th><th scope="col">依赖状态</th></>}<th scope="col" className="capability-actions-column">操作</th></tr></thead><tbody>
      {!isLoading && (collectionsView ? capabilityCollections.map(collection => <tr key={collection.id}><td className="capability-name-cell"><b title={collection.name}>{collection.name}</b><small title={collection.description}>{collection.description || '暂无说明'}</small></td><td>{collection.category || '未分类'}</td><td>{collection.members.length} 项</td><td>{new Date(collection.created_at).toLocaleString()}</td><td>{new Date(collection.updated_at).toLocaleString()}</td><td><CapabilityOverflowMenu label={`Skill 组合 ${collection.name}`}>{close => <><button type="button" role="menuitem" disabled={busy} onClick={() => { void openCollectionEditor(collection); close(); }}><Pencil size={13}/>编辑</button><button type="button" className="danger" role="menuitem" onClick={() => { void removeCollection(collection); close(); }}><Trash2 size={13}/>删除</button></>}</CapabilityOverflowMenu></td></tr>) : visible.map(group => <CapabilityCard key={group.id} group={group} selected={selectedLineageIds.has(group.id)} onToggle={() => toggleLineage(group)} onEdit={() => group.latest.capability_type === 'MCP' ? void openMcpEditor(group.latest) : void openEditor(group.latest)} onCompare={() => void compareSkillVersions(group.latest)} onRollback={() => void rollbackSkillVersion(group.latest)} onViewContext={() => void openContextSource(group.latest)} onViewAgentDefinition={() => setViewingAgentDefinition(group.latest)} onProfileHistory={() => setProfileHistory(group)} onDelete={() => void remove([group])}/>))}
      </tbody></table>{isLoading ? <div className="empty">加载 {heading}…</div> : !total && !queryError && <div className="empty"><FileArchive size={28}/><b>{repositoryQuery ? `没有匹配的 ${heading}` : `暂无 ${heading}`}</b><span>{repositoryQuery ? '调整搜索条件，或清除搜索查看全部能力。' : '使用列表工具栏创建或导入。'}</span></div>}
    </div><footer className="capability-repository-footer"><span>每页 {pageSize} 项{selectedVisible.length ? ` · 已选 ${selectedVisible.length} 项` : ''}</span><Pagination page={page} pageSize={pageSize} total={total} onPageChange={setPage}/></footer></div>


    {profileHistory && <AgentProfileHistoryDialog packageId={profileHistory.id} capabilityKey={profileHistory.latest.capability_key} onClose={() => setProfileHistory(undefined)}/>}
    {contextOpen && <ContextDialog title={contextTitle} description={contextDescription} content={contextContent} file={contextFile} manifest={contextBundleManifest} busy={importingSkill} onTitleChange={setContextTitle} onDescriptionChange={setContextDescription} onContentChange={setContextContent} onFileChange={file => { setContextFile(file); setContextContent(''); setContextBundleManifest(undefined); }} onManifestChange={setContextBundleManifest} onClose={closeContextForm} onSave={() => void createContext()}/>}
    {hookOpen && <HookEditorDialog name={hookName} description={hookDescription} event={hookEvent} matcher={hookMatcher} mode={hookMode} file={hookFile} busy={importingSkill} onNameChange={setHookName} onDescriptionChange={setHookDescription} onEventChange={setHookEvent} onMatcherChange={setHookMatcher} onModeChange={mode => { setHookMode(mode); setHookFile(undefined); }} onFileChange={setHookFile} onClose={closeHookForm} onSave={() => void createHook()}/>}
    {viewingContext && <CapabilityPackageDialog key={viewingContext.id} item={viewingContext} onClose={() => setViewingContext(undefined)} onPublished={refresh}/>}
    {editing && <CapabilityPackageDialog key={editing.id} item={editing} onClose={() => setEditing(undefined)} onPublished={async () => { await refresh(); setNotice(`已发布 ${editing.capability_key} 的新版本；已有会话继续使用冻结版本。`); }}/>}
    {skillComparison && <CapabilityPackageDialog key={`compare:${skillComparison.id}`} item={skillComparison} comparison onClose={() => setSkillComparison(undefined)} onPublished={refresh}/>}
    {editingCollection !== undefined && <CapabilityCollectionEditorDialog collection={editingCollection ?? undefined} capabilities={capabilities} busy={busy} onClose={() => setEditingCollection(undefined)} onSave={payload => void saveCollection(payload)}/>}
    {mcpOpen && <McpEditorDialog editing={Boolean(editingMcp)} mode={mcpMode} json={mcpJson} jsonError={mcpJsonError || parsedMcp.error || mcpContractError} selectedServer={activeMcpServerName} server={activeMcpServer} transport={activeMcpTransport} connectionKind={activeMcpConnectionKind} scripts={mcpScripts.filter(script => script.server === activeMcpServerName)} busy={busy} onModeChange={switchMcpMode} onJsonChange={value => { setMcpJson(value); setMcpJsonError(''); }} onConnectionKindChange={switchMcpConnectionKind} onRenameServer={renameMcpServer} onUpdateServer={updateMcpServer} onAddScripts={(server, files) => void addMcpScripts(server, files).catch(reason => setMcpJsonError(errorMessage(reason)))} onRemoveScript={removeMcpScript} onClose={() => { setMcpOpen(false); setEditingMcp(undefined); }} onSave={() => void createMcp()}/>}
    {viewingAgentDefinition && <AgentDefinitionDetailDialog item={viewingAgentDefinition} onClose={() => setViewingAgentDefinition(undefined)} />}
  </section>;
}

interface McpEditorDialogProps {
  editing: boolean;
  mode: McpEditorMode;
  json: string;
  jsonError: string;
  selectedServer: string;
  server: McpServerConfig;
  transport: McpTransport;
  connectionKind: McpConnectionKind;
  scripts: McpScriptAsset[];
  busy: boolean;
  onModeChange: (mode: McpEditorMode) => void;
  onJsonChange: (value: string) => void;
  onConnectionKindChange: (kind: McpConnectionKind) => void;
  onRenameServer: (name: string) => void;
  onUpdateServer: (patch: McpServerConfig, removed?: string[]) => void;
  onAddScripts: (server: string, files: File[]) => void;
  onRemoveScript: (server: string, filename: string) => void;
  onClose: () => void;
  onSave: () => void;
}

function McpEditorDialog(props: McpEditorDialogProps) {
  const {
    editing, mode, json, jsonError, selectedServer, server, transport, connectionKind, scripts, busy,
    onModeChange, onJsonChange, onConnectionKindChange,
    onRenameServer, onUpdateServer, onAddScripts, onRemoveScript, onClose, onSave,
  } = props;
  const args = Array.isArray(server.args) ? server.args.filter((item): item is string => typeof item === 'string').join('\n') : '';
  const timeout = typeof server.timeout === 'number' ? String(server.timeout) : '';
  const stringValue = (key: string) => typeof server[key] === 'string' ? String(server[key]) : '';
  const isExecutableScript = (filename: string) => /\.(?:py|js|mjs|cjs|sh)$/i.test(filename);
  const updateTransport = (next: McpTransport) => {
    onUpdateServer({ transport: next }, ['type']);
  };
  const setScriptAsEntry = (filename: string) => {
    const extension = filename.split('.').pop()?.toLowerCase();
    const command = extension === 'py' ? 'python' : extension === 'sh' ? 'sh' : 'node';
    onUpdateServer({ command, args: [`scripts/${filename}`] });
  };
  return <div className="modal-backdrop capability-editor-backdrop"><section className="modal capability-source-editor mcp-editor" role="dialog" aria-modal="true" aria-label={editing ? '编辑 MCP' : '新建 MCP'}>
    <header><div><span className="eyebrow">{editing ? 'EDIT MCP' : 'NEW MCP'}</span><h2>{editing ? '编辑 MCP Server' : '新建 MCP Server'}</h2></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <p>{editing ? '保存会发布新的不可变 MCP 版本；Server 名称保持不变，已有会话和 Run Snapshot 继续引用原版本。' : '选择远程或本地连接形态后填写对应配置。表单和 JSON 是同一份单 Server 配置的两种视图。'}</p>
    <div className="mcp-mode-tabs" role="tablist"><button type="button" role="tab" aria-selected={mode === 'FORM'} className={mode === 'FORM' ? 'active' : ''} onClick={() => onModeChange('FORM')}>表单配置</button><button type="button" role="tab" aria-selected={mode === 'JSON'} className={mode === 'JSON' ? 'active' : ''} onClick={() => onModeChange('JSON')}>JSON 配置</button></div>
    {mode === 'FORM' ? <div className="mcp-form-view">
      <aside className="mcp-server-list" aria-label="MCP 连接类型"><header><b>连接类型</b></header><button type="button" className={connectionKind === 'REMOTE' ? 'active' : ''} aria-pressed={connectionKind === 'REMOTE'} onClick={() => onConnectionKindChange('REMOTE')}><span><b>远程</b><small>通过 URL 连接 MCP 服务</small></span></button><button type="button" className={connectionKind === 'LOCAL' ? 'active' : ''} aria-pressed={connectionKind === 'LOCAL'} onClick={() => onConnectionKindChange('LOCAL')}><span><b>本地</b><small>通过 stdio 启动本地命令</small></span></button></aside>
      <div className="mcp-form"><>
        <label><span>Server 名称 *</span><input aria-label="Server 名称" value={selectedServer} placeholder={connectionKind === 'REMOTE' ? '例如 remote' : '例如 local'} disabled={editing} onChange={event => onRenameServer(event.target.value)}/><small>{editing ? '已发布 MCP 的身份不可修改。' : '作为能力名和 OpenHands mcp_config 中的唯一键。'}</small></label>
        {connectionKind === 'REMOTE' ? <label><span>远程协议 *</span><select aria-label="远程协议" value={transport} onChange={event => updateTransport(event.target.value as McpTransport)}><option value="streamable-http">Streamable HTTP（推荐）</option><option value="http">HTTP</option><option value="sse">SSE</option></select><small>仅按 MCP 服务端实际支持的协议选择。</small></label> : <label><span>连接协议</span><input aria-label="连接协议" value="stdio" disabled/><small>本地 MCP 固定通过标准输入输出连接。</small></label>}
        {transport === 'stdio' ? <><label className="full-row"><span>CLI 命令 *</span><input aria-label="CLI 命令" value={stringValue('command')} placeholder="例如 python、node 或 mcp-tool-server" onChange={event => onUpdateServer({ command: event.target.value })}/><small>可使用终端环境内已安装的 CLI，或把下方上传的脚本设为入口。</small></label><label><span>参数</span><textarea aria-label="CLI 参数" value={args} placeholder={'每行一个参数，例如：\nscripts/server.py\n--readonly'} onChange={event => { const values = event.target.value.split('\n').map(item => item.trim()).filter(Boolean); onUpdateServer(values.length ? { args: values } : {}, values.length ? [] : ['args']); }}/></label><label><span>工作目录</span><input aria-label="工作目录" value={stringValue('cwd')} placeholder="留空则使用 MCP 工作目录" onChange={event => onUpdateServer(event.target.value ? { cwd: event.target.value } : {}, event.target.value ? [] : ['cwd'])}/></label><section className="mcp-script-panel full-row"><header><div><b>脚本资源</b><span>随当前 Server 持久化并解包到 <code>scripts/</code></span></div><label className="secondary file-button">上传脚本<input type="file" multiple accept=".py,.js,.mjs,.cjs,.sh,.json,.yaml,.yml,.toml,.txt" onChange={event => { const files = [...(event.target.files ?? [])]; event.target.value = ''; if (files.length) onAddScripts(selectedServer, files); }}/></label></header>{scripts.length ? <div className="mcp-script-list">{scripts.map(script => <article key={script.filename}><div><b>{script.filename}</b><small>{formatBytes(script.byteSize)} · scripts/{script.filename}</small></div>{isExecutableScript(script.filename) && <button type="button" className="secondary" onClick={() => setScriptAsEntry(script.filename)}>设为入口</button>}<button type="button" className="ghost" onClick={() => onRemoveScript(selectedServer, script.filename)}>移除</button></article>)}</div> : <div className="mcp-script-empty">可上传多个入口脚本和辅助配置；单文件 1 MiB，合计 10 MiB，最多 20 个。</div>}</section></> : <label className="full-row"><span>Server URL *</span><input aria-label="Server URL" type="url" value={stringValue('url')} placeholder="https://mcp.example.com/mcp" onChange={event => onUpdateServer({ url: event.target.value })}/><small>填写 Runtime 容器网络可访问的 HTTP(S) 地址。</small></label>}
        <label><span>超时（秒）</span><input aria-label="超时" type="number" min="1" value={timeout} onChange={event => onUpdateServer(event.target.value ? { timeout: Number(event.target.value) } : {}, event.target.value ? [] : ['timeout'])}/></label>
        <label><span>说明</span><input aria-label="MCP 说明" value={stringValue('description')} placeholder="说明该 Server 提供什么工具" onChange={event => onUpdateServer(event.target.value ? { description: event.target.value } : {}, event.target.value ? [] : ['description'])}/></label>
        <div className="mcp-form-actions full-row"><span>高级字段请切换到 JSON 编辑；字段仍受远程 / 本地连接契约限制。</span></div>
      </></div>
    </div> : <div className="mcp-json-editor"><textarea aria-label="MCP JSON" value={json} spellCheck={false} onChange={event => onJsonChange(event.target.value)}/><div className="mcp-config-help"><b>FlowWeave JSON 结构</b><span>新建 MCP 一次只接受一个具名 Server。</span><span>远程：<code>url</code> + <code>transport</code>，可使用 headers、auth、keep_alive。</span><span>本地：<code>command</code> + <code>args</code> + <code>transport: stdio</code>，可使用 env、cwd。</span><span>远程与本地字段不可混用。</span></div></div>}
    {jsonError && <p className="error" role="alert">{jsonError}</p>}
    <div className="mcp-security-note"><b>凭据与运行环境</b><span>配置禁止包含 token、secret、password、Authorization 等敏感字段。CLI 和依赖来自节点绑定并已发布的终端环境，不会根据配置自动安装。</span></div>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !json.trim() || Boolean(jsonError)} onClick={onSave}>{busy ? '保存中…' : editing ? '发布新版本' : '校验并保存'}</button></footer>
  </section></div>;
}

interface HookEditorDialogProps {
  name: string; description: string; event: HookEvent; matcher: string; mode: HookMode; file?: File; busy: boolean;
  onNameChange: (value: string) => void; onDescriptionChange: (value: string) => void; onEventChange: (value: HookEvent) => void;
  onMatcherChange: (value: string) => void; onModeChange: (value: HookMode) => void; onFileChange: (file?: File) => void;
  onClose: () => void; onSave: () => void;
}

function HookEditorDialog({ name, description, event, matcher, mode, file, busy, onNameChange, onDescriptionChange, onEventChange, onMatcherChange, onModeChange, onFileChange, onClose, onSave }: HookEditorDialogProps) {
  const selectedEvent = HOOK_EVENTS.find(item => item.value === event) ?? HOOK_EVENTS[0];
  const accept = mode === 'PROMPT' ? '.md,.markdown,.txt,text/markdown,text/plain' : '.sh,text/x-shellscript';
  const validFile = file && (mode === 'PROMPT' ? /\.(?:md|markdown|txt)$/i.test(file.name) : /\.sh$/i.test(file.name));
  return <div className="modal-backdrop"><section className="modal capability-source-editor hook-editor" role="dialog" aria-modal="true" aria-label="新建 Hook">
    <header><div><span className="eyebrow">NEW OPENHANDS HOOK</span><h2>新建 Hook</h2><p>选择一个 OpenHands 原生回调事件，再上传一种执行规则。平台会冻结文件、编译官方 <code>hook_config</code>，并在新建会话时注册。</p></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <div className="hook-form-grid">
      <label><span>名称 *</span><input aria-label="Hook 名称" value={name} maxLength={200} placeholder="例如 数据外发检查" onChange={item => onNameChange(item.target.value)}/></label>
      <label><span>说明</span><input aria-label="Hook 说明" value={description} maxLength={2000} placeholder="说明何时执行、检查什么" onChange={item => onDescriptionChange(item.target.value)}/></label>
      <section className="hook-event-picker full-row"><header><b>回调事件 *</b><small>选择 OpenHands 的正式生命周期事件。</small></header><div>{HOOK_EVENTS.map(item => <button type="button" key={item.value} className={event === item.value ? 'selected' : ''} aria-pressed={event === item.value} onClick={() => onEventChange(item.value)}><span><b>{item.label}</b><small>{item.native} · {item.description}</small></span></button>)}</div></section>
      {selectedEvent.toolScoped && <section className="hook-matcher-field full-row"><label><span>工具匹配规则 *</span><input aria-label="工具匹配规则" value={matcher} placeholder="例如 terminal、browser、mcp_.* 或 *" onChange={item => onMatcherChange(item.target.value)}/><small>遵循 OpenHands Matcher：精确名称、<code>*</code>、正则或 <code>/正则/</code>。规则会对实际工具名作完整匹配。</small></label><div className="hook-matcher-examples"><span>常用示例</span>{[['所有工具', '*'], ['终端', 'terminal'], ['浏览器', 'browser'], ['全部 MCP', 'mcp_.*'], ['文件工具', 'file_.*']].map(([label, value]) => <button type="button" key={value} onClick={() => onMatcherChange(value)}>{label}<code>{value}</code></button>)}</div></section>}
      <section className="hook-mode-picker full-row"><header><b>执行方式 *</b><small>提示词与脚本二选一，均从上传文件读取。</small></header><div><button type="button" className={mode === 'PROMPT' ? 'selected' : ''} aria-pressed={mode === 'PROMPT'} onClick={() => onModeChange('PROMPT')}><b>提示词 Hook</b><small>上传 Markdown 或文本，由当前会话模型按 OpenHands Prompt Hook 执行。</small></button><button type="button" className={mode === 'SCRIPT' ? 'selected' : ''} aria-pressed={mode === 'SCRIPT'} onClick={() => onModeChange('SCRIPT')}><b>脚本 Hook</b><small>上传一个 Shell 脚本，平台物化为只读文件并注册为 OpenHands Command Hook。</small></button></div></section>
      <label className="context-file-upload hook-file-upload full-row"><input aria-label={mode === 'PROMPT' ? '提示词文件' : '脚本文件'} type="file" accept={accept} onChange={item => onFileChange(item.target.files?.[0])}/><span><b>{file ? file.name : mode === 'PROMPT' ? '选择提示词文件' : '选择 Shell 脚本'}</b><small>{file ? `${formatBytes(file.size)} · ${validFile ? '文件类型有效' : '文件类型不符合当前执行方式'}` : mode === 'PROMPT' ? '支持 UTF-8 .md、.markdown、.txt，最大 1 MiB。' : '仅支持 UTF-8 .sh，最大 1 MiB。'}</small></span><em>选择文件</em></label>
      <div className="hook-native-note full-row"><b>运行语义</b><span>{selectedEvent.toolScoped ? `仅匹配“${matcher.trim() || '*'}”的工具。` : '该事件不匹配工具，平台固定使用 *。'} {mode === 'PROMPT' ? '提示词由 OpenHands 使用当前会话模型判定。' : '脚本 stdin 接收 OpenHands JSON 事件；exit 2 可按上游语义阻断支持阻断的事件。'}</span></div>
    </div>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !name.trim() || !file || !validFile} onClick={onSave}>{busy ? '发布中…' : '发布 Hook'}</button></footer>
  </section></div>;
}

interface ContextDialogProps {
  title: string;
  description: string;
  content: string;
  file?: File;
  manifest?: ContextBundleDraft;
  busy: boolean;
  onTitleChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onContentChange: (value: string) => void;
  onFileChange: (file?: File) => void;
  onManifestChange: (manifest: ContextBundleDraft) => void;
  onClose: () => void;
  onSave: () => void;
}

function ContextDialog({ title, description, content, file, manifest, busy, onTitleChange, onDescriptionChange, onContentChange, onFileChange, onManifestChange, onClose, onSave }: ContextDialogProps) {
  const [entrypointMenuOpen, setEntrypointMenuOpen] = useState(false);
  const [draggingFile, setDraggingFile] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const selectFile = (nextFile?: File) => {
    if (!nextFile) return;
    onFileChange(nextFile);
  };
  const moveDocument = (index: number, direction: -1 | 1) => {
    if (!manifest || index + direction < 0 || index + direction >= manifest.documents.length) return;
    const documents = [...manifest.documents];
    [documents[index], documents[index + direction]] = [documents[index + direction], documents[index]];
    onManifestChange({ ...manifest, documents });
  };
  const selectEntrypoint = (entrypoint: string | null) => {
    if (!manifest) return;
    onManifestChange({ ...manifest, entrypoint });
    setEntrypointMenuOpen(false);
  };
  const selectedEntrypoint = manifest?.entrypoint ?? '';
  const hasSource = Boolean(file || content.trim());
  return <div className="modal-backdrop"><section className="modal context-dialog" role="dialog" aria-modal="true" aria-label="新增 Context">
    <header><div><span className="eyebrow">NEW CONTEXT</span><h2>新增 Context</h2></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <p>{manifest ? '资料包已安全解析。可调整阅读目录；全部文档仍会共同冻结并加载到同一个 Context。' : '填写标题和说明，再直接编写、粘贴文本，或上传资料文件。发布后内容、标题和说明都会被冻结；更新请新建 Context 并显式重新绑定。'}</p>
    <label><span>标题 *</span><input aria-label="Context 标题" value={title} maxLength={200} placeholder="例如：产品发布手册" onChange={event => onTitleChange(event.target.value)}/></label>
    <label><span>说明</span><textarea aria-label="Context 说明" value={description} maxLength={2000} placeholder="说明这份背景信息适用于哪些任务" onChange={event => onDescriptionChange(event.target.value)}/></label>
    <section className={`context-source-input${draggingFile ? ' dragging' : ''}`} onDragEnter={event => { event.preventDefault(); setDraggingFile(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (event.currentTarget === event.target) setDraggingFile(false); }} onDrop={event => { event.preventDefault(); setDraggingFile(false); selectFile(event.dataTransfer.files?.[0]); }}>
      <header><b>Context 内容 *</b><span>直接输入或粘贴文本，也可拖入文件或 ZIP 资料包。</span></header>
      <textarea aria-label="Context 内容" value={content} disabled={Boolean(file)} maxLength={MCP_JSON_MAX_BYTES} placeholder="点击此处直接编写或粘贴 Context 内容…" onChange={event => onContentChange(event.target.value)}/>
      <footer><span>{file ? `${file.name} · ${formatBytes(file.size)}${manifest ? ' · 已解析资料包' : ''}` : content.trim() ? `${formatBytes(new Blob([content]).size)} · 将作为 context.txt 发布` : '支持 UTF-8 .txt、.md、.markdown 或只含这些文档的 ZIP，最大 1 MiB。'}</span>{file ? <button type="button" className="ghost" onClick={() => { onFileChange(undefined); if (fileInputRef.current) fileInputRef.current.value = ''; }}>移除文件</button> : <button type="button" className="secondary" onClick={() => fileInputRef.current?.click()}>上传文件</button>}</footer>
      <input ref={fileInputRef} aria-label="Context 文件" type="file" accept=".txt,.md,.markdown,.zip,text/plain,text/markdown,application/zip" onChange={event => { selectFile(event.target.files?.[0]); event.target.value = ''; }}/>
    </section>
    {manifest && <section className="context-bundle-editor"><header><div><b>资料包目录</b><small>文档顺序决定冲突覆盖顺序；后面的文档覆盖前面的文档。</small></div><span>{manifest.documents.length} 份文档</span></header><div className="context-entrypoint-menu"><span id="context-entrypoint-label">阅读入口</span><button type="button" className="context-entrypoint-trigger" aria-label="资料包入口" aria-describedby="context-entrypoint-label" aria-haspopup="listbox" aria-expanded={entrypointMenuOpen} onClick={() => setEntrypointMenuOpen(open => !open)}><span>{selectedEntrypoint || '不指定入口'}</span><ChevronDown size={14} aria-hidden="true"/></button>{entrypointMenuOpen && <div className="context-entrypoint-options" role="listbox" aria-label="资料包入口"><button type="button" role="option" aria-selected={!selectedEntrypoint} onClick={() => selectEntrypoint(null)}>不指定入口</button>{manifest.documents.map(document => <button type="button" role="option" key={document.path} aria-selected={selectedEntrypoint === document.path} onClick={() => selectEntrypoint(document.path)}>{document.path}</button>)}</div>}</div><ol>{manifest.documents.map((document, index) => <li key={document.path}><div><input aria-label={`资料标题 ${document.path}`} value={document.title} maxLength={200} onChange={event => onManifestChange({ ...manifest, documents: manifest.documents.map(item => item.path === document.path ? { ...item, title: event.target.value } : item) })}/><small>{document.path}</small></div><span><button type="button" className="ghost" aria-label={`上移 ${document.path}`} disabled={index === 0} onClick={() => moveDocument(index, -1)}>↑</button><button type="button" className="ghost" aria-label={`下移 ${document.path}`} disabled={index === manifest.documents.length - 1} onClick={() => moveDocument(index, 1)}>↓</button></span></li>)}</ol></section>}
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !title.trim() || !hasSource} onClick={onSave}>{busy ? '处理中…' : manifest ? '确认并发布' : file?.name.toLowerCase().endsWith('.zip') ? '解析资料包' : '校验并发布'}</button></footer>
  </section></div>;
}

function AgentDefinitionDetailDialog({ item, onClose }: { item: CapabilityAsset; onClose: () => void }) {
  useEscapeClose(onClose);
  const definition = item.document as Record<string, unknown>;
  const list = (key: string) => Array.isArray(definition[key]) ? definition[key].filter((value): value is string => typeof value === 'string') : [];
  const description = item.description.replace(/<example>[\s\S]*?<\/example>/giu, '').replace(/\s+/gu, ' ').trim();
  return <div className="modal-backdrop"><section className="modal context-preview-dialog" role="dialog" aria-modal="true" aria-label={`查看 Agent Definition ${item.capability_key}`}><header><div><span className="eyebrow">OPENHANDS AGENT DEFINITION</span><h2>{item.capability_key}</h2></div><button className="ghost" onClick={onClose}>关闭</button></header><p>{description || '暂无说明'} · {item.filename}</p><dl className="agent-definition-detail"><dt>模型</dt><dd>{String(definition.model ?? 'inherit')}</dd><dt>权限模式</dt><dd>{String(definition.permission_mode ?? 'never_confirm')}</dd><dt>允许工具</dt><dd>{list('tools').join('、') || '无'}</dd><dt>适用示例</dt><dd>{list('when_to_use_examples').join('；') || '未声明'}</dd><dt>最大轮次</dt><dd>{String(definition.max_iteration_per_run ?? '默认')}</dd><dt>最大预算</dt><dd>{String(definition.max_budget_per_run ?? '未设置')}</dd></dl><section className="context-bundle-preview-directory"><b>系统提示词</b><pre>{String(definition.system_prompt ?? '')}</pre></section><footer><button className="primary" onClick={onClose}>完成</button></footer></section></div>;
}


interface CardProps { group: CapabilityLineage; selected: boolean; onToggle: () => void; onEdit: () => void; onCompare: () => void; onRollback: () => void; onViewContext: () => void; onViewAgentDefinition: () => void; onProfileHistory: () => void; onDelete: () => void }

function CapabilityOverflowMenu({ label, children }: { label: string; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer);
    };
  }, [open]);

  useEscapeClose(() => setOpen(false), open);
  const close = () => setOpen(false);
  const toggle = () => setOpen(current => !current);
  return <div className="capability-overflow-menu" ref={triggerRef}>
    <button type="button" className="capability-overflow-trigger" aria-label={`更多操作：${label}`} title="更多操作" aria-haspopup="menu" aria-expanded={open} onClick={toggle}><Ellipsis size={18}/></button>
    {open && <ViewportPopover anchor={() => triggerRef.current} align="end" gap={6} ref={menuRef} className="capability-overflow-popover" role="menu" aria-label={`${label} 操作菜单`} >{children(close)}</ViewportPopover>}
  </div>;
}

function CapabilityCard({ group, selected, onToggle, onEdit, onCompare, onRollback, onViewContext, onViewAgentDefinition, onProfileHistory, onDelete }: CardProps) {
  const item = group.latest;
  const description = item.capability_type === 'AGENT_DEFINITION'
    ? item.description.replace(/<example>[\s\S]*?<\/example>/giu, '').replace(/\s+/gu, ' ').trim()
    : item.description;
  const dependencyLabel = item.dependency_build_state === 'READY' ? '依赖可用' : item.dependency_build_state === 'PENDING' ? '依赖构建中' : item.dependency_build_state === 'FAILED' ? '依赖构建失败' : '无需额外依赖';
  const totalReferences = group.versions.reduce((total, version) => total + version.reference_count, 0);
  const open = item.capability_type === 'CONTEXT' ? onViewContext : item.capability_type === 'AGENT_DEFINITION' ? onViewAgentDefinition : item.capability_type === 'SKILL' ? onEdit : undefined;
  const openLabel = item.capability_type === 'CONTEXT' ? `查看 Context ${item.capability_key}` : item.capability_type === 'AGENT_DEFINITION' ? `查看 Agent Definition ${item.capability_key}` : item.capability_type === 'SKILL' ? `编辑 Skill ${item.capability_key}` : undefined;
  const versioned = item.capability_type === 'SKILL' || item.capability_type === 'CONTEXT';
  const hasPreviousSkillVersion = versioned && group.versions.length > 1;
  return <tr className={`capability-card ${selected ? 'selected' : ''}`} onClick={event => {
    // Keep controls (including portaled menu actions) independent from row selection.
    if (item.is_builtin || event.detail > 1 || (event.target instanceof Element && event.target.closest('button, a, input, label, [role="menu"]'))) return;
    onToggle();
  }}><td className="capability-check-column"><input type="checkbox" aria-label={`选择能力 ${item.capability_key}`} checked={selected} disabled={item.is_builtin} onChange={onToggle}/></td><td className="capability-name-cell"><b title={item.capability_key}>{open ? <button className="capability-name-open" aria-label={openLabel} onClick={open}>{item.capability_key}</button> : item.capability_key}{item.is_builtin && <LockKeyhole size={12}/>}</b><small title={description}>{description || '暂无说明'}</small></td><td className="capability-file-cell" title={item.filename}>{item.filename}</td><td>{formatBytes(item.byte_size)}</td><td><span>{versioned ? '当前版本' : `rev ${item.revision_number}${item.version ? ` · ${item.version}` : ''}`}</span>{versioned && <small>{hasPreviousSkillVersion ? '可回退到上一版本' : '暂无上一版本'}</small>}</td><td>{new Date(item.created_at).toLocaleString()}</td><td>{totalReferences}</td><td><span className={`capability-dependency ${item.dependency_build_state.toLowerCase()}`} title={item.dependency_build_error || ''}>{dependencyLabel}</span></td><td><CapabilityOverflowMenu label={`${typeLabel(item.capability_type)} ${item.capability_key}`}>{close => <>{(item.capability_type === 'SKILL' || item.capability_type === 'MCP') && <button type="button" role="menuitem" onClick={() => { onEdit(); close(); }}><Pencil size={14}/>编辑</button>}{versioned && <><a role="menuitem" href={capabilityPackageUrl(item.id)} download aria-label={`下载 ${typeLabel(item.capability_type)} ${item.capability_key}`} title="下载当前版本完整包" onClick={close}><Download size={14}/>下载</a><button type="button" role="menuitem" disabled={!hasPreviousSkillVersion} title={hasPreviousSkillVersion ? '对比上一版本和当前版本' : '暂无上一版本可对比'} onClick={() => { onCompare(); close(); }}><GitCompareArrows size={14}/>版本对比</button><button type="button" role="menuitem" disabled={!hasPreviousSkillVersion} title={hasPreviousSkillVersion ? '删除当前版本并恢复上一版本' : '暂无上一版本可回退'} onClick={() => { onRollback(); close(); }}><RotateCcw size={14}/>回退到上一版本</button></>}{item.capability_type === 'AGENT_PROFILE' && <button type="button" role="menuitem" onClick={() => { onProfileHistory(); close(); }}><Layers3 size={14}/>版本与绑定</button>}{item.is_builtin ? <button type="button" role="menuitem" disabled title="系统内置默认策略，不能删除"><LockKeyhole size={14}/>不可删除</button> : <button type="button" className="danger" role="menuitem" title={totalReferences > 0 ? '有关联的记录会保留并说明绑定节点，其余记录直接删除' : '删除能力'} onClick={() => { onDelete(); close(); }}><Trash2 size={14}/>删除能力</button>}</>}</CapabilityOverflowMenu></td></tr>;
}
