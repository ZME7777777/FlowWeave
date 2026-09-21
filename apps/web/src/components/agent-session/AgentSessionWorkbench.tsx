import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { type InfiniteData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import hljs from 'highlight.js/lib/common';
import { ArrowLeft, Bell, Bot, Boxes, Check, ChevronDown, ChevronRight, CircleDot, Copy, CornerDownRight, Download, Ellipsis, FileCode2, FileText, Folder, FolderOpen, FolderPlus, GitBranch, GripVertical, ImageIcon, Layers3, Link2, LoaderCircle, Maximize2, Minimize2, MonitorCog, PanelRightOpen, Pin, PinOff, Play, Plus, Quote, RefreshCw, Search, Send, ShieldAlert, Square, Trash2, X } from 'lucide-react';
import { createContext, forwardRef, isValidElement, useCallback, useContext, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type ComponentPropsWithoutRef, type CSSProperties, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type WheelEvent as ReactWheelEvent } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ApiError, randomId, type AgentStreamEvent } from '../../api/client';
import { agentWorkspaceSessionGateway, type AgentSessionGateway } from '../../api/agent-session-gateway';
import { withoutDeploymentBase } from '../../deploymentPath';
import { agentWorkspaceSessionHost, type AgentSessionHost } from './session-host';
import { ConversationSurface, ConversationTaskPlan, type ConversationHistoryPrepend, type ConversationReference } from '../ConversationSurface';
import { isOpenHandsAgentReply, isOpenHandsEmptyResponseRecovery } from '../conversationEvents';
import { useProductDialog } from '../ProductDialogContext';
import { useEscapeClose } from '../useEscapeClose';
import { MermaidDiagram } from '../MermaidDiagram';
import { isMermaidDiagram, markdownCodeText } from '../markdownCodeBlock';
import { selectCapabilityVersion, selectCapabilityVersions } from '../../utils/capabilitySelection';
import { SubagentAvatar } from '../SubagentAvatar';
import { subagentAvatarSlots, type SubagentAvatarSlot } from '../../utils/subagentAvatar';
import { workspaceFileChanges, workspaceRelativePath, type WorkspaceFileChange } from './fileChanges';
import type { AgentAttachment, AgentConversation, AgentConversationAnnotation, AgentConversationCredentialSyncEntry, AgentConversationPage, AgentConversationReference, AgentConversationSearch, AgentPendingConfirmationAction, AgentSessionCapability, AgentSessionMcpReadiness, AgentSessionWorkDirectory, AgentSessionWorkDirectoryList, AgentSessionWorkspaceDetails, AgentWorkspaceReference, CapabilityAsset, CapabilityCollection, ModelProvider, OpenHandsConversationEvent, OpenHandsConversationEventBatch, ProviderModel, RuntimeTaskControlSnapshot, RuntimeTaskUsageSnapshot, WorkspaceGitChangeKind, WorkspaceGitChangedFile, WorkspaceGitChanges, WorkspaceGitCommitDetails, WorkspaceGitFileDiff } from '../../types';
import '../../pages/agent-workbench.css';
import '../../pages/agent-workbench-layout.css';

const WORKSPACE_FILE_TRANSFER_TYPE = 'application/x-flowweave-workspace-file-path';
const ACTIVE_EVENT_RECOVERY_INTERVAL_MS = 4_000;
const WORKSPACE_PATH_COPIED_DURATION_MS = 1_500;
const SESSION_PERFORMANCE_MARK_PREFIX = 'flowweave.agent-session.';
// These are the frozen OpenHands/LiteLLM request settings applied by the
// Runtime payload. They govern each model request made while a Task runs;
// they are not a wall-clock deadline for the whole child task.
const MODEL_REQUEST_TIMEOUT_SECONDS = 120;
const MODEL_REQUEST_MAX_RETRIES = 3;
const DEFAULT_CONTEXT_COMPACTION_THRESHOLD_TOKENS = 256_000;
type StreamStatus = 'connecting' | 'live' | 'recovering' | 'disabled';
type TurnState = 'idle' | 'running' | 'pausing' | 'paused' | 'resuming';
type ConversationActivityState = TurnState | 'synchronizing';
type QueueDeliveryState = 'queued' | 'dispatching' | 'ambiguous' | 'rejected';
type ConversationOrderSync = { state: 'syncing' | 'failed'; orderedBindingIds: string[] };
interface RewriteRequest {
  eventId: string;
  content: string;
  attachments?: AgentAttachment[];
  references?: AgentConversationReference[];
  workspaceReferences?: AgentWorkspaceReference[];
  annotations?: AgentConversationAnnotation[];
}
interface QueuedMessage {
  id: string;
  scope: string;
  content: string;
  items: AgentAttachment[];
  references: ConversationReference[];
  workspaceReferences?: AgentWorkspaceReference[];
  annotations: AgentConversationAnnotation[];
  /** Browser-local intent only; OpenHands events remain the delivery fact. */
  deliveryState?: QueueDeliveryState;
  /** A formal user event may be appended while the current turn is running. */
  nativeGuidance?: boolean;
  createdAt?: number;
  deliveryError?: string;
}
type FileSelection = NonNullable<AgentWorkspaceReference['selection']>;

function annotationFileSelection(annotation: AgentConversationAnnotation): { path: string; selection: FileSelection } | undefined {
  if (annotation.anchor_kind !== 'WORKSPACE_FILE_RANGE') return undefined;
  const { path, selection } = annotation.anchor;
  if (typeof path !== 'string' || !selection || typeof selection !== 'object') return undefined;
  const value = selection as Record<string, unknown>;
  if (!['start_line', 'start_column', 'end_line', 'end_column'].every(name => typeof value[name] === 'number')) return undefined;
  return { path, selection: value as FileSelection };
}

function annotationFileDisplay(annotation: AgentConversationAnnotation): { filename: string; path: string; range: string } | undefined {
  const file = annotationFileSelection(annotation);
  if (!file) return undefined;
  const filename = file.path.split('/').filter(Boolean).at(-1) || file.path;
  const { start_line, start_column, end_line, end_column } = file.selection;
  return {
    filename,
    path: file.path,
    range: `${start_line}:${start_column}–${end_line}:${end_column}`,
  };
}

function annotationReferenceName(annotation: AgentConversationAnnotation, index: number): string {
  return annotationFileDisplay(annotation)?.filename ?? `会话引用 ${index + 1}`;
}

function ComposerAnnotationList({ annotations, onLocate, onRemove, onUpdate }: {
  annotations: AgentConversationAnnotation[];
  onLocate: (annotation: AgentConversationAnnotation) => void;
  onRemove: (annotation: AgentConversationAnnotation) => void;
  onUpdate: (annotation: AgentConversationAnnotation, comment: string) => void;
}) {
  const rootRef = useRef<HTMLElement>(null);
  const [openedId, setOpenedId] = useState<string>();
  const [editingId, setEditingId] = useState<string>();
  const [comment, setComment] = useState('');
  const latestAnnotationId = annotations.at(-1)?.id;
  const latestAnnotationRef = useRef<AgentConversationAnnotation | undefined>(undefined);
  latestAnnotationRef.current = annotations.at(-1);
  useEffect(() => {
    const latest = latestAnnotationRef.current;
    if (!latestAnnotationId || !latest) return;
    setOpenedId(latestAnnotationId);
    setEditingId(latestAnnotationId);
    setComment(latest.comment);
  }, [latestAnnotationId]);
  const opened = annotations.find(annotation => annotation.id === openedId);
  const openedIndex = opened ? annotations.findIndex(annotation => annotation.id === opened.id) : -1;
  const openedFile = opened ? annotationFileDisplay(opened) : undefined;
  const closeOpened = useCallback(() => {
    if (opened && editingId === opened.id) onUpdate(opened, comment);
    setEditingId(undefined);
    setOpenedId(undefined);
  }, [comment, editingId, onUpdate, opened]);
  useEscapeClose(closeOpened, Boolean(opened));
  useEffect(() => {
    if (!opened) return;
    const closeWhenPointerLeavesCard = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) closeOpened();
    };
    document.addEventListener('pointerdown', closeWhenPointerLeavesCard, true);
    return () => document.removeEventListener('pointerdown', closeWhenPointerLeavesCard, true);
  }, [closeOpened, opened]);
  if (!annotations.length) return null;
  return <section ref={rootRef} className="agent-composer-annotations" aria-label={`已添加的引用 ${annotations.length} 条`}>
    <div className="agent-attachments agent-conversation-references">{annotations.map((annotation, index) => {
      const file = annotationFileDisplay(annotation);
      const referenceName = annotationReferenceName(annotation, index);
      return <span key={annotation.id}>
        <button type="button" className="agent-attachment-open" title={file ? `${file.path} · ${file.range}` : '查看、定位或编辑评论'} aria-expanded={openedId === annotation.id} onClick={() => {
          if (openedId === annotation.id) { closeOpened(); return; }
          setComment(annotation.comment);
          setEditingId(undefined);
          setOpenedId(annotation.id);
        }}>{file ? <FileText size={14}/> : <Quote size={14}/>}<em>{file ? `${file.filename} · ${file.range}` : referenceName}</em></button>
        <button type="button" className="agent-attachment-remove" aria-label={`移除${referenceName}`} onClick={() => { setOpenedId(current => current === annotation.id ? undefined : current); setEditingId(current => current === annotation.id ? undefined : current); onRemove(annotation); }}>×</button>
      </span>;
    })}</div>
    {opened && <article className="agent-composer-annotation-card" role="dialog" aria-label={`${annotationReferenceName(opened, openedIndex)} 详情`}>
      <header><span>{openedFile ? <FileText size={13}/> : <Quote size={13}/>}{annotationReferenceName(opened, openedIndex)}</span><button type="button" aria-label="关闭引用并保存评论" onClick={closeOpened}>×</button></header>
      <small title={openedFile?.path}>{openedFile ? `文件内容 · ${openedFile.filename} · ${openedFile.range}` : '会话文本'}</small>
      {typeof opened.anchor.quote === 'string' && opened.anchor.quote && <blockquote>{opened.anchor.quote}</blockquote>}
      {editingId === opened.id ? <textarea className="agent-composer-annotation-comment" aria-label="编辑注释评论" placeholder="可选：写下你的评论…" value={comment} onChange={event => { setComment(event.target.value); onUpdate(opened, event.target.value); }}/> : <button type="button" className="agent-composer-annotation-comment-display" title="双击编辑评论" onDoubleClick={() => { setComment(opened.comment); setEditingId(opened.id); }}>{opened.comment || '双击添加评论'}</button>}
      <footer><button type="button" onClick={() => { if (editingId === opened.id) onUpdate(opened, comment); onLocate(opened); }}>定位原文</button></footer>
    </article>}
  </section>;
}
interface BoundQueuedMessage extends QueuedMessage {
  bindingId: string;
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
  annotations: AgentConversationAnnotation[];
  providerId: string;
  modelName: string;
  reasoningEffort: string | null;
}
interface ComposerDraftRecovery {
  content: string;
  attachments: AgentAttachment[];
  references: ConversationReference[];
  workspaceReferences: AgentWorkspaceReference[];
  annotations: AgentConversationAnnotation[];
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
  /** A formal parent-turn error arrived before this Task produced a result. */
  parentTerminal?: { eventId: string; at?: string };
}

function parentUserEventId(event: OpenHandsConversationEvent, byId: ReadonlyMap<string, OpenHandsConversationEvent>): string | undefined {
  const visited = new Set<string>();
  let current: OpenHandsConversationEvent | undefined = event;
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    if (current.event_type === 'MESSAGE' && ['user', 'human'].includes(String(current.payload.source ?? '').toLowerCase())) return current.id;
    const parentId: string | null | undefined = current.payload.parent_id;
    current = parentId ? byId.get(parentId) : undefined;
  }
  return undefined;
}

function isTaskSpecificAgentError(event: OpenHandsConversationEvent): boolean {
  if (event.event_type !== 'ERROR') return false;
  const isAgentError = event.payload.event_name === 'AgentErrorEvent'
    || event.payload.source_type === 'AgentErrorEvent';
  return isAgentError && typeof event.payload.tool_call_id === 'string' && event.payload.tool_call_id.length > 0;
}

/**
 * Build a UI-only projection from the formal OpenHands Task lifecycle.
 * The action/observation relationship is always the formal action_id or
 * tool_call_id; event order and text are deliberately never used as a join.
 */
function runtimeTasksFromEvents(events: OpenHandsConversationEvent[], usageSnapshots: RuntimeTaskUsageSnapshot[] = [], controlSnapshots: RuntimeTaskControlSnapshot[] = []): RuntimeTaskProjection[] {
  const avatarSlots = subagentAvatarSlots(events);
  const byEventId = new Map(events.map(event => [event.id, event]));
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
    if (item.status !== 'RUNNING') continue;
    const action = byEventId.get(item.actionEventId);
    if (!action) continue;
    const userEventId = parentUserEventId(action, byEventId);
    if (!userEventId) continue;
    const terminal = events.find(event => event.event_type === 'ERROR'
      && !isTaskSpecificAgentError(event)
      && parentUserEventId(event, byEventId) === userEventId);
    if (terminal) item.parentTerminal = {
      eventId: terminal.id,
      at: typeof terminal.payload.timestamp === 'string' ? terminal.payload.timestamp : undefined,
    };
  }
  return [...tasks.values()].sort((left, right) => (right.startedAt || '').localeCompare(left.startedAt || ''));
}

function runtimeTaskStatus(task: RuntimeTaskProjection, sessionStopped = false): string {
  if (sessionStopped && task.status === 'RUNNING') return '会话已停止，结果未返回';
  if (task.parentTerminal && task.status === 'RUNNING') return '主会话异常结束，结果未返回';
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
  if (task.parentTerminal) return false;
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
    if (task.status !== 'RUNNING' || task.parentTerminal) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [task.parentTerminal, task.status]);
  const definition = definitions.find(item => item.capability_type === 'AGENT_DEFINITION' && item.capability_key === task.subagentType);
  const document = definition?.document && typeof definition.document === 'object' ? definition.document : {};
  const record = document as Record<string, unknown>;
  const tools = definitionStrings(record.tools);
  const skills = definitionStrings(record.skills);
  const outcome = taskOutcomeText(task.outcome);
  const nativeDefinition = !definition;
  const startedAt = task.startedAt ? Date.parse(task.startedAt) : NaN;
  const controlStopsClock = sessionStopped || Boolean(task.parentTerminal) || ['INTERRUPT_CONFIRMED', 'RECOVERED', 'RECOVERY_FAILED', 'WATCHDOG_FAILED', 'INTERRUPT_CONFIRMATION_FAILED'].includes(task.control?.control_state ?? '');
  const finishedAt = task.finishedAt
    ? Date.parse(task.finishedAt)
    : task.parentTerminal?.at
      ? Date.parse(task.parentTerminal.at)
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
      <section><h3>本次任务</h3><dl><dt>状态</dt><dd className={`agent-subagent-status ${runtimeTaskIsActive(task, sessionStopped) ? 'running' : task.status === 'COMPLETED' ? 'completed' : 'error'}`}>{runtimeTaskStatus(task, sessionStopped)}</dd><dt>任务说明</dt><dd>{task.description || 'OpenHands 未提供任务摘要。'}</dd><dt>子智能体类型</dt><dd><code>{task.subagentType}</code></dd><dt>模型请求策略</dt><dd>单次最长 {MODEL_REQUEST_TIMEOUT_SECONDS} 秒；失败最多重试 {MODEL_REQUEST_MAX_RETRIES} 次</dd><dt>子任务墙钟耗时</dt><dd title="从正式 TaskAction 到当前时间或正式 TaskObservation（或所属主会话的正式错误）的经过时间；包含模型调用、重试等待和工具执行。">{elapsedLabel}</dd>{task.startedAt && <><dt>开始时间</dt><dd>{new Date(task.startedAt).toLocaleString('zh-CN')}</dd></>}{task.finishedAt && <><dt>结束时间</dt><dd>{new Date(task.finishedAt).toLocaleString('zh-CN')}</dd></>}{task.parentTerminal?.at && <><dt>主会话异常时间</dt><dd>{new Date(task.parentTerminal.at).toLocaleString('zh-CN')}</dd></>}{task.lastEventType && <><dt>最近事件</dt><dd>{task.lastEventType}{task.lastEventAt ? ` · ${new Date(task.lastEventAt).toLocaleString('zh-CN')}` : ''}</dd></>}{task.lastEventSummary && <><dt>最近事件摘要</dt><dd>{task.lastEventSummary}</dd></>}{task.control && <><dt>平台处理</dt><dd>{task.control.control_state}{task.control.updated_at ? ` · ${new Date(task.control.updated_at).toLocaleString('zh-CN')}` : ''}</dd>{task.control.deadline_at && <><dt>观察截止</dt><dd>{new Date(task.control.deadline_at).toLocaleString('zh-CN')}</dd></>}{task.control.last_error && <><dt>处理错误</dt><dd>{task.control.last_error}</dd></>}</>}</dl><p className="agent-subagent-note">OpenHands 当前只发布 Task 的开始、结果和终态错误；单次模型重试在上游内部完成，未提供正式重试事件，因此这里不会把它猜测成“1/3”。{task.parentTerminal && ' 主会话已正式异常结束，但 OpenHands 未发布这个子任务的结果，因此不会把它伪装成子任务失败。'}</p></section>
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
    {!sessionStopped && tasks.some(task => task.parentTerminal && task.status === 'RUNNING') && <div className="agent-subagent-session-notice" role="status"><CircleDot size={15}/><span><b>主会话已异常结束</b><small>未返回结果的子智能体不再显示为运行中；该状态不表示子任务已完成或自身失败。</small></span></div>}
    <aside className="agent-subagent-task-list"><header><div><span className="eyebrow">SUBAGENTS</span><b>子智能体记录</b></div><span className={running ? 'running' : ''}>{running ? `${running} 个运行中` : `${tasks.length} 个任务`}</span></header><div>{tasks.map(task => <button type="button" key={task.id} className={task.id === selectedTask.id ? 'active' : ''} aria-current={task.id === selectedTask.id ? 'true' : undefined} onClick={() => onSelect(task.id)}><RuntimeTaskGlyph task={task} size={13} sessionStopped={sessionStopped}/><span><b>{task.description || task.subagentType}</b><small>{task.subagentType} · {runtimeTaskStatus(task, sessionStopped)}</small></span><ChevronRight size={14}/></button>)}</div></aside>
    <RuntimeTaskRecord task={selectedTask} definitions={definitions} sessionStopped={sessionStopped}/>
  </section>;
}

interface WorkspaceConversationGroupProps {
  groupId: string;
  label: string;
  children: (visibleCount: number) => ReactNode;
  conversationCount: number;
  forceExpanded?: boolean;
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

function conversationHasReachedTerminalState(executionStatus: string | null | undefined): boolean {
  // OpenHands writes a formal ConversationErrorEvent before ending a failed
  // run as `error`. Since that native state is ready for a later explicit
  // message, it must clear any earlier local sending transition too.
  return ['idle', 'completed', 'stopped', 'finished', 'error', 'stuck'].includes(
    executionStatus?.trim().toLowerCase() ?? '',
  );
}

function unreadConversationStorageKey(hostId: string, workspaceId: string): string {
  return `flowweave:agent-workspace-unread:${hostId}:${workspaceId}`;
}

function pinnedConversationStorageKey(hostId: string, workspaceId: string): string {
  return `flowweave:agent-workspace-pinned:${hostId}:${workspaceId}`;
}

function readPinnedConversationIds(storageKey: string | undefined): Set<string> {
  if (!storageKey) return new Set();
  try {
    const stored = window.localStorage.getItem(storageKey);
    const values: unknown = stored ? JSON.parse(stored) : [];
    return new Set(Array.isArray(values) ? values.filter((value): value is string => typeof value === 'string') : []);
  } catch {
    return new Set();
  }
}

function writePinnedConversationIds(storageKey: string | undefined, conversationIds: Set<string>) {
  if (!storageKey) return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify([...conversationIds]));
  } catch {
    // Pinning is browser-local presentation state.
  }
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
// A readiness terminal state can precede its formal OpenHands event page. Keep
// the reconciliation window short so a missing event cannot permanently lock
// the composer.
const TERMINAL_EVENT_RECONCILIATION_MS = 8_000;

const AgentSessionGatewayContext = createContext<AgentSessionGateway>(agentWorkspaceSessionGateway);
const AgentSessionHostContext = createContext<AgentSessionHost>(agentWorkspaceSessionHost);

function useAgentSessionGateway(): AgentSessionGateway {
  return useContext(AgentSessionGatewayContext);
}

function useAgentSessionHost(): AgentSessionHost {
  return useContext(AgentSessionHostContext);
}

function WorkspaceConversationGroup({ groupId, label, children, conversationCount, forceExpanded = false, canCreateConversation = false, onCreateConversation, onDelete }: WorkspaceConversationGroupProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [visibleCount, setVisibleCount] = useState(3);
  const contentId = `agent-workspace-group-${groupId}`;
  const canLoadMore = visibleCount < conversationCount;
  useEffect(() => {
    if (!forceExpanded) return;
    setCollapsed(false);
    setVisibleCount(conversationCount);
  }, [conversationCount, forceExpanded]);

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

function conversationSearchSnippet(content: string, query: string): string {
  const text = content.replace(/\s+/g, ' ').trim();
  const index = text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  if (index < 0) return text.slice(0, 180);
  const start = Math.max(0, index - 56);
  const end = Math.min(text.length, index + query.length + 124);
  return (start ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '');
}

function ConversationSearchDialog({ search, onClose, onSubmit, submitting, onOpenHit }: {
  search?: AgentConversationSearch;
  onClose: () => void;
  onSubmit: (query: string) => void;
  submitting: boolean;
  onOpenHit: (bindingId: string, eventId: string) => void;
}) {
  const [query, setQuery] = useState(search?.query ?? '');
  useEscapeClose(onClose);
  useEffect(() => { setQuery(search?.query ?? ''); }, [search?.id, search?.query]);
  const state = search?.state;
  const running = state === 'PENDING' || state === 'RUNNING';
  return <div className="agent-conversation-search-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="agent-conversation-search-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-conversation-search-title">
      <form onSubmit={event => { event.preventDefault(); if (query.trim() && !submitting) onSubmit(query.trim()); }}>
        <Search size={21}/><input autoFocus aria-label="搜索会话内容" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索全部工作区中的会话…"/><kbd>↵</kbd>
        <button type="button" aria-label="关闭搜索" onClick={onClose}><X size={17}/></button>
      </form>
      <header><div><span className="eyebrow">CONVERSATION SEARCH</span><h2 id="agent-conversation-search-title">{search ? '“' + search.query + '”' : '搜索会话'}</h2></div>{running && <span className="agent-conversation-search-state running"><LoaderCircle size={13}/>后台搜索中</span>}{state === 'SUCCEEDED' && <span className="agent-conversation-search-state done"><Check size={13}/>已完成</span>}{state === 'FAILED' && <span className="agent-conversation-search-state failed">搜索失败</span>}</header>
      <div className="agent-conversation-search-results">
        {!search && <p>输入关键词并按回车。关闭窗口不会取消后台搜索。</p>}
        {running && <p>正在逐个搜索所有工作区会话的原生消息记录。你可以关闭窗口，完成后从左上角按钮重新打开结果。</p>}
        {state === 'FAILED' && <p>{search?.failure_summary || '搜索无法完成，请重新搜索。'}</p>}
        {state === 'SUCCEEDED' && !search?.hits?.length && <p>没有找到包含该内容的会话消息。</p>}
        {search?.hits?.map(hit => <button type="button" className="agent-conversation-search-hit" key={hit.binding_id + ':' + hit.event_id} onClick={() => onOpenHit(hit.binding_id, hit.event_id)}>
          <span><b>{hit.title}</b><small>{hit.source === 'user' || hit.source === 'human' ? '你的消息' : 'Agent 回复'}{hit.timestamp ? ' · ' + new Date(hit.timestamp).toLocaleString() : ''}</small></span><p>{conversationSearchSnippet(hit.content, search.query)}</p><ChevronRight size={16}/>
        </button>)}
      </div>
    </section>
  </div>;
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
  item, selectedBindingId, running, unread, pinned, conversationWritable, removing, deleteDisabled, dragging, dropPosition, orderSyncState, onPointerDragStart, onRetryOrder, onSelect, onTogglePin, onMarkUnread, onDelete, reveal,
}: {
  item: AgentConversation;
  selectedBindingId?: string;
  running: boolean;
  unread: boolean;
  pinned: boolean;
  conversationWritable: boolean;
  removing: boolean;
  deleteDisabled: boolean;
  dragging?: boolean;
  dropPosition?: 'before' | 'after';
  orderSyncState?: 'syncing' | 'failed';
  onPointerDragStart?: (event: React.PointerEvent<HTMLButtonElement>) => void;
  onRetryOrder?: () => void;
  onSelect: () => void;
  onTogglePin: () => void;
  onMarkUnread: () => void;
  onDelete?: () => void;
  reveal?: boolean;
}) {
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number }>();
  useEscapeClose(() => setContextMenu(undefined), Boolean(contextMenu));
  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(undefined);
    window.addEventListener('pointerdown', close);
    window.addEventListener('resize', close);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('resize', close); };
  }, [contextMenu]);
  return <div data-conversation-binding-id={item.id} className={`agent-workspace-conversation${dragging ? ' dragging' : ''}${dropPosition ? ` drop-${dropPosition}` : ''}${orderSyncState ? ` order-sync-${orderSyncState}` : ''}${reveal ? ' sidebar-reveal' : ''}`} onContextMenu={event => {
    event.preventDefault();
    setContextMenu({ x: Math.min(event.clientX, window.innerWidth - 180), y: Math.min(event.clientY, window.innerHeight - 52) });
  }}>
    {onPointerDragStart && <button type="button" className="agent-workspace-conversation-drag" aria-label={`拖拽排序会话 ${conversationName(item)}`} title="拖拽调整当前工作区内的顺序" onClick={event => event.stopPropagation()} onPointerDown={onPointerDragStart}><GripVertical size={13}/></button>}
    <button type="button" className={`agent-workspace-conversation-select${item.id === selectedBindingId ? ' active' : ''}`} onClick={onSelect}>
      <CircleDot size={13}/><span><b>{conversationName(item)}</b></span>
    </button>
    {running && <LoaderCircle className="agent-workspace-conversation-running" role="img" aria-label="会话正在运行" size={14}/>}
    {!running && unread && <span className="agent-workspace-conversation-unread" role="img" aria-label="会话已完成，有未读回复" title="会话已完成，有未读回复"/>}
    {orderSyncState === 'syncing' && <span className="agent-workspace-conversation-sync" title="排序正在后台同步" aria-label="排序正在后台同步"><LoaderCircle size={12}/></span>}
    {orderSyncState === 'failed' && <button type="button" className="agent-workspace-conversation-sync failed" title="排序暂未同步；点击重试。当前前端顺序已保留。" aria-label="排序暂未同步，点击重试" onClick={event => { event.stopPropagation(); onRetryOrder?.(); }}>!</button>}
    {onDelete && !running && <button type="button" className="agent-workspace-conversation-delete" aria-label={`删除会话 ${conversationName(item)}`} title={deleteDisabled ? '会话运行中，请先停止' : '删除会话'} disabled={!conversationWritable || deleteDisabled || removing} onClick={onDelete}><Trash2 size={13}/></button>}
    {contextMenu && createPortal(<div className="agent-conversation-context-menu" role="menu" aria-label={`会话操作菜单：${conversationName(item)}`} style={{ left: contextMenu.x, top: contextMenu.y }} onPointerDown={event => event.stopPropagation()} onContextMenu={event => event.preventDefault()}>
      {pinned
        ? <button type="button" role="menuitem" onClick={() => { onTogglePin(); setContextMenu(undefined); }}><PinOff size={13}/>取消置顶</button>
        : <><button type="button" role="menuitem" onClick={() => { onTogglePin(); setContextMenu(undefined); }}><Pin size={13}/>置顶</button><button type="button" role="menuitem" onClick={() => { onMarkUnread(); setContextMenu(undefined); }}><CircleDot size={13}/>标记为未读</button></>}
    </div>, document.body)}
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

const MAX_BROWSER_QUEUED_MESSAGES = 20;
const MAX_BROWSER_QUEUED_MESSAGE_CHARS = 100_000;

