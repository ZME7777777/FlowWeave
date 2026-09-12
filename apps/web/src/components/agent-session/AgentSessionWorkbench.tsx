import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import hljs from 'highlight.js/lib/common';
import { ArrowLeft, Bot, Boxes, Check, ChevronDown, ChevronRight, CircleDot, Copy, CornerDownRight, Download, Ellipsis, FileCode2, FileText, Folder, FolderOpen, FolderPlus, GitBranch, GripVertical, ImageIcon, Layers3, Link2, LoaderCircle, Maximize2, Minimize2, MonitorCog, PanelRightOpen, Play, Plus, Quote, Search, Send, ShieldAlert, Square, Trash2, X } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type ComponentPropsWithoutRef, type CSSProperties, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type UIEvent as ReactUIEvent, type WheelEvent as ReactWheelEvent } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ApiError, randomId, type AgentStreamEvent } from '../../api/client';
import { agentWorkspaceSessionGateway, type AgentSessionGateway } from '../../api/agent-session-gateway';
import { withoutDeploymentBase } from '../../deploymentPath';
import { agentWorkspaceSessionHost, type AgentSessionHost } from './session-host';
import {
  readConversationShellSnapshot,
  reconcileLogicalConversationCache,
  writeConversationShellSnapshot,
  type LogicalConversationCacheEntry,
} from './conversation-cache';
import { ConversationSurface, ConversationTaskPlan, type ConversationReference } from '../ConversationSurface';
import { useProductDialog } from '../ProductDialogContext';
import { useEscapeClose } from '../useEscapeClose';
import { selectCapabilityVersion, selectCapabilityVersions } from '../../utils/capabilitySelection';
import { SubagentAvatar } from '../SubagentAvatar';
import { subagentAvatarSlots, type SubagentAvatarSlot } from '../../utils/subagentAvatar';
import { workspaceFileChanges, workspaceRelativePath, type WorkspaceFileChange } from './fileChanges';
import type { AgentAttachment, AgentConversation, AgentPendingConfirmationAction, AgentSessionCapability, AgentSessionMcpReadiness, AgentSessionWorkDirectory, AgentSessionWorkDirectoryList, AgentSessionWorkspaceDetails, AgentWorkspaceReference, CapabilityAsset, CapabilityCollection, ModelProvider, OpenHandsConversationEvent, OpenHandsConversationEventBatch, ProviderModel, RuntimeTaskControlSnapshot, RuntimeTaskUsageSnapshot, WorkspaceGitCommitDetails, WorkspaceGitFileDiff } from '../../types';
import '../../pages/agent-workbench.css';
import '../../pages/agent-workbench-layout.css';

const WORKSPACE_FILE_TRANSFER_TYPE = 'application/x-flowweave-workspace-file-path';
const ACTIVE_EVENT_RECOVERY_INTERVAL_MS = 4_000;
const WORKSPACE_PATH_COPIED_DURATION_MS = 1_500;
const SESSION_PERFORMANCE_MARK_PREFIX = 'flowweave.agent-session.';
type StreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';
type TurnState = 'idle' | 'running' | 'pausing' | 'paused' | 'resuming';
interface QueuedMessage {
  id: string;
  scope: string;
  content: string;
  items: AgentAttachment[];
  references: ConversationReference[];
  workspaceReferences?: AgentWorkspaceReference[];
}
type FileSelection = NonNullable<AgentWorkspaceReference['selection']>;
interface BoundQueuedMessage extends QueuedMessage {
  bindingId: string;
  /**
   * UI-only marker for Command/Ctrl+Enter while a native Agent turn is in
   * progress. It never changes the transport payload: the server decides
   * whether the formal user event is appended during a running turn.
   */
  nativeGuidance?: boolean;
}
interface ConversationDraft { id: string; workDirectoryId?: string; displayName: string; capabilityVersionIds?: string[]; }
interface BootstrapRecovery {
  draft: ConversationDraft;
  message: QueuedMessage;
  providerId: string;
  modelName: string;
  reasoningEffort: string | null;
  attempts: number;
}
interface ConversationDraftRecovery {
  draft: ConversationDraft;
  content: string;
  attachments: AgentAttachment[];
  references: ConversationReference[];
  workspaceReferences?: AgentWorkspaceReference[];
  providerId: string;
  modelName: string;
  reasoningEffort: string | null;
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

function markSessionPerformance(phase: 'selected' | 'events-ready' | 'workspace-ready'): void {
  // Browser Performance Timeline is a local diagnostic only. It intentionally
  // contains no workspace, conversation, user, path, or message identifier.
  if (typeof performance !== 'undefined' && typeof performance.mark === 'function') {
    performance.mark(`${SESSION_PERFORMANCE_MARK_PREFIX}${phase}`);
  }
}

async function copyTextToClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Embedded or permission-restricted browsers can still permit the
      // user-gesture fallback below.
    }
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('Clipboard is unavailable');
}

type RuntimeTaskStatus = 'RUNNING' | 'COMPLETED' | 'ERROR';
interface RuntimeTaskProjection {
  id: string;
  actionEventId: string;
  toolCallId?: string;
  taskId?: string;
  subagentType: string;
  description?: string;
  startedAt?: string;
  finishedAt?: string;
  status: RuntimeTaskStatus;
  avatarSlot: SubagentAvatarSlot;
  outcome?: unknown;
  lastEventType?: string;
  lastEventAt?: string;
  lastEventSummary?: string;
  usage?: RuntimeTaskUsageSnapshot;
  control?: RuntimeTaskControlSnapshot;
}

/**
 * Build a UI-only projection from the formal OpenHands Task lifecycle.
 * The action/observation relationship is always the formal action_id or
 * tool_call_id; event order and text are deliberately never used as a join.
 */
function runtimeTasksFromEvents(events: OpenHandsConversationEvent[], usageSnapshots: RuntimeTaskUsageSnapshot[] = [], controlSnapshots: RuntimeTaskControlSnapshot[] = []): RuntimeTaskProjection[] {
  const avatarSlots = subagentAvatarSlots(events);
  const tasks = new Map<string, RuntimeTaskProjection>();
  const byToolCall = new Map<string, RuntimeTaskProjection>();
  for (const event of events) {
    const task = event.payload.runtime_task;
    if (!task || task.phase !== 'REQUESTED') continue;
    const actionEventId = task.action_event_id || event.id;
    if (!actionEventId || tasks.has(actionEventId)) continue;
    const item: RuntimeTaskProjection = {
      id: actionEventId, actionEventId, toolCallId: task.tool_call_id || undefined,
      subagentType: task.subagent_type || 'general-purpose',
      description: typeof task.description === 'string' ? task.description : undefined,
      startedAt: typeof event.payload.timestamp === 'string' ? event.payload.timestamp : undefined,
      status: 'RUNNING',
      avatarSlot: avatarSlots.get(actionEventId) ?? 'orbit',
      lastEventType: 'TaskAction',
      lastEventAt: typeof event.payload.timestamp === 'string' ? event.payload.timestamp : undefined,
      lastEventSummary: task.description || `调用 ${task.subagent_type || 'general-purpose'} 子智能体`,
    };
    tasks.set(actionEventId, item);
    if (item.toolCallId) byToolCall.set(item.toolCallId, item);
  }
  for (const event of events) {
    const task = event.payload.runtime_task;
    if (!task || (task.phase !== 'COMPLETED' && task.phase !== 'ERROR')) continue;
    const item = tasks.get(task.action_event_id)
      ?? (task.tool_call_id ? byToolCall.get(task.tool_call_id) : undefined);
    if (!item) continue;
    item.status = task.phase === 'COMPLETED' ? 'COMPLETED' : 'ERROR';
    item.taskId = task.task_id || item.taskId;
    item.subagentType = task.subagent_type || item.subagentType;
    item.finishedAt = typeof event.payload.timestamp === 'string' ? event.payload.timestamp : item.finishedAt;
    item.outcome = task.outcome;
    item.lastEventType = 'TaskObservation';
    item.lastEventAt = item.finishedAt;
    const observationSummary = taskOutcomeText(task.outcome);
    item.lastEventSummary = observationSummary
      ? observationSummary.slice(0, 240)
      : task.status ? `Task ${task.status}` : '子智能体已返回结果';
  }
  for (const event of events) {
    if (event.event_type !== 'ERROR' || event.payload.event_name !== 'AgentErrorEvent') continue;
    const toolCallId = typeof event.payload.tool_call_id === 'string' ? event.payload.tool_call_id : '';
    const item = toolCallId ? byToolCall.get(toolCallId) : undefined;
    if (!item) continue;
    item.status = 'ERROR';
    item.finishedAt = typeof event.payload.timestamp === 'string' ? event.payload.timestamp : item.finishedAt;
    item.outcome = { is_error: true, content: event.payload.content };
    item.lastEventType = 'AgentErrorEvent';
    item.lastEventAt = item.finishedAt;
    item.lastEventSummary = String(event.payload.content || '子智能体执行失败').trim().slice(0, 240);
  }
  const usageByTaskId = new Map(usageSnapshots.map(snapshot => [snapshot.task_id, snapshot]));
  const controlByAction = new Map(controlSnapshots.map(snapshot => [snapshot.action_event_id, snapshot]));
  const controlByTool = new Map(controlSnapshots.map(snapshot => [snapshot.tool_call_id, snapshot]));
  for (const item of tasks.values()) {
    if (item.taskId) item.usage = usageByTaskId.get(item.taskId);
    item.control = controlByAction.get(item.actionEventId)
      ?? (item.toolCallId ? controlByTool.get(item.toolCallId) : undefined);
  }
  return [...tasks.values()].sort((left, right) => (right.startedAt || '').localeCompare(left.startedAt || ''));
}

function runtimeTaskStatus(task: RuntimeTaskProjection, sessionStopped = false): string {
  if (sessionStopped && task.status === 'RUNNING') return '会话已停止，结果未返回';
  if (task.status === 'COMPLETED') return '已完成';
  if (task.status === 'ERROR') return '失败';
  const control = task.control?.control_state;
  if (control === 'INTERRUPT_CONFIRMING') return '正在确认中断';
  if (control === 'INTERRUPT_CONFIRMED') return '中断已确认，结果未返回';
  if (control === 'RUNTIME_REPLACING') return 'Runtime 恢复中';
  if (control === 'RECOVERED') return 'Runtime 已恢复，结果未确认';
  if (control === 'TIMEOUT_CHECKED') return '超时已处理，等待结果';
  if (control === 'RECOVERY_FAILED' || control === 'WATCHDOG_FAILED' || control === 'INTERRUPT_CONFIRMATION_FAILED') return '处理失败';
  if (task.control?.deadline_at && Date.parse(task.control.deadline_at) <= Date.now()) return '长时间无新事件';
  return '运行中';
}

function runtimeTaskIsActive(task: RuntimeTaskProjection, sessionStopped = false): boolean {
  if (task.status !== 'RUNNING') return false;
  if (sessionStopped) return false;
  return !['INTERRUPT_CONFIRMED', 'RECOVERED', 'RECOVERY_FAILED', 'WATCHDOG_FAILED', 'INTERRUPT_CONFIRMATION_FAILED'].includes(
    task.control?.control_state ?? '',
  );
}

function RuntimeTaskGlyph({ task, size = 15, sessionStopped = false }: { task: RuntimeTaskProjection; size?: number; sessionStopped?: boolean }) {
  const status = task.status === 'COMPLETED'
    ? 'completed'
    : runtimeTaskIsActive(task, sessionStopped) ? 'running' : 'error';
  return <SubagentAvatar slot={task.avatarSlot} status={status} size={size}/>;
}

function definitionStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    : [];
}

function taskOutcomeText(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const outcome = value as { content?: unknown };
  const content = outcome.content;
  if (typeof content === 'string') return content.trim().slice(0, 1_000) || undefined;
  if (!Array.isArray(content)) return undefined;
  return content.map(item => {
    if (!item || typeof item !== 'object') return '';
    const record = item as { text?: unknown };
    return typeof record.text === 'string' ? record.text : '';
  }).filter(Boolean).join('\n').slice(0, 1_000) || undefined;
}

function RuntimeTaskRecord({ task, definitions, sessionStopped }: {
  task: RuntimeTaskProjection; definitions: CapabilityAsset[]; sessionStopped?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (task.status !== 'RUNNING') return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [task.status]);
  const definition = definitions.find(item => item.capability_type === 'AGENT_DEFINITION' && item.capability_key === task.subagentType);
  const document = definition?.document && typeof definition.document === 'object' ? definition.document : {};
  const record = document as Record<string, unknown>;
  const tools = definitionStrings(record.tools);
  const skills = definitionStrings(record.skills);
  const outcome = taskOutcomeText(task.outcome);
  const nativeDefinition = !definition;
  const startedAt = task.startedAt ? Date.parse(task.startedAt) : NaN;
  const controlStopsClock = sessionStopped || ['INTERRUPT_CONFIRMED', 'RECOVERED', 'RECOVERY_FAILED', 'WATCHDOG_FAILED', 'INTERRUPT_CONFIRMATION_FAILED'].includes(task.control?.control_state ?? '');
  const finishedAt = task.finishedAt
    ? Date.parse(task.finishedAt)
    : controlStopsClock && task.control?.updated_at
      ? Date.parse(task.control.updated_at)
      : now;
  const elapsedSeconds = Number.isFinite(startedAt) && Number.isFinite(finishedAt)
    ? Math.max(0, Math.floor((finishedAt - startedAt) / 1000))
    : undefined;
  const elapsedLabel = elapsedSeconds === undefined
    ? '未知'
    : `${Math.floor(elapsedSeconds / 60)}分 ${String(elapsedSeconds % 60).padStart(2, '0')}秒`;
  const usage = task.usage;
  return <section className="agent-subagent-record" aria-label={`${task.subagentType} 任务详情`}>
      <header><div><span className="eyebrow">SUBAGENT</span><h2>{task.subagentType}</h2><p>{runtimeTaskStatus(task, sessionStopped)}{task.taskId ? ` · ${task.taskId}` : ''}</p></div></header>
      <section><h3>本次任务</h3><dl><dt>状态</dt><dd className={`agent-subagent-status ${runtimeTaskIsActive(task, sessionStopped) ? 'running' : task.status.toLowerCase()}`}>{runtimeTaskStatus(task, sessionStopped)}</dd><dt>任务说明</dt><dd>{task.description || 'OpenHands 未提供任务摘要。'}</dd><dt>子智能体类型</dt><dd><code>{task.subagentType}</code></dd><dt>运行耗时</dt><dd>{elapsedLabel}</dd>{task.startedAt && <><dt>开始时间</dt><dd>{new Date(task.startedAt).toLocaleString('zh-CN')}</dd></>}{task.finishedAt && <><dt>结束时间</dt><dd>{new Date(task.finishedAt).toLocaleString('zh-CN')}</dd></>}{task.lastEventType && <><dt>最近事件</dt><dd>{task.lastEventType}{task.lastEventAt ? ` · ${new Date(task.lastEventAt).toLocaleString('zh-CN')}` : ''}</dd></>}{task.lastEventSummary && <><dt>最近事件摘要</dt><dd>{task.lastEventSummary}</dd></>}{task.control && <><dt>平台处理</dt><dd>{task.control.control_state}{task.control.updated_at ? ` · ${new Date(task.control.updated_at).toLocaleString('zh-CN')}` : ''}</dd>{task.control.deadline_at && <><dt>观察截止</dt><dd>{new Date(task.control.deadline_at).toLocaleString('zh-CN')}</dd></>}{task.control.last_error && <><dt>处理错误</dt><dd>{task.control.last_error}</dd></>}</>}</dl></section>
      {usage && <section><h3>用量</h3><dl><dt>模型</dt><dd><code>{usage.model_name}</code></dd><dt>累计 Token</dt><dd>{(usage.prompt_tokens + usage.completion_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.reasoning_tokens).toLocaleString('zh-CN')}</dd><dt>输入 / 输出</dt><dd>{usage.prompt_tokens.toLocaleString('zh-CN')} / {usage.completion_tokens.toLocaleString('zh-CN')}</dd><dt>推理 Token</dt><dd>{usage.reasoning_tokens.toLocaleString('zh-CN')}</dd><dt>缓存读 / 写</dt><dd>{usage.cache_read_tokens.toLocaleString('zh-CN')} / {usage.cache_write_tokens.toLocaleString('zh-CN')}</dd><dt>当前轮 Token</dt><dd>{usage.per_turn_tokens.toLocaleString('zh-CN')}</dd><dt>上下文窗口</dt><dd>{usage.context_window.toLocaleString('zh-CN')}</dd><dt>累计费用</dt><dd>${usage.accumulated_cost.toFixed(6)}</dd></dl></section>}
      <section><h3>子智能体定义</h3>{nativeDefinition ? <p className="agent-subagent-note">这是 OpenHands 原生 <code>{task.subagentType}</code> 类型。当前正式事件未携带可版本化的 FlowWeave Agent Definition，因此不会把它伪装成自定义定义。</p> : <><p>{definition.description || '已发布的 FlowWeave Agent Definition。'}</p><dl><dt>已发布版本</dt><dd>{definition.version}</dd><dt>内容摘要</dt><dd><code>{definition.content_hash.slice(0, 16)}</code></dd>{tools.length > 0 && <><dt>允许工具</dt><dd>{tools.join('、')}</dd></>}{skills.length > 0 && <><dt>技能</dt><dd>{skills.join('、')}</dd></>}</dl><p className="agent-subagent-note">此处展示当前可读取的已发布定义。会话运行时使用的定义版本由 OpenHands 创建请求冻结，事件未提供版本 ID 时不据此声称两者相同。</p></>}</section>
      {outcome && <section><h3>执行结果</h3><pre>{outcome}</pre></section>}
    </section>
}

function RuntimeTaskTab({ tasks, definitions, selectedTaskId, onSelect, sessionStopped = false }: {
  tasks: RuntimeTaskProjection[]; definitions: CapabilityAsset[]; selectedTaskId?: string; onSelect: (taskId: string) => void; sessionStopped?: boolean;
}) {
  const running = tasks.filter(task => runtimeTaskIsActive(task, sessionStopped)).length;
  const selectedTask = tasks.find(task => task.id === selectedTaskId) ?? tasks[0];
  if (!selectedTask) return <div className="agent-drawer-empty"><b>暂无子智能体记录</b><span>本会话出现 OpenHands TaskAction 后，记录会显示在这里。</span></div>;
  return <section className="agent-subagent-tab" aria-label="子智能体记录">
    {sessionStopped && <div className="agent-subagent-session-notice" role="status"><Check size={15}/><span><b>会话已暂停</b><small>本次会话中的子智能体均已停止等待，尚未返回的结果不会继续生成。</small></span></div>}
    <aside className="agent-subagent-task-list"><header><div><span className="eyebrow">SUBAGENTS</span><b>子智能体记录</b></div><span className={running ? 'running' : ''}>{running ? `${running} 个运行中` : `${tasks.length} 个任务`}</span></header><div>{tasks.map(task => <button type="button" key={task.id} className={task.id === selectedTask.id ? 'active' : ''} aria-current={task.id === selectedTask.id ? 'true' : undefined} onClick={() => onSelect(task.id)}><RuntimeTaskGlyph task={task} size={13} sessionStopped={sessionStopped}/><span><b>{task.description || task.subagentType}</b><small>{task.subagentType} · {runtimeTaskStatus(task, sessionStopped)}</small></span><ChevronRight size={14}/></button>)}</div></aside>
    <RuntimeTaskRecord task={selectedTask} definitions={definitions} sessionStopped={sessionStopped}/>
  </section>;
}

interface WorkspaceConversationGroupProps {
  groupId: string;
  label: string;
  children: (visibleCount: number) => ReactNode;
  conversationCount: number;
  canCreateConversation?: boolean;
  onCreateConversation?: () => void;
  onDelete?: () => void;
}

function sessionQueryKey(host: AgentSessionHost, resource: string, ...identifiers: Array<string | undefined>) {
  return host.queryKey(resource, ...identifiers);
}

function conversationIsRunning(executionStatus: string | null | undefined): boolean {
  return [
    'starting', 'running', 'executing', 'stopping',
    'waiting_for_confirmation', 'pausing', 'resuming',
  ].includes(executionStatus?.trim().toLowerCase() ?? '');
}

function unreadConversationStorageKey(hostId: string, workspaceId: string): string {
  return `flowweave:agent-workspace-unread:${hostId}:${workspaceId}`;
}

function readUnreadConversationIds(storageKey: string | undefined): Set<string> {
  if (!storageKey) return new Set();
  try {
    const stored = window.localStorage.getItem(storageKey);
    const values: unknown = stored ? JSON.parse(stored) : [];
    return new Set(Array.isArray(values) ? values.filter((value): value is string => typeof value === 'string') : []);
  } catch {
    return new Set();
  }
}

function writeUnreadConversationIds(storageKey: string | undefined, conversationIds: Set<string>) {
  if (!storageKey) return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify([...conversationIds]));
  } catch {
    // Completion markers are browser-local presentation state.
  }
}

const MAX_BOOTSTRAP_RECONCILIATION_ATTEMPTS = 3;
const STREAM_IDLE_GRACE_MS = 5 * 60 * 1000;
// A newly accepted user event can briefly still report idle before OpenHands
// starts its Agent loop.  Wait for a bounded second native snapshot before
// treating idle as an abnormal end of a locally-running turn.
const ABNORMAL_IDLE_RECONCILIATION_MS = 4_000;

const AgentSessionGatewayContext = createContext<AgentSessionGateway>(agentWorkspaceSessionGateway);
const AgentSessionHostContext = createContext<AgentSessionHost>(agentWorkspaceSessionHost);

function useAgentSessionGateway(): AgentSessionGateway {
  return useContext(AgentSessionGatewayContext);
}

function useAgentSessionHost(): AgentSessionHost {
  return useContext(AgentSessionHostContext);
}

function WorkspaceConversationGroup({ groupId, label, children, conversationCount, canCreateConversation = false, onCreateConversation, onDelete }: WorkspaceConversationGroupProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [visibleCount, setVisibleCount] = useState(3);
  const contentId = `agent-workspace-group-${groupId}`;
  const canLoadMore = visibleCount < conversationCount;

  return <section className={`agent-workspace-group${collapsed ? ' collapsed' : ''}`}>
    <header>
      <button type="button" className="agent-workspace-group-toggle" aria-label={`${collapsed ? '展开' : '收起'}工作区 ${label}`} aria-expanded={!collapsed} aria-controls={contentId} onClick={() => setCollapsed(current => {
        if (!current) setVisibleCount(3);
        return !current;
      })}>
        <Folder size={14}/><span>{label}</span><ChevronDown size={13}/>
      </button>
      <div className="agent-workspace-group-actions">{onCreateConversation && <button type="button" aria-label={`在${label}中新建会话`} disabled={!canCreateConversation} onClick={onCreateConversation}><Plus size={13}/></button>}
      {onDelete && <button type="button" className="danger" aria-label={`删除工作区 ${label}`} onClick={onDelete}><Trash2 size={13}/></button>}</div>
    </header>
    <div id={contentId} className="agent-workspace-group-content" hidden={collapsed}>
      {children(visibleCount)}
      {canLoadMore && <button type="button" className="agent-workspace-group-more" onClick={() => setVisibleCount(current => current + 3)}>展开显示</button>}
    </div>
  </section>;
}

function ConversationStreamObserver({
  workspaceId, bindingId, enabled, onEvent, onStatus, onReconnect,
}: {
  workspaceId: string;
  bindingId: string;
  enabled: boolean;
  onEvent: (event: AgentStreamEvent) => void;
  onStatus: (status: StreamStatus) => void;
  onReconnect?: () => void;
}) {
  const { subscribe } = useAgentSessionGateway();
  const onEventRef = useRef(onEvent);
  const onStatusRef = useRef(onStatus);
  const onReconnectRef = useRef(onReconnect);
  const connectedRef = useRef(false);
  onEventRef.current = onEvent;
  onStatusRef.current = onStatus;
  onReconnectRef.current = onReconnect;
  useEffect(() => {
    if (!enabled) return;
    connectedRef.current = false;
    return subscribe(
      workspaceId,
      bindingId,
      event => onEventRef.current(event),
      status => {
        if (status === 'live') {
          if (connectedRef.current) onReconnectRef.current?.();
          connectedRef.current = true;
        }
        onStatusRef.current(status);
      },
    );
  }, [bindingId, enabled, subscribe, workspaceId]);
  return null;
}

function WorkspaceConversationRow({
  item, selectedBindingId, running, unread, runtimeWritable, removing, deleteDisabled, onSelect, onDelete,
}: {
  item: AgentConversation;
  selectedBindingId?: string;
  running: boolean;
  unread: boolean;
  runtimeWritable: boolean;
  removing: boolean;
  deleteDisabled: boolean;
  onSelect: () => void;
  onDelete?: () => void;
}) {
  return <div className="agent-workspace-conversation">
    <button type="button" className={`agent-workspace-conversation-select${item.id === selectedBindingId ? ' active' : ''}`} onClick={onSelect}>
      <CircleDot size={13}/><span><b>{conversationName(item)}</b></span>
    </button>
    {running && <LoaderCircle className="agent-workspace-conversation-running" role="img" aria-label="会话正在运行" size={14}/>}
    {!running && unread && <span className="agent-workspace-conversation-unread" role="img" aria-label="会话已完成，有未读回复" title="会话已完成，有未读回复"/>}
    {onDelete && !running && <button type="button" className="agent-workspace-conversation-delete" aria-label={`删除会话 ${conversationName(item)}`} title={deleteDisabled ? '会话运行中，请先停止' : '删除会话'} disabled={!runtimeWritable || deleteDisabled || removing} onClick={onDelete}><Trash2 size={13}/></button>}
  </div>;
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
    recovery.message.references = Array.isArray(recovery.message.references)
      ? recovery.message.references.filter((item): item is ConversationReference => Boolean(item)
        && typeof item.eventId === 'string' && typeof item.content === 'string')
      : [];
    recovery.message.workspaceReferences = workspaceReferencesFromStorage(
      recovery.message.workspaceReferences,
    );
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

function workspaceReferencesFromStorage(value: unknown): AgentWorkspaceReference[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.filter((item): item is AgentWorkspaceReference => {
    if (!item || typeof item !== 'object') return false;
    const candidate = item as Partial<AgentWorkspaceReference>;
    if (typeof candidate.path !== 'string' || !candidate.path.trim()
      || (candidate.kind !== 'file' && candidate.kind !== 'directory')
      || typeof candidate.display_name !== 'string' || !candidate.display_name.trim()
      || seen.has(workspaceReferenceKey(candidate as AgentWorkspaceReference))) return false;
    seen.add(workspaceReferenceKey(candidate as AgentWorkspaceReference));
    return true;
  }).slice(0, 20);
}

function readConversationDraft(storageKey: string): ConversationDraftRecovery | undefined {
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (!stored) return undefined;
    const value = JSON.parse(stored) as Partial<ConversationDraftRecovery>;
    if (!value.draft?.id || typeof value.draft.displayName !== 'string'
      || typeof value.content !== 'string' || !Array.isArray(value.attachments)
      || typeof value.providerId !== 'string' || typeof value.modelName !== 'string'
      || (value.reasoningEffort !== null && typeof value.reasoningEffort !== 'string')) return undefined;
    if (value.attachments.some(item => !item || typeof item.filename !== 'string'
      || typeof item.mime_type !== 'string' || typeof item.byte_size !== 'number'
      || typeof item.path !== 'string')) return undefined;
    const references = Array.isArray(value.references)
      ? value.references.filter((item): item is ConversationReference => Boolean(item)
        && typeof item.eventId === 'string' && typeof item.content === 'string')
      : [];
    return {
      ...value,
      references,
      workspaceReferences: workspaceReferencesFromStorage(value.workspaceReferences),
    } as ConversationDraftRecovery;
  } catch {
    return undefined;
  }
}

