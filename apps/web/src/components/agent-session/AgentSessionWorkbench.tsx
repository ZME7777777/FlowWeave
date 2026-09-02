import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Bot, Boxes, Check, ChevronDown, ChevronRight, CircleDot, Copy, Download, FileCode2, FileText, Folder, FolderOpen, FolderPlus, GitBranch, ImageIcon, Layers3, Link2, LoaderCircle, Maximize2, Minimize2, MonitorCog, PanelRightOpen, Play, Plus, Search, Send, ShieldAlert, Square, Trash2, X } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ApiError, randomId } from '../../api/client';
import { agentWorkspaceSessionGateway, type AgentSessionGateway } from '../../api/agent-session-gateway';
import { withoutDeploymentBase } from '../../deploymentPath';
import { agentWorkspaceSessionHost, type AgentSessionHost } from './session-host';
import { ConversationSurface } from '../ConversationSurface';
import { useEscapeClose } from '../useEscapeClose';
import { selectCapabilityVersion, selectCapabilityVersions } from '../../utils/capabilitySelection';
import type { AgentAttachment, AgentConversation, AgentPendingConfirmationAction, AgentSessionCapability, AgentSessionMcpReadiness, AgentSessionWorkDirectory, AgentSessionWorkDirectoryList, CapabilityAsset, CapabilityCollection, ModelProvider, OpenHandsConversationEvent, OpenHandsConversationEventBatch, ProviderModel } from '../../types';
import '../../pages/agent-workbench.css';
import '../../pages/agent-workbench-layout.css';

type StreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';
type TurnState = 'idle' | 'running' | 'pausing' | 'paused' | 'resuming';
interface QueuedMessage {
  id: string;
  scope: string;
  content: string;
  items: AgentAttachment[];
}
interface BoundQueuedMessage extends QueuedMessage { bindingId: string; }
interface ConversationDraft { id: string; workDirectoryId?: string; displayName: string; capabilityVersionIds?: string[]; }
interface BootstrapRecovery {
  draft: ConversationDraft;
  message: QueuedMessage;
  providerId: string;
  modelName: string;
  reasoningEffort: string | null;
  attempts: number;
}
interface OptimisticBootstrapTurn {
  scope: string;
  event: OpenHandsConversationEvent;
}
interface ConversationSource {
  id: string;
  kind: 'url' | 'file' | 'image';
  label: string;
  url?: string;
  attachment?: AgentAttachment;
  pending?: boolean;
}

interface WorkspaceConversationGroupProps {
  groupId: string;
  label: string;
  children: ReactNode;
  canCreateConversation?: boolean;
  onCreateConversation?: () => void;
}

function sessionQueryKey(host: AgentSessionHost, resource: string, ...identifiers: Array<string | undefined>) {
  return host.queryKey(resource, ...identifiers);
}

const MAX_BOOTSTRAP_RECONCILIATION_ATTEMPTS = 3;

const AgentSessionGatewayContext = createContext<AgentSessionGateway>(agentWorkspaceSessionGateway);
const AgentSessionHostContext = createContext<AgentSessionHost>(agentWorkspaceSessionHost);

function useAgentSessionGateway(): AgentSessionGateway {
  return useContext(AgentSessionGatewayContext);
}

function useAgentSessionHost(): AgentSessionHost {
  return useContext(AgentSessionHostContext);
}

function WorkspaceConversationGroup({ groupId, label, children, canCreateConversation = false, onCreateConversation }: WorkspaceConversationGroupProps) {
  const [collapsed, setCollapsed] = useState(false);
  const contentId = `agent-workspace-group-${groupId}`;

  return <section className={`agent-workspace-group${collapsed ? ' collapsed' : ''}`}>
    <header>
      <button type="button" className="agent-workspace-group-toggle" aria-label={`${collapsed ? '展开' : '收起'}工作区 ${label}`} aria-expanded={!collapsed} aria-controls={contentId} onClick={() => setCollapsed(current => !current)}>
        <Folder size={14}/><span>{label}</span><ChevronDown size={13}/>
      </button>
      {onCreateConversation && <button type="button" aria-label={`在${label}中新建会话`} disabled={!canCreateConversation} onClick={onCreateConversation}><Plus size={13}/></button>}
    </header>
    <div id={contentId} className="agent-workspace-group-content" hidden={collapsed}>{children}</div>
  </section>;
}

function readBootstrapRecovery(storageKey: string): BootstrapRecovery | undefined {
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (!stored) return undefined;
    const value = JSON.parse(stored) as Partial<BootstrapRecovery>;
    if (!value.draft?.id || !value.message?.scope || value.message.scope !== value.draft.id
      || typeof value.message.content !== 'string' || typeof value.providerId !== 'string'
      || typeof value.modelName !== 'string' || typeof value.attempts !== 'number') return undefined;
    const recovery = value as BootstrapRecovery;
    // Old clients could persist a terminal recovery forever. Give such a
    // record one final server reconciliation, then the normal error path
    // restores the draft and removes it.
    return recovery.attempts >= MAX_BOOTSTRAP_RECONCILIATION_ATTEMPTS
      ? { ...recovery, attempts: MAX_BOOTSTRAP_RECONCILIATION_ATTEMPTS - 1 }
      : recovery;
  } catch {
    return undefined;
  }
}

function writeBootstrapRecovery(storageKey: string, recovery: BootstrapRecovery | undefined) {
  try {
    if (recovery) window.sessionStorage.setItem(storageKey, JSON.stringify(recovery));
    else window.sessionStorage.removeItem(storageKey);
  } catch {
    // Browser storage is only a recovery aid. The server-side command remains
    // the source of truth and retries still use the stable draft UUID.
  }
}

function isBootstrapAmbiguous(error: Error): boolean {
  return [
    'AGENT_BOOTSTRAP_CREATION_AMBIGUOUS',
    'AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS',
  ].includes((error as ApiError).code);
}

type AgentCapabilityType = 'SKILL' | 'MCP' | 'PLUGIN' | 'CONTEXT';
type ComposerSuggestionKind = 'SKILL' | 'COMMAND' | 'MCP' | 'NATIVE';
type NativeComposerAction = 'CONDENSE';
interface ComposerSuggestion {
  id: string;
  kind: ComposerSuggestionKind;
  token: string;
  label: string;
  detail: string;
  nativeAction?: NativeComposerAction;
}

function composerTrigger(value: string): { sigil: '$' | '/'; query: string; start: number } | undefined {
  const match = /(?:^|\s)([$/])([^\s]*)$/.exec(value);
  if (!match) return undefined;
  return { sigil: match[1] as '$' | '/', query: match[2], start: value.length - match[0].length + (match[0].startsWith(' ') ? 1 : 0) };
}

function stringValues(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : [];
}

function ComposerCapabilityAutocomplete({
  draft, suggestions, disabled, placeholder, onDraftChange, onPaste, onSubmit, onManageCapabilities, onNativeAction,
}: {
  draft: string; suggestions: ComposerSuggestion[]; disabled: boolean; placeholder: string;
  onDraftChange: (value: string) => void; onPaste: (event: ReactClipboardEvent<HTMLTextAreaElement>) => void; onSubmit: () => void;
  onManageCapabilities?: () => void;
  onNativeAction?: (action: NativeComposerAction) => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const trigger = composerTrigger(draft);
  const visible = useMemo(() => {
    if (!trigger) return [];
    const needle = trigger.query.toLocaleLowerCase();
    return suggestions.filter(item => (trigger.sigil === '$' ? item.kind === 'SKILL' : item.kind !== 'SKILL')
      && (!needle || `${item.token} ${item.label} ${item.detail}`.toLocaleLowerCase().includes(needle)));
  }, [suggestions, trigger]);
  useEffect(() => setActiveIndex(0), [draft]);
  const select = (item: ComposerSuggestion) => {
    if (!trigger) return;
    if (item.kind === 'NATIVE' && item.nativeAction) {
      onDraftChange(`${draft.slice(0, trigger.start)}${draft.slice(trigger.start + trigger.query.length + 1)}`);
      onNativeAction?.(item.nativeAction);
      return;
    }
    onDraftChange(`${draft.slice(0, trigger.start)}${item.token} ${draft.slice(trigger.start + trigger.query.length + 1)}`);
    requestAnimationFrame(() => input.current?.focus());
  };
  const hasSuggestions = visible.length > 0;
  // A slash is an explicit request for a command or MCP.  Keep the picker
  // available for a draft before it has a native Conversation binding too.
  const showCapabilityManager = Boolean(trigger && onManageCapabilities);
  const hasMenu = Boolean(trigger && (hasSuggestions || showCapabilityManager));
  const hasNativeSuggestions = suggestions.some(item => item.kind === 'NATIVE');
  return <div className="agent-composer-input">
    <textarea ref={input} aria-label="发送 Agent 消息" aria-autocomplete="list" aria-controls={hasMenu ? 'agent-composer-capabilities' : undefined} aria-expanded={hasMenu} value={draft} maxLength={200_000} placeholder={placeholder} disabled={disabled} onChange={event => onDraftChange(event.target.value)} onPaste={onPaste} onKeyDown={event => {
      if (isImeComposition(event)) return;
      if (hasMenu && event.key === 'Escape') { onDraftChange(draft.slice(0, -trigger!.query.length - 1)); return; }
      if (hasSuggestions && ['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key)) {
        event.preventDefault();
        if (event.key === 'ArrowDown') { setActiveIndex(index => (index + 1) % visible.length); return; }
        if (event.key === 'ArrowUp') { setActiveIndex(index => (index - 1 + visible.length) % visible.length); return; }
        select(visible[activeIndex] ?? visible[0]);
        return;
      }
      if (hasMenu && ['Enter', 'Tab'].includes(event.key)) { event.preventDefault(); return; }
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); onSubmit(); }
    }}/>
    {hasMenu && <div id="agent-composer-capabilities" className="agent-composer-capability-menu" role="listbox" aria-label={trigger!.sigil === '$' ? '选择技能' : hasNativeSuggestions ? '选择 OpenHands 原生能力、命令或 MCP' : '选择命令或 MCP'}>{hasSuggestions ? visible.map((item, index) => <div className="agent-composer-capability-option" key={item.id}>{trigger!.sigil === '/' && (index === 0 || visible[index - 1]?.kind === 'NATIVE') && item.kind !== 'NATIVE' && <div className="agent-composer-capability-section">MCP 与命令</div>}{trigger!.sigil === '/' && item.kind === 'NATIVE' && (index === 0 || visible[index - 1]?.kind !== 'NATIVE') && <div className="agent-composer-capability-section">OpenHands 原生能力</div>}<button type="button" role="option" aria-selected={index === activeIndex} className={index === activeIndex ? 'active' : ''} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => select(item)}><code>{item.token}</code><span><b>{item.label}</b><small>{item.detail}</small></span><em>{item.kind === 'SKILL' ? '技能' : item.kind === 'COMMAND' ? '命令' : item.kind === 'NATIVE' ? '原生' : 'MCP'}</em></button></div>) : <div className="agent-composer-capability-empty"><span><b>{!suggestions.length ? trigger!.sigil === '$' ? '当前会话还没有加载 Skill' : '当前会话还没有加载命令或 MCP' : '当前会话没有匹配的能力'}</b><small>{!suggestions.length ? `先为此会话加载能力，随后可在这里用 ${trigger!.sigil} 选择并插入。` : '调整输入关键词，或管理当前会话能力。'}</small></span></div>}{showCapabilityManager && <div className="agent-composer-capability-manage"><span>管理当前会话能力</span><button type="button" onMouseDown={event => event.preventDefault()} onClick={onManageCapabilities}>管理</button></div>}</div>}
  </div>;
}