function queuedMessageFromStorage(value: unknown): QueuedMessage | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const candidate = value as Partial<QueuedMessage>;
  if (typeof candidate.id !== 'string' || !candidate.id
    || typeof candidate.scope !== 'string' || !candidate.scope
    || typeof candidate.content !== 'string' || candidate.content.length > MAX_BROWSER_QUEUED_MESSAGE_CHARS
    || !Array.isArray(candidate.items) || !Array.isArray(candidate.references)) return undefined;
  if (candidate.items.some(item => !item || typeof item.filename !== 'string'
    || typeof item.mime_type !== 'string' || typeof item.byte_size !== 'number'
    || typeof item.path !== 'string')) return undefined;
  const references = candidate.references.filter((item): item is ConversationReference => Boolean(item)
    && typeof item.eventId === 'string' && typeof item.content === 'string').slice(0, 20);
  const deliveryState: QueueDeliveryState = ['queued', 'dispatching', 'ambiguous', 'rejected'].includes(candidate.deliveryState ?? '')
    ? candidate.deliveryState as QueueDeliveryState
    : 'queued';
  return {
    id: candidate.id,
    scope: candidate.scope,
    content: candidate.content,
    // Image previews may be large and should not be copied into browser
    // recovery state. The server-authorized attachment metadata is enough to
    // reconstruct the later formal OpenHands event.
    items: candidate.items.slice(0, 20).map(item => ({ filename: item.filename, mime_type: item.mime_type, byte_size: item.byte_size, path: item.path })),
    references,
    workspaceReferences: workspaceReferencesFromStorage(candidate.workspaceReferences),
    annotations: annotationsFromStorage(candidate.annotations),
    deliveryState,
    nativeGuidance: candidate.nativeGuidance === true,
    createdAt: typeof candidate.createdAt === 'number' ? candidate.createdAt : Date.now(),
    deliveryError: typeof candidate.deliveryError === 'string' ? candidate.deliveryError.slice(0, 1000) : undefined,
  };
}

function readQueuedMessages(storageKey: string | undefined): QueuedMessage[] {
  if (!storageKey) return [];
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    const value: unknown = stored ? JSON.parse(stored) : [];
    if (!Array.isArray(value)) return [];
    const ids = new Set<string>();
    return value.flatMap(item => {
      const message = queuedMessageFromStorage(item);
      if (!message || ids.has(message.id)) return [];
      ids.add(message.id);
      return [message];
    }).slice(0, MAX_BROWSER_QUEUED_MESSAGES);
  } catch {
    return [];
  }
}

function writeQueuedMessages(storageKey: string | undefined, messages: QueuedMessage[]) {
  if (!storageKey) return;
  try {
    if (messages.length) window.sessionStorage.setItem(storageKey, JSON.stringify(messages));
    else window.sessionStorage.removeItem(storageKey);
  } catch {
    // This is a browser-only recovery aid. A storage quota failure must not
    // block an otherwise valid explicit user submission.
  }
}

function isAmbiguousDelivery(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.code === 'AGENT_MESSAGE_DELIVERY_AMBIGUOUS'
    || error.code === 'NETWORK_ERROR'
    || error.status === 0
    || error.status >= 500;
}

function annotationsFromStorage(value: unknown): AgentConversationAnnotation[] {
  if (!Array.isArray(value)) return [];
  const ids = new Set<string>();
  return value.filter((item): item is AgentConversationAnnotation => {
    if (!item || typeof item !== 'object') return false;
    const candidate = item as Partial<AgentConversationAnnotation>;
    if (typeof candidate.id !== 'string' || !candidate.id
      || (candidate.anchor_kind !== 'CONVERSATION_TEXT' && candidate.anchor_kind !== 'WORKSPACE_FILE_RANGE')
      || !candidate.anchor || typeof candidate.anchor !== 'object'
      || typeof candidate.comment !== 'string' || ids.has(candidate.id)) return false;
    ids.add(candidate.id);
    return true;
  }).slice(0, 20);
}

function readConversationDraft(storageKey: string): ConversationDraftRecovery | undefined {
  try {
    const stored = window.localStorage.getItem(storageKey) ?? window.sessionStorage.getItem(storageKey);
    if (!stored) return undefined;
    const value = JSON.parse(stored) as Partial<ConversationDraftRecovery>;
    if (!value.draft?.id || typeof value.draft.displayName !== 'string'
      || typeof value.content !== 'string' || !Array.isArray(value.attachments)
      || typeof value.providerId !== 'string' || typeof value.modelName !== 'string'
      || (value.reasoningEffort !== null && typeof value.reasoningEffort !== 'string')) return undefined;
    if (value.content.length > 200_000 || value.attachments.some(item => !item || typeof item.filename !== 'string'
      || typeof item.mime_type !== 'string' || typeof item.byte_size !== 'number'
      || typeof item.path !== 'string')) return undefined;
    const references = Array.isArray(value.references)
      ? value.references.filter((item): item is ConversationReference => Boolean(item)
        && typeof item.eventId === 'string' && typeof item.content === 'string')
      : [];
    return {
      ...value,
      attachments: value.attachments.slice(0, 20).map(item => ({
        filename: item.filename, mime_type: item.mime_type, byte_size: item.byte_size, path: item.path,
      })),
      references,
      workspaceReferences: workspaceReferencesFromStorage(value.workspaceReferences),
      annotations: annotationsFromStorage(value.annotations),
    } as ConversationDraftRecovery;
  } catch {
    return undefined;
  }
}

function writeConversationDraft(storageKey: string, recovery: ConversationDraftRecovery | undefined) {
  try {
    if (recovery) window.localStorage.setItem(storageKey, JSON.stringify(recovery));
    else window.localStorage.removeItem(storageKey);
    // v1 was current-tab-only. Remove it after the v2 durable write succeeds.
    window.sessionStorage.removeItem(storageKey);
  } catch {
    // This recovery aid deliberately remains browser-only. A first message is
    // still the only action that creates a server-side Conversation.
  }
}

function conversationComposerDraftStorageKey(hostId: string, workspaceId: string, bindingId: string): string {
  return `flowweave:agent-conversation-composer-draft:v2:${hostId}:${workspaceId}:${bindingId}`;
}

function clearConversationComposerDraft(hostId: string, workspaceId: string, bindingId: string): void {
  try {
    window.localStorage.removeItem(conversationComposerDraftStorageKey(hostId, workspaceId, bindingId));
  } catch {
    // Browser storage is only a recovery aid; cleanup must not block sending.
  }
}

function conversationDraftStorageKey(hostId: string, workspaceId: string, workDirectoryId?: string): string {
  return `flowweave:agent-conversation-draft:v2:${hostId}:${workspaceId}:${workDirectoryId ?? 'root'}`;
}

function readConversationComposerDraft(storageKey: string): ComposerDraftRecovery {
  try {
    const stored = window.localStorage.getItem(storageKey);
    if (stored) {
      const value = JSON.parse(stored) as Partial<ComposerDraftRecovery>;
      if (typeof value.content === 'string' && value.content.length <= 200_000
        && Array.isArray(value.attachments) && Array.isArray(value.references)) {
        return {
          content: value.content,
          attachments: value.attachments.filter((item): item is AgentAttachment => Boolean(item)
            && typeof item.filename === 'string' && typeof item.mime_type === 'string'
            && typeof item.byte_size === 'number' && typeof item.path === 'string').slice(0, 20).map(item => ({
              filename: item.filename, mime_type: item.mime_type, byte_size: item.byte_size, path: item.path,
            })),
          references: value.references.filter((item): item is ConversationReference => Boolean(item)
            && typeof item.eventId === 'string' && typeof item.content === 'string').slice(0, 20),
          workspaceReferences: workspaceReferencesFromStorage(value.workspaceReferences),
          annotations: annotationsFromStorage(value.annotations),
        };
      }
    }
    // Preserve text-only drafts created by the previous current-tab store.
    const legacy = window.sessionStorage.getItem(storageKey.replace(':v2:', ':v1:'));
    return { content: legacy && legacy.length <= 200_000 ? legacy : '', attachments: [], references: [], workspaceReferences: [], annotations: [] };
  } catch {
    return { content: '', attachments: [], references: [], workspaceReferences: [], annotations: [] };
  }
}