function writeConversationDraft(storageKey: string, recovery: ConversationDraftRecovery | undefined) {
  try {
    if (recovery) window.sessionStorage.setItem(storageKey, JSON.stringify(recovery));
    else window.sessionStorage.removeItem(storageKey);
  } catch {
    // This recovery aid deliberately remains browser-only. A first message is
    // still the only action that creates a server-side Conversation.
  }
}

function transferredFiles(transfer: DataTransfer): File[] {
  const files = Array.from(transfer.files);
  if (files.length) return files;
  return Array.from(transfer.items)
    .filter(item => item.kind === 'file')
    .map(item => item.getAsFile())
    .filter((file): file is File => file !== null);
}

function transferredWorkspacePaths(transfer: DataTransfer): string[] {
  if (!transfer.types.includes(WORKSPACE_FILE_TRANSFER_TYPE)) return [];
  return transfer.getData(WORKSPACE_FILE_TRANSFER_TYPE).split('\n').map(path => path.trim()).filter(Boolean);
}

function isBootstrapAmbiguous(error: Error): boolean {
  return [
    'AGENT_BOOTSTRAP_CREATION_AMBIGUOUS',
    'AGENT_BOOTSTRAP_DELIVERY_AMBIGUOUS',
  ].includes((error as ApiError).code);
}

type AgentCapabilityType = 'SKILL' | 'MCP' | 'PLUGIN' | 'CONTEXT' | 'AGENT_DEFINITION' | 'HOOK';
type ComposerSuggestionKind = 'SKILL' | 'COMMAND' | 'MCP' | 'NATIVE' | 'REFERENCE';
type NativeComposerAction = 'CONDENSE';
interface ComposerSuggestion {
  id: string;
  kind: ComposerSuggestionKind;
  token: string;
  label: string;
  detail: string;
  available?: boolean;
  nativeAction?: NativeComposerAction;
}

function composerTrigger(value: string): { sigil: '$' | '/' | '@'; query: string; start: number } | undefined {
  const match = /(?:^|\s)([$/@])([^\s]*)$/.exec(value);
  if (!match) return undefined;
  return { sigil: match[1] as '$' | '/' | '@', query: match[2], start: value.length - match[0].length + (match[0].startsWith(' ') ? 1 : 0) };
}

function stringValues(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : [];
}

function ComposerCapabilityAutocomplete({
  draft, suggestions, disabled, placeholder, onDraftChange, onPaste, onDropFiles, onDropWorkspaceFiles, onSubmit, onDirectSubmit, onManageCapabilities, onNativeAction, onWorkspaceReferenceSelected,
}: {
  draft: string; suggestions: ComposerSuggestion[]; disabled: boolean; placeholder: string;
  onDraftChange: (value: string) => void; onPaste: (event: ReactClipboardEvent<HTMLTextAreaElement>) => void; onDropFiles?: (files: File[]) => void; onDropWorkspaceFiles?: (paths: string[]) => void; onSubmit: () => void;
  onDirectSubmit?: () => void;
  onManageCapabilities?: () => void;
  onNativeAction?: (action: NativeComposerAction) => void;
  onWorkspaceReferenceSelected?: () => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const dragDepth = useRef(0);
  const [activeIndex, setActiveIndex] = useState(0);
  const [fileDragActive, setFileDragActive] = useState(false);
  const [inputOverflowing, setInputOverflowing] = useState(false);
  const resizeInput = useCallback(() => {
    const textarea = input.current;
    if (!textarea) return;
    // The first layout pass can run while the composer is still being sized.
    // Avoid treating that transient zero-width box as a long, wrapped draft.
    if (textarea.getBoundingClientRect().width <= 0) return;
    const styles = window.getComputedStyle(textarea);
    const lineHeight = Number.parseFloat(styles.lineHeight);
    const verticalPadding = Number.parseFloat(styles.paddingTop) + Number.parseFloat(styles.paddingBottom);
    const minHeight = Math.max(Number.parseFloat(styles.minHeight), lineHeight + verticalPadding);
    const maxHeight = lineHeight * 10 + verticalPadding;
    // Measure text in a standalone element rather than another textarea. A
    // cloned textarea may retain its old scroll-box state and report an
    // inflated scrollHeight after a long draft is shortened.
    const measurement = document.createElement('div');
    measurement.textContent = textarea.value || ' ';
    measurement.setAttribute('aria-hidden', 'true');
    measurement.style.setProperty('position', 'fixed', 'important');
    measurement.style.setProperty('visibility', 'hidden', 'important');
    measurement.style.setProperty('pointer-events', 'none', 'important');
    measurement.style.setProperty('box-sizing', 'content-box', 'important');
    measurement.style.setProperty('width', `${Math.max(0, textarea.clientWidth - verticalPadding)}px`, 'important');
    measurement.style.setProperty('margin', '0', 'important');
    measurement.style.setProperty('padding', '0', 'important');
    measurement.style.setProperty('border', '0', 'important');
    measurement.style.setProperty('font', styles.font, 'important');
    measurement.style.setProperty('font-kerning', styles.fontKerning, 'important');
    measurement.style.setProperty('letter-spacing', styles.letterSpacing, 'important');
    measurement.style.setProperty('line-height', styles.lineHeight, 'important');
    measurement.style.setProperty('white-space', 'pre-wrap', 'important');
    measurement.style.setProperty('overflow-wrap', 'anywhere', 'important');
    measurement.style.setProperty('word-break', 'break-word', 'important');
    document.body.append(measurement);
    const contentHeight = measurement.getBoundingClientRect().height + verticalPadding;
    measurement.remove();
    const height = Math.min(Math.max(contentHeight, minHeight), maxHeight);
    textarea.style.height = `${height}px`;
    textarea.style.setProperty('overflow-y', contentHeight > maxHeight + 1 ? 'auto' : 'hidden', 'important');
    setInputOverflowing(current => {
      const next = contentHeight > maxHeight + 1;
      return current === next ? current : next;
    });
  }, []);
  useLayoutEffect(() => { resizeInput(); }, [draft, resizeInput]);
  useEffect(() => {
    window.addEventListener('resize', resizeInput);
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(resizeInput);
    if (input.current?.parentElement) observer?.observe(input.current.parentElement);
    const layoutFrame = requestAnimationFrame(resizeInput);
    return () => {
      window.removeEventListener('resize', resizeInput);
      observer?.disconnect();
      cancelAnimationFrame(layoutFrame);
    };
  }, [resizeInput]);
  const trigger = composerTrigger(draft);
  const visible = useMemo(() => {
    if (!trigger) return [];
    const needle = trigger.query.toLocaleLowerCase();
    if (trigger.sigil === '@') {
      const reference: ComposerSuggestion = {
        id: 'reference:workspace-files',
        kind: 'REFERENCE',
        token: '@文件和目录',
        label: '文件和目录',
        detail: '引用当前容器工作区中的文件或目录',
      };
      return !needle || `${reference.token} ${reference.label} ${reference.detail}`.toLocaleLowerCase().includes(needle)
        ? [reference]
        : [];
    }
    return suggestions.filter(item => (trigger.sigil === '$' ? item.kind === 'SKILL' : item.kind !== 'SKILL')
      && (!needle || `${item.token} ${item.label} ${item.detail}`.toLocaleLowerCase().includes(needle)));
  }, [suggestions, trigger]);
  useEffect(() => setActiveIndex(0), [draft]);
  const select = (item: ComposerSuggestion) => {
    if (!trigger || item.available === false) return;
    if (item.kind === 'NATIVE' && item.nativeAction) {
      onDraftChange(`${draft.slice(0, trigger.start)}${draft.slice(trigger.start + trigger.query.length + 1)}`);
      onNativeAction?.(item.nativeAction);
      return;
    }
    if (item.kind === 'REFERENCE') {
      onDraftChange(`${draft.slice(0, trigger.start)}${draft.slice(trigger.start + trigger.query.length + 1)}`);
      onWorkspaceReferenceSelected?.();
      return;
    }
    onDraftChange(`${draft.slice(0, trigger.start)}${item.token} ${draft.slice(trigger.start + trigger.query.length + 1)}`);
    requestAnimationFrame(() => input.current?.focus());
  };
  const hasSuggestions = visible.length > 0;
  // A slash is an explicit request for a command or MCP.  Keep the picker
  // available for a draft before it has a native Conversation binding too.
  const showCapabilityManager = Boolean(trigger && trigger.sigil !== '@' && onManageCapabilities);
  const hasMenu = Boolean(trigger && (hasSuggestions || showCapabilityManager));
  const hasNativeSuggestions = suggestions.some(item => item.kind === 'NATIVE');
  const acceptsFileDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes('Files');
  const acceptsWorkspaceFileDrag = (event: ReactDragEvent<HTMLElement>) => event.dataTransfer.types.includes(WORKSPACE_FILE_TRANSFER_TYPE);
  const acceptsAttachmentDrag = (event: ReactDragEvent<HTMLElement>) => (Boolean(onDropFiles) && acceptsFileDrag(event)) || (Boolean(onDropWorkspaceFiles) && acceptsWorkspaceFileDrag(event));
  const clearFileDrag = () => { dragDepth.current = 0; setFileDragActive(false); };
  return <div className={`agent-composer-input${fileDragActive ? ' file-drag-active' : ''}`} onDragEnter={event => {
    if (disabled || !acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    dragDepth.current += 1;
    setFileDragActive(true);
  }} onDragOver={event => {
    if (disabled || !acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }} onDragLeave={() => {
    if (!fileDragActive) return;
    dragDepth.current -= 1;
    if (dragDepth.current <= 0) clearFileDrag();
  }} onDrop={event => {
    if (!acceptsAttachmentDrag(event)) return;
    event.preventDefault();
    const workspacePaths = disabled ? [] : transferredWorkspacePaths(event.dataTransfer);
    const files = disabled ? [] : transferredFiles(event.dataTransfer);
    clearFileDrag();
    if (workspacePaths.length) onDropWorkspaceFiles?.(workspacePaths);
    if (files.length) onDropFiles?.(files);
  }}>
    <textarea ref={input} data-overflowing={inputOverflowing || undefined} aria-label="发送 Agent 消息" aria-autocomplete="list" aria-controls={hasMenu ? 'agent-composer-capabilities' : undefined} aria-expanded={hasMenu} value={draft} maxLength={200_000} placeholder={placeholder} disabled={disabled} onChange={event => onDraftChange(event.target.value)} onPaste={onPaste} onKeyDown={event => {
      if (isImeComposition(event)) return;
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.shiftKey) {
        event.preventDefault();
        onDirectSubmit?.();
        return;
      }
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
    {fileDragActive && <div className="agent-composer-file-drop" aria-live="polite">松开以添加附件</div>}
    {hasMenu && <div id="agent-composer-capabilities" className="agent-composer-capability-menu" role="listbox" aria-label={trigger!.sigil === '$' ? '选择技能' : trigger!.sigil === '@' ? '选择引用类型' : hasNativeSuggestions ? '选择 OpenHands 原生能力、命令或 MCP' : '选择命令或 MCP'}>{hasSuggestions ? <>{visible.map((item, index) => <div className="agent-composer-capability-option" key={item.id}>{trigger!.sigil === '@' && index === 0 && <div className="agent-composer-capability-section">引用类型</div>}{trigger!.sigil === '/' && (index === 0 || visible[index - 1]?.kind === 'NATIVE') && item.kind !== 'NATIVE' && <div className="agent-composer-capability-section">MCP 与命令</div>}{trigger!.sigil === '/' && item.kind === 'NATIVE' && (index === 0 || visible[index - 1]?.kind !== 'NATIVE') && <div className="agent-composer-capability-section">OpenHands 原生能力</div>}<button type="button" role="option" aria-selected={index === activeIndex} aria-disabled={item.available === false || undefined} disabled={item.available === false} className={`${index === activeIndex ? 'active' : ''}${item.available === false ? ' unavailable' : ''}`} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => select(item)}><code>{item.token}</code><span><b>{item.label}</b><small>{item.detail}</small></span><em>{item.kind === 'SKILL' ? '技能' : item.kind === 'COMMAND' ? '命令' : item.kind === 'NATIVE' ? '原生' : item.kind === 'REFERENCE' ? '引用' : 'MCP'}</em></button></div>)}{trigger!.sigil === '/' && !visible.some(item => item.kind !== 'NATIVE') && <div className="agent-composer-capability-empty"><span><b>当前会话还没有加载命令或 MCP</b><small>先为此会话加载能力，随后可在这里用 / 选择并插入。</small></span></div>}</> : <div className="agent-composer-capability-empty"><span><b>{trigger!.sigil === '@' ? '当前没有匹配的引用类型' : !suggestions.length ? trigger!.sigil === '$' ? '当前会话还没有加载 Skill' : '当前会话还没有加载命令或 MCP' : '当前会话没有匹配的能力'}</b><small>{trigger!.sigil === '@' ? '调整输入关键词以筛选引用类型。' : !suggestions.length ? `先为此会话加载能力，随后可在这里用 ${trigger!.sigil} 选择并插入。` : '调整输入关键词，或管理当前会话能力。'}</small></span></div>}{showCapabilityManager && <div className="agent-composer-capability-manage"><span>管理当前会话能力</span><button type="button" onMouseDown={event => event.preventDefault()} onClick={onManageCapabilities}>管理</button></div>}</div>}
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
  const frozenCreationOnlyIds = useMemo(() => new Set(
    (conversationCapabilities ?? [])
      .filter(item => item.capability_type === 'CONTEXT' || item.capability_type === 'AGENT_DEFINITION' || item.capability_type === 'HOOK')
      .map(item => item.id),
  ), [conversationCapabilities]);
  const frozenContextCount = (conversationCapabilities ?? []).filter(item => item.capability_type === 'CONTEXT').length;
  const frozenAgentCount = (conversationCapabilities ?? []).filter(item => item.capability_type === 'AGENT_DEFINITION').length;
  const frozenHookCount = (conversationCapabilities ?? []).filter(item => item.capability_type === 'HOOK').length;
  const capabilities = useMemo(() => (catalogQuery.data ?? []).filter(item =>
    ['SKILL', 'MCP', 'PLUGIN', 'CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type)
    && (item.is_latest || frozenCreationOnlyIds.has(item.id))
    && (!bindingId || !['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) || frozenCreationOnlyIds.has(item.id)),
  ), [bindingId, catalogQuery.data, frozenCreationOnlyIds]);
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return capabilities.filter(item =>
      (kind === 'ALL' || item.capability_type === kind)
      && (!bindingId || !['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) || kind === item.capability_type)
      && (!needle || `${item.capability_key} ${item.description} ${item.filename}`.toLocaleLowerCase().includes(needle)),
    );
  }, [bindingId, capabilities, kind, query]);
  const byId = useMemo(() => new Map(capabilities.map(item => [item.id, item])), [capabilities]);
  const selectedMcpIds = useMemo(() => selectedIds.filter(id => byId.get(id)?.capability_type === 'MCP'), [byId, selectedIds]);
  const readonlyCreationOnly = Boolean(bindingId && (kind === 'CONTEXT' || kind === 'AGENT_DEFINITION' || kind === 'HOOK'));
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
        void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspaceId) });
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
      <header><div><span className="eyebrow">{bindingId ? 'CURRENT AGENT SESSION' : 'NEW AGENT SESSION'}</span><h2 id="agent-capability-title">能力</h2><p>{bindingId ? '可为当前会话注册新的已发布 Skill、MCP 或 Plugin。Context、Agent 与 Hook 仅在创建会话时冻结；可在对应标签查看已装配版本，但不能新增、编辑或删除。' : '新会话默认不挂载能力；在这里选择的版本只会冻结到即将创建的这一个会话。Context 会作为 OpenHands 系统级会话上下文，Agent 会作为原生子 Agent 定义，Hook 会通过官方 hook_config 注册。'}</p></div><button ref={closeButton} type="button" aria-label="关闭插件管理" disabled={save.isPending} onClick={onClose}><X size={18}/></button></header>
      <div className="agent-capability-toolbar"><div className="agent-capability-tabs">{([['ALL', 'all'], ['PLUGIN', 'plugin'], ['MCP', 'MCP'], ['SKILL', 'skill'], ['CONTEXT', 'context'], ['AGENT_DEFINITION', 'agent'], ['HOOK', 'hook']] as const).map(([value, label]) => <button type="button" key={value} className={kind === value ? 'active' : ''} onClick={() => setKind(value)}>{label}</button>)}</div><div className="agent-capability-toolbar-actions">{!readonlyCreationOnly && <button type="button" className="agent-capability-select-visible" disabled={!selectableVisibleIds.length} onClick={toggleVisible}>{allVisibleSelected ? '取消选择筛选结果' : `选择筛选结果 (${selectableVisibleIds.length})`}</button>}<label className="agent-capability-search"><Search size={15}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、说明或文件…"/></label></div></div>
      <div className="agent-capability-summary"><span>{readonlyCreationOnly ? <>已装配 <b>{kind === 'CONTEXT' ? frozenContextCount : kind === 'HOOK' ? frozenHookCount : frozenAgentCount}</b> 个 {kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'}</> : <>{bindingId ? '已注册' : '已选择'} <b>{selectedIds.length}</b> 项</>}</span><span>{readonlyCreationOnly ? `${kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'} 在创建会话时冻结，仅供查看，不能新增、编辑、取消或删除。` : bindingId ? '可继续注册新能力；已注册能力已锁定，不能取消或删除。' : '选择只作用于本次新会话，不会改变工作区或其他会话。'}</span></div>
      {(kind === 'ALL' || kind === 'SKILL') && collectionsQuery.data?.length ? <section className="agent-capability-collections"><span>Skill 组合</span><div>{collectionsQuery.data.map(collection => { const memberIds = collection.members.map(member => member.id).filter(id => byId.has(id)); const selected = memberIds.length > 0 && memberIds.every(id => selectedIds.includes(id)); return <button type="button" key={collection.id} className={selected ? 'selected' : ''} onClick={() => toggleCollection(collection)}><Layers3 size={13}/><b>{collection.name}</b><em>{collection.members.length}</em>{selected && <Check size={13}/>}</button>; })}</div></section> : null}
      <div className="agent-capability-list">{catalogQuery.isLoading ? <p>正在读取能力仓库…</p> : visible.length === 0 ? <p>{readonlyCreationOnly ? `此会话创建时没有装配 ${kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'}。` : '没有匹配的已发布能力。'}</p> : visible.map(item => { const checked = selectedIds.includes(item.id); const isFrozenCreationOnly = Boolean(bindingId && ['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) && frozenCreationOnlyIds.has(item.id)); const locked = Boolean(bindingId && (conversationCapabilities ?? []).some(enabled => enabled.id === item.id)); const isMcp = item.capability_type === 'MCP'; const readiness = mcpReadiness[item.id]; const checking = isMcp && checkingMcpIds.has(item.id); const readinessLabel = item.capability_type === 'CONTEXT' ? '系统上下文' : item.capability_type === 'AGENT_DEFINITION' ? 'Agent' : item.capability_type === 'HOOK' ? 'Hook' : !isMcp ? (item.capability_type === 'SKILL' ? '技能' : item.capability_type) : !checked ? 'MCP' : checking ? '检测中' : readiness?.state === 'READY' ? '已连接' : readiness?.error_kind === 'timeout' ? '连接超时' : readiness?.error_kind === 'connection' ? '连接失败' : '不可用'; const detail = isFrozenCreationOnly ? '创建会话时已装配，仅供查看，不能编辑或删除。' : locked ? '已注册到当前会话，不能取消或删除。' : item.capability_type === 'CONTEXT' ? '创建会话时冻结，并追加到 OpenHands 系统提示词后缀。' : item.capability_type === 'AGENT_DEFINITION' ? '创建会话时冻结为 OpenHands 原生子 Agent 定义。' : item.capability_type === 'HOOK' ? '创建会话时冻结，并通过 OpenHands 官方 hook_config 注册。' : isMcp && checked && readiness?.state === 'UNAVAILABLE' ? `MCP ${readinessLabel}；不会保存为新会话默认能力。` : item.description || item.filename; const lockedLabel = isFrozenCreationOnly ? `${item.capability_key}（创建时已装配，只读）` : `${item.capability_key}（已注册，不能取消）`; const lockedTitle = isFrozenCreationOnly ? '该能力在创建会话时已装配，仅供查看，不能新增、编辑或删除。' : '该能力已注册到当前会话，不能取消或删除。'; return <button type="button" key={item.id} className={`${checked ? 'selected' : ''}${locked ? ' locked' : ''}`} disabled={locked} aria-label={locked ? lockedLabel : undefined} title={locked ? lockedTitle : undefined} onClick={() => toggle(item)}><span className={`agent-capability-icon ${item.capability_type.toLowerCase()}`}><Boxes size={17}/></span><span><b>{item.capability_key}</b><small>{detail}</small><em className={isMcp && checked ? `mcp-status ${readiness?.state === 'READY' ? 'ready' : readiness?.state === 'UNAVAILABLE' ? 'unavailable' : 'checking'}` : undefined}>{readinessLabel}</em></span><i aria-hidden="true">{checked ? <Check size={15}/> : null}</i></button>; })}</div>
      {save.error && <div className="agent-capability-error"><p>{save.error.message}</p>{save.error instanceof ApiError && save.error.code === 'AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE' && onCreateEnhancedConversation && <div className="agent-capability-migration"><span>这条历史会话未在创建时注册原生能力市场。可新建一个空能力会话后，再按需选择要挂载的能力；此会话的历史内容会保留不变。</span><button type="button" className="secondary" disabled={save.isPending} onClick={() => onCreateEnhancedConversation([])}><Plus size={13}/>新建可使用能力的会话</button></div>}</div>}
      <footer>{!readonlyCreationOnly && selectedMcpIds.length > 0 && <button type="button" className="secondary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => void checkMcpReadiness(selectedMcpIds)}>重新检测 MCP</button>}<button type="button" className="secondary" disabled={save.isPending} onClick={onClose}>{readonlyCreationOnly ? '关闭' : '取消'}</button>{!readonlyCreationOnly && <button type="button" className="primary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => save.mutate()}>{save.isPending ? '正在注册…' : bindingId ? '注册到当前会话' : '用于新建会话'}</button>}</footer>
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

type TerminalContextMenu = { x: number; y: number; text: string; line: string };

function WorkspaceTerminal({ workspaceId, terminalInstanceId, bindingId, workDirectoryId, workingDirectory }: { workspaceId: string; terminalInstanceId: string; bindingId?: string; workDirectoryId?: string; workingDirectory: string }) {
  const { terminalUrl } = useAgentSessionGateway();
  const host = useRef<HTMLDivElement>(null);
  const sendTerminalInput = useRef<(data: string) => void>(() => undefined);
  const closeTerminalPane = useRef<() => void>(() => undefined);
  const [state, setState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [detail, setDetail] = useState('正在连接工作区终端…');
  const [contextMenu, setContextMenu] = useState<TerminalContextMenu>();

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
    // tmux's native terminal menu shares the mouse protocol with xterm. A
    // pointer move is therefore interpreted as another terminal mouse event
    // and dismisses that menu. Own the right click before xterm sees it and
    // render a persistent DOM menu whose commands are forwarded to tmux.
    const document = element.ownerDocument;
    const terminalTextAt = (event: MouseEvent) => {
      const cell = terminalCellForMouseEvent(event);
      const line = cell ? terminal.buffer.active.getLine(cell.row)?.translateToString(true) ?? '' : '';
      const selected = terminal.getSelection().trim();
      if (selected) return { text: selected, line };
      const index = cell ? Math.min(Math.max(cell.column, 0), Math.max(0, line.length - 1)) : 0;
      const before = line.slice(0, index + 1).match(/[^\s]+$/)?.[0] ?? '';
      const after = line.slice(index + 1).match(/^[^\s]+/)?.[0] ?? '';
      return { text: `${before}${after}`, line };
    };
    const openTerminalContextMenu = (event: MouseEvent) => {
      if (event.button !== 2 || !element.contains(event.target as Node)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const { text, line } = terminalTextAt(event);
      setContextMenu({ x: Math.min(event.clientX, window.innerWidth - 230), y: Math.min(event.clientY, window.innerHeight - 330), text, line });
      terminal.focus();
    };
    const closeTerminalContextMenu = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : undefined;
      if (event.button !== 2 && !target?.closest('.agent-terminal-context-menu')) setContextMenu(undefined);
    };
    const suppressTerminalRightMouseUp = (event: MouseEvent) => {
      if (event.button === 2 && element.contains(event.target as Node)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    const suppressBrowserContextMenu = (event: MouseEvent) => {
      if (!element.contains(event.target as Node)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const dismissTerminalContextMenu = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setContextMenu(undefined);
    };
    document.addEventListener('mousedown', openTerminalContextMenu, true);
    document.addEventListener('mousedown', closeTerminalContextMenu, true);
    document.addEventListener('mouseup', suppressTerminalRightMouseUp, true);
    document.addEventListener('contextmenu', suppressBrowserContextMenu, true);
    document.addEventListener('keydown', dismissTerminalContextMenu, true);
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
        if (event.code === 4401) {
          window.dispatchEvent(new Event('flowweave:auth-required'));
          return;
        }
        if (event.code === 4409 || attempts >= reconnectDelays.length) { setState('unavailable'); setDetail(event.reason || '终端暂时不可用'); return; }
        const delay = reconnectDelays[attempts];
        attempts += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };
    const input = terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    sendTerminalInput.current = data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); };
    closeTerminalPane.current = () => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'close-pane' })); };
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    void document.fonts?.ready.then(resize);
    return () => { disposed = true; sendTerminalInput.current = () => undefined; closeTerminalPane.current = () => undefined; if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer); if (resizeFrame !== undefined) window.cancelAnimationFrame(resizeFrame); if (remoteResizeTimer !== undefined) window.clearTimeout(remoteResizeTimer); observer.disconnect(); removeForcedSelectionListeners?.(); terminalScreen?.removeEventListener('mousedown', forceTextSelection, true); document.removeEventListener('mousedown', openTerminalContextMenu, true); document.removeEventListener('mousedown', closeTerminalContextMenu, true); document.removeEventListener('mouseup', suppressTerminalRightMouseUp, true); document.removeEventListener('contextmenu', suppressBrowserContextMenu, true); document.removeEventListener('keydown', dismissTerminalContextMenu, true); input.dispose(); socket?.close(1000); terminal.dispose(); };
  }, [bindingId, terminalInstanceId, terminalUrl, workDirectoryId, workingDirectory, workspaceId]);

  const closeMenu = () => setContextMenu(undefined);
  const runTmux = (command: string) => { sendTerminalInput.current(command); closeMenu(); };
  const killCurrentPane = () => { closeMenu(); closeTerminalPane.current(); };
  const copy = (value: string) => {
    closeMenu();
    if (value) void navigator.clipboard.writeText(value).catch(() => undefined);
  };
  const selectedText = contextMenu?.text || '选中内容';
  return <section className="agent-workspace-terminal"><header><span className={`terminal-dot ${state}`}/><span>{detail}</span></header><div ref={host} aria-label="Agent 工作区终端"/>{contextMenu && createPortal(<div className="agent-terminal-context-menu" role="menu" aria-label="终端操作菜单" style={{ left: contextMenu.x, top: contextMenu.y }} onMouseDown={event => event.stopPropagation()} onContextMenu={event => event.preventDefault()}><button type="button" role="menuitem" disabled={!contextMenu.text} onClick={() => copy(contextMenu.text)}>复制 “{selectedText}”</button><button type="button" role="menuitem" disabled={!contextMenu.line} onClick={() => copy(contextMenu.line)}>复制当前行</button><button type="button" role="menuitem" disabled={!contextMenu.text} onClick={() => runTmux(contextMenu.text)}>输入 “{selectedText}”</button><hr/><button type="button" role="menuitem" onClick={() => runTmux('\u0002%')}>左右分屏</button><button type="button" role="menuitem" onClick={() => runTmux('\u0002"')}>上下分屏</button><button type="button" role="menuitem" onClick={() => runTmux('\u0002m')}>标记窗格</button><button type="button" role="menuitem" className="danger" onClick={killCurrentPane}>关闭当前窗格</button></div>, document.body)}</section>;
}