function CapabilityManager({ workspaceId, bindingId, conversationCapabilities, draftCapabilityIds, onClose, onCreateEnhancedConversation }: {
  workspaceId: string; bindingId?: string; conversationCapabilities?: AgentSessionCapability[]; onClose: () => void;
  draftCapabilityIds?: string[]; onCreateEnhancedConversation?: (capabilityVersionIds: string[]) => void;
}) {
  const { api } = useAgentSessionGateway();
  const host = useAgentSessionHost();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<AgentCapabilityType | 'ALL'>('ALL');
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [mcpReadiness, setMcpReadiness] = useState<Record<string, AgentSessionMcpReadiness | undefined>>({});
  const [checkingMcpIds, setCheckingMcpIds] = useState<Set<string>>(new Set());
  const dialog = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEscapeClose(() => { if (!save.isPending) onClose(); });
  useEffect(() => {
    const opener = returnFocus.current;
    closeButton.current?.focus();
    return () => { opener?.focus(); };
  }, []);
  const catalogQuery = useQuery({ queryKey: sessionQueryKey(host, 'capability-catalog'), queryFn: api.capabilities });
  const collectionsQuery = useQuery({ queryKey: sessionQueryKey(host, 'capability-collections'), queryFn: api.capabilityCollections });
  useEffect(() => {
    const current = bindingId ? conversationCapabilities : draftCapabilityIds?.map(id => ({ id }));
    if (current) setSelectedIds(current.map(item => item.id));
  }, [bindingId, conversationCapabilities, draftCapabilityIds]);
  const frozenContextIds = useMemo(() => new Set(
    (conversationCapabilities ?? [])
      .filter(item => item.capability_type === 'CONTEXT')
      .map(item => item.id),
  ), [conversationCapabilities]);
  const frozenContextCount = frozenContextIds.size;
  const capabilities = useMemo(() => (catalogQuery.data ?? []).filter(item =>
    ['SKILL', 'MCP', 'PLUGIN', 'CONTEXT'].includes(item.capability_type)
    && (item.is_latest || frozenContextIds.has(item.id))
    && (!bindingId || item.capability_type !== 'CONTEXT' || frozenContextIds.has(item.id)),
  ), [bindingId, catalogQuery.data, frozenContextIds]);
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return capabilities.filter(item =>
      (kind === 'ALL' || item.capability_type === kind)
      && (!needle || `${item.capability_key} ${item.description} ${item.filename}`.toLocaleLowerCase().includes(needle)),
    );
  }, [capabilities, kind, query]);
  const byId = useMemo(() => new Map(capabilities.map(item => [item.id, item])), [capabilities]);
  const selectedMcpIds = useMemo(() => selectedIds.filter(id => byId.get(id)?.capability_type === 'MCP'), [byId, selectedIds]);
  const readonlyContext = Boolean(bindingId && kind === 'CONTEXT');
  const selectableVisibleIds = useMemo(() => visible
    .filter(item => !bindingId || !(conversationCapabilities ?? []).some(enabled => enabled.id === item.id))
    .map(item => item.id), [bindingId, conversationCapabilities, visible]);
  const allVisibleSelected = selectableVisibleIds.length > 0 && selectableVisibleIds.every(id => selectedIds.includes(id));
  const checkMcpReadiness = useCallback(async (ids: string[]) => {
    if (!ids.length) return [] as Array<readonly [string, AgentSessionMcpReadiness]>;
    setCheckingMcpIds(current => new Set([...current, ...ids]));
    const results = await Promise.all(ids.map(async id => {
      try {
        return [id, await api.mcpReadiness(workspaceId, id)] as const;
      } catch {
        return [id, { state: 'UNAVAILABLE', error_kind: 'unknown', checked_at: new Date().toISOString() } satisfies AgentSessionMcpReadiness] as const;
      }
    }));
    setMcpReadiness(current => ({ ...current, ...Object.fromEntries(results) }));
    setCheckingMcpIds(current => {
      const next = new Set(current);
      ids.forEach(id => next.delete(id));
      return next;
    });
    return results;
  }, [api, workspaceId]);
  useEffect(() => {
    if (selectedMcpIds.length) void checkMcpReadiness(selectedMcpIds);
  }, [checkMcpReadiness, selectedMcpIds]);
  const save = useMutation({
    mutationFn: async () => {
      const readiness = await checkMcpReadiness(selectedMcpIds);
      const unavailable = readiness.find(([, status]) => status.state !== 'READY');
      if (unavailable) {
        const [id, status] = unavailable;
        const capability = byId.get(id);
        const reason = status.error_kind === 'timeout' ? '连接超时' : status.error_kind === 'connection' ? '无法连接' : '暂时不可用';
        throw new Error(`MCP「${capability?.capability_key ?? '未命名'}」${reason}，请重新检测后再保存。`);
      }
      if (!bindingId) return selectedIds;
      const loaded = new Set((conversationCapabilities ?? []).map(item => item.id));
      let latest: AgentConversation | undefined;
      for (const capabilityVersionId of selectedIds.filter(id => !loaded.has(id))) {
        latest = await api.addConversationCapability(workspaceId, bindingId, capabilityVersionId);
      }
      return latest;
    },
    onSuccess: value => {
      if (!bindingId) onCreateEnhancedConversation?.(value as string[]);
      if (bindingId && value) {
        queryClient.setQueryData<AgentConversation[]>(sessionQueryKey(host, 'conversations', workspaceId), current =>
          current?.map(item => item.id === bindingId ? value as AgentConversation : item),
        );
        queryClient.setQueryData<AgentConversation>(sessionQueryKey(host, 'conversation', workspaceId, bindingId), value as AgentConversation);
      }
      onClose();
    },
  });
  const toggle = (item: CapabilityAsset) => setSelectedIds(current => {
    if (current.includes(item.id)) {
      // The formal OpenHands API is additive.  Do not display an uncheckable
      // illusion for a capability already loaded into this native session.
      if (bindingId && (conversationCapabilities ?? []).some(enabled => enabled.id === item.id)) return current;
      return current.filter(id => id !== item.id);
    }
    return selectCapabilityVersion(current, item, byId);
  });
  const toggleVisible = () => setSelectedIds(current => {
    if (allVisibleSelected) return current.filter(id => !selectableVisibleIds.includes(id));
    return selectCapabilityVersions(current, selectableVisibleIds.map(id => byId.get(id)).filter((item): item is CapabilityAsset => Boolean(item)), byId);
  });
  const toggleCollection = (collection: CapabilityCollection) => setSelectedIds(current => {
    const memberIds = collection.members.map(member => member.id).filter(id => byId.has(id));
    if (!memberIds.length) return current;
    const isSelected = memberIds.every(id => current.includes(id));
    if (isSelected) return current.filter(id => !memberIds.includes(id) || (bindingId && (conversationCapabilities ?? []).some(item => item.id === id)));
    return selectCapabilityVersions(current, memberIds.map(id => byId.get(id)).filter((item): item is CapabilityAsset => Boolean(item)), byId);
  });
  const trapFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])') ?? [])].filter(item => item.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  return <div className="agent-capability-backdrop" role="presentation" onPointerDown={event => { if (event.target === event.currentTarget && !save.isPending) onClose(); }}>
    <section ref={dialog} className="agent-capability-manager" role="dialog" aria-modal="true" aria-labelledby="agent-capability-title" onKeyDown={trapFocus}>
      <header><div><span className="eyebrow">{bindingId ? 'CURRENT AGENT SESSION' : 'NEW AGENT SESSION'}</span><h2 id="agent-capability-title">能力</h2><p>{bindingId ? '可为当前会话注册新的已发布 Skill、MCP 或 Plugin。Context 仅在创建会话时冻结；在 Context 标签中可查看已装配版本，但不能新增、编辑或删除。' : '新会话默认不挂载能力；在这里选择的版本只会冻结到即将创建的这一个会话。Context 会作为 OpenHands 系统级会话上下文，不会作为用户消息发送。'}</p></div><button ref={closeButton} type="button" aria-label="关闭插件管理" disabled={save.isPending} onClick={onClose}><X size={18}/></button></header>
      <div className="agent-capability-toolbar"><div className="agent-capability-tabs">{([['ALL', '全部'], ['PLUGIN', '插件'], ['MCP', 'MCP'], ['SKILL', '技能'], ['CONTEXT', 'Context']] as const).map(([value, label]) => <button type="button" key={value} className={kind === value ? 'active' : ''} onClick={() => setKind(value)}>{label}</button>)}</div><div className="agent-capability-toolbar-actions">{!readonlyContext && <button type="button" className="agent-capability-select-visible" disabled={!selectableVisibleIds.length} onClick={toggleVisible}>{allVisibleSelected ? '取消选择筛选结果' : `选择筛选结果 (${selectableVisibleIds.length})`}</button>}<label className="agent-capability-search"><Search size={15}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、说明或文件…"/></label></div></div>
      <div className="agent-capability-summary"><span>{readonlyContext ? <>已装配 <b>{frozenContextCount}</b> 个 Context</> : <>{bindingId ? '已注册' : '已选择'} <b>{selectedIds.length}</b> 项</>}</span><span>{readonlyContext ? 'Context 在创建会话时冻结，仅供查看，不能新增、编辑、取消或删除。' : bindingId ? '可继续注册新能力；已注册能力已锁定，不能取消或删除。' : '选择只作用于本次新会话，不会改变工作区或其他会话。'}</span></div>
      {(kind === 'ALL' || kind === 'SKILL') && collectionsQuery.data?.length ? <section className="agent-capability-collections"><span>Skill 组合</span><div>{collectionsQuery.data.map(collection => { const memberIds = collection.members.map(member => member.id).filter(id => byId.has(id)); const selected = memberIds.length > 0 && memberIds.every(id => selectedIds.includes(id)); return <button type="button" key={collection.id} className={selected ? 'selected' : ''} onClick={() => toggleCollection(collection)}><Layers3 size={13}/><b>{collection.name}</b><em>{collection.members.length}</em>{selected && <Check size={13}/>}</button>; })}</div></section> : null}
      <div className="agent-capability-list">{catalogQuery.isLoading ? <p>正在读取能力仓库…</p> : visible.length === 0 ? <p>{readonlyContext ? '此会话创建时没有装配 Context。' : '没有匹配的已发布能力。'}</p> : visible.map(item => { const checked = selectedIds.includes(item.id); const isFrozenContext = Boolean(bindingId && item.capability_type === 'CONTEXT' && frozenContextIds.has(item.id)); const locked = Boolean(bindingId && (conversationCapabilities ?? []).some(enabled => enabled.id === item.id)); const isMcp = item.capability_type === 'MCP'; const readiness = mcpReadiness[item.id]; const checking = isMcp && checkingMcpIds.has(item.id); const readinessLabel = item.capability_type === 'CONTEXT' ? '系统上下文' : !isMcp ? (item.capability_type === 'SKILL' ? '技能' : item.capability_type) : !checked ? 'MCP' : checking ? '检测中' : readiness?.state === 'READY' ? '已连接' : readiness?.error_kind === 'timeout' ? '连接超时' : readiness?.error_kind === 'connection' ? '连接失败' : '不可用'; const detail = isFrozenContext ? '创建会话时已装配，仅供查看，不能编辑或删除。' : locked ? '已注册到当前会话，不能取消或删除。' : item.capability_type === 'CONTEXT' ? '创建会话时冻结，并追加到 OpenHands 系统提示词后缀。' : isMcp && checked && readiness?.state === 'UNAVAILABLE' ? `MCP ${readinessLabel}；不会保存为新会话默认能力。` : item.description || item.filename; const lockedLabel = isFrozenContext ? `${item.capability_key}（创建时已装配，只读）` : `${item.capability_key}（已注册，不能取消）`; const lockedTitle = isFrozenContext ? '该 Context 在创建会话时已装配，仅供查看，不能新增、编辑或删除。' : '该能力已注册到当前会话，不能取消或删除。'; return <button type="button" key={item.id} className={`${checked ? 'selected' : ''}${locked ? ' locked' : ''}`} disabled={locked} aria-label={locked ? lockedLabel : undefined} title={locked ? lockedTitle : undefined} onClick={() => toggle(item)}><span className={`agent-capability-icon ${item.capability_type.toLowerCase()}`}><Boxes size={17}/></span><span><b>{item.capability_key}</b><small>{detail}</small><em className={isMcp && checked ? `mcp-status ${readiness?.state === 'READY' ? 'ready' : readiness?.state === 'UNAVAILABLE' ? 'unavailable' : 'checking'}` : undefined}>{readinessLabel}</em></span><i aria-hidden="true">{checked ? <Check size={15}/> : null}</i></button>; })}</div>
      {save.error && <div className="agent-capability-error"><p>{save.error.message}</p>{save.error instanceof ApiError && save.error.code === 'AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE' && onCreateEnhancedConversation && <div className="agent-capability-migration"><span>这条历史会话未在创建时注册原生能力市场。可新建一个空能力会话后，再按需选择要挂载的能力；此会话的历史内容会保留不变。</span><button type="button" className="secondary" disabled={save.isPending} onClick={() => onCreateEnhancedConversation([])}><Plus size={13}/>新建可使用能力的会话</button></div>}</div>}
      <footer>{!readonlyContext && selectedMcpIds.length > 0 && <button type="button" className="secondary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => void checkMcpReadiness(selectedMcpIds)}>重新检测 MCP</button>}<button type="button" className="secondary" disabled={save.isPending} onClick={onClose}>{readonlyContext ? '关闭' : '取消'}</button>{!readonlyContext && <button type="button" className="primary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => save.mutate()}>{save.isPending ? '正在注册…' : bindingId ? '注册到当前会话' : '用于新建会话'}</button>}</footer>
    </section>
  </div>;
}