function writeConversationComposerDraft(storageKey: string | undefined, value: ComposerDraftRecovery | undefined) {
  if (!storageKey) return;
  try {
    if (value && (value.content || value.attachments.length || value.references.length || value.workspaceReferences.length || value.annotations.length)) {
      window.localStorage.setItem(storageKey, JSON.stringify({
        ...value,
        attachments: value.attachments.slice(0, 20).map(item => ({ filename: item.filename, mime_type: item.mime_type, byte_size: item.byte_size, path: item.path })),
      }));
    } else window.localStorage.removeItem(storageKey);
  } catch {
    // Composer recovery is opportunistic and must never block sending.
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
type ComposerSuggestionKind = 'SKILL' | 'COMMAND' | 'MCP' | 'REFERENCE';
interface ComposerSuggestion {
  id: string;
  kind: ComposerSuggestionKind;
  token: string;
  label: string;
  detail: string;
  available?: boolean;
}

function composerTrigger(value: string): { sigil: '$' | '/' | '@'; query: string; start: number } | undefined {
  const match = /(?:^|\s)([$/@])([^\s]*)$/.exec(value);
  if (!match) return undefined;
  return { sigil: match[1] as '$' | '/' | '@', query: match[2], start: value.length - match[0].length + (match[0].startsWith(' ') ? 1 : 0) };
}

function stringValues(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : [];
}

interface ComposerHandle {
  replace: (value: string) => void;
  value: () => string;
  focus: () => void;
}

const COMPOSER_DRAFT_PERSIST_DELAY_MS = 400;
const EMPTY_TASK_CONTROL: RuntimeTaskControlSnapshot[] = [];

const ComposerCapabilityAutocomplete = forwardRef<ComposerHandle, {
  initialDraft: string; scope?: string; suggestions: ComposerSuggestion[]; disabled: boolean; placeholder: string;
  onDraftChange: (value: string) => void; onContentPresenceChange: (hasContent: boolean) => void; onDraftPersist: (scope: string | undefined, value: string) => void; onPaste: (event: ReactClipboardEvent<HTMLTextAreaElement>) => void; onDropFiles?: (files: File[]) => void; onDropWorkspaceFiles?: (paths: string[]) => void; onSubmit: (content: string) => void;
  onDirectSubmit?: (content: string) => void;
  onManageCapabilities?: () => void;
  onWorkspaceReferenceSelected?: () => void;
}>(function ComposerCapabilityAutocomplete({
  initialDraft, scope, suggestions, disabled, placeholder, onDraftChange, onContentPresenceChange, onDraftPersist, onPaste, onDropFiles, onDropWorkspaceFiles, onSubmit, onDirectSubmit, onManageCapabilities, onWorkspaceReferenceSelected,
}, ref) {
  const input = useRef<HTMLTextAreaElement>(null);
  const dragDepth = useRef(0);
  const draftRef = useRef(initialDraft);
  const previousScope = useRef(scope);
  const persistDraftRef = useRef(onDraftPersist);
  const [draft, setDraft] = useState(initialDraft);
  const [activeIndex, setActiveIndex] = useState(0);
  const [fileDragActive, setFileDragActive] = useState(false);
  const [inputOverflowing, setInputOverflowing] = useState(false);
  const updateDraft = useCallback((value: string) => {
    draftRef.current = value;
    setDraft(current => current === value ? current : value);
    onDraftChange(value);
  }, [onDraftChange]);
  useImperativeHandle(ref, () => ({
    replace: updateDraft,
    value: () => draftRef.current,
    focus: () => input.current?.focus(),
  }), [updateDraft]);
  useLayoutEffect(() => {
    if (previousScope.current === scope) return;
    previousScope.current = scope;
    updateDraft(initialDraft);
  }, [initialDraft, scope, updateDraft]);
  const hasText = Boolean(draft.trim());
  useEffect(() => { onContentPresenceChange(hasText); }, [hasText, onContentPresenceChange]);
  useEffect(() => {
    persistDraftRef.current = onDraftPersist;
  }, [onDraftPersist]);
  useLayoutEffect(() => {
    const persist = persistDraftRef.current;
    const scopeForCleanup = scope;
    return () => persist(scopeForCleanup, draftRef.current);
  }, [scope]);
  useEffect(() => {
    const scopeForPersist = scope;
    const timer = window.setTimeout(() => persistDraftRef.current(scopeForPersist, draftRef.current), COMPOSER_DRAFT_PERSIST_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [draft, scope]);
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
    if (item.kind === 'REFERENCE') {
      updateDraft(`${draft.slice(0, trigger.start)}${draft.slice(trigger.start + trigger.query.length + 1)}`);
      onWorkspaceReferenceSelected?.();
      return;
    }
    updateDraft(`${draft.slice(0, trigger.start)}${item.token} ${draft.slice(trigger.start + trigger.query.length + 1)}`);
    requestAnimationFrame(() => input.current?.focus());
  };
  const hasSuggestions = visible.length > 0;
  // A slash is an explicit request for a command or MCP.  Keep the picker
  // available for a draft before it has a native Conversation binding too.
  const showCapabilityManager = Boolean(trigger && trigger.sigil !== '@' && onManageCapabilities);
  const hasMenu = Boolean(trigger && (hasSuggestions || showCapabilityManager));
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
    <textarea ref={input} data-overflowing={inputOverflowing || undefined} aria-label="发送 Agent 消息" aria-autocomplete="list" aria-controls={hasMenu ? 'agent-composer-capabilities' : undefined} aria-expanded={hasMenu} value={draft} maxLength={200_000} placeholder={placeholder} disabled={disabled} onChange={event => updateDraft(event.target.value)} onPaste={onPaste} onKeyDown={event => {
      if (isImeComposition(event)) return;
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.shiftKey) {
        event.preventDefault();
        onDirectSubmit?.(draft);
        return;
      }
      if (hasMenu && event.key === 'Escape') { updateDraft(draft.slice(0, -trigger!.query.length - 1)); return; }
      if (hasSuggestions && ['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key)) {
        event.preventDefault();
        if (event.key === 'ArrowDown') { setActiveIndex(index => (index + 1) % visible.length); return; }
        if (event.key === 'ArrowUp') { setActiveIndex(index => (index - 1 + visible.length) % visible.length); return; }
        select(visible[activeIndex] ?? visible[0]);
        return;
      }
      if (hasMenu && ['Enter', 'Tab'].includes(event.key)) { event.preventDefault(); return; }
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); onSubmit(draft); }
    }}/>
    {fileDragActive && <div className="agent-composer-file-drop" aria-live="polite">松开以添加附件</div>}
    {hasMenu && <div id="agent-composer-capabilities" className="agent-composer-capability-menu" role="listbox" aria-label={trigger!.sigil === '$' ? '选择技能' : trigger!.sigil === '@' ? '选择引用类型' : '选择命令或 MCP'}>{hasSuggestions ? <>{visible.map((item, index) => <div className="agent-composer-capability-option" key={item.id}>{trigger!.sigil === '@' && index === 0 && <div className="agent-composer-capability-section">引用类型</div>}{trigger!.sigil === '/' && index === 0 && <div className="agent-composer-capability-section">MCP 与命令</div>}<button type="button" role="option" aria-selected={index === activeIndex} aria-disabled={item.available === false || undefined} disabled={item.available === false} className={`${index === activeIndex ? 'active' : ''}${item.available === false ? ' unavailable' : ''}`} onMouseDown={event => event.preventDefault()} onMouseEnter={() => setActiveIndex(index)} onClick={() => select(item)}><code>{item.token}</code><span><b>{item.label}</b><small>{item.detail}</small></span><em>{item.kind === 'SKILL' ? '技能' : item.kind === 'COMMAND' ? '命令' : item.kind === 'REFERENCE' ? '引用' : 'MCP'}</em></button></div>)}</> : <div className="agent-composer-capability-empty"><span><b>{trigger!.sigil === '@' ? '当前没有匹配的引用类型' : !suggestions.length ? trigger!.sigil === '$' ? '当前会话还没有加载 Skill' : '当前会话还没有加载命令或 MCP' : '当前会话没有匹配的能力'}</b><small>{trigger!.sigil === '@' ? '调整输入关键词以筛选引用类型。' : !suggestions.length ? `先为此会话加载能力，随后可在这里用 ${trigger!.sigil} 选择并插入。` : '调整输入关键词，或管理当前会话能力。'}</small></span></div>}{showCapabilityManager && <div className="agent-composer-capability-manage"><span>管理当前会话能力</span><button type="button" onMouseDown={event => event.preventDefault()} onClick={onManageCapabilities}>管理</button></div>}</div>}
  </div>;
});

function CapabilityManager({ workspaceId, bindingId, conversationCapabilities, draftCapabilityIds, onClose, onCreateEnhancedConversation }: {
  workspaceId: string; bindingId?: string; conversationCapabilities?: AgentSessionCapability[]; onClose: () => void;
  draftCapabilityIds?: string[]; onCreateEnhancedConversation?: (capabilityVersionIds: string[]) => void;
}) {
  const { api } = useAgentSessionGateway();
  const host = useAgentSessionHost();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<AgentCapabilityType | 'ALL'>('ALL');
  const [managerTab, setManagerTab] = useState<'CAPABILITIES' | 'CREDENTIALS'>('CAPABILITIES');
  const [collectionMenuOpen, setCollectionMenuOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectedCredentialIds, setSelectedCredentialIds] = useState<string[]>([]);
  const [mcpReadiness, setMcpReadiness] = useState<Record<string, AgentSessionMcpReadiness | undefined>>({});
  const [checkingMcpIds, setCheckingMcpIds] = useState<Set<string>>(new Set());
  const dialog = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const collectionMenu = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const credentialSelectionInitialized = useRef(false);
  useEffect(() => {
    const opener = returnFocus.current;
    closeButton.current?.focus();
    return () => { opener?.focus(); };
  }, []);
  const catalogQuery = useQuery({ queryKey: sessionQueryKey(host, 'capability-catalog'), queryFn: api.capabilities });
  const collectionsQuery = useQuery({ queryKey: sessionQueryKey(host, 'capability-collections'), queryFn: api.capabilityCollections });
  const credentialQuery = useQuery({
    queryKey: sessionQueryKey(host, 'credential-sync', workspaceId, bindingId),
    queryFn: () => api.credentialSync(workspaceId, bindingId!),
    enabled: Boolean(bindingId && managerTab === 'CREDENTIALS'),
  });
  useEffect(() => {
    const current = bindingId ? conversationCapabilities : draftCapabilityIds?.map(id => ({ id }));
    if (current) setSelectedIds(current.map(item => item.id));
  }, [bindingId, conversationCapabilities, draftCapabilityIds]);
  useEffect(() => {
    if (!credentialQuery.data || credentialSelectionInitialized.current) return;
    credentialSelectionInitialized.current = true;
    setSelectedCredentialIds(credentialQuery.data.credentials.map(item => item.id));
  }, [credentialQuery.data]);
  useEffect(() => {
    if (!collectionMenuOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !collectionMenu.current?.contains(event.target)) {
        setCollectionMenuOpen(false);
      }
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer, true);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer, true);
  }, [collectionMenuOpen]);
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
  const credentialSync = useMutation({
    mutationFn: () => api.synchronizeCredentials(workspaceId, bindingId!, selectedCredentialIds),
    onSuccess: value => {
      queryClient.setQueryData(sessionQueryKey(host, 'credential-sync', workspaceId, bindingId), value);
    },
  });
  useEscapeClose(() => { if (!save.isPending && !credentialSync.isPending) onClose(); });
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
  const busy = save.isPending || credentialSync.isPending;
  const toggleCredential = (credential: AgentConversationCredentialSyncEntry) => setSelectedCredentialIds(current => current.includes(credential.id) ? current.filter(id => id !== credential.id) : [...current, credential.id]);
  return <div className="agent-capability-backdrop" role="presentation" onPointerDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section ref={dialog} className={`agent-capability-manager${bindingId ? ' has-session-tabs' : ''}`} role="dialog" aria-modal="true" aria-labelledby="agent-capability-title" onKeyDown={trapFocus}>
      <header><h2 id="agent-capability-title">会话配置</h2><button ref={closeButton} type="button" aria-label="关闭会话配置" disabled={busy} onClick={onClose}><X size={18}/></button></header>
      {bindingId && <div className="agent-session-config-tabs"><button type="button" className={managerTab === 'CAPABILITIES' ? 'active' : ''} onClick={() => setManagerTab('CAPABILITIES')}>能力</button><button type="button" className={managerTab === 'CREDENTIALS' ? 'active' : ''} onClick={() => setManagerTab('CREDENTIALS')}>认证</button></div>}
      {managerTab === 'CREDENTIALS' ? <><div className="agent-capability-summary">{!credentialQuery.data?.initialized_at && <span>此会话未记录此前的认证同步状态</span>}<span>同步会追加新变量，并覆盖同名变量的当前值</span></div><div className="agent-capability-error"><p>取消选择、删除认证或切换认证类型不会从已存在会话撤销变量；请新建会话以轮换认证</p></div><div className="agent-capability-list">{credentialQuery.isLoading ? <p>正在读取认证信息…</p> : credentialQuery.error ? <p>{credentialQuery.error instanceof Error ? credentialQuery.error.message : '认证信息暂不可用。'}</p> : credentialQuery.data?.credentials.length ? credentialQuery.data.credentials.map(credential => { const checked = selectedCredentialIds.includes(credential.id); const state = credential.sync_state === 'CURRENT' ? '已同步' : credential.sync_state === 'NEEDS_SYNC' ? '需要同步' : '未记录'; return <button type="button" key={credential.id} className={checked ? 'selected' : ''} onClick={() => toggleCredential(credential)}><span className="agent-capability-icon plugin"><ShieldAlert size={17}/></span><span><b>{credential.name}</b><small>{credential.target_host}{credential.target_path} · {credential.auth_type === 'TOKEN' ? 'Token' : '用户名密码'}</small><em>{state}</em></span><i aria-hidden="true">{checked ? <Check size={15}/> : null}</i></button>; }) : <p>当前没有可同步的认证信息。</p>}</div>{credentialSync.error && <div className="agent-capability-error"><p>{credentialSync.error.message}</p></div>}<footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>关闭</button><button type="button" className="primary" disabled={busy || credentialQuery.isLoading} onClick={() => credentialSync.mutate()}><RefreshCw size={14}/>{credentialSync.isPending ? '正在同步…' : '同步认证'}</button></footer></> : <>
      <div className="agent-capability-toolbar"><div className="agent-capability-tabs">{([['ALL', 'all'], ['PLUGIN', 'plugin'], ['MCP', 'MCP'], ['SKILL', 'skill'], ['CONTEXT', 'context'], ['AGENT_DEFINITION', 'agent'], ['HOOK', 'hook']] as const).map(([value, label]) => <button type="button" key={value} className={kind === value ? 'active' : ''} onClick={() => { setKind(value); setCollectionMenuOpen(false); }}>{label}</button>)}</div><div className="agent-capability-toolbar-actions">{(kind === 'ALL' || kind === 'SKILL') && collectionsQuery.data?.length ? <div ref={collectionMenu} className="agent-capability-collection-menu"><button type="button" className="agent-capability-collection-trigger" aria-label="Skill 组合" title="Skill 组合" aria-expanded={collectionMenuOpen} onClick={() => setCollectionMenuOpen(current => !current)}><Layers3 size={15}/></button>{collectionMenuOpen && <div className="agent-capability-collection-popover" role="menu" aria-label="Skill 组合">{collectionsQuery.data.map(collection => { const memberIds = collection.members.map(member => member.id).filter(id => byId.has(id)); const selected = memberIds.length > 0 && memberIds.every(id => selectedIds.includes(id)); return <button type="button" role="menuitemcheckbox" aria-checked={selected} key={collection.id} className={selected ? 'selected' : ''} onClick={() => toggleCollection(collection)}><span><b>{collection.name}</b><small>{memberIds.length} 项能力</small></span>{selected && <Check size={14}/>}</button>; })}</div>}</div> : null}{!readonlyCreationOnly && <button type="button" className="agent-capability-select-visible" disabled={!selectableVisibleIds.length} onClick={toggleVisible}>{allVisibleSelected ? '取消选择筛选结果' : `选择筛选结果 (${selectableVisibleIds.length})`}</button>}<label className="agent-capability-search"><Search size={15}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、说明或文件…"/></label></div></div>
      <div className="agent-capability-summary"><span>{readonlyCreationOnly ? <>已装配 <b>{kind === 'CONTEXT' ? frozenContextCount : kind === 'HOOK' ? frozenHookCount : frozenAgentCount}</b> 个 {kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'}</> : <>{bindingId ? '已注册' : '已选择'} <b>{selectedIds.length}</b> 项</>}</span><span>{readonlyCreationOnly ? `${kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'} 在创建会话时冻结，仅供查看，不能新增、编辑、取消或删除。` : bindingId ? '可继续注册新能力；已注册能力已锁定，不能取消或删除。' : '选择只作用于本次新会话，不会改变工作区或其他会话。'}</span></div>
      <div className="agent-capability-list">{catalogQuery.isLoading ? <p>正在读取能力仓库…</p> : visible.length === 0 ? <p>{readonlyCreationOnly ? `此会话创建时没有装配 ${kind === 'CONTEXT' ? 'Context' : kind === 'HOOK' ? 'Hook' : 'Agent'}。` : '没有匹配的已发布能力。'}</p> : visible.map(item => { const checked = selectedIds.includes(item.id); const isFrozenCreationOnly = Boolean(bindingId && ['CONTEXT', 'AGENT_DEFINITION', 'HOOK'].includes(item.capability_type) && frozenCreationOnlyIds.has(item.id)); const locked = Boolean(bindingId && (conversationCapabilities ?? []).some(enabled => enabled.id === item.id)); const isMcp = item.capability_type === 'MCP'; const readiness = mcpReadiness[item.id]; const checking = isMcp && checkingMcpIds.has(item.id); const readinessLabel = item.capability_type === 'CONTEXT' ? '系统上下文' : item.capability_type === 'AGENT_DEFINITION' ? 'Agent' : item.capability_type === 'HOOK' ? 'Hook' : !isMcp ? (item.capability_type === 'SKILL' ? '技能' : item.capability_type) : !checked ? 'MCP' : checking ? '检测中' : readiness?.state === 'READY' ? '已连接' : readiness?.error_kind === 'timeout' ? '连接超时' : readiness?.error_kind === 'connection' ? '连接失败' : '不可用'; const detail = isFrozenCreationOnly ? '创建会话时已装配，仅供查看，不能编辑或删除。' : locked ? '已注册到当前会话，不能取消或删除。' : item.capability_type === 'CONTEXT' ? '创建会话时冻结，并追加到 OpenHands 系统提示词后缀。' : item.capability_type === 'AGENT_DEFINITION' ? '创建会话时冻结为 OpenHands 原生子 Agent 定义。' : item.capability_type === 'HOOK' ? '创建会话时冻结，并通过 OpenHands 官方 hook_config 注册。' : isMcp && checked && readiness?.state === 'UNAVAILABLE' ? `MCP ${readinessLabel}；不会保存为新会话默认能力。` : item.description || item.filename; const lockedLabel = isFrozenCreationOnly ? `${item.capability_key}（创建时已装配，只读）` : `${item.capability_key}（已注册，不能取消）`; const lockedTitle = isFrozenCreationOnly ? '该能力在创建会话时已装配，仅供查看，不能新增、编辑或删除。' : '该能力已注册到当前会话，不能取消或删除。'; return <button type="button" key={item.id} className={`${checked ? 'selected' : ''}${locked ? ' locked' : ''}`} disabled={locked} aria-label={locked ? lockedLabel : undefined} title={locked ? lockedTitle : undefined} onClick={() => toggle(item)}><span className={`agent-capability-icon ${item.capability_type.toLowerCase()}`}><Boxes size={17}/></span><span><b>{item.capability_key}</b><small>{detail}</small><em className={isMcp && checked ? `mcp-status ${readiness?.state === 'READY' ? 'ready' : readiness?.state === 'UNAVAILABLE' ? 'unavailable' : 'checking'}` : undefined}>{readinessLabel}</em></span><i aria-hidden="true">{checked ? <Check size={15}/> : null}</i></button>; })}</div>
      {save.error && <div className="agent-capability-error"><p>{save.error.message}</p>{save.error instanceof ApiError && save.error.code === 'AGENT_CONVERSATION_MARKETPLACE_UNAVAILABLE' && onCreateEnhancedConversation && <div className="agent-capability-migration"><span>这条历史会话未在创建时注册原生能力市场。可新建一个空能力会话后，再按需选择要挂载的能力；此会话的历史内容会保留不变。</span><button type="button" className="secondary" disabled={save.isPending} onClick={() => onCreateEnhancedConversation([])}><Plus size={13}/>新建可使用能力的会话</button></div>}</div>}
      <footer>{!readonlyCreationOnly && selectedMcpIds.length > 0 && <button type="button" className="secondary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => void checkMcpReadiness(selectedMcpIds)}>重新检测 MCP</button>}<button type="button" className="secondary" disabled={save.isPending} onClick={onClose}>{readonlyCreationOnly ? '关闭' : '取消'}</button>{!readonlyCreationOnly && <button type="button" className="primary" disabled={save.isPending || checkingMcpIds.size > 0} onClick={() => save.mutate()}>{save.isPending ? '正在注册…' : bindingId ? '注册到当前会话' : '用于新建会话'}</button>}</footer></>}
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

function conversationScopeKey(conversation: AgentConversation): string {
  return conversation.work_directory_id ?? '__root__';
}

function moveConversationInGroup(items: AgentConversation[], sourceId: string, targetId: string, after: boolean): AgentConversation[] {
  const source = items.find(item => item.id === sourceId);
  if (!source || sourceId === targetId) return items;
  const withoutSource = items.filter(item => item.id !== sourceId);
  const targetIndex = withoutSource.findIndex(item => item.id === targetId);
  if (targetIndex < 0) return items;
  const next = [...withoutSource];
  next.splice(targetIndex + (after ? 1 : 0), 0, source);
  return next;
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
  // REST remains the source of truth for fields it has persisted.  A current
  // branch read is intentionally bounded, however, and the stream can carry a
  // fuller projection of the same formal event before that read catches up.
  // Merge by the native event ID so a refresh cannot make a visible ToolAction
  // briefly disappear (or lose its command/details) while the turn is active.
  // Append browser-only frames after the stable REST order so an optimistic
  // current user turn cannot jump in front of its eventual formal parent.
  const mergeEvent = (current: OpenHandsConversationEvent, incoming: OpenHandsConversationEvent): OpenHandsConversationEvent => ({
    ...current,
    ...incoming,
    payload: {
      ...current.payload,
      ...incoming.payload,
      details: { ...current.payload.details, ...incoming.payload.details },
    },
  });
  const merged = new Map(durable.map(event => [event.id, event]));
  for (const event of transient) {
    const current = merged.get(event.id);
    merged.set(event.id, current ? mergeEvent(current, event) : event);
  }
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
      || isOpenHandsAgentReply(event);
    return isTerminal && descendsFromActiveUser(event);
  });
}

function hasAssistantReplyForTurn(events: OpenHandsConversationEvent[], userEventId: string): boolean {
  const byId = new Map(events.map(event => [event.id, event]));
  return events.some(event => {
    if (!isOpenHandsAgentReply(event)) return false;
    const visited = new Set<string>();
    let parentId = event.payload.parent_id;
    while (parentId && parentId !== '__root__' && !visited.has(parentId)) {
      if (parentId === userEventId) return true;
      visited.add(parentId);
      parentId = byId.get(parentId)?.payload.parent_id;
    }
    return false;
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
    || /^(?:application\/(?:json|xml|javascript|sql|x-java-properties)|text\/(?:markdown|x-[^/]+))$/i.test(mimeType)
    || /\.(?:md|mdx|txt|json|ya?ml|toml|ini|conf|properties|xml|html?|css|scss|less|tsx?|jsx?|py|java|kt|go|rs|rb|php|sh|zsh|sql|graphql|vue|svelte)$/i.test(path);
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
    case 'toml': case 'ini': case 'conf': case 'properties': return 'ini';
    default: return undefined;
  }
}

function highlightedCode(value: string, language?: string): string {
  if (language && hljs.getLanguage(language)) return hljs.highlight(value, { language, ignoreIllegals: true }).value;
  return hljs.highlightAuto(value).value;
}

/**
 * Diff payloads contain only source lines (their Git prefix and hunk headers
 * have already been parsed away), so they can use the same safe highlighter as
 * a workspace source preview.  Keep the path at the call site: a Git Diff is
 * repository-relative, while a review Diff is workspace-relative.
 */
function HighlightedDiffCode({ path, value }: { path: string; value: string }) {
  return <code dangerouslySetInnerHTML={{ __html: highlightedCode(value, filePreviewLanguage(path)) }}/>;
}

function WorkspaceMarkdownCode({ className, children, ...props }: ComponentPropsWithoutRef<'code'>) {
  const language = /language-([\w+-]+)/.exec(className ?? '')?.[1];
  if (!language) return <code className={className} {...props}>{children}</code>;
  const value = String(children).replace(/\n$/, '');
  return <code className={className} {...props} dangerouslySetInnerHTML={{ __html: highlightedCode(value, language) }}/>;
}

function WorkspaceMarkdownPre({ children, node: _node, ...props }: ComponentPropsWithoutRef<'pre'> & { node?: unknown }) {
  void _node;
  if (isValidElement(children)) {
    const code = children.props as { className?: string; children?: ReactNode };
    const source = markdownCodeText(code.children).replace(/\n$/, '');
    if (source && isMermaidDiagram(code.className, source)) return <MermaidDiagram source={source}/>;
  }
  return <pre {...props}>{children}</pre>;
}

function WorkspaceMarkdownLink({ href, onOpenWorkspaceFile, onClick, node: _node, ...props }: ComponentPropsWithoutRef<'a'> & { onOpenWorkspaceFile?: (href: string) => boolean; node?: unknown }) {
  void _node;
  return <a {...props} href={href} onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented || !href) return;
    if (onOpenWorkspaceFile?.(href)) event.preventDefault();
  }}/>;
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
  // File annotation navigation is an intentional jump, so keep the first
  // selected line near the upper third of the viewport. This leaves enough
  // following context to read the selected block without pinning it at the
  // bottom edge of the file preview.
  const previewRect = scrollContainer.getBoundingClientRect();
  const rangeRect = Array.from(range.getClientRects()).at(0) ?? range.getBoundingClientRect();
  const rangeTop = scrollContainer.scrollTop + rangeRect.top - previewRect.top;
  const top = Math.max(0, rangeTop - scrollContainer.clientHeight / 3);
  const rangeLeft = scrollContainer.scrollLeft + rangeRect.left - previewRect.left;
  const rangeRight = rangeLeft + Math.max(rangeRect.width, 1);
  const horizontalPadding = Math.min(28, Math.max(12, scrollContainer.clientWidth * 0.05));
  let left = scrollContainer.scrollLeft;
  if (rangeLeft < scrollContainer.scrollLeft + horizontalPadding) left = Math.max(0, rangeLeft - horizontalPadding);
  else if (rangeRight > scrollContainer.scrollLeft + scrollContainer.clientWidth - horizontalPadding) left = Math.max(0, rangeRight - scrollContainer.clientWidth + horizontalPadding);
  if (top !== scrollContainer.scrollTop || left !== scrollContainer.scrollLeft) {
    scrollContainer.scrollTo({ top, left, behavior: 'auto' });
  }
  return range;
}

function selectPreviewText(root: HTMLElement, scrollContainer: HTMLElement, content: string, selection: FileSelection): Range | undefined {
  const range = revealPreviewText(root, scrollContainer, content, selection);
  if (!range) return undefined;
  const browserSelection = window.getSelection();
  browserSelection?.removeAllRanges(); browserSelection?.addRange(range);
  return range;
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

function previewSelectionRects(range: Range, preview: HTMLElement): WorkspaceSelectionRect[] {
  const previewRect = preview.getBoundingClientRect();
  const rects = Array.from(range.getClientRects())
    .filter(rect => rect.width > 0 && rect.height > 0)
    .map(rect => ({
      left: rect.left - previewRect.left + preview.scrollLeft,
      top: rect.top - previewRect.top + preview.scrollTop,
      width: rect.width,
      height: rect.height,
    }));
  // Syntax highlighting splits one visual source line into many spans. Merge
  // those fragments so programmatic navigation looks like one normal text
  // selection rather than a collection of small green boxes.
  return rects.reduce<WorkspaceSelectionRect[]>((merged, rect) => {
    const previous = merged.at(-1);
    if (previous && Math.abs(previous.top - rect.top) < 2 && Math.abs(previous.height - rect.height) < 2 && rect.left <= previous.left + previous.width + 3) {
      previous.width = Math.max(previous.width, rect.left + rect.width - previous.left);
    } else {
      merged.push(rect);
    }
    return merged;
  }, []);
}

function WorkspaceTextPreview({ path, content, highlight, highlightLine, onAnnotate, onOpenWorkspaceFile, lightweight = false }: { path: string; content: string; highlight?: FileSelection; highlightLine?: number; onAnnotate?: (selection: FileSelection, quote: string) => void; onOpenWorkspaceFile?: (href: string) => boolean; lightweight?: boolean }) {
  const previewRef = useRef<HTMLDivElement>(null);
  const previewContentRef = useRef<HTMLElement>(null);
  const [selectionAction, setSelectionAction] = useState<{ selection: FileSelection; quote: string; left: number; top: number; highlights: WorkspaceSelectionRect[] }>();
  const [pinnedSelectionHighlights, setPinnedSelectionHighlights] = useState<WorkspaceSelectionRect[]>();
  const [lineHighlight, setLineHighlight] = useState<number>();
  const markdownPreview = !lightweight && /\.(?:md|mdx|markdown)$/i.test(path);
  const codeLines = useMemo(() => content.split('\n'), [content]);
  const positionSelectionAction = useCallback((selection: FileSelection, quote: string, range: Range) => {
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
      selection, quote, left, top,
      // Native selection paint can disappear when the action is rendered.
      // Keep an independent visual layer inside this scrolling preview so the
      // selected file content remains unambiguously visible.
      highlights: previewSelectionRects(range, preview),
    });
  }, []);
  const highlightRange = useCallback((range: Range) => {
    const preview = previewRef.current;
    if (!preview) return;
    setPinnedSelectionHighlights(previewSelectionRects(range, preview));
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
    setPinnedSelectionHighlights(undefined);
    if (highlight || markdownPreview) {
      const range = selectPreviewText(source, preview, content, selection);
      if (!range) return;
      if (highlight) highlightRange(range);
    } else {
      revealPreviewText(source, preview, content, selection);
      setLineHighlight(line);
    }
    const timer = window.setTimeout(() => {
      window.getSelection()?.removeAllRanges();
      preview.classList.remove('workspace-selection-flash');
      setLineHighlight(current => current === line ? undefined : current);
      setPinnedSelectionHighlights(undefined);
    }, highlight ? 3_800 : 1_600);
    preview.classList.add('workspace-selection-flash');
    return () => window.clearTimeout(timer);
  }, [content, highlight, highlightLine, highlightRange, markdownPreview]);
  const captureSelection = () => {
    const preview = previewRef.current;
    const source = previewContentRef.current;
    if (!preview || !source) return;
    setPinnedSelectionHighlights(undefined);
    const selection = selectionFromPreview(source, content);
    const range = window.getSelection()?.rangeCount ? window.getSelection()?.getRangeAt(0) : undefined;
    if (!selection || !range) {
      setSelectionAction(undefined);
      return;
    }
    if (onAnnotate) positionSelectionAction(selection, range.toString(), range);
  };
  const action = selectionAction && <><div className="agent-file-selection-highlights" aria-hidden="true">{selectionAction.highlights.map((rect, index) => <i key={`${rect.left}:${rect.top}:${index}`} style={rect}/>)}</div><span className="agent-file-selection-action" style={{ left: selectionAction.left, top: selectionAction.top }}><button type="button" onMouseDown={event => event.preventDefault()} onMouseUp={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); onAnnotate?.(selectionAction.selection, selectionAction.quote); setSelectionAction(undefined); window.getSelection()?.removeAllRanges(); }}><Quote size={13}/>添加到会话</button></span></>;
  const pinnedHighlight = pinnedSelectionHighlights?.length ? <div className="agent-file-selection-highlights pinned" aria-hidden="true">{pinnedSelectionHighlights.map((rect, index) => <i key={`${rect.left}:${rect.top}:${index}`} style={rect}/>)}</div> : undefined;
  // A large Markdown table can produce tens of thousands of table-cell DOM
  // nodes. Keep the first paged window responsive by showing it as raw text;
  // users can still load the next page or download the complete file.
  if (lightweight) return <pre className="agent-file-large-text-preview">{content}</pre>;
  if (markdownPreview) {
    return <div ref={previewRef} className="agent-file-preview-selection" onMouseUp={captureSelection}>{pinnedHighlight}{action}<article ref={previewContentRef} className="agent-file-markdown-preview"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ pre: WorkspaceMarkdownPre, code: WorkspaceMarkdownCode, a: props => <WorkspaceMarkdownLink {...props} onOpenWorkspaceFile={onOpenWorkspaceFile}/> }}>{content}</ReactMarkdown></article></div>;
  }
  const language = filePreviewLanguage(path);
  return <div ref={previewRef} className="agent-file-preview-selection" onMouseUp={captureSelection}>{pinnedHighlight}{action}<div className={`agent-file-code-preview${language ? ' highlighted' : ''}`}><ol className="agent-file-line-numbers" aria-hidden="true">{codeLines.map((_, index) => <li key={index}>{index + 1}</li>)}</ol>{lineHighlight && <i className="agent-file-line-highlight" style={{ '--source-line': lineHighlight } as CSSProperties}/>}<code ref={previewContentRef} dangerouslySetInnerHTML={{ __html: highlightedCode(content, language) }}/></div></div>;
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

type SplitDiffRow<T> = {
  before?: { line: T; index: number };
  after?: { line: T; index: number };
};

/**
 * Align each contiguous replacement block before handing it to the two panes.
 * A unified diff serializes removals and additions, whereas a split diff must
 * show them on the same visual rows.
 */
function splitDiffRows<T extends { kind: 'context' | 'addition' | 'deletion' }>(
  lines: T[],
  replacementBoundary: (previous: T, next: T) => boolean = () => false,
): SplitDiffRow<T>[] {
  const rows: SplitDiffRow<T>[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (line.kind === 'context') {
      rows.push({ before: { line, index }, after: { line, index } });
      index += 1;
      continue;
    }
    const deletions: Array<{ line: T; index: number }> = [];
    const additions: Array<{ line: T; index: number }> = [];
    const blockStart = index;
    while (index < lines.length && lines[index].kind !== 'context'
      && (index === blockStart || !replacementBoundary(lines[index - 1], lines[index]))) {
      const changed = { line: lines[index], index };
      if (changed.line.kind === 'deletion') deletions.push(changed);
      else additions.push(changed);
      index += 1;
    }
    for (let offset = 0; offset < Math.max(deletions.length, additions.length); offset += 1) {
      rows.push({ before: deletions[offset], after: additions[offset] });
    }
  }
  return rows;
}

/**
 * A split diff has one horizontal code position, not two independently
 * scrollable panes. The bottom scrollbar drives the clipped code bodies in
 * both columns while their headers, gutters, and divider stay put.
 */
function SharedSplitDiff({ before, after, resetKey }: { before: ReactNode; after: ReactNode; resetKey: string }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const scrollbarRef = useRef<HTMLDivElement>(null);
  const [scrollbarWidth, setScrollbarWidth] = useState(0);
  const applyOffset = useCallback((scrollLeft: number) => {
    viewportRef.current?.querySelectorAll<HTMLElement>('.agent-diff-split-code-content').forEach(content => {
      content.style.setProperty('--agent-diff-code-offset', `-${scrollLeft}px`);
    });
  }, []);
  const measureScrollbar = useCallback(() => {
    const viewport = viewportRef.current;
    const scrollbar = scrollbarRef.current;
    if (!viewport || !scrollbar) return;
    const lines = Array.from(viewport.querySelectorAll<HTMLElement>('.agent-diff-split .agent-diff-line:not(.empty)'));
    // The gutter is deliberately fixed while only the code cell moves.  The
    // scroll range must therefore be calculated from each code cell's actual
    // visible width, not from the entire half-pane; otherwise it reaches its
    // end one gutter-width before a long line is fully revealed.
    const maxOffset = Math.max(0, ...lines.map(line => {
      const code = line.querySelector<HTMLElement>('code');
      if (!code) return 0;
      const visibleCodeWidth = Math.max(0, line.clientWidth - code.offsetLeft);
      return Math.max(0, code.scrollWidth - visibleCodeWidth);
    }));
    setScrollbarWidth(Math.ceil(scrollbar.clientWidth + maxOffset));
  }, []);
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const scrollbar = scrollbarRef.current;
    if (!viewport || !scrollbar) return;
    viewport.scrollTop = 0;
    scrollbar.scrollLeft = 0;
    applyOffset(0);
    measureScrollbar();
    const observer = new ResizeObserver(measureScrollbar);
    observer.observe(viewport);
    observer.observe(scrollbar);
    return () => observer.disconnect();
  }, [applyOffset, measureScrollbar, resetKey]);
  const scrollSplitCode = (event: ReactWheelEvent<HTMLDivElement>) => {
    const viewport = viewportRef.current;
    const scrollbar = scrollbarRef.current;
    if (!viewport || !scrollbar) return;
    const horizontalDelta = event.deltaX || (event.shiftKey ? event.deltaY : 0);
    if (horizontalDelta) {
      event.preventDefault();
      event.stopPropagation();
      const maxLeft = Math.max(0, scrollbar.scrollWidth - scrollbar.clientWidth);
      scrollbar.scrollLeft = Math.min(maxLeft, Math.max(0, scrollbar.scrollLeft + horizontalDelta));
      applyOffset(scrollbar.scrollLeft);
      return;
    }
    const maxTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const reachesVerticalBoundary = (event.deltaY < 0 && viewport.scrollTop <= 0)
      || (event.deltaY > 0 && viewport.scrollTop >= maxTop - 1);
    event.stopPropagation();
    if (reachesVerticalBoundary) {
      event.preventDefault();
      viewport.scrollTop = Math.min(maxTop, Math.max(0, viewport.scrollTop + event.deltaY));
    }
  };
  return <div className="agent-diff-split">
    <div ref={viewportRef} className="agent-diff-split-viewport" onWheelCapture={scrollSplitCode}>
      <div className="agent-diff-split-content">
        <div className="agent-diff-before agent-diff-split-pane"><header>修改前</header><div className="agent-diff-split-code-content">{before}</div></div>
        <div className="agent-diff-split-pane"><header>修改后</header><div className="agent-diff-split-code-content">{after}</div></div>
      </div>
    </div>
    <div ref={scrollbarRef} className="agent-diff-split-scrollbar" aria-label="同步查看两侧被截断的代码" onScroll={event => applyOffset(event.currentTarget.scrollLeft)}><div style={{ width: scrollbarWidth }}/></div>
  </div>;
}

function WorkspaceChangesReview({ changes, selectedId, onSelect, onOpenSource, workspaceRoot }: { changes: WorkspaceFileChange[]; selectedId?: string; onSelect: (id: string) => void; onOpenSource: (change: WorkspaceFileChange, line: number) => void; workspaceRoot?: string }) {
  const [mode, setMode] = useState<'unified' | 'split'>('split');
  const selected = changes.find(change => change.id === selectedId) ?? changes[0];
  useEffect(() => { if (selected && selected.id !== selectedId) onSelect(selected.id); }, [onSelect, selected, selectedId]);
  const stopDiffOverscroll = (event: ReactWheelEvent<HTMLElement>) => {
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
    }
  };
  if (!selected) return <div className="agent-changes-empty"><b>没有可审查的文件改动</b><span>仅显示 OpenHands FileEditor 已成功写入、且带有原始前后内容的改动。</span></div>;
  const splitLines = splitDiffRows(selected.lines);
  const renderSplitLine = (entry: SplitDiffRow<WorkspaceFileChange['lines'][number]>['before'], side: 'before' | 'after', rowIndex: number) => {
    if (!entry) return <div key={`${side}:${rowIndex}`} className="agent-diff-line empty" aria-hidden="true"/>;
    const { line, index } = entry;
    const number = side === 'before' ? line.oldLine : line.newLine;
    return <button type="button" className={`agent-diff-line ${line.kind}`} key={`${side}:${rowIndex}:${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForDiffLine(selected.lines, index)} 行`} onClick={() => onOpenSource(selected, sourceLineForDiffLine(selected.lines, index))}><i>{number ?? ''}</i><HighlightedDiffCode path={selected.path} value={line.text || ' '}/></button>;
  };
  return <section className="agent-changes-review">
    <ChangedFilesTree
      title="改动文件"
      empty="没有可审查的文件改动。"
      items={changes.map(change => ({ path: workspaceRelativePath(change.path, workspaceRoot), value: change }))}
      selectedPath={workspaceRelativePath(selected.path, workspaceRoot)}
      onSelect={change => onSelect(change.id)}
      renderMeta={change => <em><ins>{`+${change.additions}`}</ins><del>{`-${change.deletions}`}</del></em>}
    />
    <article className="agent-changes-diff">
      <header><div><b title={workspaceRelativePath(selected.path, workspaceRoot)}>{workspaceRelativePath(selected.path, workspaceRoot)}</b><small><ins>{`+${selected.additions}`}</ins><del>{`-${selected.deletions}`}</del></small></div><div className="agent-changes-diff-actions"><button type="button" className="agent-open-source-file" onClick={() => onOpenSource(selected, sourceLineForDiffLine(selected.lines, selected.lines.findIndex(line => line.kind !== 'deletion')))}><FileCode2 size={12}/>查看源文件</button><div className="agent-diff-mode"><button type="button" className={mode === 'unified' ? 'active' : ''} onClick={() => setMode('unified')}>统一</button><button type="button" className={mode === 'split' ? 'active' : ''} onClick={() => setMode('split')}>并排</button></div></div></header>
      {mode === 'unified'
        ? <pre className="agent-diff-unified" onWheelCapture={stopDiffOverscroll}>{selected.lines.map((line, index) => <button type="button" className={`agent-diff-line ${line.kind}`} key={`${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForDiffLine(selected.lines, index)} 行`} onClick={() => onOpenSource(selected, sourceLineForDiffLine(selected.lines, index))}><i>{line.oldLine ?? line.newLine ?? ''}</i><strong>{line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '-' : ' '}</strong><HighlightedDiffCode path={selected.path} value={line.text || ' '}/></button>)}</pre>
        : <SharedSplitDiff resetKey={selected.id} before={splitLines.map((row, index) => renderSplitLine(row.before, 'before', index))} after={splitLines.map((row, index) => renderSplitLine(row.after, 'after', index))}/>
      }
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

/**
 * Resolve a Markdown link only when it stays inside the currently authorized
 * workspace. Relative links in an Agent reply are file references, not web
 * routes; routing them through the drawer avoids a browser-level navigation.
 */
function normalizedWorkspacePath(path: string): string {
  const absolute = path.startsWith('/');
  const parts: string[] = [];
  for (const part of path.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  return `${absolute ? '/' : ''}${parts.join('/')}`;
}

function workspaceMarkdownFileHref(href: string): boolean {
  return Boolean(href && !/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(href) && href.split(/[?#]/, 1)[0]);
}

function workspaceMarkdownLinkPath(href: string, workingDirectory?: string, sourcePath?: string): string | undefined {
  const root = workingDirectory ? normalizedWorkspacePath(workingDirectory.replace(/\\/g, '/')) : '';
  if (!root || !workspaceMarkdownFileHref(href)) return undefined;
  const rawPath = href.split(/[?#]/, 1)[0];
  if (!rawPath) return undefined;
  let path: string;
  try { path = decodeURIComponent(rawPath).replace(/\\/g, '/'); } catch { return undefined; }
  const normalizedSource = sourcePath ? normalizedWorkspacePath(sourcePath.replace(/\\/g, '/')) : undefined;
  const sourceDirectory = normalizedSource && normalizedSource.startsWith(`${root}/`)
    ? normalizedSource.slice(0, normalizedSource.lastIndexOf('/'))
    : root;
  const resolved = normalizedWorkspacePath(path.startsWith('/') ? path : `${sourceDirectory}/${path}`);
  return resolved === root || resolved.startsWith(`${root}/`) ? resolved : undefined;
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

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function WorkspaceFileTree({ entries, root, selectedFile, selectedPaths, expanded, pagination, loadingDirectories, onExpandedChange, onLoadMore, onSelect, onSelectionChange, onActivateDirectory, onContextMenu, fileDownloadUrl }: { entries: WorkspaceEntry[]; root: string; selectedFile?: string; selectedPaths: Set<string>; expanded: Set<string>; pagination: Map<string, string | undefined>; loadingDirectories: Set<string>; onExpandedChange: (updater: (current: Set<string>) => Set<string>) => void; onLoadMore: (parentPath?: string) => void; onSelect: (path?: string) => void; onSelectionChange: (paths: Set<string>) => void; onActivateDirectory: (path?: string) => void; onContextMenu: (path: string, kind: 'file' | 'directory', event: ReactMouseEvent<HTMLButtonElement>) => void; fileDownloadUrl: (path: string) => string }) {
  const nodes = useMemo(() => workspaceTree(entries, root), [entries, root]);
  const selectionAnchor = useRef<string | undefined>(undefined);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const visibleNodes = useMemo(() => {
    const visible: Array<{ node: WorkspaceTreeNode; depth: number }> = [];
    const collect = (items: WorkspaceTreeNode[], depth = 0) => items.forEach(node => {
      visible.push({ node, depth });
      if (node.kind === 'directory' && expanded.has(node.path)) collect(node.children, depth + 1);
    });
    collect(nodes);
    return visible;
  }, [expanded, nodes]);
  useEffect(() => {
    if (!selectedFile) return;
    // Source navigation can expand several lazy directory pages before the
    // target row exists. Once it does, keep the selected source visible rather
    // than merely marking an off-screen row active.
    rowRefs.current.get(selectedFile)?.scrollIntoView({ block: 'nearest' });
  }, [selectedFile, visibleNodes]);
  const selectEntry = (node: WorkspaceTreeNode, event: ReactMouseEvent<HTMLButtonElement>) => {
    const toggling = event.metaKey || event.ctrlKey;
    const anchorIndex = selectionAnchor.current ? visibleNodes.findIndex(item => item.node.path === selectionAnchor.current) : -1;
    const targetIndex = visibleNodes.findIndex(item => item.node.path === node.path);
    const deselectingOnlyEntry = node.kind === 'file'
      && !event.shiftKey
      && !toggling
      && selectedPaths.size === 1
      && selectedPaths.has(node.path);
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
    else {
      onActivateDirectory(node.path);
      onExpandedChange(current => {
        const next = new Set(current);
        if (next.has(node.path)) next.delete(node.path); else next.add(node.path);
        return next;
      });
    }
  };
  const renderNodes = (items: WorkspaceTreeNode[] = nodes, depth = 0): ReactNode => items.map(node => {
    const open = expanded.has(node.path);
    const hasMore = node.kind === 'directory' && open && Boolean(pagination.get(node.path));
    const loading = loadingDirectories.has(node.path);
    return <div key={node.path} className={`agent-file-tree-node${node.kind === 'directory' && open ? ' open-directory' : ''}`}>
      <div ref={element => { if (element) rowRefs.current.set(node.path, element); else rowRefs.current.delete(node.path); }} className={`agent-file-tree-row${selectedPaths.has(node.path) ? ' selected' : ''}`} role="treeitem" aria-expanded={node.kind === 'directory' ? open : undefined} aria-level={depth + 1} aria-selected={selectedPaths.has(node.path)} style={{ '--tree-depth': depth } as CSSProperties}>
      {node.kind === 'directory' ? <button type="button" className="agent-file-tree-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${node.name}`} onClick={() => onExpandedChange(current => { const next = new Set(current); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); return next; })}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button> : <span className="agent-tree-spacer" aria-hidden="true"/>}
      <button type="button" draggable={node.kind === 'file'} className={`agent-file-tree-item ${node.kind}${selectedFile === node.path ? ' active' : ''}`} onDragStart={event => {
        if (node.kind !== 'file') return;
        event.dataTransfer.effectAllowed = 'copy';
        event.dataTransfer.setData(WORKSPACE_FILE_TRANSFER_TYPE, node.path);
      }} onClick={event => selectEntry(node, event)} onContextMenu={event => { event.preventDefault(); if (node.kind === 'directory') onActivateDirectory(node.path); onContextMenu(node.path, node.kind, event); }}>
        {node.kind === 'directory' ? open ? <FolderOpen size={14}/> : <Folder size={14}/> : <FileCode2 size={14}/>}
        <span>{node.name}</span>
      </button>
      {node.kind === 'file' && <span className="agent-file-tree-file-meta"><em className="agent-file-tree-size">{formatFileSize(node.size)}</em><a className="agent-file-tree-download" href={fileDownloadUrl(node.path)} aria-label={`下载 ${node.name}`} title={`下载 ${node.name}`}><Download size={12}/></a></span>}
      </div>
      {node.kind === 'directory' && open && <div role="group">{renderNodes(node.children, depth + 1)}{hasMore && <button type="button" className="agent-file-tree-load-more" style={{ '--tree-depth': depth + 1 } as CSSProperties} disabled={loading} onClick={() => onLoadMore(node.path)}>{loading ? '正在加载…' : '加载更多'}</button>}</div>}
    </div>;
  });
  return <div className="agent-file-tree" role="tree" aria-label="工作区目录树">
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

function selectedGitRepository(repositories: AgentSessionWorkspaceDetails['repositories'], selectedPath?: string) {
  return repositories
    // A repository may be exactly the selected work-directory root.  It is a
    // valid Git repository, not merely a workspace container: filtering it
    // out hides the complete Git sidebar for root workspaces such as
    // `hq-interface`, while an identically structured nested workspace works.
    .filter(item => selectedPath === item.path || Boolean(selectedPath?.startsWith(`${item.path}/`)))
    .sort((left, right) => right.path.length - left.path.length)[0];
}