function isTextPreviewable(path: string, mimeType = ''): boolean {
  return mimeType.startsWith('text/')
    || /^(?:application\/(?:json|xml|javascript|sql)|text\/(?:markdown|x-[^/]+))$/i.test(mimeType)
    || /\.(?:md|mdx|txt|json|ya?ml|toml|ini|conf|xml|html?|css|scss|less|tsx?|jsx?|py|java|kt|go|rs|rb|php|sh|zsh|sql|graphql|vue|svelte)$/i.test(path);
}

function filePreviewLanguage(path: string): string | undefined {
  const extension = path.split('.').pop()?.toLowerCase();
  switch (extension) {
    case 'py': case 'pyi': return 'python';
    case 'java': return 'java';
    case 'sql': return 'sql';
    case 'ts': case 'tsx': return 'typescript';
    case 'js': case 'jsx': case 'mjs': case 'cjs': return 'javascript';
    case 'json': return 'json';
    case 'yaml': case 'yml': return 'yaml';
    case 'xml': case 'svg': case 'html': case 'htm': case 'vue': return 'xml';
    case 'css': case 'scss': case 'less': return 'css';
    case 'sh': case 'bash': case 'zsh': return 'bash';
    case 'go': return 'go';
    case 'rs': return 'rust';
    case 'rb': return 'ruby';
    case 'php': return 'php';
    case 'c': case 'h': return 'c';
    case 'cc': case 'cpp': case 'cxx': case 'hpp': return 'cpp';
    case 'cs': return 'csharp';
    case 'kt': case 'kts': return 'kotlin';
    case 'toml': return 'ini';
    default: return undefined;
  }
}

function highlightedCode(value: string, language?: string): string {
  if (language && hljs.getLanguage(language)) return hljs.highlight(value, { language, ignoreIllegals: true }).value;
  return hljs.highlightAuto(value).value;
}

function WorkspaceMarkdownCode({ className, children, ...props }: ComponentPropsWithoutRef<'code'>) {
  const language = /language-([\w+-]+)/.exec(className ?? '')?.[1];
  if (!language) return <code className={className} {...props}>{children}</code>;
  const value = String(children).replace(/\n$/, '');
  return <code className={className} {...props} dangerouslySetInnerHTML={{ __html: highlightedCode(value, language) }}/>;
}

function textPosition(content: string, offset: number): { line: number; column: number } {
  const before = content.slice(0, offset);
  const line = before.split('\n').length;
  return { line, column: before.length - before.lastIndexOf('\n') };
}

function selectionFromPreview(root: HTMLElement, content: string): FileSelection | undefined {
  const selection = window.getSelection();
  if (!selection?.rangeCount || selection.isCollapsed) return undefined;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return undefined;
  const before = document.createRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length;
  const end = start + range.toString().length;
  if (!range.toString().trim() || end <= start) return undefined;
  const startPosition = textPosition(content, start);
  const endPosition = textPosition(content, end);
  return { start_line: startPosition.line, start_column: startPosition.column, end_line: endPosition.line, end_column: endPosition.column };
}

function previewOffset(content: string, line: number, column: number): number {
  const lines = content.split('\n');
  return lines.slice(0, line - 1).reduce((total, value) => total + value.length + 1, 0) + column - 1;
}

function previewTextRange(root: HTMLElement, content: string, selection: FileSelection): Range | undefined {
  const start = previewOffset(content, selection.start_line, selection.start_column);
  const end = previewOffset(content, selection.end_line, selection.end_column);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let consumed = 0;
  let startNode: Text | undefined; let startOffset = 0; let endNode: Text | undefined; let endOffset = 0;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const next = consumed + node.data.length;
    if (!startNode && start >= consumed && start <= next) { startNode = node; startOffset = start - consumed; }
    if (end >= consumed && end <= next) { endNode = node; endOffset = end - consumed; break; }
    consumed = next;
  }
  if (!startNode || !endNode) return undefined;
  const range = document.createRange();
  range.setStart(startNode, startOffset); range.setEnd(endNode, endOffset);
  return range;
}

function revealPreviewText(root: HTMLElement, scrollContainer: HTMLElement, content: string, selection: FileSelection): Range | undefined {
  const range = previewTextRange(root, content, selection);
  if (!range) return undefined;
  // Keep an already open file at its current reading position whenever the
  // destination is visible.  When it is outside the viewport, reveal it with
  // the smallest smooth scroll instead of forcing every jump to one fixed
  // offset in the file.
  const previewRect = scrollContainer.getBoundingClientRect();
  const rangeRect = Array.from(range.getClientRects()).at(0) ?? range.getBoundingClientRect();
  const rangeTop = scrollContainer.scrollTop + rangeRect.top - previewRect.top;
  const rangeBottom = rangeTop + Math.max(rangeRect.height, 1);
  const verticalPadding = Math.min(28, Math.max(12, scrollContainer.clientHeight * 0.08));
  let top = scrollContainer.scrollTop;
  if (rangeTop < scrollContainer.scrollTop + verticalPadding) top = Math.max(0, rangeTop - verticalPadding);
  else if (rangeBottom > scrollContainer.scrollTop + scrollContainer.clientHeight - verticalPadding) top = Math.max(0, rangeBottom - scrollContainer.clientHeight + verticalPadding);
  const rangeLeft = scrollContainer.scrollLeft + rangeRect.left - previewRect.left;
  const rangeRight = rangeLeft + Math.max(rangeRect.width, 1);
  const horizontalPadding = Math.min(28, Math.max(12, scrollContainer.clientWidth * 0.05));
  let left = scrollContainer.scrollLeft;
  if (rangeLeft < scrollContainer.scrollLeft + horizontalPadding) left = Math.max(0, rangeLeft - horizontalPadding);
  else if (rangeRight > scrollContainer.scrollLeft + scrollContainer.clientWidth - horizontalPadding) left = Math.max(0, rangeRight - scrollContainer.clientWidth + horizontalPadding);
  if (top !== scrollContainer.scrollTop || left !== scrollContainer.scrollLeft) {
    scrollContainer.scrollTo({ top, left, behavior: 'smooth' });
  }
  return range;
}

function selectPreviewText(root: HTMLElement, scrollContainer: HTMLElement, content: string, selection: FileSelection): boolean {
  const range = revealPreviewText(root, scrollContainer, content, selection);
  if (!range) return false;
  const browserSelection = window.getSelection();
  browserSelection?.removeAllRanges(); browserSelection?.addRange(range);
  return true;
}

function workspaceReferenceLabel(reference: AgentWorkspaceReference): string {
  const selection = reference.selection;
  return selection
    ? `${reference.relative_path ?? reference.display_name} · ${selection.start_line}:${selection.start_column}–${selection.end_line}:${selection.end_column}`
    : reference.kind === 'directory' ? '工作区目录 · 本地路径引用' : '工作区文件 · 本地路径引用';
}

function workspaceReferenceKey(reference: AgentWorkspaceReference): string {
  return `${reference.path}:${JSON.stringify(reference.selection ?? {})}`;
}

interface WorkspaceSelectionRect { left: number; top: number; width: number; height: number; }

function WorkspaceTextPreview({ path, content, highlight, highlightLine, onSelect }: { path: string; content: string; highlight?: FileSelection; highlightLine?: number; onSelect?: (selection: FileSelection) => void }) {
  const previewRef = useRef<HTMLDivElement>(null);
  const previewContentRef = useRef<HTMLElement>(null);
  const [selectionAction, setSelectionAction] = useState<{ selection: FileSelection; left: number; top: number; highlights: WorkspaceSelectionRect[] }>();
  const [lineHighlight, setLineHighlight] = useState<number>();
  const markdownPreview = /\.(?:md|mdx|markdown)$/i.test(path);
  const codeLines = useMemo(() => content.split('\n'), [content]);
  const positionSelectionAction = useCallback((selection: FileSelection, range: Range) => {
    const preview = previewRef.current;
    if (!preview) return;
    // A multi-line range's bounding box starts at the first selected line.
    // The last client rect keeps the action attached to the line where the
    // selection ends, which is where users expect a contextual action.
    const rects = Array.from(range.getClientRects()).filter(rect => rect.width > 0 && rect.height > 0);
    const anchor = rects.at(-1) ?? range.getBoundingClientRect();
    const previewRect = preview.getBoundingClientRect();
    const actionWidth = 136;
    const left = Math.min(
      Math.max(8, anchor.right - previewRect.left + preview.scrollLeft - actionWidth),
      Math.max(8, preview.scrollWidth - actionWidth - 8),
    );
    const top = anchor.top - previewRect.top + preview.scrollTop >= 36
      ? anchor.top - previewRect.top + preview.scrollTop - 36
      : anchor.bottom - previewRect.top + preview.scrollTop + 8;
    setSelectionAction({
      selection, left, top,
      // Native selection paint can disappear when the action is rendered.
      // Keep an independent visual layer inside this scrolling preview so the
      // selected file content remains unambiguously visible.
      highlights: rects.map(rect => ({
        left: rect.left - previewRect.left + preview.scrollLeft,
        top: rect.top - previewRect.top + preview.scrollTop,
        width: rect.width,
        height: rect.height,
      })),
    });
  }, []);
  useEffect(() => {
    const line = highlightLine && highlightLine > 0
      ? Math.min(highlightLine, Math.max(1, content.split('\n').length))
      : undefined;
    const lineSelection = line
      ? { start_line: line, start_column: 1, end_line: line, end_column: (content.split('\n')[line - 1]?.length ?? 0) + 1 }
      : undefined;
    const preview = previewRef.current;
    const source = previewContentRef.current;
    const selection = highlight ?? lineSelection;
    if (!selection || !preview || !source) return;
    setSelectionAction(undefined);
    if (highlight || markdownPreview) {
      if (!selectPreviewText(source, preview, content, selection)) return;
    } else {
      revealPreviewText(source, preview, content, selection);
      setLineHighlight(line);
    }
    const timer = window.setTimeout(() => {
      window.getSelection()?.removeAllRanges();
      preview.classList.remove('workspace-selection-flash');
      setLineHighlight(current => current === line ? undefined : current);
    }, 1_600);
    preview.classList.add('workspace-selection-flash');
    return () => window.clearTimeout(timer);
  }, [content, highlight, highlightLine, markdownPreview]);
  const captureSelection = () => {
    const preview = previewRef.current;
    const source = previewContentRef.current;
    if (!preview || !source) return;
    const selection = selectionFromPreview(source, content);
    const range = window.getSelection()?.rangeCount ? window.getSelection()?.getRangeAt(0) : undefined;
    if (!selection || !range) {
      setSelectionAction(undefined);
      return;
    }
    positionSelectionAction(selection, range);
  };
  const action = selectionAction && <><div className="agent-file-selection-highlights" aria-hidden="true">{selectionAction.highlights.map((rect, index) => <i key={`${rect.left}:${rect.top}:${index}`} style={rect}/>)}</div><button type="button" className="agent-file-selection-action" style={{ left: selectionAction.left, top: selectionAction.top }} onMouseDown={event => event.preventDefault()} onMouseUp={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); onSelect?.(selectionAction.selection); setSelectionAction(undefined); window.getSelection()?.removeAllRanges(); }}><Quote size={13}/>追加到会话</button></>;
  if (markdownPreview) {
    return <div ref={previewRef} className="agent-file-preview-selection" onMouseUp={captureSelection}>{action}<article ref={previewContentRef} className="agent-file-markdown-preview"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ code: WorkspaceMarkdownCode }}>{content}</ReactMarkdown></article></div>;
  }
  const language = filePreviewLanguage(path);
  return <div ref={previewRef} className="agent-file-preview-selection" onMouseUp={captureSelection}>{action}<div className={`agent-file-code-preview${language ? ' highlighted' : ''}`}><ol className="agent-file-line-numbers" aria-hidden="true">{codeLines.map((_, index) => <li key={index}>{index + 1}</li>)}</ol>{lineHighlight && <i className="agent-file-line-highlight" style={{ '--source-line': lineHighlight } as CSSProperties}/>}<code ref={previewContentRef} dangerouslySetInnerHTML={{ __html: highlightedCode(content, language) }}/></div></div>;
}

function sourceLineForDiffLine(lines: WorkspaceFileChange['lines'], selectedIndex: number): number {
  const selected = lines[selectedIndex];
  if (selected?.newLine) return selected.newLine;
  // A removed line no longer has a source coordinate. Land on the following
  // surviving/new line, or the preceding one when deletion reaches EOF.
  for (let index = selectedIndex + 1; index < lines.length; index += 1) {
    if (lines[index].newLine) return lines[index].newLine!;
  }
  for (let index = selectedIndex - 1; index >= 0; index -= 1) {
    if (lines[index].newLine) return lines[index].newLine!;
  }
  return 1;
}

function sourceLineForGitDiffLine(lines: GitDiffLine[], selectedIndex: number): number {
  const selected = lines[selectedIndex];
  if (selected?.newLine) return selected.newLine;
  // Git deletion rows have no coordinate in the working tree.  Keep source
  // navigation useful by landing on the nearest surviving current line.
  for (let index = selectedIndex + 1; index < lines.length; index += 1) {
    if (lines[index].newLine) return lines[index].newLine!;
  }
  for (let index = selectedIndex - 1; index >= 0; index -= 1) {
    if (lines[index].newLine) return lines[index].newLine!;
  }
  return 1;
}

function WorkspaceChangesReview({ changes, selectedId, onSelect, onOpenSource, workspaceRoot }: { changes: WorkspaceFileChange[]; selectedId?: string; onSelect: (id: string) => void; onOpenSource: (change: WorkspaceFileChange, line: number) => void; workspaceRoot?: string }) {
  const [mode, setMode] = useState<'unified' | 'split'>('split');
  const afterDiffRef = useRef<HTMLPreElement>(null);
  const beforeDiffContentRef = useRef<HTMLDivElement>(null);
  const selected = changes.find(change => change.id === selectedId) ?? changes[0];
  useEffect(() => { if (selected && selected.id !== selectedId) onSelect(selected.id); }, [onSelect, selected, selectedId]);
  useEffect(() => {
    if (mode !== 'split') return;
    if (afterDiffRef.current) {
      afterDiffRef.current.scrollTop = 0;
      afterDiffRef.current.scrollLeft = 0;
    }
    if (beforeDiffContentRef.current) beforeDiffContentRef.current.style.transform = 'translate(0, 0)';
  // Workspace tool tabs are restored from session storage before the selected
  // conversation's events have finished loading.  A restored changes tab can
  // therefore legitimately have no selected file on its first render.
  }, [mode, selected?.id]);
  const syncAfterDiffScroll = (event: ReactUIEvent<HTMLPreElement>) => {
    syncBeforeDiffOffset(event.currentTarget);
  };
  const syncBeforeDiffOffset = (pane: HTMLPreElement) => {
    const { scrollLeft, scrollTop } = pane;
    // Keep the read-only left pane aligned without scheduling a full React
    // render for every scroll tick; the latter caused a visible flash on long
    // diffs, especially when the scrollbar reached its lower boundary.
    if (beforeDiffContentRef.current) beforeDiffContentRef.current.style.transform = `translate(${-scrollLeft}px, ${-scrollTop}px)`;
  };
  const stopDiffOverscroll = (event: ReactWheelEvent<HTMLPreElement>) => {
    const pane = event.currentTarget;
    const maxTop = Math.max(0, pane.scrollHeight - pane.clientHeight);
    const maxLeft = Math.max(0, pane.scrollWidth - pane.clientWidth);
    const reachesVerticalBoundary = (event.deltaY < 0 && pane.scrollTop <= 0)
      || (event.deltaY > 0 && pane.scrollTop >= maxTop - 1);
    const reachesHorizontalBoundary = (event.deltaX < 0 && pane.scrollLeft <= 0)
      || (event.deltaX > 0 && pane.scrollLeft >= maxLeft - 1);
    // Never let a wheel gesture bubble out of the review pane.  In
    // particular, this prevents the remaining inertial delta at the bottom
    // of a diff from reaching the page and producing an elastic refresh.
    event.stopPropagation();
    if (reachesVerticalBoundary || reachesHorizontalBoundary) {
      event.preventDefault();
      pane.scrollTop = Math.min(maxTop, Math.max(0, pane.scrollTop + event.deltaY));
      pane.scrollLeft = Math.min(maxLeft, Math.max(0, pane.scrollLeft + event.deltaX));
      syncBeforeDiffOffset(pane);
    }
  };
  if (!selected) return <div className="agent-changes-empty"><b>没有可审查的文件改动</b><span>仅显示 OpenHands FileEditor 已成功写入、且带有原始前后内容的改动。</span></div>;
  const renderLine = (line: WorkspaceFileChange['lines'][number], side: 'before' | 'after', index: number) => {
    const shown = side === 'before' ? line.kind !== 'addition' : line.kind !== 'deletion';
    if (!shown) return <div className="agent-diff-line empty" aria-hidden="true"/>;
    const number = side === 'before' ? line.oldLine : line.newLine;
    return <button type="button" className={`agent-diff-line ${line.kind}`} key={`${side}:${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForDiffLine(selected.lines, index)} 行`} onClick={() => onOpenSource(selected, sourceLineForDiffLine(selected.lines, index))}><i>{number ?? ''}</i><code>{line.text || ' '}</code></button>;
  };
  return <section className="agent-changes-review">
    <nav className="agent-changes-file-list" aria-label="本轮修改的文件">
      <header><b>改动文件</b><span>{changes.length}</span></header>
      {changes.map(change => <button key={change.id} type="button" className={change.id === selected.id ? 'active' : ''} onClick={() => onSelect(change.id)}><FileText size={14}/><span title={workspaceRelativePath(change.path, workspaceRoot)}>{workspaceRelativePath(change.path, workspaceRoot)}</span><em><ins>{`+${change.additions}`}</ins><del>{`-${change.deletions}`}</del></em></button>)}
    </nav>
    <article className="agent-changes-diff">
      <header><div><b title={workspaceRelativePath(selected.path, workspaceRoot)}>{workspaceRelativePath(selected.path, workspaceRoot)}</b><small><ins>{`+${selected.additions}`}</ins><del>{`-${selected.deletions}`}</del></small></div><div className="agent-changes-diff-actions"><button type="button" className="agent-open-source-file" onClick={() => onOpenSource(selected, sourceLineForDiffLine(selected.lines, selected.lines.findIndex(line => line.kind !== 'deletion')))}><FileCode2 size={12}/>查看源文件</button><div className="agent-diff-mode"><button type="button" className={mode === 'unified' ? 'active' : ''} onClick={() => setMode('unified')}>统一</button><button type="button" className={mode === 'split' ? 'active' : ''} onClick={() => setMode('split')}>并排</button></div></div></header>
      {mode === 'unified' ? <pre className="agent-diff-unified" onWheelCapture={stopDiffOverscroll}>{selected.lines.map((line, index) => <button type="button" className={`agent-diff-line ${line.kind}`} key={`${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForDiffLine(selected.lines, index)} 行`} onClick={() => onOpenSource(selected, sourceLineForDiffLine(selected.lines, index))}><i>{line.oldLine ?? line.newLine ?? ''}</i><strong>{line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '-' : ' '}</strong><code>{line.text || ' '}</code></button>)}</pre> : <div className="agent-diff-split"><div className="agent-diff-before"><header>修改前</header><div ref={beforeDiffContentRef} className="agent-diff-before-content">{selected.lines.map((line, index) => renderLine(line, 'before', index))}</div></div><pre ref={afterDiffRef} onWheelCapture={stopDiffOverscroll} onScroll={syncAfterDiffScroll}><header>修改后</header>{selected.lines.map((line, index) => renderLine(line, 'after', index))}</pre></div>}
    </article>
  </section>;
}

function conversationSourceKind(source: ConversationSource): string {
  if (source.kind === 'url') return '链接';
  if (source.pending) return '待发送文件';
  return source.kind === 'image' ? '图片' : '文件';
}

function ConversationSourceGlyph({ source, size = 18 }: { source: ConversationSource; size?: number }) {
  if (source.kind === 'image' && source.attachment?.image_data_url) return <img src={source.attachment.image_data_url} alt=""/>;
  return source.kind === 'url' ? <Link2 size={size}/> : source.kind === 'image' ? <ImageIcon size={size}/> : <FileText size={size}/>;
}

function ConversationSourcesReview({ sources, onOpenAttachment }: { sources: ConversationSource[]; onOpenAttachment: (attachment: AgentAttachment) => void }) {
  if (!sources.length) return <div className="agent-changes-empty"><b>暂无来源</b><span>用户输入的链接、文件和图片会显示在这里。</span></div>;
  return <section className="agent-sources-review" aria-label="全部会话来源">
    {sources.map(source => {
      const content = <><span className="agent-source-icon"><ConversationSourceGlyph source={source}/></span><span><b title={source.label}>{source.label}</b><code title={source.url ?? source.attachment?.path}>{source.url ?? source.attachment?.path ?? '未提供来源路径'}</code><em>{source.pending ? '待随下一条消息发送' : '已附加到对话'}</em></span></>;
      return source.kind === 'url' && source.url
        ? <a key={source.id} href={source.url} target="_blank" rel="noopener noreferrer" title={`打开链接：${source.label}`}>{content}<ChevronRight size={15}/></a>
        : <button type="button" key={source.id} title={`在工作区查看：${source.label}`} disabled={!source.attachment} onClick={() => { if (source.attachment) onOpenAttachment(source.attachment); }}>{content}<ChevronRight size={15}/></button>;
    })}
  </section>;
}

function relativeWorkspacePath(path: string, root: string): string {
  return path === root ? '.' : path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
}

function workspaceSourcePath(path: string, workingDirectory?: string): string {
  let source = path.trim().replace(/\\/g, '/').replace(/\/+$|^\.\//g, '');
  const root = workingDirectory?.replace(/\/+$|^\.\//g, '');
  // Some FileEditor observations incorrectly preserve a worktree label before
  // the Runtime path.  The Runtime root is the only authoritative absolute
  // anchor; discard that display-only prefix before resolving the file.
  const runtimeMarker = 'runtime/workspace/project/';
  const runtimeOffset = source.indexOf(runtimeMarker);
  if (runtimeOffset >= 0) source = `/${source.slice(runtimeOffset)}`;
  if (!source || !root) return source;
  if (source === root || source.startsWith(`${root}/`)) return source;
  if (source.startsWith('/runtime/workspace/project/')) return source;
  // Git reports can use the work-directory display label as though it were a
  // path prefix (for example `slow-interface-fix/repos/service/...`).  That
  // label is already the last component of `root`; retaining it would point
  // outside the selected workspace as `${root}/slow-interface-fix/...`.
  const rootLabel = root.split('/').filter(Boolean).at(-1);
  let relative = source.replace(/^\/+/, '');
  if (rootLabel && relative === rootLabel) return root;
  if (rootLabel && relative.startsWith(`${rootLabel}/`)) relative = relative.slice(rootLabel.length + 1);
  return `${root}/${relative}`;
}

function sourceParentDirectories(path: string, root: string): string[] {
  if (path === root || !path.startsWith(`${root}/`)) return [];
  const parts = path.slice(root.length + 1).split('/').filter(Boolean);
  return parts.slice(0, -1).map((_, index) => `${root}/${parts.slice(0, index + 1).join('/')}`);
}

type WorkspaceEntry = { path: string; kind: 'file' | 'directory'; size: number; displayName?: string };
type WorkspaceDirectoryPage = { entries: WorkspaceEntry[]; nextCursor?: string };
type WorkspaceTreeNode = WorkspaceEntry & { name: string; children: WorkspaceTreeNode[] };

function WorkspaceReferencePicker({ entries, root, query, onQueryChange, selectedReferences, onApply, onClose }: {
  entries: WorkspaceEntry[];
  root: string;
  query: string;
  onQueryChange: (value: string) => void;
  selectedReferences: AgentWorkspaceReference[];
  onApply: (entries: WorkspaceEntry[]) => void;
  onClose: () => void;
}) {
  useEscapeClose(onClose);
  const nodes = useMemo(() => workspaceTree(entries, root), [entries, root]);
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(() => new Set(selectedReferences.map(item => item.path)));
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const needle = query.trim().toLocaleLowerCase();
  const matches = useCallback((node: WorkspaceTreeNode): boolean => {
    if (!needle) return true;
    const label = `${node.name} ${node.path}`.toLocaleLowerCase();
    return label.includes(needle) || node.children.some(matches);
  }, [needle]);
  useEffect(() => {
    if (!needle) return;
    const next = new Set<string>();
    const collect = (items: WorkspaceTreeNode[]) => items.forEach(node => {
      if (node.children.some(matches)) next.add(node.path);
      collect(node.children);
    });
    collect(nodes);
    setExpanded(current => new Set([...current, ...next]));
  }, [matches, needle, nodes]);
  const toggleExpanded = (path: string) => setExpanded(current => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path); else next.add(path);
    return next;
  });
  const toggleSelected = (path: string) => setSelectedPaths(current => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path); else next.add(path);
    return next;
  });
  const renderNodes = (items: WorkspaceTreeNode[], depth = 0): ReactNode => items.filter(matches).map(node => {
    const open = expanded.has(node.path) || Boolean(needle);
    const hasChildren = node.children.length > 0;
    return <div key={node.path} role="treeitem" aria-expanded={node.kind === 'directory' && hasChildren ? open : undefined}>
      <div className="agent-workspace-reference-tree-row" style={{ '--reference-depth': depth } as CSSProperties}>
        {node.kind === 'directory' && hasChildren
          ? <button type="button" className="agent-workspace-reference-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${node.name}`} onClick={() => toggleExpanded(node.path)}>{open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button>
          : <span className="agent-workspace-reference-spacer" aria-hidden="true"/>}
        <input type="checkbox" aria-label={`引用 ${node.kind === 'directory' ? '目录' : '文件'} ${node.path}`} checked={selectedPaths.has(node.path)} disabled={!selectedPaths.has(node.path) && selectedPaths.size >= 20} onChange={() => toggleSelected(node.path)}/>
        {node.kind === 'directory' ? open ? <FolderOpen size={16}/> : <Folder size={16}/> : <FileCode2 size={16}/>}
        <span title={node.path}>{node.name}</span>
        <small>{node.kind === 'directory' ? '目录' : '文件'}</small>
      </div>
      {node.kind === 'directory' && hasChildren && open && <div role="group">{renderNodes(node.children, depth + 1)}</div>}
    </div>;
  });
  const selectedEntries = entries.filter(entry => selectedPaths.has(entry.path));
  return <div className="agent-workspace-reference-picker-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="agent-workspace-reference-picker" role="dialog" aria-modal="true" aria-label="引用当前工作区文件或目录">
      <header><div><b>引用当前工作区</b><small>仅引用容器内路径，不上传或复制文件内容</small></div><button type="button" aria-label="关闭工作区引用" onClick={onClose}><X size={15}/></button></header>
      <input autoFocus aria-label="筛选工作区文件或目录" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="筛选文件或目录…"/>
      <section className="agent-workspace-reference-tree" aria-label="当前工作区文件树">
        <header><div><b>选择文件或目录</b><span>展开目录后可选择任意子目录或子文件</span></div><em>{selectedEntries.length}/20</em></header>
        <div role="tree">{nodes.length ? renderNodes(nodes) : <p>当前工作区没有可引用的文件或目录。</p>}</div>
      </section>
      <footer><button type="button" onClick={onClose}>取消</button><button type="button" className="primary" disabled={!selectedEntries.length} onClick={() => onApply(selectedEntries)}>添加 {selectedEntries.length ? `${selectedEntries.length} 个引用` : '引用'}</button></footer>
    </section>
  </div>;
}

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