function ComposerModelMenu({
  providers, providerId, modelName, models, efforts, effort, disabled, onProviderChange, onModelChange, onEffortChange,
}: {
  providers: ModelProvider[]; providerId: string; modelName: string; models: ProviderModel[]; efforts: string[];
  effort: string; disabled: boolean; onProviderChange: (value: string) => void; onModelChange: (value: string) => void;
  onEffortChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<'root' | 'provider' | 'model' | 'effort'>('root');
  const menu = useRef<HTMLDivElement>(null);
  const popover = useRef<HTMLElement>(null);
  const sidePanel = useRef<HTMLElement>(null);
  const [sidePosition, setSidePosition] = useState<{ left: number; top: number }>();
  useEffect(() => {
    if (disabled) { setOpen(false); setPanel('root'); }
  }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target) && !sidePanel.current?.contains(event.target)) close();
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, [open]);
  const close = () => { setOpen(false); setPanel('root'); };
  const selectProvider = (value: string) => { onProviderChange(value); close(); };
  const selectModel = (value: string) => { onModelChange(value); close(); };
  const selectEffort = (value: string) => { onEffortChange(value); close(); };
  const currentProvider = providers.find(provider => provider.id === providerId)?.name ?? '选择供应商';
  const choices = [effort, ...efforts].filter((value, index, all): value is string => Boolean(value) && all.indexOf(value) === index);
  const rootRow = (key: 'provider' | 'model' | 'effort', label: string, value: string, unavailable = false) => <button type="button" className="agent-model-picker-row" disabled={disabled || unavailable} onClick={() => setPanel(key)}><span>{label}</span><em>{value}</em><ChevronRight size={15}/></button>;
  const option = (key: string, label: string, selected: boolean, action: () => void) => <button type="button" key={key} className={`agent-model-picker-option${selected ? ' selected' : ''}`} onClick={action}><span>{label}</span>{selected && <Check size={15}/>}</button>;
  const panelTitle = panel === 'provider' ? '选择供应商' : panel === 'model' ? '选择模型' : '选择思考程度';
  useEffect(() => {
    if (!open || panel === 'root') { setSidePosition(undefined); return; }
    const place = () => {
      const anchor = popover.current?.getBoundingClientRect();
      if (!anchor) return;
      const width = Math.min(320, Math.max(220, window.innerWidth - anchor.right - 20));
      setSidePosition({ left: Math.min(anchor.right + 8, window.innerWidth - width - 12), top: Math.max(12, anchor.top) });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open, panel]);
  const pickerPanel = panel !== 'root' && sidePosition && <section ref={sidePanel} className="agent-model-picker-side-panel agent-model-picker-side-panel-portal" aria-label={panelTitle} style={sidePosition}>
    <header><b>{panelTitle}</b></header>
    <div className="agent-model-picker-options">
      {panel === 'provider' && providers.map(provider => option(provider.id, provider.name, provider.id === providerId, () => selectProvider(provider.id)))}
      {panel === 'model' && models.map(model => option(model.model_name, model.model_name, model.model_name === modelName, () => selectModel(model.model_name)))}
      {panel === 'effort' && choices.map(value => option(value, reasoningEffortLabel(value), value === effort, () => selectEffort(value)))}
    </div>
  </section>;
  return <div ref={menu} className="agent-composer-model-menu" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); close(); } }}>
    <button type="button" className="agent-composer-model-trigger" aria-label="打开模型与推理设置" aria-expanded={open} disabled={disabled} onClick={() => { setOpen(current => !current); setPanel('root'); }}><span className="agent-composer-model-summary"><span>{modelName || '选择模型'}</span>{effort && <em>{reasoningEffortLabel(effort)}</em>}</span><ChevronDown size={14}/></button>
    {open && <div className={`agent-composer-model-flyout${panel === 'root' ? '' : ' has-side-panel'}`}>
      <section ref={popover} className="agent-composer-model-popover" aria-label="模型与推理设置">
        {rootRow('provider', '供应商', currentProvider)}
        {rootRow('model', '模型', modelName || '选择模型', !providerId)}
        {efforts.length > 0 && rootRow('effort', '思考程度', effort ? reasoningEffortLabel(effort) : '默认', !providerId)}
        <p className="agent-model-picker-note">设置仅作用于当前会话。</p>
      </section>
    </div>}
    {pickerPanel && createPortal(pickerPanel, document.body)}
  </div>;
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