function WorkspaceGitSidebar({ details, repository, selectedCommit, onSelectCommit, loadLog, loadCommit, loadDiff, loadChanges, loadWorkingDiff, onOpenFileDiff, onOpenWorkingDiff, closedDiffEpoch }: {
  details: AgentSessionWorkspaceDetails;
  repository: AgentSessionWorkspaceDetails['repositories'][number];
  selectedCommit?: string;
  onSelectCommit: (commit?: string) => void;
  loadLog: (repositoryPath: string) => Promise<import('../../types').WorkspaceGitLog>;
  loadCommit: (repositoryPath: string, commit: string) => Promise<WorkspaceGitCommitDetails>;
  loadDiff: (repositoryPath: string, commit: string, path: string) => Promise<WorkspaceGitFileDiff>;
  loadChanges: (repositoryPath: string) => Promise<WorkspaceGitChanges>;
  loadWorkingDiff: (repositoryPath: string, kind: WorkspaceGitChangeKind, path: string) => Promise<WorkspaceGitFileDiff>;
  onOpenFileDiff: (details: WorkspaceGitCommitDetails, diff: WorkspaceGitFileDiff) => void;
  onOpenWorkingDiff: (repository: WorkspaceGitChanges['repository'], kind: WorkspaceGitChangeKind, file: WorkspaceGitChangedFile, diff: WorkspaceGitFileDiff) => void;
  closedDiffEpoch: number;
}) {
  const [mode, setMode] = useState<'history' | 'changes'>('history');
  const [selectedCommitFile, setSelectedCommitFile] = useState<string>();
  const openedDiffRef = useRef<string | undefined>(undefined);
  const [workingDiffError, setWorkingDiffError] = useState('');
  useEffect(() => {
    openedDiffRef.current = undefined;
    setSelectedCommitFile(undefined);
    setWorkingDiffError('');
    setMode('history');
  }, [repository.path]);
  useEffect(() => {
    if (!closedDiffEpoch) return;
    openedDiffRef.current = undefined;
    setSelectedCommitFile(undefined);
  }, [closedDiffEpoch]);
  const logQuery = useQuery({
    queryKey: ['workspace-git-log', repository.path],
    queryFn: () => loadLog(repository.path),
    enabled: mode === 'history',
    staleTime: 15_000,
  });
  const changesQuery = useQuery({
    queryKey: ['workspace-git-changes', repository.path],
    queryFn: () => loadChanges(repository.path),
    enabled: mode === 'changes',
    staleTime: 5_000,
  });
  const commitQuery = useQuery({
    queryKey: ['workspace-git-commit', repository.path, selectedCommit],
    queryFn: () => loadCommit(repository.path, selectedCommit!),
    enabled: Boolean(mode === 'history' && selectedCommit),
  });
  const diffQuery = useQuery({
    queryKey: ['workspace-git-diff', repository.path, selectedCommit, selectedCommitFile],
    queryFn: () => loadDiff(repository.path, selectedCommit!, selectedCommitFile!),
    enabled: Boolean(mode === 'history' && selectedCommit && selectedCommitFile),
  });
  useEffect(() => {
    if (!commitQuery.data || !diffQuery.data || !selectedCommitFile) return;
    const key = `${commitQuery.data.commit.id}:${selectedCommitFile}`;
    if (openedDiffRef.current === key) return;
    openedDiffRef.current = key;
    onOpenFileDiff(commitQuery.data, diffQuery.data);
  }, [commitQuery.data, diffQuery.data, onOpenFileDiff, selectedCommitFile]);
  const openWorkingFile = async (kind: WorkspaceGitChangeKind, file: WorkspaceGitChangedFile) => {
    const key = `${kind}:${file.path}`;
    if (openedDiffRef.current === key || !changesQuery.data) return;
    openedDiffRef.current = key;
    setWorkingDiffError('');
    try {
      const diff = await loadWorkingDiff(repository.path, kind, file.path);
      onOpenWorkingDiff(changesQuery.data.repository, kind, file, diff);
    } catch {
      openedDiffRef.current = undefined;
      setWorkingDiffError('文件 Diff 读取失败，请重试。');
    }
  };
  const selectMode = (next: 'history' | 'changes') => {
    setMode(next);
    onSelectCommit(undefined);
    setSelectedCommitFile(undefined);
    setWorkingDiffError('');
    openedDiffRef.current = undefined;
  };
  return <aside className="agent-workspace-git-sidebar" aria-label="Git">
    <header><div><span><GitBranch size={15}/>Git</span><b title={workspaceRelativePath(repository.path, details.root)}>{workspaceRelativePath(repository.path, details.root)}</b></div>{repository.branch && <em title="当前分支（只读，暂不支持切换）">{repository.branch}</em>}</header>
    <nav className="agent-git-view-tabs" aria-label="Git 视图"><button type="button" className={mode === 'history' ? 'active' : ''} aria-pressed={mode === 'history'} onClick={() => selectMode('history')}>提交记录</button><button type="button" className={mode === 'changes' ? 'active' : ''} aria-pressed={mode === 'changes'} onClick={() => selectMode('changes')}>本地改动</button></nav>
    {mode === 'history' ? logQuery.isLoading ? <p className="agent-git-loading">正在读取提交历史…</p> : logQuery.isError ? <p className="agent-git-error">Git 历史读取失败。<button type="button" onClick={() => void logQuery.refetch()}>重试</button></p> : <>
      <div className="agent-git-log">{(logQuery.data?.commits ?? []).map(commit => <button key={commit.id} type="button" onClick={() => { onSelectCommit(commit.id); setSelectedCommitFile(undefined); }}><b>{commit.subject || '（无提交说明）'}</b><span><code>{commit.short_id}</code><em>{commit.author}</em><time>{commit.date}</time></span></button>)}{!logQuery.data?.commits.length && <p>该仓库没有可展示的提交。</p>}</div>
      {selectedCommit && <WorkspaceGitCommitSidebarDetail details={commitQuery.data} loading={commitQuery.isLoading} error={commitQuery.isError} selectedPath={selectedCommitFile} onSelectFile={path => { openedDiffRef.current = undefined; setSelectedCommitFile(path); }} onClose={() => { onSelectCommit(undefined); setSelectedCommitFile(undefined); }}/>}
    </> : changesQuery.isLoading ? <p className="agent-git-loading">正在读取本地改动…</p> : changesQuery.isError ? <p className="agent-git-error">本地改动读取失败。<button type="button" onClick={() => void changesQuery.refetch()}>重试</button></p> : <div className="agent-git-local-changes">
      {workingDiffError && <p className="agent-git-error" role="alert">{workingDiffError}</p>}
      <ChangedFilesTree title="暂存区" empty="暂存区没有文件。" items={(changesQuery.data?.staged ?? []).map(file => ({ path: file.path, value: file }))} onSelect={file => void openWorkingFile('STAGED', file)} renderMeta={file => <em>{file.status}</em>}/>
      <ChangedFilesTree title="未暂存" empty="没有未暂存文件。" items={(changesQuery.data?.unstaged ?? []).map(file => ({ path: file.path, value: file }))} onSelect={file => void openWorkingFile('UNSTAGED', file)} renderMeta={file => <em>{file.status}</em>}/>
    </div>}
  </aside>;
}

type WorkspaceToolTab =
  | { id: 'files'; kind: 'files' }
  | { id: 'changes'; kind: 'changes' }
  | { id: 'sources'; kind: 'sources' }
  | { id: 'git'; kind: 'git'; details: WorkspaceGitCommitDetails; diff: WorkspaceGitFileDiff }
  | { id: 'git-working'; kind: 'git-working'; repository: WorkspaceGitChanges['repository']; changeKind: WorkspaceGitChangeKind; file: WorkspaceGitChangedFile; diff: WorkspaceGitFileDiff }
  | { id: 'subagents'; kind: 'subagents' }
  | { id: string; kind: 'terminal'; terminalInstanceId: string };
type WorkspaceToolScopeState = { tabs: WorkspaceToolTab[]; activeTabId?: string; selectedFile?: string; selectedChangeId?: string; selectedGitFile?: string; selectedGitCommit?: string; selectedGitRepositoryPath?: string; selectedRuntimeTaskId?: string };
type MarkdownFileRequest = { key: string; path: string };

type ChangedFileTreeNode<T> = { name: string; path: string; value?: T; children: ChangedFileTreeNode<T>[] };
type CollapsibleTreeNode = { name: string; path: string; children: CollapsibleTreeNode[] };

function collapseDirectoryChain<T extends CollapsibleTreeNode>(node: T): { node: T; label: string } {
  let terminal = node;
  const names = [node.name];
  // Keep the first directory which contains a changed file or multiple
  // branches visible. Everything before that is navigation-only context.
  while (terminal.children.length === 1 && terminal.children[0].children.length > 0) {
    terminal = terminal.children[0] as T;
    names.push(terminal.name);
  }
  return { node: terminal, label: names.join('/') };
}

function changedFileTree<T>(items: Array<{ path: string; value: T }>): ChangedFileTreeNode<T>[] {
  const roots: ChangedFileTreeNode<T>[] = [];
  for (const item of items) {
    const parts = item.path.split('/').filter(Boolean);
    let children = roots;
    let parentPath = '';
    parts.forEach((name, index) => {
      const path = parentPath ? `${parentPath}/${name}` : name;
      const leaf = index === parts.length - 1;
      let node = children.find(candidate => candidate.path === path);
      if (!node) {
        node = { name, path, value: leaf ? item.value : undefined, children: [] };
        children.push(node);
      }
      children = node.children;
      parentPath = path;
    });
  }
  const sort = (nodes: ChangedFileTreeNode<T>[]) => {
    nodes.sort((left, right) => Number(Boolean(right.children.length)) - Number(Boolean(left.children.length)) || left.name.localeCompare(right.name));
    nodes.forEach(node => sort(node.children));
  };
  sort(roots);
  return roots;
}

function ChangedFilesTree<T>({ items, selectedPath, title, empty, onSelect, renderMeta }: {
  items: Array<{ path: string; value: T }>;
  selectedPath?: string;
  title: string;
  empty: string;
  onSelect: (value: T) => void;
  renderMeta?: (value: T) => ReactNode;
}) {
  const tree = changedFileTree(items);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const renderTree = (nodes: ChangedFileTreeNode<T>[], depth = 0): ReactNode => nodes.map(node => {
    const collapsedDirectory = node.children.length ? collapseDirectoryChain(node) : undefined;
    const displayed = collapsedDirectory?.node ?? node;
    const directory = displayed.children.length > 0;
    const label = collapsedDirectory?.label ?? node.name;
    const open = !collapsed.has(displayed.path);
    const toggle = () => setCollapsed(current => {
      const next = new Set(current);
      if (next.has(displayed.path)) next.delete(displayed.path); else next.add(displayed.path);
      return next;
    });
    return <div key={node.path} className="agent-git-tree-node" role="treeitem" aria-expanded={directory ? open : undefined} aria-level={depth + 1}>
      <div className={`agent-git-tree-row${selectedPath === node.path ? ' active' : ''}`} style={{ '--git-tree-depth': depth } as CSSProperties}>
        {directory ? <button type="button" className="agent-git-tree-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${label}`} onClick={toggle}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button> : <span className="agent-git-tree-spacer" aria-hidden="true"/>}
        <button type="button" className="agent-git-tree-item" title={displayed.path} onClick={() => { if (directory) toggle(); else if (node.value !== undefined) onSelect(node.value); }}><span>{directory ? open ? <FolderOpen size={14}/> : <Folder size={14}/> : <FileCode2 size={14}/>}</span><b>{directory ? `${label}/` : label}</b>{!directory && node.value !== undefined && renderMeta?.(node.value)}</button>
      </div>
      {directory && open && <div role="group">{renderTree(displayed.children as ChangedFileTreeNode<T>[], depth + 1)}</div>}
    </div>;
  });
  return <nav className="agent-changes-file-tree" aria-label={title}>
    <header><b>{title}</b><span>{items.length}</span></header>
    {tree.length ? <div className="agent-git-file-tree" role="tree">{renderTree(tree)}</div> : <p>{empty}</p>}
  </nav>;
}

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
    const collapsedDirectory = node.children.length ? collapseDirectoryChain(node) : undefined;
    const displayed = collapsedDirectory?.node ?? node;
    const directory = displayed.children.length > 0;
    const label = collapsedDirectory?.label ?? node.name;
    const open = expanded.has(displayed.path);
    const toggleDirectory = () => setExpanded(current => {
      const next = new Set(current);
      if (next.has(displayed.path)) next.delete(displayed.path);
      else next.add(displayed.path);
      return next;
    });
    return <div key={node.path} className="agent-git-tree-node" role="treeitem" aria-expanded={directory ? open : undefined} aria-level={depth + 1}>
      <div className={`agent-git-tree-row${selectedPath === node.path ? ' active' : ''}`} style={{ '--git-tree-depth': depth } as CSSProperties}>
        {directory ? <button type="button" className="agent-git-tree-disclosure" aria-label={`${open ? '收起' : '展开'}目录 ${label}`} onClick={toggleDirectory}>{open ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}</button> : <span className="agent-git-tree-spacer" aria-hidden="true"/>}
        <button type="button" className="agent-git-tree-item" title={displayed.path} onClick={() => { if (directory) toggleDirectory(); else onSelectFile(node.path); }}><span>{directory ? open ? <FolderOpen size={14}/> : <Folder size={14}/> : <FileCode2 size={14}/>}</span><b>{directory ? `${label}/` : label}</b>{node.status && <em>{node.status}</em>}</button>
      </div>
      {directory && open && <div role="group">{renderTree(displayed.children as WorkspaceGitTreeNode[], depth + 1)}</div>}
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

type GitDiffLine = { oldLine?: number; newLine?: number; kind: 'context' | 'addition' | 'deletion'; text: string; hunk: number };

function gitDiffLines(value: string): GitDiffLine[] {
  const lines: GitDiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  let hunkNumber = 0;
  let inHunk = false;
  for (const sourceLine of value.split('\n')) {
    const hunkMatch = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(sourceLine);
    if (hunkMatch) {
      oldLine = Number(hunkMatch[1]);
      newLine = Number(hunkMatch[2]);
      hunkNumber += 1;
      inHunk = true;
      continue;
    }
    if (!inHunk || sourceLine === '\\ No newline at end of file') continue;
    if (sourceLine.startsWith('+')) {
      lines.push({ kind: 'addition', newLine, text: sourceLine.slice(1), hunk: hunkNumber });
      newLine += 1;
    } else if (sourceLine.startsWith('-')) {
      lines.push({ kind: 'deletion', oldLine, text: sourceLine.slice(1), hunk: hunkNumber });
      oldLine += 1;
    } else if (sourceLine.startsWith(' ')) {
      lines.push({ kind: 'context', oldLine, newLine, text: sourceLine.slice(1), hunk: hunkNumber });
      oldLine += 1;
      newLine += 1;
    }
  }
  return lines;
}

function WorkspaceGitFileDiffReview({ details, diff, onOpenSource }: { details: WorkspaceGitCommitDetails; diff: WorkspaceGitFileDiff; onOpenSource: (path: string, line: number) => void }) {
  const [mode, setMode] = useState<'unified' | 'split'>('split');
  const lines = useMemo(() => gitDiffLines(diff.diff), [diff.diff]);
  const splitLines = useMemo(() => splitDiffRows(lines, (previous, next) => previous.hunk !== next.hunk), [lines]);
  // Git diff paths are always relative to the repository root, whereas the
  // shared workspace navigator expects a path in the current work-directory
  // coordinate system. Keep the Git root here so every navigation affordance
  // (including rows in split mode) uses the same absolute source path.
  const sourcePath = `${details.repository.path.replace(/\/+$/, '')}/${diff.path}`;
  const stopDiffOverscroll = (event: ReactWheelEvent<HTMLElement>) => {
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
    }
  };
  const renderSplitLine = (entry: SplitDiffRow<GitDiffLine>['before'], side: 'before' | 'after', rowIndex: number) => {
    if (!entry) return <div key={`${side}:${rowIndex}`} className="agent-diff-line empty" aria-hidden="true"/>;
    const { line, index } = entry;
    const number = side === 'before' ? line.oldLine : line.newLine;
    return <button type="button" className={`agent-diff-line ${line.kind}`} key={`${side}:${rowIndex}:${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForGitDiffLine(lines, index)} 行`} onClick={() => onOpenSource(sourcePath, sourceLineForGitDiffLine(lines, index))}><i>{number ?? ''}</i><HighlightedDiffCode path={diff.path} value={line.text || ' '}/></button>;
  };
  return <section className="agent-git-file-diff-review">
    <header><div><b title={diff.path}>{diff.path}</b><small><code>{details.commit.short_id}</code><span title={details.commit.subject}>{details.commit.subject || '（无提交说明）'}</span>{diff.truncated && <em>已截断</em>}</small></div><div className="agent-changes-diff-actions"><button type="button" className="agent-open-source-file" onClick={() => onOpenSource(sourcePath, sourceLineForGitDiffLine(lines, lines.findIndex(line => line.kind !== 'deletion')))}><FileCode2 size={12}/>查看源文件</button>{lines.length > 0 && <div className="agent-diff-mode"><button type="button" className={mode === 'unified' ? 'active' : ''} onClick={() => setMode('unified')}>统一</button><button type="button" className={mode === 'split' ? 'active' : ''} onClick={() => setMode('split')}>并排</button></div>}</div></header>
    {!lines.length ? <p className="agent-git-file-diff-empty">{diff.diff ? '该文件没有可展示的文本行级 Diff。' : '该文件没有可显示的文本 Diff。'}</p> : mode === 'unified'
      ? <pre className="agent-diff-unified" onWheelCapture={stopDiffOverscroll}>{lines.map((line, index) => <button type="button" className={`agent-diff-line ${line.kind}`} key={`${line.oldLine ?? ''}:${line.newLine ?? ''}:${line.text}`} title={`打开源文件第 ${sourceLineForGitDiffLine(lines, index)} 行`} onClick={() => onOpenSource(sourcePath, sourceLineForGitDiffLine(lines, index))}><i>{line.oldLine ?? line.newLine ?? ''}</i><strong>{line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '-' : ' '}</strong><HighlightedDiffCode path={diff.path} value={line.text || ' '}/></button>)}</pre>
      : <SharedSplitDiff resetKey={diff.path} before={splitLines.map((row, index) => renderSplitLine(row.before, 'before', index))} after={splitLines.map((row, index) => renderSplitLine(row.after, 'after', index))}/>
    }
  </section>;
}

