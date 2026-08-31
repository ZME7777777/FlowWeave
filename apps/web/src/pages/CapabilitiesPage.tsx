import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Braces, CheckSquare, Eye, FileArchive, Layers3, LockKeyhole, Pencil, PlugZap, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import { HookEditorDialog, type HookScriptAsset } from '../components/HookEditorDialog';
import { CapabilityCollectionEditorDialog } from '../components/CapabilityCollectionEditorDialog';
import { Pagination } from '../components/Pagination';
import { MarketplaceCatalogDialog } from '../components/MarketplaceCatalogDialog';
import { AgentProfileHistoryDialog } from '../components/AgentProfileHistoryDialog';
import { useProductDialog } from '../components/ProductDialogContext';
import { useEscapeClose } from '../components/useEscapeClose';
import type { BlockedCapabilityDelete, CapabilityAsset, CapabilityAssetType, CapabilityCollection, CapabilityCollectionWrite, PluginSourceResolution } from '../types';

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
const AGENT_DEFINITION_JSON_EXAMPLE = JSON.stringify({
  name: 'change-reviewer',
  description: '审查变更并报告可验证的问题',
  model: 'inherit',
  tools: ['terminal', 'grep'],
  system_prompt: '审查收到的变更，给出具体证据、风险和建议。',
  when_to_use_examples: ['审查一个补丁或实现方案'],
  permission_mode: 'confirm_risky',
  max_iteration_per_run: 20,
  max_budget_per_run: 1.5,
  condenser: { kind: 'NoOpCondenser' },
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

const CAPABILITY_MODULES: CapabilityAssetType[] = ['SKILL', 'PLUGIN', 'MCP', 'HOOK', 'AGENT_DEFINITION', 'CONTEXT'];

type McpEditorMode = 'FORM' | 'JSON';
type McpTransport = 'http' | 'streamable-http' | 'sse' | 'stdio';
type McpConnectionKind = 'REMOTE' | 'LOCAL';
type McpServerConfig = Record<string, unknown>;
interface McpDocument { mcpServers: Record<string, McpServerConfig> }
interface McpScriptAsset { server: string; filename: string; contentBase64: string; byteSize: number }

function capabilityPageSize() {
  const columns = window.innerWidth >= 1320 ? 4 : window.innerWidth >= 1100 ? 3 : 2;
  const rows = window.innerHeight >= 900 ? 5 : window.innerHeight >= 760 ? 4 : 3;
  return columns * rows;
}

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
  if (type === 'HOOK') return 'Hook';
  if (type === 'AGENT_DEFINITION') return 'Agent Definition';
  if (type === 'CONTEXT') return 'Context';
  return type;
}
function capabilityModuleDescription(type: CapabilityAssetType): string {
  if (type === 'SKILL') return '可复用指令与工作方法；选择后按具体版本冻结。';
  if (type === 'PLUGIN') return '以固定来源发布的 OpenHands 原生扩展包。';
  if (type === 'MCP') return '受治理的 MCP Server 配置，运行前会检测连接状态。';
  if (type === 'HOOK') return '按 OpenHands 生命周期执行的受控 Hook 配置。';
  if (type === 'AGENT_DEFINITION') return '可委派的原生 Agent 定义与运行预算。';
  if (type === 'CONTEXT') return '可上传并冻结到节点 OpenHands 系统上下文的文本。';
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
  const { data: capabilities = [], isLoading } = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const { data: capabilityCollections = [], isLoading: collectionsLoading } = useQuery({ queryKey: ['capability-collections'], queryFn: api.capabilityCollections });
  const [type, setType] = useState<CapabilityAssetType>('SKILL');
  const [editing, setEditing] = useState<CapabilityAsset>();
  useEscapeClose(() => setEditing(undefined), Boolean(editing));
  const [contextOpen, setContextOpen] = useState(false);
  const [contextTitle, setContextTitle] = useState('');
  const [contextDescription, setContextDescription] = useState('');
  const [contextFile, setContextFile] = useState<File>();
  const [viewingContext, setViewingContext] = useState<{ item: CapabilityAsset; content: string }>();
  useEscapeClose(() => setContextOpen(false), contextOpen);
  useEscapeClose(() => setViewingContext(undefined), Boolean(viewingContext));
  const [editingCollection, setEditingCollection] = useState<CapabilityCollection | null | undefined>();
  const [mcpOpen, setMcpOpen] = useState(false);
  const [editingMcp, setEditingMcp] = useState<CapabilityAsset>();
  useEscapeClose(() => setMcpOpen(false), mcpOpen);
  const [hookOpen, setHookOpen] = useState(false);
  useEscapeClose(() => setHookOpen(false), hookOpen);
  const [agentDefinitionOpen, setAgentDefinitionOpen] = useState(false);
  useEscapeClose(() => setAgentDefinitionOpen(false), agentDefinitionOpen);
  const [gitPluginOpen, setGitPluginOpen] = useState(false);
  const [marketplaceOpen, setMarketplaceOpen] = useState(false);
  const [profileHistory, setProfileHistory] = useState<CapabilityLineage>();
  const [gitSourceUrl, setGitSourceUrl] = useState('https://github.com/');
  const [gitCommit, setGitCommit] = useState('');
  const [gitRepoPath, setGitRepoPath] = useState('');
  const [gitResolution, setGitResolution] = useState<PluginSourceResolution>();
  const [gitBusy, setGitBusy] = useState(false);
  const gitPollGeneration = useRef(0);
  const closeGitPlugin = () => {
    gitPollGeneration.current += 1;
    setGitBusy(false);
    setGitPluginOpen(false);
  };
  useEscapeClose(closeGitPlugin, gitPluginOpen);
  const [source, setSource] = useState('');
  const [mcpMode, setMcpMode] = useState<McpEditorMode>('FORM');
  const [mcpJson, setMcpJson] = useState(MCP_JSON_EXAMPLE);
  const [mcpSelectedServer, setMcpSelectedServer] = useState('remote');
  const [mcpJsonError, setMcpJsonError] = useState('');
  const [mcpScripts, setMcpScripts] = useState<McpScriptAsset[]>([]);
  const [hookJson, setHookJson] = useState(HOOK_JSON_EXAMPLE);
  const [hookScripts, setHookScripts] = useState<HookScriptAsset[]>([]);
  const [agentDefinitionJson, setAgentDefinitionJson] = useState(AGENT_DEFINITION_JSON_EXAMPLE);
  const [busy, setBusy] = useState(false);
  const [importingSkill, setImportingSkill] = useState(false);
  const [selectedLineageIds, setSelectedLineageIds] = useState<Set<string>>(new Set());
  const [repositoryQuery, setRepositoryQuery] = useState('');
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const lineages = useMemo(() => groupCapabilities(capabilities), [capabilities]);
  const visible = useMemo(() => {
    const needle = repositoryQuery.trim().toLocaleLowerCase();
    return lineages.filter(group => group.latest.capability_type === type
      && (!needle || `${group.latest.capability_key} ${group.latest.description} ${group.latest.filename}`.toLocaleLowerCase().includes(needle)));
  }, [lineages, repositoryQuery, type]);
  const selectableVisible = useMemo(() => visible.filter(group => !group.latest.is_builtin), [visible]);
  const selectedVisible = useMemo(() => selectableVisible.filter(group => selectedLineageIds.has(group.id)), [selectableVisible, selectedLineageIds]);
  const allVisibleSelected = selectableVisible.length > 0 && selectableVisible.every(group => selectedLineageIds.has(group.id));
  const [pageSize, setPageSize] = useState(capabilityPageSize);
  const pagedVisible = visible.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => setPage(1), [type, repositoryQuery]);
  useEffect(() => { if (page > Math.max(1, Math.ceil(visible.length / pageSize))) setPage(1); }, [page, pageSize, visible.length]);
  useEffect(() => {
    const updatePageSize = () => setPageSize(capabilityPageSize());
    window.addEventListener('resize', updatePageSize);
    return () => window.removeEventListener('resize', updatePageSize);
  }, []);
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
  const importFileCapability = async (file: File, capabilityType: 'SKILL' | 'PLUGIN' | 'CONTEXT') => {
    setImportingSkill(true); setError(''); setNotice('');
    try {
      if (file.size > (capabilityType === 'CONTEXT' ? MCP_JSON_MAX_BYTES : SKILL_ZIP_MAX_BYTES)) throw new Error(capabilityType === 'CONTEXT' ? 'Context 文本不能超过 1 MiB。' : `${capabilityType === 'SKILL' ? 'Skill' : 'Plugin'} ZIP 不能超过 25 MiB。`);
      const validated = await api.validateCapability({ capability_type: capabilityType, filename: file.name, content_base64: toBase64(await file.arrayBuffer()) });
      const preview = validated.preview;
      const capabilityCount = preview.capabilities?.length ?? 0;
      const label = capabilityType === 'SKILL' ? 'Skill' : capabilityType === 'PLUGIN' ? 'Plugin' : 'Context';
      const message = capabilityType === 'CONTEXT' ? `将 ${file.name} 冻结为 1 个 Context 版本，并在节点选择时作为 OpenHands 系统上下文。` : `识别到 ${capabilityCount} 个 ${label}、${preview.file_count ?? 0} 个有效文件。${preview.ignored_entry_count ? `已忽略 ${preview.ignored_entry_count} 个 macOS 元数据条目。` : ''}`;
      const confirmed = await dialog.confirm({ title: `确认导入 ${file.name}`, message, confirmLabel: `导入 ${capabilityCount} 项能力` });
      if (!confirmed) return;
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); setNotice(`已从 ${file.name} 导入 ${committed.capabilities.length} 项 ${label} 能力。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImportingSkill(false); }
  };
  const closeContextForm = () => {
    setContextOpen(false); setContextTitle(''); setContextDescription(''); setContextFile(undefined);
  };
  const createContext = async () => {
    setImportingSkill(true); setError(''); setNotice('');
    try {
      const title = contextTitle.trim();
      if (!title) throw new Error('请填写 Context 标题。');
      if (!contextFile) throw new Error('请选择一个 Context 文件。');
      if (contextFile.size > MCP_JSON_MAX_BYTES) throw new Error('Context 文本不能超过 1 MiB。');
      const validated = await api.validateCapability({
        capability_type: 'CONTEXT', filename: contextFile.name,
        content_base64: toBase64(await contextFile.arrayBuffer()),
        context_title: title, context_description: contextDescription.trim(),
      });
      const committed = await api.commitCapability(validated.import_token);
      await refresh(); closeContextForm();
      setNotice(`已发布 Context“${committed.capabilities[0]?.capability_key ?? title}”。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setImportingSkill(false); }
  };
  const openContextSource = async (item: CapabilityAsset) => {
    setBusy(true); setError('');
    try {
      const source = await api.contextSource(item.id);
      setViewingContext({ item, content: source.content });
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
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
  const createAgentDefinition = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      const bytes = new TextEncoder().encode(agentDefinitionJson);
      if (bytes.byteLength > MCP_JSON_MAX_BYTES) throw new Error('Agent Definition JSON 不能超过 1 MiB。');
      const validated = await api.validateCapability({
        capability_type: 'AGENT_DEFINITION',
        filename: 'agent-definition.json',
        content_base64: toBase64(bytes.buffer),
      });
      const capabilityCount = validated.preview.capabilities?.length ?? 0;
      if (capabilityCount !== 1) throw new Error('Agent Definition 必须发布且只能发布一个定义版本。');
      const committed = await api.commitCapability(validated.import_token);
      setAgentDefinitionOpen(false); setAgentDefinitionJson(AGENT_DEFINITION_JSON_EXAMPLE);
      await refresh(); setNotice(`已发布 Agent Definition“${committed.capabilities[0]?.capability_key ?? ''}”的不可变版本。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  };
  const pollGitPlugin = async (initial: PluginSourceResolution, generation: number) => {
    let current = initial;
    const deadline = Date.now() + 5 * 60_000;
    while (current.state === 'PENDING' && Date.now() < deadline && gitPollGeneration.current === generation) {
      await new Promise(resolve => window.setTimeout(resolve, 1500));
      if (gitPollGeneration.current !== generation) return;
      current = await api.pluginSourceResolution(current.id);
      setGitResolution(current);
    }
    if (gitPollGeneration.current !== generation) return;
    if (current.state === 'PENDING') setError('远端 Plugin 仍在解析。可稍后关闭并用相同来源重新打开状态。');
    else if (current.state === 'FAILED') setError(current.error_detail || '远端 Plugin 解析失败，可按相同来源重试。');
    else if (current.state === 'EXPIRED') setError('远端 Plugin 解析已过期，可按相同来源重新提交。');
  };
  const resolveGitPlugin = async () => {
    setGitBusy(true); setError(''); setNotice('');
    const sourceUrl = gitSourceUrl.trim();
    const commit = gitCommit.trim().toLowerCase();
    const repoPath = gitRepoPath.trim();
    try {
      if (!/^https:\/\/[^\s]+$/u.test(sourceUrl)) throw new Error('来源必须是允许域名上的 HTTPS Git URL。');
      if (!/^[0-9a-f]{40}$/u.test(commit)) throw new Error('Commit 必须是完整的 40 位十六进制 SHA。');
      if (repoPath && !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/u.test(repoPath)) throw new Error('仓库子路径格式无效。');
      const generation = ++gitPollGeneration.current;
      const resolution = await api.createPluginSourceResolution({ source_url: sourceUrl, commit, repo_path: repoPath || null });
      setGitResolution(resolution);
      await pollGitPlugin(resolution, generation);
    } catch (reason) { setError(errorMessage(reason)); } finally { setGitBusy(false); }
  };
  const publishGitPlugin = async () => {
    if (!gitResolution || gitResolution.state !== 'READY') return;
    setGitBusy(true); setError(''); setNotice('');
    try {
      const published = await api.publishPluginSourceResolution(gitResolution.id, gitResolution.state_version);
      setGitResolution(published);
      await refresh();
      setNotice(`已发布固定 commit ${published.requested_commit.slice(0, 12)} 的不可变 Plugin 版本。`);
    } catch (reason) { setError(errorMessage(reason)); } finally { setGitBusy(false); }
  };
  const toggleLineage = (lineageId: string) => setSelectedLineageIds(old => {
    const next = new Set(old);
    if (next.has(lineageId)) next.delete(lineageId); else next.add(lineageId);
    return next;
  });
  const toggleVisible = () => setSelectedLineageIds(old => {
    const next = new Set(old);
    if (allVisibleSelected) selectableVisible.forEach(group => next.delete(group.id));
    else selectableVisible.forEach(group => next.add(group.id));
    return next;
  });
  const remove = async (groups: CapabilityLineage[], retainBlockedSelection = false) => {
    if (!groups.length || !await dialog.confirm({ title: `删除所选的 ${groups.length} 项能力？`, message: '将删除所选能力的全部历史版本；有关联或系统内置的版本会保留并说明原因，其余版本会直接删除。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setBusy(true); setError(''); setNotice('');
    try {
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
  const count = (capabilityType: CapabilityAssetType) => lineages.filter(item => item.latest.capability_type === capabilityType).length;

  return <section className="page capabilities-page">
    <nav className="capability-module-nav" aria-label="能力模块">{CAPABILITY_MODULES.map(item => <button key={item} className={type === item ? 'active' : ''} aria-current={type === item ? 'page' : undefined} onClick={() => { setType(item); setSelectedLineageIds(new Set()); setRepositoryQuery(''); setError(''); setNotice(''); }}><span><b>{typeLabel(item)}</b><small>{capabilityModuleDescription(item)}</small></span><em>{count(item)}</em></button>)}</nav>
    {marketplaceOpen && <MarketplaceCatalogDialog onClose={() => setMarketplaceOpen(false)} onPublished={async () => { await refresh(); setNotice('Marketplace Plugin 已发布为不可变 Capability Version。'); }}/>}
    {gitPluginOpen && <div className="modal-backdrop"><section className="modal git-plugin-dialog" role="dialog" aria-modal="true" aria-label="从固定 Git commit 导入 Plugin">
      <header><div><span className="eyebrow">PINNED GIT PLUGIN</span><h2>解析远端 Plugin</h2></div><button className="ghost" onClick={closeGitPlugin}>关闭</button></header>
      <p>解析器在隔离容器中拉取固定 commit，并将通过校验的内容冻结为本地 ZIP。远端 URL 不会进入 Runtime。</p>
      <label>HTTPS Git URL<input value={gitSourceUrl} placeholder="https://github.com/org/repository.git" onChange={event => setGitSourceUrl(event.target.value)}/></label>
      <label>完整 commit SHA<input value={gitCommit} maxLength={40} spellCheck={false} placeholder="40 位十六进制 SHA" onChange={event => setGitCommit(event.target.value)}/></label>
      <label>仓库子路径（可选）<input value={gitRepoPath} placeholder="plugins/my-plugin" onChange={event => setGitRepoPath(event.target.value)}/></label>
      {gitResolution && <section className={`git-plugin-status ${gitResolution.state.toLowerCase()}`}><header><b>{gitResolution.state}</b><code>v{gitResolution.state_version}</code></header><small>{gitResolution.requested_commit}</small>{gitResolution.content_hash && <small>内容摘要：{gitResolution.content_hash}</small>}<div className="git-plugin-contributions">{Object.entries(gitResolution.preview.contributions ?? {}).map(([kind, values]) => <span key={kind}><b>{kind}</b><em>{values.length ? values.join('、') : '无'}</em></span>)}</div>{gitResolution.error_detail && <p>{gitResolution.error_detail}</p>}</section>}
      <div className="mcp-security-note"><b>发布边界</b><span>READY 仅表示解析完成；只有点击“发布不可变版本”后才会进入能力仓库。FAILED / EXPIRED 可使用相同来源重新解析。</span></div>
      <footer><button className="ghost" onClick={closeGitPlugin}>取消</button><button className="secondary" disabled={gitBusy} onClick={() => void resolveGitPlugin()}>{gitBusy && gitResolution?.state === 'PENDING' ? '解析中…' : gitResolution?.state === 'FAILED' || gitResolution?.state === 'EXPIRED' ? '重新解析' : '开始解析'}</button><button className="primary" disabled={gitBusy || gitResolution?.state !== 'READY'} onClick={() => void publishGitPlugin()}>{gitBusy ? '处理中…' : '发布不可变版本'}</button></footer>
    </section></div>}
    <div className="capability-module-scroll">{error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    {type === 'SKILL' && <section className="capability-collection-section skill-collection-section"><header><div><Layers3 size={19}/><span><b>Skill 组合</b><small>用于批量选择固定 Skill 版本。</small></span></div><div className="skill-collection-header-actions"><em>{capabilityCollections.length} 个组合</em><button className="secondary" disabled={importingSkill || busy} onClick={() => { setError(''); setEditingCollection(null); }}><Layers3 size={14}/>新建 Skill 组合</button></div></header>{collectionsLoading ? <div className="empty compact">加载 Skill 组合…</div> : capabilityCollections.length ? <div className="capability-collection-list">{capabilityCollections.map(collection => <article key={collection.id}><span>{collection.category || '未分类'}</span><b>{collection.name}</b><em>{collection.members.length} 项</em><small title={collection.description}>{collection.description || '暂无说明'}</small><footer><button className="secondary" onClick={() => setEditingCollection(collection)}><Pencil size={12}/>编辑</button><button className="ghost" onClick={() => void removeCollection(collection)}><Trash2 size={12}/>删除</button></footer></article>)}</div> : <div className="capability-collection-empty"><span>还没有 Skill 组合。选择固定 Skill 版本后，可在会话中一键展开。</span><button className="secondary" onClick={() => setEditingCollection(null)}>创建第一个 Skill 组合</button></div>}</section>}
    {!isLoading && <div className="capability-bulk-tools"><span>{repositoryQuery ? `找到 ${visible.length} 项匹配的 ${typeLabel(type)}` : `共 ${visible.length} 项 ${typeLabel(type)}`}{visible.some(group => group.latest.is_builtin) ? ` · ${visible.filter(group => group.latest.is_builtin).length} 项系统内置不可删除` : ''}</span><div className="capability-list-controls"><label className="capability-repository-search"><input aria-label="搜索当前能力模块" value={repositoryQuery} onChange={event => setRepositoryQuery(event.target.value)} placeholder={`搜索 ${typeLabel(type)} 名称、说明或文件…`}/>{repositoryQuery && <button type="button" aria-label="清除搜索" onClick={() => setRepositoryQuery('')}>×</button>}</label>{type === 'SKILL' && <label className="primary file-button"><Upload size={15}/>{importingSkill ? '上传中…' : '上传 Skill ZIP'}<input type="file" disabled={importingSkill || busy} accept=".zip" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFileCapability(file, 'SKILL'); }}/></label>}{type === 'PLUGIN' && <><label className="primary file-button"><Upload size={15}/>{importingSkill ? '上传中…' : '上传 Plugin ZIP'}<input type="file" disabled={importingSkill || busy} accept=".zip" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFileCapability(file, 'PLUGIN'); }}/></label><button className="secondary" disabled={busy || importingSkill} onClick={() => { setError(''); setNotice(''); setMarketplaceOpen(true); }}><PlugZap size={14}/>浏览 Marketplace</button><button className="secondary" disabled={busy || importingSkill} onClick={() => { setError(''); setNotice(''); setGitPluginOpen(true); }}><PlugZap size={14}/>Git Plugin</button></>}{type === 'CONTEXT' && <button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setNotice(''); setContextOpen(true); }}><Upload size={15}/>新增 Context</button>}{type === 'MCP' && <button className="primary" disabled={importingSkill || busy} onClick={() => void openMcpEditor()}><Braces size={15}/>新建 MCP</button>}{type === 'HOOK' && <button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setHookOpen(true); }}><ShieldCheck size={15}/>新建 Hook</button>}{type === 'AGENT_DEFINITION' && <button className="primary" disabled={importingSkill || busy} onClick={() => { setError(''); setAgentDefinitionOpen(true); }}><Braces size={15}/>新建 Agent Definition</button>}</div><div className="bulk-actions"><button className="secondary" disabled={!selectableVisible.length || busy} onClick={toggleVisible}><CheckSquare size={14}/>{allVisibleSelected ? '取消全选' : '全选当前模块'}</button><button className="danger" disabled={!selectedVisible.length || busy} onClick={() => void remove(selectedVisible, true)}><Trash2 size={14}/>{busy ? '删除中…' : `批量删除 (${selectedVisible.length})`}</button></div></div>}
    {isLoading ? <div className="empty">加载 {typeLabel(type)}…</div> : visible.length ? <><div className="capability-card-grid compact-capability-list">{pagedVisible.map(group => <CapabilityCard key={group.id} group={group} selected={selectedLineageIds.has(group.id)} onToggle={() => toggleLineage(group.id)} onEdit={() => group.latest.capability_type === 'MCP' ? void openMcpEditor(group.latest) : void openEditor(group.latest)} onViewContext={() => void openContextSource(group.latest)} onProfileHistory={() => setProfileHistory(group)} onDelete={() => void remove([group])}/>)}</div><Pagination page={page} pageSize={pageSize} total={visible.length} onPageChange={setPage}/></> : <div className="empty"><FileArchive size={30}/><b>{repositoryQuery ? `没有匹配的 ${typeLabel(type)}` : `暂无 ${typeLabel(type)}`}</b><span>{repositoryQuery ? '调整搜索条件，或清除搜索查看全部能力。' : '使用本模块右上角的功能创建或导入。'}</span></div>}</div>

    {profileHistory && <AgentProfileHistoryDialog packageId={profileHistory.id} capabilityKey={profileHistory.latest.capability_key} onClose={() => setProfileHistory(undefined)}/>}
    {contextOpen && <ContextDialog title={contextTitle} description={contextDescription} file={contextFile} busy={importingSkill} onTitleChange={setContextTitle} onDescriptionChange={setContextDescription} onFileChange={setContextFile} onClose={closeContextForm} onSave={() => void createContext()}/>}
    {viewingContext && <div className="modal-backdrop"><section className="modal context-preview-dialog" role="dialog" aria-modal="true" aria-label={`查看 Context ${viewingContext.item.capability_key}`}><header><div><span className="eyebrow">FROZEN CONTEXT</span><h2>{viewingContext.item.capability_key}</h2></div><button className="ghost" onClick={() => setViewingContext(undefined)}>关闭</button></header><p>{viewingContext.item.description || '暂无说明'} · {viewingContext.item.filename}</p><pre>{viewingContext.content}</pre><footer><button className="primary" onClick={() => setViewingContext(undefined)}>完成</button></footer></section></div>}
    {editing && <div className="modal-backdrop"><section className="modal capability-source-editor" role="dialog" aria-label={`编辑 Skill ${editing.capability_key}`}><header><div><span className="eyebrow">EDIT SKILL</span><h2>编辑 {editing.capability_key}</h2></div><button className="ghost" onClick={() => setEditing(undefined)}>关闭</button></header><p>保存会发布新的不可变 Skill 版本；已有节点和 Run Snapshot 继续引用原版本，升级必须显式重新绑定。</p><textarea aria-label="Skill 源码" value={source} onChange={event => setSource(event.target.value)}/><div className="dependency-policy"><b>声明依赖（写入 SKILL.md frontmatter）</b><code>{DEPENDENCY_EXAMPLE}</code><span>所有版本必须精确固定。CLI 必须在平台白名单中；不接受终端命令。</span></div><footer><button className="ghost" onClick={() => setEditing(undefined)}>取消</button><button className="primary" disabled={busy} onClick={() => void saveSource()}>{busy ? '保存中…' : '发布新版本'}</button></footer></section></div>}
    {editingCollection !== undefined && <CapabilityCollectionEditorDialog collection={editingCollection ?? undefined} capabilities={capabilities} busy={busy} onClose={() => setEditingCollection(undefined)} onSave={payload => void saveCollection(payload)}/>}
    {mcpOpen && <McpEditorDialog editing={Boolean(editingMcp)} mode={mcpMode} json={mcpJson} jsonError={mcpJsonError || parsedMcp.error || mcpContractError} selectedServer={activeMcpServerName} server={activeMcpServer} transport={activeMcpTransport} connectionKind={activeMcpConnectionKind} scripts={mcpScripts.filter(script => script.server === activeMcpServerName)} busy={busy} onModeChange={switchMcpMode} onJsonChange={value => { setMcpJson(value); setMcpJsonError(''); }} onConnectionKindChange={switchMcpConnectionKind} onRenameServer={renameMcpServer} onUpdateServer={updateMcpServer} onAddScripts={(server, files) => void addMcpScripts(server, files).catch(reason => setMcpJsonError(errorMessage(reason)))} onRemoveScript={removeMcpScript} onClose={() => { setMcpOpen(false); setEditingMcp(undefined); }} onSave={() => void createMcp()}/>}
    {hookOpen && <HookEditorDialog json={hookJson} scripts={hookScripts} busy={busy} onJsonChange={setHookJson} onScriptsChange={setHookScripts} onClose={() => setHookOpen(false)} onSave={() => void createHook()}/>}
        {agentDefinitionOpen && <div className="modal-backdrop"><section className="modal capability-source-editor agent-definition-editor" role="dialog" aria-modal="true" aria-label="新建 Agent Definition"><header><div><span className="eyebrow">NEW AGENT DEFINITION</span><h2>发布 Agent Definition</h2></div><button className="ghost" onClick={() => setAgentDefinitionOpen(false)}>关闭</button></header><p>定义将按 OpenHands 1.42.0 原生 AgentDefinition 严格子集发布。模型必须继承父 Agent；暂不允许嵌套 Skill、MCP、Hook、profile path 或任意 metadata。</p><textarea aria-label="Agent Definition JSON" value={agentDefinitionJson} spellCheck={false} onChange={event => setAgentDefinitionJson(event.target.value)}/><div className="mcp-security-note"><b>原生委派边界</b><span>所有会话均使用固定完整 Runtime Tool 集；定义内 Tool 只能引用该集合，且不得递归启用 task_tool_set。</span></div><footer><button className="ghost" onClick={() => setAgentDefinitionOpen(false)}>取消</button><button className="primary" disabled={busy || !agentDefinitionJson.trim()} onClick={() => void createAgentDefinition()}>{busy ? '校验中…' : '校验并发布'}</button></footer></section></div>}
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
  return <div className="modal-backdrop"><section className="modal capability-source-editor mcp-editor" role="dialog" aria-modal="true" aria-label={editing ? '编辑 MCP' : '新建 MCP'}>
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

interface ContextDialogProps {
  title: string;
  description: string;
  file?: File;
  busy: boolean;
  onTitleChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onFileChange: (file?: File) => void;
  onClose: () => void;
  onSave: () => void;
}

function ContextDialog({ title, description, file, busy, onTitleChange, onDescriptionChange, onFileChange, onClose, onSave }: ContextDialogProps) {
  return <div className="modal-backdrop"><section className="modal context-dialog" role="dialog" aria-modal="true" aria-label="新增 Context">
    <header><div><span className="eyebrow">NEW CONTEXT</span><h2>新增 Context</h2></div><button className="ghost" onClick={onClose}>关闭</button></header>
    <p>填写标题和说明后上传文本。发布后内容、标题和说明都会被冻结；更新请新建 Context 并显式重新绑定。</p>
    <label><span>标题 *</span><input aria-label="Context 标题" value={title} maxLength={200} placeholder="例如：产品发布手册" onChange={event => onTitleChange(event.target.value)}/></label>
    <label><span>说明</span><textarea aria-label="Context 说明" value={description} maxLength={2000} placeholder="说明这份背景信息适用于哪些任务" onChange={event => onDescriptionChange(event.target.value)}/></label>
    <label className="context-file-upload"><input aria-label="Context 文件" type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" onChange={event => onFileChange(event.target.files?.[0])}/><span><b>{file ? file.name : '选择 Context 文件'}</b><small>{file ? `${formatBytes(file.size)} · ${file.type || '文本文件'}` : '支持 .txt、.md、.markdown，UTF-8 编码，最大 1 MiB。'}</small></span><em>选择文件</em></label>
    <div className="mcp-security-note"><b>安全校验</b><span>只接受非空 UTF-8 文本；包含明文 API Key、Token、Secret、Password 或 Authorization 赋值的文件会被拒绝。</span></div>
    <footer><button className="ghost" onClick={onClose}>取消</button><button className="primary" disabled={busy || !title.trim() || !file} onClick={onSave}>{busy ? '发布中…' : '校验并发布'}</button></footer>
  </section></div>;
}

interface CardProps { group: CapabilityLineage; selected: boolean; onToggle: () => void; onEdit: () => void; onViewContext: () => void; onProfileHistory: () => void; onDelete: () => void }
function CapabilityCard({ group, selected, onToggle, onEdit, onViewContext, onProfileHistory, onDelete }: CardProps) {
  const item = group.latest;
  const dependencyLabel = item.dependency_build_state === 'READY' ? '依赖可用' : item.dependency_build_state === 'PENDING' ? '依赖构建中' : item.dependency_build_state === 'FAILED' ? '依赖构建失败' : '无需额外依赖';
  const totalReferences = group.versions.reduce((total, version) => total + version.reference_count, 0);
  return <article className={`capability-card ${item.is_builtin ? 'builtin' : ''} ${selected ? 'selected' : ''}`}><header><label className="capability-card-select resource-check" title={item.is_builtin ? '系统内置默认策略，不能选择删除' : `选择能力 ${item.capability_key}`}><input type="checkbox" aria-label={`选择能力 ${item.capability_key}`} checked={selected} disabled={item.is_builtin} onChange={onToggle}/><span className={`capability-card-icon ${item.capability_type.toLowerCase()}`}>{item.capability_type === 'SKILL' || item.capability_type === 'CONTEXT' ? <FileArchive size={18}/> : <PlugZap size={18}/>}</span></label>{item.is_builtin && <span className="builtin-badge"><LockKeyhole size={12}/>系统内置</span>}<span className="cap-type">{typeLabel(item.capability_type)}</span></header><h3>{item.capability_key}</h3><p>{item.description || '暂无能力说明'}</p><div className="capability-version"><span>rev {item.revision_number}</span><code title={item.id}>{item.id.slice(0, 8)}</code><code title={item.content_hash}>{item.content_hash.slice(0, 10)}</code></div><div className={`dependency-state ${item.dependency_build_state.toLowerCase()}`} title={item.dependency_build_error || ''}>{dependencyLabel}{item.dependency_build_error ? `：${item.dependency_build_error}` : ''}</div><dl><dt>来源文件</dt><dd>{item.filename}</dd><dt>文件大小</dt><dd>{formatBytes(item.byte_size)}</dd><dt>更新时间</dt><dd>{new Date(item.created_at).toLocaleString()}</dd><dt>节点引用</dt><dd>{totalReferences} 个</dd></dl><footer>{item.capability_type === 'CONTEXT' && <button className="secondary" onClick={onViewContext}><Eye size={13}/>查看内容</button>}{(item.capability_type === 'SKILL' || item.capability_type === 'MCP') && <button className="secondary" onClick={onEdit}><Pencil size={13}/>编辑</button>}{item.capability_type === 'AGENT_PROFILE' && <button className="secondary" onClick={onProfileHistory}><Layers3 size={13}/>版本与绑定</button>}{item.is_builtin ? <button className="ghost" disabled title="系统内置默认策略，不能删除"><LockKeyhole size={13}/>不可删除</button> : <button className="ghost" title={totalReferences > 0 ? '有关联的记录会保留并说明绑定节点，其余记录直接删除' : '删除能力'} onClick={onDelete}><Trash2 size={13}/>删除能力</button>}</footer></article>;
}