function WorkspaceFileTree({ entries, root, selectedFile, selectedPaths, expanded, pagination, loadingDirectories, onExpandedChange, onDirectoriesChange, onLoadMore, onSelect, onSelectionChange, onActivateDirectory, onContextMenu }: { entries: WorkspaceEntry[]; root: string; selectedFile?: string; selectedPaths: Set<string>; expanded: Set<string>; pagination: Map<string, string | undefined>; loadingDirectories: Set<string>; onExpandedChange: (updater: (current: Set<string>) => Set<string>) => void; onDirectoriesChange: (updater: (current: string[]) => string[]) => void; onLoadMore: (parentPath?: string) => void; onSelect: (path?: string) => void; onSelectionChange: (paths: Set<string>) => void; onActivateDirectory: (path?: string) => void; onContextMenu: (path: string, kind: 'file' | 'directory', event: ReactMouseEvent<HTMLButtonElement>) => void }) {
  const nodes = useMemo(() => workspaceTree(entries, root), [entries, root]);
  const selectionAnchor = useRef<string | undefined>(undefined);
  const treeRef = useRef<HTMLDivElement>(null);
  const stickyOverlayRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const [stickyDirectoryPaths, setStickyDirectoryPaths] = useState<string[]>([]);
  useEffect(() => {
    const paths: string[] = [];
    const collect = (items: WorkspaceTreeNode[]) => items.forEach(node => { if (node.kind === 'directory') { paths.push(node.path); collect(node.children); } });
    collect(nodes);
    onDirectoriesChange(current => current.length === paths.length && current.every((path, index) => path === paths[index]) ? current : paths);
    // Manual expansion only tracks directories which are actually present in
    // the loaded tree. Source navigation has a separate, ordered expansion
    // state so a stale or malformed source path cannot keep causing 404s.
    onExpandedChange(current => {
      const next = new Set([...current].filter(path => paths.includes(path)));
      return next.size === current.size ? current : next;
    });
  }, [nodes, onDirectoriesChange, onExpandedChange]);
  const visibleNodes = useMemo(() => {
    const visible: Array<{ node: WorkspaceTreeNode; depth: number }> = [];
    const collect = (items: WorkspaceTreeNode[], depth = 0) => items.forEach(node => {
      visible.push({ node, depth });
      if (node.kind === 'directory' && expanded.has(node.path)) collect(node.children, depth + 1);
    });
    collect(nodes);
    return visible;
  }, [expanded, nodes]);
  const directoriesByPath = useMemo(() => {
    const directories = new Map<string, WorkspaceTreeNode>();
    const collect = (items: WorkspaceTreeNode[]) => items.forEach(node => {
      if (node.kind === 'directory') { directories.set(node.path, node); collect(node.children); }
    });
    collect(nodes);
    return directories;
  }, [nodes]);
  const stickyDirectoriesFor = useCallback((node: WorkspaceTreeNode): string[] => {
    const relative = relativeWorkspacePath(node.path, root);
    const parts = relative.split('/').filter(Boolean);
    const directoryParts = node.kind === 'directory' ? parts : parts.slice(0, -1);
    const paths: string[] = [];
    for (let index = 1; index <= directoryParts.length; index += 1) {
      const path = `${root}/${directoryParts.slice(0, index).join('/')}`;
      if (directoriesByPath.has(path) && expanded.has(path)) paths.push(path);
    }
    return paths;
  }, [directoriesByPath, expanded, root]);
  const updateStickyDirectories = useCallback(() => {
    const tree = treeRef.current;
    const overlay = stickyOverlayRef.current;
    if (!tree) return;
    // Keep the overlay attached to the scroll viewport synchronously.  Using
    // React state for this offset made it lag a frame during an upward wheel
    // gesture, so the directory path visibly drifted into the middle.
    if (overlay) overlay.style.transform = `translateY(${tree.scrollTop}px)`;
    if (tree.scrollTop <= 1) {
      setStickyDirectoryPaths(current => current.length ? [] : current);
      return;
    }
    // The source-tree viewport is the sole anchor. The overlay must never
    // decide its own contents: adding or removing a pinned directory changes
    // its height, which would otherwise make adjacent directories oscillate
    // at the boundary.
    const visibleTop = tree.getBoundingClientRect().top + 1;
    const firstVisible = visibleNodes.find(({ node }) => {
      const row = rowRefs.current.get(node.path);
      return row && row.getBoundingClientRect().bottom > visibleTop;
    });
    const next = firstVisible ? stickyDirectoriesFor(firstVisible.node) : [];
    setStickyDirectoryPaths(current => current.length === next.length && current.every((path, index) => path === next[index]) ? current : next);
  }, [stickyDirectoriesFor, visibleNodes]);
  useEffect(() => {
    updateStickyDirectories();
  }, [updateStickyDirectories]);
  useEffect(() => {
    if (!selectedFile) return;
    // Source navigation can expand several lazy directory pages before the
    // target row exists. Once it does, keep the selected source visible rather
    // than merely marking an off-screen row active.
    rowRefs.current.get(selectedFile)?.scrollIntoView({ block: 'nearest' });
  }, [selectedFile, visibleNodes]);
  useLayoutEffect(() => {
    const tree = treeRef.current;
    const overlay = stickyOverlayRef.current;
    if (tree && overlay) overlay.style.transform = `translateY(${tree.scrollTop}px)`;
  }, [stickyDirectoryPaths]);
  const selectEntry = (node: WorkspaceTreeNode, event: ReactMouseEvent<HTMLButtonElement>) => {
    const toggling = event.metaKey || event.ctrlKey;
    const anchorIndex = selectionAnchor.current ? visibleNodes.findIndex(item => item.node.path === selectionAnchor.current) : -1;
    const targetIndex = visibleNodes.findIndex(item => item.node.path === node.path);
    const deselectingOnlyEntry = !event.shiftKey && !toggling && selectedPaths.size === 1 && selectedPaths.has(node.path);
    if (deselectingOnlyEntry) {
      onSelectionChange(new Set());
      selectionAnchor.current = undefined;
      if (node.kind === 'file') onSelect(undefined);
      else onActivateDirectory(undefined);
      return;
    }
    if (event.shiftKey && anchorIndex >= 0 && targetIndex >= 0) {
      const range = visibleNodes.slice(Math.min(anchorIndex, targetIndex), Math.max(anchorIndex, targetIndex) + 1).map(item => item.node.path);
      onSelectionChange(new Set(toggling ? [...selectedPaths, ...range] : range));
    } else if (toggling) {
      const next = new Set(selectedPaths);
      if (next.has(node.path)) next.delete(node.path); else next.add(node.path);
      onSelectionChange(next);
      selectionAnchor.current = node.path;
    } else {
      onSelectionChange(new Set([node.path]));
      selectionAnchor.current = node.path;
    }
    if (node.kind === 'file') onSelect(node.path);
    else onActivateDirectory(node.path);
  };
  const renderNodes = () => visibleNodes.flatMap(({ node, depth }, index) => {
    const open = expanded.has(node.path);
    const nextDepth = visibleNodes[index + 1]?.depth;
    const hasMore = node.kind === 'directory' && open && Boolean(pagination.get(node.path));
    const loading = loadingDirectories.has(node.path);
    const isSubtreeEnd = nextDepth === undefined || nextDepth <= depth;
    return [<div key={node.path} ref={element => { if (element) rowRefs.current.set(node.path, element); else rowRefs.current.delete(node.path); }} className={`agent-file-tree-row${selectedPaths.has(node.path) ? ' selected' : ''}`} role="treeitem" aria-expanded={node.kind === 'directory' ? open : undefined} aria-level={depth + 1} aria-selected={selectedPaths.has(node.path)} style={{ '--tree-depth': depth } as CSSProperties}>
      {node.kind === 'directory' ? <button type="button" className="agent-file-tree-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${node.name}`} onClick={() => onExpandedChange(current => { const next = new Set(current); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); return next; })}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button> : <span className="agent-tree-spacer" aria-hidden="true"/>}
      <button type="button" draggable={node.kind === 'file'} className={`agent-file-tree-item ${node.kind}${selectedFile === node.path ? ' active' : ''}`} onDragStart={event => {
        if (node.kind !== 'file') return;
        event.dataTransfer.effectAllowed = 'copy';
        event.dataTransfer.setData(WORKSPACE_FILE_TRANSFER_TYPE, node.path);
      }} onClick={event => selectEntry(node, event)} onContextMenu={event => { event.preventDefault(); if (node.kind === 'directory') onActivateDirectory(node.path); onContextMenu(node.path, node.kind, event); }}>
        {node.kind === 'directory' ? open ? <FolderOpen size={14}/> : <Folder size={14}/> : <FileCode2 size={14}/>}
        <span>{node.name}</span>
        {node.kind === 'file' && <em>{node.size ? `${Math.ceil(node.size / 1024)} KB` : '0 KB'}</em>}
      </button>
    </div>, hasMore && isSubtreeEnd ? <button key={`${node.path}:more`} type="button" className="agent-file-tree-load-more" style={{ '--tree-depth': depth + 1 } as CSSProperties} disabled={loading} onClick={() => onLoadMore(node.path)}>{loading ? '正在加载…' : '加载更多'}</button> : null];
  });
  return <div ref={treeRef} className={`agent-file-tree${stickyDirectoryPaths.length ? ' has-sticky-path' : ''}`} role="tree" aria-label="工作区目录树" onScroll={updateStickyDirectories}>
    {stickyDirectoryPaths.length > 0 && <div ref={stickyOverlayRef} className="agent-file-tree-sticky-path" aria-label="当前文件所在目录">{stickyDirectoryPaths.map((path, depth) => {
      const directory = directoriesByPath.get(path);
      const open = expanded.has(path);
      return directory && <div key={path} className="agent-file-tree-row sticky-directory" role="presentation" style={{ '--tree-depth': depth } as CSSProperties}>
        <button type="button" className="agent-file-tree-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${directory.name}`} onClick={() => onExpandedChange(current => { const next = new Set(current); if (next.has(path)) next.delete(path); else next.add(path); return next; })}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button>
        <button type="button" className="agent-file-tree-item directory" title={`定位目录 ${directory.name}`} onClick={() => onActivateDirectory(path)}>{open ? <FolderOpen size={14}/> : <Folder size={14}/>}<span>{directory.name}</span></button>
      </div>;
    })}</div>}
    {nodes.length ? renderNodes() : <p>当前目录没有可展示的文件。</p>}
    {pagination.get('') && <button type="button" className="agent-file-tree-load-more" disabled={loadingDirectories.has('')} onClick={() => onLoadMore()}>{loadingDirectories.has('') ? '正在加载…' : '加载更多'}</button>}
  </div>;
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
    queryFn: () => api.workspaceDetails(workspaceId, { fullIndex: true }),
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
        <header><div><b>选择目录</b><span>从 {details?.root ?? '当前用户工作区'} 中选择一个或多个子目录</span></div><em>{selectedPaths.length}/20</em></header>
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

function selectedGitRepository(repositories: AgentSessionWorkspaceDetails['repositories'], selectedPath?: string, ...containerPaths: Array<string | undefined>) {
  return repositories
    .filter(item => !containerPaths.includes(item.path))
    .filter(item => selectedPath === item.path || Boolean(selectedPath?.startsWith(`${item.path}/`)))
    .sort((left, right) => right.path.length - left.path.length)[0];
}

function WorkspaceGitSidebar({ details, repository, loadLog, loadCommit, loadDiff, onOpenFileDiff, closedDiffEpoch }: {
  details: AgentSessionWorkspaceDetails;
  repository: AgentSessionWorkspaceDetails['repositories'][number];
  loadLog: (repositoryPath: string) => Promise<import('../../types').WorkspaceGitLog>;
  loadCommit: (repositoryPath: string, commit: string) => Promise<WorkspaceGitCommitDetails>;
  loadDiff: (repositoryPath: string, commit: string, path: string) => Promise<WorkspaceGitFileDiff>;
  onOpenFileDiff: (details: WorkspaceGitCommitDetails, diff: WorkspaceGitFileDiff) => void;
  closedDiffEpoch: number;
}) {
  const [selectedCommit, setSelectedCommit] = useState<string>();
  const [selectedCommitFile, setSelectedCommitFile] = useState<string>();
  const openedDiffRef = useRef<string | undefined>(undefined);
  useEffect(() => { setSelectedCommit(undefined); setSelectedCommitFile(undefined); }, [repository.path]);
  useEffect(() => {
    if (!closedDiffEpoch) return;
    // The central Diff tab was explicitly closed.  Its selected source file
    // must be selectable again; otherwise the cached opened key suppresses a
    // second click on that same file.
    openedDiffRef.current = undefined;
    setSelectedCommitFile(undefined);
  }, [closedDiffEpoch]);
  const logQuery = useQuery({
    queryKey: ['workspace-git-log', repository.path],
    queryFn: () => loadLog(repository.path),
    staleTime: 15_000,
  });
  const commitQuery = useQuery({
    queryKey: ['workspace-git-commit', repository.path, selectedCommit],
    queryFn: () => loadCommit(repository.path, selectedCommit!),
    enabled: Boolean(selectedCommit),
  });
  const diffQuery = useQuery({
    queryKey: ['workspace-git-diff', repository.path, selectedCommit, selectedCommitFile],
    queryFn: () => loadDiff(repository.path, selectedCommit!, selectedCommitFile!),
    enabled: Boolean(selectedCommit && selectedCommitFile),
  });
  useEffect(() => {
    if (!commitQuery.data || !diffQuery.data || !selectedCommitFile) return;
    const key = `${commitQuery.data.commit.id}:${selectedCommitFile}`;
    if (openedDiffRef.current === key) return;
    openedDiffRef.current = key;
    onOpenFileDiff(commitQuery.data, diffQuery.data);
  }, [commitQuery.data, diffQuery.data, onOpenFileDiff, selectedCommitFile]);
  return <aside className="agent-workspace-git-sidebar" aria-label="Git 提交历史">
    <header><div><span><GitBranch size={15}/>Git</span><b title={workspaceRelativePath(repository.path, details.root)}>{workspaceRelativePath(repository.path, details.root)}</b></div>{repository.branch && <em title="当前分支（只读，暂不支持切换）">{repository.branch}</em>}</header>
    {logQuery.isLoading ? <p className="agent-git-loading">正在读取提交历史…</p> : logQuery.isError ? <p className="agent-git-error">Git 历史读取失败。<button type="button" onClick={() => void logQuery.refetch()}>重试</button></p> : <>
      <div className="agent-git-log">{(logQuery.data?.commits ?? []).map(commit => <button key={commit.id} type="button" onClick={() => { setSelectedCommit(commit.id); setSelectedCommitFile(undefined); }}><b>{commit.subject || '（无提交说明）'}</b><span><code>{commit.short_id}</code><em>{commit.author}</em><time>{commit.date}</time></span></button>)}{!logQuery.data?.commits.length && <p>该仓库没有可展示的提交。</p>}</div>
      {selectedCommit && <WorkspaceGitCommitSidebarDetail details={commitQuery.data} loading={commitQuery.isLoading} error={commitQuery.isError} selectedPath={selectedCommitFile} onSelectFile={path => { openedDiffRef.current = undefined; setSelectedCommitFile(path); }} onClose={() => { setSelectedCommit(undefined); setSelectedCommitFile(undefined); }}/>}
    </>}
  </aside>;
}

type WorkspaceToolTab =
  | { id: 'files'; kind: 'files' }
  | { id: 'changes'; kind: 'changes' }
  | { id: 'sources'; kind: 'sources' }
  | { id: 'git'; kind: 'git'; details: WorkspaceGitCommitDetails; diff: WorkspaceGitFileDiff }
  | { id: 'subagents'; kind: 'subagents' }
  | { id: string; kind: 'terminal'; terminalInstanceId: string };
type WorkspaceToolScopeState = { tabs: WorkspaceToolTab[]; activeTabId?: string; selectedFile?: string; selectedChangeId?: string; selectedGitFile?: string; selectedRuntimeTaskId?: string };

type WorkspaceGitTreeNode = { name: string; path: string; status?: string; children: WorkspaceGitTreeNode[] };

function gitCommitTree(files: WorkspaceGitCommitDetails['files']): WorkspaceGitTreeNode[] {
  const roots: WorkspaceGitTreeNode[] = [];
  for (const file of files) {
    const parts = file.path.split('/').filter(Boolean);
    let children = roots;
    let parentPath = '';
    parts.forEach((name, index) => {
      const path = parentPath ? `${parentPath}/${name}` : name;
      const leaf = index === parts.length - 1;
      let node = children.find(item => item.path === path);
      if (!node) {
        node = { name, path, status: leaf ? file.status : undefined, children: [] };
        children.push(node);
      }
      children = node.children;
      parentPath = path;
    });
  }
  const sort = (nodes: WorkspaceGitTreeNode[]) => {
    nodes.sort((left, right) => {
      const typeOrder = Number(Boolean(right.children.length)) - Number(Boolean(left.children.length));
      return typeOrder || left.name.localeCompare(right.name);
    });
    nodes.forEach(node => sort(node.children));
  };
  sort(roots);
  return roots;
}

function gitCommitTimestamp(value?: string): string {
  if (!value) return '未提供';
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString('zh-CN', { hour12: false });
}

function WorkspaceGitCommitSidebarDetail({ details, loading, error, selectedPath, onSelectFile, onClose }: {
  details?: WorkspaceGitCommitDetails;
  loading: boolean;
  error: boolean;
  selectedPath?: string;
  onSelectFile: (path: string) => void;
  onClose: () => void;
}) {
  const tree = useMemo(() => gitCommitTree(details?.files ?? []), [details?.files]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filesHeight, setFilesHeight] = useState(260);
  useEffect(() => {
    // A commit tree is a focused, finite list.  Show all changed leaves
    // immediately; long or deeply nested paths scroll in the tree pane.
    const directories: string[] = [];
    const collect = (nodes: WorkspaceGitTreeNode[]) => nodes.forEach(node => {
      if (!node.children.length) return;
      directories.push(node.path);
      collect(node.children);
    });
    collect(tree);
    setExpanded(new Set(directories));
  }, [tree]);
  const resizeFiles = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const container = event.currentTarget.parentElement;
    if (!container) return;
    const startY = event.clientY;
    const startHeight = filesHeight;
    const move = (moveEvent: PointerEvent) => {
      const maximum = Math.max(180, container.clientHeight - 190);
      setFilesHeight(Math.max(140, Math.min(maximum, startHeight + moveEvent.clientY - startY)));
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      document.body.style.removeProperty('cursor');
      document.body.style.removeProperty('user-select');
    };
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop, { once: true });
  };
  const renderTree = (nodes: WorkspaceGitTreeNode[], depth = 0): ReactNode => nodes.map(node => {
    const directory = node.children.length > 0;
    const open = expanded.has(node.path);
    const toggleDirectory = () => setExpanded(current => {
      const next = new Set(current);
      if (next.has(node.path)) next.delete(node.path);
      else next.add(node.path);
      return next;
    });
    return <div key={node.path} className="agent-git-tree-node" role="treeitem" aria-expanded={directory ? open : undefined} aria-level={depth + 1}>
      <div className={`agent-git-tree-row${selectedPath === node.path ? ' active' : ''}`} style={{ '--git-tree-depth': depth } as CSSProperties}>
        {directory ? <button type="button" className="agent-git-tree-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${node.name}`} onClick={toggleDirectory}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button> : <span className="agent-git-tree-spacer" aria-hidden="true"/>}
        <button type="button" className="agent-git-tree-item" title={node.path} onClick={() => { if (directory) toggleDirectory(); else onSelectFile(node.path); }}><span>{directory ? open ? <FolderOpen size={14}/> : <Folder size={14}/> : <FileCode2 size={14}/>}</span><b>{node.name}</b>{node.status && <em>{node.status}</em>}</button>
      </div>
      {directory && open && <div role="group">{renderTree(node.children, depth + 1)}</div>}
    </div>;
  });
  if (loading) return <section className="agent-git-commit-detail"><p>正在读取提交详情…</p></section>;
  if (error) return <section className="agent-git-commit-detail"><p className="agent-git-error">提交详情读取失败。</p></section>;
  if (!details) return null;
  return <section className="agent-git-commit-detail agent-git-commit-sidebar" style={{ '--git-files-height': `${filesHeight}px` } as CSSProperties}>
    <header><button type="button" className="agent-git-commit-back" aria-label="返回提交历史" title="返回提交历史" onClick={onClose}><ArrowLeft size={14}/></button><div><b title={details.commit.subject}>{details.commit.subject || '（无提交说明）'}</b><span>{details.commit.short_id}</span></div></header>
    <section className="agent-git-commit-files">
      <header><div><b>提交文件</b><span><em>{details.files.length} 个文件</em></span></div></header>
      {tree.length ? <div className="agent-git-file-tree" role="tree" aria-label="提交文件树">{renderTree(tree)}</div> : <p>这个提交没有可展示的文件。</p>}
    </section>
    <div className="agent-git-review-resizer" role="separator" aria-label="调整文件与提交信息区域高度" aria-orientation="horizontal" onPointerDown={resizeFiles}/>
    <article className="agent-git-commit-info">
      <pre className="agent-git-commit-message">{details.commit.message || details.commit.subject || '（无提交说明）'}</pre>
      <dl><dt>Hash</dt><dd><code title={details.commit.id}>{details.commit.short_id}</code></dd><dt>作者</dt><dd>{details.commit.author || '未提供'}{details.commit.author_email && <> &lt;{details.commit.author_email}&gt;</>}</dd><dt>作者时间</dt><dd>{gitCommitTimestamp(details.commit.authored_at ?? details.commit.date)}</dd></dl>
    </article>
  </section>;
}

type GitDiffLine = { oldLine?: number; newLine?: number; kind: 'context' | 'addition' | 'deletion'; text: string };

function gitDiffLines(value: string): GitDiffLine[] {
  const lines: GitDiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  for (const sourceLine of value.split('\n')) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(sourceLine);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      inHunk = true;
      continue;
    }
    if (!inHunk || sourceLine === '\\ No newline at end of file') continue;
    if (sourceLine.startsWith('+')) {
      lines.push({ kind: 'addition', newLine, text: sourceLine.slice(1) });
      newLine += 1;
    } else if (sourceLine.startsWith('-')) {
      lines.push({ kind: 'deletion', oldLine, text: sourceLine.slice(1) });
      oldLine += 1;
    } else if (sourceLine.startsWith(' ')) {
      lines.push({ kind: 'context', oldLine, newLine, text: sourceLine.slice(1) });
      oldLine += 1;
      newLine += 1;
    }
  }
  return lines;
}