function exactCount(value: number): string {
  return value.toLocaleString('en-US');
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

function conversationName(conversation: AgentConversation) {
  return conversation.display_title || '新会话';
}

function pendingConversationName(message: QueuedMessage | undefined) {
  const firstLine = message?.content.trim().split(/\r?\n/, 1)[0]?.trim();
  if (!firstLine) return message?.items.length ? '附件会话' : '新会话';
  return firstLine.length > 36 ? `${firstLine.slice(0, 36)}…` : firstLine;
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

const USER_SOURCE_URL_PATTERN = /\b(?:https?:\/\/|www\.)[^\s<>"'`()[\]{}]+/gi;

function sourceUrl(value: string): string | undefined {
  const trimmed = value.replace(/[.,;:!?]+$/, '');
  const href = /^www\./i.test(trimmed) ? `http://${trimmed}` : trimmed;
  try {
    const parsed = new URL(href);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

function userProvidedSources(events: OpenHandsConversationEvent[]): ConversationSource[] {
  const sources = new Map<string, ConversationSource>();
  for (const event of events) {
    const isUserMessage = event.event_type === 'MESSAGE' && ['user', 'human'].includes(String(event.payload.source ?? '').toLowerCase());
    if (!isUserMessage) continue;
    for (const attachment of event.payload.attachments ?? []) {
      const kind = attachment.mime_type.startsWith('image/') ? 'image' : 'file';
      sources.set(`attachment:${attachment.path}`, { id: `attachment:${attachment.path}`, kind, label: attachment.filename, attachment });
    }
    for (const match of String(event.payload.content ?? '').matchAll(USER_SOURCE_URL_PATTERN)) {
      const url = sourceUrl(match[0]);
      if (!url || sources.has(`url:${url}`)) continue;
      sources.set(`url:${url}`, { id: `url:${url}`, kind: 'url', label: url.replace(/^https?:\/\//i, ''), url });
    }
  }
  return [...sources.values()];
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

function latestUnfinishedUserEventId(events: OpenHandsConversationEvent[]): string | undefined {
  const userEvents = events.filter(event => event.event_type === 'MESSAGE'
    && ['user', 'human'].includes(String(event.payload.source ?? '').toLowerCase()));
  return [...userEvents].reverse().find(event => !hasFinishedTurn(events, event.id))?.id;
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
  const { terminalUrl } = useAgentSessionGateway();
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
    let removeForcedSelectionListeners: (() => void) | undefined;
    const terminalCellForMouseEvent = (event: MouseEvent) => {
      const screen = element.querySelector<HTMLElement>('.xterm-screen');
      if (!screen) return undefined;
      const bounds = screen.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return undefined;
      const column = Math.max(0, Math.min(terminal.cols - 1, Math.floor((event.clientX - bounds.left) * terminal.cols / bounds.width)));
      const viewportRow = Math.max(0, Math.min(terminal.rows - 1, Math.floor((event.clientY - bounds.top) * terminal.rows / bounds.height)));
      return { column, row: terminal.buffer.active.viewportY + viewportRow };
    };
    const forceTextSelection = (event: MouseEvent) => {
      // tmux enables xterm mouse reporting for its native scroll/copy mode.
      // xterm then disables its selection service and forwards mouseup to the
      // PTY, which clears a just-dragged selection. Own a plain left-drag
      // through xterm's public Buffer/selection APIs so the PTY never sees it.
      if (event.button !== 0 || event.shiftKey || terminal.modes.mouseTrackingMode === 'none') return;
      const start = terminalCellForMouseEvent(event);
      if (!start) return;
      // The xterm listener is attached after this capture listener. Mark this
      // pointer sequence as handled before its mouse transport can forward a
      // press/release into tmux and clear the completed selection.
      event.preventDefault();
      event.stopImmediatePropagation();
      const document = element.ownerDocument;
      let selecting = false;
      const updateSelection = (current: MouseEvent) => {
        const end = terminalCellForMouseEvent(current);
        if (!end) return;
        const startOffset = start.row * terminal.cols + start.column;
        const endOffset = end.row * terminal.cols + end.column;
        const first = startOffset <= endOffset ? start : end;
        terminal.select(first.column, first.row, Math.abs(endOffset - startOffset));
      };
      const removeListeners = () => {
        document.removeEventListener('mousemove', moveSelection, true);
        document.removeEventListener('mouseup', finishSelection, true);
        removeForcedSelectionListeners = undefined;
      };
      const moveSelection = (current: MouseEvent) => {
        current.preventDefault();
        current.stopImmediatePropagation();
        if (!selecting && Math.hypot(current.clientX - event.clientX, current.clientY - event.clientY) > 2) selecting = true;
        if (selecting) updateSelection(current);
      };
      const finishSelection = (current: MouseEvent) => {
        current.preventDefault();
        current.stopImmediatePropagation();
        if (selecting) updateSelection(current);
        else terminal.focus();
        removeListeners();
      };
      removeForcedSelectionListeners?.();
      removeForcedSelectionListeners = removeListeners;
      document.addEventListener('mousemove', moveSelection, true);
      document.addEventListener('mouseup', finishSelection, true);
    };
    const terminalScreen = element.querySelector<HTMLElement>('.xterm-screen');
    terminalScreen?.addEventListener('mousedown', forceTextSelection, { capture: true });
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
      const current = new WebSocket(terminalUrl(workspaceId, terminal.rows, terminal.cols, { terminalInstanceId, bindingId, workDirectoryId }));
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
    return () => { disposed = true; if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer); if (resizeFrame !== undefined) window.cancelAnimationFrame(resizeFrame); if (remoteResizeTimer !== undefined) window.clearTimeout(remoteResizeTimer); observer.disconnect(); removeForcedSelectionListeners?.(); terminalScreen?.removeEventListener('mousedown', forceTextSelection, true); input.dispose(); socket?.close(1000); terminal.dispose(); };
  }, [bindingId, terminalInstanceId, terminalUrl, workDirectoryId, workingDirectory, workspaceId]);

  return <section className="agent-workspace-terminal"><header><span className={`terminal-dot ${state}`}/><span>{detail}</span></header><div ref={host} aria-label="Agent 工作区终端"/></section>;
}

function isTextPreviewable(path: string, mimeType = ''): boolean {
  return mimeType.startsWith('text/')
    || /^(?:application\/(?:json|xml|javascript|sql)|text\/(?:markdown|x-[^/]+))$/i.test(mimeType)
    || /\.(?:md|mdx|txt|json|ya?ml|toml|ini|conf|xml|html?|css|scss|less|tsx?|jsx?|py|java|kt|go|rs|rb|php|sh|zsh|sql|graphql|vue|svelte)$/i.test(path);
}

function relativeWorkspacePath(path: string, root: string): string {
  return path === root ? '.' : path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
}

type WorkspaceEntry = { path: string; kind: 'file' | 'directory'; size: number; displayName?: string };
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
        ? { ...entry, name: entry.displayName ?? parts[index], children: [] }
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
  onCreated: (directory: AgentSessionWorkDirectory) => void;
}) {
  const { api } = useAgentSessionGateway();
  const host = useAgentSessionHost();
  const [displayName, setDisplayName] = useState('');
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  useEscapeClose(() => { if (!create.isPending) onClose(); });
  const detailsQuery = useQuery({
    queryKey: sessionQueryKey(host, 'work-directory-creation', workspaceId),
    queryFn: () => api.workspaceDetails(workspaceId),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const create = useMutation({
    mutationFn: () => api.createWorkDirectory(workspaceId, displayName.trim(), selectedPaths),
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

function SshAccessGuide({
  host, port, path, onClose,
}: { host: string; port: number; path: string; onClose: () => void }) {
  const [copied, setCopied] = useState<string>();
  useEscapeClose(onClose);
  const copy = (field: string, value: string) => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(field);
      window.setTimeout(() => setCopied(current => current === field ? undefined : current), 1500);
    });
  };
  const fields = [
    ['host', '主机 / IP', host],
    ['port', '端口', String(port)],
    ['path', '当前会话工作目录', path],
  ] as const;

  return <div className="agent-ssh-access-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="agent-ssh-access-guide" role="dialog" aria-modal="true" aria-labelledby="agent-ssh-access-title">
      <header><div><span className="eyebrow">SSH ACCESS</span><h2 id="agent-ssh-access-title">SSH 接入说明</h2><p>以下信息只对应当前会话的持久工作目录。</p></div><button type="button" aria-label="关闭 SSH 接入说明" onClick={onClose}><X size={18}/></button></header>
      <div className="agent-ssh-access-fields">{fields.map(([field, label, value]) => <div key={field}><span>{label}</span><code title={value}>{value}</code><button type="button" aria-label={`复制${label}`} onClick={() => copy(field, value)}><Copy size={12}/>{copied === field ? '已复制' : '复制'}</button></div>)}</div>
      <ol>
        <li>在你的设备上生成并保管自己的 SSH 私钥；只向 SSH 管理员提交对应的 <code>.pub</code> 公钥。</li>
        <li>在 JetBrains Gateway 或 SSH 客户端中填写上方主机和端口，使用已授权的个人 SSH 用户名与私钥认证。</li>
        <li>认证成功后，只打开上方“当前会话工作目录”。FlowWeave 不接收、保存或展示私钥。</li>
      </ol>
      <aside className="agent-ssh-access-warning"><ShieldAlert size={17}/><div><b>当前尚未具备会话级 SSH 隔离</b><p>现在的 <code>flowweave</code> 是共享的宿主机 SSH 账户；它能访问 Unix 文件权限允许的目录，而不只是此会话目录。因此它只能用于本机开发或可信单用户环境，不能作为多用户生产访问方案。</p></div></aside>
      <footer><button type="button" className="primary" onClick={onClose}>我已了解</button></footer>
    </section>
  </div>;
}

function clampWorkspaceToolWidth(value: number): number {
  if (window.innerWidth <= 1100) return Math.max(300, Math.min(720, value));
  const viewportMaximum = Math.max(300, window.innerWidth - 700);
  return Math.max(300, Math.min(720, viewportMaximum, value));
}

function readWorkspaceToolState(storageKey: string): Record<string, WorkspaceToolScopeState> {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(storageKey) ?? '{}') as Record<string, WorkspaceToolScopeState>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function WorkspaceDrawer({
  open, onOpen, onClose, workspaceId, scopeKey, migrateFromScopeKey, bindingId, workDirectoryId, attachments, sources, attachmentRequest, runtimeAvailable,
}: {
  open: boolean; onOpen: () => void; onClose: () => void; workspaceId: string; scopeKey: string; migrateFromScopeKey?: string; bindingId?: string; workDirectoryId?: string; attachments: AgentAttachment[]; sources: ConversationSource[]; attachmentRequest?: { key: string; attachment: AgentAttachment }; runtimeAvailable: boolean;
}) {
  const { api, fileUrl } = useAgentSessionGateway();
  const host = useAgentSessionHost();
  const toolsStorageKey = host.workspaceToolsStorageKey(workspaceId);
  const [scopeStates, setScopeStates] = useState<Record<string, WorkspaceToolScopeState>>(() => readWorkspaceToolState(toolsStorageKey));
  const [panelWidth, setPanelWidth] = useState(() => {
    const stored = Number(localStorage.getItem('flowweave:workspace-tool-width'));
    return clampWorkspaceToolWidth(Number.isFinite(stored) ? stored : 400);
  });
  const [fileTreeWidth, setFileTreeWidth] = useState(() => {
    const stored = Number(localStorage.getItem('flowweave:workspace-file-tree-width'));
    return Math.min(520, Math.max(180, Number.isFinite(stored) ? stored : 300));
  });
  const [panelError, setPanelError] = useState('');
  const [closingTerminalId, setClosingTerminalId] = useState<string>();
  const [pendingTerminalClose, setPendingTerminalClose] = useState<Extract<WorkspaceToolTab, { kind: 'terminal' }>>();
  const [fullScreen, setFullScreen] = useState(false);
  const [sshAccessOpen, setSshAccessOpen] = useState(false);
  useEscapeClose(() => {
    if (pendingTerminalClose && !closingTerminalId) setPendingTerminalClose(undefined);
  }, Boolean(pendingTerminalClose) && !closingTerminalId);
  const handledAttachmentRequestKey = useRef<string | undefined>(undefined);
  const scopeState = scopeStates[scopeKey] ?? { tabs: [] };
  const updateScope = useCallback((updater: (current: WorkspaceToolScopeState) => WorkspaceToolScopeState) => {
    setScopeStates(current => ({ ...current, [scopeKey]: updater(current[scopeKey] ?? { tabs: [] }) }));
  }, [scopeKey]);
  useEffect(() => {
    sessionStorage.setItem(toolsStorageKey, JSON.stringify(scopeStates));
  }, [scopeStates, toolsStorageKey]);
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
    localStorage.setItem('flowweave:workspace-file-tree-width', String(fileTreeWidth));
  }, [fileTreeWidth]);
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
    queryKey: sessionQueryKey(host, 'workspace-details', workspaceId, bindingId, workDirectoryId),
    queryFn: () => api.workspaceDetails(workspaceId, { bindingId, workDirectoryId }),
    enabled: true,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const details = detailsQuery.data;
  const selectedFile = scopeState.selectedFile;
  const selectedAttachment = attachments.find(item => item.path === selectedFile);
  const selectedMimeType = selectedAttachment?.mime_type ?? '';
  const textPreviewable = Boolean(selectedFile && isTextPreviewable(selectedFile, selectedMimeType));
  const previewQuery = useQuery({
    queryKey: sessionQueryKey(host, 'file-preview', workspaceId, bindingId, selectedFile),
    queryFn: ({ signal }) => api.filePreview(workspaceId, selectedFile!, { bindingId, workDirectoryId }, signal),
    enabled: Boolean(open && scopeState.activeTabId === 'files' && textPreviewable),
    retry: false,
  });
  const visibleFiles = useMemo(() => {
    const files = new Map<string, WorkspaceEntry>((details?.files ?? []).map(file => [file.path, file]));
    for (const attachment of attachments) {
      files.set(attachment.path, { path: attachment.path, kind: 'file', size: attachment.byte_size, displayName: attachment.filename });
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
    // `onOpen` is supplied by the page and may change identity on a render.
    // Consume each request once so closing the file tab cannot immediately
    // reopen it from this effect.
    if (!attachmentRequest || handledAttachmentRequestKey.current === attachmentRequest.key) return;
    handledAttachmentRequestKey.current = attachmentRequest.key;
    openFiles(attachmentRequest.attachment.path);
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
        await api.closeTerminal(workspaceId, tab.terminalInstanceId);
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
  const startFileTreeResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (window.innerWidth <= 680) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = fileTreeWidth;
    const maximum = () => Math.min(520, Math.max(180, window.innerWidth * 0.55));
    const move = (moveEvent: PointerEvent) => {
      setFileTreeWidth(Math.min(maximum(), Math.max(180, startWidth + moveEvent.clientX - startX)));
    };
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
  const selectedFileUrl = selectedFile
    ? fileUrl(workspaceId, selectedFile, { bindingId, workDirectoryId, download: false })
    : '';
  const canPreviewImage = Boolean(selectedFile && (selectedMimeType.startsWith('image/') || /\.(?:avif|gif|jpe?g|png|svg|webp)$/i.test(selectedFile)));
  const canPreviewPdf = Boolean(selectedFile && (selectedMimeType === 'application/pdf' || /\.pdf$/i.test(selectedFile)));
  const sshRemoteReady = Boolean(
    details?.ide.gateway.supported
    && details.ide.gateway.host
    && details.ide.gateway.port
    && details.ide.gateway.path,
  );
  const summary = details && <section className="agent-workspace-overview">
    <article><FolderOpen size={16}/><div><small>当前工作区</small><b>{details.scope.display_name}</b><code>{details.working_directory}</code></div></article>
    <article><MonitorCog size={16}/><div><small>运行环境</small><b>{details.runtime.container_id || (details.runtime.write_available ? '运行中' : '恢复中')}</b><p>所有会话共用此 Workspace Runtime；每个终端保留独立会话。</p></div></article>
    <article><GitBranch size={16}/><div><small>Git 仓库</small>{details.repositories.length ? details.repositories.map(repository => <p key={repository.path}><b>{relativeWorkspacePath(repository.path, details.root)}</b>{repository.branch && <span>{repository.branch}</span>}{repository.head && <em>{repository.head.slice(0, 12)}</em>}{repository.remote && <code>{repository.remote}</code>}</p>) : <p>当前目录未检测到 Git 仓库。</p>}</div></article>
    <article className="agent-workspace-ide"><MonitorCog size={16}/><div><small>IDEA / Gateway</small><b>{details.ide.gateway.status}</b>{sshRemoteReady ? <button type="button" className="agent-ssh-access-trigger" onClick={() => setSshAccessOpen(true)}>SSH 接入说明<ChevronRight size={13}/></button> : <code>{details.ide.workspace_path}</code>}<p>{details.ide.gateway.note}</p></div></article>
    <article className="agent-workspace-sources"><Link2 size={16}/><div><small>来源</small>{sources.length ? <div className="agent-workspace-source-list">{sources.map(source => source.kind === 'url' ? <a key={source.id} href={source.url} target="_blank" rel="noopener noreferrer" title={`打开链接：${source.label}`}><Link2 size={12}/><span><b>{source.label}</b><em>链接</em></span></a> : <button type="button" key={source.id} title={`在工作区预览：${source.label}`} onClick={() => source.attachment && selectFile(source.attachment.path)}>{source.kind === 'image' ? <ImageIcon size={12}/> : <FileText size={12}/>}<span><b>{source.label}</b><em>{source.pending ? '待发送' : source.kind === 'image' ? '图片' : '文件'}</em></span></button>)}</div> : <p>用户输入的链接、文件和图片会集中显示在这里。</p>}</div></article>
  </section>;
  return <><aside className={`agent-workspace-drawer ${open ? 'tools-open' : 'summary-open'}${fullScreen ? ' fullscreen' : ''}`} style={{ width: fullScreen ? undefined : open ? panelWidth : 272 }} role={fullScreen ? 'dialog' : undefined} aria-modal={fullScreen || undefined} aria-label={fullScreen ? '全屏工作区工具' : undefined}>
    <div className="agent-workspace-resizer" role="separator" aria-label="调整工作区工具宽度" aria-orientation="vertical" onPointerDown={startResize}/>
    <section className={`agent-workspace-summary ${open ? 'panel-hidden' : ''}`}>
      <header><div><span className="eyebrow">WORKSPACE</span><b>环境信息</b></div><button type="button" aria-label="打开工作区工具" onClick={onOpen}><PanelRightOpen size={16}/></button></header>
      <div className="agent-workspace-quick-actions"><button type="button" onClick={() => openFiles()}><FileCode2 size={14}/>文件</button><button type="button" disabled={!runtimeAvailable} onClick={openTerminal}><Plus size={14}/>新终端</button></div>
      {loadingOrError || summary}
    </section>
    <section className={`agent-workspace-tool-shell ${open ? '' : 'panel-hidden'}`}>
      <header><nav className="agent-workspace-tabs" aria-label="工作区工具页签">{scopeState.tabs.map(tab => <div key={tab.id} className={scopeState.activeTabId === tab.id ? 'active' : ''}><button type="button" className="agent-workspace-tab-select" onClick={() => updateScope(current => ({ ...current, activeTabId: tab.id }))}><span>{tab.kind === 'files' ? '文件' : details?.runtime.container_id || (details?.runtime.write_available ? '终端' : '连接中…')}</span></button><button type="button" className="agent-workspace-tab-close" aria-label={`关闭${tab.kind === 'files' ? '文件' : `终端 ${details?.runtime.container_id || ''}`}页签`} disabled={tab.kind === 'terminal' && closingTerminalId === tab.terminalInstanceId} onClick={() => { if (tab.kind !== 'terminal' || closingTerminalId !== tab.terminalInstanceId) requestCloseTab(tab); }}><X size={12}/></button></div>)}</nav><div className="agent-workspace-tool-actions"><details><summary aria-label="新增工作区工具"><Plus size={15}/></summary><div><button type="button" onClick={event => { openFiles(); event.currentTarget.closest('details')?.removeAttribute('open'); }}><FileCode2 size={13}/>文件</button><button type="button" disabled={!runtimeAvailable} onClick={event => { openTerminal(); event.currentTarget.closest('details')?.removeAttribute('open'); }}><Plus size={13}/>终端</button></div></details><button type="button" aria-label={fullScreen ? '退出全屏' : '全屏查看工作区工具'} title={fullScreen ? '退出全屏（Esc）' : '全屏查看'} onClick={() => setFullScreen(current => !current)}>{fullScreen ? <Minimize2 size={16}/> : <Maximize2 size={16}/>}</button><button type="button" aria-label="关闭工作区工具" onClick={() => { setFullScreen(false); onClose(); }}><X size={16}/></button></div></header>
      <div className="agent-workspace-tool-body">
        {panelError && <p className="agent-workspace-panel-error">{panelError}</p>}
        {loadingOrError || (!scopeState.tabs.length ? <div className="agent-drawer-empty"><b>选择工作区工具</b><span>文件仅打开一个页签；终端可按需打开多个独立实例。</span><div><button type="button" className="secondary" onClick={() => openFiles()}>打开文件</button><button type="button" className="secondary" disabled={!runtimeAvailable} onClick={openTerminal}>新建终端</button></div></div> : details && <div className="agent-workspace-tool-content">
          {scopeState.tabs.some(tab => tab.kind === 'files') && <section className={`agent-workspace-files ${scopeState.activeTabId === 'files' ? 'active' : ''}`} style={{ '--file-tree-width': `${fileTreeWidth}px` } as CSSProperties}>
            <div className="agent-file-tree-pane">
              <WorkspaceFileTree entries={visibleFiles} root={details.root} selectedFile={selectedFile} onSelect={path => updateScope(current => ({ ...current, selectedFile: path }))}/>
            </div>
            <div className="agent-file-tree-resizer" role="separator" aria-label="调整文件目录宽度" aria-orientation="vertical" onPointerDown={startFileTreeResize}/>
            <div className="agent-file-preview">{selectedFile ? <>
              <header><span title={selectedFile}>{selectedAttachment?.filename || relativeWorkspacePath(selectedFile, details.root)}</span><a href={fileUrl(workspaceId, selectedFile, { bindingId, workDirectoryId, download: true })}><Download size={13}/>下载</a></header>
              {canPreviewImage ? <img className="agent-file-media-preview" src={selectedAttachment?.image_data_url || selectedFileUrl} alt={selectedAttachment?.filename || '附件预览'}/> : canPreviewPdf ? <iframe className="agent-file-media-preview" title={selectedAttachment?.filename || 'PDF 预览'} src={selectedFileUrl}/> : textPreviewable ? previewQuery.isLoading ? <p>正在读取文件…</p> : previewQuery.isError ? <p>文件预览不可用，请下载后查看。</p> : <pre>{previewQuery.data}</pre> : <p>此文件不提供浏览器预览，请下载后查看。</p>}
            </> : <p>选择一个文件以预览或下载。</p>}</div>
          </section>}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'terminal' }> => tab.kind === 'terminal').map(tab => <div key={tab.id} className={`agent-terminal-tab-panel ${scopeState.activeTabId === tab.id ? 'active' : ''}`}>{runtimeAvailable ? <WorkspaceTerminal workspaceId={workspaceId} terminalInstanceId={tab.terminalInstanceId} bindingId={bindingId} workDirectoryId={workDirectoryId} workingDirectory={details.working_directory}/> : <div className="agent-drawer-empty"><LoaderCircle className="agent-drawer-spinner" size={20}/><b>终端正在恢复</b><span>文件仍可使用；运行环境恢复后终端会自动可用。</span></div>}</div>)}
        </div>)}
      </div>
    </section>
  </aside>{sshAccessOpen && sshRemoteReady && <SshAccessGuide host={details!.ide.gateway.host!} port={details!.ide.gateway.port!} path={details!.ide.gateway.path!} onClose={() => setSshAccessOpen(false)}/>} {pendingTerminalClose && <div className="agent-terminal-close-backdrop" onPointerDown={event => { if (event.target === event.currentTarget && !closingTerminalId) setPendingTerminalClose(undefined); }}><section role="dialog" aria-modal="true" aria-labelledby="agent-terminal-close-title" className="agent-terminal-close-dialog"><header><span className="eyebrow">TERMINAL</span><h2 id="agent-terminal-close-title">关闭此终端？</h2></header><p>关闭后会停止该终端中正在执行的命令，并清除这一个终端会话；其他终端和当前会话不会受影响。</p><footer><button type="button" className="secondary" disabled={Boolean(closingTerminalId)} onClick={() => setPendingTerminalClose(undefined)}>取消</button><button type="button" className="danger" autoFocus disabled={Boolean(closingTerminalId)} onClick={() => { const tab = pendingTerminalClose; setPendingTerminalClose(undefined); void closeTab(tab); }}>{closingTerminalId ? '正在关闭…' : '关闭终端'}</button></footer></section></div>}</>;
}

export interface AgentSessionWorkbenchProps {
  onNavigate: (path: string, replace?: boolean) => void;
  /** Provided by scoped hosts whose conversation page has a product parent. */
  onReturnToSource?: () => void;
  autoOpenDraft?: boolean;
  hideDraftTitle?: boolean;
  gateway?: AgentSessionGateway;
  host?: AgentSessionHost;
}

/**
 * The sole complete Agent-session surface. Hosts provide navigation and an
 * existing transport gateway; conversation UI and state live here.
 */
export function AgentSessionWorkbench({
  gateway = agentWorkspaceSessionGateway,
  host = agentWorkspaceSessionHost,
  ...props
}: AgentSessionWorkbenchProps) {
  return <AgentSessionGatewayContext.Provider value={gateway}>
    <AgentSessionHostContext.Provider value={host}>
      <AgentSessionWorkbenchContent {...props}/>
    </AgentSessionHostContext.Provider>
  </AgentSessionGatewayContext.Provider>;
}

function AgentSessionWorkbenchContent({ onNavigate, onReturnToSource, autoOpenDraft = false, hideDraftTitle = false }: Omit<AgentSessionWorkbenchProps, 'gateway' | 'host'>) {
  const { api, subscribe, features } = useAgentSessionGateway();
  const host = useAgentSessionHost();
  const queryClient = useQueryClient();
  const initialBootstrapRecovery = useRef<BootstrapRecovery | undefined>(readBootstrapRecovery(host.bootstrapRecoveryStorageKey));
  const [draft, setDraft] = useState(() => initialBootstrapRecovery.current?.message.content ?? '');
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('disabled');
  const [liveText, setLiveText] = useState('');
  const [liveEvents, setLiveEvents] = useState<OpenHandsConversationEvent[]>([]);
  const [optimisticBootstrapTurn, setOptimisticBootstrapTurn] = useState<OptimisticBootstrapTurn>();
  const [pendingBootstrap, setPendingBootstrap] = useState<{ draft: ConversationDraft; message: QueuedMessage } | undefined>(() => {
    const recovery = initialBootstrapRecovery.current;
    return recovery ? { draft: recovery.draft, message: recovery.message } : undefined;
  });
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
  const [newConversationProviderId, setNewConversationProviderId] = useState(() => initialBootstrapRecovery.current?.providerId ?? '');
  const [newConversationModelName, setNewConversationModelName] = useState(() => initialBootstrapRecovery.current?.modelName ?? '');
  const [newConversationReasoningEffort, setNewConversationReasoningEffort] = useState<string | null>(() => initialBootstrapRecovery.current?.reasoningEffort ?? null);
  const [conversationProviderId, setConversationProviderId] = useState('');
  const [conversationModelName, setConversationModelName] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AgentAttachment[]>(() => initialBootstrapRecovery.current?.message.items ?? []);
  const [attachmentRequest, setAttachmentRequest] = useState<{ key: string; attachment: AgentAttachment }>();
  const [operationError, setOperationError] = useState<Error>();
  const [condensationStatus, setCondensationStatus] = useState<{ bindingId: string; state: 'running' | 'failed'; startedAt: number; message?: string }>();
  const [condensationConfirmationOpen, setCondensationConfirmationOpen] = useState(false);
  const [pendingCreatedId, setPendingCreatedId] = useState<string>();
  const [pendingMigratedSend, setPendingMigratedSend] = useState<BoundQueuedMessage>();
  const [conversationDraft, setConversationDraft] = useState<ConversationDraft | undefined>(() => initialBootstrapRecovery.current?.draft);
  const [bootstrapRecovery, setBootstrapRecovery] = useState<BootstrapRecovery | undefined>(() => initialBootstrapRecovery.current);
  const [workspaceScopeMigration, setWorkspaceScopeMigration] = useState<string>();
  const [workDirectoryCreatorOpen, setWorkDirectoryCreatorOpen] = useState(false);
  const [capabilityManagerOpen, setCapabilityManagerOpen] = useState(false);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const pendingLiveText = useRef('');
  const liveTextFrame = useRef<number | undefined>(undefined);
  const pendingLiveEvents = useRef<OpenHandsConversationEvent[]>([]);
  const liveEventsFrame = useRef<number | undefined>(undefined);
  const bootstrapTransitionScope = useRef<string | undefined>(undefined);
  const selectedBindingId = host.bindingIdFromPathname(withoutDeploymentBase(window.location.pathname));
  // A FlowRun may briefly report a recoverable 409 while its Attempt and
  // Runtime records are being published.  Do not leave the node workbench
  // permanently stuck on the first transient response.
  const workspaceQuery = useQuery({
    queryKey: sessionQueryKey(host, 'default-host'),
    queryFn: api.defaultHost,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500 && error.status !== 409) && count < 3,
    retryDelay: attempt => Math.min(1000 * 2 ** attempt, 5000),
    refetchOnWindowFocus: true,
  });
  const workspace = workspaceQuery.data;
  const runtimeQuery = useQuery({ queryKey: sessionQueryKey(host, 'runtime', workspace?.id), queryFn: () => api.runtime(workspace!.id), enabled: Boolean(workspace), refetchInterval: query => query.state.data?.state === 'RECOVERING' ? 5000 : false });
  const conversationsQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversations', workspace?.id),
    queryFn: () => api.conversations(workspace!.id),
    enabled: Boolean(workspace),
    // Title generation is an isolated one-shot metadata task. Poll only while
    // at least one visible binding is pending so the generated title replaces
    // its first-message fallback without requiring a page refresh.
    refetchInterval: query => query.state.data?.some(item => item.title_state === 'PENDING')
      ? 1000
      : false,
  });
  const workDirectoriesQuery = useQuery({ queryKey: sessionQueryKey(host, 'work-directories', workspace?.id), queryFn: () => api.workDirectories(workspace!.id), enabled: Boolean(workspace && features.workDirectories) });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers, enabled: Boolean(workspace && features.modelSelection) });
  const capabilityCatalogQuery = useQuery({ queryKey: sessionQueryKey(host, 'capability-catalog'), queryFn: api.capabilities, enabled: Boolean(workspace && features.capabilities) });
  const conversations = useMemo(() => conversationsQuery.data ?? [], [conversationsQuery.data]);
  const selected = useMemo(() => conversations.find(item => item.id === selectedBindingId), [conversations, selectedBindingId]);
  const composerCapabilityReferences = useMemo(() => {
    if (selected?.capabilities) return selected.capabilities;
    if (!conversationDraft) return [];
    const catalog = new Map((capabilityCatalogQuery.data ?? []).map(item => [item.id, item]));
    return (conversationDraft.capabilityVersionIds ?? []).flatMap(id => {
      const item = catalog.get(id);
      return item ? [{ id: item.id, capability_type: item.capability_type as AgentSessionCapability['capability_type'], capability_key: item.capability_key, digest: item.content_hash }] : [];
    });
  }, [capabilityCatalogQuery.data, conversationDraft, selected?.capabilities]);
  const composerSuggestions = useMemo(() => {
    const catalog = new Map((capabilityCatalogQuery.data ?? []).map(item => [item.id, item]));
    const seen = new Set<string>();
    const add = (item: ComposerSuggestion) => {
      if (!seen.has(item.id)) { seen.add(item.id); return item; }
      return undefined;
    };
    const capabilities = composerCapabilityReferences.flatMap(reference => {
      const capability = catalog.get(reference.id);
      const description = capability?.description || reference.capability_key;
      if (reference.capability_type === 'SKILL') {
        const item = add({ id: `skill:${reference.id}`, kind: 'SKILL', token: `$${reference.capability_key}`, label: reference.capability_key, detail: description });
        return item ? [item] : [];
      }
      if (reference.capability_type === 'MCP') {
        const item = add({ id: `mcp:${reference.id}`, kind: 'MCP', token: `使用 MCP「${reference.capability_key}」：`, label: reference.capability_key, detail: `${description} · 以自然语言说明要执行的操作` });
        return item ? [item] : [];
      }
      const contributions = capability?.document.contributions;
      const commands = contributions && typeof contributions === 'object'
        ? stringValues((contributions as Record<string, unknown>).commands)
        : [];
      const skills = contributions && typeof contributions === 'object'
        ? stringValues((contributions as Record<string, unknown>).skills)
        : [];
      return [
        ...commands.map(command => add({ id: `command:${reference.id}:${command}`, kind: 'COMMAND', token: `/${reference.capability_key}:${command}`, label: command, detail: `${reference.capability_key} 命令 · ${description}` })).filter((item): item is ComposerSuggestion => Boolean(item)),
        ...skills.map(skill => add({ id: `plugin-skill:${reference.id}:${skill}`, kind: 'SKILL', token: `$${skill}`, label: skill, detail: `${reference.capability_key} 提供的技能 · ${description}` })).filter((item): item is ComposerSuggestion => Boolean(item)),
      ];
    });
    const native: ComposerSuggestion[] = selected && (turnState === 'idle' || turnState === 'paused') ? [{
      id: 'native:condense',
      kind: 'NATIVE',
      token: '/condense',
      label: '压缩上下文',
      detail: '调用 OpenHands 原生 condenser，完成后保留正式 Condensation 事件',
      nativeAction: 'CONDENSE',
    }] : [];
    return [...native, ...capabilities];
  }, [capabilityCatalogQuery.data, composerCapabilityReferences, selected, turnState]);
  const composerScope = selected?.id ?? conversationDraft?.id;
  const activeComposerScope = useRef<string | undefined>(undefined);
  activeComposerScope.current = composerScope;
  const reportOperationError = useCallback((scope: string | undefined, error: Error) => {
    if (scope && activeComposerScope.current === scope) setOperationError(error);
  }, []);
  const clearBootstrapRecovery = useCallback(() => {
    setBootstrapRecovery(undefined);
    writeBootstrapRecovery(host.bootstrapRecoveryStorageKey, undefined);
  }, [host.bootstrapRecoveryStorageKey]);
  const connectedProviders = (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default));
  const runtime = runtimeQuery.data;
  const runtimeWritable = Boolean(workspace && runtime?.write_available);
  const canOpenConversation = runtimeWritable;
  const canBootstrap = Boolean(runtimeWritable && conversationDraft && (!features.modelSelection || (newConversationProviderId && newConversationModelName)));
  const isGenerating = turnState === 'running' || turnState === 'pausing' || turnState === 'resuming';
  const eventsQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-events', workspace?.id, selected?.id), queryFn: () => api.conversationEvents(workspace!.id, selected!.id), enabled: Boolean(workspace && selected), refetchInterval: isGenerating ? 1200 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const displayedEvents = useMemo(() => {
    const activeScope = selected?.id ?? conversationDraft?.id;
    const bootstrapEvent = optimisticBootstrapTurn && optimisticBootstrapTurn.scope === activeScope
      ? [optimisticBootstrapTurn.event]
      : [];
    return mergeConversationEvents(
      mergeConversationEvents(eventsQuery.data?.events ?? [], liveEvents),
      bootstrapEvent,
    )
      .filter(event => !hiddenEventIds.has(event.id));
  }, [conversationDraft?.id, eventsQuery.data?.events, hiddenEventIds, liveEvents, optimisticBootstrapTurn, selected?.id]);
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
  const drawerSources = useMemo(() => {
    const byId = new Map(userProvidedSources(displayedEvents).map(source => [source.id, source]));
    for (const attachment of attachments) {
      const id = `attachment:${attachment.path}`;
      if (byId.has(id)) continue;
      byId.set(id, {
        id,
        kind: attachment.mime_type.startsWith('image/') ? 'image' : 'file',
        label: attachment.filename,
        attachment,
        pending: true,
      });
    }
    return [...byId.values()];
  }, [attachments, displayedEvents]);
  const openAttachmentInDrawer = useCallback((attachment: AgentAttachment) => {
    setAttachmentRequest({ key: randomId(), attachment });
    setDrawerOpen(true);
  }, []);
  const inputReadinessQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-input-readiness', workspace?.id, selected?.id),
    queryFn: () => api.inputReadiness(workspace!.id, selected!.id),
    // This is the formal OpenHands execution-state read used to restore an
    // in-flight turn after a browser reload. It is not persisted by FlowWeave.
    enabled: Boolean(workspace && selected),
    refetchInterval: query => turnState === 'pausing' || queuedMessages.length > 0 || isGenerating || query.state.data?.ready === false ? 700 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const contextQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-context', workspace?.id, selected?.id),
    queryFn: () => api.conversationContext(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected), refetchInterval: isGenerating ? 2000 : false,
  });
  const compactionPolicyCurrent = contextQuery.data?.compaction_policy_current !== false;
  const canWrite = Boolean(runtimeWritable && selected);
  const canCompose = Boolean(canWrite || (runtimeWritable && conversationDraft));
  const confirmationQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace?.id, selected?.id),
    queryFn: () => api.pendingConfirmation(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected && runtime?.write_available && features.confirmations),
    refetchInterval: isGenerating ? 1200 : 2500,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const pendingConfirmation = confirmationQuery.data?.pending ? confirmationQuery.data : undefined;
  const refresh = useCallback(() => {
    if (!workspace) return;
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'runtime', workspace.id) });
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
    if (selected?.id) {
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-events', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-context', workspace.id, selected.id) });
    }
  }, [host, queryClient, selected?.id, workspace]);
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
  const appendLiveEvent = useCallback((event: OpenHandsConversationEvent) => {
    pendingLiveEvents.current.push(event);
    if (liveEventsFrame.current !== undefined) return;
    liveEventsFrame.current = window.requestAnimationFrame(() => {
      liveEventsFrame.current = undefined;
      const next = pendingLiveEvents.current;
      pendingLiveEvents.current = [];
      if (next.length) setLiveEvents(current => mergeConversationEvents(current, next));
    });
  }, []);
  useEffect(() => () => {
    if (liveTextFrame.current !== undefined) window.cancelAnimationFrame(liveTextFrame.current);
    if (liveEventsFrame.current !== undefined) window.cancelAnimationFrame(liveEventsFrame.current);
  }, []);
  const onStreamEvent = useCallback((event: { type: 'delta' | 'event' | 'message_complete'; content?: string; event?: OpenHandsConversationEvent }) => {
    if (event.type === 'delta' && event.content) appendLiveText(event.content);
    if (event.type === 'event' && event.event) {
      appendLiveEvent(event.event);
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
  }, [appendLiveEvent, appendLiveText, clearLiveText, refresh]);

  useEffect(() => {
    if (!conversationDraft && !selectedBindingId && conversations.length) onNavigate(host.conversationPath(conversations[0].id), true);
    if (selectedBindingId && conversations.length && !selected && pendingCreatedId !== selectedBindingId && !conversationsQuery.isFetching) onNavigate(host.rootPath, true);
  }, [conversationDraft, conversations, conversationsQuery.isFetching, host, onNavigate, pendingCreatedId, selected, selectedBindingId]);
  useEffect(() => { if (!workspace || !selected || !runtime?.write_available) { setStreamStatus('disabled'); return; } return subscribe(workspace.id, selected.id, onStreamEvent, setStreamStatus); }, [onStreamEvent, runtime?.write_available, selected, subscribe, workspace]);
  useEffect(() => { if (selected?.id === pendingCreatedId) setPendingCreatedId(undefined); }, [pendingCreatedId, selected?.id]);
  useEffect(() => {
    if (bootstrapTransitionScope.current === composerScope) {
      bootstrapTransitionScope.current = undefined;
      return;
    }
    setEditing(false); clearLiveText(); pendingLiveEvents.current = []; if (liveEventsFrame.current !== undefined) window.cancelAnimationFrame(liveEventsFrame.current); liveEventsFrame.current = undefined; setLiveEvents([]); setHiddenEventIds(new Set()); setActiveTurnEventId(undefined); setRequestStartedAt(undefined); setConfirmationReason(''); setCondensationConfirmationOpen(false); setTurnState('idle'); setQueuedMessages([]); setPendingRewrite(undefined); setAttachments([]); setOperationError(undefined);
  }, [clearLiveText, composerScope]);
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
    if (!selected || inputReadinessQuery.data?.execution_status?.toLowerCase() !== 'paused') return;
    setTurnState(current => current === 'idle' ? 'paused' : current);
  }, [inputReadinessQuery.data?.execution_status, selected]);
  useEffect(() => {
    if (!selected || inputReadinessQuery.data?.ready !== false) return;
    const userEventId = latestUnfinishedUserEventId(displayedEvents);
    if (!userEventId) return;
    setActiveTurnEventId(current => current ?? userEventId);
    setTurnState(current => current === 'idle' ? 'running' : current);
  }, [displayedEvents, inputReadinessQuery.data?.ready, selected]);
  useEffect(() => {
    if ((turnState === 'running' || turnState === 'resuming') && activeTurnEventId && hasFinishedTurn(displayedEvents, activeTurnEventId)) {
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      setTurnState('idle');
      refresh();
    }
  }, [activeTurnEventId, clearLiveText, displayedEvents, refresh, turnState]);

  const bootstrap = useMutation({ mutationFn: (message: QueuedMessage) => api.bootstrapConversation(
    workspace!.id,
    conversationDraft!.id,
    newConversationProviderId,
    newConversationModelName,
    newConversationReasoningEffort,
    message.content,
    message.items,
    conversationDraft?.workDirectoryId,
    conversationDraft?.capabilityVersionIds ?? [],
    conversationDraft!.id,
  ), onSuccess: (value, message) => {
    if (!workspace) return;
    const conversation = value.conversation;
    setWorkspaceScopeMigration(message.scope);
    setPendingCreatedId(conversation.id);
    bootstrapTransitionScope.current = conversation.id;
    setActiveTurnEventId(value.cursor ?? undefined);
    setOptimisticBootstrapTurn(current => current?.scope === message.scope ? undefined : current);
    setPendingBootstrap(undefined);
    setConversationDraft(undefined);
    clearBootstrapRecovery();
    setOperationError(undefined);
    queryClient.setQueryData<AgentConversation[]>(sessionQueryKey(host, 'conversations', workspace.id), current => [conversation, ...(current ?? []).filter(item => item.id !== conversation.id)]);
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
    onNavigate(host.conversationPath(conversation.id));
  }, onError: (error, message) => {
    if (isBootstrapAmbiguous(error)) {
      // React Query can retain a mutation observer from the render that
      // initiated the request. The command scope is the draft UUID, so it is
      // sufficient to reconstruct the retry handle even if that observer's
      // closure predates the draft state update.
      const draftForRecovery = conversationDraft ?? {
        id: message.scope,
        displayName: '根工作区',
      };
      const recovery = {
        draft: draftForRecovery,
        message,
        providerId: newConversationProviderId,
        modelName: newConversationModelName,
        reasoningEffort: newConversationReasoningEffort,
        attempts: (bootstrapRecovery?.message.scope === message.scope ? bootstrapRecovery.attempts : 0) + 1,
      };
      if (recovery.attempts < MAX_BOOTSTRAP_RECONCILIATION_ATTEMPTS) {
        setConversationDraft(current => current ?? draftForRecovery);
        setBootstrapRecovery(recovery);
        writeBootstrapRecovery(host.bootstrapRecoveryStorageKey, recovery);
        setOperationError(undefined);
        return;
      }
      setOptimisticBootstrapTurn(current => current?.scope === message.scope ? undefined : current);
      setPendingBootstrap(undefined);
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      setTurnState('idle');
      setConversationDraft(current => current ?? draftForRecovery);
      if (activeComposerScope.current === message.scope) {
        setDraft(message.content);
        setAttachments(message.items);
      }
      clearBootstrapRecovery();
      reportOperationError(message.scope, new ApiError(
        '首条消息核对超时，已停止等待并恢复草稿。请稍后重新发送。',
        'AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS',
        {},
        504,
      ));
      return;
    }
    setOptimisticBootstrapTurn(current => current?.scope === message.scope ? undefined : current);
    setPendingBootstrap(undefined);
    clearLiveText();
    setActiveTurnEventId(undefined);
    setRequestStartedAt(undefined);
    setTurnState('idle');
    if (activeComposerScope.current === message.scope) {
      setDraft(message.content);
      setAttachments(message.items);
    }
    clearBootstrapRecovery();
    reportOperationError(message.scope, error);
  } });
  useEffect(() => {
    if (!bootstrapRecovery || !workspace || bootstrap.isPending
      || bootstrapRecovery.attempts >= MAX_BOOTSTRAP_RECONCILIATION_ATTEMPTS) return;
    const delay = 750 * bootstrapRecovery.attempts;
    const timer = window.setTimeout(() => bootstrap.mutate(bootstrapRecovery.message), delay);
    return () => window.clearTimeout(timer);
  }, [bootstrap, bootstrapRecovery, workspace]);
  const rename = useMutation({ mutationFn: () => api.updateConversation(workspace!.id, selected!.id, title.trim()), onSuccess: conversation => {
    queryClient.setQueryData<AgentConversation[]>(sessionQueryKey(host, 'conversations', workspace!.id), current => (current ?? []).map(item => item.id === conversation.id ? conversation : item));
    setTitle(conversationName(conversation));
    setEditing(false);
    refresh();
  }, onError: error => reportOperationError(selected?.id, error) });
  const remove = useMutation({ mutationFn: () => api.deleteConversation(workspace!.id, selected!.id), onSuccess: () => { setDrawerOpen(false); onNavigate(host.rootPath, true); refresh(); }, onError: error => reportOperationError(selected?.id, error) });
  const persistModel = useMutation({
    mutationFn: ({ providerId, modelName, effort }: { providerId: string; modelName: string; effort: string | null }) => api.switchConversationModel(workspace!.id, selected!.id, providerId, modelName, effort),
    onSuccess: value => {
      queryClient.setQueryData<AgentConversation[]>(sessionQueryKey(host, 'conversations', workspace!.id), current => (current ?? []).map(item => item.id === selected!.id ? { ...item, ...value } : item));
      setConversationProviderId(value.model_provider_id);
      setConversationModelName(value.model_name ?? '');
      setReasoningEffort(value.reasoning_effort ?? null);
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-context', workspace!.id, selected!.id) });
    },
    onError: () => {
      setConversationProviderId(selected?.model_provider_id ?? '');
      setConversationModelName(selected?.model_name ?? '');
      setReasoningEffort(selected?.reasoning_effort ?? null);
      reportOperationError(selected?.id, persistModel.error as Error);
    },
  });
  const send = useMutation({
    mutationFn: (message: BoundQueuedMessage) => api.sendMessage(workspace!.id, message.bindingId, message.content, message.items),
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
      return api.migrateStreamingConversation(
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
      queryClient.setQueryData<AgentConversation[]>(sessionQueryKey(host, 'conversations', workspace.id), current => [value, ...(current ?? []).filter(item => item.id !== value.id)]);
      setPendingMigratedSend({ ...message, bindingId: value.id });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
      onNavigate(host.conversationPath(value.id));
    },
    onError: (error, message) => {
      if (activeComposerScope.current === message.scope) { setDraft(message.content); setAttachments(message.items); }
      reportOperationError(message.scope, error);
    },
  });
  const upload = useMutation({ mutationFn: ({ file }: { file: File; scope: string }) => selected
    ? api.uploadConversationAttachment(workspace!.id, selected.id, file)
    : api.uploadDraftAttachment(workspace!.id, file, conversationDraft?.workDirectoryId, conversationDraft?.id), onSuccess: (value, request) => {
    if (activeComposerScope.current === request.scope) setAttachments(items => [...items, value]);
  }, onError: (error, request) => reportOperationError(request.scope, error) });
  const fork = useMutation({ mutationFn: (eventId: string) => api.forkConversation(workspace!.id, selected!.id, eventId), onSuccess: value => {
    if (!workspace) return;
    setPendingCreatedId(value.id);
    queryClient.setQueryData<AgentConversation[]>(sessionQueryKey(host, 'conversations', workspace.id), current => [value, ...(current ?? []).filter(item => item.id !== value.id)]);
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
    onNavigate(host.conversationPath(value.id));
  }, onError: error => reportOperationError(selected?.id, error) });
  const condense = useMutation({
    mutationFn: async () => {
      const workspaceId = workspace!.id;
      const bindingId = selected!.id;
      const queryKey = sessionQueryKey(host, 'conversation-events', workspaceId, bindingId);
      const existing = queryClient.getQueryData<OpenHandsConversationEventBatch>(queryKey);
      const completedBefore = new Set((existing?.events ?? [])
        .filter(event => event.event_type === 'CONDENSATION_COMPLETED')
        .map(event => event.id));
      const accepted = await api.condenseConversation(workspaceId, bindingId);
      const deadline = Date.now() + 360_000;
      while (Date.now() < deadline) {
        const batch = await api.conversationEvents(workspaceId, bindingId);
        queryClient.setQueryData(queryKey, batch);
        if (batch.events.some(event =>
          event.event_type === 'CONDENSATION_COMPLETED' && !completedBefore.has(event.id)
        )) return accepted;
        await new Promise(resolve => window.setTimeout(resolve, 600));
      }
      throw new Error('上下文压缩请求已接受，但未在 6 分钟内收到正式完成事件。');
    },
    onMutate: () => {
      setOperationError(undefined);
      setCondensationConfirmationOpen(false);
      if (selected) setCondensationStatus({ bindingId: selected.id, state: 'running', startedAt: Date.now() });
    },
    onSuccess: () => { setCondensationStatus(undefined); refresh(); },
    onError: error => {
      const message = error instanceof Error ? error.message : 'OpenHands 未能完成上下文压缩，请稍后重试。';
      setCondensationStatus(current => current ? { ...current, state: 'failed', message } : current);
    },
  });
  const interrupt = useMutation({ mutationFn: () => api.interruptConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('pausing'), onSuccess: refresh, onError: error => { setTurnState('running'); reportOperationError(selected?.id, error); } });
  const resume = useMutation({ mutationFn: () => api.resumeConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('resuming'), onSuccess: value => { if (value.cursor) setActiveTurnEventId(value.cursor); setTurnState('running'); refresh(); }, onError: error => { setTurnState('paused'); reportOperationError(selected?.id, error); } });
  const decideConfirmation = useMutation({
    mutationFn: (accept: boolean) => api.decideConfirmation(workspace!.id, selected!.id, pendingConfirmation!.pending_actions_digest!, accept, confirmationReason.trim()),
    onSuccess: value => {
      const cursor = value.cursor ?? undefined;
      if (cursor) setActiveTurnEventId(current => current ?? cursor);
      setConfirmationReason('');
      setRequestStartedAt(Date.now());
      setTurnState('running');
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace!.id, selected!.id) });
      refresh();
    },
    onError: error => reportOperationError(selected?.id, error),
  });
  const rewrite = useMutation({
    mutationFn: ({ eventId, content }: { eventId: string; content: string }) => api.rerunMessage(workspace!.id, selected!.id, eventId, content),
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
    clearBootstrapRecovery();
    setConversationDraft({ ...next, id: randomId(), capabilityVersionIds: next.capabilityVersionIds ?? [] });
    setPendingBootstrap(undefined);
    setWorkspaceScopeMigration(undefined);
    setDraft('');
    setAttachments([]);
    clearLiveText();
    setLiveEvents([]);
    setOptimisticBootstrapTurn(undefined);
    setHiddenEventIds(new Set());
    setTurnState('idle');
    onNavigate(host.rootPath);
  }, [clearBootstrapRecovery, clearLiveText, host.rootPath, onNavigate]);
  useEffect(() => {
    if (!autoOpenDraft || !workspace || selectedBindingId || conversationDraft) return;
    openConversationDraft({ displayName: '根工作区' });
  }, [autoOpenDraft, conversationDraft, openConversationDraft, selectedBindingId, workspace]);
  const enqueueDraft = useCallback(() => {
    const content = draft.trim();
    if ((!content && !attachments.length) || migrateStreaming.isPending || pendingMigratedSend || turnState === 'pausing' || turnState === 'resuming') return;
    setDraft('');
    setOperationError(undefined);
    if (!composerScope) return;
    const message = { id: randomId(), scope: composerScope, content, items: attachments };
    setAttachments([]);
    if (conversationDraft) {
      if (canBootstrap && !bootstrap.isPending) {
        setPendingBootstrap({ draft: conversationDraft, message });
        setOptimisticBootstrapTurn({
          scope: conversationDraft.id,
          event: { id: `pending-bootstrap:${message.id}`, event_type: 'MESSAGE', payload: { source: 'user', content, attachments } },
        });
        setRequestStartedAt(Date.now());
        setTurnState('running');
        bootstrap.mutate(message);
      }
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
  if (workspaceQuery.error || !workspace) {
    const error = workspaceQuery.error;
    const detail = error instanceof ApiError ? `${error.code}：${error.message}` : '无法读取节点会话上下文。';
    return <main className="agent-workbench-loading"><b>无法打开 Agent 工作台</b><span>{detail}</span><button type="button" className="secondary" onClick={() => void workspaceQuery.refetch()}>重试</button></main>;
  }
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
  const visibleContextWindow = typeof contextQuery.data?.window_tokens === 'number' && contextQuery.data.window_tokens > 0
    ? contextQuery.data.window_tokens
    : conversationDraft
      ? draftConversationModel?.context_window
      : conversationModel?.context_window;
  const contextUsagePending = Boolean(selected && contextQuery.data?.usage_current === false);
  const visibleContextTokens = typeof contextQuery.data?.used_tokens === 'number' && contextQuery.data.used_tokens >= 0
    ? contextQuery.data.used_tokens
    : contextUsagePending ? undefined : 0;
  const contextProgress = typeof visibleContextWindow === 'number' && visibleContextWindow > 0
    && typeof visibleContextTokens === 'number'
    ? {
      used: visibleContextTokens,
      window: visibleContextWindow,
      usedLabel: exactCount(visibleContextTokens),
      windowLabel: exactCount(visibleContextWindow),
      percentage: Math.min(100, Math.round((visibleContextTokens / visibleContextWindow) * 100)),
    }
    : undefined;
  const forgottenEventIds = new Set(displayedEvents.flatMap(event =>
    event.event_type === 'CONDENSATION_COMPLETED' && Array.isArray(event.payload.forgotten_event_ids)
      ? event.payload.forgotten_event_ids.filter((id): id is string => typeof id === 'string')
      : [],
  ));
  const activeContextEvents = displayedEvents.filter(event => !forgottenEventIds.has(event.id));
  const activeEventCount = activeContextEvents.length;
  const eventLimit = typeof contextQuery.data?.condenser_max_size === 'number'
    && contextQuery.data.condenser_max_size > 0
    ? contextQuery.data.condenser_max_size
    : 10_000;
  const eventProgress = eventLimit
    ? Math.min(100, Math.round((activeEventCount / eventLimit) * 100))
    : 0;
  const compactionThreshold = Math.round((contextQuery.data?.proactive_compaction_ratio ?? 0.8) * 100);
  const manualCompactionNeedsConfirmation = contextProgress !== undefined
    && contextProgress.percentage < compactionThreshold
    && eventProgress < compactionThreshold;
  const requestManualCompaction = () => {
    if (manualCompactionNeedsConfirmation) {
      setCondensationConfirmationOpen(true);
      return;
    }
    condense.mutate();
  };
  const contextTitle = contextProgress
    ? `Token：OpenHands 当前 View ${contextProgress.used.toLocaleString()} / ${contextProgress.window.toLocaleString()}（${contextProgress.percentage}%）；达到 ${Math.round((contextQuery.data?.proactive_compaction_ratio ?? 0.8) * 100)}% 时发送前主动调用原生压缩`
    : undefined;
  const tokenPendingLabel = contextQuery.isLoading
    ? '读取中'
    : contextQuery.isError
      ? '暂不可用'
      : contextUsagePending
        ? '待模型更新'
        : '模型窗口未知';
  const tokenPendingTitle = contextUsagePending
    ? 'OpenHands 已完成原生压缩；下一次主模型调用后会产生当前 View 的新 Token 用量。'
    : '当前模型尚未提供可验证的上下文窗口；不会显示估算值。';
  const activityTitle = `当前活动事件 ${activeEventCount.toLocaleString()} / ${eventLimit?.toLocaleString() ?? 'OpenHands 自身上限'}。OpenHands 按事件规模触发兜底压缩。`;
  const composerStatus = bootstrapRecovery
    ? '正在安全核对首条消息'
    : conversationDraft && !newConversationModelName ? '请选择模型' : condense.isPending ? '正在压缩上下文' : contextUsagePending ? '压缩已完成，等待下次模型调用更新用量' : persistModel.isPending ? '正在保存模型设置' : migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : turnState === 'pausing' ? '正在暂停' : turnState === 'paused' ? '已暂停' : turnState === 'resuming' ? '正在继续' : turnState === 'running' ? '正在处理' : streamStatus === 'recovering' ? '连接恢复中' : undefined;
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
    || condense.isPending
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
  const openCurrentDirectoryDraft = () => {
    const directory = selected?.work_directory_id
      ? workDirectories.find(item => item.id === selected.work_directory_id)
      : undefined;
    openConversationDraft({
      workDirectoryId: directory?.id,
      displayName: directory?.display_name ?? '根工作区',
    });
  };
  const pendingBootstrapItem = pendingBootstrap
    ? <button className={pendingBootstrap.draft.id === conversationDraft?.id ? 'active' : ''} aria-current={pendingBootstrap.draft.id === conversationDraft?.id ? 'page' : undefined} aria-label={`${pendingConversationName(pendingBootstrap.message)}，正在创建会话`}><LoaderCircle className="conversation-activity-spin" size={13}/><span><b>{pendingConversationName(pendingBootstrap.message)}</b><small>正在创建会话</small></span><ChevronRight size={13}/></button>
    : null;
  const rootConversations = conversations.filter(item => !item.work_directory_id);
  const conversationsForDirectory = (workDirectoryId: string) => conversations.filter(
    item => item.work_directory_id === workDirectoryId,
  );
  const selectConversation = (bindingId: string) => {
    setConversationDraft(undefined);
    onNavigate(host.conversationPath(bindingId));
  };

  return <main className="agent-workbench-page">
    <aside className="agent-workbench-rail">
      <header>{onReturnToSource && <button type="button" className="agent-session-return" aria-label="返回节点执行" title="返回节点执行" onClick={onReturnToSource}><ArrowLeft size={16}/></button>}<div className="agent-session-host-heading"><span className="eyebrow">{onReturnToSource ? 'FLOWRUN NODE WORKSPACE' : features.workDirectories ? 'AGENT WORKSPACE' : 'FLOWRUN NODE'}</span><h1>{onReturnToSource ? workspace?.display_name || '节点会话' : features.workDirectories ? 'Agent 会话' : '节点会话'}</h1></div><div className="agent-workbench-create-actions"><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: '根工作区' })}><Plus size={15}/>新建会话</button>{features.workDirectories && <button type="button" className="secondary" aria-label="新增工作区" onClick={() => setWorkDirectoryCreatorOpen(true)}><FolderPlus size={14}/>新增工作区</button>}</div></header>
      <div className="agent-workbench-list">
        <WorkspaceConversationGroup groupId="root" label="根工作区" canCreateConversation={canOpenConversation} onCreateConversation={() => openConversationDraft({ displayName: '根工作区' })}>
          {pendingBootstrapItem && !pendingBootstrap?.draft.workDirectoryId ? pendingBootstrapItem : null}
          {rootConversations.map(item => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => selectConversation(item.id)}><CircleDot size={13}/><span><b>{conversationName(item)}</b><small>{item.title_state === 'PENDING' ? '正在生成标题' : '可继续会话'}</small></span><ChevronRight size={13}/></button>)}
        </WorkspaceConversationGroup>
        {features.workDirectories && workDirectories.map(directory => <WorkspaceConversationGroup key={directory.id} groupId={directory.id} label={directory.display_name} canCreateConversation={canOpenConversation} onCreateConversation={() => openConversationDraft({ workDirectoryId: directory.id, displayName: directory.display_name })}>
          {pendingBootstrapItem && pendingBootstrap?.draft.workDirectoryId === directory.id ? pendingBootstrapItem : null}
          {conversationsForDirectory(directory.id).map(item => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => selectConversation(item.id)}><CircleDot size={13}/><span><b>{conversationName(item)}</b><small>{item.title_state === 'PENDING' ? '正在生成标题' : '可继续会话'}</small></span><ChevronRight size={13}/></button>)}
        </WorkspaceConversationGroup>)}
      </div>
      {features.capabilities && (selected || features.draftCapabilitySelection) && <footer className="agent-workbench-rail-footer"><button type="button" onClick={() => setCapabilityManagerOpen(true)}><Boxes size={15}/><span><b>能力</b><small>{selected ? '管理当前会话能力' : '为新会话选择能力'}</small></span><ChevronRight size={14}/></button></footer>}
    </aside>
    <section className="agent-workbench-main">
      <header className="agent-workbench-header"><div><span className="eyebrow">DIRECT AGENT SESSION</span>{editing ? <div className="agent-title-edit"><input ref={titleInput} aria-label="会话标题" value={title} onChange={event => setTitle(event.target.value)} onBlur={() => { if (!rename.isPending) { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }} onKeyDown={event => { if (event.key === 'Enter' && title.trim()) { event.preventDefault(); rename.mutate(); } if (event.key === 'Escape') { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }}/></div> : !(hideDraftTitle && conversationDraft) && <h2 title={selected ? '双击修改标题' : undefined} onDoubleClick={() => { if (!selected) return; setTitle(conversationName(selected)); setEditing(true); }}>{selected ? conversationName(selected) : conversationDraft ? '新会话' : '开始一个新的会话'}</h2>}{features.modelSelection && (selected || conversationDraft) && <small className="agent-session-provider">当前供应商：{selected ? boundProviderInfo?.name ?? '未配置' : draftProviderInfo?.name ?? '请选择模型供应商'}{conversationDraft ? ` · ${conversationDraft.displayName}` : ''}</small>}</div><div className="agent-header-actions">{features.conversationDeletion && selected && <button type="button" className="danger" aria-label="删除会话" disabled={remove.isPending} onClick={() => remove.mutate()}><Trash2 size={14}/></button>}</div></header>
      {runtime?.state === 'RECOVERING' && <section className="agent-runtime-recover"><LoaderCircle size={18}/><div><b>运行环境正在恢复</b><span>{runtime.message || '历史会话和工作区文件仍可查看；恢复完成后可继续发送消息和使用终端。'}</span></div></section>}
      {selected && !compactionPolicyCurrent && <section className="agent-compaction-policy-warning" aria-label="历史压缩策略兼容保护"><ShieldAlert size={18}/><div><b>已启用历史会话兼容保护</b><span>此会话继承了旧的事件数压缩策略。继续发送或恢复执行前，系统会先调用 OpenHands 原生压缩并校验摘要；校验失败时不会发送新消息。</span>{features.workDirectories && <button type="button" className="primary" disabled={!canOpenConversation} onClick={openCurrentDirectoryDraft}><Plus size={14}/>在相同工作目录新建会话</button>}</div></section>}
      {selected || conversationDraft ? <ConversationSurface key={selected?.id ?? conversationDraft?.id} events={displayedEvents} liveText={liveText} isGenerating={isGenerating} requestStartedAt={requestStartedAt} requestSubmitting={send.isPending || bootstrap.isPending || rewrite.isPending} rewritePending={rewrite.isPending || Boolean(pendingRewrite)} condensationStatus={selected && condensationStatus?.bindingId === selected.id ? condensationStatus : undefined} onRetryCondensation={selected && condensationStatus?.bindingId === selected.id && condensationStatus.state === 'failed' ? requestManualCompaction : undefined} onRewrite={selected && features.rewrite ? requestRewrite : undefined} onFork={selected && features.fork ? eventId => fork.mutate(eventId) : undefined} onOpenAttachment={features.attachments ? openAttachmentInDrawer : undefined}/> : <div className="agent-workbench-empty"><Bot size={32}/><b>新建会话开始协作</b><span>{features.workDirectories ? '每个会话共享同一工作区，但保留独立的对话与事件记录。' : '会话固定在当前节点 Attempt 的隔离工作目录。'}</span><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: features.workDirectories ? '根工作区' : '节点工作目录' })}><Plus size={15}/>新建会话</button></div>}
      {(selected || conversationDraft) && runtime?.state !== 'RECOVERING' && <div className={`agent-composer ${turnState !== 'idle' || pendingConfirmation ? 'busy' : ''}`}>
        {pendingConfirmation && <section className="agent-confirmation" aria-label="工具执行确认"><header><ShieldAlert size={17}/><div><b>工具正在等待你的确认</b><span>动作尚未执行。请核对整批内容后批准或拒绝。</span></div></header><div className="agent-confirmation-actions">{(pendingConfirmation.actions ?? []).map((action: AgentPendingConfirmationAction) => <article key={action.digest}><div><b>{action.summary || action.tool_name}</b><span>{action.security_risk || 'UNKNOWN'}</span></div>{Object.keys(action.arguments).length > 0 && <pre>{JSON.stringify(action.arguments, null, 2)}</pre>}</article>)}</div><textarea aria-label="工具确认理由" value={confirmationReason} maxLength={2000} placeholder="填写批准或拒绝理由…" onChange={event => setConfirmationReason(event.target.value)}/><footer><button type="button" className="danger" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(false)}><X size={14}/>拒绝整批</button><button type="button" className="primary" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(true)}><Check size={14}/>批准整批</button></footer></section>}
        {queuedMessages.length > 0 && <section className="agent-queued-messages" aria-label="已排队消息"><header><b>消息队列</b><span>{queuedMessages.length} 条将在当前回复完成后依次发送</span></header>{queuedMessages.map((message, index) => <article key={message.id}><small>{index + 1}</small><p>{message.content || '图片附件'}</p><span>{message.items.length ? `${message.items.length} 个附件` : ''}</span><div><button type="button" aria-label={`编辑排队消息 ${index + 1}`} onClick={() => { setDraft(message.content); setAttachments(message.items); setQueuedMessages(items => items.filter(item => item.id !== message.id)); }}>编辑</button><button type="button" aria-label={`移除排队消息 ${index + 1}`} onClick={() => setQueuedMessages(items => items.filter(item => item.id !== message.id))}><X size={13}/></button></div></article>)}</section>}
        {condensationConfirmationOpen && selected && <section className="agent-condensation-confirmation" aria-label="确认低用量上下文压缩" role="alertdialog" aria-modal="false">
          <ShieldAlert size={17}/><div><b>当前上下文用量较低</b><p>Token {contextProgress?.usedLabel} / {contextProgress?.windowLabel}（{contextProgress?.percentage}%），事件 {activeEventCount.toLocaleString()} / {eventLimit.toLocaleString()}（{eventProgress}%）。现在压缩可能没有足够的可压缩区间，并且仍会调用摘要模型。</p><footer><button type="button" onClick={() => setCondensationConfirmationOpen(false)}>取消</button><button type="button" className="primary" onClick={() => condense.mutate()}>仍然压缩</button></footer></div>
        </section>}
        <ComposerCapabilityAutocomplete draft={draft} suggestions={composerSuggestions} placeholder={pendingConfirmation ? '请先处理上方工具确认…' : turnState === 'paused' ? '已暂停：可继续，也可编辑上方消息重新思考…' : features.capabilities ? '给 Agent 发消息…（输入 $ 选择 Skill，/ 选择 OpenHands 原生能力、命令或 MCP）' : '给 Agent 发消息…'} disabled={!canCompose || Boolean(pendingConfirmation) || bootstrap.isPending || condense.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend) || turnState === 'pausing' || turnState === 'resuming'} onDraftChange={setDraft} onPaste={event => { if (!features.attachments) return; const images = Array.from(event.clipboardData.items).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter((file): file is File => file !== null); if (!images.length || !composerScope) return; event.preventDefault(); for (const image of images) upload.mutate({ file: image, scope: composerScope }); }} onSubmit={enqueueDraft} onManageCapabilities={features.capabilities && (selected || features.draftCapabilitySelection) ? () => setCapabilityManagerOpen(true) : undefined} onNativeAction={action => { if (action === 'CONDENSE' && selected && (turnState === 'idle' || turnState === 'paused') && !pendingConfirmation && !condense.isPending) requestManualCompaction(); }}/>
        {features.attachments && attachments.length > 0 && <div className="agent-attachments">{attachments.map(item => <span key={item.path}><button type="button" className="agent-attachment-open" title={`在右侧查看附件：${item.filename}`} onClick={() => openAttachmentInDrawer(item)}>{item.image_data_url && <img src={item.image_data_url} alt=""/>}<em>{item.filename}</em></button><button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(all => all.filter(candidate => candidate.path !== item.path))}>×</button></span>)}</div>}
        <footer>
          <div className="agent-composer-context">
            {features.attachments && (selected || conversationDraft) && <><input ref={attachmentInput} aria-label="上传附件" type="file" multiple hidden onChange={event => { if (composerScope) for (const file of Array.from(event.target.files ?? [])) upload.mutate({ file, scope: composerScope }); event.currentTarget.value = ''; }}/><button type="button" aria-label="添加附件" disabled={!canCompose || Boolean(pendingConfirmation) || upload.isPending} onClick={() => attachmentInput.current?.click()}><Plus size={17}/></button></>}
            {contextProgress ? <span className="agent-context-progress token" title={contextTitle} aria-label={`Token 上下文用量 ${contextProgress.percentage}%，80% 时主动压缩`}><i style={{ '--context-progress': `${contextProgress.percentage}%` } as CSSProperties}/><em><small>Token</small>{contextProgress.usedLabel} / {contextProgress.windowLabel}</em></span> : (selected || conversationDraft) && <span className="agent-context-progress token pending" title={tokenPendingTitle} aria-label={`Token 上下文用量${tokenPendingLabel}`}><i style={{ '--context-progress': '0%' } as CSSProperties}/><em><small>Token</small>{tokenPendingLabel}</em></span>}
            {(selected || conversationDraft) && <span className="agent-context-progress activity events" title={activityTitle} aria-label={`当前活动事件 ${activeEventCount} 条，上限 ${eventLimit} 条`}><i style={{ '--context-progress': `${eventProgress}%` } as CSSProperties}/><em><small>事件</small>{exactCount(activeEventCount)} / {exactCount(eventLimit)}</em></span>}
            {composerStatus && <span className="agent-composer-status">{composerStatus}</span>}
            {composerNote && <span className="agent-composer-note">{composerNote}</span>}
          </div>
          <div className="agent-composer-actions">
            {features.modelSelection && (selected ? <ComposerModelMenu providers={connectedProviders} providerId={conversationProviderId} modelName={activeConversationModelName} models={availableConversationModels} efforts={supportedEfforts} effort={reasoningEffort ?? selected.reasoning_effort ?? contextQuery.data?.reasoning_effort ?? conversationModel?.default_reasoning_effort ?? ''} disabled={!canWrite || isGenerating || queuedMessages.length > 0 || Boolean(pendingConfirmation) || persistModel.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend)} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; const effort = model.default_reasoning_effort ?? null; setConversationProviderId(providerId); setConversationModelName(model.model_name); setReasoningEffort(effort); persistModel.mutate({ providerId, modelName: model.model_name, effort }); }} onModelChange={modelName => { const model = availableConversationModels.find(item => item.model_name === modelName); const effort = model?.default_reasoning_effort ?? null; setConversationModelName(modelName); setReasoningEffort(effort); persistModel.mutate({ providerId: conversationProviderId, modelName, effort }); }} onEffortChange={effort => { const nextEffort = effort || null; setReasoningEffort(nextEffort); persistModel.mutate({ providerId: conversationProviderId, modelName: activeConversationModelName, effort: nextEffort }); }}/> : <ComposerModelMenu providers={connectedProviders} providerId={newConversationProviderId} modelName={newConversationModelName} models={availableDraftModels} efforts={supportedDraftEfforts} effort={newConversationReasoningEffort ?? draftConversationModel?.default_reasoning_effort ?? ''} disabled={!runtimeWritable || bootstrap.isPending} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; setNewConversationProviderId(providerId); setNewConversationModelName(model.model_name); setNewConversationReasoningEffort(model.default_reasoning_effort ?? null); }} onModelChange={modelName => { const model = availableDraftModels.find(item => item.model_name === modelName); setNewConversationModelName(modelName); setNewConversationReasoningEffort(model?.default_reasoning_effort ?? null); }} onEffortChange={effort => setNewConversationReasoningEffort(effort || null)}/>) }
            <button type="button" className={`agent-send${turnState === 'paused' || turnState === 'resuming' ? ' resume' : ''}`} aria-label={composerActionLabel} disabled={composerActionDisabled} onClick={runComposerAction}>{pendingConfirmation ? <ShieldAlert size={14}/> : turnState === 'idle' ? <Send size={16}/> : turnState === 'paused' || turnState === 'resuming' ? <Play size={12} fill="currentColor"/> : <Square size={10} fill="currentColor"/>}</button>
          </div>
        </footer>
      </div>}
      {visibleError && <p className="agent-workbench-error">{visibleError.message}</p>}
    </section>
    <WorkspaceDrawer open={drawerOpen} onOpen={() => setDrawerOpen(true)} onClose={() => setDrawerOpen(false)} workspaceId={workspace.id} scopeKey={selected?.id ?? pendingCreatedId ?? conversationDraft?.id ?? 'workspace-root'} migrateFromScopeKey={workspaceScopeMigration} bindingId={selected?.id} workDirectoryId={selected ? undefined : conversationDraft?.workDirectoryId} attachments={drawerAttachments} sources={drawerSources} attachmentRequest={attachmentRequest} runtimeAvailable={Boolean(runtime?.write_available && (!features.terminalRequiresConversation || selected))}/>
    {workDirectoryCreatorOpen && <WorkDirectoryCreator workspaceId={workspace.id} onClose={() => setWorkDirectoryCreatorOpen(false)} onCreated={directory => {
      queryClient.setQueryData<AgentSessionWorkDirectoryList>(sessionQueryKey(host, 'work-directories', workspace.id), current => current ? { ...current, items: [directory, ...current.items.filter(item => item.id !== directory.id)] } : current);
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'work-directories', workspace.id) });
    }}/>}
    {capabilityManagerOpen && (selected || features.draftCapabilitySelection) && <CapabilityManager workspaceId={workspace.id} bindingId={selected?.id} conversationCapabilities={selected?.capabilities} draftCapabilityIds={conversationDraft?.capabilityVersionIds} onClose={() => setCapabilityManagerOpen(false)} onCreateEnhancedConversation={capabilityVersionIds => { const directory = selected?.work_directory_id ? workDirectories.find(item => item.id === selected.work_directory_id) : undefined; setCapabilityManagerOpen(false); if (conversationDraft) setConversationDraft(current => current ? { ...current, capabilityVersionIds } : current); else openConversationDraft({ workDirectoryId: directory?.id, displayName: directory?.display_name ?? '根工作区', capabilityVersionIds }); }}/>}
  </main>;
}
