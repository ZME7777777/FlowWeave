import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, Check, ChevronDown, ChevronRight, CircleDot, Download, FileCode2, FileText, Folder, FolderOpen, FolderPlus, GitBranch, LoaderCircle, Maximize2, Minimize2, MonitorCog, PanelRightOpen, Play, Plus, Send, ShieldAlert, Square, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { ApiError, agentWorkspaceFileUrl, agentWorkspaceTerminalUrl, api, randomId, subscribeToAgentWorkspaceStream } from '../api/client';
import { ConversationSurface } from '../components/ConversationSurface';
import type { AgentAttachment, AgentConversation, AgentPendingConfirmationAction, AgentWorkDirectory, AgentWorkDirectoryList, ModelProvider, OpenHandsConversationEvent, ProviderModel } from '../types';
import './agent-workbench.css';
import './agent-workbench-layout.css';

type StreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';
type TurnState = 'idle' | 'running' | 'pausing' | 'paused' | 'resuming';
interface QueuedMessage {
  id: string;
  scope: string;
  content: string;
  items: AgentAttachment[];
}
interface BoundQueuedMessage extends QueuedMessage { bindingId: string; }
interface ConversationDraft { id: string; workDirectoryId?: string; displayName: string; }

function ComposerModelMenu({
  providers, providerId, modelName, models, efforts, effort, disabled, onProviderChange, onModelChange, onEffortChange,
}: {
  providers: ModelProvider[]; providerId: string; modelName: string; models: ProviderModel[]; efforts: string[];
  effort: string; disabled: boolean; onProviderChange: (value: string) => void; onModelChange: (value: string) => void;
  onEffortChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, [open]);
  return <details ref={menu} className="agent-composer-model-menu" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary aria-label="打开模型与推理设置"><span className="agent-composer-model-summary"><span>{modelName || '选择模型'}</span>{effort && <em>{reasoningEffortLabel(effort)}</em>}</span><ChevronDown size={14}/></summary>
    <section className="agent-composer-model-popover">
      <label><span>供应商</span><select aria-label="会话供应商" value={providerId} disabled={disabled} onChange={event => onProviderChange(event.target.value)}><option value="" disabled>选择供应商</option>{providers.map(provider => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select><ChevronRight size={14}/></label>
      <label><span>模型</span><select aria-label="会话模型" value={modelName} disabled={disabled || !providerId} onChange={event => onModelChange(event.target.value)}>{modelName && !models.some(model => model.model_name === modelName) && <option value={modelName} disabled>{modelName}</option>}{models.map(model => <option key={model.model_name} value={model.model_name}>{model.model_name}</option>)}</select><ChevronRight size={14}/></label>
      {efforts.length > 0 && <label><span>推理强度</span><select aria-label="思考程度" value={effort} disabled={disabled || !providerId} onChange={event => onEffortChange(event.target.value)}>{[effort, ...efforts].filter((value, index, all): value is string => Boolean(value) && all.indexOf(value) === index).map(value => <option key={value} value={value}>{value}</option>)}</select><ChevronRight size={14}/></label>}
      <details className="agent-composer-model-advanced"><summary><span>高级</span><ChevronDown size={12}/></summary><p>供应商、模型和推理强度仅作用于当前会话。</p></details>
    </section>
  </details>;
}

function reasoningEffortLabel(value: string): string {
  return {
    low: '低',
    medium: '中',
    high: '高',
    xhigh: '很高',
    max: '最高',
    ultra: '极高',
  }[value] ?? value;
}

function compactTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value % 1_000 ? 1 : 0)}k`;
  return String(value);
}

function configuredModelName(provider: ModelProvider | undefined, runtimeModel: string | null | undefined): string | undefined {
  if (!provider || !runtimeModel) return undefined;
  return provider.models.find(model => model.enabled && (
    model.model_name === runtimeModel || `openai/${model.model_name}` === runtimeModel
  ))?.model_name;
}

function isImeComposition(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}

function bindingIdFromLocation(): string | undefined {
  const match = window.location.pathname.match(/^\/agent\/conversations\/([^/]+)$/);
  return match ? decodeURIComponent(match[1]) : undefined;
}

function conversationName(conversation: AgentConversation) {
  return conversation.display_title || '新会话';
}

function mergeConversationEvents(
  durable: OpenHandsConversationEvent[],
  transient: OpenHandsConversationEvent[],
): OpenHandsConversationEvent[] {
  // REST remains the source of truth for an event already persisted.  Append
  // browser-only frames after that stable order so an optimistic current user
  // turn cannot jump in front of the existing transcript before its parent is
  // returned by OpenHands.
  const merged = new Map(durable.map(event => [event.id, event]));
  for (const event of transient) if (!merged.has(event.id)) merged.set(event.id, event);
  return [...merged.values()];
}

function hasFinishedTurn(events: OpenHandsConversationEvent[], userEventId: string): boolean {
  const byId = new Map(events.map(event => [event.id, event]));
  if (!byId.has(userEventId)) return false;
  const descendsFromActiveUser = (event: OpenHandsConversationEvent): boolean => {
    const visited = new Set<string>();
    let parentId = event.payload.parent_id;
    while (parentId && parentId !== '__root__' && !visited.has(parentId)) {
      if (parentId === userEventId) return true;
      visited.add(parentId);
      parentId = byId.get(parentId)?.payload.parent_id;
    }
    return false;
  };
  return events.some(event => {
    const isTerminal = event.event_type === 'ERROR'
      || (event.event_type === 'COMPLETED' && event.payload.event_name === 'FinishAction')
      || (event.event_type === 'MESSAGE' && !['user', 'human'].includes(String(event.payload.source ?? '').toLowerCase()));
    return isTerminal && descendsFromActiveUser(event);
  });
}

function eventBranchIds(events: OpenHandsConversationEvent[], rootEventId: string): Set<string> {
  const children = new Map<string, string[]>();
  for (const event of events) {
    const parentId = event.payload.parent_id;
    if (!parentId) continue;
    const siblings = children.get(parentId) ?? [];
    siblings.push(event.id);
    children.set(parentId, siblings);
  }
  const branch = new Set<string>();
  const visit = (eventId: string) => {
    if (branch.has(eventId)) return;
    branch.add(eventId);
    for (const childId of children.get(eventId) ?? []) visit(childId);
  };
  visit(rootEventId);
  return branch;
}

function WorkspaceTerminal({ workspaceId, terminalInstanceId, bindingId, workDirectoryId, workingDirectory }: { workspaceId: string; terminalInstanceId: string; bindingId?: string; workDirectoryId?: string; workingDirectory: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在连接工作区终端…');

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const terminal = new XTerm({ cursorBlink: true, scrollback: 3000, fontSize: 13, lineHeight: 1.25, fontFamily: "'DM Mono', ui-monospace, SFMono-Regular, Menlo, monospace", theme: { background: '#07110b', foreground: '#c8f7d8', cursor: '#75e99d', selectionBackground: '#315d42' } });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    let socket: WebSocket | null = null;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let resizeFrame: number | undefined;
    let remoteResizeTimer: number | undefined;
    let pendingDimensions: { cols: number; rows: number } | undefined;
    let lastSentDimensions: { cols: number; rows: number } | undefined;
    let attempts = 0;
    let connectionStarted = false;
    const reconnectDelays = [1000, 2000, 5000, 10_000, 30_000];
    const sendPendingResize = () => {
      remoteResizeTimer = undefined;
      const dimensions = pendingDimensions;
      if (!dimensions || socket?.readyState !== WebSocket.OPEN) return;
      if (lastSentDimensions?.cols === dimensions.cols && lastSentDimensions.rows === dimensions.rows) return;
      socket.send(JSON.stringify({ type: 'resize', rows: dimensions.rows, columns: dimensions.cols }));
      lastSentDimensions = dimensions;
    };
    const scheduleRemoteResize = (dimensions: { cols: number; rows: number }, delay = 80) => {
      pendingDimensions = dimensions;
      if (remoteResizeTimer !== undefined) window.clearTimeout(remoteResizeTimer);
      remoteResizeTimer = window.setTimeout(sendPendingResize, delay);
    };
    const resize = () => {
      if (resizeFrame !== undefined) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = undefined;
        if (disposed || element.clientWidth < 160 || element.clientHeight < 100) return;
        let dimensions: { cols: number; rows: number } | undefined;
        try { dimensions = fit.proposeDimensions(); } catch { return; }
        // The drawer can report a transient zero-width layout while its grid
        // transition is opening. Never create or resize the PTY from that size.
        if (!dimensions || dimensions.cols < 20 || dimensions.rows < 2) return;
        const wasAtBottom = terminal.buffer.active.viewportY >= terminal.buffer.active.baseY;
        if (terminal.cols !== dimensions.cols || terminal.rows !== dimensions.rows) {
          terminal.resize(dimensions.cols, dimensions.rows);
          terminal.refresh(0, terminal.rows - 1);
          if (wasAtBottom) terminal.scrollToBottom();
        }
        if (!connectionStarted) {
          connectionStarted = true;
          connect();
          return;
        }
        if (socket?.readyState === WebSocket.OPEN) scheduleRemoteResize(dimensions);
      });
    };
    const connect = () => {
      if (disposed) return;
      setState('connecting');
      setDetail(attempts ? '终端已断开，正在重新连接…' : '正在连接工作区终端…');
      const current = new WebSocket(agentWorkspaceTerminalUrl(workspaceId, terminal.rows, terminal.cols, { terminalInstanceId, bindingId, workDirectoryId }));
      socket = current;
      current.binaryType = 'arraybuffer';
      current.onopen = () => {
        attempts = 0;
        lastSentDimensions = undefined;
        setState('connected');
        setDetail(`已连接 ${workingDirectory}`);
        scheduleRemoteResize({ cols: terminal.cols, rows: terminal.rows }, 0);
        resize();
        terminal.focus();
      };
      current.onmessage = event => {
        const wasAtBottom = terminal.buffer.active.viewportY >= terminal.buffer.active.baseY;
        terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data), () => {
          if (wasAtBottom) terminal.scrollToBottom();
        });
      };
      current.onclose = event => {
        if (socket === current) socket = null;
        if (disposed || event.code === 1000) return;
        if (event.code === 4409 || attempts >= reconnectDelays.length) { setState('unavailable'); setDetail(event.reason || '终端暂时不可用'); return; }
        const delay = reconnectDelays[attempts];
        attempts += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };
    const input = terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    void document.fonts?.ready.then(resize);
    return () => { disposed = true; if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer); if (resizeFrame !== undefined) window.cancelAnimationFrame(resizeFrame); if (remoteResizeTimer !== undefined) window.clearTimeout(remoteResizeTimer); observer.disconnect(); input.dispose(); socket?.close(1000); terminal.dispose(); };
  }, [bindingId, terminalInstanceId, workDirectoryId, workingDirectory, workspaceId]);

  return <section className="agent-workspace-terminal"><header><span className={`terminal-dot ${state}`}/><span>{detail}</span></header><div ref={host} aria-label="Agent 工作区终端"/></section>;
}

function isTextPreviewable(path: string): boolean {
  return /(?:^|\/)(?:[^.]+|.*\.(?:md|mdx|txt|json|ya?ml|toml|ini|conf|xml|html?|css|scss|less|tsx?|jsx?|py|java|kt|go|rs|rb|php|sh|zsh|sql|graphql|vue|svelte))$/i.test(path);
}

function relativeWorkspacePath(path: string, root: string): string {
  return path === root ? '.' : path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
}

type WorkspaceEntry = { path: string; kind: 'file' | 'directory'; size: number };
type WorkspaceTreeNode = WorkspaceEntry & { name: string; children: WorkspaceTreeNode[] };

function workspaceTree(entries: WorkspaceEntry[], root: string): WorkspaceTreeNode[] {
  const nodes = new Map<string, WorkspaceTreeNode>();
  const ensureDirectory = (path: string): WorkspaceTreeNode => {
    const existing = nodes.get(path);
    if (existing) return existing;
    const node: WorkspaceTreeNode = { path, kind: 'directory', size: 0, name: path.split('/').pop() || path, children: [] };
    nodes.set(path, node);
    return node;
  };
  for (const entry of entries) {
    const relative = relativeWorkspacePath(entry.path, root);
    if (relative === '.' || relative.startsWith('../') || relative.startsWith('/')) continue;
    const parts = relative.split('/').filter(Boolean);
    let parentPath = root;
    for (let index = 0; index < parts.length; index += 1) {
      const path = `${parentPath}/${parts[index]}`;
      const isLeaf = index === parts.length - 1;
      const node = isLeaf && entry.kind === 'file'
        ? { ...entry, name: parts[index], children: [] }
        : ensureDirectory(path);
      nodes.set(path, node);
      const siblings = parentPath === root ? undefined : ensureDirectory(parentPath).children;
      if (siblings && !siblings.some(candidate => candidate.path === path)) siblings.push(node);
      parentPath = path;
    }
  }
  const roots = [...nodes.values()].filter(node => {
    const parent = node.path.slice(0, node.path.lastIndexOf('/'));
    return parent === root;
  });
  const sort = (items: WorkspaceTreeNode[]) => {
    items.sort((a, b) => a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'directory' ? -1 : 1);
    items.forEach(item => sort(item.children));
  };
  sort(roots);
  return roots;
}

function WorkspaceFileTree({ entries, root, selectedFile, onSelect }: { entries: WorkspaceEntry[]; root: string; selectedFile?: string; onSelect: (path: string) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const nodes = useMemo(() => workspaceTree(entries, root), [entries, root]);
  useEffect(() => {
    setExpanded(new Set(nodes.filter(node => node.kind === 'directory').map(node => node.path)));
  }, [nodes]);
  const renderNodes = (items: WorkspaceTreeNode[], depth = 0): ReactNode => items.map(node => {
    const open = expanded.has(node.path);
    return <div key={node.path} role="treeitem" aria-expanded={node.kind === 'directory' ? open : undefined}>
      <button type="button" className={`${node.kind}${selectedFile === node.path ? ' active' : ''}`} style={{ '--tree-depth': depth } as CSSProperties} onClick={() => {
        if (node.kind === 'file') onSelect(node.path);
        else setExpanded(current => { const next = new Set(current); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); return next; });
      }}>
        {node.kind === 'directory' ? open ? <ChevronDown size={13}/> : <ChevronRight size={13}/> : <span className="agent-tree-spacer"/>}
        {node.kind === 'directory' ? open ? <FolderOpen size={14}/> : <Folder size={14}/> : <FileCode2 size={14}/>}
        <span>{node.name}</span>
        {node.kind === 'file' && <em>{node.size ? `${Math.ceil(node.size / 1024)} KB` : '0 KB'}</em>}
      </button>
      {node.kind === 'directory' && open && <div role="group">{renderNodes(node.children, depth + 1)}</div>}
    </div>;
  });
  return <div className="agent-file-tree" role="tree" aria-label="工作区目录树">{nodes.length ? renderNodes(nodes) : <p>当前目录没有可展示的文件。</p>}</div>;
}

function WorkDirectoryCreator({ workspaceId, onClose, onCreated }: {
  workspaceId: string;
  onClose: () => void;
  onCreated: (directory: AgentWorkDirectory) => void;
}) {
  const [displayName, setDisplayName] = useState('');
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const detailsQuery = useQuery({
    queryKey: ['agent-workspace-create-directory', workspaceId],
    queryFn: () => api.agentWorkspaceDetails(workspaceId),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const create = useMutation({
    mutationFn: () => api.createAgentWorkDirectory(workspaceId, displayName.trim(), selectedPaths),
    onSuccess: directory => {
      onCreated(directory);
      setDisplayName('');
      setSelectedPaths([]);
      onClose();
    },
  });
  const details = detailsQuery.data;
  const directoryNodes = useMemo(
    () => details ? workspaceTree(details.files.filter(entry => entry.kind === 'directory'), details.root) : [],
    [details],
  );
  useEffect(() => {
    setExpanded(new Set(directoryNodes.map(node => node.path)));
  }, [directoryNodes]);
  const togglePath = (path: string) => {
    setSelectedPaths(current => {
      if (current.includes(path)) return current.filter(item => item !== path);
      const next = [...current.filter(item => !item.startsWith(`${path}/`) && !path.startsWith(`${item}/`)), path];
      return next.sort();
    });
    if (!displayName.trim()) setDisplayName(path.split('/').pop() || path);
  };
  const renderDirectories = (nodes: WorkspaceTreeNode[], depth = 0): ReactNode => nodes.map(node => {
    const relativePath = relativeWorkspacePath(node.path, details!.root);
    const open = expanded.has(node.path);
    const checked = selectedPaths.includes(relativePath);
    const hasChildren = node.children.length > 0;
    return <div key={node.path} role="treeitem" aria-expanded={hasChildren ? open : undefined}>
      <div className="agent-work-directory-row" style={{ '--directory-depth': depth } as CSSProperties}>
        {hasChildren ? <button type="button" aria-label={(open ? '收起目录 ' : '展开目录 ') + relativePath} onClick={() => setExpanded(current => {
          const next = new Set(current);
          if (next.has(node.path)) next.delete(node.path); else next.add(node.path);
          return next;
        })}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button> : <span className="agent-work-directory-spacer"/>}
        <input type="checkbox" aria-label={relativePath} checked={checked} disabled={!checked && selectedPaths.length >= 20} onChange={() => togglePath(relativePath)}/>
        {open ? <FolderOpen size={14}/> : <Folder size={14}/>}<span title={relativePath}>{node.name}</span>
      </div>
      {hasChildren && open && <div role="group">{renderDirectories(node.children, depth + 1)}</div>}
    </div>;
  });
  const canSubmit = Boolean(displayName.trim() && selectedPaths.length && !create.isPending);
  return <div className="agent-work-directory-backdrop" onPointerDown={event => { if (event.target === event.currentTarget && !create.isPending) onClose(); }}>
    <section role="dialog" aria-modal="true" aria-labelledby="agent-work-directory-title" className="agent-work-directory-dialog">
      <header><div><span className="eyebrow">AGENT WORKSPACE</span><h2 id="agent-work-directory-title">新增工作区</h2></div><button type="button" aria-label="关闭新增工作区" disabled={create.isPending} onClick={onClose}><X size={16}/></button></header>
      <label className="agent-work-directory-name"><span>工作区名称</span><input autoFocus value={displayName} maxLength={160} placeholder="例如：后端服务" onChange={event => setDisplayName(event.target.value)}/></label>
      <section className="agent-work-directory-picker" aria-label="选择工作区目录">
        <header><div><b>选择目录</b><span>从 /runtime/workspace/project 中选择一个或多个子目录</span></div><em>{selectedPaths.length}/20</em></header>
        <div>
          {detailsQuery.isLoading && <p>正在读取项目目录…</p>}
          {detailsQuery.isError && <p className="error">{detailsQuery.error instanceof Error ? detailsQuery.error.message : '项目目录读取失败，请稍后重试。'}</p>}
          {!detailsQuery.isLoading && !detailsQuery.isError && !directoryNodes.length && <p>项目根目录中还没有可选择的子目录。</p>}
          {details && directoryNodes.length > 0 && <div role="tree" aria-label="项目目录树">{renderDirectories(directoryNodes)}</div>}
        </div>
      </section>
      <p className="agent-work-directory-note">选择多个目录时，它们属于同一个逻辑工作区；Agent 仍复用当前唯一 Runtime 容器。</p>
      {create.error && <p className="agent-work-directory-error">{create.error.message}</p>}
      <footer><button type="button" className="secondary" disabled={create.isPending} onClick={onClose}>取消</button><button type="button" className="primary" disabled={!canSubmit} onClick={() => create.mutate()}>{create.isPending ? '正在创建…' : '创建工作区'}</button></footer>
    </section>
  </div>;
}

type WorkspaceToolTab =
  | { id: 'files'; kind: 'files' }
  | { id: string; kind: 'terminal'; terminalInstanceId: string };
type WorkspaceToolScopeState = { tabs: WorkspaceToolTab[]; activeTabId?: string; selectedFile?: string };

function clampWorkspaceToolWidth(value: number): number {
  if (window.innerWidth <= 1100) return Math.max(300, Math.min(720, value));
  const viewportMaximum = Math.max(300, window.innerWidth - 700);
  return Math.max(300, Math.min(720, viewportMaximum, value));
}

function readWorkspaceToolState(workspaceId: string): Record<string, WorkspaceToolScopeState> {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(`flowweave:workspace-tools:${workspaceId}`) ?? '{}') as Record<string, WorkspaceToolScopeState>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function WorkspaceDrawer({
  open, onOpen, onClose, workspaceId, scopeKey, migrateFromScopeKey, bindingId, workDirectoryId, attachments, attachmentRequest, runtimeAvailable,
}: {
  open: boolean; onOpen: () => void; onClose: () => void; workspaceId: string; scopeKey: string; migrateFromScopeKey?: string; bindingId?: string; workDirectoryId?: string; attachments: AgentAttachment[]; attachmentRequest?: { key: string; attachment: AgentAttachment }; runtimeAvailable: boolean;
}) {
  const [scopeStates, setScopeStates] = useState<Record<string, WorkspaceToolScopeState>>(() => readWorkspaceToolState(workspaceId));
  const [panelWidth, setPanelWidth] = useState(() => {
    const stored = Number(localStorage.getItem('flowweave:workspace-tool-width'));
    return clampWorkspaceToolWidth(Number.isFinite(stored) ? stored : 400);
  });
  const [panelError, setPanelError] = useState('');
  const [closingTerminalId, setClosingTerminalId] = useState<string>();
  const [pendingTerminalClose, setPendingTerminalClose] = useState<Extract<WorkspaceToolTab, { kind: 'terminal' }>>();
  const [fullScreen, setFullScreen] = useState(false);
  const scopeState = scopeStates[scopeKey] ?? { tabs: [] };
  const updateScope = useCallback((updater: (current: WorkspaceToolScopeState) => WorkspaceToolScopeState) => {
    setScopeStates(current => ({ ...current, [scopeKey]: updater(current[scopeKey] ?? { tabs: [] }) }));
  }, [scopeKey]);
  useEffect(() => {
    sessionStorage.setItem(`flowweave:workspace-tools:${workspaceId}`, JSON.stringify(scopeStates));
  }, [scopeStates, workspaceId]);
  useEffect(() => {
    if (!migrateFromScopeKey || migrateFromScopeKey === scopeKey) return;
    setScopeStates(current => {
      const source = current[migrateFromScopeKey];
      if (!source || current[scopeKey]) return current;
      const next = { ...current, [scopeKey]: source };
      delete next[migrateFromScopeKey];
      return next;
    });
  }, [migrateFromScopeKey, scopeKey]);
  useEffect(() => {
    localStorage.setItem('flowweave:workspace-tool-width', String(panelWidth));
  }, [panelWidth]);
  useEffect(() => {
    const clamp = () => setPanelWidth(current => clampWorkspaceToolWidth(current));
    window.addEventListener('resize', clamp);
    return () => window.removeEventListener('resize', clamp);
  }, []);
  useEffect(() => {
    if (!fullScreen) return;
    const exitOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setFullScreen(false); };
    window.addEventListener('keydown', exitOnEscape);
    return () => window.removeEventListener('keydown', exitOnEscape);
  }, [fullScreen]);
  useEffect(() => {
    if (!open) setFullScreen(false);
  }, [open]);
  const detailsQuery = useQuery({
    queryKey: ['agent-workspace-details', workspaceId, bindingId, workDirectoryId],
    queryFn: () => api.agentWorkspaceDetails(workspaceId, { bindingId, workDirectoryId }),
    enabled: true,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const details = detailsQuery.data;
  const selectedFile = scopeState.selectedFile;
  const previewQuery = useQuery({
    queryKey: ['agent-workspace-file-preview', workspaceId, bindingId, selectedFile],
    queryFn: () => api.agentWorkspaceFilePreview(workspaceId, selectedFile!, { bindingId, workDirectoryId }),
    enabled: Boolean(open && scopeState.activeTabId === 'files' && selectedFile && isTextPreviewable(selectedFile)),
    retry: false,
  });
  const visibleFiles = useMemo(() => {
    const files = new Map((details?.files ?? []).map(file => [file.path, file]));
    for (const attachment of attachments) {
      files.set(attachment.path, { path: attachment.path, kind: 'file', size: attachment.byte_size });
    }
    return [...files.values()];
  }, [attachments, details?.files]);
  const openFiles = useCallback((path?: string) => {
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'files') ? current.tabs : [{ id: 'files', kind: 'files' }, ...current.tabs],
      activeTabId: 'files',
      selectedFile: path ?? current.selectedFile,
    }));
    onOpen();
  }, [onOpen, updateScope]);
  useEffect(() => {
    if (attachmentRequest) openFiles(attachmentRequest.attachment.path);
  }, [attachmentRequest, openFiles]);
  const openTerminal = useCallback(() => {
    if (!runtimeAvailable) return;
    const terminalInstanceId = randomId();
    updateScope(current => ({
      ...current,
      tabs: [...current.tabs, { id: `terminal:${terminalInstanceId}`, kind: 'terminal', terminalInstanceId }],
      activeTabId: `terminal:${terminalInstanceId}`,
    }));
    onOpen();
  }, [onOpen, runtimeAvailable, updateScope]);
  const selectFile = (path: string) => openFiles(path);
  const closeTab = async (tab: WorkspaceToolTab) => {
    if (tab.kind === 'terminal') {
      setPanelError('');
      setClosingTerminalId(tab.terminalInstanceId);
      try {
        await api.closeAgentWorkspaceTerminal(workspaceId, tab.terminalInstanceId);
      } catch (error) {
        setPanelError(error instanceof Error ? error.message : '终端关闭失败，请稍后重试。');
        setClosingTerminalId(undefined);
        return;
      }
      setClosingTerminalId(undefined);
    }
    updateScope(current => {
      const tabs = current.tabs.filter(candidate => candidate.id !== tab.id);
      return { ...current, tabs, activeTabId: current.activeTabId === tab.id ? tabs[tabs.length - 1]?.id : current.activeTabId };
    });
    if (scopeState.tabs.length === 1 && scopeState.tabs[0]?.id === tab.id) {
      setFullScreen(false);
      onClose();
    }
  };
  const requestCloseTab = (tab: WorkspaceToolTab) => {
    if (tab.kind === 'terminal') {
      setPendingTerminalClose(tab);
      return;
    }
    void closeTab(tab);
  };
  useEffect(() => {
    if (!pendingTerminalClose || closingTerminalId) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPendingTerminalClose(undefined);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [closingTerminalId, pendingTerminalClose]);
  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!open || window.innerWidth <= 960) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    const move = (moveEvent: PointerEvent) => setPanelWidth(clampWorkspaceToolWidth(startWidth + startX - moveEvent.clientX));
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      document.body.style.removeProperty('cursor');
      document.body.style.removeProperty('user-select');
    };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop, { once: true });
  };
  const loadingOrError = detailsQuery.isError
    ? <div className="agent-drawer-empty"><b>工作区读取失败</b><span>{detailsQuery.error instanceof Error ? detailsQuery.error.message : '暂时无法读取工作区，请稍后重试。'}</span><button type="button" className="secondary" onClick={() => void detailsQuery.refetch()}>重试</button></div>
    : !details
      ? <div className="agent-drawer-empty"><LoaderCircle className="agent-drawer-spinner" size={20}/><span>正在读取工作区…</span></div>
      : null;
  const selectedAttachment = attachments.find(item => item.path === selectedFile);
  const selectedMimeType = selectedAttachment?.mime_type ?? '';
  const selectedFileUrl = selectedFile
    ? agentWorkspaceFileUrl(workspaceId, selectedFile, { bindingId, workDirectoryId, download: false })
    : '';
  const canPreviewImage = Boolean(selectedFile && (selectedMimeType.startsWith('image/') || /\.(?:avif|gif|jpe?g|png|svg|webp)$/i.test(selectedFile)));
  const canPreviewPdf = Boolean(selectedFile && (selectedMimeType === 'application/pdf' || /\.pdf$/i.test(selectedFile)));
  const summary = details && <section className="agent-workspace-overview">
    <article><FolderOpen size={16}/><div><small>当前工作区</small><b>{details.scope.display_name}</b><code>{details.working_directory}</code></div></article>
    <article><MonitorCog size={16}/><div><small>运行容器</small><b>{details.runtime.container_id || '运行环境恢复中'}</b><p>所有会话共用此 Workspace Runtime；每个终端保留独立会话。</p></div></article>
    <article><GitBranch size={16}/><div><small>Git 仓库</small>{details.repositories.length ? details.repositories.map(repository => <p key={repository.path}><b>{relativeWorkspacePath(repository.path, details.root)}</b>{repository.branch && <span>{repository.branch}</span>}{repository.head && <em>{repository.head.slice(0, 12)}</em>}{repository.remote && <code>{repository.remote}</code>}</p>) : <p>当前目录未检测到 Git 仓库。</p>}</div></article>
    <article><MonitorCog size={16}/><div><small>IDEA / Gateway</small><b>{details.ide.gateway.status}</b><code>{details.ide.workspace_path}</code><p>{details.ide.gateway.note}</p></div></article>
    <article><FileText size={16}/><div><small>本会话附件</small>{attachments.length ? attachments.map(item => <button type="button" key={item.path} onClick={() => selectFile(item.path)}>{item.filename}</button>) : <p>当前会话还没有附件。</p>}</div></article>
  </section>;
  return <><aside className={`agent-workspace-drawer ${open ? 'tools-open' : 'summary-open'}${fullScreen ? ' fullscreen' : ''}`} style={{ width: fullScreen ? undefined : open ? panelWidth : 272 }} role={fullScreen ? 'dialog' : undefined} aria-modal={fullScreen || undefined} aria-label={fullScreen ? '全屏工作区工具' : undefined}>
    <div className="agent-workspace-resizer" role="separator" aria-label="调整工作区工具宽度" aria-orientation="vertical" onPointerDown={startResize}/>
    <section className={`agent-workspace-summary ${open ? 'panel-hidden' : ''}`}>
      <header><div><span className="eyebrow">WORKSPACE</span><b>环境信息</b></div><button type="button" aria-label="打开工作区工具" onClick={onOpen}><PanelRightOpen size={16}/></button></header>
      <div className="agent-workspace-quick-actions"><button type="button" onClick={() => openFiles()}><FileCode2 size={14}/>文件</button><button type="button" disabled={!runtimeAvailable} onClick={openTerminal}><Plus size={14}/>新终端</button></div>
      {loadingOrError || summary}
    </section>
    <section className={`agent-workspace-tool-shell ${open ? '' : 'panel-hidden'}`}>
      <header><nav className="agent-workspace-tabs" aria-label="工作区工具页签">{scopeState.tabs.map(tab => <div key={tab.id} className={scopeState.activeTabId === tab.id ? 'active' : ''}><button type="button" className="agent-workspace-tab-select" onClick={() => updateScope(current => ({ ...current, activeTabId: tab.id }))}><span>{tab.kind === 'files' ? '文件' : details?.runtime.container_id || '连接中…'}</span></button><button type="button" className="agent-workspace-tab-close" aria-label={`关闭${tab.kind === 'files' ? '文件' : `终端 ${details?.runtime.container_id || ''}`}页签`} disabled={tab.kind === 'terminal' && closingTerminalId === tab.terminalInstanceId} onClick={() => { if (tab.kind !== 'terminal' || closingTerminalId !== tab.terminalInstanceId) requestCloseTab(tab); }}><X size={12}/></button></div>)}</nav><div className="agent-workspace-tool-actions"><details><summary aria-label="新增工作区工具"><Plus size={15}/></summary><div><button type="button" onClick={event => { openFiles(); event.currentTarget.closest('details')?.removeAttribute('open'); }}><FileCode2 size={13}/>文件</button><button type="button" disabled={!runtimeAvailable} onClick={event => { openTerminal(); event.currentTarget.closest('details')?.removeAttribute('open'); }}><Plus size={13}/>终端</button></div></details><button type="button" aria-label={fullScreen ? '退出全屏' : '全屏查看工作区工具'} title={fullScreen ? '退出全屏（Esc）' : '全屏查看'} onClick={() => setFullScreen(current => !current)}>{fullScreen ? <Minimize2 size={16}/> : <Maximize2 size={16}/>}</button><button type="button" aria-label="关闭工作区工具" onClick={() => { setFullScreen(false); onClose(); }}><X size={16}/></button></div></header>
      <div className="agent-workspace-tool-body">
        {panelError && <p className="agent-workspace-panel-error">{panelError}</p>}
        {loadingOrError || (!scopeState.tabs.length ? <div className="agent-drawer-empty"><b>选择工作区工具</b><span>文件仅打开一个页签；终端可按需打开多个独立实例。</span><div><button type="button" className="secondary" onClick={() => openFiles()}>打开文件</button><button type="button" className="secondary" disabled={!runtimeAvailable} onClick={openTerminal}>新建终端</button></div></div> : details && <div className="agent-workspace-tool-content">
          {scopeState.tabs.some(tab => tab.kind === 'files') && <section className={`agent-workspace-files ${scopeState.activeTabId === 'files' ? 'active' : ''}`}>
            <WorkspaceFileTree entries={visibleFiles} root={details.root} selectedFile={selectedFile} onSelect={path => updateScope(current => ({ ...current, selectedFile: path }))}/>
            <div className="agent-file-preview">{selectedFile ? <>
              <header><span>{relativeWorkspacePath(selectedFile, details.root)}</span><a href={agentWorkspaceFileUrl(workspaceId, selectedFile, { bindingId, workDirectoryId, download: true })}><Download size={13}/>下载</a></header>
              {canPreviewImage ? <img className="agent-file-media-preview" src={selectedAttachment?.image_data_url || selectedFileUrl} alt={selectedAttachment?.filename || '附件预览'}/> : canPreviewPdf ? <iframe className="agent-file-media-preview" title={selectedAttachment?.filename || 'PDF 预览'} src={selectedFileUrl}/> : isTextPreviewable(selectedFile) ? previewQuery.isLoading ? <p>正在读取文件…</p> : previewQuery.isError ? <p>文件预览不可用，请下载后查看。</p> : <pre>{previewQuery.data}</pre> : <p>此文件不提供浏览器预览，请下载后查看。</p>}
            </> : <p>选择一个文件以预览或下载。</p>}</div>
          </section>}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'terminal' }> => tab.kind === 'terminal').map(tab => <div key={tab.id} className={`agent-terminal-tab-panel ${scopeState.activeTabId === tab.id ? 'active' : ''}`}>{runtimeAvailable ? <WorkspaceTerminal workspaceId={workspaceId} terminalInstanceId={tab.terminalInstanceId} bindingId={bindingId} workDirectoryId={workDirectoryId} workingDirectory={details.working_directory}/> : <div className="agent-drawer-empty"><LoaderCircle className="agent-drawer-spinner" size={20}/><b>终端正在恢复</b><span>文件仍可使用；运行环境恢复后终端会自动可用。</span></div>}</div>)}
        </div>)}
      </div>
    </section>
  </aside>{pendingTerminalClose && <div className="agent-terminal-close-backdrop" onPointerDown={event => { if (event.target === event.currentTarget && !closingTerminalId) setPendingTerminalClose(undefined); }}><section role="dialog" aria-modal="true" aria-labelledby="agent-terminal-close-title" className="agent-terminal-close-dialog"><header><span className="eyebrow">TERMINAL</span><h2 id="agent-terminal-close-title">关闭此终端？</h2></header><p>关闭后会停止该终端中正在执行的命令，并清除这一个终端会话；其他终端和当前会话不会受影响。</p><footer><button type="button" className="secondary" disabled={Boolean(closingTerminalId)} onClick={() => setPendingTerminalClose(undefined)}>取消</button><button type="button" className="danger" autoFocus disabled={Boolean(closingTerminalId)} onClick={() => { const tab = pendingTerminalClose; setPendingTerminalClose(undefined); void closeTab(tab); }}>{closingTerminalId ? '正在关闭…' : '关闭终端'}</button></footer></section></div>}</>;
}

interface Props { onNavigate: (path: string, replace?: boolean) => void; onOpenModels: () => void; }

export function AgentWorkbenchPage({ onNavigate, onOpenModels }: Props) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('disabled');
  const [liveText, setLiveText] = useState('');
  const [liveEvents, setLiveEvents] = useState<OpenHandsConversationEvent[]>([]);
  const [hiddenEventIds, setHiddenEventIds] = useState<Set<string>>(() => new Set());
  const [turnState, setTurnState] = useState<TurnState>('idle');
  const [activeTurnEventId, setActiveTurnEventId] = useState<string>();
  const [requestStartedAt, setRequestStartedAt] = useState<number>();
  const [confirmationReason, setConfirmationReason] = useState('');
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [pendingRewrite, setPendingRewrite] = useState<{ eventId: string; content: string }>();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState('');
  const [newConversationProviderId, setNewConversationProviderId] = useState('');
  const [newConversationModelName, setNewConversationModelName] = useState('');
  const [newConversationReasoningEffort, setNewConversationReasoningEffort] = useState<string | null>(null);
  const [conversationProviderId, setConversationProviderId] = useState('');
  const [conversationModelName, setConversationModelName] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AgentAttachment[]>([]);
  const [attachmentRequest, setAttachmentRequest] = useState<{ key: string; attachment: AgentAttachment }>();
  const [operationError, setOperationError] = useState<Error>();
  const [pendingCreatedId, setPendingCreatedId] = useState<string>();
  const [pendingMigratedSend, setPendingMigratedSend] = useState<BoundQueuedMessage>();
  const [conversationDraft, setConversationDraft] = useState<ConversationDraft>();
  const [workspaceScopeMigration, setWorkspaceScopeMigration] = useState<string>();
  const [workDirectoryCreatorOpen, setWorkDirectoryCreatorOpen] = useState(false);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const pendingLiveText = useRef('');
  const liveTextFrame = useRef<number | undefined>(undefined);
  const selectedBindingId = bindingIdFromLocation();
  const workspaceQuery = useQuery({ queryKey: ['agent-workspace-default'], queryFn: api.defaultAgentWorkspace, retry: false });
  const workspace = workspaceQuery.data;
  const runtimeQuery = useQuery({ queryKey: ['agent-workspace-runtime', workspace?.id], queryFn: () => api.agentWorkspaceRuntime(workspace!.id), enabled: Boolean(workspace), refetchInterval: query => query.state.data?.state === 'RECOVERING' ? 5000 : false });
  const conversationsQuery = useQuery({ queryKey: ['agent-conversations', workspace?.id], queryFn: () => api.agentConversations(workspace!.id), enabled: Boolean(workspace) });
  const workDirectoriesQuery = useQuery({ queryKey: ['agent-work-directories', workspace?.id], queryFn: () => api.agentWorkDirectories(workspace!.id), enabled: Boolean(workspace) });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers, enabled: Boolean(workspace) });
  const conversations = useMemo(() => conversationsQuery.data ?? [], [conversationsQuery.data]);
  const selected = useMemo(() => conversations.find(item => item.id === selectedBindingId), [conversations, selectedBindingId]);
  const composerScope = selected?.id ?? conversationDraft?.id;
  const activeComposerScope = useRef<string | undefined>(undefined);
  activeComposerScope.current = composerScope;
  const reportOperationError = useCallback((scope: string | undefined, error: Error) => {
    if (scope && activeComposerScope.current === scope) setOperationError(error);
  }, []);
  const connectedProviders = (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default));
  const runtime = runtimeQuery.data;
  const runtimeWritable = Boolean(workspace && runtime?.write_available);
  const canOpenConversation = runtimeWritable;
  const canWrite = Boolean(runtimeWritable && selected);
  const canBootstrap = Boolean(runtimeWritable && conversationDraft && newConversationProviderId && newConversationModelName);
  const canCompose = Boolean(canWrite || (runtimeWritable && conversationDraft));
  const isGenerating = turnState === 'running' || turnState === 'pausing' || turnState === 'resuming';
  const eventsQuery = useQuery({
    queryKey: ['agent-conversation-events', workspace?.id, selected?.id], queryFn: () => api.agentConversationEvents(workspace!.id, selected!.id), enabled: Boolean(workspace && selected), refetchInterval: isGenerating ? 1200 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const displayedEvents = useMemo(
    () => mergeConversationEvents(eventsQuery.data?.events ?? [], liveEvents).filter(event => !hiddenEventIds.has(event.id)),
    [eventsQuery.data?.events, hiddenEventIds, liveEvents],
  );
  const sessionAttachments = useMemo(() => {
    const byPath = new Map<string, AgentAttachment>();
    for (const event of displayedEvents) {
      if (event.event_type !== 'MESSAGE' || !Array.isArray(event.payload.attachments)) continue;
      for (const attachment of event.payload.attachments) byPath.set(attachment.path, attachment);
    }
    return [...byPath.values()];
  }, [displayedEvents]);
  const drawerAttachments = useMemo(() => {
    const byPath = new Map(sessionAttachments.map(attachment => [attachment.path, attachment]));
    for (const attachment of attachments) byPath.set(attachment.path, attachment);
    return [...byPath.values()];
  }, [attachments, sessionAttachments]);
  const openAttachmentInDrawer = useCallback((attachment: AgentAttachment) => {
    setAttachmentRequest({ key: randomId(), attachment });
    setDrawerOpen(true);
  }, []);
  const inputReadinessQuery = useQuery({
    queryKey: ['agent-conversation-input-readiness', workspace?.id, selected?.id],
    queryFn: () => api.agentConversationInputReadiness(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected && (turnState === 'pausing' || queuedMessages.length > 0)),
    refetchInterval: turnState === 'pausing' || queuedMessages.length > 0 ? 700 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const contextQuery = useQuery({
    queryKey: ['agent-conversation-context', workspace?.id, selected?.id],
    queryFn: () => api.agentConversationContext(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected), refetchInterval: isGenerating ? 2000 : false,
  });
  const confirmationQuery = useQuery({
    queryKey: ['agent-conversation-confirmation', workspace?.id, selected?.id],
    queryFn: () => api.agentPendingConfirmation(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected && runtime?.write_available),
    refetchInterval: isGenerating ? 1200 : 2500,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const pendingConfirmation = confirmationQuery.data?.pending ? confirmationQuery.data : undefined;
  const refresh = useCallback(() => {
    if (!workspace) return;
    void queryClient.invalidateQueries({ queryKey: ['agent-workspace-runtime', workspace.id] });
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
    if (selected?.id) {
      void queryClient.invalidateQueries({ queryKey: ['agent-conversation-events', workspace.id, selected.id] });
      void queryClient.invalidateQueries({ queryKey: ['agent-conversation-confirmation', workspace.id, selected.id] });
    }
  }, [queryClient, selected?.id, workspace]);
  const clearLiveText = useCallback(() => {
    pendingLiveText.current = '';
    if (liveTextFrame.current !== undefined) window.cancelAnimationFrame(liveTextFrame.current);
    liveTextFrame.current = undefined;
    setLiveText('');
  }, []);
  const appendLiveText = useCallback((content: string) => {
    pendingLiveText.current += content;
    if (liveTextFrame.current !== undefined) return;
    liveTextFrame.current = window.requestAnimationFrame(() => {
      liveTextFrame.current = undefined;
      const next = pendingLiveText.current;
      pendingLiveText.current = '';
      if (next) setLiveText(current => current + next);
    });
  }, []);
  useEffect(() => () => {
    if (liveTextFrame.current !== undefined) window.cancelAnimationFrame(liveTextFrame.current);
  }, []);
  const onStreamEvent = useCallback((event: { type: 'delta' | 'event' | 'message_complete'; content?: string; event?: OpenHandsConversationEvent }) => {
    if (event.type === 'delta' && event.content) appendLiveText(event.content);
    if (event.type === 'event' && event.event) {
      setLiveEvents(current => mergeConversationEvents(current, [event.event!]));
      const formalCommentary = typeof event.event.payload.thought === 'string'
        ? event.event.payload.thought
        : ['THOUGHT', 'TOOL_CALL'].includes(event.event.event_type) && typeof event.event.payload.content === 'string'
          ? event.event.payload.content
          : '';
      // Replace a streamed commentary draft only when its formal ActionEvent
      // projection arrives. Empty tool/status frames must not erase visible
      // model output before OpenHands has persisted an equivalent event.
      if (formalCommentary || ['MESSAGE', 'ERROR', 'COMPLETED'].includes(event.event.event_type)) clearLiveText();
    }
    // Completion frames do not identify the originating user event.  A stale
    // frame must never complete a newer turn; durable assistant/error events
    // associated with activeTurnEventId are the authoritative terminal signal.
    if (event.type === 'message_complete') { clearLiveText(); refresh(); }
  }, [appendLiveText, clearLiveText, refresh]);

  useEffect(() => {
    if (!conversationDraft && !selectedBindingId && conversations.length) onNavigate(`/agent/conversations/${encodeURIComponent(conversations[0].id)}`, true);
    if (selectedBindingId && conversations.length && !selected && pendingCreatedId !== selectedBindingId && !conversationsQuery.isFetching) onNavigate('/agent', true);
  }, [conversationDraft, conversations, conversationsQuery.isFetching, onNavigate, pendingCreatedId, selected, selectedBindingId]);
  useEffect(() => { if (!workspace || !selected || !runtime?.write_available) { setStreamStatus('disabled'); return; } return subscribeToAgentWorkspaceStream(workspace.id, selected.id, onStreamEvent, setStreamStatus); }, [onStreamEvent, runtime?.write_available, selected, workspace]);
  useEffect(() => { if (selected?.id === pendingCreatedId) setPendingCreatedId(undefined); }, [pendingCreatedId, selected?.id]);
  useEffect(() => { setEditing(false); clearLiveText(); setLiveEvents([]); setHiddenEventIds(new Set()); setActiveTurnEventId(undefined); setRequestStartedAt(undefined); setConfirmationReason(''); setTurnState('idle'); setQueuedMessages([]); setPendingRewrite(undefined); setAttachments([]); setOperationError(undefined); }, [clearLiveText, composerScope]);
  useEffect(() => {
    if (!editing) setTitle(selected?.display_title ?? '');
  }, [editing, selected?.display_title]);
  useEffect(() => {
    if (!editing) return;
    titleInput.current?.focus();
    titleInput.current?.select();
  }, [editing]);
  useEffect(() => {
    setConversationProviderId(selected?.model_provider_id ?? '');
    setConversationModelName(selected?.model_name ?? '');
    setReasoningEffort(selected?.reasoning_effort ?? null);
    setNewConversationProviderId(current => current || selected?.model_provider_id || '');
  }, [selected?.id, selected?.model_name, selected?.model_provider_id, selected?.reasoning_effort]);
  useEffect(() => {
    const provider = connectedProviders.find(item => item.id === newConversationProviderId)
      ?? connectedProviders[0];
    if (!provider) return;
    const model = provider.models.find(item => item.enabled && item.model_name === newConversationModelName)
      ?? provider.models.find(item => item.enabled && item.is_default);
    if (!newConversationProviderId) setNewConversationProviderId(provider.id);
    if (model && model.model_name !== newConversationModelName) {
      setNewConversationModelName(model.model_name);
      setNewConversationReasoningEffort(model.default_reasoning_effort ?? null);
    }
  }, [connectedProviders, newConversationModelName, newConversationProviderId]);
  useEffect(() => {
    if (turnState === 'pausing' && inputReadinessQuery.data?.ready) setTurnState('paused');
  }, [inputReadinessQuery.data?.ready, turnState]);
  useEffect(() => {
    if ((turnState === 'running' || turnState === 'resuming') && activeTurnEventId && hasFinishedTurn(displayedEvents, activeTurnEventId)) {
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      setTurnState('idle');
      refresh();
    }
  }, [activeTurnEventId, clearLiveText, displayedEvents, refresh, turnState]);

  const bootstrap = useMutation({ mutationFn: (message: QueuedMessage) => api.bootstrapAgentConversation(
    workspace!.id,
    conversationDraft!.id,
    newConversationProviderId,
    newConversationModelName,
    newConversationReasoningEffort,
    message.content,
    message.items,
    conversationDraft?.workDirectoryId,
  ), onSuccess: value => {
    if (!workspace) return;
    const conversation = value.conversation;
    setWorkspaceScopeMigration(conversationDraft?.id);
    setPendingCreatedId(conversation.id);
    setConversationDraft(undefined);
    queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace.id], current => [conversation, ...(current ?? []).filter(item => item.id !== conversation.id)]);
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
    onNavigate(`/agent/conversations/${encodeURIComponent(conversation.id)}`);
  }, onError: (error, message) => { if (activeComposerScope.current === message.scope) { setDraft(message.content); setAttachments(message.items); } reportOperationError(message.scope, error); } });
  const rename = useMutation({ mutationFn: () => api.updateAgentConversation(workspace!.id, selected!.id, title.trim()), onSuccess: conversation => {
    queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace!.id], current => (current ?? []).map(item => item.id === conversation.id ? conversation : item));
    setTitle(conversationName(conversation));
    setEditing(false);
    refresh();
  }, onError: error => reportOperationError(selected?.id, error) });
  const remove = useMutation({ mutationFn: () => api.deleteAgentConversation(workspace!.id, selected!.id), onSuccess: () => { setDrawerOpen(false); onNavigate('/agent', true); refresh(); }, onError: error => reportOperationError(selected?.id, error) });
  const persistModel = useMutation({
    mutationFn: ({ providerId, modelName, effort }: { providerId: string; modelName: string; effort: string | null }) => api.switchAgentConversationModel(workspace!.id, selected!.id, providerId, modelName, effort),
    onSuccess: value => {
      queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace!.id], current => (current ?? []).map(item => item.id === selected!.id ? { ...item, ...value } : item));
      setConversationProviderId(value.model_provider_id);
      setConversationModelName(value.model_name ?? '');
      setReasoningEffort(value.reasoning_effort ?? null);
      void queryClient.invalidateQueries({ queryKey: ['agent-conversation-context', workspace!.id, selected!.id] });
    },
    onError: () => {
      setConversationProviderId(selected?.model_provider_id ?? '');
      setConversationModelName(selected?.model_name ?? '');
      setReasoningEffort(selected?.reasoning_effort ?? null);
      reportOperationError(selected?.id, persistModel.error as Error);
    },
  });
  const send = useMutation({
    mutationFn: (message: BoundQueuedMessage) => api.sendAgentMessage(workspace!.id, message.bindingId, message.content, message.items),
    onMutate: message => {
      const optimisticEventId = `pending-user:${randomId()}`;
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(Date.now());
      setLiveEvents([{ id: optimisticEventId, event_type: 'MESSAGE', payload: { source: 'user', content: message.content } }]);
      setTurnState('running');
      return { optimisticEventId };
    },
    onSuccess: (value, message, context) => {
      const cursor = value.cursor;
      if (cursor) {
        setActiveTurnEventId(cursor);
        setLiveEvents(current => mergeConversationEvents(
          current.filter(event => event.id !== context?.optimisticEventId),
          [{ id: cursor, event_type: 'MESSAGE', payload: { source: 'user', content: message.content, attachments: message.items } }],
        ));
      }
      setAttachments([]);
      refresh();
    },
    onError: (error, message, context) => {
      if (error instanceof ApiError && error.code === 'AGENT_CONVERSATION_BUSY') {
        setQueuedMessages(current => [...current, { id: message.id, scope: message.bindingId, content: message.content, items: message.items }]);
        setLiveEvents(current => current.filter(event => event.id !== context?.optimisticEventId));
        setActiveTurnEventId(undefined);
        setTurnState('running');
        return;
      }
      setLiveEvents(current => current.filter(event => event.id !== context?.optimisticEventId));
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      setTurnState('idle');
      reportOperationError(message.bindingId, error);
    },
  });
  const migrateStreaming = useMutation({
    mutationFn: (_message: QueuedMessage) => {
      void _message;
      const providerId = selected?.model_provider_id || contextQuery.data?.provider_id;
      if (!providerId) throw new ApiError('此历史会话缺少可迁移的模型供应商，请新建会话。', 'AGENT_CONVERSATION_PROVIDER_REQUIRED', {}, 409);
      const provider = connectedProviders.find(item => item.id === providerId);
      return api.migrateAgentStreamingConversation(
        workspace!.id,
        selected!.id,
        providerId,
        selected?.model_name || configuredModelName(provider, contextQuery.data?.model_name),
        selected?.model_name ? selected.reasoning_effort : contextQuery.data?.reasoning_effort,
      );
    },
    onSuccess: (value, message) => {
      if (!workspace) return;
      setPendingCreatedId(value.id);
      queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace.id], current => [value, ...(current ?? []).filter(item => item.id !== value.id)]);
      setPendingMigratedSend({ ...message, bindingId: value.id });
      void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
      onNavigate(`/agent/conversations/${encodeURIComponent(value.id)}`);
    },
    onError: (error, message) => {
      if (activeComposerScope.current === message.scope) { setDraft(message.content); setAttachments(message.items); }
      reportOperationError(message.scope, error);
    },
  });
  const upload = useMutation({ mutationFn: ({ file }: { file: File; scope: string }) => selected
    ? api.uploadAgentAttachment(workspace!.id, selected.id, file)
    : api.uploadAgentWorkspaceAttachment(workspace!.id, file, conversationDraft?.workDirectoryId, conversationDraft?.id), onSuccess: (value, request) => {
    if (activeComposerScope.current === request.scope) setAttachments(items => [...items, value]);
  }, onError: (error, request) => reportOperationError(request.scope, error) });
  const fork = useMutation({ mutationFn: (eventId: string) => api.forkAgentConversation(workspace!.id, selected!.id, eventId), onSuccess: value => {
    if (!workspace) return;
    setPendingCreatedId(value.id);
    queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace.id], current => [value, ...(current ?? []).filter(item => item.id !== value.id)]);
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
    onNavigate(`/agent/conversations/${encodeURIComponent(value.id)}`);
  }, onError: error => reportOperationError(selected?.id, error) });
  const interrupt = useMutation({ mutationFn: () => api.interruptAgentConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('pausing'), onSuccess: refresh, onError: error => { setTurnState('running'); reportOperationError(selected?.id, error); } });
  const resume = useMutation({ mutationFn: () => api.resumeAgentConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('resuming'), onSuccess: value => { if (value.cursor) setActiveTurnEventId(value.cursor); setTurnState('running'); refresh(); }, onError: error => { setTurnState('paused'); reportOperationError(selected?.id, error); } });
  const decideConfirmation = useMutation({
    mutationFn: (accept: boolean) => api.decideAgentConfirmation(workspace!.id, selected!.id, pendingConfirmation!.pending_actions_digest!, accept, confirmationReason.trim()),
    onSuccess: value => {
      const cursor = value.cursor ?? undefined;
      if (cursor) setActiveTurnEventId(current => current ?? cursor);
      setConfirmationReason('');
      setRequestStartedAt(Date.now());
      setTurnState('running');
      void queryClient.invalidateQueries({ queryKey: ['agent-conversation-confirmation', workspace!.id, selected!.id] });
      refresh();
    },
    onError: error => reportOperationError(selected?.id, error),
  });
  const rewrite = useMutation({
    mutationFn: ({ eventId, content }: { eventId: string; content: string }) => api.rerunAgentMessage(workspace!.id, selected!.id, eventId, content),
    onMutate: request => {
      const optimisticEventId = `pending-rewrite:${randomId()}`;
      const branch = eventBranchIds(displayedEvents, request.eventId);
      const replacementParentId = displayedEvents.find(event => event.id === request.eventId)?.payload.parent_id;
      setQueuedMessages([]);
      clearLiveText();
      setHiddenEventIds(current => new Set([...current, ...branch]));
      setLiveEvents([{ id: optimisticEventId, event_type: 'MESSAGE', payload: { source: 'user', content: request.content, parent_id: replacementParentId } }]);
      setActiveTurnEventId(undefined);
      setRequestStartedAt(Date.now());
      setTurnState('running');
      return { optimisticEventId, branch, replacementParentId };
    },
    onSuccess: (value, request, context) => {
      const cursor = value.cursor;
      if (cursor) {
        setActiveTurnEventId(cursor);
        setLiveEvents(current => mergeConversationEvents(
          current.filter(event => event.id !== context?.optimisticEventId),
          [{ id: cursor, event_type: 'MESSAGE', payload: { source: 'user', content: request.content, parent_id: context?.replacementParentId } }],
        ));
      }
      refresh();
    },
    onError: (error, _request, context) => {
      setLiveEvents(current => current.filter(event => event.id !== context?.optimisticEventId));
      setHiddenEventIds(current => {
        const next = new Set(current);
        for (const eventId of context?.branch ?? []) next.delete(eventId);
        return next;
      });
      setRequestStartedAt(undefined);
      setTurnState('paused');
      reportOperationError(selected?.id, error);
    },
  });
  useEffect(() => {
    if (!pendingMigratedSend || selected?.id !== pendingMigratedSend.bindingId || send.isPending) return;
    const message = pendingMigratedSend;
    setPendingMigratedSend(undefined);
    send.mutate(message);
  }, [pendingMigratedSend, selected?.id, send]);
  useEffect(() => {
    if (turnState !== 'pausing' || !inputReadinessQuery.data?.ready) return;
    if (pendingRewrite) {
      const request = pendingRewrite;
      setPendingRewrite(undefined);
      rewrite.mutate(request);
    } else setTurnState('paused');
  }, [inputReadinessQuery.data?.ready, pendingRewrite, rewrite, turnState]);
  const requestRewrite = useCallback((eventId: string, content: string) => {
    if (turnState === 'running') {
      setPendingRewrite({ eventId, content });
      interrupt.mutate();
      return;
    }
    if (turnState === 'pausing') {
      setPendingRewrite({ eventId, content });
      return;
    }
    if (turnState === 'idle' || turnState === 'paused') rewrite.mutate({ eventId, content });
  }, [interrupt, rewrite, turnState]);
  const openConversationDraft = useCallback((next: Omit<ConversationDraft, 'id'>) => {
    setConversationDraft({ ...next, id: randomId() });
    setWorkspaceScopeMigration(undefined);
    setDraft('');
    setAttachments([]);
    clearLiveText();
    setLiveEvents([]);
    setHiddenEventIds(new Set());
    setTurnState('idle');
    onNavigate('/agent');
  }, [clearLiveText, onNavigate]);
  const enqueueDraft = useCallback(() => {
    const content = draft.trim();
    if ((!content && !attachments.length) || migrateStreaming.isPending || pendingMigratedSend || turnState === 'pausing' || turnState === 'resuming') return;
    setDraft('');
    if (!composerScope) return;
    const message = { id: randomId(), scope: composerScope, content, items: attachments };
    setAttachments([]);
    if (conversationDraft) {
      if (canBootstrap && !bootstrap.isPending) bootstrap.mutate(message);
      else { setDraft(content); setAttachments(attachments); }
      return;
    }
    if (!canWrite) { setDraft(content); setAttachments(attachments); return; }
    if (turnState === 'idle') {
      if (selected?.streaming_callback_ready) send.mutate({ ...message, bindingId: selected.id });
      else migrateStreaming.mutate(message);
    }
    else setQueuedMessages(items => [...items, message]);
  }, [attachments, bootstrap, canBootstrap, canWrite, composerScope, conversationDraft, draft, migrateStreaming, pendingMigratedSend, selected, send, turnState]);
  useEffect(() => {
    if (turnState !== 'idle' || !queuedMessages.length || send.isPending || migrateStreaming.isPending || pendingMigratedSend) return;
    const [next, ...rest] = queuedMessages;
    setQueuedMessages(rest);
    if (selected?.streaming_callback_ready) send.mutate({ ...next, bindingId: selected.id });
    else migrateStreaming.mutate(next);
  }, [migrateStreaming, pendingMigratedSend, queuedMessages, selected, send, turnState]);
  useEffect(() => {
    if (turnState === 'pausing' || !queuedMessages.length || !inputReadinessQuery.data?.ready || send.isPending) return;
    setTurnState('idle');
  }, [inputReadinessQuery.data?.ready, queuedMessages.length, send.isPending, turnState]);

  if (workspaceQuery.isLoading) return <main className="agent-workbench-loading">正在打开 Agent 工作台…</main>;
  if (workspaceQuery.error || !workspace) return <main className="agent-workbench-loading"><b>Agent 工作台正在初始化</b><span>默认运行环境准备完成后，会话列表会自动出现。</span></main>;
  const draftProviderInfo = connectedProviders.find(item => item.id === newConversationProviderId);
  const draftConversationModel = draftProviderInfo?.models.find(model => model.enabled && model.model_name === newConversationModelName)
    ?? draftProviderInfo?.models.find(model => model.enabled && model.is_default);
  const availableDraftModels = draftProviderInfo?.models.filter(model => model.enabled) ?? [];
  const supportedDraftEfforts = draftConversationModel?.supported_reasoning_efforts ?? [];
  const conversationProviderInfo = connectedProviders.find(item => item.id === conversationProviderId);
  const boundProviderInfo = connectedProviders.find(item => item.id === selected?.model_provider_id);
  const activeConversationModelName = conversationModelName
    || selected?.model_name
    || configuredModelName(conversationProviderInfo, contextQuery.data?.model_name)
    || contextQuery.data?.model_name
    || '';
  const conversationModel = conversationProviderInfo?.models.find(model => model.enabled && model.model_name === activeConversationModelName)
    ?? conversationProviderInfo?.models.find(model => model.enabled && model.is_default);
  const availableConversationModels = conversationProviderInfo?.models.filter(model => model.enabled) ?? [];
  const supportedEfforts = conversationModel?.supported_reasoning_efforts ?? [];
  const contextProgress = contextQuery.data
    && typeof contextQuery.data.used_tokens === 'number' && contextQuery.data.used_tokens > 0
    && typeof contextQuery.data.window_tokens === 'number' && contextQuery.data.window_tokens > 0
    ? {
      used: contextQuery.data.used_tokens,
      window: contextQuery.data.window_tokens,
      usedLabel: compactTokenCount(contextQuery.data.used_tokens),
      windowLabel: compactTokenCount(contextQuery.data.window_tokens),
      percentage: Math.min(100, Math.round((contextQuery.data.used_tokens / contextQuery.data.window_tokens) * 100)),
    }
    : undefined;
  const composerStatus = conversationDraft && !newConversationModelName ? '请选择模型' : persistModel.isPending ? '正在保存模型设置' : migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : turnState === 'pausing' ? '正在暂停' : turnState === 'paused' ? '已暂停' : turnState === 'resuming' ? '正在继续' : turnState === 'running' ? '正在处理' : streamStatus === 'recovering' ? '连接恢复中' : undefined;
  const composerNote = queuedMessages.length > 0 ? `已排队 ${queuedMessages.length} 条` : '';
  const visibleError = operationError ?? confirmationQuery.error ?? eventsQuery.error;
  const composerActionLabel = bootstrap.isPending ? '正在创建会话' : migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : turnState === 'idle'
    ? '发送消息'
    : turnState === 'running'
      ? '暂停当前 Agent'
      : turnState === 'paused'
        ? '继续当前 Agent'
        : turnState === 'pausing' ? '正在暂停 Agent' : '正在继续 Agent';
  const composerActionDisabled = !(canWrite || canBootstrap)
    || Boolean(pendingConfirmation)
    || bootstrap.isPending
    || migrateStreaming.isPending
    || Boolean(pendingMigratedSend)
    || (turnState === 'idle' && ((!draft.trim() && !attachments.length) || send.isPending))
    || turnState === 'pausing'
    || turnState === 'resuming';
  const runComposerAction = () => {
    if (turnState === 'idle') enqueueDraft();
    else if (turnState === 'running') interrupt.mutate();
    else if (turnState === 'paused') resume.mutate();
  };
  const workDirectories = workDirectoriesQuery.data?.items ?? [];
  const activeWorkDirectoryIds = new Set(workDirectories.map(item => item.id));
  const rootConversations = conversations.filter(item => !item.work_directory_id);
  const archivedDirectoryConversations = conversations.filter(item => {
    const workDirectoryId = item.work_directory_id;
    return workDirectoryId ? !activeWorkDirectoryIds.has(workDirectoryId) : false;
  });
  const conversationsForDirectory = (workDirectoryId: string) => conversations.filter(
    item => item.work_directory_id === workDirectoryId,
  );
  const selectConversation = (bindingId: string) => {
    setConversationDraft(undefined);
    onNavigate(`/agent/conversations/${encodeURIComponent(bindingId)}`);
  };

  return <main className="agent-workbench-page">
    <aside className="agent-workbench-rail">
      <header><div><span className="eyebrow">AGENT WORKSPACE</span><h1>Agent 会话</h1></div><div className="agent-workbench-create-actions"><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: '根工作区' })}><Plus size={15}/>新建会话</button><button type="button" className="secondary" aria-label="新增工作区" onClick={() => setWorkDirectoryCreatorOpen(true)}><FolderPlus size={14}/>新增工作区</button></div></header>
      <div className="agent-workbench-list">
        <section className="agent-workspace-group">
          <header><div><Folder size={14}/><span>根工作区</span></div><button type="button" aria-label="在根工作区中新建会话" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: '根工作区' })}><Plus size={13}/></button></header>
          {rootConversations.map(item => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => selectConversation(item.id)}><CircleDot size={13}/><span><b>{conversationName(item)}</b><small>{item.title_state === 'PENDING' ? '正在生成标题' : '可继续会话'}</small></span><ChevronRight size={13}/></button>)}
        </section>
        {workDirectories.map(directory => <section key={directory.id} className="agent-workspace-group">
          <header><div><Folder size={14}/><span>{directory.display_name}</span></div><button type="button" aria-label={`在${directory.display_name}中新建会话`} disabled={!canOpenConversation} onClick={() => openConversationDraft({ workDirectoryId: directory.id, displayName: directory.display_name })}><Plus size={13}/></button></header>
          {conversationsForDirectory(directory.id).map(item => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => selectConversation(item.id)}><CircleDot size={13}/><span><b>{conversationName(item)}</b><small>{item.title_state === 'PENDING' ? '正在生成标题' : '可继续会话'}</small></span><ChevronRight size={13}/></button>)}
        </section>)}
        {archivedDirectoryConversations.length > 0 && <section className="agent-workspace-group">
          <header><div><Folder size={14}/><span>已归档工作目录</span></div></header>
          {archivedDirectoryConversations.map(item => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => selectConversation(item.id)}><CircleDot size={13}/><span><b>{conversationName(item)}</b><small>保留的历史会话</small></span><ChevronRight size={13}/></button>)}
        </section>}
      </div>
      {!conversations.length && !conversationDraft && <div className="agent-workbench-rail-empty"><Bot size={25}/><b>还没有会话</b><span>{connectedProviders.length ? '选择工作目录后新建会话开始协作。' : '请先完成至少一个模型供应商的连接测试。'}</span></div>}
    </aside>
    <section className="agent-workbench-main">
      <header className="agent-workbench-header"><div><span className="eyebrow">DIRECT AGENT SESSION</span>{editing ? <div className="agent-title-edit"><input ref={titleInput} aria-label="会话标题" value={title} onChange={event => setTitle(event.target.value)} onBlur={() => { if (!rename.isPending) { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }} onKeyDown={event => { if (event.key === 'Enter' && title.trim()) { event.preventDefault(); rename.mutate(); } if (event.key === 'Escape') { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }}/></div> : <h2 title={selected ? '双击修改标题' : undefined} onDoubleClick={() => { if (!selected) return; setTitle(conversationName(selected)); setEditing(true); }}>{selected ? conversationName(selected) : conversationDraft ? '新会话' : '开始一个新的会话'}</h2>}{(selected || conversationDraft) && <small className="agent-session-provider">当前供应商：{selected ? boundProviderInfo?.name ?? '未配置' : draftProviderInfo?.name ?? '请选择模型供应商'}{conversationDraft ? ` · ${conversationDraft.displayName}` : ''}</small>}</div>{selected && <div className="agent-header-actions"><button type="button" className="danger" aria-label="删除会话" disabled={remove.isPending} onClick={() => remove.mutate()}><Trash2 size={14}/></button></div>}</header>
      {runtime?.state === 'RECOVERING' && <section className="agent-runtime-recover"><LoaderCircle size={18}/><div><b>运行环境正在恢复</b><span>{runtime.message || '历史会话和工作区文件仍可查看；恢复完成后可继续发送消息和使用终端。'}</span></div></section>}
      {selected ? <ConversationSurface events={displayedEvents} liveText={liveText} isGenerating={isGenerating} requestStartedAt={requestStartedAt} requestSubmitting={send.isPending || rewrite.isPending} rewritePending={rewrite.isPending || Boolean(pendingRewrite)} onRewrite={requestRewrite} onFork={eventId => fork.mutate(eventId)} onOpenAttachment={openAttachmentInDrawer}/> : conversationDraft ? <div className="agent-workbench-empty"><Bot size={32}/><b>需要我帮你完成什么？</b><span>你可以直接发送问题，也可以先选择模型、添加附件或打开工作区工具。</span>{!connectedProviders.length && <button className="primary" onClick={onOpenModels}>配置模型供应商</button>}</div> : <div className="agent-workbench-empty"><Bot size={32}/><b>新建会话开始协作</b><span>每个会话共享同一工作区，但保留独立的对话与事件记录。</span><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: '根工作区' })}><Plus size={15}/>新建会话</button></div>}
      {(selected || conversationDraft) && runtime?.state !== 'RECOVERING' && <div className={`agent-composer ${turnState !== 'idle' || pendingConfirmation ? 'busy' : ''}`}>
        {pendingConfirmation && <section className="agent-confirmation" aria-label="工具执行确认"><header><ShieldAlert size={17}/><div><b>工具正在等待你的确认</b><span>动作尚未执行。请核对整批内容后批准或拒绝。</span></div></header><div className="agent-confirmation-actions">{(pendingConfirmation.actions ?? []).map((action: AgentPendingConfirmationAction) => <article key={action.digest}><div><b>{action.summary || action.tool_name}</b><span>{action.security_risk || 'UNKNOWN'}</span></div>{Object.keys(action.arguments).length > 0 && <pre>{JSON.stringify(action.arguments, null, 2)}</pre>}</article>)}</div><textarea aria-label="工具确认理由" value={confirmationReason} maxLength={2000} placeholder="填写批准或拒绝理由…" onChange={event => setConfirmationReason(event.target.value)}/><footer><button type="button" className="danger" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(false)}><X size={14}/>拒绝整批</button><button type="button" className="primary" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(true)}><Check size={14}/>批准整批</button></footer></section>}
        {queuedMessages.length > 0 && <section className="agent-queued-messages" aria-label="已排队消息"><header><b>消息队列</b><span>{queuedMessages.length} 条将在当前回复完成后依次发送</span></header>{queuedMessages.map((message, index) => <article key={message.id}><small>{index + 1}</small><p>{message.content || '图片附件'}</p><span>{message.items.length ? `${message.items.length} 个附件` : ''}</span><div><button type="button" aria-label={`编辑排队消息 ${index + 1}`} onClick={() => { setDraft(message.content); setAttachments(message.items); setQueuedMessages(items => items.filter(item => item.id !== message.id)); }}>编辑</button><button type="button" aria-label={`移除排队消息 ${index + 1}`} onClick={() => setQueuedMessages(items => items.filter(item => item.id !== message.id))}><X size={13}/></button></div></article>)}</section>}
        <textarea aria-label="发送 Agent 消息" value={draft} maxLength={200_000} placeholder={pendingConfirmation ? '请先处理上方工具确认…' : turnState === 'paused' ? '已暂停：可继续，也可编辑上方消息重新思考…' : '给 Agent 发消息…'} disabled={!canCompose || Boolean(pendingConfirmation) || bootstrap.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend) || turnState === 'pausing' || turnState === 'resuming'} onChange={event => setDraft(event.target.value)} onPaste={event => { const images = Array.from(event.clipboardData.items).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter((file): file is File => file !== null); if (!images.length || !composerScope) return; event.preventDefault(); for (const image of images) upload.mutate({ file: image, scope: composerScope }); }} onKeyDown={event => { if (isImeComposition(event)) return; if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); enqueueDraft(); } }}/>
        {attachments.length > 0 && <div className="agent-attachments">{attachments.map(item => <span key={item.path}><button type="button" className="agent-attachment-open" title={`在右侧查看附件：${item.filename}`} onClick={() => openAttachmentInDrawer(item)}>{item.image_data_url && <img src={item.image_data_url} alt=""/>}<em>{item.filename}</em></button><button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(all => all.filter(candidate => candidate.path !== item.path))}>×</button></span>)}</div>}
        <footer>
          <div className="agent-composer-context">
            {(selected || conversationDraft) && <><input ref={attachmentInput} aria-label="上传附件" type="file" multiple hidden onChange={event => { if (composerScope) for (const file of Array.from(event.target.files ?? [])) upload.mutate({ file, scope: composerScope }); event.currentTarget.value = ''; }}/><button type="button" aria-label="添加附件" disabled={!canCompose || Boolean(pendingConfirmation) || upload.isPending} onClick={() => attachmentInput.current?.click()}><Plus size={17}/></button></>}
            {contextProgress && <span className="agent-context-progress" title={`当前上下文 ${contextProgress.used.toLocaleString()} / ${contextProgress.window.toLocaleString()} tokens`} aria-label={`上下文用量 ${contextProgress.percentage}%`}><i style={{ '--context-progress': `${contextProgress.percentage}%` } as CSSProperties}/><em>{contextProgress.usedLabel} / {contextProgress.windowLabel}</em></span>}
            {composerStatus && <span className="agent-composer-status">{composerStatus}</span>}
            {composerNote && <span className="agent-composer-note">{composerNote}</span>}
          </div>
          <div className="agent-composer-actions">
            {selected ? <ComposerModelMenu providers={connectedProviders} providerId={conversationProviderId} modelName={activeConversationModelName} models={availableConversationModels} efforts={supportedEfforts} effort={reasoningEffort ?? selected.reasoning_effort ?? contextQuery.data?.reasoning_effort ?? conversationModel?.default_reasoning_effort ?? ''} disabled={!canWrite || isGenerating || queuedMessages.length > 0 || Boolean(pendingConfirmation) || persistModel.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend)} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; const effort = model.default_reasoning_effort ?? null; setConversationProviderId(providerId); setConversationModelName(model.model_name); setReasoningEffort(effort); persistModel.mutate({ providerId, modelName: model.model_name, effort }); }} onModelChange={modelName => { const model = availableConversationModels.find(item => item.model_name === modelName); const effort = model?.default_reasoning_effort ?? null; setConversationModelName(modelName); setReasoningEffort(effort); persistModel.mutate({ providerId: conversationProviderId, modelName, effort }); }} onEffortChange={effort => { const nextEffort = effort || null; setReasoningEffort(nextEffort); persistModel.mutate({ providerId: conversationProviderId, modelName: activeConversationModelName, effort: nextEffort }); }}/> : <ComposerModelMenu providers={connectedProviders} providerId={newConversationProviderId} modelName={newConversationModelName} models={availableDraftModels} efforts={supportedDraftEfforts} effort={newConversationReasoningEffort ?? draftConversationModel?.default_reasoning_effort ?? ''} disabled={!runtimeWritable || bootstrap.isPending} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; setNewConversationProviderId(providerId); setNewConversationModelName(model.model_name); setNewConversationReasoningEffort(model.default_reasoning_effort ?? null); }} onModelChange={modelName => { const model = availableDraftModels.find(item => item.model_name === modelName); setNewConversationModelName(modelName); setNewConversationReasoningEffort(model?.default_reasoning_effort ?? null); }} onEffortChange={effort => setNewConversationReasoningEffort(effort || null)}/>}
            <button type="button" className={`agent-send${turnState === 'paused' || turnState === 'resuming' ? ' resume' : ''}`} aria-label={composerActionLabel} disabled={composerActionDisabled} onClick={runComposerAction}>{pendingConfirmation ? <ShieldAlert size={14}/> : turnState === 'idle' ? <Send size={16}/> : turnState === 'paused' || turnState === 'resuming' ? <Play size={12} fill="currentColor"/> : <Square size={10} fill="currentColor"/>}</button>
          </div>
        </footer>
      </div>}
      {visibleError && <p className="agent-workbench-error">{visibleError.message}</p>}
    </section>
    <WorkspaceDrawer open={drawerOpen} onOpen={() => setDrawerOpen(true)} onClose={() => setDrawerOpen(false)} workspaceId={workspace.id} scopeKey={selected?.id ?? pendingCreatedId ?? conversationDraft?.id ?? 'workspace-root'} migrateFromScopeKey={workspaceScopeMigration} bindingId={selected?.id} workDirectoryId={selected ? undefined : conversationDraft?.workDirectoryId} attachments={drawerAttachments} attachmentRequest={attachmentRequest} runtimeAvailable={Boolean(runtime?.write_available)}/>
    {workDirectoryCreatorOpen && <WorkDirectoryCreator workspaceId={workspace.id} onClose={() => setWorkDirectoryCreatorOpen(false)} onCreated={directory => {
      queryClient.setQueryData<AgentWorkDirectoryList>(['agent-work-directories', workspace.id], current => current ? { ...current, items: [directory, ...current.items.filter(item => item.id !== directory.id)] } : current);
      void queryClient.invalidateQueries({ queryKey: ['agent-work-directories', workspace.id] });
    }}/>}
  </main>;
}