function WorkspaceGitFileDiffReview({ details, diff, onOpenSource }: { details: WorkspaceGitCommitDetails; diff: WorkspaceGitFileDiff; onOpenSource: (path: string, line: number) => void }) {
  const [mode, setMode] = useState<'unified' | 'split'>('split');
  const afterDiffRef = useRef<HTMLPreElement>(null);
  const beforeDiffContentRef = useRef<HTMLDivElement>(null);
  const lines = useMemo(() => gitDiffLines(diff.diff), [diff.diff]);
  // Git diff paths are always relative to the repository root, whereas the
  // shared workspace navigator expects a path in the current work-directory
  // coordinate system. Keep the Git root here so every navigation affordance
  // (including rows in split mode) uses the same absolute source path.
  const sourcePath = `${details.repository.path.replace(/\/+$/, '')}/${diff.path}`;
  useEffect(() => {
    if (mode !== 'split') return;
    if (afterDiffRef.current) {
      afterDiffRef.current.scrollTop = 0;
      afterDiffRef.current.scrollLeft = 0;
    }
    if (beforeDiffContentRef.current) beforeDiffContentRef.current.style.transform = 'translate(0, 0)';
  }, [diff.path, mode]);
  const syncBeforeDiffOffset = (pane: HTMLPreElement) => {
    if (beforeDiffContentRef.current) beforeDiffContentRef.current.style.transform = `translate(${-pane.scrollLeft}px, ${-pane.scrollTop}px)`;
  };
  const stopDiffOverscroll = (event: ReactWheelEvent<HTMLPreElement>) => {
    const pane = event.currentTarget;
    const maxTop = Math.max(0, pane.scrollHeight - pane.clientHeight);
    const maxLeft = Math.max(0, pane.scrollWidth - pane.clientWidth);
    const reachesVerticalBoundary = (event.deltaY < 0 && pane.scrollTop <= 0)
      || (event.deltaY > 0 && pane.scrollTop >= maxTop - 1);
    const reachesHorizontalBoundary = (event.deltaX < 0 && pane.scrollLeft <= 0)
      || (event.deltaX > 0 && pane.scrollLeft >= maxLeft - 1);
    event.stopPropagation();
    if (reachesVerticalBoundary || reachesHorizontalBoundary) {
      event.preventDefault();
      pane.scrollTop = Math.min(maxTop, Math.max(0, pane.scrollTop + event.deltaY));
      pane.scrollLeft = Math.min(maxLeft, Math.max(0, pane.scrollLeft + event.deltaX));
      syncBeforeDiffOffset(pane);
    }
  };
  const renderLine = (line: GitDiffLine, side: 'before' | 'after', index: number) => {
    const shown = side === 'before' ? line.kind !== 'addition' : line.kind !== 'deletion';
    if (!shown) return <div className="agent-diff-line empty" aria-hidden="true"/>;
    const number = side === 'before' ? line.oldLine : line.newLine;
    return <button type="button" className={`agent-diff-line ${line.kind}`} key={`${side}:${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForGitDiffLine(lines, index)} 行`} onClick={() => onOpenSource(sourcePath, sourceLineForGitDiffLine(lines, index))}><i>{number ?? ''}</i><code>{line.text || ' '}</code></button>;
  };
  return <section className="agent-git-file-diff-review">
    <header><div><b title={diff.path}>{diff.path}</b><small><code>{details.commit.short_id}</code><span title={details.commit.subject}>{details.commit.subject || '（无提交说明）'}</span>{diff.truncated && <em>已截断</em>}</small></div><div className="agent-changes-diff-actions"><button type="button" className="agent-open-source-file" onClick={() => onOpenSource(sourcePath, sourceLineForGitDiffLine(lines, lines.findIndex(line => line.kind !== 'deletion')))}><FileCode2 size={12}/>查看源文件</button>{lines.length > 0 && <div className="agent-diff-mode"><button type="button" className={mode === 'unified' ? 'active' : ''} onClick={() => setMode('unified')}>统一</button><button type="button" className={mode === 'split' ? 'active' : ''} onClick={() => setMode('split')}>并排</button></div>}</div></header>
    {!lines.length ? <p className="agent-git-file-diff-empty">{diff.diff ? '该文件没有可展示的文本行级 Diff。' : '该文件没有可显示的文本 Diff。'}</p> : mode === 'unified' ? <pre className="agent-diff-unified" onWheelCapture={stopDiffOverscroll}>{lines.map((line, index) => <button type="button" className={`agent-diff-line ${line.kind}`} key={`${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForGitDiffLine(lines, index)} 行`} onClick={() => onOpenSource(sourcePath, sourceLineForGitDiffLine(lines, index))}><i>{line.oldLine ?? line.newLine ?? ''}</i><strong>{line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '-' : ' '}</strong><code>{line.text || ' '}</code></button>)}</pre> : <div className="agent-diff-split"><div className="agent-diff-before"><header>修改前</header><div ref={beforeDiffContentRef} className="agent-diff-before-content">{lines.map((line, index) => renderLine(line, 'before', index))}</div></div><pre ref={afterDiffRef} onWheelCapture={stopDiffOverscroll} onScroll={event => syncBeforeDiffOffset(event.currentTarget)}><header>修改后</header>{lines.map((line, index) => renderLine(line, 'after', index))}</pre></div>}
  </section>;
}
type CandidateFilePreviewRequest = { key: string; filename: string; url: string };

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
  open, onOpen, onClose, onAddFileSelection, highlightedFileSelection, workspaceId, scopeKey, migrateFromScopeKey, bindingId, workDirectoryId, conversation, attachments, sources, attachmentRequest, candidatePreviewRequest, reviewChanges = [], reviewRequestId, sessionChanges = [], onReviewChanges, runtimeAvailable, runtimeTasks, agentDefinitions, sessionStopped,
}: {
  open: boolean; onOpen: () => void; onClose: () => void; onAddFileSelection?: (path: string, selection: FileSelection) => void; highlightedFileSelection?: { path: string; selection: FileSelection }; workspaceId: string; scopeKey: string; migrateFromScopeKey?: string; bindingId?: string; workDirectoryId?: string; conversation?: AgentConversation; attachments: AgentAttachment[]; sources: ConversationSource[]; attachmentRequest?: { key: string; attachment: AgentAttachment }; candidatePreviewRequest?: CandidateFilePreviewRequest; reviewChanges?: WorkspaceFileChange[]; reviewRequestId?: string; sessionChanges?: WorkspaceFileChange[]; onReviewChanges?: (changes: WorkspaceFileChange[]) => void; runtimeAvailable: boolean; runtimeTasks: RuntimeTaskProjection[]; agentDefinitions: CapabilityAsset[]; sessionStopped: boolean;
}) {
  const { api, fileUrl } = useAgentSessionGateway();
  const host = useAgentSessionHost();
  const dialog = useProductDialog();
  const queryClient = useQueryClient();
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
  const [toolMenuOpen, setToolMenuOpen] = useState(false);
  const toolMenuRef = useRef<HTMLDivElement>(null);
  useEscapeClose(() => {
    if (pendingTerminalClose && !closingTerminalId) setPendingTerminalClose(undefined);
  }, Boolean(pendingTerminalClose) && !closingTerminalId);
  const handledAttachmentRequestKey = useRef<string | undefined>(undefined);
  const handledCandidatePreviewRequestKey = useRef<string | undefined>(undefined);
  const handledReviewRequestId = useRef<string | undefined>(undefined);
  const [candidatePreview, setCandidatePreview] = useState<CandidateFilePreviewRequest>();
  const [sourceFileNavigation, setSourceFileNavigation] = useState<{ path: string; line: number }>();
  const [pendingSourceNavigation, setPendingSourceNavigation] = useState<{ path: string; line: number; directories: string[] }>();
  const [selectedEntryPaths, setSelectedEntryPaths] = useState<Set<string>>(new Set());
  const [activeDirectory, setActiveDirectory] = useState<string>();
  const [gitContextPath, setGitContextPath] = useState<string>();
  const [closedGitDiffEpoch, setClosedGitDiffEpoch] = useState(0);
  const [expandedFilePaths, setExpandedFilePaths] = useState<Set<string>>(new Set());
  const [fileDirectoryPaths, setFileDirectoryPaths] = useState<string[]>([]);
  const allFileDirectoriesExpanded = fileDirectoryPaths.length > 0 && fileDirectoryPaths.every(path => expandedFilePaths.has(path));
  const [entryMenu, setEntryMenu] = useState<{ path: string; kind: 'file' | 'directory'; x: number; y: number }>();
  useEscapeClose(() => setEntryMenu(undefined), Boolean(entryMenu));
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
    if (!open) {
      setFullScreen(false);
      setToolMenuOpen(false);
    }
  }, [open]);
  useEffect(() => {
    if (!toolMenuOpen) return;
    const closeWhenPointerLeavesMenu = (event: PointerEvent) => {
      if (event.target instanceof Node && !toolMenuRef.current?.contains(event.target)) setToolMenuOpen(false);
    };
    document.addEventListener('pointerdown', closeWhenPointerLeavesMenu, true);
    return () => document.removeEventListener('pointerdown', closeWhenPointerLeavesMenu, true);
  }, [toolMenuOpen]);
  useEffect(() => {
    if (!entryMenu) return;
    const close = () => setEntryMenu(undefined);
    window.addEventListener('pointerdown', close);
    window.addEventListener('resize', close);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('resize', close); };
  }, [entryMenu]);
  const detailsQuery = useQuery({
    queryKey: sessionQueryKey(host, 'workspace-details', workspaceId, bindingId, workDirectoryId),
    queryFn: () => api.workspaceDetails(workspaceId, { bindingId, workDirectoryId }),
    enabled: true,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const details = detailsQuery.data;
  const directoryQueryKey = sessionQueryKey(host, 'workspace-directory', workspaceId, bindingId, workDirectoryId);
  const rootDirectoryQuery = useQuery({
    queryKey: [...directoryQueryKey, 'root'],
    queryFn: () => api.workspaceDirectory(workspaceId, { bindingId, workDirectoryId }),
    enabled: Boolean(open && scopeState.activeTabId === 'files'),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const [directoryPages, setDirectoryPages] = useState<Map<string, WorkspaceDirectoryPage>>(() => new Map());
  const loadingDirectories = useRef(new Set<string>());
  const [loadingDirectoryPaths, setLoadingDirectoryPaths] = useState<Set<string>>(() => new Set());
  const loadDirectory = useCallback(async (parentPath?: string) => {
    const key = parentPath ?? '';
    if (loadingDirectories.current.has(key)) return;
    const current = directoryPages.get(key);
    if (current && !current.nextCursor) return;
    loadingDirectories.current.add(key);
    setLoadingDirectoryPaths(paths => new Set(paths).add(key));
    try {
      const page = await api.workspaceDirectory(workspaceId, { bindingId, workDirectoryId, parentPath, cursor: current?.nextCursor });
      setDirectoryPages(pages => {
        const next = new Map(pages);
        const previous = next.get(key);
        next.set(key, { entries: [...(previous?.entries ?? []), ...page.entries], nextCursor: page.next_cursor ?? undefined });
        return next;
      });
    } catch (reason) {
      setPanelError(reason instanceof Error ? reason.message : '读取目录失败');
    } finally {
      loadingDirectories.current.delete(key);
      setLoadingDirectoryPaths(paths => { const next = new Set(paths); next.delete(key); return next; });
    }
  }, [api, bindingId, directoryPages, workDirectoryId, workspaceId]);
  useEffect(() => {
    // A directory belongs to the currently authorized session scope. Never
    // reuse a selection after switching conversations or work directories.
    setActiveDirectory(undefined);
    setSelectedEntryPaths(new Set());
    setGitContextPath(undefined);
    setDirectoryPages(new Map());
    loadingDirectories.current.clear();
    setLoadingDirectoryPaths(new Set());
  }, [bindingId, details?.working_directory, workDirectoryId]);
  useEffect(() => {
    if (!rootDirectoryQuery.data) return;
    setDirectoryPages(pages => {
      const next = new Map(pages);
      next.set('', { entries: rootDirectoryQuery.data.entries, nextCursor: rootDirectoryQuery.data.next_cursor ?? undefined });
      return next;
    });
  }, [rootDirectoryQuery.data, rootDirectoryQuery.dataUpdatedAt]);
  useEffect(() => {
    if (!details || !open || scopeState.activeTabId !== 'files') return;
    const missing = [...expandedFilePaths].filter(path => !directoryPages.has(path));
    if (!missing.length) return;
    let cancelled = false;
    void Promise.all(missing.map(async parentPath => {
      if (!cancelled) await loadDirectory(parentPath);
    }));
    return () => { cancelled = true; };
  }, [details, directoryPages, expandedFilePaths, loadDirectory, open, scopeState.activeTabId]);
  useEffect(() => {
    if (!pendingSourceNavigation || !details || !open || scopeState.activeTabId !== 'files') return;
    const { directories, line, path } = pendingSourceNavigation;
    const root = details.working_directory.replace(/\/+$/, '');
    if (path !== root && !path.startsWith(`${root}/`)) {
      setPendingSourceNavigation(undefined);
      setPanelError('源文件不在当前工作目录中。');
      return;
    }
    const verifyEntry = (parentKey: string, childPath: string, kind: WorkspaceEntry['kind']) => {
      const page = directoryPages.get(parentKey);
      if (!page) return 'waiting' as const;
      const entry = page.entries.find(candidate => candidate.path === childPath && candidate.kind === kind);
      if (entry) return entry;
      if (page.nextCursor) {
        void loadDirectory(parentKey || undefined);
        return 'loading-more' as const;
      }
      return undefined;
    };
    for (let index = 0; index < directories.length; index += 1) {
      const directory = directories[index];
      const parentKey = index === 0 ? '' : directories[index - 1];
      const entry = verifyEntry(parentKey, directory, 'directory');
      if (entry === 'waiting' || entry === 'loading-more') return;
      if (!entry) {
        setPendingSourceNavigation(undefined);
        setExpandedFilePaths(current => new Set([...current].filter(candidate => !directories.includes(candidate))));
        setPanelError('未能在当前工作目录中找到源文件。');
        return;
      }
      if (!expandedFilePaths.has(directory)) {
        setExpandedFilePaths(current => new Set([...current, directory]));
        return;
      }
      if (!directoryPages.has(directory)) {
        void loadDirectory(directory);
        return;
      }
    }
    const parentKey = directories.at(-1) ?? '';
    const file = verifyEntry(parentKey, path, 'file');
    if (file === 'waiting' || file === 'loading-more') return;
    if (!file) {
      setPendingSourceNavigation(undefined);
      setPanelError('未能在当前工作目录中找到源文件。');
      return;
    }
    setSourceFileNavigation({ path: file.path, line });
    updateScope(current => ({ ...current, selectedFile: file.path }));
    setSelectedEntryPaths(new Set([file.path]));
    setPendingSourceNavigation(undefined);
  }, [details, directoryPages, expandedFilePaths, loadDirectory, open, pendingSourceNavigation, scopeState.activeTabId, updateScope]);
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
    const files = new Map<string, WorkspaceEntry>();
    for (const page of directoryPages.values()) {
      for (const entry of page.entries) files.set(entry.path, entry);
    }
    for (const attachment of attachments) {
      files.set(attachment.path, { path: attachment.path, kind: 'file', size: attachment.byte_size, displayName: attachment.filename });
    }
    return [...files.values()];
  }, [attachments, directoryPages]);
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
    if (!highlightedFileSelection) return;
    openFiles(highlightedFileSelection.path);
  }, [highlightedFileSelection, openFiles]);
  const openRuntimeTasks = useCallback((taskId?: string) => {
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'subagents') ? current.tabs : [...current.tabs, { id: 'subagents', kind: 'subagents' }],
      activeTabId: 'subagents',
      selectedRuntimeTaskId: taskId ?? current.selectedRuntimeTaskId ?? runtimeTasks[0]?.id,
    }));
    onOpen();
  }, [onOpen, runtimeTasks, updateScope]);
  const openChanges = useCallback((selectedChangeId?: string) => {
    if (!reviewChanges.length) return;
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'changes') ? current.tabs : [{ id: 'changes', kind: 'changes' }, ...current.tabs],
      activeTabId: 'changes',
      selectedChangeId: selectedChangeId ?? current.selectedChangeId ?? reviewChanges[0]?.id,
    }));
    onOpen();
  }, [onOpen, reviewChanges, updateScope]);
  const openSources = useCallback(() => {
    if (!sources.length) return;
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'sources') ? current.tabs : [...current.tabs, { id: 'sources', kind: 'sources' }],
      activeTabId: 'sources',
    }));
    onOpen();
  }, [onOpen, sources, updateScope]);
  const openGitFileDiff = useCallback((commitDetails: WorkspaceGitCommitDetails, diff: WorkspaceGitFileDiff) => {
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'git')
        ? current.tabs.map(tab => tab.kind === 'git' ? { id: 'git', kind: 'git', details: commitDetails, diff } : tab)
        : [...current.tabs, { id: 'git', kind: 'git', details: commitDetails, diff }],
      activeTabId: 'git',
      selectedGitFile: diff.path,
    }));
    onOpen();
  }, [onOpen, updateScope]);
  // Retained only for stale in-memory UI callbacks. Git history is not a
  // workspace-wide tool: it is rendered from the selected file-tree path.
  const openGitHistory = useCallback(() => undefined, []);
  useEffect(() => {
    // A review request is a one-shot navigation command.  Its data remains
    // available for the user to reopen review manually, but an unrelated
    // parent render must never reopen a tab the user has just closed.
    if (!reviewRequestId || !reviewChanges.length || handledReviewRequestId.current === reviewRequestId) return;
    handledReviewRequestId.current = reviewRequestId;
    openChanges(reviewChanges[0]?.id);
  }, [openChanges, reviewChanges, reviewRequestId]);
  useEffect(() => {
    // `onOpen` is supplied by the page and may change identity on a render.
    // Consume each request once so closing the file tab cannot immediately
    // reopen it from this effect.
    if (!attachmentRequest || handledAttachmentRequestKey.current === attachmentRequest.key) return;
    handledAttachmentRequestKey.current = attachmentRequest.key;
    setCandidatePreview(undefined);
    openFiles(attachmentRequest.attachment.path);
  }, [attachmentRequest, openFiles]);
  useEffect(() => {
    if (!candidatePreviewRequest || handledCandidatePreviewRequestKey.current === candidatePreviewRequest.key) return;
    handledCandidatePreviewRequestKey.current = candidatePreviewRequest.key;
    setCandidatePreview(candidatePreviewRequest);
    openFiles();
  }, [candidatePreviewRequest, openFiles]);
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
  const selectFile = (path?: string) => {
    setCandidatePreview(undefined);
    setGitContextPath(path);
    if (path) {
      openFiles(path);
      return;
    }
    updateScope(current => ({ ...current, selectedFile: undefined }));
  };
  const openSourcePath = useCallback((path: string, line: number) => {
    const sourcePath = workspaceSourcePath(path, details?.working_directory);
    if (!sourcePath) return;
    setCandidatePreview(undefined);
    setSourceFileNavigation(undefined);
    setPanelError('');
    setPendingSourceNavigation({
      path: sourcePath,
      line,
      directories: sourceParentDirectories(sourcePath, details?.working_directory ?? ''),
    });
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'files') ? current.tabs : [{ id: 'files', kind: 'files' }, ...current.tabs],
      activeTabId: 'files',
      selectedFile: undefined,
    }));
    onOpen();
  }, [details?.working_directory, onOpen, updateScope]);
  const openSourceFile = useCallback((change: WorkspaceFileChange, line: number) => {
    openSourcePath(change.path, line);
  }, [openSourcePath]);
  const selectedEntryRoots = useMemo(() => [...selectedEntryPaths].filter(path => ![...selectedEntryPaths].some(other => other !== path && path.startsWith(`${other}/`))), [selectedEntryPaths]);
  const removeEntries = async (items: Array<{ path: string; kind: 'file' | 'directory' }>) => {
    if (!api.deleteFile || !items.length) return;
    const directories = items.filter(item => item.kind === 'directory');
    if (!await dialog.confirm({ title: `删除 ${items.length} 项？`, message: directories.length ? '目录将递归删除其内容；此操作无法撤销。会话附件和不安全路径会受到保护。' : '所选文件会被永久删除，且无法撤销。', confirmLabel: '确认删除', tone: 'danger' })) return;
    setPanelError('');
    try {
      await Promise.all(items.map(item => api.deleteFile!(workspaceId, item.path, { bindingId, workDirectoryId, recursive: item.kind === 'directory' })));
      if (items.some(item => selectedFile === item.path || selectedFile?.startsWith(`${item.path}/`))) updateScope(current => ({ ...current, selectedFile: undefined }));
      setSelectedEntryPaths(new Set());
      await queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'workspace-details', workspaceId, bindingId, workDirectoryId) });
      setDirectoryPages(new Map());
      await queryClient.invalidateQueries({ queryKey: directoryQueryKey });
    } catch (reason) { setPanelError(reason instanceof Error ? reason.message : '删除文件失败'); }
  };
  const createEntry = async (parentPath: string, kind: 'FILE' | 'DIRECTORY') => {
    if (!api.createFile) return;
    const name = await dialog.prompt({ title: `新建${kind === 'DIRECTORY' ? '目录' : '文件'}`, message: '名称不能包含路径分隔符、隐藏前缀或上级目录。', inputLabel: '名称', placeholder: kind === 'DIRECTORY' ? '例如：assets' : '例如：notes.md', confirmLabel: '创建' });
    if (!name) return;
    setPanelError('');
    try {
      await api.createFile(workspaceId, parentPath, name, kind, { bindingId, workDirectoryId });
      await queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'workspace-details', workspaceId, bindingId, workDirectoryId) });
      setDirectoryPages(new Map());
      await queryClient.invalidateQueries({ queryKey: directoryQueryKey });
    } catch (reason) { setPanelError(reason instanceof Error ? reason.message : '创建失败'); }
  };
  const createAtActiveDirectory = (kind: 'FILE' | 'DIRECTORY') => {
    if (!details) return;
    const parentPath = details.scope.kind === 'ROOT' ? details.working_directory : activeDirectory;
    if (!parentPath) {
      setPanelError('请先在文件树中选择要新建内容的目录。');
      return;
    }
    void createEntry(parentPath, kind);
  };
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
      return {
        ...current,
        tabs,
        activeTabId: current.activeTabId === tab.id ? tabs[tabs.length - 1]?.id : current.activeTabId,
        selectedGitFile: tab.kind === 'git' ? undefined : current.selectedGitFile,
      };
    });
    if (tab.kind === 'git') setClosedGitDiffEpoch(current => current + 1);
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
  const filesTabIsActive = scopeState.activeTabId === 'files';
  const gitRepositoriesQuery = useQuery({
    queryKey: sessionQueryKey(host, 'workspace-git-repositories', workspaceId, bindingId, workDirectoryId, gitContextPath),
    queryFn: () => api.gitRepositories(workspaceId, { bindingId, workDirectoryId }),
    enabled: Boolean(fullScreen && filesTabIsActive && gitContextPath),
    staleTime: 15_000,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const gitRepository = useMemo(() => selectedGitRepository(gitRepositoriesQuery.data?.repositories ?? [], gitContextPath, details?.root, details?.working_directory), [details?.root, details?.working_directory, gitContextPath, gitRepositoriesQuery.data?.repositories]);
  const gitSidebarVisible = fullScreen && filesTabIsActive && Boolean(gitRepository);
  const gitOptions = { bindingId, workDirectoryId };
  const sshRemoteReady = Boolean(
    details?.ide.gateway.supported
    && details.ide.gateway.host
    && details.ide.gateway.port
    && details.ide.gateway.path,
  );
  const conversationUsage = conversation?.usage;
  const sessionChangeAdditions = sessionChanges.reduce((total, change) => total + change.additions, 0);
  const sessionChangeDeletions = sessionChanges.reduce((total, change) => total + change.deletions, 0);
  const visibleSources = sources.slice(0, 3);
  const summary = details && <section className="agent-workspace-overview">
    {conversation && <article className="agent-workspace-conversation-config"><Bot size={16}/><div><small>会话用量</small><p className="agent-workspace-usage-line"><span>{`累计 ${(conversationUsage?.total_tokens ?? 0).toLocaleString('zh-CN')} Token`}</span><span>{`$${(conversationUsage?.accumulated_cost ?? 0).toFixed(6)}`}</span></p></div></article>}
    {conversation && <article className="agent-workspace-changes"><FileText size={16}/><div><small>变更</small><button type="button" disabled={!sessionChanges.length} onClick={() => onReviewChanges?.(sessionChanges)}><span><b>{sessionChanges.length ? `${sessionChanges.length} 个文件已更改` : '暂无变更'}</b>{sessionChanges.length > 0 && <em><ins>{`+${sessionChangeAdditions}`}</ins><del>{`-${sessionChangeDeletions}`}</del></em>}</span><ChevronRight size={13}/></button></div></article>}
    {runtimeTasks.length > 0 && <article className="agent-workspace-subagents"><Bot size={16}/><div><small>子智能体</small><button type="button" onClick={() => openRuntimeTasks()}><b>{runtimeTasks.filter(task => runtimeTaskIsActive(task, sessionStopped)).length ? `${runtimeTasks.filter(task => runtimeTaskIsActive(task, sessionStopped)).length} 个运行中` : `${runtimeTasks.length} 个任务`}</b><ChevronRight size={13}/></button><div className="agent-workspace-subagent-glyphs" aria-label={`${runtimeTasks.length} 个子智能体任务`}>{runtimeTasks.slice(0, 5).map((task, index) => <button type="button" key={task.id} aria-label={`查看第 ${index + 1} 个子智能体任务：${runtimeTaskStatus(task, sessionStopped)}`} onClick={() => openRuntimeTasks(task.id)}><RuntimeTaskGlyph task={task} sessionStopped={sessionStopped}/></button>)}{runtimeTasks.length > 5 && <button type="button" className="agent-subagent-overflow" aria-label={`查看其余 ${runtimeTasks.length - 5} 个子智能体任务`} onClick={() => openRuntimeTasks()}>+{runtimeTasks.length - 5}</button>}</div></div></article>}
    {/*
      Git 仓库概览暂时隐藏：当前仅罗列工作区中的仓库路径、分支、提交与远端地址，
      对用户的下一步操作帮助有限。保留原始实现，待补充状态、差异和常用 Git 操作后再恢复。
      <article><GitBranch size={16}/><div><small>Git 仓库</small>{details.repositories.length ? details.repositories.map(repository => <p key={repository.path}><b>{relativeWorkspacePath(repository.path, details.root)}</b>{repository.branch && <span>{repository.branch}</span>}{repository.head && <em>{repository.head.slice(0, 12)}</em>}{repository.remote && <code>{repository.remote}</code>}</p>) : <p>当前目录未检测到 Git 仓库。</p>}</div></article>
    */}
    <article className="agent-workspace-ide"><MonitorCog size={16}/><div><small>IDEA / Gateway</small><b>{details.ide.gateway.status}</b>{sshRemoteReady ? <button type="button" className="agent-ssh-access-trigger" onClick={() => setSshAccessOpen(true)}>SSH 接入说明<ChevronRight size={13}/></button> : <code>{details.ide.workspace_path}</code>}<p>{details.ide.gateway.note}</p></div></article>
    <article className="agent-workspace-sources"><Link2 size={16}/><div><small>来源</small>{sources.length ? <><div className="agent-workspace-source-list">{visibleSources.map(source => <button type="button" key={source.id} title={`查看来源详情：${source.label}`} onClick={openSources}><span className="agent-workspace-source-glyph"><ConversationSourceGlyph source={source} size={12}/></span><span><b>{source.label}</b><em>{conversationSourceKind(source)}</em></span></button>)}</div>{sources.length > visibleSources.length && <button type="button" className="agent-workspace-view-all-sources" onClick={openSources}><Link2 size={12}/><span>查看全部</span><em>{sources.length}</em><ChevronRight size={13}/></button>}</> : <p>用户输入的链接、文件和图片会集中显示在这里。</p>}</div></article>
  </section>;
  return <><aside className={`agent-workspace-drawer ${open ? 'tools-open' : 'summary-open'}${fullScreen ? ' fullscreen' : ''}`} style={{ width: fullScreen ? undefined : open ? panelWidth : 272 }} role={fullScreen ? 'dialog' : undefined} aria-modal={fullScreen || undefined} aria-label={fullScreen ? '全屏工作区工具' : undefined}>
    <div className="agent-workspace-resizer" role="separator" aria-label="调整工作区工具宽度" aria-orientation="vertical" onPointerDown={startResize}/>
    <section className={`agent-workspace-summary ${open ? 'panel-hidden' : ''}`}>
      <header><div><span className="eyebrow">WORKSPACE</span><b>环境信息</b></div><button type="button" aria-label="打开工作区工具" onClick={onOpen}><PanelRightOpen size={16}/></button></header>
      <div className="agent-workspace-quick-actions"><button type="button" onClick={() => openFiles()}><FileCode2 size={14}/>文件</button><button type="button" disabled={!runtimeAvailable} onClick={openTerminal}><Plus size={14}/>新终端</button></div>
      {loadingOrError || summary}
    </section>
    <section className={`agent-workspace-tool-shell ${open ? '' : 'panel-hidden'}`}>
      <header><nav className="agent-workspace-tabs" aria-label="工作区工具页签">{scopeState.tabs.map(tab => <div key={tab.id} className={scopeState.activeTabId === tab.id ? 'active' : ''}><button type="button" className="agent-workspace-tab-select" onClick={() => updateScope(current => ({ ...current, activeTabId: tab.id }))}><span>{tab.kind === 'files' ? '文件' : tab.kind === 'changes' ? `审查${reviewChanges.length ? ` · ${reviewChanges.length}` : ''}` : tab.kind === 'sources' ? `来源${sources.length ? ` · ${sources.length}` : ''}` : tab.kind === 'git' ? `提交 · ${tab.details.commit.short_id}` : tab.kind === 'subagents' ? '子智能体' : details?.runtime.container_id || (details?.runtime.write_available ? '终端' : '连接中…')}</span></button><button type="button" className="agent-workspace-tab-close" aria-label={`关闭${tab.kind === 'files' ? '文件' : tab.kind === 'changes' ? '改动审查' : tab.kind === 'sources' ? '来源' : tab.kind === 'git' ? '提交审查' : tab.kind === 'subagents' ? '子智能体' : `终端 ${details?.runtime.container_id || ''}`}页签`} disabled={tab.kind === 'terminal' && closingTerminalId === tab.terminalInstanceId} onClick={() => { if (tab.kind !== 'terminal' || closingTerminalId !== tab.terminalInstanceId) requestCloseTab(tab); }}><X size={12}/></button></div>)}</nav><div className="agent-workspace-tool-actions"><div ref={toolMenuRef} className="agent-workspace-tool-menu"><button type="button" className="agent-workspace-tool-menu-trigger" aria-label="新增工作区工具" aria-expanded={toolMenuOpen} aria-haspopup="menu" onClick={() => setToolMenuOpen(current => !current)}><Plus size={15}/></button>{toolMenuOpen && <div role="menu"><button type="button" role="menuitem" onClick={() => { openFiles(); setToolMenuOpen(false); }}><FileCode2 size={13}/>文件</button><button type="button" role="menuitem" onClick={() => { openGitHistory(); setToolMenuOpen(false); }}><GitBranch size={13}/>Git 历史</button>{reviewChanges.length > 0 && <button type="button" role="menuitem" onClick={() => { openChanges(); setToolMenuOpen(false); }}><FileText size={13}/>审查改动</button>}{sources.length > 0 && <button type="button" role="menuitem" onClick={() => { openSources(); setToolMenuOpen(false); }}><Link2 size={13}/>来源</button>}{runtimeTasks.length > 0 && <button type="button" role="menuitem" onClick={() => { openRuntimeTasks(); setToolMenuOpen(false); }}><Bot size={13}/>子智能体</button>}<button type="button" role="menuitem" disabled={!runtimeAvailable} onClick={() => { openTerminal(); setToolMenuOpen(false); }}><Plus size={13}/>终端</button></div>}</div><button type="button" aria-label={fullScreen ? '退出全屏' : '全屏查看工作区工具'} title={fullScreen ? '退出全屏（Esc）' : '全屏查看'} onClick={() => setFullScreen(current => !current)}>{fullScreen ? <Minimize2 size={16}/> : <Maximize2 size={16}/>}</button><button type="button" aria-label="关闭工作区工具" onClick={() => { setFullScreen(false); onClose(); }}><X size={16}/></button></div></header>
      <div className="agent-workspace-tool-body">
        {panelError && <p className="agent-workspace-panel-error" role="alert"><span>{panelError}</span><button type="button" aria-label="关闭错误提示" onClick={() => setPanelError('')}><X size={13}/></button></p>}
        {loadingOrError || (!scopeState.tabs.length ? <div className="agent-drawer-empty"><b>选择工作区工具</b><span>文件仅打开一个页签；终端可按需打开多个独立实例。</span><div><button type="button" className="secondary" onClick={() => openFiles()}>打开文件</button><button type="button" className="secondary" disabled={!runtimeAvailable} onClick={openTerminal}>新建终端</button></div></div> : details && <div className={`agent-workspace-tool-content${gitSidebarVisible ? ' fullscreen-git-layout' : ''}`}>
          {scopeState.tabs.some(tab => tab.kind === 'files') && <section className={`agent-workspace-files ${scopeState.activeTabId === 'files' ? 'active' : ''}${gitSidebarVisible ? ' fullscreen-git' : ''}`} style={{ '--file-tree-width': `${fileTreeWidth}px` } as CSSProperties}>
            <div className="agent-file-tree-pane">
              <header className="agent-file-tree-toolbar"><span>{selectedEntryPaths.size ? `已选 ${selectedEntryPaths.size} 项` : '文件'}</span><div className="agent-file-tree-actions"><button type="button" title="新建文件" aria-label="新建文件" onClick={() => createAtActiveDirectory('FILE')}><FileCode2 size={13}/></button><button type="button" title="新建目录" aria-label="新建目录" onClick={() => createAtActiveDirectory('DIRECTORY')}><FolderPlus size={13}/></button><button type="button" className={`agent-file-tree-expand-toggle${allFileDirectoriesExpanded ? ' expanded' : ''}`} title={allFileDirectoriesExpanded ? '全部收起' : '全部展开'} aria-label={allFileDirectoriesExpanded ? '全部收起目录' : '全部展开目录'} disabled={!fileDirectoryPaths.length} onClick={() => setExpandedFilePaths(allFileDirectoriesExpanded ? new Set() : new Set(fileDirectoryPaths))}>{allFileDirectoriesExpanded ? <ChevronRight size={13}/> : <ChevronDown size={13}/>}</button><button type="button" className="danger" title="删除选中项" aria-label="删除选中项" disabled={!selectedEntryRoots.length} onClick={() => void removeEntries(selectedEntryRoots.map(path => ({ path, kind: visibleFiles.find(item => item.path === path)?.kind ?? 'directory' })))}><Trash2 size={13}/></button></div></header>
              <WorkspaceFileTree entries={visibleFiles} root={details.working_directory} selectedFile={selectedFile} selectedPaths={selectedEntryPaths} expanded={expandedFilePaths} pagination={new Map([...directoryPages].map(([path, page]) => [path, page.nextCursor]))} loadingDirectories={loadingDirectoryPaths} onExpandedChange={setExpandedFilePaths} onDirectoriesChange={setFileDirectoryPaths} onLoadMore={parentPath => { void loadDirectory(parentPath); }} onSelect={path => { setActiveDirectory(undefined); selectFile(path); }} onSelectionChange={setSelectedEntryPaths} onActivateDirectory={path => { setActiveDirectory(path); setGitContextPath(path); }} onContextMenu={(path, kind, event) => { setEntryMenu({ path, kind, x: Math.min(event.clientX, window.innerWidth - 190), y: Math.min(event.clientY, window.innerHeight - 190) }); }}/>
            </div>
            <div className="agent-file-tree-resizer" role="separator" aria-label="调整文件目录宽度" aria-orientation="vertical" onPointerDown={startFileTreeResize}/>
            <div className="agent-file-preview">{candidatePreview ? <>
              <header><span title={candidatePreview.filename}>{candidatePreview.filename} · 候选输出</span></header>
              <iframe className="agent-file-media-preview" sandbox="" title={`${candidatePreview.filename} 候选文件预览`} src={candidatePreview.url}/>
            </> : selectedFile ? <>
              <header><span title={selectedFile}>{selectedAttachment?.filename || relativeWorkspacePath(selectedFile, details.root)}</span><a href={fileUrl(workspaceId, selectedFile, { bindingId, workDirectoryId, download: true })}><Download size={13}/>下载</a></header>
              {canPreviewImage ? <img className="agent-file-media-preview" src={selectedAttachment?.image_data_url || selectedFileUrl} alt={selectedAttachment?.filename || '附件预览'}/> : canPreviewPdf ? <iframe className="agent-file-media-preview" title={selectedAttachment?.filename || 'PDF 预览'} src={selectedFileUrl}/> : textPreviewable ? previewQuery.isLoading ? <p>正在读取文件…</p> : previewQuery.isError ? <p>文件预览不可用，请下载后查看。</p> : <WorkspaceTextPreview path={selectedFile} content={previewQuery.data ?? ''} highlight={highlightedFileSelection?.path === selectedFile ? highlightedFileSelection.selection : undefined} highlightLine={sourceFileNavigation?.path === selectedFile ? sourceFileNavigation.line : undefined} onSelect={selection => { onAddFileSelection?.(selectedFile, selection); onClose(); }}/> : <p>此文件不提供浏览器预览，请下载后查看。</p>}
            </> : <p>选择一个文件以预览或下载。</p>}</div>
          </section>}
          {scopeState.tabs.some(tab => tab.kind === 'changes') && <div className={`agent-changes-tab-panel ${scopeState.activeTabId === 'changes' ? 'active' : ''}`}><WorkspaceChangesReview changes={reviewChanges} selectedId={scopeState.selectedChangeId} onSelect={selectedChangeId => updateScope(current => ({ ...current, selectedChangeId }))} onOpenSource={openSourceFile} workspaceRoot={details.working_directory}/></div>}
          {scopeState.tabs.some(tab => tab.kind === 'sources') && <div className={`agent-changes-tab-panel ${scopeState.activeTabId === 'sources' ? 'active' : ''}`}><ConversationSourcesReview sources={sources} onOpenAttachment={attachment => { setCandidatePreview(undefined); selectFile(attachment.path); }}/></div>}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'git' }> => tab.kind === 'git').map(tab => <div key={tab.id} className={`agent-changes-tab-panel agent-git-commit-tab ${scopeState.activeTabId === tab.id ? 'active' : ''}`}><WorkspaceGitFileDiffReview details={tab.details} diff={tab.diff} onOpenSource={openSourcePath}/></div>)}
          {scopeState.tabs.some(tab => tab.kind === 'subagents') && <div className={`agent-subagent-tab-panel ${scopeState.activeTabId === 'subagents' ? 'active' : ''}`}><RuntimeTaskTab tasks={runtimeTasks} definitions={agentDefinitions} selectedTaskId={scopeState.selectedRuntimeTaskId} onSelect={taskId => updateScope(current => ({ ...current, selectedRuntimeTaskId: taskId }))} sessionStopped={sessionStopped}/></div>}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'terminal' }> => tab.kind === 'terminal').map(tab => <div key={tab.id} className={`agent-terminal-tab-panel ${scopeState.activeTabId === tab.id ? 'active' : ''}`}>{runtimeAvailable ? <WorkspaceTerminal workspaceId={workspaceId} terminalInstanceId={tab.terminalInstanceId} bindingId={bindingId} workDirectoryId={workDirectoryId} workingDirectory={details.working_directory}/> : <div className="agent-drawer-empty"><LoaderCircle className="agent-drawer-spinner" size={20}/><b>终端正在恢复</b><span>文件仍可使用；运行环境恢复后终端会自动可用。</span></div>}</div>)}
          {gitSidebarVisible && gitRepository && <WorkspaceGitSidebar details={details} repository={gitRepository} loadLog={repositoryPath => api.gitLog(workspaceId, repositoryPath, gitOptions)} loadCommit={(repositoryPath, commit) => api.gitCommit(workspaceId, repositoryPath, commit, gitOptions)} loadDiff={(repositoryPath, commit, path) => api.gitDiff(workspaceId, repositoryPath, commit, path, gitOptions)} onOpenFileDiff={openGitFileDiff} closedDiffEpoch={closedGitDiffEpoch}/>}
        </div>)}
      </div>
    </section>{entryMenu && <div className="agent-file-context-menu" role="menu" aria-label="文件操作菜单" style={{ left: entryMenu.x, top: entryMenu.y }} onPointerDown={event => event.stopPropagation()}>{entryMenu.kind === 'directory' && <><button type="button" role="menuitem" onClick={() => { void createEntry(entryMenu.path, 'FILE'); setEntryMenu(undefined); }}><FileCode2 size={14}/>新建文件</button><button type="button" role="menuitem" onClick={() => { void createEntry(entryMenu.path, 'DIRECTORY'); setEntryMenu(undefined); }}><FolderPlus size={14}/>新建目录</button></>}<button type="button" className="danger" role="menuitem" onClick={() => { void removeEntries([{ path: entryMenu.path, kind: entryMenu.kind }]); setEntryMenu(undefined); }}><Trash2 size={14}/>{entryMenu.kind === 'directory' ? '删除目录' : '删除文件'}</button></div>}
  </aside>{sshAccessOpen && sshRemoteReady && <SshAccessGuide host={details!.ide.gateway.host!} port={details!.ide.gateway.port!} path={details!.ide.gateway.path!} onClose={() => setSshAccessOpen(false)}/>} {pendingTerminalClose && <div className="agent-terminal-close-backdrop" onPointerDown={event => { if (event.target === event.currentTarget && !closingTerminalId) setPendingTerminalClose(undefined); }}><section role="dialog" aria-modal="true" aria-labelledby="agent-terminal-close-title" className="agent-terminal-close-dialog"><header><span className="eyebrow">TERMINAL</span><h2 id="agent-terminal-close-title">关闭此终端？</h2></header><p>关闭后会停止该终端中正在执行的命令，并清除这一个终端会话；其他终端和当前会话不会受影响。</p><footer><button type="button" className="secondary" disabled={Boolean(closingTerminalId)} onClick={() => setPendingTerminalClose(undefined)}>取消</button><button type="button" className="danger" autoFocus disabled={Boolean(closingTerminalId)} onClick={() => { const tab = pendingTerminalClose; setPendingTerminalClose(undefined); void closeTab(tab); }}>{closingTerminalId ? '正在关闭…' : '关闭终端'}</button></footer></section></div>}</>;
}