function WorkspaceGitCommitReview({ details, initialDiff, loadDiff, onOpenSource }: {
  details: WorkspaceGitCommitDetails;
  initialDiff: WorkspaceGitFileDiff;
  loadDiff: (path: string) => Promise<WorkspaceGitFileDiff>;
  onOpenSource: (path: string, line: number) => void;
}) {
  const [selectedPath, setSelectedPath] = useState(initialDiff.path);
  const diffQuery = useQuery({
    queryKey: ['workspace-git-commit-review-diff', details.repository.path, details.commit.id, selectedPath],
    queryFn: () => selectedPath === initialDiff.path ? Promise.resolve(initialDiff) : loadDiff(selectedPath),
  });
  return <section className="agent-git-commit-review">
    <ChangedFilesTree
      title="提交文件"
      empty="这个提交没有可展示的文件。"
      items={details.files.map(file => ({ path: file.path, value: file }))}
      selectedPath={selectedPath}
      onSelect={file => setSelectedPath(file.path)}
      renderMeta={file => <em>{file.status}</em>}
    />
    {diffQuery.isLoading ? <div className="agent-changes-empty"><span>正在读取文件 Diff…</span></div> : diffQuery.isError || !diffQuery.data ? <div className="agent-changes-empty"><b>文件 Diff 读取失败</b><span>请重新选择文件后重试。</span></div> : <WorkspaceGitFileDiffReview details={details} diff={diffQuery.data} onOpenSource={onOpenSource}/>}
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
  open, onOpen, onClose, onAnnotateFileSelection, highlightedFileSelection, workspaceId, scopeKey, migrateFromScopeKey, bindingId, workDirectoryId, conversation, conversationCumulativeTokens, attachments, sources, attachmentRequest, candidatePreviewRequest, markdownFileRequest, reviewChanges = [], reviewRequestId, sessionChanges = [], onReviewChanges, runtimeAvailable, runtimeTasks, agentDefinitions, sessionStopped,
}: {
  open: boolean; onOpen: () => void; onClose: () => void; onAnnotateFileSelection?: (path: string, selection: FileSelection, quote: string) => void; highlightedFileSelection?: { path: string; selection: FileSelection }; workspaceId: string; scopeKey: string; migrateFromScopeKey?: string; bindingId?: string; workDirectoryId?: string; conversation?: AgentConversation; conversationCumulativeTokens?: number | null; attachments: AgentAttachment[]; sources: ConversationSource[]; attachmentRequest?: { key: string; attachment: AgentAttachment }; candidatePreviewRequest?: CandidateFilePreviewRequest; markdownFileRequest?: MarkdownFileRequest; reviewChanges?: WorkspaceFileChange[]; reviewRequestId?: string; sessionChanges?: WorkspaceFileChange[]; onReviewChanges?: (changes: WorkspaceFileChange[]) => void; runtimeAvailable: boolean; runtimeTasks: RuntimeTaskProjection[]; agentDefinitions: CapabilityAsset[]; sessionStopped: boolean;
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
  const handledMarkdownFileRequestKey = useRef<string | undefined>(undefined);
  const handledReviewRequestId = useRef<string | undefined>(undefined);
  const [candidatePreview, setCandidatePreview] = useState<CandidateFilePreviewRequest>();
  const [sourceFileNavigation, setSourceFileNavigation] = useState<{ path: string; line: number }>();
  const [pendingSourceNavigation, setPendingSourceNavigation] = useState<{ path: string; line: number; directories: string[] }>();
  const [selectedEntryPaths, setSelectedEntryPaths] = useState<Set<string>>(new Set());
  const [activeDirectory, setActiveDirectory] = useState<string>();
  const [gitContextPath, setGitContextPath] = useState<string>();
  const [gitSidebarRequested, setGitSidebarRequested] = useState(false);
  const [closedGitDiffEpoch, setClosedGitDiffEpoch] = useState(0);
  const [expandedFilePaths, setExpandedFilePaths] = useState<Set<string>>(new Set());
  const [expandAllFileDirectories, setExpandAllFileDirectories] = useState(false);
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
    setGitSidebarRequested(false);
    setExpandedFilePaths(new Set());
    setExpandAllFileDirectories(false);
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
    queryFn: ({ signal }) => api.filePreview(workspaceId, selectedFile!, { bindingId, workDirectoryId }, undefined, signal),
    enabled: Boolean(open && scopeState.activeTabId === 'files' && textPreviewable),
    retry: false,
  });
  const [previewState, setPreviewState] = useState<{ path: string; content: string; totalBytes: number; nextOffset?: number }>();
  const [previewMoreLoading, setPreviewMoreLoading] = useState(false);
  useEffect(() => {
    if (!selectedFile || !previewQuery.data) {
      setPreviewState(undefined);
      return;
    }
    setPreviewState({ path: selectedFile, ...previewQuery.data });
  }, [previewQuery.data, selectedFile]);
  const loadMorePreview = useCallback(async () => {
    if (!selectedFile || previewState?.nextOffset === undefined || previewMoreLoading) return;
    setPreviewMoreLoading(true);
    try {
      const next = await api.filePreview(
        workspaceId,
        selectedFile,
        { bindingId, workDirectoryId },
        previewState.nextOffset,
      );
      setPreviewState(current => current?.path === selectedFile ? {
        path: selectedFile,
        content: current.content + next.content,
        totalBytes: next.totalBytes || current.totalBytes,
        nextOffset: next.nextOffset,
      } : current);
    } catch (reason) {
      setPanelError(reason instanceof Error ? reason.message : '继续读取文件失败');
    } finally {
      setPreviewMoreLoading(false);
    }
  }, [api, bindingId, previewMoreLoading, previewState, selectedFile, workDirectoryId, workspaceId]);
  const lightweightPreview = Boolean(
    previewState && (previewState.totalBytes > 128 * 1024 || previewState.content.length > 128 * 1024),
  );
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
  const fileDirectoryPaths = useMemo(() => {
    if (!details) return [];
    const paths: string[] = [];
    const collect = (items: WorkspaceTreeNode[]) => items.forEach(node => {
      if (node.kind !== 'directory') return;
      paths.push(node.path);
      collect(node.children);
    });
    collect(workspaceTree(visibleFiles, details.working_directory));
    return paths;
  }, [details, visibleFiles]);
  useEffect(() => {
    if (!expandAllFileDirectories) return;
    setExpandedFilePaths(current => {
      const missing = fileDirectoryPaths.filter(path => !current.has(path));
      return missing.length ? new Set([...current, ...missing]) : current;
    });
  }, [expandAllFileDirectories, fileDirectoryPaths]);
  const toggleAllFileDirectories = () => {
    if (expandAllFileDirectories) {
      setExpandAllFileDirectories(false);
      setExpandedFilePaths(new Set());
      return;
    }
    setExpandAllFileDirectories(true);
    setExpandedFilePaths(new Set(fileDirectoryPaths));
  };
  const updateExpandedFilePaths = (updater: (current: Set<string>) => Set<string>) => {
    setExpandedFilePaths(current => {
      const next = updater(current);
      if (next.size < current.size) setExpandAllFileDirectories(false);
      return next;
    });
  };
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
    if (!details) return;
    const root = details.working_directory.replace(/\/+$/, '');
    const path = highlightedFileSelection.path;
    if (path !== root && !path.startsWith(`${root}/`)) return;
    const parts = path.slice(root.length).split('/').filter(Boolean);
    const directories = parts.slice(0, -1).map((_, index) => `${root}/${parts.slice(0, index + 1).join('/')}`);
    // Reuse source navigation so a file annotation expands each lazy parent
    // directory before selecting its leaf in the visible tree.
    setPendingSourceNavigation({ path, line: highlightedFileSelection.selection.start_line, directories });
  }, [details, highlightedFileSelection, openFiles]);
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
  const openGitWorkingDiff = useCallback((repository: WorkspaceGitChanges['repository'], changeKind: WorkspaceGitChangeKind, file: WorkspaceGitChangedFile, diff: WorkspaceGitFileDiff) => {
    updateScope(current => ({
      ...current,
      tabs: current.tabs.some(tab => tab.kind === 'git-working')
        ? current.tabs.map(tab => tab.kind === 'git-working' ? { id: 'git-working', kind: 'git-working', repository, changeKind, file, diff } : tab)
        : [...current.tabs, { id: 'git-working', kind: 'git-working', repository, changeKind, file, diff }],
      activeTabId: 'git-working',
    }));
    onOpen();
  }, [onOpen, updateScope]);
  const openGitHistory = useCallback(() => {
    if (!gitContextPath) return;
    setGitSidebarRequested(true);
  }, [gitContextPath]);
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
    setGitSidebarRequested(false);
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
  const openMarkdownPreviewLink = useCallback((href: string) => {
    if (!workspaceMarkdownFileHref(href)) return false;
    const path = workspaceMarkdownLinkPath(href, details?.working_directory, selectedFile);
    if (!path) {
      setPanelError('链接目标不在当前工作目录中。');
      return true;
    }
    openSourcePath(path, 1);
    return true;
  }, [details?.working_directory, openSourcePath, selectedFile]);
  const openSourceFile = useCallback((change: WorkspaceFileChange, line: number) => {
    openSourcePath(change.path, line);
  }, [openSourcePath]);
  useEffect(() => {
    if (!markdownFileRequest || handledMarkdownFileRequestKey.current === markdownFileRequest.key) return;
    handledMarkdownFileRequestKey.current = markdownFileRequest.key;
    openSourcePath(markdownFileRequest.path, 1);
  }, [markdownFileRequest, openSourcePath]);
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
    if (tab.kind === 'git' || tab.kind === 'git-working') setClosedGitDiffEpoch(current => current + 1);
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
    enabled: Boolean(fullScreen && filesTabIsActive && gitSidebarRequested && gitContextPath),
    staleTime: 15_000,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const gitRepository = useMemo(() => selectedGitRepository(gitRepositoriesQuery.data?.repositories ?? [], gitContextPath), [gitContextPath, gitRepositoriesQuery.data?.repositories]);
  const gitSidebarVisible = fullScreen && filesTabIsActive && gitSidebarRequested && Boolean(gitRepository);
  const gitOptions = { bindingId, workDirectoryId };
  const sshRemoteReady = Boolean(
    details?.ide.gateway.supported
    && details.ide.gateway.host
    && details.ide.gateway.port
    && details.ide.gateway.path,
  );
  const conversationUsage = conversation?.usage;
  const conversationTotalTokens = typeof conversationCumulativeTokens === 'number' && conversationCumulativeTokens > 0
    ? conversationCumulativeTokens
    : conversationUsage?.total_tokens ?? 0;
  const sessionChangeAdditions = sessionChanges.reduce((total, change) => total + change.additions, 0);
  const sessionChangeDeletions = sessionChanges.reduce((total, change) => total + change.deletions, 0);
  const visibleSources = sources.slice(0, 3);
  const summary = details && <section className="agent-workspace-overview">
    {conversation && <article className="agent-workspace-conversation-config"><Bot size={16}/><div><small>会话用量</small><p className="agent-workspace-usage-line"><span>{`累计 ${conversationTotalTokens.toLocaleString('zh-CN')} Token`}</span><span>{`$${(conversationUsage?.accumulated_cost ?? 0).toFixed(6)}`}</span></p></div></article>}
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
      <header><nav className="agent-workspace-tabs" aria-label="工作区工具页签">{scopeState.tabs.map(tab => <div key={tab.id} className={scopeState.activeTabId === tab.id ? 'active' : ''}><button type="button" className="agent-workspace-tab-select" onClick={() => updateScope(current => ({ ...current, activeTabId: tab.id }))}><span>{tab.kind === 'files' ? '文件' : tab.kind === 'changes' ? `审查${reviewChanges.length ? ` · ${reviewChanges.length}` : ''}` : tab.kind === 'sources' ? `来源${sources.length ? ` · ${sources.length}` : ''}` : tab.kind === 'git' ? `提交 · ${tab.details.commit.short_id}` : tab.kind === 'git-working' ? `${tab.changeKind === 'STAGED' ? '暂存' : '本地'} · ${tab.file.path.split('/').at(-1)}` : tab.kind === 'subagents' ? '子智能体' : details?.runtime.container_id || (details?.runtime.write_available ? '终端' : '连接中…')}</span></button><button type="button" className="agent-workspace-tab-close" aria-label={`关闭${tab.kind === 'files' ? '文件' : tab.kind === 'changes' ? '改动审查' : tab.kind === 'sources' ? '来源' : tab.kind === 'git' ? '提交审查' : tab.kind === 'git-working' ? '本地改动 Diff' : tab.kind === 'subagents' ? '子智能体' : `终端 ${details?.runtime.container_id || ''}`}页签`} disabled={tab.kind === 'terminal' && closingTerminalId === tab.terminalInstanceId} onClick={() => { if (tab.kind !== 'terminal' || closingTerminalId !== tab.terminalInstanceId) requestCloseTab(tab); }}><X size={12}/></button></div>)}</nav><div className="agent-workspace-tool-actions"><div ref={toolMenuRef} className="agent-workspace-tool-menu"><button type="button" className="agent-workspace-tool-menu-trigger" aria-label="新增工作区工具" aria-expanded={toolMenuOpen} aria-haspopup="menu" onClick={() => setToolMenuOpen(current => !current)}><Plus size={15}/></button>{toolMenuOpen && <div role="menu"><button type="button" role="menuitem" onClick={() => { openFiles(); setToolMenuOpen(false); }}><FileCode2 size={13}/>文件</button><button type="button" role="menuitem" onClick={() => { openGitHistory(); setToolMenuOpen(false); }}><GitBranch size={13}/>Git 历史</button>{reviewChanges.length > 0 && <button type="button" role="menuitem" onClick={() => { openChanges(); setToolMenuOpen(false); }}><FileText size={13}/>审查改动</button>}{sources.length > 0 && <button type="button" role="menuitem" onClick={() => { openSources(); setToolMenuOpen(false); }}><Link2 size={13}/>来源</button>}{runtimeTasks.length > 0 && <button type="button" role="menuitem" onClick={() => { openRuntimeTasks(); setToolMenuOpen(false); }}><Bot size={13}/>子智能体</button>}<button type="button" role="menuitem" disabled={!runtimeAvailable} onClick={() => { openTerminal(); setToolMenuOpen(false); }}><Plus size={13}/>终端</button></div>}</div><button type="button" aria-label={fullScreen ? '退出全屏' : '全屏查看工作区工具'} title={fullScreen ? '退出全屏（Esc）' : '全屏查看'} onClick={() => setFullScreen(current => !current)}>{fullScreen ? <Minimize2 size={16}/> : <Maximize2 size={16}/>}</button><button type="button" aria-label="关闭工作区工具" onClick={() => { setFullScreen(false); onClose(); }}><X size={16}/></button></div></header>
      <div className="agent-workspace-tool-body">
        {panelError && <p className="agent-workspace-panel-error" role="alert"><span>{panelError}</span><button type="button" aria-label="关闭错误提示" onClick={() => setPanelError('')}><X size={13}/></button></p>}
        {loadingOrError || (!scopeState.tabs.length ? <div className="agent-drawer-empty"><b>选择工作区工具</b><span>文件仅打开一个页签；终端可按需打开多个独立实例。</span><div><button type="button" className="secondary" onClick={() => openFiles()}>打开文件</button><button type="button" className="secondary" disabled={!runtimeAvailable} onClick={openTerminal}>新建终端</button></div></div> : details && <div className={`agent-workspace-tool-content${gitSidebarVisible ? ' fullscreen-git-layout' : ''}`}>
          {scopeState.tabs.some(tab => tab.kind === 'files') && <section className={`agent-workspace-files ${scopeState.activeTabId === 'files' ? 'active' : ''}${gitSidebarVisible ? ' fullscreen-git' : ''}`} style={{ '--file-tree-width': `${fileTreeWidth}px` } as CSSProperties}>
            <div className="agent-file-tree-pane">
              <header className="agent-file-tree-toolbar"><span>{selectedEntryPaths.size ? `已选 ${selectedEntryPaths.size} 项` : '文件'}</span><div className="agent-file-tree-actions"><button type="button" title="新建文件" aria-label="新建文件" onClick={() => createAtActiveDirectory('FILE')}><FileCode2 size={13}/></button><button type="button" title="新建目录" aria-label="新建目录" onClick={() => createAtActiveDirectory('DIRECTORY')}><FolderPlus size={13}/></button><button type="button" className={`agent-file-tree-expand-toggle${expandAllFileDirectories ? ' expanded' : ''}`} title={expandAllFileDirectories ? '全部收起' : '全部展开'} aria-label={expandAllFileDirectories ? '全部收起目录' : '全部展开目录'} disabled={!fileDirectoryPaths.length} onClick={toggleAllFileDirectories}>{expandAllFileDirectories ? <ChevronRight size={13}/> : <ChevronDown size={13}/>}</button><button type="button" className="danger" title="删除选中项" aria-label="删除选中项" disabled={!selectedEntryRoots.length} onClick={() => void removeEntries(selectedEntryRoots.map(path => ({ path, kind: visibleFiles.find(item => item.path === path)?.kind ?? 'directory' })))}><Trash2 size={13}/></button></div></header>
              <WorkspaceFileTree entries={visibleFiles} root={details.working_directory} selectedFile={selectedFile} selectedPaths={selectedEntryPaths} expanded={expandedFilePaths} pagination={new Map([...directoryPages].map(([path, page]) => [path, page.nextCursor]))} loadingDirectories={loadingDirectoryPaths} onExpandedChange={updateExpandedFilePaths} onLoadMore={parentPath => { void loadDirectory(parentPath); }} onSelect={path => { setActiveDirectory(undefined); selectFile(path); }} onSelectionChange={setSelectedEntryPaths} onActivateDirectory={path => { setActiveDirectory(path); setGitContextPath(path); setGitSidebarRequested(Boolean(path)); }} onContextMenu={(path, kind, event) => { setEntryMenu({ path, kind, x: Math.min(event.clientX, window.innerWidth - 190), y: Math.min(event.clientY, window.innerHeight - 190) }); }} fileDownloadUrl={path => fileUrl(workspaceId, path, { bindingId, workDirectoryId, download: true })}/>
            </div>
            <div className="agent-file-tree-resizer" role="separator" aria-label="调整文件目录宽度" aria-orientation="vertical" onPointerDown={startFileTreeResize}/>
            <div className="agent-file-preview">{candidatePreview ? <>
              <header><span title={candidatePreview.filename}>{candidatePreview.filename} · 候选输出</span></header>
              <iframe className="agent-file-media-preview" sandbox="" title={`${candidatePreview.filename} 候选文件预览`} src={candidatePreview.url}/>
            </> : selectedFile ? <>
              <header><span title={selectedFile}>{selectedAttachment?.filename || relativeWorkspacePath(selectedFile, details.root)}</span><a href={fileUrl(workspaceId, selectedFile, { bindingId, workDirectoryId, download: true })}><Download size={13}/>下载</a></header>
              {canPreviewImage ? <img className="agent-file-media-preview" src={selectedAttachment?.image_data_url || selectedFileUrl} alt={selectedAttachment?.filename || '附件预览'}/> : canPreviewPdf ? <iframe className="agent-file-media-preview" title={selectedAttachment?.filename || 'PDF 预览'} src={selectedFileUrl}/> : textPreviewable ? previewQuery.isLoading ? <p>正在读取文件…</p> : previewQuery.isError ? <p>文件预览不可用，请下载后查看。</p> : previewState ? <div className="agent-file-preview-paged"><WorkspaceTextPreview path={selectedFile} content={previewState.content} lightweight={lightweightPreview} highlight={lightweightPreview ? undefined : highlightedFileSelection?.path === selectedFile ? highlightedFileSelection.selection : undefined} highlightLine={lightweightPreview ? undefined : sourceFileNavigation?.path === selectedFile ? sourceFileNavigation.line : undefined} onAnnotate={!lightweightPreview && onAnnotateFileSelection ? (selection, quote) => onAnnotateFileSelection(selectedFile, selection, quote) : undefined} onOpenWorkspaceFile={openMarkdownPreviewLink}/>{previewState.nextOffset !== undefined && <footer><span>已加载 {formatFileSize(previewState.nextOffset)} / {formatFileSize(previewState.totalBytes)}</span><button type="button" onClick={() => void loadMorePreview()} disabled={previewMoreLoading}>{previewMoreLoading ? '正在加载…' : '加载更多'}</button></footer>}</div> : null : <p>此文件不提供浏览器预览，请下载后查看。</p>}
            </> : <p>选择一个文件以预览或下载。</p>}</div>
          </section>}
          {scopeState.tabs.some(tab => tab.kind === 'changes') && <div className={`agent-changes-tab-panel ${scopeState.activeTabId === 'changes' ? 'active' : ''}`}><WorkspaceChangesReview changes={reviewChanges} selectedId={scopeState.selectedChangeId} onSelect={selectedChangeId => updateScope(current => ({ ...current, selectedChangeId }))} onOpenSource={openSourceFile} workspaceRoot={details.working_directory}/></div>}
          {scopeState.tabs.some(tab => tab.kind === 'sources') && <div className={`agent-changes-tab-panel ${scopeState.activeTabId === 'sources' ? 'active' : ''}`}><ConversationSourcesReview sources={sources} onOpenAttachment={attachment => { setCandidatePreview(undefined); selectFile(attachment.path); }}/></div>}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'git' }> => tab.kind === 'git').map(tab => <div key={tab.id} className={`agent-changes-tab-panel agent-git-commit-tab ${scopeState.activeTabId === tab.id ? 'active' : ''}`}><WorkspaceGitCommitReview key={`${tab.details.commit.id}:${tab.diff.path}`} details={tab.details} initialDiff={tab.diff} loadDiff={path => api.gitDiff(workspaceId, tab.details.repository.path, tab.details.commit.id, path, gitOptions)} onOpenSource={openSourcePath}/></div>)}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'git-working' }> => tab.kind === 'git-working').map(tab => <div key={tab.id} className={`agent-changes-tab-panel agent-git-commit-tab ${scopeState.activeTabId === tab.id ? 'active' : ''}`}><WorkspaceGitFileDiffReview details={{ repository: tab.repository, commit: { id: tab.changeKind, short_id: tab.changeKind === 'STAGED' ? '暂存区' : '未暂存', author: '', date: '', subject: tab.changeKind === 'STAGED' ? '已加入暂存区' : '尚未加入暂存区' }, files: [tab.file] }} diff={tab.diff} onOpenSource={openSourcePath}/></div>)}
          {scopeState.tabs.some(tab => tab.kind === 'subagents') && <div className={`agent-subagent-tab-panel ${scopeState.activeTabId === 'subagents' ? 'active' : ''}`}><RuntimeTaskTab tasks={runtimeTasks} definitions={agentDefinitions} selectedTaskId={scopeState.selectedRuntimeTaskId} onSelect={taskId => updateScope(current => ({ ...current, selectedRuntimeTaskId: taskId }))} sessionStopped={sessionStopped}/></div>}
          {scopeState.tabs.filter((tab): tab is Extract<WorkspaceToolTab, { kind: 'terminal' }> => tab.kind === 'terminal').map(tab => <div key={tab.id} className={`agent-terminal-tab-panel ${scopeState.activeTabId === tab.id ? 'active' : ''}`}>{runtimeAvailable ? <WorkspaceTerminal workspaceId={workspaceId} terminalInstanceId={tab.terminalInstanceId} bindingId={bindingId} workDirectoryId={workDirectoryId} workingDirectory={details.working_directory}/> : <div className="agent-drawer-empty"><LoaderCircle className="agent-drawer-spinner" size={20}/><b>终端正在恢复</b><span>文件仍可使用；运行环境恢复后终端会自动可用。</span></div>}</div>)}
          {gitSidebarVisible && gitRepository && <WorkspaceGitSidebar details={details} repository={gitRepository} selectedCommit={scopeState.selectedGitRepositoryPath === gitRepository.path ? scopeState.selectedGitCommit : undefined} onSelectCommit={commit => updateScope(current => ({
            ...current,
            selectedGitRepositoryPath: commit ? gitRepository.path : undefined,
            selectedGitCommit: commit,
          }))} loadLog={repositoryPath => api.gitLog(workspaceId, repositoryPath, gitOptions)} loadCommit={(repositoryPath, commit) => api.gitCommit(workspaceId, repositoryPath, commit, gitOptions)} loadDiff={(repositoryPath, commit, path) => api.gitDiff(workspaceId, repositoryPath, commit, path, gitOptions)} loadChanges={repositoryPath => api.gitChanges(workspaceId, repositoryPath, gitOptions)} loadWorkingDiff={(repositoryPath, kind, path) => api.gitWorkingDiff(workspaceId, repositoryPath, kind, path, gitOptions)} onOpenFileDiff={openGitFileDiff} onOpenWorkingDiff={openGitWorkingDiff} closedDiffEpoch={closedGitDiffEpoch}/>}
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
  // New-conversation recovery is loaded only after the authorized workspace
  // identity is known. The former host-wide key had no workspace boundary.
  const initialConversationDraft = useRef<ConversationDraftRecovery | undefined>(undefined);
  const initialComposerDraft = initialBootstrapRecovery.current?.message.content ?? initialConversationDraft.current?.content ?? '';
  const composerDraftRef = useRef(initialComposerDraft);
  const composerRef = useRef<ComposerHandle>(null);
  const [composerHasText, setComposerHasText] = useState(() => Boolean(initialComposerDraft.trim()));
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
  const [expiredTerminalSyncTurnKey, setExpiredTerminalSyncTurnKey] = useState<string>();
  const [requestStartedAt, setRequestStartedAt] = useState<number>();
  const [confirmationReason, setConfirmationReason] = useState('');
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [draggedQueuedMessageId, setDraggedQueuedMessageId] = useState<string>();
  const [queuedMessageMenuId, setQueuedMessageMenuId] = useState<string>();
  const [pendingRewrite, setPendingRewrite] = useState<RewriteRequest>();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [reviewChanges, setReviewChanges] = useState<WorkspaceFileChange[]>([]);
  const [reviewRequestId, setReviewRequestId] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [draggedBindingId, setDraggedBindingId] = useState<string>();
  const [dragTarget, setDragTarget] = useState<{ bindingId: string; after: boolean }>();
  const [conversationOrder, setConversationOrder] = useState<Record<string, string[]>>({});
  const [conversationOrderSync, setConversationOrderSync] = useState<Record<string, ConversationOrderSync>>({});
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
  const [composerAnnotations, setComposerAnnotations] = useState<AgentConversationAnnotation[]>(
    () => initialBootstrapRecovery.current?.message.annotations ?? initialConversationDraft.current?.annotations ?? [],
  );
  const [workspaceReferencePickerOpen, setWorkspaceReferencePickerOpen] = useState(false);
  const [workspaceReferenceQuery, setWorkspaceReferenceQuery] = useState('');
  const [attachmentRequest, setAttachmentRequest] = useState<{ key: string; attachment: AgentAttachment }>();
  const [markdownFileRequest, setMarkdownFileRequest] = useState<MarkdownFileRequest>();
  const [fileSelectionReference, setFileSelectionReference] = useState<{ path: string; selection: FileSelection }>();
  useEffect(() => {
    const openSelection = (event: Event) => {
      const reference = (event as CustomEvent<AgentWorkspaceReference>).detail;
      if (reference?.selection) {
        setFileSelectionReference({ path: reference.path, selection: { ...reference.selection } });
        setDrawerOpen(true);
      }
    };
    window.addEventListener('flowweave:open-workspace-selection', openSelection);
    return () => window.removeEventListener('flowweave:open-workspace-selection', openSelection);
  }, []);
  const [candidatePreviewRequest, setCandidatePreviewRequest] = useState<CandidateFilePreviewRequest>();
  const [operationError, setOperationError] = useState<Error>();
  const [historyLoadingBindingId, setHistoryLoadingBindingId] = useState<string>();
  const [historyPrepend, setHistoryPrepend] = useState<ConversationHistoryPrepend>();
  const [streamHold, setStreamHold] = useState<{ bindingId: string; expiresAt: number }>();
  const [pageVisible, setPageVisible] = useState(() => document.visibilityState === 'visible');
  const [pendingCreatedId, setPendingCreatedId] = useState<string>();
  const [pendingMigratedSend, setPendingMigratedSend] = useState<BoundQueuedMessage>();
  const [conversationDraft, setConversationDraft] = useState<ConversationDraft | undefined>(() => initialBootstrapRecovery.current?.draft ?? initialConversationDraft.current?.draft);
  const [bootstrapRecovery, setBootstrapRecovery] = useState<BootstrapRecovery | undefined>(() => initialBootstrapRecovery.current);
  const [workspaceScopeMigration, setWorkspaceScopeMigration] = useState<string>();
  const [workDirectoryCreatorOpen, setWorkDirectoryCreatorOpen] = useState(false);
  const [capabilityManagerOpen, setCapabilityManagerOpen] = useState(false);
  const [workspacePathCopied, setWorkspacePathCopied] = useState(false);
  const [conversationSearchOpen, setConversationSearchOpen] = useState(false);
  const [conversationSearchId, setConversationSearchId] = useState<string>();
  const [conversationSearchTargetEventId, setConversationSearchTargetEventId] = useState<string>();
  const [sidebarListMode, setSidebarListMode] = useState<'workspaces' | 'activity'>('workspaces');
  const [sidebarRevealBindingId, setSidebarRevealBindingId] = useState<string>();
  const attachmentInput = useRef<HTMLInputElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const workspacePathCopyTimer = useRef<number | undefined>(undefined);
  const pendingLiveEvents = useRef<OpenHandsConversationEvent[]>([]);
  const liveEventsFrame = useRef<number | undefined>(undefined);
  const historyLoadingScopes = useRef(new Set<string>());
  const historyFailedCursors = useRef(new Map<string, string>());
  const historyPrependWaiters = useRef(new Map<number, {
    scope: string;
    capture?: (accepted: boolean) => void;
    restore?: (accepted: boolean) => void;
  }>());
  const nextHistoryPrependId = useRef(0);
  const draggedConversationRef = useRef<string | undefined>(undefined);
  const dragTargetRef = useRef<{ bindingId: string; after: boolean } | undefined>(undefined);
  const pointerDragGroupRef = useRef<AgentConversation[] | undefined>(undefined);
  const queuedMessagesStorageKeyRef = useRef<string | undefined>(undefined);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  const nativeGuidanceOptimisticEventIds = useRef<Map<string, string>>(new Map());
  useEffect(() => () => {
    for (const resource of [
      'conversation-events',
      'conversation-input-readiness',
      'conversation-context',
    ]) {
      queryClient.removeQueries({ queryKey: sessionQueryKey(host, resource) });
    }
  }, [host, queryClient]);
  useEffect(() => () => {
    if (workspacePathCopyTimer.current !== undefined) window.clearTimeout(workspacePathCopyTimer.current);
  }, []);
  const bootstrapTransitionScope = useRef<string | undefined>(undefined);
  const selectedBindingId = host.bindingIdFromPathname(withoutDeploymentBase(window.location.pathname));
  const previousComposerScope = useRef<string | undefined>(undefined);
  const activityBaseline = useRef<Map<string, boolean>>(new Map());
  const [unreadConversationIds, setUnreadConversationIds] = useState<Set<string>>(() => new Set());
  const [pinnedConversationIds, setPinnedConversationIds] = useState<Set<string>>(() => new Set());
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
  const conversationSearchSupported = Boolean(api.startConversationSearch && api.conversationSearch);
  const conversationSearchQuery = useQuery<AgentConversationSearch>({
    queryKey: sessionQueryKey(host, 'conversation-search', workspace?.id, conversationSearchId),
    queryFn: () => api.conversationSearch!(workspace!.id, conversationSearchId!),
    enabled: Boolean(workspace && conversationSearchId && api.conversationSearch),
    refetchInterval: query => {
      const state = query.state.data?.state;
      return state === 'PENDING' || state === 'RUNNING' ? 1_000 : false;
    },
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const draftRecoveryStorageKey = workspace && conversationDraft
    ? conversationDraftStorageKey(host.id, workspace.id, conversationDraft.workDirectoryId)
    : undefined;
  const unreadStorageKey = workspace ? unreadConversationStorageKey(host.id, workspace.id) : undefined;
  const pinnedStorageKey = workspace ? pinnedConversationStorageKey(host.id, workspace.id) : undefined;
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
  const conversations = useMemo(() => {
    const serverOrder = [...(conversationsQuery.data?.pages.flatMap(page => page.items) ?? [])].sort(
      (left, right) => (Number(right.sort_key) || Date.parse(right.created_at))
        - (Number(left.sort_key) || Date.parse(left.created_at))
        || right.id.localeCompare(left.id),
    );
    const groups = new Map<string, AgentConversation[]>();
    for (const item of serverOrder) {
      const scope = conversationScopeKey(item);
      const group = groups.get(scope);
      if (group) group.push(item);
      else groups.set(scope, [item]);
    }
    return [...groups.values()].flatMap(group => {
      const local = conversationOrder[conversationScopeKey(group[0])];
      if (!local) return group;
      const indexed = new Map(local.map((id, index) => [id, index]));
      return [...group].sort((left, right) => {
        const leftIndex = indexed.get(left.id);
        const rightIndex = indexed.get(right.id);
        if (leftIndex !== undefined && rightIndex !== undefined) return leftIndex - rightIndex;
        if (leftIndex !== undefined) return -1;
        if (rightIndex !== undefined) return 1;
        return group.indexOf(left) - group.indexOf(right);
      });
    });
  }, [conversationOrder, conversationsQuery.data]);
  const pinnedConversations = useMemo(() => {
    const conversationsById = new Map(conversations.map(item => [item.id, item]));
    return [...pinnedConversationIds].flatMap(bindingId => {
      const item = conversationsById.get(bindingId);
      return item ? [item] : [];
    });
  }, [conversations, pinnedConversationIds]);
  const unpinnedConversations = conversations.filter(item => !pinnedConversationIds.has(item.id));
  const activityConversations = useMemo(() => conversations
    .filter(item => conversationIsRunning(item.execution_status) || unreadConversationIds.has(item.id))
    .sort((left, right) => {
      const leftUpdatedAt = Date.parse(left.updated_at) || Date.parse(left.created_at) || 0;
      const rightUpdatedAt = Date.parse(right.updated_at) || Date.parse(right.created_at) || 0;
      return rightUpdatedAt - leftUpdatedAt || right.id.localeCompare(left.id);
    }), [conversations, unreadConversationIds]);
  const revealedUnpinnedConversation = sidebarRevealBindingId && !pinnedConversationIds.has(sidebarRevealBindingId)
    ? conversations.find(item => item.id === sidebarRevealBindingId)
    : undefined;
  useEffect(() => {
    if (sidebarListMode !== 'workspaces' || !sidebarRevealBindingId) return;
    let timer: number | undefined;
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const row = Array.from(document.querySelectorAll<HTMLElement>('.agent-workbench-list [data-conversation-binding-id]')).find(
          item => item.dataset.conversationBindingId === sidebarRevealBindingId,
        );
        row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        timer = window.setTimeout(() => {
          setSidebarRevealBindingId(current => current === sidebarRevealBindingId ? undefined : current);
        }, 2_600);
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [sidebarListMode, sidebarRevealBindingId]);
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
  const activeHistoryScope = useRef<string | undefined>(selected?.id);
  activeHistoryScope.current = selected?.id;
  const resolveHistoryPrepend = useCallback((transaction: ConversationHistoryPrepend, accepted: boolean) => {
    const waiter = historyPrependWaiters.current.get(transaction.id);
    if (!waiter || waiter.scope !== transaction.scope || activeHistoryScope.current !== transaction.scope) return;
    const resolve = waiter[transaction.phase];
    if (!resolve) return;
    waiter[transaction.phase] = undefined;
    resolve(accepted);
    if (transaction.phase === 'restore') historyPrependWaiters.current.delete(transaction.id);
  }, []);
  const onHistoryAnchorCaptured = useCallback((transaction: ConversationHistoryPrepend) => {
    resolveHistoryPrepend(transaction, true);
  }, [resolveHistoryPrepend]);
  const onHistoryAnchorRestored = useCallback((transaction: ConversationHistoryPrepend) => {
    resolveHistoryPrepend(transaction, true);
  }, [resolveHistoryPrepend]);
  useEffect(() => {
    const scope = selected?.id;
    for (const [transactionId, waiter] of historyPrependWaiters.current) {
      if (waiter.scope === scope) continue;
      waiter.capture?.(false);
      waiter.restore?.(false);
      historyPrependWaiters.current.delete(transactionId);
    }
    setHistoryPrepend(current => current?.scope === scope ? current : undefined);
  }, [selected?.id]);
  useEffect(() => () => {
    for (const waiter of historyPrependWaiters.current.values()) {
      waiter.capture?.(false);
      waiter.restore?.(false);
    }
    historyPrependWaiters.current.clear();
  }, []);
  const queuedMessagesStorageKey = workspace && selected
    ? host.queuedMessagesStorageKey(workspace.id, selected.id)
    : undefined;
  const createAnnotation = useCallback((anchorKind: 'CONVERSATION_TEXT' | 'WORKSPACE_FILE_RANGE', anchor: Record<string, unknown>) => {
    if (!selected && !conversationDraft) return;
    setComposerAnnotations(current => [...current, {
      id: randomId(), anchor_kind: anchorKind, anchor, comment: '',
    }]);
  }, [conversationDraft, selected]);
  const updateAnnotation = useCallback((annotation: AgentConversationAnnotation, comment: string) => {
    setComposerAnnotations(current => current.map(item => item.id === annotation.id
      ? { ...item, comment: comment.trim() }
      : item));
  }, []);
  const locateAnnotation = useCallback((annotation: AgentConversationAnnotation) => {
    if (annotation.anchor_kind === 'CONVERSATION_TEXT') {
      window.dispatchEvent(new CustomEvent('flowweave:locate-conversation-annotation', { detail: annotation }));
      return;
    }
    const file = annotationFileSelection(annotation);
    if (!file) return;
    // Always create a new selection identity: users may locate the same
    // annotation repeatedly, and each click must re-open/reveal its range.
    setFileSelectionReference({ path: file.path, selection: { ...file.selection } });
    setDrawerOpen(true);
  }, []);
  useEffect(() => {
    const listener = (event: Event) => void createAnnotation('CONVERSATION_TEXT', (event as CustomEvent<Record<string, unknown>>).detail);
    window.addEventListener('flowweave:create-conversation-annotation', listener);
    return () => window.removeEventListener('flowweave:create-conversation-annotation', listener);
  }, [createAnnotation]);
  useEffect(() => {
    const listener = (event: Event) => void createAnnotation('WORKSPACE_FILE_RANGE', (event as CustomEvent<Record<string, unknown>>).detail);
    window.addEventListener('flowweave:create-file-annotation', listener);
    return () => window.removeEventListener('flowweave:create-file-annotation', listener);
  }, [createAnnotation]);
  useEffect(() => {
    if (selected?.id) markSessionPerformance('selected');
  }, [selected?.id]);
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
  const markConversationUnread = useCallback((bindingId: string) => {
    updateUnreadConversationIds(current => current.has(bindingId) ? current : new Set([...current, bindingId]));
  }, [updateUnreadConversationIds]);
  const updatePinnedConversationIds = useCallback((update: (current: Set<string>) => Set<string>) => {
    setPinnedConversationIds(current => {
      const next = update(current);
      writePinnedConversationIds(pinnedStorageKey, next);
      return next;
    });
  }, [pinnedStorageKey]);
  const toggleConversationPin = useCallback((bindingId: string) => {
    updatePinnedConversationIds(current => {
      const next = new Set(current);
      if (next.has(bindingId)) next.delete(bindingId);
      else next.add(bindingId);
      return next;
    });
  }, [updatePinnedConversationIds]);
  useEffect(() => {
    activityBaseline.current = new Map();
    setUnreadConversationIds(readUnreadConversationIds(unreadStorageKey));
  }, [unreadStorageKey]);
  useEffect(() => {
    setPinnedConversationIds(readPinnedConversationIds(pinnedStorageKey));
  }, [pinnedStorageKey]);
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
    if (!conversationsQuery.data) return;
    const present = new Set(conversations.map(item => item.id));
    const next = new Set([...pinnedConversationIds].filter(bindingId => present.has(bindingId)));
    if (next.size === pinnedConversationIds.size) return;
    setPinnedConversationIds(next);
    writePinnedConversationIds(pinnedStorageKey, next);
  }, [conversations, conversationsQuery.data, pinnedConversationIds, pinnedStorageKey]);
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
    return capabilities;
  }, [capabilityCatalogQuery.data, composerCapabilityReferences]);
  const composerScope = selected?.id ?? conversationDraft?.id;
  const setComposerDraft = useCallback((value: string) => {
    composerDraftRef.current = value;
  }, []);
  const replaceComposerDraft = useCallback((value: string) => {
    composerDraftRef.current = value;
    composerRef.current?.replace(value);
    const hasText = Boolean(value.trim());
    setComposerHasText(current => current === hasText ? current : hasText);
  }, []);
  const onComposerContentPresenceChange = useCallback((hasText: boolean) => {
    setComposerHasText(current => current === hasText ? current : hasText);
  }, []);
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
    if (draftRecoveryStorageKey) writeConversationDraft(draftRecoveryStorageKey, undefined);
  }, [draftRecoveryStorageKey]);
  const persistComposerDraft = useCallback((scope: string | undefined, content = composerDraftRef.current) => {
    const active = scope === activeComposerScope.current;
    const storageKey = workspace && scope
      ? conversationComposerDraftStorageKey(host.id, workspace.id, scope)
      : undefined;
    if (active) composerDraftRef.current = content;
    if (storageKey) {
      writeConversationComposerDraft(storageKey, {
        content, attachments, references, workspaceReferences, annotations: composerAnnotations,
      });
    }
    if (!active) return;
    if (pendingBootstrap || bootstrapRecovery) {
      clearConversationDraft();
      return;
    }
    if (!conversationDraft || !draftRecoveryStorageKey) return;
    writeConversationDraft(draftRecoveryStorageKey, {
      draft: conversationDraft, content, attachments, references, workspaceReferences, annotations: composerAnnotations, providerId: newConversationProviderId,
      modelName: newConversationModelName, reasoningEffort: newConversationReasoningEffort,
    });
  }, [attachments, bootstrapRecovery, clearConversationDraft, composerAnnotations, conversationDraft, draftRecoveryStorageKey, host.id, newConversationModelName, newConversationProviderId, newConversationReasoningEffort, pendingBootstrap, references, workspace, workspaceReferences]);
  const connectedProviders = (providersQuery.data ?? []).filter(item => item.connection_state === 'CONNECTED' && item.models.some(model => model.enabled && model.is_default));
  const runtime = runtimeQuery.data;
  const runtimeWritable = Boolean(workspace && runtime?.write_available);
  const canOpenConversation = Boolean(workspace && (runtime?.write_available || runtime?.fork_available));
  const canBootstrap = Boolean(canOpenConversation && conversationDraft && (!features.modelSelection || (newConversationProviderId && newConversationModelName)));
  const localTurnGenerating = turnState === 'running' || turnState === 'pausing' || turnState === 'resuming';
  const eventQueryKey = sessionQueryKey(host, 'conversation-events', workspace?.id, selected?.id);
  const inputReadinessQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-input-readiness', workspace?.id, selected?.id),
    queryFn: () => api.inputReadiness(workspace!.id, selected!.id),
    // This is the formal OpenHands execution-state read used to restore an
    // in-flight turn after a browser reload. It is not persisted by FlowWeave.
    enabled: Boolean(workspace && selected),
    refetchInterval: query => {
      const needsFallback = conversationIsRunning(selected?.execution_status)
        || turnState === 'pausing'
        || queuedMessages.length > 0
        || localTurnGenerating
        || query.state.data?.ready === false;
      if (!pageVisible || !needsFallback) return false;
      return Math.min(2000 * 2 ** query.state.fetchFailureCount, 10_000);
    },
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const nativeExecutionStatus = inputReadinessQuery.data?.execution_status;
  const nativeTurnRunning = conversationIsRunning(nativeExecutionStatus);
  const nativeTurnTerminal = inputReadinessQuery.data?.ready === true
    && conversationHasReachedTerminalState(nativeExecutionStatus);
  // OpenHands owns the Conversation execution lifecycle. Local state may
  // bridge a command request, but it must never declare a native turn ended.
  const effectiveTurnState: TurnState = nativeTurnTerminal
    ? 'idle'
    : nativeExecutionStatus?.trim().toLowerCase() === 'paused'
      ? 'paused'
      : turnState === 'pausing' || turnState === 'resuming' || turnState === 'paused'
        ? turnState
      : nativeTurnRunning
      ? 'running'
        : turnState;
  // Delivery always enters the browser-local ledger before its one HTTP
  // attempt. When the native Conversation is already idle (including after a
  // formal error), that ledger entry is immediately eligible for dispatch; it
  // is not user-visible queueing. Keep actual running-turn queue entries and
  // every non-success outcome visible and actionable.
  const visibleQueuedMessages = queuedMessages.filter(message =>
    message.deliveryState !== 'dispatching'
    && (message.deliveryState === 'ambiguous'
      || message.deliveryState === 'rejected'
      || effectiveTurnState !== 'idle'
      || message.scope !== selected?.id),
  );
  const nativeGuidanceDispatching = queuedMessages.some(message =>
    message.nativeGuidance
    && message.deliveryState === 'dispatching'
    && message.scope === selected?.id,
  );
  const isGenerating = effectiveTurnState === 'running' || effectiveTurnState === 'pausing' || effectiveTurnState === 'resuming';
  const streamEnabled = Boolean(
    selected
    && (runtime?.write_available || selected.write_available)
    && (isGenerating || streamHold?.bindingId === selected.id),
  );
  const eventsQuery = useQuery<OpenHandsConversationEventBatch>({
    queryKey: eventQueryKey,
    queryFn: async () => {
      const latest = await api.conversationEvents(workspace!.id, selected!.id);
      const current = queryClient.getQueryData<OpenHandsConversationEventBatch>(eventQueryKey);
      // The OpenHands latest-page cursor is a bounded branch projection, not
      // an instruction to clear already-rendered activity. Keep identities we
      // have read during this turn and let the newest REST payload refresh the
      // matching events' persisted fields.
      return current
        ? { ...current, ...latest, events: mergeConversationEvents(current.events, latest.events), history_cursor: current.history_cursor ?? latest.history_cursor }
        : latest;
    },
    // Native event reads begin at the current leaf. The resulting bounded
    // latest page is rendered before older pages are prefetched below.
    enabled: Boolean(workspace && selected),
    refetchInterval: query => {
      const selectedMayBeActive = conversationIsRunning(selected?.execution_status)
        || query.state.data?.result?.status === 'RUNNING'
        || Boolean(latestUnfinishedUserEventId(query.state.data?.events ?? []));
      return pageVisible && selectedMayBeActive ? ACTIVE_EVENT_RECOVERY_INTERVAL_MS : false;
    },
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  useEffect(() => {
    if (selected && eventsQuery.data) markSessionPerformance('events-ready');
  }, [eventsQuery.data, selected]);
  const synchronizeConversationEvents = useCallback(async (preferLatest = false) => {
    if (!workspace || !selected) return;
    const current = queryClient.getQueryData<OpenHandsConversationEventBatch>(eventQueryKey);
    const cursor = preferLatest ? undefined : current?.next_cursor ?? undefined;
    try {
      const incoming = await api.conversationEvents(workspace.id, selected.id, cursor);
      queryClient.setQueryData<OpenHandsConversationEventBatch>(eventQueryKey, existing => existing
        ? (() => {
            // A stream frame or another reconciliation may have advanced the
            // cursor while this read was in flight. Never move that progress
            // backwards merely because this older request completed later.
            const cursorUnchanged = existing.next_cursor === cursor;
            return {
              ...existing,
              ...incoming,
              events: mergeConversationEvents(existing.events, incoming.events),
              next_cursor: cursor
                ? (cursorUnchanged ? (incoming.next_cursor ?? cursor) : existing.next_cursor)
                : (incoming.next_cursor ?? existing.next_cursor),
              history_cursor: existing.history_cursor ?? incoming.history_cursor,
            };
          })()
        : incoming,
      );
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-context', workspace.id, selected.id) });
    } catch {
      // The next scheduled cursor reconciliation is sufficient. A transient
      // read failure must not clear already-rendered native events.
    }
  }, [api, eventQueryKey, host, queryClient, selected, workspace]);
  // Let the latest native window paint before background history starts. This
  // makes the first visual state deterministic. History is a read-only
  // native projection and must keep loading while the current turn runs.
  const historyPrefetchDelayMs = 100;
  const loadAllHistory = useCallback(async () => {
    if (!workspace || !selected || !eventsQuery.data?.history_cursor) return;
    const scope = selected.id;
    const scopeIsActive = () => activeHistoryScope.current === scope;
    if (historyLoadingScopes.current.has(scope)) return;
    historyLoadingScopes.current.add(scope);
    setHistoryLoadingBindingId(scope);
    let historyCursor: string | null | undefined = eventsQuery.data.history_cursor;
    if (historyFailedCursors.current.get(scope) !== historyCursor) historyFailedCursors.current.delete(scope);
    const seenHistoryCursors = new Set<string>();
    try {
      // The first response is the native latest page. Prepend older pages only
      // after it has been positioned, yielding between pages so a long branch
      // never delays the initial conversation view.
      while (historyCursor) {
        const cursor = historyCursor;
        if (seenHistoryCursors.has(cursor)) throw new Error('读取更早会话记录时，历史分页游标没有推进。');
        seenHistoryCursors.add(cursor);
        const older = await api.conversationEvents(workspace.id, scope, undefined, cursor);
        if (!scopeIsActive()) return;
        // This transaction captures the actual viewport immediately before
        // this exact page is inserted, then restores only after React has
        // rendered the matching page. It cannot leak into another session.
        const transaction: ConversationHistoryPrepend = {
          id: ++nextHistoryPrependId.current,
          scope,
          phase: 'capture',
        };
        const captured = new Promise<boolean>(resolve => {
          historyPrependWaiters.current.set(transaction.id, { scope, capture: resolve });
        });
        setHistoryPrepend(transaction);
        if (!await captured || !scopeIsActive()) return;
        queryClient.setQueryData<OpenHandsConversationEventBatch>(eventQueryKey, current => current
          ? { ...current, events: mergeConversationEvents(older.events, current.events), history_cursor: older.history_cursor }
          : older,
        );
        const restored = new Promise<boolean>(resolve => {
          const waiter = historyPrependWaiters.current.get(transaction.id);
          if (waiter) waiter.restore = resolve;
          else resolve(false);
        });
        setHistoryPrepend({ ...transaction, phase: 'restore' });
        if (!await restored || !scopeIsActive()) return;
        setHistoryPrepend(current => current?.id === transaction.id ? undefined : current);
        historyCursor = older.history_cursor;
      }
    } catch (error) {
      if (historyCursor) historyFailedCursors.current.set(scope, historyCursor);
      reportOperationError(scope, error instanceof Error ? error : new Error('读取更早会话记录失败'));
    } finally {
      historyLoadingScopes.current.delete(scope);
      setHistoryLoadingBindingId(current => current === scope ? undefined : current);
    }
  }, [api, eventQueryKey, eventsQuery.data?.history_cursor, queryClient, reportOperationError, selected, workspace]);
  useEffect(() => {
    const bindingId = selected?.id;
    const historyCursor = eventsQuery.data?.history_cursor;
    // A refreshed running conversation must recover its complete native
    // history too. Prepending pages preserves the reader viewport, so this
    // read-only pagination can proceed independently of live event recovery.
    if (!bindingId || !historyCursor || historyLoadingScopes.current.has(bindingId) || historyFailedCursors.current.get(bindingId) === historyCursor) return;
    const timer = window.setTimeout(() => { void loadAllHistory(); }, historyPrefetchDelayMs);
    return () => window.clearTimeout(timer);
  }, [eventsQuery.data?.history_cursor, historyPrefetchDelayMs, loadAllHistory, selected?.id]);
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
  useEffect(() => {
    if (!conversationSearchTargetEventId || !selected) return;
    const target = Array.from(document.querySelectorAll<HTMLElement>('[data-conversation-event-id]')).find(
      item => item.dataset.conversationEventId === conversationSearchTargetEventId,
    );
    const surface = target?.closest<HTMLElement>('.conversation-surface');
    if (!target || !surface) return;
    const top = target.getBoundingClientRect().top - surface.getBoundingClientRect().top + surface.scrollTop - 28;
    surface.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    target.classList.add('conversation-search-target');
    const timer = window.setTimeout(() => target.classList.remove('conversation-search-target'), 2_600);
    setConversationSearchTargetEventId(undefined);
    return () => window.clearTimeout(timer);
  }, [conversationSearchTargetEventId, displayedEvents, selected]);
  const activeNativeTurnId = activeTurnEventId ?? latestUnfinishedUserEventId(displayedEvents);
  const hasUnfinishedFormalTurn = Boolean(latestUnfinishedUserEventId(displayedEvents));
  const terminalSyncTurnKey = selected && nativeTurnTerminal
    ? (() => {
        const userEventId = latestUnfinishedUserEventId(displayedEvents);
        return userEventId ? `${selected.id}:${userEventId}` : undefined;
      })()
    : undefined;
  const terminalSyncExpired = terminalSyncTurnKey === expiredTerminalSyncTurnKey;
  const latestFormalUserEventId = [...displayedEvents].reverse().find(event => event.event_type === 'MESSAGE'
    && ['user', 'human'].includes(String(event.payload.source ?? '').toLowerCase()))?.id;
  // A durable OpenHands ERROR/Finish/assistant event is also authoritative
  // while the exact readiness request is still loading or reconnecting.
  // This is not a browser-side lifecycle guess: it is the native event tree.
  const latestFormalTurnFinished = Boolean(
    latestFormalUserEventId && hasFinishedTurn(displayedEvents, latestFormalUserEventId),
  );
  const finalReplyAwaitingNativeCompletion = nativeTurnRunning
    && Boolean(activeNativeTurnId && hasAssistantReplyForTurn(displayedEvents, activeNativeTurnId));
  // All conversation controls consume this one projection. OpenHands
  // readiness is the lifecycle authority; its formal event tree is the
  // completion evidence. A terminal readiness result can arrive one request
  // before the corresponding terminal event page, so keep the UI visibly
  // synchronizing instead of letting separate controls disagree about idle.
  const conversationActivity = useMemo(() => {
    const synchronizing = nativeTurnTerminal && hasUnfinishedFormalTurn && !terminalSyncExpired;
    const state: ConversationActivityState = synchronizing ? 'synchronizing' : effectiveTurnState;
    return {
      state,
      active: state === 'running' || state === 'pausing' || state === 'resuming' || state === 'synchronizing',
      synchronizing,
    };
  }, [effectiveTurnState, hasUnfinishedFormalTurn, nativeTurnTerminal, terminalSyncExpired]);
  const latestDisplayedEvent = displayedEvents.at(-1);
  const emptyResponseRecoveryActive = conversationActivity.state === 'running'
    && Boolean(latestDisplayedEvent && isOpenHandsEmptyResponseRecovery(latestDisplayedEvent));
  useEffect(() => {
    // The OpenHands readiness read can briefly (or after a missed stream
    // transition, persistently) report idle before it has observed the
    // descendant TaskAction. A formal unfinished user turn and its native
    // cursor prove there is still an active branch to reconcile. Keep this
    // read-only recovery alive without changing the displayed execution state.
    const recoverUnfinishedTurn = hasUnfinishedFormalTurn
      && !latestFormalTurnFinished
      && !terminalSyncExpired;
    if (!workspace || !selected || !(isGenerating || recoverUnfinishedTurn) || !pageVisible) return;
    let cancelled = false;
    let timer: number | undefined;
    const recover = async () => {
      if (cancelled) return;
      // A live WebSocket can be silently wedged. When no cursor is available,
      // read the bounded latest page instead of waiting for Pause/Resume to
      // accidentally force a full UI refresh.
      await synchronizeConversationEvents();
      if (!cancelled) timer = window.setTimeout(() => { void recover(); }, ACTIVE_EVENT_RECOVERY_INTERVAL_MS);
    };
    timer = window.setTimeout(() => { void recover(); }, ACTIVE_EVENT_RECOVERY_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [eventsQuery.data?.next_cursor, hasUnfinishedFormalTurn, isGenerating, latestFormalTurnFinished, pageVisible, selected, synchronizeConversationEvents, terminalSyncExpired, workspace]);
  const messageAnnotations = useMemo(() => displayedEvents.flatMap(event => {
    const raw = event.payload.collaboration_annotations;
    return Array.isArray(raw) ? raw.filter((item): item is AgentConversationAnnotation => Boolean(
      item
      && typeof item === 'object'
      && typeof (item as AgentConversationAnnotation).id === 'string'
      && typeof (item as AgentConversationAnnotation).anchor_kind === 'string'
      && typeof (item as AgentConversationAnnotation).anchor === 'object'
      && typeof (item as AgentConversationAnnotation).comment === 'string',
    )) : [];
  }), [displayedEvents]);
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
    enabled: Boolean(workspace && selected),
  });
  const canWrite = Boolean(selected?.write_available);
  // A completed FlowRun keeps its source node Conversation read-only, but it
  // may create the same native Fork available in an ordinary Agent session.
  // The host owns this narrow capability flag; it does not make the composer
  // or any other session mutation writable again.
  const canFork = Boolean(selected && features.fork && (canWrite || runtime?.fork_available));
  const canCompose = Boolean(canWrite || (canOpenConversation && conversationDraft));
  const selectedConversationRunning = conversationActivity.active;
  const sessionStopped = conversationActivity.state === 'pausing' || conversationActivity.state === 'paused'
    || nativeExecutionStatus?.toLowerCase() === 'paused';
  const confirmationQuery = useQuery({
    queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace?.id, selected?.id),
    queryFn: () => api.pendingConfirmation(workspace!.id, selected!.id),
    enabled: Boolean(workspace && selected && canWrite && features.confirmations),
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  });
  const pendingConfirmation = confirmationQuery.data?.pending ? confirmationQuery.data : undefined;
  const refresh = useCallback(() => {
    if (!workspace) return;
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'runtime', workspace.id) });
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
    if (selected?.id) {
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-events', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-input-readiness', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace.id, selected.id) });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-context', workspace.id, selected.id) });
    }
  }, [host, queryClient, selected?.id, workspace]);
  const reconcileConversationProjection = useCallback(() => {
    void synchronizeConversationEvents(true);
    if (!workspace || !selected) return;
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-input-readiness', workspace.id, selected.id) });
    void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversation-confirmation', workspace.id, selected.id) });
  }, [host, queryClient, selected, synchronizeConversationEvents, workspace]);
  const clearLiveText = useCallback(() => {
    setLiveText('');
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
    if (liveEventsFrame.current !== undefined) window.cancelAnimationFrame(liveEventsFrame.current);
  }, []);
  const commitQueuedMessages = useCallback((update: (current: QueuedMessage[]) => QueuedMessage[]) => {
    const next = update(queuedMessagesRef.current);
    queuedMessagesRef.current = next;
    // Persist before a caller may start the corresponding HTTP request.
    writeQueuedMessages(queuedMessagesStorageKeyRef.current, next);
    setQueuedMessages(next);
  }, []);
  const updateQueuedMessage = useCallback((id: string, update: (message: QueuedMessage) => QueuedMessage) => {
    commitQueuedMessages(current => current.map(message => message.id === id ? update(message) : message));
  }, [commitQueuedMessages]);
  const onStreamEvent = useCallback((event: AgentStreamEvent) => {
    // The upstream stream remains necessary for timely process/event delivery,
    // but text deltas are transient and must never appear as a partial final
    // reply. Final answer content is rendered only from formal OpenHands events.
    if (event.type === 'stream_closed') reconcileConversationProjection();
    if (event.type === 'event' && event.event) appendLiveEvent(event.event);
    // Completion frames do not identify the originating user event.  A stale
    // frame must never complete a newer turn; durable assistant/error events
    // associated with activeTurnEventId are the authoritative terminal signal.
    if (event.type === 'message_complete') { clearLiveText(); reconcileConversationProjection(); }
  }, [appendLiveEvent, clearLiveText, reconcileConversationProjection]);
  const onStreamReconnect = useCallback(() => {
    // A WebSocket is a live projection only. Events written while the browser
    // was disconnected are recovered from the authoritative REST feed after
    // the socket is live again; only formal event projections are retained.
    reconcileConversationProjection();
  }, [reconcileConversationProjection]);
  useEffect(() => {
    if (!streamEnabled) setStreamStatus('disabled');
  }, [streamEnabled]);

  useEffect(() => {
    if (!conversationDraft && !selectedBindingId && conversations.length) onNavigate(host.conversationPath(conversations[0].id), true);
    if (selectedBindingId && conversations.length && !selected && pendingCreatedId !== selectedBindingId && !conversationsQuery.isFetching) onNavigate(host.rootPath, true);
  }, [conversationDraft, conversations, conversationsQuery.isFetching, host, onNavigate, pendingCreatedId, selected, selectedBindingId]);
  useEffect(() => { if (selected?.id === pendingCreatedId) setPendingCreatedId(undefined); }, [pendingCreatedId, selected?.id]);
  useLayoutEffect(() => {
    if (previousComposerScope.current === composerScope) return;
    const previousScope = previousComposerScope.current;
    if (workspace && previousScope) {
      // Commit the outgoing scope before reading the incoming one. The keyed
      // Composer cleanup runs in the same layout phase, and reading first can
      // otherwise lose an attachment during a fast A -> B -> A switch.
      persistComposerDraft(previousScope, composerDraftRef.current);
    }
    previousComposerScope.current = composerScope;
    const recoveredComposer = workspace && composerScope && selected
      ? readConversationComposerDraft(conversationComposerDraftStorageKey(host.id, workspace.id, composerScope))
      : undefined;
    if (bootstrapTransitionScope.current === composerScope) {
      bootstrapTransitionScope.current = undefined;
      return;
    }
    setEditing(false); setQueuedMessageMenuId(undefined); clearLiveText(); pendingLiveEvents.current = []; if (liveEventsFrame.current !== undefined) window.cancelAnimationFrame(liveEventsFrame.current); liveEventsFrame.current = undefined; setLiveEvents([]); setHiddenEventIds(new Set()); setActiveTurnEventId(undefined); setExpiredTerminalSyncTurnKey(undefined); setRequestStartedAt(undefined); setConfirmationReason(''); setTurnState('idle'); queuedMessagesRef.current = []; nativeGuidanceOptimisticEventIds.current.clear(); setQueuedMessages([]); setPendingRewrite(undefined);
    if (recoveredComposer) {
      replaceComposerDraft(recoveredComposer.content);
      setAttachments(recoveredComposer.attachments);
      setReferences(recoveredComposer.references);
      setWorkspaceReferences(recoveredComposer.workspaceReferences);
      setComposerAnnotations(recoveredComposer.annotations);
    } else if (!conversationDraft) {
      replaceComposerDraft(''); setAttachments([]); setReferences([]); setWorkspaceReferences([]); setComposerAnnotations([]);
    }
    setOperationError(undefined);
  }, [clearLiveText, composerScope, conversationDraft, host.id, persistComposerDraft, replaceComposerDraft, selected, workspace]);
  useEffect(() => {
    if (composerScope) persistComposerDraft(composerScope);
  }, [composerScope, persistComposerDraft]);
  useEffect(() => {
    if (!queuedMessagesStorageKey || queuedMessagesStorageKeyRef.current !== queuedMessagesStorageKey) return;
    writeQueuedMessages(queuedMessagesStorageKey, queuedMessages);
  }, [queuedMessages, queuedMessagesStorageKey]);
  useEffect(() => {
    if (!queuedMessagesStorageKey) return;
    // A request that was in flight while the page closed has no formal
    // OpenHands correlation field. Treat it as unresolved, never as safe to
    // repeat; the user can refresh the transcript and decide explicitly.
    queuedMessagesStorageKeyRef.current = queuedMessagesStorageKey;
    const restored: QueuedMessage[] = readQueuedMessages(queuedMessagesStorageKey).map(message =>
      message.deliveryState === 'dispatching'
        ? { ...message, deliveryState: 'ambiguous' as const, deliveryError: '页面在等待 OpenHands 确认时关闭；发送结果不确定，请刷新会话确认。' }
        : message,
    );
    queuedMessagesRef.current = restored;
    writeQueuedMessages(queuedMessagesStorageKey, restored);
    setQueuedMessages(restored);
  }, [queuedMessagesStorageKey]);
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
    if (turnState === 'pausing' && nativeExecutionStatus?.toLowerCase() === 'paused') {
      setTurnState('paused');
    }
  }, [nativeExecutionStatus, turnState]);
  useEffect(() => {
    if (!selected || inputReadinessQuery.data?.execution_status?.toLowerCase() !== 'paused') return;
    setTurnState(current => current === 'idle' ? 'paused' : current);
  }, [inputReadinessQuery.data?.execution_status, selected]);
  useEffect(() => {
    if (!terminalSyncTurnKey) {
      setExpiredTerminalSyncTurnKey(undefined);
      return;
    }
    if (terminalSyncExpired) return;

    const [bindingId] = terminalSyncTurnKey.split(':', 1);
    const timer = window.setTimeout(() => {
      // Readiness is authoritative for whether OpenHands accepts new input,
      // but no browser state may fabricate the missing formal result.
      setExpiredTerminalSyncTurnKey(terminalSyncTurnKey);
      setActiveTurnEventId(undefined);
      setRequestStartedAt(undefined);
      clearLiveText();
      setStreamHold({ bindingId, expiresAt: Date.now() + STREAM_IDLE_GRACE_MS });
      setTurnState('idle');
      commitQueuedMessages(messages => messages.map(message => (
        message.scope === bindingId && message.deliveryState === 'queued'
          ? {
              ...message,
              deliveryState: 'ambiguous' as const,
              deliveryError: '会话同步结束前未自动发送；发送结果不确定，请刷新会话确认后重新编辑。',
            }
          : message
      )));
      refresh();
    }, TERMINAL_EVENT_RECONCILIATION_MS);
    return () => window.clearTimeout(timer);
  }, [clearLiveText, commitQueuedMessages, refresh, terminalSyncExpired, terminalSyncTurnKey]);
  useEffect(() => {
    if (!selected || !latestFormalTurnFinished) return;

    // The latest formal user turn has reached an OpenHands terminal event.
    // It therefore cannot be revived by an older browser-local turn bridge.
    clearLiveText();
    setActiveTurnEventId(undefined);
    setRequestStartedAt(undefined);
    setTurnState(current => current === 'running' || current === 'resuming' ? 'idle' : current);
    setStreamHold({ bindingId: selected.id, expiresAt: Date.now() + STREAM_IDLE_GRACE_MS });
  }, [clearLiveText, displayedEvents, latestFormalTurnFinished, selected]);
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
    if (nativeTurnTerminal && (turnState === 'running' || turnState === 'resuming') && activeTurnEventId && hasFinishedTurn(displayedEvents, activeTurnEventId)) {
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
  }, [activeTurnEventId, clearLiveText, displayedEvents, nativeTurnTerminal, refresh, selected?.id, turnState]);
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
    message.annotations,
  ), onSuccess: (value, message) => {
    if (!workspace) return;
    const conversation = value.conversation;
    // Keep the freshly created binding in both projections before changing the
    // URL. Without this handoff, clearing the draft makes the workbench render
    // its empty state for one frame while the route-specific GET is still in
    // flight, which reads as a page flash immediately after the first send.
    queryClient.setQueryData<AgentConversation>(
      sessionQueryKey(host, 'conversation', workspace.id, conversation.id),
      conversation,
    );
    queryClient.setQueryData<InfiniteData<AgentConversationPage>>(
      sessionQueryKey(host, 'conversations', workspace.id),
      current => {
        if (!current?.pages.length) return current;
        const [firstPage, ...remainingPages] = current.pages;
        const exists = current.pages.some(page => page.items.some(item => item.id === conversation.id));
        return {
          ...current,
          pages: [
            {
              ...firstPage,
              items: exists
                ? firstPage.items.map(item => item.id === conversation.id ? conversation : item)
                : [conversation, ...firstPage.items],
            },
            ...remainingPages,
          ],
        };
      },
    );
    setWorkspaceScopeMigration(message.scope);
    setPendingCreatedId(conversation.id);
    bootstrapTransitionScope.current = conversation.id;
    clearConversationComposerDraft(host.id, workspace.id, conversation.id);
    // The first message has now been accepted by the server. Clear the
    // in-memory composer before switching from the draft scope to the new
    // conversation scope, otherwise the keyed Composer mounts with the sent
    // message as its initial draft during the route handoff.
    replaceComposerDraft('');
    setAttachments([]);
    setReferences([]);
    setWorkspaceReferences([]);
    setComposerAnnotations([]);
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
        replaceComposerDraft(message.content);
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
      replaceComposerDraft(message.content);
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
  const synchronizeConversationOrder = useCallback((bindingId: string, orderedBindingIds: string[]) => {
    if (!workspace || !api.reorderConversation) return;
    setConversationOrderSync(current => ({ ...current, [bindingId]: { state: 'syncing', orderedBindingIds } }));
    void api.reorderConversation(workspace.id, bindingId, orderedBindingIds)
      .then(() => setConversationOrderSync(current => {
        const next = { ...current };
        delete next[bindingId];
        return next;
      }))
      .catch(() => setConversationOrderSync(current => ({
        ...current,
        [bindingId]: { state: 'failed', orderedBindingIds },
      })));
  }, [api, workspace]);
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
  const showOptimisticUserBubble = useCallback((message: QueuedMessage, deliveryStatus?: string): string => {
    const existing = nativeGuidanceOptimisticEventIds.current.get(message.id);
    if (existing) return existing;
    const optimisticEventId = `pending-user:${randomId()}`;
    nativeGuidanceOptimisticEventIds.current.set(message.id, optimisticEventId);
    setLiveEvents(current => mergeConversationEvents(current, [{
      id: optimisticEventId,
      event_type: 'MESSAGE',
      payload: {
        source: 'user',
        content: message.content,
        attachments: message.items,
        conversation_references: message.references.map(item => ({ event_id: item.eventId, content: item.content })),
        workspace_references: message.workspaceReferences,
        collaboration_annotations: message.annotations,
        ...(deliveryStatus ? { _flowweave_delivery_status: deliveryStatus } : {}),
      },
    }]));
    return optimisticEventId;
  }, []);
  const send = useMutation({
    mutationFn: (message: BoundQueuedMessage) => api.sendMessage(workspace!.id, message.bindingId, message.content, message.items, message.references.map(item => ({ event_id: item.eventId, content: item.content })), message.workspaceReferences ?? [], message.annotations, message.id),
    onMutate: message => {
      const optimisticEventId = showOptimisticUserBubble(
        message,
        message.nativeGuidance ? '正在追加到当前回复' : undefined,
      );
      if (message.nativeGuidance) {
        // The user has asked to append direction to the active turn, so show
        // that intent immediately. It remains explicitly provisional until
        // the append request returns a formal OpenHands cursor.
        return { optimisticEventId, nativeGuidance: true };
      }
      clearLiveText();
      setActiveTurnEventId(undefined);
      setRequestStartedAt(Date.now());
      setLiveEvents(current => mergeConversationEvents(current, [{
        id: optimisticEventId,
        event_type: 'MESSAGE',
        payload: {
          source: 'user',
          content: message.content,
          attachments: message.items,
          conversation_references: message.references.map(item => ({ event_id: item.eventId, content: item.content })),
          workspace_references: message.workspaceReferences,
          collaboration_annotations: message.annotations,
        },
      }]));
      setTurnState('running');
      return { optimisticEventId, nativeGuidance: false };
    },
    onSuccess: (value, message, context) => {
      nativeGuidanceOptimisticEventIds.current.delete(message.id);
      const cursor = value.cursor;
      if (cursor) {
        if (!context?.nativeGuidance) setActiveTurnEventId(cursor);
        setLiveEvents(current => mergeConversationEvents(
          current.filter(event => event.id !== context?.optimisticEventId),
          [{ id: cursor, event_type: 'MESSAGE', payload: { source: 'user', content: message.content, attachments: message.items, conversation_references: message.references.map(item => ({ event_id: item.eventId, content: item.content })), workspace_references: message.workspaceReferences, collaboration_annotations: message.annotations } }],
        ));
      }
      if (value.accepted && cursor) {
        commitQueuedMessages(current => current.filter(item => item.id !== message.id));
      } else {
        updateQueuedMessage(message.id, item => ({
          ...item,
          deliveryState: 'ambiguous',
          deliveryError: '服务未返回可验证的 OpenHands 事件 ID；发送结果不确定，请刷新会话确认。',
        }));
      }
      if (!context?.nativeGuidance) { setAttachments([]); setComposerAnnotations([]); }
      refresh();
    },
    onError: (error, message, context) => {
      nativeGuidanceOptimisticEventIds.current.delete(message.id);
      if (error instanceof ApiError && error.code === 'AGENT_CONVERSATION_BUSY') {
        updateQueuedMessage(message.id, item => ({ ...item, deliveryState: 'queued', nativeGuidance: false, deliveryError: undefined }));
      } else if (isAmbiguousDelivery(error)) {
        updateQueuedMessage(message.id, item => ({
          ...item,
          deliveryState: 'ambiguous',
          deliveryError: '发送结果不确定，请先刷新会话确认；系统不会自动重新发送。',
        }));
      } else {
        updateQueuedMessage(message.id, item => ({
          ...item,
          deliveryState: 'rejected',
          deliveryError: error instanceof Error ? error.message : '消息被服务器拒绝。',
        }));
      }
      if (context?.nativeGuidance) {
        setLiveEvents(current => current.filter(event => event.id !== context.optimisticEventId));
        reportOperationError(message.bindingId, error);
        return;
      }
      if (error instanceof ApiError && error.code === 'AGENT_CONVERSATION_BUSY') {
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
      const nextScopeMessage: QueuedMessage = {
        ...message,
        scope: value.id,
        deliveryState: 'queued',
        nativeGuidance: false,
        deliveryError: undefined,
      };
      const nextStorageKey = host.queuedMessagesStorageKey(workspace.id, value.id);
      const existing = readQueuedMessages(nextStorageKey).filter(item => item.id !== message.id);
      writeQueuedMessages(nextStorageKey, [...existing, nextScopeMessage]);
      commitQueuedMessages(current => current.filter(item => item.id !== message.id));
      setPendingCreatedId(value.id);
      setPendingMigratedSend({ ...nextScopeMessage, bindingId: value.id });
      void queryClient.invalidateQueries({ queryKey: sessionQueryKey(host, 'conversations', workspace.id) });
      onNavigate(host.conversationPath(value.id));
    },
    onError: (error, message) => {
      updateQueuedMessage(message.id, item => ({
        ...item,
        deliveryState: isAmbiguousDelivery(error) ? 'ambiguous' : 'rejected',
        deliveryError: isAmbiguousDelivery(error)
          ? '会话迁移结果不确定，请刷新会话确认；系统不会自动重新发送。'
          : error instanceof Error ? error.message : '会话迁移被服务器拒绝。',
      }));
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
  const interrupt = useMutation({ mutationFn: () => api.interruptConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('pausing'), onSuccess: () => { reconcileConversationProjection(); onHostStateChanged?.(); }, onError: error => {
    setTurnState('running');
    reportOperationError(selected?.id, error);
  } });
  const resume = useMutation({ mutationFn: () => api.resumeConversation(workspace!.id, selected!.id), onMutate: () => setTurnState('resuming'), onSuccess: value => { if (value.cursor) setActiveTurnEventId(value.cursor); setTurnState('running'); reconcileConversationProjection(); onHostStateChanged?.(); }, onError: error => { setTurnState('paused'); reportOperationError(selected?.id, error); } });
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
    mutationFn: ({ eventId, content, attachments, references, workspaceReferences, annotations }: RewriteRequest) => api.rerunMessage(
      workspace!.id, selected!.id, eventId, content, attachments, references, workspaceReferences, annotations,
    ),
    onMutate: request => {
      const optimisticEventId = `pending-rewrite:${randomId()}`;
      const branch = eventBranchIds(displayedEvents, request.eventId);
      const replacementParentId = displayedEvents.find(event => event.id === request.eventId)?.payload.parent_id;
      commitQueuedMessages(() => []);
      clearLiveText();
      setHiddenEventIds(current => new Set([...current, ...branch]));
      setLiveEvents([{ id: optimisticEventId, event_type: 'MESSAGE', payload: {
        source: 'user', content: request.content, parent_id: replacementParentId,
        attachments: request.attachments,
        conversation_references: request.references,
        workspace_references: request.workspaceReferences,
        collaboration_annotations: request.annotations,
      } }]);
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
          [{ id: cursor, event_type: 'MESSAGE', payload: {
            source: 'user', content: request.content, parent_id: context?.replacementParentId,
            attachments: request.attachments,
            conversation_references: request.references,
            workspace_references: request.workspaceReferences,
            collaboration_annotations: request.annotations,
          } }],
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
    updateQueuedMessage(message.id, item => ({ ...item, deliveryState: 'dispatching' }));
    send.mutate(message);
  }, [pendingMigratedSend, selected?.id, send, updateQueuedMessage]);
  useEffect(() => {
    if (turnState !== 'pausing' || !inputReadinessQuery.data?.ready) return;
    if (pendingRewrite) {
      const request = pendingRewrite;
      setPendingRewrite(undefined);
      rewrite.mutate(request);
    } else setTurnState(nativeTurnTerminal ? 'idle' : 'paused');
  }, [inputReadinessQuery.data?.ready, nativeTurnTerminal, pendingRewrite, rewrite, turnState]);
  const requestRewrite = useCallback((eventId: string, content: string) => {
    const original = displayedEvents.find(event => event.id === eventId);
    const request: RewriteRequest = {
      eventId,
      content,
      attachments: original?.payload.attachments,
      references: original?.payload.conversation_references,
      workspaceReferences: original?.payload.workspace_references,
      annotations: original?.payload.collaboration_annotations,
    };
    if (effectiveTurnState === 'running') {
      setPendingRewrite(request);
      interrupt.mutate();
      return;
    }
    if (effectiveTurnState === 'pausing') {
      setPendingRewrite(request);
      return;
    }
    if (effectiveTurnState === 'idle' || effectiveTurnState === 'paused') rewrite.mutate(request);
  }, [displayedEvents, effectiveTurnState, interrupt, rewrite]);
  const openConversationDraft = useCallback((next: Omit<ConversationDraft, 'id'>) => {
    clearBootstrapRecovery();
    const recovery = workspace
      ? readConversationDraft(conversationDraftStorageKey(host.id, workspace.id, next.workDirectoryId))
      : undefined;
    if (recovery) {
      setConversationDraft(recovery.draft);
      replaceComposerDraft(recovery.content);
      setAttachments(recovery.attachments);
      setReferences(recovery.references);
      setWorkspaceReferences(recovery.workspaceReferences ?? []);
      setComposerAnnotations(recovery.annotations);
      setNewConversationProviderId(recovery.providerId);
      setNewConversationModelName(recovery.modelName);
      setNewConversationReasoningEffort(recovery.reasoningEffort);
    } else {
      setConversationDraft({ ...next, id: randomId(), capabilityVersionIds: next.capabilityVersionIds ?? [] });
      replaceComposerDraft('');
      setAttachments([]);
      setReferences([]); setWorkspaceReferences([]);
      setComposerAnnotations([]);
    }
    setPendingBootstrap(undefined);
    setWorkspaceScopeMigration(undefined);
    clearLiveText();
    setLiveEvents([]);
    setOptimisticBootstrapTurn(undefined);
    setHiddenEventIds(new Set());
    setTurnState('idle');
    onNavigate(host.rootPath);
  }, [clearBootstrapRecovery, clearLiveText, host.id, host.rootPath, onNavigate, replaceComposerDraft, workspace]);
  useEffect(() => {
    if (!autoOpenDraft || !workspace || selectedBindingId || conversationDraft) return;
    openConversationDraft({ displayName: '根工作区' });
  }, [autoOpenDraft, conversationDraft, openConversationDraft, selectedBindingId, workspace]);
  const enqueueDraft = useCallback((draftContent = composerDraftRef.current) => {
    const content = draftContent.trim();
    if ((!content && !attachments.length && !references.length && !workspaceReferences.length && !composerAnnotations.length) || migrateStreaming.isPending || pendingMigratedSend || effectiveTurnState === 'pausing' || effectiveTurnState === 'resuming') return;
    replaceComposerDraft('');
    setOperationError(undefined);
    if (!composerScope) return;
    if (workspace) clearConversationComposerDraft(host.id, workspace.id, composerScope);
    const message = { id: randomId(), scope: composerScope, content, items: attachments, references, workspaceReferences, annotations: composerAnnotations };
    setAttachments([]);
    setReferences([]); setWorkspaceReferences([]); setComposerAnnotations([]);
    if (conversationDraft) {
      if (canBootstrap && !bootstrap.isPending) {
        setPendingBootstrap({ draft: conversationDraft, message });
        setOptimisticBootstrapTurn({
          scope: conversationDraft.id,
          event: { id: `pending-bootstrap:${message.id}`, event_type: 'MESSAGE', payload: { source: 'user', content, attachments, conversation_references: references.map(item => ({ event_id: item.eventId, content: item.content })), workspace_references: workspaceReferences, collaboration_annotations: composerAnnotations } },
        });
        setRequestStartedAt(Date.now());
        setTurnState('running');
        bootstrap.mutate(message);
      }
      else { replaceComposerDraft(content); setAttachments(attachments); setReferences(references); setWorkspaceReferences(workspaceReferences); setComposerAnnotations(composerAnnotations); }
      return;
    }
    if (!canWrite) { replaceComposerDraft(content); setAttachments(attachments); setReferences(references); setWorkspaceReferences(workspaceReferences); setComposerAnnotations(composerAnnotations); return; }
    if (!selected) return;
    // Enter is the safe/default action while OpenHands is running: keep the
    // message in the browser queue and let the next native idle boundary
    // dispatch it. Only Command/Ctrl+Enter opts into native running-turn
    // append (see sendDraftDirectly below).
    const shouldQueue = effectiveTurnState === 'running'
      || (effectiveTurnState === 'idle' && !selected.streaming_callback_ready);
    const queuedMessage: QueuedMessage = {
      ...message,
      nativeGuidance: false,
      deliveryState: shouldQueue ? 'queued' : 'dispatching',
      createdAt: Date.now(),
    };
    commitQueuedMessages(items => [...items, queuedMessage]);
    if (queuedMessage.deliveryState === 'queued') return;
    showOptimisticUserBubble(queuedMessage);
    send.mutate({ ...queuedMessage, bindingId: selected.id });
  }, [attachments, bootstrap, canBootstrap, canWrite, commitQueuedMessages, composerAnnotations, composerScope, conversationDraft, effectiveTurnState, host.id, migrateStreaming.isPending, pendingMigratedSend, references, replaceComposerDraft, selected, send, showOptimisticUserBubble, workspace, workspaceReferences]);
  const sendDraftDirectly = useCallback((draftContent = composerDraftRef.current) => {
    const content = draftContent.trim();
    const hasComposerMessage = Boolean(content || attachments.length || references.length || workspaceReferences.length || composerAnnotations.length);
    if (!hasComposerMessage) {
      const queuedMessage = queuedMessages.find(message => message.deliveryState === 'queued');
      if (!queuedMessage || !canWrite || conversationDraft || migrateStreaming.isPending || pendingMigratedSend
        || effectiveTurnState !== 'running' || !selected?.streaming_callback_ready || queuedMessage.scope !== selected.id) return;
      // With an empty composer, Command/Ctrl+Enter promotes the current queue
      // head. Keep the durable entry and let the common dispatcher mark it
      // in-flight before its one and only HTTP attempt.
      showOptimisticUserBubble(queuedMessage, '正在追加到当前回复');
      updateQueuedMessage(queuedMessage.id, message => ({ ...message, nativeGuidance: true }));
      return;
    }
    if (!canWrite || conversationDraft || !selected) return;
    if (effectiveTurnState === 'running' && !selected.streaming_callback_ready) {
      // A historical conversation cannot use OpenHands' native running-turn
      // append until its streaming bridge has been migrated. Keep the intent
      // safe and editable rather than attempting a request the server must
      // reject.
      enqueueDraft(draftContent);
      return;
    }
    replaceComposerDraft('');
    setOperationError(undefined);
    if (workspace && composerScope) clearConversationComposerDraft(host.id, workspace.id, composerScope);
    const message: QueuedMessage = {
      id: randomId(),
      scope: selected.id,
      content,
      items: attachments,
      references,
      workspaceReferences,
      annotations: composerAnnotations,
      nativeGuidance: effectiveTurnState === 'running',
      deliveryState: 'dispatching',
      createdAt: Date.now(),
    };
    setAttachments([]);
    setReferences([]); setWorkspaceReferences([]); setComposerAnnotations([]);
    commitQueuedMessages(items => [...items, message]);
    showOptimisticUserBubble(message, message.nativeGuidance ? '正在追加到当前回复' : undefined);
    send.mutate({ ...message, bindingId: selected.id });
  }, [attachments, canWrite, commitQueuedMessages, composerAnnotations, composerScope, conversationDraft, effectiveTurnState, enqueueDraft, host.id, migrateStreaming.isPending, pendingMigratedSend, queuedMessages, references, replaceComposerDraft, selected, send, showOptimisticUserBubble, workspace, workspaceReferences, updateQueuedMessage]);
  const sendQueuedMessageImmediately = useCallback((message: QueuedMessage) => {
    if (!canWrite || effectiveTurnState !== 'running' || !selected?.streaming_callback_ready || message.scope !== selected.id || message.deliveryState !== 'queued') return;
    // The message stays in the persisted queue until the formal OpenHands
    // event cursor returns. Mark it as native guidance so the dispatcher may
    // append it during the current native turn.
    showOptimisticUserBubble(message, '正在追加到当前回复');
    updateQueuedMessage(message.id, item => ({ ...item, nativeGuidance: true }));
  }, [canWrite, effectiveTurnState, selected, showOptimisticUserBubble, updateQueuedMessage]);
  const moveQueuedMessage = useCallback((sourceId: string, targetId: string) => {
    if (sourceId === targetId) return;
    commitQueuedMessages(items => {
      const sourceIndex = items.findIndex(item => item.id === sourceId);
      const targetIndex = items.findIndex(item => item.id === targetId);
      if (sourceIndex < 0 || targetIndex < 0) return items;
      const next = [...items];
      const [source] = next.splice(sourceIndex, 1);
      next.splice(sourceIndex < targetIndex ? targetIndex - 1 : targetIndex, 0, source);
      return next;
    });
  }, [commitQueuedMessages]);
  const moveQueuedMessageByOffset = useCallback((sourceId: string, offset: -1 | 1) => {
    commitQueuedMessages(items => {
      const sourceIndex = items.findIndex(item => item.id === sourceId);
      const targetIndex = sourceIndex + offset;
      if (sourceIndex < 0 || targetIndex < 0 || targetIndex >= items.length) return items;
      const next = [...items];
      const [source] = next.splice(sourceIndex, 1);
      next.splice(targetIndex, 0, source);
      return next;
    });
  }, [commitQueuedMessages]);
  const editQueuedMessage = useCallback((message: QueuedMessage) => {
    if (message.deliveryState !== 'queued') return;
    commitQueuedMessages(items => items.filter(item => item.id !== message.id));
    replaceComposerDraft(message.content);
    setAttachments(message.items);
    setReferences(message.references);
    setWorkspaceReferences(message.workspaceReferences ?? []);
    setComposerAnnotations(message.annotations);
    setQueuedMessageMenuId(undefined);
    requestAnimationFrame(() => composerRef.current?.focus());
  }, [commitQueuedMessages, replaceComposerDraft]);
  useEffect(() => {
    if (!selected || !eventsQuery.isSuccess || !queuedMessages.length || conversationActivity.synchronizing || send.isPending || migrateStreaming.isPending || pendingMigratedSend
      || (effectiveTurnState !== 'idle' && effectiveTurnState !== 'running' && effectiveTurnState !== 'paused')) return;
    const next = queuedMessages.find(message => message.scope === selected.id
      && message.deliveryState === 'queued'
      && (effectiveTurnState === 'running' ? message.nativeGuidance : !message.nativeGuidance));
    if (!next) return;
    updateQueuedMessage(next.id, message => ({ ...message, deliveryState: 'dispatching', deliveryError: undefined }));
    if (selected.streaming_callback_ready) {
      // If native state became idle while this entry waited, it starts the
      // next ordinary turn. OpenHands is still the source of that decision.
      send.mutate({ ...next, bindingId: selected.id, nativeGuidance: effectiveTurnState === 'running' && next.nativeGuidance });
    } else if (effectiveTurnState === 'idle') {
      migrateStreaming.mutate(next);
    } else {
      updateQueuedMessage(next.id, message => ({ ...message, deliveryState: 'queued' }));
    }
  }, [conversationActivity.synchronizing, effectiveTurnState, eventsQuery.isSuccess, migrateStreaming, pendingMigratedSend, queuedMessages, selected, send, updateQueuedMessage]);
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
  // The product limit is the frozen native condenser threshold, not the
  // model's larger physical input window. A Conversation supplies its exact
  // frozen value; a draft uses the platform's current frozen default.
  const visibleContextWindow = typeof contextQuery.data?.condenser_max_tokens === 'number'
    && contextQuery.data.condenser_max_tokens > 0
    ? contextQuery.data.condenser_max_tokens
    : DEFAULT_CONTEXT_COMPACTION_THRESHOLD_TOKENS;
  const currentContextTokens = contextQuery.data?.used_tokens;
  const hasCurrentContextUsage = typeof currentContextTokens === 'number' && currentContextTokens >= 0;
  const contextUsagePending = Boolean(
    selected && (!hasCurrentContextUsage || contextQuery.data?.usage_current === false),
  );
  const visibleContextTokens = hasCurrentContextUsage ? currentContextTokens : undefined;
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
  // This is a passive View indicator only. OpenHands owns the actual
  // condensation decision and the resulting event history.
  const currentViewEventCount = contextQuery.data?.view_event_count;
  const hasCurrentViewEventCount = typeof currentViewEventCount === 'number' && currentViewEventCount >= 0;
  const eventLimit = typeof contextQuery.data?.condenser_max_size === 'number'
    && contextQuery.data.condenser_max_size > 0
    ? contextQuery.data.condenser_max_size
    : 10_000;
  const eventProgress = hasCurrentViewEventCount
    ? Math.min(100, Math.round((currentViewEventCount / eventLimit) * 100))
    : undefined;
  const contextTitle = contextProgress
    ? `Token：OpenHands 当前 View ${contextProgress.used.toLocaleString()} / 自动压缩阈值 ${contextProgress.window.toLocaleString()}（${contextProgress.percentage}%）`
    : undefined;
  const tokenPendingLabel = contextQuery.isLoading
    ? '读取中'
    : contextQuery.isError
      ? '暂不可用'
      : contextUsagePending
        ? '待模型更新'
        : '压缩阈值未知';
  const tokenPendingTitle = contextUsagePending
    ? 'OpenHands 尚未返回可确认的当前 View 用量；不会将缺失数据显示为 0。'
    : '当前会话尚未提供可验证的自动压缩阈值；不会显示估算值。';
  const eventPendingLabel = contextQuery.isLoading
    ? '读取中'
    : contextQuery.isError
      ? '暂不可用'
      : selected
        ? '待 Runtime 更新'
        : '待会话创建';
  const eventPendingTitle = selected
    ? '当前 Runtime 尚未返回 OpenHands 压缩后活动 View 的事件数；不会用完整历史事件数代替。'
    : '创建会话后显示 OpenHands 当前活动 View 的正式事件数。';
  const activityTitle = hasCurrentViewEventCount
    ? `OpenHands 当前活动 View 事件 ${currentViewEventCount.toLocaleString()} / 自动压缩阈值 ${eventLimit.toLocaleString()}。`
    : undefined;
  const composerStatus = bootstrapRecovery
    ? '正在安全核对首条消息'
    : conversationDraft && !newConversationModelName ? '请选择模型' : persistModel.isPending ? '正在保存模型设置' : migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : conversationActivity.synchronizing ? '正在同步会话结束' : conversationActivity.state === 'pausing' ? '正在暂停' : conversationActivity.state === 'paused' ? '已暂停' : conversationActivity.state === 'resuming' ? '正在继续' : finalReplyAwaitingNativeCompletion ? '回复已生成，正在收尾' : conversationActivity.state === 'running' ? '正在处理' : streamStatus === 'recovering' ? '连接恢复中' : undefined;
  const composerNote = visibleQueuedMessages.length > 0 ? `已排队 ${visibleQueuedMessages.length} 条` : '';
  const visibleError = operationError ?? confirmationQuery.error ?? eventsQuery.error;
  const composerHasContent = Boolean(
    composerHasText || attachments.length || references.length || workspaceReferences.length || composerAnnotations.length,
  );
  const composerActionSends = composerHasContent && !pendingConfirmation && !conversationActivity.synchronizing;
  const composerActionLabel = bootstrap.isPending ? '正在创建会话' : migrateStreaming.isPending || pendingMigratedSend ? '正在迁移历史会话' : pendingConfirmation ? '等待工具确认' : conversationActivity.synchronizing ? '正在同步会话结束' : composerActionSends
    ? '发送消息'
    : effectiveTurnState === 'idle'
      ? '发送消息'
      : effectiveTurnState === 'running'
        ? '暂停当前 Agent'
        : effectiveTurnState === 'paused'
          ? '继续当前 Agent'
          : effectiveTurnState === 'pausing' ? '正在暂停 Agent' : '正在继续 Agent';
  const composerActionDisabled = !(canWrite || canBootstrap)
    || Boolean(pendingConfirmation)
    || bootstrap.isPending
    || migrateStreaming.isPending
    || Boolean(pendingMigratedSend)
    || conversationActivity.synchronizing
    || (effectiveTurnState === 'idle' && (!composerHasContent || send.isPending))
    || effectiveTurnState === 'pausing'
    || effectiveTurnState === 'resuming';
  const runComposerAction = () => {
    if (composerActionSends || conversationActivity.state === 'idle') enqueueDraft();
    else if (conversationActivity.state === 'running') interrupt.mutate();
    else if (conversationActivity.state === 'paused') resume.mutate();
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
  const openWorkspaceFileLink = (href: string) => {
    const path = workspaceMarkdownLinkPath(href, activeWorkspaceRoot);
    if (!path) return false;
    setFileSelectionReference(undefined);
    setAttachmentRequest(undefined);
    setCandidatePreviewRequest(undefined);
    setMarkdownFileRequest({ key: randomId(), path });
    setDrawerOpen(true);
    return true;
  };
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
  function previewPointerConversationDrop(clientX: number, clientY: number) {
    const target = document.elementFromPoint(clientX, clientY)
      ?.closest<HTMLElement>('[data-conversation-binding-id]');
    const bindingId = target?.dataset.conversationBindingId;
    const group = pointerDragGroupRef.current;
    const draggedId = draggedConversationRef.current;
    if (!target || !bindingId || !group || !draggedId || bindingId === draggedId) return;
    const candidate = group.find(item => item.id === bindingId);
    if (!candidate) return;
    const after = clientY >= target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    const next = { bindingId, after };
    dragTargetRef.current = next;
    setDragTarget(current => current?.bindingId === bindingId && current.after === after ? current : next);
  }
  function commitConversationDrop(draggedId: string | undefined, target: AgentConversation | undefined, after: boolean, group: AgentConversation[]) {
    if (!draggedId || !target || draggedId === target.id) return;
    const reordered = moveConversationInGroup(group, draggedId, target.id, after);
    const movedIndex = reordered.findIndex(item => item.id === draggedId);
    if (movedIndex < 0) return;
    // Reorder the visible list synchronously. The persistence request follows
    // in the background and never controls this interaction or rolls it back.
    setConversationOrder(current => ({
      ...current,
      [conversationScopeKey(target)]: reordered.map(item => item.id),
    }));
    synchronizeConversationOrder(draggedId, reordered.map(candidate => candidate.id));
  }
  function endPointerConversationDrag() {
    const draggedId = draggedConversationRef.current;
    const target = dragTargetRef.current;
    const group = pointerDragGroupRef.current;
    const targetItem = target && group ? group.find(item => item.id === target.bindingId) : undefined;
    commitConversationDrop(draggedId, targetItem, target?.after ?? false, group ?? []);
    draggedConversationRef.current = undefined;
    dragTargetRef.current = undefined;
    pointerDragGroupRef.current = undefined;
    setDraggedBindingId(undefined);
    setDragTarget(undefined);
  }
  function startPointerConversationDrag(event: React.PointerEvent<HTMLButtonElement>, item: AgentConversation, group: AgentConversation[]) {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    draggedConversationRef.current = item.id;
    dragTargetRef.current = undefined;
    pointerDragGroupRef.current = group;
    setDraggedBindingId(item.id);
    setDragTarget(undefined);
    const move = (pointerEvent: PointerEvent) => previewPointerConversationDrop(pointerEvent.clientX, pointerEvent.clientY);
    const end = () => {
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', end, true);
      window.removeEventListener('pointercancel', end, true);
      endPointerConversationDrag();
    };
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', end, true);
    window.addEventListener('pointercancel', end, true);
  }
  const conversationRow = (item: AgentConversation, group: AgentConversation[], options: { allowDrag?: boolean; onSelect?: () => void } = {}) => {
    // The list projection is the native OpenHands running snapshot for every
    // visible conversation. Local state only bridges the selected row between
    // a send/interrupt action and the next bounded list refresh.
    // The selected conversation also has an explicit native readiness read.
    // Once that authoritative read is terminal, do not let a delayed batch
    // list snapshot keep its running marker spinning.
    const running = item.id === selected?.id
      ? conversationActivity.active
      : conversationIsRunning(item.execution_status);
    const conversationWritable = Boolean(item.write_available);
    const sync = conversationOrderSync[item.id];
    return <WorkspaceConversationRow key={item.id} item={item} selectedBindingId={selectedBindingId} running={running} unread={unreadConversationIds.has(item.id)} pinned={pinnedConversationIds.has(item.id)} conversationWritable={conversationWritable} removing={remove.isPending} deleteDisabled={running} dragging={options.allowDrag === false ? false : draggedBindingId === item.id} dropPosition={options.allowDrag === false ? undefined : dragTarget?.bindingId === item.id ? (dragTarget.after ? 'after' : 'before') : undefined} orderSyncState={options.allowDrag === false ? undefined : sync?.state} onPointerDragStart={options.allowDrag === false ? undefined : event => startPointerConversationDrag(event, item, group)} onRetryOrder={options.allowDrag === false || sync?.state !== 'failed' ? undefined : () => synchronizeConversationOrder(item.id, sync.orderedBindingIds)} onSelect={options.onSelect ?? (() => selectConversation(item.id))} onTogglePin={() => toggleConversationPin(item.id)} onMarkUnread={() => markConversationUnread(item.id)} onDelete={features.conversationDeletion && conversationWritable ? () => void confirmDeletion('会话', conversationName(item)).then(ok => { if (ok) remove.mutate(item.id); }) : undefined} reveal={sidebarListMode === 'workspaces' && sidebarRevealBindingId === item.id}/>;
  };
  const pendingBootstrapItem = pendingBootstrap
    ? <button className={pendingBootstrap.draft.id === conversationDraft?.id ? 'active' : ''} aria-current={pendingBootstrap.draft.id === conversationDraft?.id ? 'page' : undefined} aria-label={`${pendingConversationName(pendingBootstrap.message)}，正在创建会话`}><LoaderCircle className="conversation-activity-spin" size={13}/><span><b>{pendingConversationName(pendingBootstrap.message)}</b><small>正在创建会话</small></span><ChevronRight size={13}/></button>
    : null;
  const rootConversations = unpinnedConversations.filter(item => !item.work_directory_id);
  const conversationsForDirectory = (workDirectoryId: string) => unpinnedConversations.filter(
    item => item.work_directory_id === workDirectoryId,
  );
  const selectConversation = (bindingId: string) => {
    setConversationDraft(undefined);
    onNavigate(host.conversationPath(bindingId));
  };
  const openActivityConversation = (bindingId: string) => {
    setSidebarListMode('workspaces');
    setSidebarRevealBindingId(bindingId);
    selectConversation(bindingId);
  };
  const startConversationSearch = (query: string) => {
    if (!workspace || !api.startConversationSearch) return;
    void api.startConversationSearch(workspace.id, query).then(search => {
      setConversationSearchId(search.id);
      setConversationSearchOpen(true);
    }).catch(reason => {
      setOperationError(reason instanceof Error ? reason : new Error('无法开始会话搜索'));
    });
  };
  const openConversationSearchHit = (bindingId: string, eventId: string) => {
    setConversationSearchTargetEventId(eventId);
    setConversationSearchOpen(false);
    selectConversation(bindingId);
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
    {conversationSearchOpen && <ConversationSearchDialog search={conversationSearchQuery.data} onClose={() => setConversationSearchOpen(false)} onSubmit={startConversationSearch} submitting={conversationSearchQuery.isFetching} onOpenHit={openConversationSearchHit}/>}
    <aside className="agent-workbench-rail">
      <header className={!onReturnToSource && features.workDirectories ? 'agent-workbench-rail-actions-only' : undefined}>{onReturnToSource && <button type="button" className="agent-session-return" aria-label="返回节点执行" title="返回节点执行" onClick={onReturnToSource}><ArrowLeft size={16}/></button>}{(onReturnToSource || !features.workDirectories) && <div className="agent-session-host-heading"><span className="eyebrow">{onReturnToSource ? 'FLOWRUN NODE WORKSPACE' : 'FLOWRUN NODE'}</span><h1>{onReturnToSource ? workspace?.display_name || '节点会话' : '节点会话'}</h1></div>}<div className="agent-workbench-create-actions"><button type="button" className={`agent-workbench-activity-trigger${sidebarListMode === 'activity' ? ' active' : ''}`} aria-label={`查看活动会话${activityConversations.length ? `（${activityConversations.length}）` : ''}`} title="查看活动会话" onClick={() => setSidebarListMode(current => current === 'activity' ? 'workspaces' : 'activity')}><Bell size={15}/>{activityConversations.length > 0 && <span aria-hidden="true">{activityConversations.length > 99 ? '99+' : activityConversations.length}</span>}</button><button className="primary" disabled={!conversationSearchSupported} onClick={() => setConversationSearchOpen(true)}>{conversationSearchQuery.data?.state === 'PENDING' || conversationSearchQuery.data?.state === 'RUNNING' ? <LoaderCircle className="conversation-activity-spin" size={15}/> : conversationSearchQuery.data?.state === 'SUCCEEDED' ? <Check size={15}/> : <Search size={15}/>}{conversationSearchQuery.data?.state === 'SUCCEEDED' ? '搜索完成' : '搜索会话'}</button>{features.workDirectories && <button type="button" className="secondary" aria-label="新增工作区" disabled={!runtimeWritable} onClick={() => setWorkDirectoryCreatorOpen(true)}><FolderPlus size={14}/>新增工作区</button>}</div></header>
      <div className="agent-workbench-list">
        {sidebarListMode === 'activity'
          ? <section className="agent-workspace-activity" aria-label="活动会话"><header><div><span className="eyebrow">ACTIVITY</span><b>活动</b></div><button type="button" aria-label="返回工作区列表" title="返回工作区列表" onClick={() => setSidebarListMode('workspaces')}><ArrowLeft size={15}/></button></header>{activityConversations.length ? activityConversations.map(item => conversationRow(item, [], { allowDrag: false, onSelect: () => openActivityConversation(item.id) })) : <p>没有正在运行或未读的会话。</p>}</section>
          : <>{pinnedConversations.length > 0 && <section className="agent-workspace-pinned" aria-label="置顶会话"><header><Pin size={13}/><span>置顶</span></header><div>{pinnedConversations.map(item => conversationRow(item, [], { allowDrag: false }))}</div></section>}
            <WorkspaceConversationGroup groupId="root" label="根工作区" conversationCount={rootConversations.length} forceExpanded={Boolean(revealedUnpinnedConversation && !revealedUnpinnedConversation.work_directory_id)} canCreateConversation={canOpenConversation} onCreateConversation={() => openConversationDraft({ displayName: '根工作区' })}>
              {visibleCount => <>{pendingBootstrapItem && !pendingBootstrap?.draft.workDirectoryId ? pendingBootstrapItem : null}{rootConversations.slice(0, visibleCount).map(item => conversationRow(item, rootConversations))}</>}
            </WorkspaceConversationGroup>
            {features.workDirectories && workDirectories.map(directory => <WorkspaceConversationGroup key={directory.id} groupId={directory.id} label={directory.display_name} conversationCount={conversationsForDirectory(directory.id).length} forceExpanded={revealedUnpinnedConversation?.work_directory_id === directory.id} canCreateConversation={canOpenConversation} onCreateConversation={() => openConversationDraft({ workDirectoryId: directory.id, displayName: directory.display_name })} onDelete={api.deleteWorkDirectory && runtimeWritable ? () => void removeWorkDirectory(directory) : undefined}>
              {visibleCount => { const group = conversationsForDirectory(directory.id); return <>{pendingBootstrapItem && pendingBootstrap?.draft.workDirectoryId === directory.id ? pendingBootstrapItem : null}{group.slice(0, visibleCount).map(item => conversationRow(item, group))}</>}}
            </WorkspaceConversationGroup>)}</>}
      </div>
      {features.capabilities && (selected || features.draftCapabilitySelection) && <footer className="agent-workbench-rail-footer"><button type="button" disabled={selected ? !canWrite : !runtimeWritable} onClick={() => setCapabilityManagerOpen(true)}><Boxes size={15}/><span><b>会话配置</b><small>{selected ? '管理当前会话配置' : '为新会话配置能力'}</small></span><ChevronRight size={14}/></button></footer>}
    </aside>
    <section className="agent-workbench-main">
      <header className="agent-workbench-header"><div>{editing ? <div className="agent-title-edit"><input ref={titleInput} aria-label="会话标题" value={title} onChange={event => setTitle(event.target.value)} onBlur={() => { if (!rename.isPending) { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }} onKeyDown={event => { if (event.key === 'Enter' && title.trim()) { event.preventDefault(); rename.mutate(); } if (event.key === 'Escape') { setTitle(selected ? conversationName(selected) : ''); setEditing(false); } }}/></div> : !(hideDraftTitle && conversationDraft) && <h2 className="agent-session-title" title={selected ? conversationName(selected) : undefined} aria-label={selected && canWrite ? '双击修改标题' : undefined} onDoubleClick={() => { if (!selected || !canWrite) return; setTitle(conversationName(selected)); setEditing(true); }}><span>{selected ? conversationName(selected) : conversationDraft ? '新会话' : '开始一个新的会话'}</span></h2>}{features.modelSelection && (selected || conversationDraft) && <small className="agent-session-provider">当前供应商：{selected ? boundProviderInfo?.name ?? '未配置' : draftProviderInfo?.name ?? '请选择模型供应商'}{conversationDraft ? ` · ${conversationDraft.displayName}` : ''}</small>}</div><div className="agent-header-actions">{features.conversationDeletion && selected && <button type="button" className="danger" aria-label="删除会话" title={selectedConversationRunning ? '会话运行中，请先停止' : '删除会话'} disabled={!canWrite || selectedConversationRunning || remove.isPending} onClick={() => void confirmDeletion('会话', conversationName(selected)).then(ok => { if (ok) remove.mutate(selected.id); })}><Trash2 size={14}/></button>}</div></header>
      <div className="agent-workbench-content">
      {runtime?.state === 'RECOVERING' && <section className="agent-runtime-recover"><LoaderCircle size={18}/><div><b>运行环境正在恢复</b><span>{runtime.message || '历史会话和工作区文件仍可查看；恢复完成后可继续发送消息和使用终端。'}</span></div></section>}
      {runtime && !runtime.write_available && !selected?.write_available && runtime.state !== 'RECOVERING' && <section className="agent-runtime-recover"><ShieldAlert size={18}/><div><b>节点会话已切换为只读</b><span>{runtime.message || '节点执行已停止；历史会话和工作区文件仍可查看。'}</span></div></section>}
      {selected || conversationDraft ? <ConversationSurface
        key={selected?.id ?? conversationDraft?.id}
        events={displayedEvents}
        liveText={liveText}
        isGenerating={conversationActivity.active}
        isPaused={conversationActivity.state === 'paused'}
        emptyResponseRecoveryActive={emptyResponseRecoveryActive}
        historyPending={Boolean(selected && historyLoadingBindingId === selected.id)}
        conversationScope={selected?.id ?? conversationDraft?.id}
        historyPrepend={historyPrepend}
        onHistoryAnchorCaptured={onHistoryAnchorCaptured}
        onHistoryAnchorRestored={onHistoryAnchorRestored}
        requestStartedAt={requestStartedAt}
        requestSubmitting={(send.isPending && !nativeGuidanceDispatching) || bootstrap.isPending || rewrite.isPending}
        onRewrite={selected && canWrite && features.rewrite ? requestRewrite : undefined}
        onFork={canFork ? eventId => { if (fork.isPending) return; const directoryName = selected?.work_directory_id ? workDirectories.find(directory => directory.id === selected.work_directory_id)?.display_name ?? '当前工作区' : '节点工作目录'; void dialog.confirm({ title: '从此处分叉会话？', message: `将保留当前会话在“${directoryName}”中的工作目录和截至此回复的历史记录，创建一条可独立继续的新会话。源会话不会被修改。`, confirmLabel: '创建分叉会话' }).then(confirmed => { if (confirmed) fork.mutate(eventId); }); } : undefined}
        onOpenAttachment={features.attachments ? openAttachmentInDrawer : undefined}
        onPreviewCandidateFile={candidateOutputUrl && workspace ? openCandidateFileInDrawer : undefined}
        onReviewChanges={openChangesReview}
        onOpenWorkspaceFile={openWorkspaceFileLink}
        workspaceRoot={activeWorkspaceRoot}
        annotations={messageAnnotations}
        onCreateAnnotation={selected && canWrite ? anchor => void createAnnotation('CONVERSATION_TEXT', anchor) : undefined}
        onLocateAnnotation={locateAnnotation}
        taskControl={eventsQuery.data?.task_control ?? EMPTY_TASK_CONTROL}
        monitoring={eventsQuery.data?.monitoring}
        connectionState={inputReadinessQuery.isError ? 'unavailable' : streamStatus === 'recovering' ? 'recovering' : streamStatus === 'connecting' ? 'checking' : inputReadinessQuery.isFetching && !inputReadinessQuery.data ? 'checking' : 'connected'}
      /> : <div className="agent-workbench-empty"><Bot size={32}/><b>新建会话开始协作</b><span>{features.workDirectories ? '每个会话共享同一工作区，但保留独立的对话与事件记录。' : '会话固定在当前节点 Attempt 的隔离工作目录。'}</span><button className="primary" disabled={!canOpenConversation} onClick={() => openConversationDraft({ displayName: features.workDirectories ? '根工作区' : '节点工作目录' })}><Plus size={15}/>新建会话</button></div>}
      {visibleError && <p className="agent-workbench-error">{visibleError.message}</p>}
      </div>
      {(selected || conversationDraft) && runtime?.state !== 'RECOVERING' && <div className="agent-composer-dock">
        <div className={`agent-composer ${conversationActivity.active || pendingConfirmation ? 'busy' : ''}`}>
        {pendingConfirmation && <section className="agent-confirmation" aria-label="工具执行确认"><header><ShieldAlert size={17}/><div><b>工具正在等待你的确认</b><span>动作尚未执行。请核对整批内容后批准或拒绝。</span></div></header><div className="agent-confirmation-actions">{(pendingConfirmation.actions ?? []).map((action: AgentPendingConfirmationAction) => <article key={action.digest}><div><b>{action.summary || action.tool_name}</b><span>{action.security_risk || 'UNKNOWN'}</span></div>{Object.keys(action.arguments).length > 0 && <pre>{JSON.stringify(action.arguments, null, 2)}</pre>}</article>)}</div><textarea aria-label="工具确认理由" value={confirmationReason} maxLength={2000} placeholder="填写批准或拒绝理由…" onChange={event => setConfirmationReason(event.target.value)}/><footer><button type="button" className="danger" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(false)}><X size={14}/>拒绝整批</button><button type="button" className="primary" disabled={!confirmationReason.trim() || decideConfirmation.isPending} onClick={() => decideConfirmation.mutate(true)}><Check size={14}/>批准整批</button></footer></section>}
        {visibleQueuedMessages.length > 0 && <section className="agent-queued-messages" aria-label="消息投递队列">
          <header><b>消息队列</b><span>{visibleQueuedMessages.filter(message => message.deliveryState === 'queued').length} 条等待发送；结果不确定的消息不会自动重发</span></header>
          {visibleQueuedMessages.map((message, index) => {
            const deliveryState = message.deliveryState ?? 'queued';
            const status = deliveryState === 'dispatching' ? '正在提交'
              : deliveryState === 'ambiguous' ? '结果不确定'
                : deliveryState === 'rejected' ? '已被拒绝' : '等待发送';
            const editable = deliveryState === 'queued';
            const removable = deliveryState !== 'dispatching';
            return <article
              key={message.id}
              draggable={editable}
              onDragStart={event => {
                if (!editable || !(event.target instanceof Element) || !event.target.closest('.queue-drag-handle')) {
                  event.preventDefault();
                  return;
                }
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', message.id);
                setDraggedQueuedMessageId(message.id);
              }}
              onDragOver={event => {
                if (editable && draggedQueuedMessageId && draggedQueuedMessageId !== message.id) event.preventDefault();
              }}
              onDrop={event => {
                event.preventDefault();
                const sourceId = event.dataTransfer.getData('text/plain') || draggedQueuedMessageId;
                if (sourceId && editable) moveQueuedMessage(sourceId, message.id);
                setDraggedQueuedMessageId(undefined);
              }}
              onDragEnd={() => setDraggedQueuedMessageId(undefined)}
              className={`delivery-${deliveryState}${draggedQueuedMessageId === message.id ? ' dragging' : ''}`}
            >
              <button type="button" className="queue-drag-handle" aria-label={`拖动排队消息 ${index + 1} 以调整顺序`} title="拖动调整顺序" disabled={!editable} tabIndex={-1}><GripVertical size={14}/></button>
              <small>{index + 1}</small>
              <p>{message.content || (message.references.length ? `会话引用 ${message.references.length} 条` : message.workspaceReferences?.length ? `工作区引用 ${message.workspaceReferences.length} 条` : '图片附件')}</p>
              <span>{[status, message.items.length ? `${message.items.length} 个附件` : '', message.references.length ? `${message.references.length} 条会话引用` : '', message.workspaceReferences?.length ? `${message.workspaceReferences.length} 条工作区引用` : ''].filter(Boolean).join(' · ')}</span>
              <div>
                {editable && <button type="button" aria-label={`调整方向排队消息 ${index + 1}`} title="立即发送，调整当前回复方向" disabled={!canWrite || effectiveTurnState !== 'running' || !selected?.streaming_callback_ready || message.scope !== selected.id} onClick={() => sendQueuedMessageImmediately(message)}><CornerDownRight size={12}/>调整方向</button>}
                {deliveryState === 'ambiguous' && <button type="button" className="queue-refresh" aria-label={`刷新会话确认排队消息 ${index + 1}`} title="刷新会话确认，系统不会自动重发" onClick={refresh}>刷新确认</button>}
                <button type="button" className="queue-remove" aria-label={`移除排队消息 ${index + 1}`} disabled={!removable} onClick={() => { commitQueuedMessages(items => items.filter(item => item.id !== message.id)); setQueuedMessageMenuId(current => current === message.id ? undefined : current); }}><X size={13}/></button>
                {editable && <button type="button" className="queue-more" aria-label={`更多排队消息操作 ${index + 1}`} title="更多操作" aria-expanded={queuedMessageMenuId === message.id} onClick={() => setQueuedMessageMenuId(current => current === message.id ? undefined : message.id)}><Ellipsis size={14}/></button>}
                {editable && queuedMessageMenuId === message.id && <div className="queue-menu" role="menu">
                  <button type="button" role="menuitem" disabled={index === 0} onClick={() => { moveQueuedMessageByOffset(message.id, -1); setQueuedMessageMenuId(undefined); }}>上移</button>
                  <button type="button" role="menuitem" disabled={index === visibleQueuedMessages.length - 1} onClick={() => { moveQueuedMessageByOffset(message.id, 1); setQueuedMessageMenuId(undefined); }}>下移</button>
                  <button type="button" role="menuitem" onClick={() => editQueuedMessage(message)}>编辑</button>
                </div>}
              </div>
              {message.deliveryError && <em className="queue-delivery-error">{message.deliveryError}</em>}
            </article>;
          })}
        </section>}
        <ComposerCapabilityAutocomplete key={composerScope ?? 'composer'} ref={composerRef} initialDraft={composerDraftRef.current} scope={composerScope} suggestions={composerSuggestions} placeholder={pendingConfirmation ? '请先处理上方工具确认…' : conversationActivity.synchronizing ? '正在同步上一轮结束状态…' : '给 Agent 发消息…'} disabled={!canCompose || Boolean(pendingConfirmation) || bootstrap.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend) || conversationActivity.synchronizing || conversationActivity.state === 'pausing' || conversationActivity.state === 'resuming'} onDraftChange={setComposerDraft} onContentPresenceChange={onComposerContentPresenceChange} onDraftPersist={persistComposerDraft} onPaste={event => { if (!features.attachments || !composerScope) return; const files = transferredFiles(event.clipboardData); if (!files.length) return; event.preventDefault(); for (const file of files) upload.mutate({ file, scope: composerScope }); }} onDropFiles={features.attachments && composerScope ? files => { for (const file of files) upload.mutate({ file, scope: composerScope }); } : undefined} onDropWorkspaceFiles={paths => setWorkspaceReferences(current => [...current, ...paths.flatMap(path => current.some(reference => reference.path === path) ? [] : [{ path, kind: 'file' as const, display_name: path.split('/').filter(Boolean).pop() ?? path }])])} onSubmit={enqueueDraft} onDirectSubmit={sendDraftDirectly} onManageCapabilities={features.capabilities && (selected || features.draftCapabilitySelection) ? () => setCapabilityManagerOpen(true) : undefined} onWorkspaceReferenceSelected={() => { setWorkspaceReferenceQuery(''); setWorkspaceReferencePickerOpen(true); }}/>
        {features.attachments && attachments.length > 0 && <div className="agent-attachments">{attachments.map(item => <span key={item.path}><button type="button" className="agent-attachment-open" title={`在右侧查看附件：${item.filename}`} onClick={() => openAttachmentInDrawer(item)}>{item.image_data_url && <img src={item.image_data_url} alt=""/>}<em>{item.filename}</em></button><button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(all => all.filter(candidate => candidate.path !== item.path))}>×</button></span>)}</div>}
        {references.length > 0 && <div className="agent-attachments agent-conversation-references" aria-label="已添加的会话引用">{references.map((reference, index) => <span key={`${reference.eventId}:${reference.content}`}><span className="agent-attachment-open" title={reference.content}><Quote size={14}/><em>{`会话引用 ${index + 1}`}</em></span><button type="button" className="agent-attachment-remove" aria-label={`移除会话引用 ${index + 1}`} onClick={() => setReferences(current => current.filter(item => item !== reference))}>×</button></span>)}</div>}
        {(selected || conversationDraft) && <ComposerAnnotationList annotations={composerAnnotations} onLocate={locateAnnotation} onRemove={annotation => setComposerAnnotations(current => current.filter(item => item.id !== annotation.id))} onUpdate={(annotation, comment) => void updateAnnotation(annotation, comment)}/>}
        {workspaceReferences.length > 0 && <div className="agent-attachments agent-workspace-references" aria-label="已添加的工作区引用">{workspaceReferences.map(reference => <span key={workspaceReferenceKey(reference)} title={reference.path}><span className="agent-attachment-open">{reference.kind === 'directory' ? <Folder size={14}/> : <FileCode2 size={14}/>}<em><b>{reference.display_name}</b><small>{workspaceReferenceLabel(reference)}</small></em></span><button type="button" className="agent-attachment-remove" aria-label={'移除工作区引用 ' + reference.display_name} onClick={() => setWorkspaceReferences(current => current.filter(item => workspaceReferenceKey(item) !== workspaceReferenceKey(reference)))}>×</button></span>)}</div>}
        <footer>
          <div className="agent-composer-context">
            {features.attachments && (selected || conversationDraft) && <><input ref={attachmentInput} aria-label="上传附件" type="file" multiple hidden onChange={event => { if (composerScope) for (const file of Array.from(event.target.files ?? [])) upload.mutate({ file, scope: composerScope }); event.currentTarget.value = ''; }}/><button type="button" aria-label="添加附件" disabled={!canCompose || Boolean(pendingConfirmation) || upload.isPending} onClick={() => attachmentInput.current?.click()}><Plus size={17}/></button></>}
            {contextProgress ? <span className="agent-context-progress token" title={contextTitle} aria-label={`Token 上下文用量 ${contextProgress.percentage}%`}><i style={{ '--context-progress': `${contextProgress.percentage}%` } as CSSProperties}/><em><small>Token</small>{contextProgress.usedLabel} / {contextProgress.windowLabel}</em></span> : (selected || conversationDraft) && <span className="agent-context-progress token pending" title={tokenPendingTitle} aria-label={`Token 上下文用量${tokenPendingLabel}`}><i style={{ '--context-progress': '0%' } as CSSProperties}/><em><small>Token</small>{tokenPendingLabel}</em></span>}
            {(selected || conversationDraft) && (hasCurrentViewEventCount && eventProgress !== undefined ? <span className="agent-context-progress activity events" title={activityTitle} aria-label={`OpenHands 当前活动 View 事件 ${currentViewEventCount} 条，自动压缩阈值 ${eventLimit} 条`}><i style={{ '--context-progress': `${eventProgress}%` } as CSSProperties}/><em><small>事件</small>{exactCount(currentViewEventCount)} / {exactCount(eventLimit)}</em></span> : <span className="agent-context-progress activity events pending" title={eventPendingTitle} aria-label={`事件上下文用量${eventPendingLabel}`}><i style={{ '--context-progress': '0%' } as CSSProperties}/><em><small>事件</small>{eventPendingLabel}</em></span>)}
            {composerStatus && <span className="agent-composer-status">{composerStatus}</span>}
            {composerNote && <span className="agent-composer-note">{composerNote}</span>}
          </div>
          <div className="agent-composer-actions">
            {features.modelSelection && (selected ? <ComposerModelMenu providers={connectedProviders} providerId={conversationProviderId} modelName={activeConversationModelName} models={availableConversationModels} efforts={supportedEfforts} effort={reasoningEffort ?? selected.reasoning_effort ?? contextQuery.data?.reasoning_effort ?? conversationModel?.default_reasoning_effort ?? ''} disabled={!canWrite || conversationActivity.active || queuedMessages.length > 0 || Boolean(pendingConfirmation) || persistModel.isPending || migrateStreaming.isPending || Boolean(pendingMigratedSend)} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; const effort = model.default_reasoning_effort ?? null; setConversationProviderId(providerId); setConversationModelName(model.model_name); setReasoningEffort(effort); persistModel.mutate({ providerId, modelName: model.model_name, effort }); }} onModelChange={modelName => { const model = availableConversationModels.find(item => item.model_name === modelName); const effort = model?.default_reasoning_effort ?? null; setConversationModelName(modelName); setReasoningEffort(effort); persistModel.mutate({ providerId: conversationProviderId, modelName, effort }); }} onEffortChange={effort => { const nextEffort = effort || null; setReasoningEffort(nextEffort); persistModel.mutate({ providerId: conversationProviderId, modelName: activeConversationModelName, effort: nextEffort }); }}/> : <ComposerModelMenu providers={connectedProviders} providerId={newConversationProviderId} modelName={newConversationModelName} models={availableDraftModels} efforts={supportedDraftEfforts} effort={newConversationReasoningEffort ?? draftConversationModel?.default_reasoning_effort ?? ''} disabled={!canOpenConversation || bootstrap.isPending} onProviderChange={providerId => { const provider = connectedProviders.find(item => item.id === providerId); const model = provider?.models.find(item => item.enabled && item.is_default); if (!provider || !model) return; setNewConversationProviderId(providerId); setNewConversationModelName(model.model_name); setNewConversationReasoningEffort(model.default_reasoning_effort ?? null); }} onModelChange={modelName => { const model = availableDraftModels.find(item => item.model_name === modelName); setNewConversationModelName(modelName); setNewConversationReasoningEffort(model?.default_reasoning_effort ?? null); }} onEffortChange={effort => setNewConversationReasoningEffort(effort || null)}/>) }
            <button type="button" className={`agent-send${!composerActionSends && (conversationActivity.state === 'paused' || conversationActivity.state === 'resuming') ? ' resume' : ''}`} aria-label={composerActionLabel} disabled={composerActionDisabled} onClick={runComposerAction}>{pendingConfirmation ? <ShieldAlert size={14}/> : conversationActivity.synchronizing ? <LoaderCircle className="conversation-activity-spin" size={14}/> : composerActionSends || conversationActivity.state === 'idle' ? <Send size={16}/> : conversationActivity.state === 'paused' || conversationActivity.state === 'resuming' ? <Play size={12} fill="currentColor"/> : <Square size={10} fill="currentColor"/>}</button>
          </div>
        </footer>
        </div>
        <div className="agent-composer-bottom">
          <button type="button" className={`agent-current-workspace${workspacePathCopied ? ' copied' : ''}`} title={`${workspacePathCopied ? '已复制' : '点击复制'}：${currentWorkspaceRelativePath}`} aria-label={workspacePathCopied ? `工作区路径已复制：${currentWorkspaceRelativePath}` : `当前工作区：${currentWorkspaceName}；点击复制相对根工作区路径：${currentWorkspaceRelativePath}`} onClick={copyCurrentWorkspacePath}>
            <Folder size={16}/><span>{currentWorkspaceName}</span>{workspacePathCopied && <small aria-live="polite">已复制</small>}
          </button>
          <ConversationTaskPlan events={displayedEvents} isGenerating={conversationActivity.active} conversationScope={selected?.id ?? conversationDraft?.id}/>
        </div>
      </div>}
    </section>
    <WorkspaceDrawer
      open={drawerOpen}
      onOpen={() => setDrawerOpen(true)}
      onClose={() => { setFileSelectionReference(undefined); setDrawerOpen(false); }}
      onAnnotateFileSelection={(selected && canWrite) || conversationDraft ? (path, selection, quote) => void createAnnotation('WORKSPACE_FILE_RANGE', { path, selection, quote }) : undefined}
      highlightedFileSelection={fileSelectionReference}
      workspaceId={workspace.id}
      scopeKey={selected?.id ?? pendingCreatedId ?? conversationDraft?.id ?? 'workspace-root'}
      migrateFromScopeKey={workspaceScopeMigration}
      bindingId={selected?.id}
      workDirectoryId={selected ? undefined : conversationDraft?.workDirectoryId}
      conversation={selected}
      conversationCumulativeTokens={contextQuery.data?.cumulative_tokens}
      attachments={drawerAttachments}
      sources={drawerSources}
      attachmentRequest={attachmentRequest}
      candidatePreviewRequest={candidatePreviewRequest}
      markdownFileRequest={markdownFileRequest}
      reviewChanges={reviewChanges}
      reviewRequestId={reviewRequestId}
      sessionChanges={sessionFileChanges}
      onReviewChanges={openChangesReview}
      runtimeAvailable={Boolean((runtime?.terminal_available ?? runtime?.write_available) && (!features.terminalRequiresConversation || selected))}
      runtimeTasks={runtimeTasks}
      agentDefinitions={agentDefinitionAssets}
      sessionStopped={sessionStopped}
    />
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
