import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, Check, ChevronDown, ChevronRight, CircleDot, LoaderCircle, Minimize2, PanelRightOpen, Pencil, Play, Plus, Send, Settings2, ShieldAlert, Square, Terminal, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { ApiError, agentWorkspaceTerminalUrl, api, subscribeToAgentWorkspaceStream } from '../api/client';
import { ConversationSurface } from '../components/ConversationSurface';
import type { AgentAttachment, AgentConversation, AgentPendingConfirmationAction, ModelProvider, OpenHandsConversationEvent, ProviderModel } from '../types';
import './agent-workbench.css';
import './agent-workbench-layout.css';

type StreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';
type TurnState = 'idle' | 'running' | 'pausing' | 'paused' | 'resuming';
interface QueuedMessage {
  content: string;
  items: AgentAttachment[];
}
interface BoundQueuedMessage extends QueuedMessage { bindingId: string; }

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
    <summary aria-label="打开模型与推理设置"><span>{modelName || '选择模型'}</span><ChevronDown size={14}/></summary>
    <section className="agent-composer-model-popover">
      <label><span>供应商</span><select aria-label="会话供应商" value={providerId} disabled={disabled} onChange={event => onProviderChange(event.target.value)}><option value="" disabled>选择供应商</option>{providers.map(provider => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select><ChevronRight size={14}/></label>
      <label><span>模型</span><select aria-label="会话模型" value={modelName} disabled={disabled || !providerId} onChange={event => onModelChange(event.target.value)}>{modelName && !models.some(model => model.model_name === modelName) && <option value={modelName} disabled>{modelName}</option>}{models.map(model => <option key={model.model_name} value={model.model_name}>{model.model_name}</option>)}</select><ChevronRight size={14}/></label>
      {efforts.length > 0 && <label><span>推理强度</span><select aria-label="思考程度" value={effort} disabled={disabled || !providerId} onChange={event => onEffortChange(event.target.value)}>{[effort, ...efforts].filter((value, index, all): value is string => Boolean(value) && all.indexOf(value) === index).map(value => <option key={value} value={value}>{value}</option>)}</select><ChevronRight size={14}/></label>}
      <details className="agent-composer-model-advanced"><summary><span>高级</span><ChevronDown size={12}/></summary><p>选择后立即保存并应用到当前会话。</p></details>
    </section>
  </details>;
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

function conversationName(conversation: AgentConversation, index: number) {
  return conversation.display_title || `未命名会话 ${index + 1}`;
}

function mergeConversationEvents(
  durable: OpenHandsConversationEvent[],
  transient: OpenHandsConversationEvent[],
): OpenHandsConversationEvent[] {
  const merged = new Map(transient.map(event => [event.id, event]));
  for (const event of durable) merged.set(event.id, event);
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

function WorkspaceTerminal({ workspaceId }: { workspaceId: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在连接工作区终端…');

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const terminal = new XTerm({ cursorBlink: true, scrollback: 3000, fontSize: 12, lineHeight: 1.3, fontFamily: "'DM Mono', ui-monospace, SFMono-Regular, Menlo, monospace", theme: { background: '#07110b', foreground: '#c8f7d8', cursor: '#75e99d', selectionBackground: '#315d42' } });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    let socket: WebSocket | null = null;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let attempts = 0;
    const reconnectDelays = [1000, 2000, 5000, 10_000, 30_000];
    const resize = () => {
      if (element.clientWidth < 160 || element.clientHeight < 100) return;
      try { fit.fit(); } catch { return; }
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', rows: terminal.rows, columns: terminal.cols }));
    };
    const connect = () => {
      if (disposed) return;
      setState('connecting');
      setDetail(attempts ? '终端已断开，正在重新连接…' : '正在连接工作区终端…');
      const current = new WebSocket(agentWorkspaceTerminalUrl(workspaceId, terminal.rows, terminal.cols));
      socket = current;
      current.binaryType = 'arraybuffer';
      current.onopen = () => { attempts = 0; setState('connected'); setDetail('已连接共享工作区'); resize(); terminal.focus(); };
      current.onmessage = event => terminal.write(typeof event.data === 'string' ? event.data : new Uint8Array(event.data));
      current.onclose = event => {
        if (socket === current) socket = null;
        if (disposed || event.code === 1000) return;
        if (event.code === 4409 || attempts >= reconnectDelays.length) { setState('unavailable'); setDetail(event.reason || '终端暂时不可用'); return; }
        const delay = reconnectDelays[attempts];
        attempts += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };
    connect();
    const input = terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => { disposed = true; if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer); observer.disconnect(); input.dispose(); socket?.close(1000); terminal.dispose(); };
  }, [workspaceId]);

  return <section className="agent-workspace-terminal"><header><span className={`terminal-dot ${state}`}/><span>{detail}</span></header><div ref={host} aria-label="Agent 工作区终端"/></section>;
}

interface Props { onNavigate: (path: string, replace?: boolean) => void; onOpenModels: () => void; }

export function AgentWorkbenchPage({ onNavigate, onOpenModels }: Props) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('disabled');
  const [liveText, setLiveText] = useState('');
  const [liveEvents, setLiveEvents] = useState<OpenHandsConversationEvent[]>([]);
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
  const [conversationProviderId, setConversationProviderId] = useState('');
  const [conversationModelName, setConversationModelName] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AgentAttachment[]>([]);
  const [pendingCreatedId, setPendingCreatedId] = useState<string>();
  const [pendingMigratedSend, setPendingMigratedSend] = useState<BoundQueuedMessage>();
  const attachmentInput = useRef<HTMLInputElement>(null);
  const selectedBindingId = bindingIdFromLocation();
  const workspaceQuery = useQuery({ queryKey: ['agent-workspace-default'], queryFn: api.defaultAgentWorkspace, retry: false });
  const workspace = workspaceQuery.data;
  const runtimeQuery = useQuery({ queryKey: ['agent-workspace-runtime', workspace?.id], queryFn: () => api.agentWorkspaceRuntime(workspace!.id), enabled: Boolean(workspace), refetchInterval: result => result.state.data?.state === 'RECOVERING' ? 5000 : false });
  const conversationsQuery = useQuery({ queryKey: ['agent-conversations', workspace?.id], queryFn: () => api.agentConversations(workspace!.id), enabled: Boolean(workspace) });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers, enabled: Boolean(workspace) });
  const conversations = useMemo(() => conversationsQuery.data ?? [], [conversationsQuery.data]);
  const selected = useMemo(() => conversations.find(item => item.id === selectedBindingId), [conversations, selectedBindingId]);
  const connectedProviders = (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default));
  const runtime = runtimeQuery.data;
  const runtimeWritable = Boolean(workspace && runtime?.write_available);
  const canCreate = Boolean(runtimeWritable && newConversationProviderId);
  const canWrite = Boolean(runtimeWritable && selected);
  const isGenerating = turnState === 'running' || turnState === 'pausing' || turnState === 'resuming';
  const eventsQuery = useQuery({
    queryKey: ['agent-conversation-events', workspace?.id, selected?.id], queryFn: () => api.agentConversationEvents(workspace!.id, selected!.id), enabled: Boolean(workspace && selected), refetchInterval: isGenerating ? 1200 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const displayedEvents = useMemo(
    () => mergeConversationEvents(eventsQuery.data?.events ?? [], liveEvents),
    [eventsQuery.data?.events, liveEvents],
  );
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
  const onStreamEvent = useCallback((event: { type: 'delta' | 'event' | 'message_complete'; content?: string; event?: OpenHandsConversationEvent }) => {
    if (event.type === 'delta' && event.content) setLiveText(value => value + event.content);
    if (event.type === 'event' && event.event) {
      setLiveEvents(current => mergeConversationEvents(current, [event.event!]));
      if (['THOUGHT', 'TOOL_CALL', 'MESSAGE', 'ERROR', 'COMPLETED'].includes(event.event.event_type)) setLiveText('');
    }
    // Completion frames do not identify the originating user event.  A stale
    // frame must never complete a newer turn; durable assistant/error events
    // associated with activeTurnEventId are the authoritative terminal signal.
    if (event.type === 'message_complete') { setLiveText(''); refresh(); }
  }, [refresh]);

  useEffect(() => {
    if (!selectedBindingId && conversations.length) onNavigate(`/agent/conversations/${encodeURIComponent(conversations[0].id)}`, true);
    if (selectedBindingId && conversations.length && !selected && pendingCreatedId !== selectedBindingId && !conversationsQuery.isFetching) onNavigate('/agent', true);
  }, [conversations, conversationsQuery.isFetching, onNavigate, pendingCreatedId, selected, selectedBindingId]);
  useEffect(() => { if (!workspace || !selected || !runtime?.write_available) { setStreamStatus('disabled'); return; } return subscribeToAgentWorkspaceStream(workspace.id, selected.id, onStreamEvent, setStreamStatus); }, [onStreamEvent, runtime?.write_available, selected, workspace]);
  useEffect(() => { if (selected?.id === pendingCreatedId) setPendingCreatedId(undefined); }, [pendingCreatedId, selected?.id]);
  useEffect(() => { setEditing(false); setTitle(selected?.display_title ?? ''); setLiveText(''); setLiveEvents([]); setActiveTurnEventId(undefined); setRequestStartedAt(undefined); setConfirmationReason(''); setTurnState('idle'); setQueuedMessages([]); setPendingRewrite(undefined); setAttachments([]); }, [selected?.display_title, selected?.id]);
  useEffect(() => {
    setConversationProviderId(selected?.model_provider_id ?? '');
    setConversationModelName(selected?.model_name ?? '');
    setReasoningEffort(selected?.reasoning_effort ?? null);
    setNewConversationProviderId(current => current || selected?.model_provider_id || '');
  }, [selected?.id, selected?.model_name, selected?.model_provider_id, selected?.reasoning_effort]);
  useEffect(() => {
    if (!newConversationProviderId && connectedProviders.length) {
      setNewConversationProviderId(connectedProviders[0].id);
    }
  }, [connectedProviders, newConversationProviderId]);
  useEffect(() => {
    if (turnState === 'pausing' && inputReadinessQuery.data?.ready) setTurnState('paused');
  }, [inputReadinessQuery.data?.ready, turnState]);
  useEffect(() => {
    if ((turnState === 'running' || turnState === 'resuming') && activeTurnEventId && hasFinishedTurn(displayedEvents, activeTurnEventId)) {
      setLiveText('');
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      setTurnState('idle');
      refresh();
    }
  }, [activeTurnEventId, displayedEvents, refresh, turnState]);

  const create = useMutation({ mutationFn: () => api.createAgentConversation(workspace!.id, newConversationProviderId), onSuccess: value => {
    if (!workspace) return;
    setPendingCreatedId(value.id);
    queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace.id], current => [value, ...(current ?? []).filter(item => item.id !== value.id)]);
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
    onNavigate(`/agent/conversations/${encodeURIComponent(value.id)}`);
  } });
  const rename = useMutation({ mutationFn: () => api.updateAgentConversation(workspace!.id, selected!.id, title.trim()), onSuccess: () => { setEditing(false); refresh(); } });
  const remove = useMutation({ mutationFn: () => api.deleteAgentConversation(workspace!.id, selected!.id), onSuccess: () => { setDrawerOpen(false); onNavigate('/agent', true); refresh(); } });
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
    },
  });
  const send = useMutation({ mutationFn: (message: BoundQueuedMessage) => api.sendAgentMessage(workspace!.id, message.bindingId, message.content, message.items), onMutate: () => { setActiveTurnEventId(undefined); setRequestStartedAt(Date.now()); setTurnState('running'); }, onSuccess: (value, message) => { const cursor = value.cursor; setLiveText(''); if (cursor) { setActiveTurnEventId(cursor); setLiveEvents([{ id: cursor, event_type: 'MESSAGE', payload: { source: 'user', content: message.content } }]); } setAttachments([]); refresh(); }, onError: (error, message) => { if (error instanceof ApiError && error.code === 'AGENT_CONVERSATION_BUSY') { setQueuedMessages(current => [...current, { content: message.content, items: message.items }]); setActiveTurnEventId(undefined); setTurnState('running'); return; } setActiveTurnEventId(undefined); setRequestStartedAt(undefined); setTurnState('idle'); } });
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
    onError: (_error, message) => {
      setDraft(message.content);
      setAttachments(message.items);
    },
  });
  const upload = useMutation({ mutationFn: (file: File) => api.uploadAgentAttachment(workspace!.id, selected!.id, file), onSuccess: value => setAttachments(items => [...items, value]) });
  const condense = useMutation({ mutationFn: () => api.condenseAgentConversation(workspace!.id, selected!.id), onSuccess: refresh });
  const fork = useMutation({ mutationFn: (eventId: string) => api.forkAgentConversation(workspace!.id, selected!.id, eventId), onSuccess: value => {
    if (!workspace) return;
    setPendingCreatedId(value.id);
    queryClient.setQueryData<AgentConversation[]>(['agent-conversations', workspace.id], current => [value, ...(current ?? []).filter(item => item.id !== value.id)]);
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations', workspace.id] });
    onNavigate(`/agent/conversations/${encodeURIComponent(value.id)}`);
  } });
  const interrupt = useMutation({ mutationFn: () => api.interruptAgentConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('pausing'), onSuccess: refresh, onError: () => setTurnState('running') });
  const resume = useMutation({ mutationFn: () => api.resumeAgentConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('resuming'), onSuccess: value => { if (value.cursor) setActiveTurnEventId(value.cursor); setTurnState('running'); refresh(); }, onError: () => setTurnState('paused') });
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
  });
  const rewrite = useMutation({ mutationFn: ({ eventId, content }: { eventId: string; content: string }) => api.rerunAgentMessage(workspace!.id, selected!.id, eventId, content), onMutate: () => { setQueuedMessages([]); setLiveText(''); setActiveTurnEventId(undefined); setRequestStartedAt(Date.now()); setTurnState('running'); }, onSuccess: value => { if (value.cursor) setActiveTurnEventId(value.cursor); refresh(); }, onError: () => { setRequestStartedAt(undefined); setTurnState('paused'); } });
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
  const enqueueDraft = useCallback(() => {
    const content = draft.trim();
    if (!content || !canWrite || migrateStreaming.isPending || pendingMigratedSend || turnState === 'pausing' || turnState === 'resuming') return;
    setDraft('');
    const message = { content, items: attachments };
    setAttachments([]);
    if (turnState === 'idle') {
      if (selected?.streaming_callback_ready) send.mutate({ ...message, bindingId: selected.id });
      else migrateStreaming.mutate(message);
    }
    else setQueuedMessages(items => [...items, message]);
  }, [attachments, canWrite, draft, migrateStreaming, pendingMigratedSend, selected, send, turnState]);
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
  const providerLabel = (item: typeof connectedProviders[number]) => `${item.name} · ${item.models.find(model => model.enabled && model.is_default)?.model_name}`;
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
  const composerStatus = persistModel.isPending ? '正在保存模型设置' : migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : turnState === 'pausing' ? '正在暂停' : turnState === 'paused' ? '已暂停' : turnState === 'resuming' ? '正在继续' : turnState === 'running' ? '正在处理' : streamStatus === 'recovering' ? '连接恢复中' : undefined;
  const composerNote = queuedMessages.length > 0 ? `已排队 ${queuedMessages.length} 条` : '';
  const visibleError = (create.error || rename.error || remove.error || upload.error || condense.error || fork.error || persistModel.error || migrateStreaming.error || interrupt.error || resume.error || rewrite.error || decideConfirmation.error || confirmationQuery.error || eventsQuery.error)
    ?? (send.error instanceof ApiError && send.error.code === 'AGENT_CONVERSATION_BUSY' ? null : send.error);
  const composerActionLabel = migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : turnState === 'idle'
    ? '发送消息'
    : turnState === 'running'
      ? '暂停当前 Agent'
      : turnState === 'paused'
        ? '继续当前 Agent'
        : turnState === 'pausing' ? '正在暂停 Agent' : '正在继续 Agent';
  const composerActionDisabled = !canWrite
    || Boolean(pendingConfirmation)
    || migrateStreaming.isPending
    || Boolean(pendingMigratedSend)
    || (turnState === 'idle' && (!draft.trim() || send.isPending))
    || turnState === 'pausing'
    || turnState === 'resuming';
  const runComposerAction = () => {
    if (turnState === 'idle') enqueueDraft();
    else if (turnState === 'running') interrupt.mutate();
    else if (turnState === 'paused') resume.mutate();
  };

  return <main className="agent-workbench-page">
    <aside className="agent-workbench-rail">
      <header><div><span className="eyebrow">AGENT WORKSPACE</span><h1>Agent 会话</h1></div><button className="primary" disabled={!canCreate || create.isPending} onClick={() => create.mutate()}><Plus size={15}/>{create.isPending ? '创建中…' : '新建会话'}</button></header>
      <div className="agent-workbench-list">{conversations.map((item, index) => <button key={item.id} className={item.id === selected?.id ? 'active' : ''} onClick={() => onNavigate(`/agent/conversations/${encodeURIComponent(item.id)}`)}><CircleDot size={13}/><span><b>{conversationName(item, index)}</b><small>{item.lifecycle === 'ACTIVE' ? '可继续会话' : '正在处理'}</small></span><ChevronRight size={13}/></button>)}</div>
      {!conversations.length && <div className="agent-workbench-rail-empty"><Bot size={25}/><b>还没有会话</b><span>{connectedProviders.length ? '选择供应商后新建一个会话开始协作。' : '请先完成至少一个模型供应商的连接测试。'}</span></div>}
      <section className="agent-workbench-model-card"><header><Settings2 size={14}/><b>新会话供应商</b></header>{connectedProviders.length ? <select aria-label="新会话供应商" value={newConversationProviderId} onChange={event => setNewConversationProviderId(event.target.value)}>{connectedProviders.map(item => <option key={item.id} value={item.id}>{providerLabel(item)}</option>)}</select> : <button className="secondary" onClick={onOpenModels}>前往模型配置</button>}<small>仅用于本次创建，不会修改其他会话。</small></section>
    </aside>
    <section className="agent-workbench-main">
      <header className="agent-workbench-header"><div><span className="eyebrow">DIRECT AGENT SESSION</span>{editing ? <div className="agent-title-edit"><input aria-label="会话标题" value={title} onChange={event => setTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && title.trim()) rename.mutate(); if (event.key === 'Escape') setEditing(false); }}/><button className="primary" disabled={!title.trim() || rename.isPending} onClick={() => rename.mutate()}>保存</button></div> : <h2>{selected ? conversationName(selected, conversations.indexOf(selected)) : '开始一个新的会话'}</h2>}{selected && <small className="agent-session-provider">当前供应商：{boundProviderInfo?.name ?? '未配置'}</small>}</div><div className="agent-header-actions">{selected && <><button type="button" aria-label="压缩上下文" title="压缩上下文" disabled={!canWrite || isGenerating || condense.isPending} onClick={() => condense.mutate()}><Minimize2 size={14}/></button><button type="button" aria-label="重命名会话" onClick={() => setEditing(true)}><Pencil size={14}/></button><button type="button" className="danger" aria-label="删除会话" disabled={remove.isPending} onClick={() => remove.mutate()}><Trash2 size={14}/></button></>}<button type="button" aria-label={drawerOpen ? '关闭工作区抽屉' : '打开工作区终端'} className={drawerOpen ? 'active' : ''} disabled={!runtime?.write_available} onClick={() => setDrawerOpen(value => !value)}><Terminal size={15}/></button></div></header>
      {runtime?.state === 'RECOVERING' ? <section className="agent-runtime-recover"><LoaderCircle size={18}/><div><b>运行环境正在恢复</b><span>{runtime.message || '会话列表和标题已保留，恢复后可继续使用。'}</span></div></section> : selected ? <ConversationSurface events={displayedEvents} liveText={liveText} isGenerating={isGenerating} requestStartedAt={requestStartedAt} requestSubmitting={send.isPending || rewrite.isPending} rewritePending={rewrite.isPending || Boolean(pendingRewrite)} onRewrite={requestRewrite} onFork={eventId => fork.mutate(eventId)}/> : <div className="agent-workbench-empty"><Bot size={32}/><b>新建会话开始协作</b><span>每个会话共享同一工作区，但保留独立的对话与事件记录。</span><button className="primary" disabled={!canCreate || create.isPending} onClick={() => create.mutate()}><Plus size={15}/>新建会话</button></div>}
      {selected && runtime?.state !== 'RECOVERING' && <div className={`agent-composer ${turnState !== 'idle' || pendingConfirmation ? 'busy' : ''}`}>
        {pendingConfirmation && <section className="agent-confirmation" aria-label="工具执行确认"><header><ShieldAlert size={17}/><div><b>工具正在等待你的确认</b><span>动作尚未执行。请核对整批内容后批准或拒绝。</span></div></header><div className="agent-confirmation-actions">{(pendingConfirmation.actions ?? []).map((action: AgentPendingConfirmationAction) => <article key={action.digest}><div><b>{action.summary || action.tool_name}</b><span>{action.security_risk || 'UNKNOWN'}</span></div>{Object.keys(action.arguments).length > 0 && <pre>{JSON.stringify(action.arguments, null, 2)}</pre>}</article>)}</div><textarea aria-label="工具确认理由" value={confirmationReason} maxLength={2000} placeholder="填写批准或拒绝理由…" onChange={event => setConfirmationReason(event.target.value)}/><footer><button type="button" className="danger" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(false)}><X size={14}/>拒绝整批</button><button type="button" className="primary" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(true)}><Check size={14}/>批准整批</button></footer></section>}
        <textarea aria-label="发送 Agent 消息" value={draft} maxLength={200_000} placeholder={pendingConfirmation ? '请先处理上方工具确认…' : turnState === 'paused' ? '已暂停：可继续，也可编辑上方消息重新思考…' : '给 Agent 发消息…'} disabled={!canWrite || Boolean(pendingConfirmation) || migrateStreaming.isPending || Boolean(pendingMigratedSend) || turnState === 'pausing' || turnState === 'resuming'} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (isImeComposition(event)) return; if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); enqueueDraft(); } }}/>
        {attachments.length > 0 && <div className="agent-attachments">{attachments.map(item => <span key={item.path}>{item.filename}<button aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(all => all.filter(candidate => candidate.path !== item.path))}>×</button></span>)}</div>}
        <footer>
          <div className="agent-composer-context">
            <input ref={attachmentInput} aria-label="上传附件" type="file" multiple hidden onChange={event => { for (const file of Array.from(event.target.files ?? [])) upload.mutate(file); event.currentTarget.value = ''; }}/>
            <button type="button" aria-label="添加附件" disabled={!canWrite || Boolean(pendingConfirmation) || upload.isPending} onClick={() => attachmentInput.current?.click()}><Plus size={17}/></button>
            {contextProgress && <span className="agent-context-progress" title={`当前上下文 ${contextProgress.used.toLocaleString()} / ${contextProgress.window.toLocaleString()} tokens`} aria-label={`上下文用量 ${contextProgress.percentage}%`}><i style={{ '--context-progress': `${contextProgress.percentage}%` } as CSSProperties}/><em>{contextProgress.usedLabel} / {contextProgress.windowLabel}</em></span>}
            {composerStatus && <span className="agent-composer-status">{composerStatus}</span>}
            {composerNote && <span className="agent-composer-note">{composerNote}</span>}
          </div>
          <div className="agent-composer-actions">
            <ComposerModelMenu providers={connectedProviders} providerId={conversationProviderId} modelName={activeConversationModelName} models={availableConversationModels} efforts={supportedEfforts} effort={reasoningEffort ?? selected?.reasoning_effort ?? contextQuery.data?.reasoning_effort ?? conversationModel?.default_reasoning_effort ?? ''} disabled={!canWrite || isGenerating || queuedMessages.length > 0 || Boolean(pendingConfirmation) || persistModel.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend)} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; const effort = model.default_reasoning_effort ?? null; setConversationProviderId(providerId); setConversationModelName(model.model_name); setReasoningEffort(effort); persistModel.mutate({ providerId, modelName: model.model_name, effort }); }} onModelChange={modelName => { const model = availableConversationModels.find(item => item.model_name === modelName); const effort = model?.default_reasoning_effort ?? null; setConversationModelName(modelName); setReasoningEffort(effort); persistModel.mutate({ providerId: conversationProviderId, modelName, effort }); }} onEffortChange={effort => { const nextEffort = effort || null; setReasoningEffort(nextEffort); persistModel.mutate({ providerId: conversationProviderId, modelName: activeConversationModelName, effort: nextEffort }); }}/>
            <button type="button" className={`agent-send${turnState === 'paused' || turnState === 'resuming' ? ' resume' : ''}`} aria-label={composerActionLabel} disabled={composerActionDisabled} onClick={runComposerAction}>{pendingConfirmation ? <ShieldAlert size={14}/> : turnState === 'idle' ? <Send size={16}/> : turnState === 'paused' || turnState === 'resuming' ? <Play size={12} fill="currentColor"/> : <Square size={10} fill="currentColor"/>}</button>
          </div>
        </footer>
      </div>}
      {visibleError && <p className="agent-workbench-error">{visibleError.message}</p>}
    </section>
    <aside className={`agent-workspace-drawer ${drawerOpen ? 'open' : ''}`}><header><div><span className="eyebrow">WORKSPACE</span><b>共享工作区终端</b></div><button type="button" aria-label="关闭终端抽屉" onClick={() => setDrawerOpen(false)}><X size={16}/></button></header>{drawerOpen ? <WorkspaceTerminal workspaceId={workspace.id}/> : <div className="agent-drawer-empty"><PanelRightOpen size={20}/><span>打开终端即可查看和操作 Agent 使用的共享工作区。</span></div>}</aside>
  </main>;
}