export interface AgentSessionWorkbenchProps {
  onNavigate: (path: string, replace?: boolean) => void;
  /** Provided by scoped hosts whose conversation page has a product parent. */
  onReturnToSource?: () => void;
  /** Refresh the parent product projection after a native session state change. */
  onHostStateChanged?: () => void;
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

function AgentSessionWorkbenchContent({ onNavigate, onReturnToSource, onHostStateChanged, autoOpenDraft = false, hideDraftTitle = false }: Omit<AgentSessionWorkbenchProps, 'gateway' | 'host'>) {
  const { api, features, candidateOutputUrl } = useAgentSessionGateway();
  const dialog = useProductDialog();
  const host = useAgentSessionHost();
  const queryClient = useQueryClient();
  const initialBootstrapRecovery = useRef<BootstrapRecovery | undefined>(readBootstrapRecovery(host.bootstrapRecoveryStorageKey));
  const initialConversationDraft = useRef<ConversationDraftRecovery | undefined>(
    initialBootstrapRecovery.current ? undefined : readConversationDraft(host.draftStorageKey),
  );
  const [draft, setDraft] = useState(() => initialBootstrapRecovery.current?.message.content ?? initialConversationDraft.current?.content ?? '');
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
  const turnStateRef = useRef<TurnState>('idle');
  turnStateRef.current = turnState;
  const [activeTurnEventId, setActiveTurnEventId] = useState<string>();
  const [requestStartedAt, setRequestStartedAt] = useState<number>();
  const [confirmationReason, setConfirmationReason] = useState('');
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [draggedQueuedMessageId, setDraggedQueuedMessageId] = useState<string>();
  const [queuedMessageMenuId, setQueuedMessageMenuId] = useState<string>();
  const [pendingNativeGuidance, setPendingNativeGuidance] = useState<BoundQueuedMessage[]>([]);
  const [pendingRewrite, setPendingRewrite] = useState<{ eventId: string; content: string }>();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [reviewChanges, setReviewChanges] = useState<WorkspaceFileChange[]>([]);
  const [reviewRequestId, setReviewRequestId] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState('');
  const [newConversationProviderId, setNewConversationProviderId] = useState(() => initialBootstrapRecovery.current?.providerId ?? initialConversationDraft.current?.providerId ?? '');
  const [newConversationModelName, setNewConversationModelName] = useState(() => initialBootstrapRecovery.current?.modelName ?? initialConversationDraft.current?.modelName ?? '');
  const [newConversationReasoningEffort, setNewConversationReasoningEffort] = useState<string | null>(() => initialBootstrapRecovery.current?.reasoningEffort ?? initialConversationDraft.current?.reasoningEffort ?? null);
  const [conversationProviderId, setConversationProviderId] = useState('');
  const [conversationModelName, setConversationModelName] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AgentAttachment[]>(() => initialBootstrapRecovery.current?.message.items ?? initialConversationDraft.current?.attachments ?? []);
  const [references, setReferences] = useState<ConversationReference[]>(() => initialBootstrapRecovery.current?.message.references ?? initialConversationDraft.current?.references ?? []);
  const [workspaceReferences, setWorkspaceReferences] = useState<AgentWorkspaceReference[]>(() => initialBootstrapRecovery.current?.message.workspaceReferences ?? initialConversationDraft.current?.workspaceReferences ?? []);
  const [workspaceReferencePickerOpen, setWorkspaceReferencePickerOpen] = useState(false);
  const [workspaceReferenceQuery, setWorkspaceReferenceQuery] = useState('');
  const [attachmentRequest, setAttachmentRequest] = useState<{ key: string; attachment: AgentAttachment }>();
  const [fileSelectionReference, setFileSelectionReference] = useState<{ path: string; selection: FileSelection }>();
  useEffect(() => {
    const openSelection = (event: Event) => {
      const reference = (event as CustomEvent<AgentWorkspaceReference>).detail;
      if (reference?.selection) {
        setFileSelectionReference({ path: reference.path, selection: reference.selection });
        setDrawerOpen(true);
      }
    };
    window.addEventListener('flowweave:open-workspace-selection', openSelection);
    return () => window.removeEventListener('flowweave:open-workspace-selection', openSelection);
  }, []);
  const [candidatePreviewRequest, setCandidatePreviewRequest] = useState<CandidateFilePreviewRequest>();
  const [operationError, setOperationError] = useState<Error>();
  const [streamHold, setStreamHold] = useState<{ bindingId: string; expiresAt: number }>();
  const [pageVisible, setPageVisible] = useState(() => document.visibilityState === 'visible');
  const [condensationStatus, setCondensationStatus] = useState<{ bindingId: string; state: 'running' | 'failed'; startedAt: number; message?: string }>();
  const [condensationConfirmationOpen, setCondensationConfirmationOpen] = useState(false);
  const [pendingCreatedId, setPendingCreatedId] = useState<string>();
  const [pendingMigratedSend, setPendingMigratedSend] = useState<BoundQueuedMessage>();
  const [conversationDraft, setConversationDraft] = useState<ConversationDraft | undefined>(() => initialBootstrapRecovery.current?.draft ?? initialConversationDraft.current?.draft);
  const [bootstrapRecovery, setBootstrapRecovery] = useState<BootstrapRecovery | undefined>(() => initialBootstrapRecovery.current);
  const [workspaceScopeMigration, setWorkspaceScopeMigration] = useState<string>();
  const [workDirectoryCreatorOpen, setWorkDirectoryCreatorOpen] = useState(false);
  const [capabilityManagerOpen, setCapabilityManagerOpen] = useState(false);
  const [workspacePathCopied, setWorkspacePathCopied] = useState(false);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const workspacePathCopyTimer = useRef<number | undefined>(undefined);
  const pendingLiveText = useRef('');
  const liveStreamItemId = useRef<string | undefined>(undefined);
  const liveTextFrame = useRef<number | undefined>(undefined);
  const pendingLiveEvents = useRef<OpenHandsConversationEvent[]>([]);
  const liveEventsFrame = useRef<number | undefined>(undefined);
  // Full branches are intentionally memory-only.  This index is only an LRU
  // lease for React Query entries; it is never used as an event locator or
  // command authorization input.
  const logicalConversationCache = useRef(new Map<string, LogicalConversationCacheEntry>());
  const activeLogicalConversation = useRef<string | undefined>(undefined);
  const logicalCachePruneTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => {
    // Leaving this host must not let React Query's general-purpose GC retain
    // whole EventLogs outside this feature's 5-session/5-minute lease. The
    // bounded tab-local shell intentionally remains for the next first paint.
    for (const resource of [
      'conversation-head',
      'conversation-hydration',
      'conversation-events',
      'conversation-input-readiness',
      'conversation-context',
    ]) {
      queryClient.removeQueries({ queryKey: sessionQueryKey(host, resource) });
    }
  }, [host, queryClient]);
  useEffect(() => () => {
    if (logicalCachePruneTimer.current !== undefined) {
      window.clearTimeout(logicalCachePruneTimer.current);
    }
  }, []);
  useEffect(() => () => {
    if (workspacePathCopyTimer.current !== undefined) window.clearTimeout(workspacePathCopyTimer.current);
  }, []);
  const bootstrapTransitionScope = useRef<string | undefined>(undefined);
  const selectedBindingId = host.bindingIdFromPathname(withoutDeploymentBase(window.location.pathname));
  const previousComposerScope = useRef<string | undefined>(undefined);
  const activityBaseline = useRef<Map<string, boolean>>(new Map());
  const [unreadConversationIds, setUnreadConversationIds] = useState<Set<string>>(() => new Set());
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
  const unreadStorageKey = workspace ? unreadConversationStorageKey(host.id, workspace.id) : undefined;
  const runtimeQuery = useQuery({ queryKey: sessionQueryKey(host, 'runtime', workspace?.id), queryFn: () => api.runtime(workspace!.id), enabled: Boolean(workspace), refetchInterval: query => query.state.data?.state === 'RECOVERING' ? 5000 : false });
  const conversationsQuery = useInfiniteQuery({
    queryKey: sessionQueryKey(host, 'conversations', workspace?.id),
    queryFn: ({ pageParam }) => api.conversations(workspace!.id, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: page => page.next_cursor || undefined,
    enabled: Boolean(workspace),
    refetchInterval: query => {
      const pages = query.state.data?.pages ?? [];
      // Title generation is an isolated one-shot metadata task. Poll only
      // while it is pending; otherwise refresh the single native running-list
      // snapshot at a bounded rate while the page is visible.
      if (pages.some(page => page.items.some(item => item.title_state === 'PENDING'))) return 1000;
      return pageVisible ? 10_000 : false;
    },
  });
  useEffect(() => {
    if (!workspace || !conversationsQuery.hasNextPage || conversationsQuery.isFetchingNextPage) return;
    void conversationsQuery.fetchNextPage();
  }, [conversationsQuery, workspace]);
  const workDirectoriesQuery = useQuery({ queryKey: sessionQueryKey(host, 'work-directories', workspace?.id), queryFn: () => api.workDirectories(workspace!.id), enabled: Boolean(workspace && features.workDirectories) });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers, enabled: Boolean(workspace && features.modelSelection) });
  const capabilityCatalogQuery = useQuery({ queryKey: sessionQueryKey(host, 'capability-catalog'), queryFn: api.capabilities, enabled: Boolean(workspace && features.capabilities) });
  const conversations = useMemo(
    () => [...(conversationsQuery.data?.pages.flatMap(page => page.items) ?? [])].sort(
      (left, right) => right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id),
    ),
    [conversationsQuery.data],
  );
  const selectedConversationQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation', workspace?.id, selectedBindingId),
    queryFn: () => api.conversation(workspace!.id, selectedBindingId!),
    enabled: Boolean(workspace && selectedBindingId),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const selected = useMemo(
    () => selectedConversationQuery.data ?? conversations.find(item => item.id === selectedBindingId),
    [conversations, selectedBindingId, selectedConversationQuery.data],
  );
  const selectedConversationId = selected?.id;
  useEffect(() => {
    if (selectedConversationId) markSessionPerformance('selected');
  }, [selectedConversationId]);
  // The workspace endpoint resolves the actual directory bound to this
  // conversation, including legacy conversations whose list projection only
  // reports the shared project root.  Keep transcript file paths scoped to
  // this authoritative directory.
  const activeWorkspaceOptions = selected
    ? { bindingId: selected.id }
    : { workDirectoryId: conversationDraft?.workDirectoryId };
  const activeWorkspaceDetailsQuery = useQuery({
    queryKey: sessionQueryKey(host, 'workspace-details', workspace?.id, activeWorkspaceOptions.bindingId, activeWorkspaceOptions.workDirectoryId),
    queryFn: () => api.workspaceDetails(workspace!.id, activeWorkspaceOptions),
    enabled: Boolean(workspace && (selected || conversationDraft)),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const workspaceReferenceIndexQuery = useQuery({
    queryKey: sessionQueryKey(host, 'workspace-reference-index', workspace?.id, activeWorkspaceOptions.bindingId, activeWorkspaceOptions.workDirectoryId),
    queryFn: () => api.workspaceDetails(workspace!.id, { ...activeWorkspaceOptions, fullIndex: true }),
    enabled: Boolean(workspace && workspaceReferencePickerOpen && (selected || conversationDraft)),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  useEffect(() => {
    if (selected && activeWorkspaceDetailsQuery.data) {
      markSessionPerformance('workspace-ready');
    }
  }, [activeWorkspaceDetailsQuery.data, selected]);
  const updateUnreadConversationIds = useCallback((update: (current: Set<string>) => Set<string>) => {
    setUnreadConversationIds(current => {
      const next = update(current);
      writeUnreadConversationIds(unreadStorageKey, next);
      return next;
    });
  }, [unreadStorageKey]);
  const markConversationRead = useCallback((bindingId: string) => {
    updateUnreadConversationIds(current => {
      if (!current.has(bindingId)) return current;
      const next = new Set(current);
      next.delete(bindingId);
      return next;
    });
  }, [updateUnreadConversationIds]);
  useEffect(() => {
    activityBaseline.current = new Map();
    setUnreadConversationIds(readUnreadConversationIds(unreadStorageKey));
  }, [unreadStorageKey]);
  useEffect(() => {
    if (selectedBindingId) markConversationRead(selectedBindingId);
  }, [markConversationRead, selectedBindingId]);
  useEffect(() => {
    // The paginated list is the single running snapshot. Detect only a
    // witnessed running -> idle edge; an initial idle row is not a new reply.
    const present = new Set(conversations.map(item => item.id));
    for (const item of conversations) {
      const running = conversationIsRunning(item.execution_status);
      const previous = activityBaseline.current.get(item.id);
      activityBaseline.current.set(item.id, running);
      if (previous === true && !running && item.id !== selectedBindingId) {
        updateUnreadConversationIds(current => current.has(item.id) ? current : new Set([...current, item.id]));
      }
    }
    for (const bindingId of activityBaseline.current.keys()) {
      if (!present.has(bindingId)) activityBaseline.current.delete(bindingId);
    }
    updateUnreadConversationIds(current => {
      const next = new Set([...current].filter(bindingId => present.has(bindingId)));
      return next.size === current.size ? current : next;
    });
  }, [conversations, selectedBindingId, updateUnreadConversationIds]);
  useEffect(() => {
    const onVisibilityChange = () => setPageVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, []);
  const composerCapabilityReferences = useMemo(() => {
    if (selected?.capabilities) return selected.capabilities;
    if (!conversationDraft) return [];
    const catalog = new Map((capabilityCatalogQuery.data ?? []).map(item => [item.id, item]));
    return (conversationDraft.capabilityVersionIds ?? []).flatMap(id => {
      const item = catalog.get(id);
      return item ? [{ id: item.id, capability_type: item.capability_type as AgentSessionCapability['capability_type'], capability_key: item.capability_key, digest: item.content_hash }] : [];
    });
  }, [capabilityCatalogQuery.data, conversationDraft, selected?.capabilities]);
  const agentDefinitionAssets = useMemo(() => {
    // The public session-capability DTO intentionally exposes only the
    // capabilities selectable in the composer, and does not contain
    // AGENT_DEFINITION. A Task event also carries a type but no frozen
    // definition version. Keep the detail view honest: show a same-name
    // published definition when it is readable, and label it as such rather
    // than claiming it is the definition frozen into this Conversation.
    return (capabilityCatalogQuery.data ?? []).filter(
      item => item.capability_type === 'AGENT_DEFINITION',
    );
  }, [capabilityCatalogQuery.data]);
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
    const native: ComposerSuggestion[] = (selected || conversationDraft) ? [{
      id: 'native:condense',
      kind: 'NATIVE',
      token: '/condense',
      label: '压缩上下文',
      detail: selected && (turnState === 'idle' || turnState === 'paused')
        ? '调用 OpenHands 原生 condenser，完成后保留正式 Condensation 事件'
        : conversationDraft
          ? '首条消息创建 OpenHands 原生会话后可调用'
          : '当前会话处理完成后可调用',
      available: Boolean(selected && (turnState === 'idle' || turnState === 'paused')),
      nativeAction: 'CONDENSE',
    }] : [];
    return [...native, ...capabilities];
  }, [capabilityCatalogQuery.data, composerCapabilityReferences, conversationDraft, selected, turnState]);
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
  const clearConversationDraft = useCallback(() => {
    writeConversationDraft(host.draftStorageKey, undefined);
  }, [host.draftStorageKey]);
  const connectedProviders = (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default));
  const runtime = runtimeQuery.data;
  const runtimeWritable = Boolean(workspace && runtime?.write_available);
  const canOpenConversation = runtimeWritable;
  const canBootstrap = Boolean(runtimeWritable && conversationDraft && (!features.modelSelection || (newConversationProviderId && newConversationModelName)));
  const isGenerating = turnState === 'running' || turnState === 'pausing' || turnState === 'resuming';
  const streamEnabled = Boolean(
    selected
    && runtime?.write_available
    && (isGenerating || streamHold?.bindingId === selected.id),
  );
  const eventQueryKey = sessionQueryKey(host, 'conversation-events', workspace?.id, selected?.id);
  const hydrationQueryKey = sessionQueryKey(host, 'conversation-hydration', workspace?.id, selected?.id);
  const headQueryKey = sessionQueryKey(host, 'conversation-head', workspace?.id, selected?.id);
  const selectedWorkspaceId = workspace?.id;
  const cachedHydration = queryClient.getQueryData<import('../../types').AgentConversationHydration>(hydrationQueryKey);
  const cachedHydrationCursor = cachedHydration?.events.next_cursor ?? undefined;
  // Query cache presence alone is not a reuse lease: the just-completed
  // hydration writes into it too. Only an entry parked while inactive may
  // take the cheaper HEAD validation path on a later selection.
  const reusableInactiveHydration = Boolean(
    selected?.id
    && cachedHydration
    && logicalConversationCache.current.has(selected.id),
  );
  useEffect(() => {
    const workspaceId = selectedWorkspaceId;
    const bindingId = selected?.id;
    if (!workspaceId || !bindingId) return;
    const previous = activeLogicalConversation.current;
    // Query data updates (context, readiness and stream reconciliation) must
    // not rerun cache bookkeeping for the same selected binding.
    if (previous === bindingId) return;
    if (previous) {
      const previousHydration = queryClient.getQueryData(
        sessionQueryKey(host, 'conversation-hydration', workspaceId, previous),
      );
      if (previousHydration) logicalConversationCache.current.set(previous, {
        bindingId: previous, lastAccessedAt: Date.now(),
      });
    }
    activeLogicalConversation.current = bindingId;
    logicalConversationCache.current.delete(bindingId);
    const result = reconcileLogicalConversationCache(
      logicalConversationCache.current.values(), bindingId, Date.now(),
    );
    logicalConversationCache.current = new Map(
      result.retained.map(entry => [entry.bindingId, entry]),
    );
    for (const evictedBindingId of result.evictedBindingIds) {
      for (const resource of [
        'conversation-hydration',
        'conversation-events',
        'conversation-input-readiness',
        'conversation-context',
      ]) {
        queryClient.removeQueries({
          queryKey: sessionQueryKey(host, resource, workspaceId, evictedBindingId),
          exact: true,
        });
      }
    }
    if (logicalCachePruneTimer.current !== undefined) {
      window.clearTimeout(logicalCachePruneTimer.current);
      logicalCachePruneTimer.current = undefined;
    }
    if (result.nextExpiresAt) {
      logicalCachePruneTimer.current = window.setTimeout(() => {
        logicalCachePruneTimer.current = undefined;
        const expired = reconcileLogicalConversationCache(
          logicalConversationCache.current.values(), activeLogicalConversation.current, Date.now(),
        );
        logicalConversationCache.current = new Map(
          expired.retained.map(entry => [entry.bindingId, entry]),
        );
        for (const evictedBindingId of expired.evictedBindingIds) {
          for (const resource of [
            'conversation-hydration',
            'conversation-events',
            'conversation-input-readiness',
            'conversation-context',
          ]) {
            queryClient.removeQueries({
              queryKey: sessionQueryKey(host, resource, workspaceId, evictedBindingId),
              exact: true,
            });
          }
        }
      }, Math.max(1, result.nextExpiresAt - Date.now()));
    }
  }, [host, queryClient, selected?.id, selectedWorkspaceId]);
  useEffect(() => {
    if (!selectedWorkspaceId || !selected?.id || cachedHydration) return;
    const shell = readConversationShellSnapshot(host.id, selectedWorkspaceId, selected.id);
    if (!shell) return;
    // The shell improves first paint after a refresh, but no command derives
    // truth from it. The complete hydration request below replaces it.
    queryClient.setQueryData(
      sessionQueryKey(host, 'conversation-events', selectedWorkspaceId, selected.id), shell.events,
    );
    if (shell.context) queryClient.setQueryData(
      sessionQueryKey(host, 'conversation-context', selectedWorkspaceId, selected.id), shell.context,
    );
    if (shell.readiness) queryClient.setQueryData(
      sessionQueryKey(host, 'conversation-input-readiness', selectedWorkspaceId, selected.id), shell.readiness,
    );
  }, [cachedHydration, host, queryClient, selected?.id, selectedWorkspaceId]);
  const headQuery = useQuery({
    queryKey: headQueryKey,
    queryFn: () => api.conversationHead(workspace!.id, selected!.id),
    // A first open gets its formal HEAD as part of complete hydration. Only a
    // previously hydrated in-memory branch needs this cheaper reuse check.
    enabled: Boolean(workspace && selected && reusableInactiveHydration),
    staleTime: 0,
    refetchOnMount: 'always',
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const hydrationQuery = useQuery({
    queryKey: hydrationQueryKey,
    queryFn: () => api.conversationHydration(workspace!.id, selected!.id),
    // A complete branch already in this tab stays usable only after the
    // bounded formal HEAD query above confirms its leaf. Missing data starts
    // hydration immediately; stale/mismatched data is refreshed below.
    enabled: Boolean(workspace && selected && !cachedHydration),
    staleTime: Infinity,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  useEffect(() => {
    if (!selected || !cachedHydration || !headQuery.isSuccess) return;
    if ((headQuery.data?.cursor ?? undefined) === cachedHydrationCursor) return;
    // Leave the old transcript visible while the formal replacement arrives.
    // A changed HEAD is never merged into an old complete branch.
    void hydrationQuery.refetch();
  }, [cachedHydration, cachedHydrationCursor, headQuery.data?.cursor, headQuery.isSuccess, hydrationQuery, selected]);
  const inputReadinessQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-input-readiness', workspace?.id, selected?.id),
    queryFn: () => api.inputReadiness(workspace!.id, selected!.id),
    // This is the formal OpenHands execution-state read used to restore an
    // in-flight turn after a browser reload. It is not persisted by FlowWeave.
    // Hydration supplies the first formal readiness snapshot. This remains a
    // dedicated live-recovery poll after that initial complete read.
    enabled: Boolean(
      workspace
      && selected
      && hydrationQuery.data
      && (hydrationQuery.data.readiness.ready === false
        || turnState === 'pausing'
        || queuedMessages.length > 0
        || isGenerating),
    ),
    refetchInterval: query => {
      const needsFallback = turnState === 'pausing' || queuedMessages.length > 0 || isGenerating || query.state.data?.ready === false;
      if (!pageVisible || !needsFallback) return false;
      return Math.min(2000 * 2 ** query.state.fetchFailureCount, 10_000);
    },
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const eventsQuery = useQuery<OpenHandsConversationEventBatch>({
    queryKey: eventQueryKey,
    queryFn: () => api.conversationEvents(workspace!.id, selected!.id),
    enabled: false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  useEffect(() => {
    if (!hydrationQuery.data) return;
    // Keep the existing event-cache surface for stream frames and cursor
    // reconciliation, but seed all three initial native projections from one
    // complete server-side hydration.  This avoids a second context/readiness
    // request for an idle session while preserving those queries for live use.
    queryClient.setQueryData<OpenHandsConversationEventBatch>(
      sessionQueryKey(host, 'conversation-events', workspace?.id, selected?.id),
      hydrationQuery.data.events,
    );
    queryClient.setQueryData(
      sessionQueryKey(host, 'conversation-input-readiness', workspace?.id, selected?.id),
      hydrationQuery.data.readiness,
    );
    queryClient.setQueryData(
      sessionQueryKey(host, 'conversation-context', workspace?.id, selected?.id),
      hydrationQuery.data.context,
    );
  }, [host, hydrationQuery.data, queryClient, selected?.id, workspace?.id]);
  useEffect(() => {
    if (selected && eventsQuery.data) markSessionPerformance('events-ready');
  }, [eventsQuery.data, selected]);
  useEffect(() => {
    if (!workspace || !selected || !isGenerating || !pageVisible || !eventsQuery.data?.next_cursor) return;
    const workspaceId = workspace.id;
    const bindingId = selected.id;
    let cancelled = false;
    let timer: number | undefined;
    const recover = async () => {
      const current = queryClient.getQueryData<OpenHandsConversationEventBatch>(eventQueryKey);
      const cursor = current?.next_cursor ?? undefined;
      if (cancelled || !cursor) return;
      try {
        const recovered = await api.conversationEvents(workspaceId, bindingId, cursor);
        if (cancelled) return;
        queryClient.setQueryData<OpenHandsConversationEventBatch>(eventQueryKey, existing => {
          if (!existing) return existing;
          const cursorUnchanged = existing.next_cursor === cursor;
          return {
            ...existing,
            ...recovered,
            events: mergeConversationEvents(existing.events, recovered.events),
            next_cursor: cursorUnchanged ? (recovered.next_cursor ?? cursor) : existing.next_cursor,
            history_cursor: existing.history_cursor ?? recovered.history_cursor,
          };
        });
      } catch (error) {
        void error;
      } finally {
        if (!cancelled) timer = window.setTimeout(() => { void recover(); }, ACTIVE_EVENT_RECOVERY_INTERVAL_MS);
      }
    };
    timer = window.setTimeout(() => { void recover(); }, ACTIVE_EVENT_RECOVERY_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [api, eventQueryKey, eventsQuery.data?.next_cursor, isGenerating, pageVisible, queryClient, selected, workspace]);
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
  const sessionFileChanges = useMemo(() => workspaceFileChanges(displayedEvents), [displayedEvents]);
  const openChangesReview = useCallback((changes: WorkspaceFileChange[]) => {
    if (!changes.length) return;
    setReviewChanges(changes);
    setReviewRequestId(randomId());
    setDrawerOpen(true);
  }, []);
  const runtimeTasks = useMemo(
    () => runtimeTasksFromEvents(displayedEvents, eventsQuery.data?.task_usage ?? [], eventsQuery.data?.task_control ?? []),
    [displayedEvents, eventsQuery.data?.task_control, eventsQuery.data?.task_usage],
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
  const openCandidateFileInDrawer = useCallback((fieldKey: string, relativePath: string) => {
    if (!workspace || !candidateOutputUrl) return;
    setCandidatePreviewRequest({
      key: randomId(),
      filename: relativePath.split('/').at(-1) || relativePath,
      url: candidateOutputUrl(workspace.id, fieldKey, relativePath),
    });
    setDrawerOpen(true);
  }, [candidateOutputUrl, workspace]);
  const contextQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-context', workspace?.id, selected?.id),
    queryFn: () => api.conversationContext(workspace!.id, selected!.id),
    enabled: false,
    staleTime: Infinity,
  });
  useEffect(() => {
    if (!workspace || !selected || !hydrationQuery.data || !eventsQuery.data) return;
    writeConversationShellSnapshot(host.id, workspace.id, selected.id, {
      events: eventsQuery.data,
      context: contextQuery.data ?? hydrationQuery.data.context,
      readiness: inputReadinessQuery.data ?? hydrationQuery.data.readiness,
    });
  }, [contextQuery.data, eventsQuery.data, host.id, hydrationQuery.data, inputReadinessQuery.data, selected, workspace]);
  const compactionPolicyCurrent = contextQuery.data?.compaction_policy_current !== false;
  const canWrite = Boolean(runtimeWritable && selected);
  const canCompose = Boolean(canWrite || (runtimeWritable && conversationDraft));
  const selectedConversationRunning = conversationIsRunning(inputReadinessQuery.data?.execution_status);
  const sessionStopped = turnState === 'pausing' || turnState === 'paused'
    || inputReadinessQuery.data?.execution_status?.toLowerCase() === 'paused';
  const confirmationQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace?.id, selected?.id),
    queryFn: () => api.pendingConfirmation(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected && runtime?.write_available && features.confirmations),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const pendingConfirmation = confirmationQuery.data?.pending ? confirmationQuery.data : undefined;
  const refresh = useCallback(() => {
    if (!workspace) return;
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'runtime', workspace.id) });
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
    if (selected?.id) {
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-hydration', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-input-readiness', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-context', workspace.id, selected.id) });
    }
  }, [host, queryClient, selected?.id, workspace]);
  const clearLiveText = useCallback(() => {
    pendingLiveText.current = '';
    liveStreamItemId.current = undefined;
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
  const onStreamEvent = useCallback((event: AgentStreamEvent) => {
    if (event.type === 'delta' && event.content) {
      if (event.item_id) liveStreamItemId.current = event.item_id;
      appendLiveText(event.content);
    }
    if (event.type === 'stream_reset' && event.item_id && liveStreamItemId.current === event.item_id) clearLiveText();
    if (event.type === 'stream_closed' && event.item_id && liveStreamItemId.current === event.item_id) {
      clearLiveText();
      refresh();
    }
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
  const onStreamReconnect = useCallback(() => {
    // A WebSocket is a live projection only. Events written while the browser
    // was disconnected are recovered from the authoritative REST feed after
    // the socket is live again; browser-only deltas/events remain merged in
    // memory until their formal counterparts arrive.
    refresh();
  }, [refresh]);
  useEffect(() => {
    if (!streamEnabled) setStreamStatus('disabled');
  }, [streamEnabled]);

  useEffect(() => {
    if (!conversationDraft && !selectedBindingId && conversations.length) onNavigate(host.conversationPath(conversations[0].id), true);
    if (selectedBindingId && conversations.length && !selected && pendingCreatedId !== selectedBindingId && !conversationsQuery.isFetching) onNavigate(host.rootPath, true);
  }, [conversationDraft, conversations, conversationsQuery.isFetching, host, onNavigate, pendingCreatedId, selected, selectedBindingId]);
  useEffect(() => { if (selected?.id === pendingCreatedId) setPendingCreatedId(undefined); }, [pendingCreatedId, selected?.id]);
  useEffect(() => {
    if (previousComposerScope.current === composerScope) return;
    previousComposerScope.current = composerScope;
    if (bootstrapTransitionScope.current === composerScope) {
      bootstrapTransitionScope.current = undefined;
      return;
    }
    setEditing(false); clearLiveText(); pendingLiveEvents.current = []; if (liveEventsFrame.current !== undefined) window.cancelAnimationFrame(liveEventsFrame.current); liveEventsFrame.current = undefined; setLiveEvents([]); setHiddenEventIds(new Set()); setActiveTurnEventId(undefined); setRequestStartedAt(undefined); setConfirmationReason(''); setCondensationConfirmationOpen(false); setTurnState('idle'); setQueuedMessages([]); setPendingNativeGuidance([]); setPendingRewrite(undefined); setAttachments([]); setReferences([]); setOperationError(undefined);
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
    if (!conversationDraft || pendingBootstrap || bootstrapRecovery) {
      clearConversationDraft();
      return;
    }
    writeConversationDraft(host.draftStorageKey, {
      draft: conversationDraft, content: draft, attachments, references, workspaceReferences, providerId: newConversationProviderId,
      modelName: newConversationModelName, reasoningEffort: newConversationReasoningEffort,
    });
  }, [attachments, bootstrapRecovery, clearConversationDraft, conversationDraft, draft, host.draftStorageKey, newConversationModelName, newConversationProviderId, newConversationReasoningEffort, pendingBootstrap, references, workspaceReferences]);
  useEffect(() => {
    if (turnState === 'pausing' && inputReadinessQuery.data?.ready) setTurnState('paused');
  }, [inputReadinessQuery.data?.ready, turnState]);
  useEffect(() => {
    if (!selected || inputReadinessQuery.data?.execution_status?.toLowerCase() !== 'paused') return;
    setTurnState(current => current === 'idle' ? 'paused' : current);
  }, [inputReadinessQuery.data?.execution_status, selected]);
  useEffect(() => {
    const executionStatus = inputReadinessQuery.data?.execution_status?.trim().toLowerCase();
    const nativeTurnEnded = inputReadinessQuery.data?.ready === true
      && ['idle', 'completed', 'stopped'].includes(executionStatus ?? '');
    if (!selected || !nativeTurnEnded || (turnState !== 'running' && turnState !== 'resuming')) return;

    // A formal assistant/error/Finish event remains the normal completion
    // signal.  This is deliberately only a recovery path for a main Agent
    // loop that ended without emitting one, so the rail cannot spin forever.
    const activeUserEventId = activeTurnEventId ?? latestUnfinishedUserEventId(displayedEvents);
    if (!activeUserEventId || hasFinishedTurn(displayedEvents, activeUserEventId)) return;

    const timer = window.setTimeout(() => {
      if (turnStateRef.current !== 'running' && turnStateRef.current !== 'resuming') return;
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      clearLiveText();
      setStreamHold({ bindingId: selected.id, expiresAt: Date.now() + STREAM_IDLE_GRACE_MS });
      setOperationError(new Error('Agent 已异常停止，本轮结果未返回。你可以继续发送消息；历史记录已保留。'));
      setTurnState('idle');
      refresh();
    }, ABNORMAL_IDLE_RECONCILIATION_MS);
    return () => window.clearTimeout(timer);
  }, [activeTurnEventId, clearLiveText, displayedEvents, inputReadinessQuery.data?.execution_status, inputReadinessQuery.data?.ready, refresh, selected, turnState]);
  useEffect(() => {
    // On first entry, the native readiness request can be delayed by a Runtime
    // reconnect. A persisted, unfinished formal user event already proves the
    // turn is active; do not render a Pause/Resume state until OpenHands has
    // explicitly reported one.
    if (!selected || inputReadinessQuery.data?.ready === true
      || inputReadinessQuery.data?.execution_status?.toLowerCase() === 'paused') return;
    const userEventId = latestUnfinishedUserEventId(displayedEvents);
    if (!userEventId) return;
    setActiveTurnEventId(current => current ?? userEventId);
    setTurnState(current => current === 'idle' ? 'running' : current);
  }, [displayedEvents, inputReadinessQuery.data?.execution_status, inputReadinessQuery.data?.ready, selected]);
  useEffect(() => {
    if ((turnState === 'running' || turnState === 'resuming') && activeTurnEventId && hasFinishedTurn(displayedEvents, activeTurnEventId)) {
      clearLiveText();
      // OpenHands may already have accepted a Command/Ctrl+Enter guidance
      // message while the previous turn finishes. Follow that formal user
      // event instead of briefly reporting idle and releasing the Enter queue.
      const nextUserEventId = latestUnfinishedUserEventId(displayedEvents);
      if (nextUserEventId && nextUserEventId !== activeTurnEventId) {
        setActiveTurnEventId(nextUserEventId);
        setRequestStartedAt(Date.now());
        setTurnState('running');
      } else {
        setActiveTurnEventId(undefined);
        setRequestStartedAt(undefined);
        setTurnState('idle');
        if (selected?.id) {
          setStreamHold({ bindingId: selected.id, expiresAt: Date.now() + STREAM_IDLE_GRACE_MS });
        }
      }
      refresh();
    }
  }, [activeTurnEventId, clearLiveText, displayedEvents, refresh, selected?.id, turnState]);
  useEffect(() => {
    if (turnState !== 'paused' || !selected?.id) return;
    setStreamHold({ bindingId: selected.id, expiresAt: Date.now() + STREAM_IDLE_GRACE_MS });
  }, [selected?.id, turnState]);
  useEffect(() => {
    if (!streamHold) return;
    const remaining = streamHold.expiresAt - Date.now();
    if (remaining <= 0) {
      setStreamHold(undefined);
      return;
    }
    const timer = window.setTimeout(() => setStreamHold(current => (
      current?.bindingId === streamHold.bindingId ? undefined : current
    )), remaining);
    return () => window.clearTimeout(timer);
  }, [streamHold]);

  const bootstrap = useMutation({ mutationFn: (message: QueuedMessage) => api.bootstrapConversation(
    workspace!.id,
    conversationDraft!.id,
    newConversationProviderId,
    newConversationModelName,
    newConversationReasoningEffort,
    message.content,
    message.items,
    message.references.map(item => ({ event_id: item.eventId, content: item.content })),
    message.workspaceReferences ?? [],
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
    clearConversationDraft();
    clearBootstrapRecovery();
    setOperationError(undefined);
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
        setReferences(message.references);
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
      setReferences(message.references);
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
    setTitle(conversationName(conversation));
    setEditing(false);
    refresh();
  }, onError: error => reportOperationError(selected?.id, error) });
  const confirmDeletion = async (kind: '会话' | '工作区', name: string): Promise<boolean> => {
    return dialog.confirm({
      title: `删除${kind}“${name}”？`,
      message: kind === '会话'
        ? '会永久删除该会话及其附件和事件记录，无法恢复。'
        : '会永久删除该工作区、其中的会话、附件和冻结目录版本，无法恢复。',
      confirmLabel: '确认删除',
      tone: 'danger',
    });
  };
  const remove = useMutation({ mutationFn: (bindingId: string) => api.deleteConversation(workspace!.id, bindingId), onSuccess: (_value, bindingId) => {
    if (selected?.id === bindingId) {
      setDrawerOpen(false);
      onNavigate(host.rootPath, true);
    }
    refresh();
  }, onError: error => setOperationError(error) });
  const persistModel = useMutation({
    mutationFn: ({ providerId, modelName, effort }: { providerId: string; modelName: string; effort: string | null }) => api.switchConversationModel(workspace!.id, selected!.id, providerId, modelName, effort),
    onSuccess: value => {
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace!.id) });
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
    mutationFn: (message: BoundQueuedMessage) => api.sendMessage(workspace!.id, message.bindingId, message.content, message.items, message.references.map(item => ({ event_id: item.eventId, content: item.content })), message.workspaceReferences ?? []),
    onMutate: message => {
      const optimisticEventId = `pending-user:${randomId()}`;
      if (message.nativeGuidance) {
        // Preserve the active streamed answer. This optimistic user event is
        // only a local projection until the formal OpenHands cursor returns.
        setLiveEvents(current => mergeConversationEvents(current, [{
          id: optimisticEventId,
          event_type: 'MESSAGE',
          payload: {
            source: 'user',
            content: message.content,
            attachments: message.items,
            conversation_references: message.references.map(item => ({ event_id: item.eventId, content: item.content })),
            workspace_references: message.workspaceReferences,
          },
        }]));
        return { optimisticEventId, nativeGuidance: true };
      }
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(Date.now());
      setLiveEvents([{ id: optimisticEventId, event_type: 'MESSAGE', payload: { source: 'user', content: message.content, conversation_references: message.references.map(item => ({ event_id: item.eventId, content: item.content })), workspace_references: message.workspaceReferences } }]);
      setTurnState('running');
      return { optimisticEventId, nativeGuidance: false };
    },
    onSuccess: (value, message, context) => {
      const cursor = value.cursor;
      if (cursor) {
        if (!context?.nativeGuidance) setActiveTurnEventId(cursor);
        setLiveEvents(current => mergeConversationEvents(
          current.filter(event => event.id !== context?.optimisticEventId),
          [{ id: cursor, event_type: 'MESSAGE', payload: { source: 'user', content: message.content, attachments: message.items, conversation_references: message.references.map(item => ({ event_id: item.eventId, content: item.content })), workspace_references: message.workspaceReferences } }],
        ));
      }
      if (!context?.nativeGuidance) setAttachments([]);
      refresh();
    },
    onError: (error, message, context) => {
      if (context?.nativeGuidance) {
        setLiveEvents(current => current.filter(event => event.id !== context.optimisticEventId));
        // Do not disturb the active native turn. Restore only empty composer
        // fields so text entered after the shortcut is never overwritten.
        if (activeComposerScope.current === message.bindingId) {
          setDraft(current => current || message.content);
          setAttachments(current => current.length ? current : message.items);
          setReferences(current => current.length ? current : message.references);
        }
        reportOperationError(message.bindingId, error);
        return;
      }
      if (error instanceof ApiError && error.code === 'AGENT_CONVERSATION_BUSY') {
        setQueuedMessages(current => [...current, { id: message.id, scope: message.bindingId, content: message.content, items: message.items, references: message.references, workspaceReferences: message.workspaceReferences }]);
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
      setPendingMigratedSend({ ...message, bindingId: value.id });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
      onNavigate(host.conversationPath(value.id));
    },
    onError: (error, message) => {
      if (activeComposerScope.current === message.scope) { setDraft(message.content); setAttachments(message.items); setReferences(message.references); }
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
        queryClient.setQueryData<OpenHandsConversationEventBatch>(queryKey, current => current
          ? {
              ...current,
              ...batch,
              events: mergeConversationEvents(current.events, batch.events),
              history_cursor: current.history_cursor ?? batch.history_cursor,
            }
          : batch,
        );
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
  const interrupt = useMutation({ mutationFn: () => api.interruptConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('pausing'), onSuccess: () => { refresh(); onHostStateChanged?.(); }, onError: error => {
    setTurnState('running');
    reportOperationError(selected?.id, error);
  } });
  const resume = useMutation({ mutationFn: () => api.resumeConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('resuming'), onSuccess: value => { if (value.cursor) setActiveTurnEventId(value.cursor); setTurnState('running'); refresh(); onHostStateChanged?.(); }, onError: error => { setTurnState('paused'); reportOperationError(selected?.id, error); } });
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
    clearConversationDraft();
    setConversationDraft({ ...next, id: randomId(), capabilityVersionIds: next.capabilityVersionIds ?? [] });
    setPendingBootstrap(undefined);
    setWorkspaceScopeMigration(undefined);
    setDraft('');
    setAttachments([]);
    setReferences([]); setWorkspaceReferences([]);
    clearLiveText();
    setLiveEvents([]);
    setOptimisticBootstrapTurn(undefined);
    setHiddenEventIds(new Set());
    setTurnState('idle');
    onNavigate(host.rootPath);
  }, [clearBootstrapRecovery, clearConversationDraft, clearLiveText, host.rootPath, onNavigate]);
  useEffect(() => {
    if (!autoOpenDraft || !workspace || !runtimeWritable || selectedBindingId || conversationDraft) return;
    openConversationDraft({ displayName: '根工作区' });
  }, [autoOpenDraft, conversationDraft, openConversationDraft, runtimeWritable, selectedBindingId, workspace]);
  const enqueueDraft = useCallback(() => {
    const content = draft.trim();
    if ((!content && !attachments.length && !references.length && !workspaceReferences.length) || migrateStreaming.isPending || pendingMigratedSend || turnState === 'pausing' || turnState === 'resuming') return;
    setDraft('');
    setOperationError(undefined);
    if (!composerScope) return;
    const message = { id: randomId(), scope: composerScope, content, items: attachments, references, workspaceReferences };
    setAttachments([]);
    setReferences([]); setWorkspaceReferences([]);
    if (conversationDraft) {
      if (canBootstrap && !bootstrap.isPending) {
        setPendingBootstrap({ draft: conversationDraft, message });
        setOptimisticBootstrapTurn({
          scope: conversationDraft.id,
          event: { id: `pending-bootstrap:${message.id}`, event_type: 'MESSAGE', payload: { source: 'user', content, attachments, conversation_references: references.map(item => ({ event_id: item.eventId, content: item.content })), workspace_references: workspaceReferences } },
        });
        setRequestStartedAt(Date.now());
        setTurnState('running');
        bootstrap.mutate(message);
      }
      else { setDraft(content); setAttachments(attachments); setReferences(references); setWorkspaceReferences(workspaceReferences); }
      return;
    }
    if (!canWrite) { setDraft(content); setAttachments(attachments); setReferences(references); setWorkspaceReferences(workspaceReferences); return; }
    if (turnState === 'idle') {
      if (selected?.streaming_callback_ready) send.mutate({ ...message, bindingId: selected.id });
      else migrateStreaming.mutate(message);
    }
    else setQueuedMessages(items => [...items, message]);
  }, [attachments, bootstrap, canBootstrap, canWrite, composerScope, conversationDraft, draft, migrateStreaming, pendingMigratedSend, references, selected, send, turnState, workspaceReferences]);
  const sendDraftDirectly = useCallback(() => {
    const content = draft.trim();
    const hasComposerMessage = Boolean(content || attachments.length || references.length || workspaceReferences.length);
    if (!hasComposerMessage) {
      const queuedMessage = queuedMessages[0];
      if (!queuedMessage || !canWrite || conversationDraft || migrateStreaming.isPending || pendingMigratedSend
        || turnState !== 'running' || !selected?.streaming_callback_ready || queuedMessage.scope !== selected.id) return;
      setQueuedMessages(items => items.slice(1));
      // With an empty composer, Command/Ctrl+Enter promotes the current queue
      // head. It uses the same formal native-guidance route as "调整方向".
      setPendingNativeGuidance(current => [{ ...queuedMessage, bindingId: selected.id, nativeGuidance: true }, ...current]);
      return;
    }
    if (!composerScope || conversationDraft || !canWrite || migrateStreaming.isPending || pendingMigratedSend
      || turnState === 'pausing' || turnState === 'resuming') return;
    const message = { id: randomId(), scope: composerScope, content, items: attachments, references, workspaceReferences };
    setDraft('');
    setAttachments([]);
    setReferences([]); setWorkspaceReferences([]);
    setOperationError(undefined);
    if (turnState === 'running') {
      // This is not a pause: OpenHands appends the formal user event and
      // consumes it after the current LLM/tool step finishes. Submit direct
      // guidance in FIFO order because each native append can wait for the
      // active event lock while the Agent completes its current step.
      if (selected?.streaming_callback_ready) {
        setPendingNativeGuidance(current => [...current, {
          ...message,
          bindingId: selected.id,
          nativeGuidance: true,
        }]);
      }
      else {
        setDraft(content);
        setAttachments(attachments);
        setReferences(references); setWorkspaceReferences(workspaceReferences);
      }
      return;
    }
    if (turnState === 'idle') {
      if (selected?.streaming_callback_ready) send.mutate({ ...message, bindingId: selected.id });
      else migrateStreaming.mutate(message);
      return;
    }
    setDraft(content);
    setAttachments(attachments);
    setReferences(references); setWorkspaceReferences(workspaceReferences);
  }, [attachments, canWrite, composerScope, conversationDraft, draft, migrateStreaming, pendingMigratedSend, queuedMessages, references, selected, send, turnState, workspaceReferences]);
  const sendQueuedMessageImmediately = useCallback((message: QueuedMessage) => {
    if (!canWrite || turnState !== 'running' || !selected?.streaming_callback_ready || message.scope !== selected.id) return;
    setQueuedMessages(items => items.filter(item => item.id !== message.id));
    // Insert ahead of browser-queued native guidance. The active request, if
    // any, finishes first; OpenHands still owns the formal turn lock.
    setPendingNativeGuidance(current => [{ ...message, bindingId: selected.id, nativeGuidance: true }, ...current]);
  }, [canWrite, selected, turnState]);
  const moveQueuedMessage = useCallback((sourceId: string, targetId: string) => {
    if (sourceId === targetId) return;
    setQueuedMessages(items => {
      const sourceIndex = items.findIndex(item => item.id === sourceId);
      const targetIndex = items.findIndex(item => item.id === targetId);
      if (sourceIndex < 0 || targetIndex < 0) return items;
      const next = [...items];
      const [source] = next.splice(sourceIndex, 1);
      next.splice(sourceIndex < targetIndex ? targetIndex - 1 : targetIndex, 0, source);
      return next;
    });
  }, []);
  const moveQueuedMessageByOffset = useCallback((sourceId: string, offset: -1 | 1) => {
    setQueuedMessages(items => {
      const sourceIndex = items.findIndex(item => item.id === sourceId);
      const targetIndex = sourceIndex + offset;
      if (sourceIndex < 0 || targetIndex < 0 || targetIndex >= items.length) return items;
      const next = [...items];
      const [source] = next.splice(sourceIndex, 1);
      next.splice(targetIndex, 0, source);
      return next;
    });
  }, []);
  useEffect(() => {
    if (!pendingNativeGuidance.length || send.isPending || !selected?.streaming_callback_ready
      || (turnState !== 'running' && turnState !== 'idle')) return;
    const [next, ...rest] = pendingNativeGuidance;
    setPendingNativeGuidance(rest);
    // If the previous native turn finished before this browser-side FIFO
    // reaches the API, send it as the next ordinary turn rather than carrying
    // running-turn UI semantics into an idle Conversation.
    send.mutate(turnState === 'running' ? next : { ...next, nativeGuidance: false });
  }, [pendingNativeGuidance, selected?.streaming_callback_ready, send, turnState]);
  useEffect(() => {
    if (turnState !== 'idle' || pendingNativeGuidance.length || !queuedMessages.length || send.isPending || migrateStreaming.isPending || pendingMigratedSend) return;
    const [next, ...rest] = queuedMessages;
    setQueuedMessages(rest);
    if (selected?.streaming_callback_ready) send.mutate({ ...next, bindingId: selected.id });
    else migrateStreaming.mutate(next);
  }, [migrateStreaming, pendingMigratedSend, pendingNativeGuidance.length, queuedMessages, selected, send, turnState]);
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
    || (turnState === 'idle' && ((!draft.trim() && !attachments.length && !references.length) || send.isPending))
    || turnState === 'pausing'
    || turnState === 'resuming';
  const runComposerAction = () => {
    if (turnState === 'idle') enqueueDraft();
    else if (turnState === 'running') interrupt.mutate();
    else if (turnState === 'paused') resume.mutate();
  };
  const workDirectories = workDirectoriesQuery.data?.items ?? [];
  // A conversation can predate the explicit work-directory binding while
  // still carrying its authoritative working_directory.  Prefer it over the
  // shared project root so paths in its transcript stay relative to the
  // directory where that conversation actually ran.
  const activeWorkspaceRoot = activeWorkspaceDetailsQuery.data?.working_directory
    ?? selected?.working_directory
    ?? (selected?.work_directory_id
      ? workDirectories.find(directory => directory.id === selected.work_directory_id)?.current_version.working_directory
      : undefined)
    ?? (conversationDraft?.workDirectoryId
      ? workDirectories.find(directory => directory.id === conversationDraft.workDirectoryId)?.current_version.working_directory
      : undefined)
    ?? workDirectoriesQuery.data?.root.working_directory;
  const rootWorkspaceDirectory = workDirectoriesQuery.data?.root.working_directory;
  const currentWorkspaceName = activeWorkspaceDetailsQuery.data?.scope.display_name
    ?? (selected?.work_directory_id
      ? workDirectories.find(directory => directory.id === selected.work_directory_id)?.display_name
      : undefined)
    ?? conversationDraft?.displayName
    ?? '根工作区';
  const currentWorkspaceRelativePath = workspaceRelativePath(
    activeWorkspaceRoot ?? rootWorkspaceDirectory ?? '',
    rootWorkspaceDirectory,
  );
  const copyCurrentWorkspacePath = () => {
    void copyTextToClipboard(currentWorkspaceRelativePath).then(() => {
      setWorkspacePathCopied(true);
      if (workspacePathCopyTimer.current !== undefined) window.clearTimeout(workspacePathCopyTimer.current);
      workspacePathCopyTimer.current = window.setTimeout(() => {
        setWorkspacePathCopied(false);
        workspacePathCopyTimer.current = undefined;
      }, WORKSPACE_PATH_COPIED_DURATION_MS);
    }).catch(() => undefined);
  };
  const conversationRow = (item: AgentConversation) => {
    // The list projection is the native OpenHands running snapshot for every
    // visible conversation. Local state only bridges the selected row between
    // a send/interrupt action and the next bounded list refresh.
    // The selected conversation also has an explicit native readiness read.
    // Once that authoritative read is terminal, do not let a delayed batch
    // list snapshot keep its running marker spinning.
    const selectedNativeIdle = item.id === selected?.id
      && inputReadinessQuery.data?.ready === true
      && ['idle', 'completed', 'stopped'].includes(
        inputReadinessQuery.data.execution_status?.trim().toLowerCase() ?? '',
      );
    const running = !selectedNativeIdle && (conversationIsRunning(item.execution_status)
      || (item.id === selected?.id && (selectedConversationRunning || isGenerating)));
    return <WorkspaceConversationRow key={item.id} item={item} selectedBindingId={selectedBindingId} running={running} unread={unreadConversationIds.has(item.id)} runtimeWritable={runtimeWritable} removing={remove.isPending} deleteDisabled={running} onSelect={() => selectConversation(item.id)} onDelete={features.conversationDeletion ? () => void confirmDeletion('会话', conversationName(item)).then(ok => { if (ok) remove.mutate(item.id); }) : undefined}/>;
  };
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
    clearConversationDraft();
    onNavigate(host.conversationPath(bindingId));
  };
  const removeWorkDirectory = async (directory: AgentSessionWorkDirectory) => {
    if (!api.deleteWorkDirectory || !await confirmDeletion('工作区', directory.display_name)) return;
    setOperationError(undefined);
    try {
      await api.deleteWorkDirectory(workspace!.id, directory.id);
      if (conversationDraft?.workDirectoryId === directory.id) setConversationDraft(undefined);
      await queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'work-directories', workspace!.id) });
      setOperationError(undefined);
    } catch (reason) { reportOperationError('work-directory-delete', reason instanceof Error ? reason : new Error('删除工作区失败')); }
  };
  return <main className="agent-workbench-page">
    {selected && <ConversationStreamObserver workspaceId={workspace.id} bindingId={selected.id} enabled={streamEnabled} onEvent={onStreamEvent} onStatus={setStreamStatus} onReconnect={onStreamReconnect}/>}
    <aside className="agent-workbench-rail">
      <header className={!onReturnToSource && features.workDirectories ? 'agent-workbench-rail-actions-only' : undefined}>{onReturnToSource && <button type="button" className="agent-session-return" aria-label="返回节点执行" title="返回节点执行" onClick={onReturnToSource}><ArrowLeft size={16}/></button>}{(onReturnToSource || !features.workDirectories) && <div className="agent-session-host-heading"><span className="eyebrow">{onReturnToSource ? 'FLOWRUN NODE WORKSPACE' : 'FLOWRUN NODE'}</span><h1>{onReturnToSource ? workspace?.display_name || '节点会话' : '节点会话'}</h1></div>}<div className="agent-workbench-create-actions"><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: '根工作区' })}><Plus size={15}/>新建会话</button>{features.workDirectories && <button type="button" className="secondary" aria-label="新增工作区" disabled={!runtimeWritable} onClick={() => setWorkDirectoryCreatorOpen(true)}><FolderPlus size={14}/>新增工作区</button>}</div></header>
      <div className="agent-workbench-list">
        <WorkspaceConversationGroup groupId="root" label="根工作区" conversationCount={rootConversations.length} canCreateConversation={canOpenConversation} onCreateConversation={() => openConversationDraft({ displayName: '根工作区' })}>
          {visibleCount => <>{pendingBootstrapItem && !pendingBootstrap?.draft.workDirectoryId ? pendingBootstrapItem : null}{rootConversations.slice(0, visibleCount).map(conversationRow)}</>}
        </WorkspaceConversationGroup>
        {features.workDirectories && workDirectories.map(directory => <WorkspaceConversationGroup key={directory.id} groupId={directory.id} label={directory.display_name} conversationCount={conversationsForDirectory(directory.id).length} canCreateConversation={canOpenConversation} onCreateConversation={() => openConversationDraft({ workDirectoryId: directory.id, displayName: directory.display_name })} onDelete={api.deleteWorkDirectory && runtimeWritable ? () => void removeWorkDirectory(directory) : undefined}>
          {visibleCount => <>{pendingBootstrapItem && pendingBootstrap?.draft.workDirectoryId === directory.id ? pendingBootstrapItem : null}{conversationsForDirectory(directory.id).slice(0, visibleCount).map(conversationRow)}</>}
        </WorkspaceConversationGroup>)}
      </div>
      {features.capabilities && (selected || features.draftCapabilitySelection) && <footer className="agent-workbench-rail-footer"><button type="button" disabled={!runtimeWritable} onClick={() => setCapabilityManagerOpen(true)}><Boxes size={15}/><span><b>能力</b><small>{selected ? '管理当前会话能力' : '为新会话选择能力'}</small></span><ChevronRight size={14}/></button></footer>}
    </aside>
    <section className="agent-workbench-main">
      <header className="agent-workbench-header"><div>{editing ? <div className="agent-title-edit"><input ref={titleInput} aria-label="会话标题" value={title} onChange={event => setTitle(event.target.value)} onBlur={() => { if (!rename.isPending) { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }} onKeyDown={event => { if (event.key === 'Enter' && title.trim()) { event.preventDefault(); rename.mutate(); } if (event.key === 'Escape') { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }}/></div> : !(hideDraftTitle && conversationDraft) && <h2 className="agent-session-title" title={selected ? conversationName(selected) : undefined} aria-label={selected && canWrite ? '双击修改标题' : undefined} onDoubleClick={() => { if (!selected || !canWrite) return; setTitle(conversationName(selected)); setEditing(true); }}><span>{selected ? conversationName(selected) : conversationDraft ? '新会话' : '开始一个新的会话'}</span></h2>}{features.modelSelection && (selected || conversationDraft) && <small className="agent-session-provider">当前供应商：{selected ? boundProviderInfo?.name ?? '未配置' : draftProviderInfo?.name ?? '请选择模型供应商'}{conversationDraft ? ` · ${conversationDraft.displayName}` : ''}</small>}</div><div className="agent-header-actions">{features.conversationDeletion && selected && <button type="button" className="danger" aria-label="删除会话" title={selectedConversationRunning ? '会话运行中，请先停止' : '删除会话'} disabled={!canWrite || selectedConversationRunning || remove.isPending} onClick={() => void confirmDeletion('会话', conversationName(selected)).then(ok => { if (ok) remove.mutate(selected.id); })}><Trash2 size={14}/></button>}</div></header>
      {runtime?.state === 'RECOVERING' && <section className="agent-runtime-recover"><LoaderCircle size={18}/><div><b>运行环境正在恢复</b><span>{runtime.message || '历史会话和工作区文件仍可查看；恢复完成后可继续发送消息和使用终端。'}</span></div></section>}
      {runtime && !runtime.write_available && runtime.state !== 'RECOVERING' && <section className="agent-runtime-recover"><ShieldAlert size={18}/><div><b>节点会话已切换为只读</b><span>{runtime.message || '节点执行已停止；历史会话和工作区文件仍可查看。'}</span></div></section>}
      {selected && !compactionPolicyCurrent && <section className="agent-compaction-policy-warning" aria-label="历史压缩策略兼容保护"><ShieldAlert size={18}/><div><b>已启用历史会话兼容保护</b><span>此会话继承了旧的事件数压缩策略。继续发送或恢复执行前，系统会先调用 OpenHands 原生压缩并校验摘要；校验失败时不会发送新消息。</span>{features.workDirectories && <button type="button" className="primary" disabled={!canOpenConversation} onClick={openCurrentDirectoryDraft}><Plus size={14}/>在相同工作目录新建会话</button>}</div></section>}
      {selected || conversationDraft ? <ConversationSurface key={selected?.id ?? conversationDraft?.id} events={displayedEvents} liveText={liveText} isGenerating={isGenerating} isPaused={inputReadinessQuery.data?.execution_status?.toLowerCase() === 'paused'} requestStartedAt={requestStartedAt} requestSubmitting={send.isPending || bootstrap.isPending || rewrite.isPending} condensationStatus={selected && condensationStatus?.bindingId === selected.id ? condensationStatus : undefined} onRetryCondensation={selected && canWrite && condensationStatus?.bindingId === selected.id && condensationStatus.state === 'failed' ? requestManualCompaction : undefined} onRewrite={selected && canWrite && features.rewrite ? requestRewrite : undefined} onFork={selected && canWrite && features.fork ? eventId => { if (fork.isPending) return; const directoryName = selected.work_directory_id ? workDirectories.find(directory => directory.id === selected.work_directory_id)?.display_name ?? '当前工作区' : '节点工作目录'; void dialog.confirm({ title: '从此处分叉会话？', message: `将保留当前会话在“${directoryName}”中的工作目录和截至此回复的历史记录，创建一条可独立继续的新会话。源会话不会被修改。`, confirmLabel: '创建分叉会话' }).then(confirmed => { if (confirmed) fork.mutate(eventId); }); } : undefined} onOpenAttachment={features.attachments ? openAttachmentInDrawer : undefined} onPreviewCandidateFile={candidateOutputUrl && workspace ? openCandidateFileInDrawer : undefined} onReviewChanges={openChangesReview} workspaceRoot={activeWorkspaceRoot} onAddReference={runtimeWritable ? reference => setReferences(current => current.some(item => item.eventId === reference.eventId && item.content === reference.content) ? current : [...current, reference]) : undefined} taskControl={eventsQuery.data?.task_control ?? []} monitoring={eventsQuery.data?.monitoring} connectionState={inputReadinessQuery.isError ? 'unavailable' : streamStatus === 'recovering' ? 'recovering' : streamStatus === 'connecting' ? 'checking' : inputReadinessQuery.isFetching && !inputReadinessQuery.data ? 'checking' : 'connected'}/> : <div className="agent-workbench-empty"><Bot size={32}/><b>新建会话开始协作</b><span>{features.workDirectories ? '每个会话共享同一工作区，但保留独立的对话与事件记录。' : '会话固定在当前节点 Attempt 的隔离工作目录。'}</span><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: features.workDirectories ? '根工作区' : '节点工作目录' })}><Plus size={15}/>新建会话</button></div>}
      {(selected || conversationDraft) && runtimeWritable && runtime?.state !== 'RECOVERING' && <div className="agent-composer-dock">
        <div className={`agent-composer ${turnState !== 'idle' || pendingConfirmation ? 'busy' : ''}`}>
        {pendingConfirmation && <section className="agent-confirmation" aria-label="工具执行确认"><header><ShieldAlert size={17}/><div><b>工具正在等待你的确认</b><span>动作尚未执行。请核对整批内容后批准或拒绝。</span></div></header><div className="agent-confirmation-actions">{(pendingConfirmation.actions ?? []).map((action: AgentPendingConfirmationAction) => <article key={action.digest}><div><b>{action.summary || action.tool_name}</b><span>{action.security_risk || 'UNKNOWN'}</span></div>{Object.keys(action.arguments).length > 0 && <pre>{JSON.stringify(action.arguments, null, 2)}</pre>}</article>)}</div><textarea aria-label="工具确认理由" value={confirmationReason} maxLength={2000} placeholder="填写批准或拒绝理由…" onChange={event => setConfirmationReason(event.target.value)}/><footer><button type="button" className="danger" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(false)}><X size={14}/>拒绝整批</button><button type="button" className="primary" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(true)}><Check size={14}/>批准整批</button></footer></section>}
        {queuedMessages.length > 0 && <section className="agent-queued-messages" aria-label="已排队消息"><header><b>消息队列</b><span>{queuedMessages.length} 条将在当前回复完成后依次发送</span></header>{queuedMessages.map((message, index) => <article key={message.id} draggable onDragStart={event => { if (!(event.target instanceof Element) || !event.target.closest('.queue-drag-handle')) { event.preventDefault(); return; } event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', message.id); setDraggedQueuedMessageId(message.id); }} onDragOver={event => { if (draggedQueuedMessageId && draggedQueuedMessageId !== message.id) event.preventDefault(); }} onDrop={event => { event.preventDefault(); const sourceId = event.dataTransfer.getData('text/plain') || draggedQueuedMessageId; if (sourceId) moveQueuedMessage(sourceId, message.id); setDraggedQueuedMessageId(undefined); }} onDragEnd={() => setDraggedQueuedMessageId(undefined)} className={draggedQueuedMessageId === message.id ? 'dragging' : undefined}><button type="button" className="queue-drag-handle" aria-label={`拖动排队消息 ${index + 1} 以调整顺序`} title="拖动调整顺序" tabIndex={-1}><GripVertical size={14}/></button><small>{index + 1}</small><p>{message.content || (message.references.length ? `会话引用 ${message.references.length} 条` : message.workspaceReferences?.length ? `工作区引用 ${message.workspaceReferences.length} 条` : '图片附件')}</p><span>{[message.items.length ? `${message.items.length} 个附件` : '', message.references.length ? `${message.references.length} 条会话引用` : '', message.workspaceReferences?.length ? `${message.workspaceReferences.length} 条工作区引用` : ''].filter(Boolean).join(' · ')}</span><div><button type="button" aria-label={`调整方向排队消息 ${index + 1}`} title="立即发送，调整当前回复方向" disabled={!canWrite || turnState !== 'running' || !selected?.streaming_callback_ready || message.scope !== selected.id} onClick={() => sendQueuedMessageImmediately(message)}><CornerDownRight size={12}/>调整方向</button><button type="button" className="queue-remove" aria-label={`移除排队消息 ${index + 1}`} onClick={() => setQueuedMessages(items => items.filter(item => item.id !== message.id))}><X size={13}/></button><button type="button" className="queue-more" aria-label={`更多排队消息操作 ${index + 1}`} title="更多操作" aria-expanded={queuedMessageMenuId === message.id} onClick={() => setQueuedMessageMenuId(current => current === message.id ? undefined : message.id)}><Ellipsis size={14}/></button>{queuedMessageMenuId === message.id && <div className="queue-menu" role="menu"><button type="button" role="menuitem" disabled={index === 0} onClick={() => { moveQueuedMessageByOffset(message.id, -1); setQueuedMessageMenuId(undefined); }}>上移</button><button type="button" role="menuitem" disabled={index === queuedMessages.length - 1} onClick={() => { moveQueuedMessageByOffset(message.id, 1); setQueuedMessageMenuId(undefined); }}>下移</button><button type="button" role="menuitem" onClick={() => { setDraft(message.content); setAttachments(message.items); setReferences(message.references); setWorkspaceReferences(message.workspaceReferences ?? []); setQueuedMessages(items => items.filter(item => item.id !== message.id)); setQueuedMessageMenuId(undefined); }}>编辑</button></div>}</div></article>)}</section>}
        {condensationConfirmationOpen && selected && <section className="agent-condensation-confirmation" aria-label="确认低用量上下文压缩" role="alertdialog" aria-modal="false">
          <ShieldAlert size={17}/><div><b>当前上下文用量较低</b><p>Token {contextProgress?.usedLabel} / {contextProgress?.windowLabel}（{contextProgress?.percentage}%），事件 {activeEventCount.toLocaleString()} / {eventLimit.toLocaleString()}（{eventProgress}%）。现在压缩可能没有足够的可压缩区间，并且仍会调用摘要模型。</p><footer><button type="button" onClick={() => setCondensationConfirmationOpen(false)}>取消</button><button type="button" className="primary" onClick={() => condense.mutate()}>仍然压缩</button></footer></div>
        </section>}
        <ComposerCapabilityAutocomplete draft={draft} suggestions={composerSuggestions} placeholder={pendingConfirmation ? '请先处理上方工具确认…' : turnState === 'paused' ? '已暂停：可继续，也可编辑上方消息重新思考…' : features.capabilities ? '给 Agent 发消息…（Enter 加入队列，⌘/Ctrl+Enter 直接发送）' : '给 Agent 发消息…'} disabled={!canCompose || Boolean(pendingConfirmation) || bootstrap.isPending || condense.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend) || turnState === 'pausing' || turnState === 'resuming'} onDraftChange={setDraft} onPaste={event => { if (!features.attachments || !composerScope) return; const files = transferredFiles(event.clipboardData); if (!files.length) return; event.preventDefault(); for (const file of files) upload.mutate({ file, scope: composerScope }); }} onDropFiles={features.attachments && composerScope ? files => { for (const file of files) upload.mutate({ file, scope: composerScope }); } : undefined} onDropWorkspaceFiles={paths => setWorkspaceReferences(current => [...current, ...paths.flatMap(path => current.some(reference => reference.path === path) ? [] : [{ path, kind: 'file' as const, display_name: path.split('/').filter(Boolean).pop() ?? path }])])} onSubmit={enqueueDraft} onDirectSubmit={sendDraftDirectly} onManageCapabilities={features.capabilities && (selected || features.draftCapabilitySelection) ? () => setCapabilityManagerOpen(true) : undefined} onNativeAction={action => { if (action === 'CONDENSE' && selected && (turnState === 'idle' || turnState === 'paused') && !pendingConfirmation && !condense.isPending) requestManualCompaction(); }} onWorkspaceReferenceSelected={() => { setWorkspaceReferenceQuery(''); setWorkspaceReferencePickerOpen(true); }}/>
        {features.attachments && attachments.length > 0 && <div className="agent-attachments">{attachments.map(item => <span key={item.path}><button type="button" className="agent-attachment-open" title={`在右侧查看附件：${item.filename}`} onClick={() => openAttachmentInDrawer(item)}>{item.image_data_url && <img src={item.image_data_url} alt=""/>}<em>{item.filename}</em></button><button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(all => all.filter(candidate => candidate.path !== item.path))}>×</button></span>)}</div>}
        {references.length > 0 && <div className="agent-attachments agent-conversation-references" aria-label="已添加的会话引用">{references.map((reference, index) => <span key={`${reference.eventId}:${reference.content}`}><span className="agent-attachment-open" title={reference.content}><Quote size={14}/><em>{`会话引用 ${index + 1}`}</em></span><button type="button" className="agent-attachment-remove" aria-label={`移除会话引用 ${index + 1}`} onClick={() => setReferences(current => current.filter(item => item !== reference))}>×</button></span>)}</div>}
        {workspaceReferences.length > 0 && <div className="agent-attachments agent-workspace-references" aria-label="已添加的工作区引用">{workspaceReferences.map(reference => <span key={workspaceReferenceKey(reference)} title={reference.path}><span className="agent-attachment-open">{reference.kind === 'directory' ? <Folder size={14}/> : <FileCode2 size={14}/>}<em><b>{reference.display_name}</b><small>{workspaceReferenceLabel(reference)}</small></em></span><button type="button" className="agent-attachment-remove" aria-label={'移除工作区引用 ' + reference.display_name} onClick={() => setWorkspaceReferences(current => current.filter(item => workspaceReferenceKey(item) !== workspaceReferenceKey(reference)))}>×</button></span>)}</div>}
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
        </div>
        <div className="agent-composer-bottom">
          <ConversationTaskPlan events={displayedEvents} isGenerating={isGenerating}/>
          <button type="button" className={`agent-current-workspace${workspacePathCopied ? ' copied' : ''}`} title={`${workspacePathCopied ? '已复制' : '点击复制'}：${currentWorkspaceRelativePath}`} aria-label={workspacePathCopied ? `工作区路径已复制：${currentWorkspaceRelativePath}` : `当前工作区：${currentWorkspaceName}；点击复制相对根工作区路径：${currentWorkspaceRelativePath}`} onClick={copyCurrentWorkspacePath}>
            <Folder size={16}/><span>{currentWorkspaceName}</span>{workspacePathCopied && <small aria-live="polite">已复制</small>}
          </button>
        </div>
      </div>}
      {visibleError && <p className="agent-workbench-error">{visibleError.message}</p>}
    </section>
    <WorkspaceDrawer open={drawerOpen} onOpen={() => setDrawerOpen(true)} onClose={() => { setFileSelectionReference(undefined); setDrawerOpen(false); }} onAddFileSelection={(path, selection) => setWorkspaceReferences(current => current.some(reference => reference.path === path && JSON.stringify(reference.selection) === JSON.stringify(selection)) ? current : [...current, { path, kind: 'file', display_name: path.split('/').filter(Boolean).pop() ?? path, selection }])} highlightedFileSelection={fileSelectionReference} workspaceId={workspace.id} scopeKey={selected?.id ?? pendingCreatedId ?? conversationDraft?.id ?? 'workspace-root'} migrateFromScopeKey={workspaceScopeMigration} bindingId={selected?.id} workDirectoryId={selected ? undefined : conversationDraft?.workDirectoryId} conversation={selected} attachments={drawerAttachments} sources={drawerSources} attachmentRequest={attachmentRequest} candidatePreviewRequest={candidatePreviewRequest} reviewChanges={reviewChanges} reviewRequestId={reviewRequestId} sessionChanges={sessionFileChanges} onReviewChanges={openChangesReview} runtimeAvailable={Boolean((runtime?.terminal_available ?? runtime?.write_available) && (!features.terminalRequiresConversation || selected))} runtimeTasks={runtimeTasks} agentDefinitions={agentDefinitionAssets} sessionStopped={sessionStopped}/>
    {workspaceReferencePickerOpen && <WorkspaceReferencePicker
      entries={workspaceReferenceIndexQuery.data?.files ?? []}
      root={workspaceReferenceIndexQuery.data?.working_directory ?? activeWorkspaceRoot ?? ''}
      query={workspaceReferenceQuery}
      onQueryChange={setWorkspaceReferenceQuery}
      selectedReferences={workspaceReferences}
      onApply={entries => {
        setWorkspaceReferences(entries.map(entry => ({
          path: entry.path,
          kind: entry.kind,
          display_name: entry.path.split('/').filter(Boolean).pop() ?? entry.path,
        })));
        setWorkspaceReferencePickerOpen(false);
        setWorkspaceReferenceQuery('');
      }}
      onClose={() => { setWorkspaceReferencePickerOpen(false); setWorkspaceReferenceQuery(''); }}
    />}
    {workDirectoryCreatorOpen && <WorkDirectoryCreator workspaceId={workspace.id} onClose={() => setWorkDirectoryCreatorOpen(false)} onCreated={directory => {
      queryClient.setQueryData<AgentSessionWorkDirectoryList>(sessionQueryKey(host, 'work-directories', workspace.id), current => current ? { ...current, items: [directory, ...current.items.filter(item => item.id !== directory.id)] } : current);
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'work-directories', workspace.id) });
    }}/>}
    {capabilityManagerOpen && (selected || features.draftCapabilitySelection) && <CapabilityManager workspaceId={workspace.id} bindingId={selected?.id} conversationCapabilities={selected?.capabilities} draftCapabilityIds={conversationDraft?.capabilityVersionIds} onClose={() => setCapabilityManagerOpen(false)} onCreateEnhancedConversation={capabilityVersionIds => { const directory = selected?.work_directory_id ? workDirectories.find(item => item.id === selected.work_directory_id) : undefined; setCapabilityManagerOpen(false); if (conversationDraft) setConversationDraft(current => current ? { ...current, capabilityVersionIds } : current); else openConversationDraft({ workDirectoryId: directory?.id, displayName: directory?.display_name ?? '根工作区', capabilityVersionIds }); }}/>}
  </main>;
}
