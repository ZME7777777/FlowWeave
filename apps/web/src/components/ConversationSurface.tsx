import { BookOpen, Check, ChevronDown, ChevronRight, CircleAlert, ClipboardList, Copy, ExternalLink, Eye, FileCode2, FileCog, FileJson, FilePenLine, FilePlus2, FileText, FileType2, GitFork, Link, LoaderCircle, PanelRightOpen, Pencil, PlugZap, Quote, Sparkles, SquareTerminal, Workflow, Wrench } from 'lucide-react';
import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type RefObject } from 'react';
import type { AgentActivitySummary, AgentAttachment, AgentConversationAnnotation, AgentConversationReference, AgentWorkspaceReference, OpenHandsConversationEvent, RuntimeTaskControlSnapshot } from '../types';
import { SubagentAvatar } from './SubagentAvatar';
import { useEscapeClose } from './useEscapeClose';
import { subagentAvatarSlotForEvent, subagentAvatarSlots, type SubagentAvatarSlot } from '../utils/subagentAvatar';
import { workspaceFileChanges, workspaceRelativePath, type WorkspaceFileChange } from './agent-session/fileChanges';
import { isOpenHandsAgentReply, isOpenHandsEmptyResponseRecovery, orderOpenHandsConversationEvents, parseOpenHandsEventTime } from './conversationEvents';
import './conversation-surface.css';

type ItemKind = 'user' | 'assistant' | 'thought' | 'tool' | 'error' | 'condensation';

interface Item {
  event: OpenHandsConversationEvent;
  kind: ItemKind;
  title: string;
  content: string;
}

interface Turn {
  id: string;
  renderKey: string;
  user?: Item;
  assistant?: Item;
  activity: Item[];
}

export interface ModelRetryStatus {
  /** Live retry frames always provide these; historical terminal events do not. */
  attempt?: number;
  maxAttempts?: number;
  failureKind: string;
  final: boolean;
  modelRole: 'primary' | 'fallback';
  subject?: 'model' | 'execution' | 'condensation';
  errorCode?: string;
}

interface UserMessageNavigationItem {
  id: string;
  content: string;
}

export interface ConversationReference {
  eventId: string;
  content: string;
}

interface ConversationAnnotationReference extends ConversationReference {
  /**
   * Offset in the event's whitespace-free rendered text.  It disambiguates
   * repeated quoted text without persisting any separate annotation record.
   */
  compactStart: number;
}

interface ConversationTextHighlight {
  eventId: string;
  quote: string;
  compactStart?: number;
}

interface ConversationHighlightRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface ActivityEntry {
  id: string;
  item: Item;
  action?: Item;
  results: Item[];
}

interface ProgressActivityGroup {
  id: string;
  progress: Item;
  entries: ActivityEntry[];
}

type ActivityRow =
  | { kind: 'entry'; entry: ActivityEntry }
  | { kind: 'progress-group'; group: ProgressActivityGroup };

type TaskListStatus = 'todo' | 'in_progress' | 'done';

interface TaskListItem {
  title: string;
  notes: string;
  status: TaskListStatus;
}

interface TaskListSnapshot {
  items: TaskListItem[];
  command: 'view' | 'plan' | undefined;
  timestamp?: string;
}

function taskListFromDetails(details: Record<string, unknown> | undefined): TaskListItem[] | undefined {
  const rawTasks = details?.task_list;
  if (!Array.isArray(rawTasks)) return undefined;
  return rawTasks.flatMap(task => {
    if (!task || typeof task !== 'object' || Array.isArray(task)) return [];
    const value = task as Record<string, unknown>;
    const title = typeof value.title === 'string' ? value.title.trim() : '';
    if (!title) return [];
    const rawStatus = typeof value.status === 'string' ? value.status : 'todo';
    const status: TaskListStatus = rawStatus === 'done' || rawStatus === 'in_progress' ? rawStatus : 'todo';
    return [{ title: title.slice(0, 1_000), notes: typeof value.notes === 'string' ? value.notes.slice(0, 8_000) : '', status }];
  });
}

function taskListSnapshot(actionDetails: Record<string, unknown>, resultDetails: Record<string, unknown>, timestamp?: unknown): TaskListSnapshot | undefined {
  const resultItems = taskListFromDetails(resultDetails);
  const actionItems = taskListFromDetails(actionDetails);
  const items = resultItems ?? actionItems;
  if (!items) return undefined;
  const command = detailText(resultDetails.command) || detailText(actionDetails.command);
  return {
    items,
    command: command === 'plan' || command === 'view' ? command : undefined,
    timestamp: typeof timestamp === 'string' ? timestamp : undefined,
  };
}

function taskStatusLabel(status: TaskListStatus): string {
  return status === 'done' ? '已完成' : status === 'in_progress' ? '进行中' : '待办';
}

function TaskStatusIcon({ status }: { status: TaskListStatus }) {
  return status === 'done'
    ? <Check className="conversation-task-status-icon done" size={14} aria-label="已完成"/>
    : <span className={`conversation-task-status-icon ${status}`} aria-label={taskStatusLabel(status)}>{status === 'in_progress' && <i/>}</span>;
}

function TaskListItems({ items, source, currentTaskIndex, currentTaskRef }: { items: TaskListItem[]; source?: string; currentTaskIndex?: number; currentTaskRef?: RefObject<HTMLLIElement | null> }) {
  if (!items.length) return <p className="conversation-task-list-empty">当前没有任务。</p>;
  return <ol className="conversation-task-list">
    {items.map((task, index) => <li key={`${index}:${task.title}`} ref={index === currentTaskIndex ? currentTaskRef : undefined} data-current-task={index === currentTaskIndex || undefined}>
      <details>
        <summary><TaskStatusIcon status={task.status}/><span><b>{task.title}</b><small>{taskStatusLabel(task.status)}</small></span><ChevronRight size={13}/></summary>
        <div className="conversation-task-detail">
          {task.notes ? <p>{task.notes}</p> : <p>此任务没有附加说明。</p>}
          {source && <small>{source}</small>}
        </div>
      </details>
    </li>)}
  </ol>;
}

function latestCurrentTaskList(events: OpenHandsConversationEvent[]): TaskListSnapshot | undefined {
  for (const event of [...events].reverse()) {
    if (event.event_type !== 'TOOL_RESULT' || event.payload.event_name !== 'TaskTrackerObservation') continue;
    const details = event.payload.details ?? {};
    if (details.is_error === true) continue;
    const items = taskListFromDetails(details);
    if (!items) continue;
    const command = detailText(details.command);
    return {
      items,
      command: command === 'plan' || command === 'view' ? command : undefined,
      timestamp: typeof event.payload.timestamp === 'string' ? event.payload.timestamp : undefined,
    };
  }
  return undefined;
}

function currentTurnEvents(events: OpenHandsConversationEvent[]): OpenHandsConversationEvent[] {
  let userEventIndex = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const source = String(event.payload.source ?? '').toLowerCase();
    if (event.event_type === 'MESSAGE' && (source === 'user' || source === 'human')) {
      userEventIndex = index;
      break;
    }
  }
  return userEventIndex >= 0 ? events.slice(userEventIndex) : [];
}

export function ConversationTaskPlan({ events, isGenerating, conversationScope }: { events: OpenHandsConversationEvent[]; isGenerating: boolean; conversationScope?: string }) {
  const currentEvents = useMemo(() => currentTurnEvents(events), [events]);
  const snapshot = useMemo(() => latestCurrentTaskList(currentEvents), [currentEvents]);
  const currentTurnId = currentEvents.find(event => event.event_type === 'MESSAGE'
    && ['user', 'human'].includes(String(event.payload.source ?? '').toLowerCase()))?.id;
  const retainedSnapshot = useRef<{ scope?: string; turnId?: string; snapshot: TaskListSnapshot } | undefined>(undefined);
  if (!isGenerating || retainedSnapshot.current?.scope !== conversationScope || retainedSnapshot.current?.turnId !== currentTurnId) {
    retainedSnapshot.current = undefined;
  }
  if (snapshot?.items.some(item => item.status !== 'done')) {
    retainedSnapshot.current = { scope: conversationScope, turnId: currentTurnId, snapshot };
  } else if (snapshot?.items.length) {
    retainedSnapshot.current = undefined;
  }
  const displayedSnapshot = snapshot?.items.some(item => item.status !== 'done')
    ? snapshot
    : retainedSnapshot.current?.snapshot;
  const completed = displayedSnapshot?.items.filter(item => item.status === 'done').length ?? 0;
  const showPlan = isGenerating && displayedSnapshot && displayedSnapshot.items.some(item => item.status !== 'done');

  if (!showPlan || !displayedSnapshot) return null;
  return <section className="conversation-live-task-plan" tabIndex={0} role="group" aria-label={`任务：${completed} / ${displayedSnapshot.items.length} 已完成`}>
    <div className="conversation-live-task-plan-summary"><ClipboardList size={14}/><span><b>任务</b><small>{`${completed} / ${displayedSnapshot.items.length} 已完成`}</small></span></div>
    <aside className="conversation-live-task-plan-preview" role="tooltip" aria-label="当前任务详情">
      <header><b>当前任务</b><small>{`${completed} / ${displayedSnapshot.items.length} 已完成`}</small></header>
      <ol>
        {displayedSnapshot.items.map((task, index) => <li key={`${index}:${task.title}`} data-status={task.status}>
          <TaskStatusIcon status={task.status}/><span><b>{task.title}</b>{task.notes && <small>{task.notes}</small>}</span><em>{taskStatusLabel(task.status)}</em>
        </li>)}
      </ol>
    </aside>
  </section>;
}

type TurnProcessBlock = { kind: 'activity'; id: string; items: Item[]; startedAt?: number; finishedAt?: number; active: boolean };

function eventAttachments(event: OpenHandsConversationEvent): AgentAttachment[] {
  return Array.isArray(event.payload.attachments) ? event.payload.attachments : [];
}

function attachmentSize(bytes: number): string {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function workspaceReferenceLabel(reference: AgentWorkspaceReference): string {
  const selection = reference.selection;
  return selection
    ? `${reference.relative_path ?? reference.display_name} · ${selection.start_line}:${selection.start_column}–${selection.end_line}:${selection.end_column}`
    : reference.kind === 'directory' ? '工作区目录 · 本地路径引用' : '工作区文件 · 本地路径引用';
}

function eventAnnotations(event: OpenHandsConversationEvent): AgentConversationAnnotation[] {
  const raw = event.payload.collaboration_annotations;
  return Array.isArray(raw) ? raw.filter((item): item is AgentConversationAnnotation => Boolean(
    item
    && typeof item === 'object'
    && typeof (item as AgentConversationAnnotation).id === 'string'
    && typeof (item as AgentConversationAnnotation).anchor_kind === 'string'
    && typeof (item as AgentConversationAnnotation).anchor === 'object'
    && typeof (item as AgentConversationAnnotation).comment === 'string',
  )) : [];
}

function annotationFileLabel(annotation: AgentConversationAnnotation): string | undefined {
  if (annotation.anchor_kind !== 'WORKSPACE_FILE_RANGE') return undefined;
  const { path, selection } = annotation.anchor;
  if (typeof path !== 'string' || !selection || typeof selection !== 'object') return undefined;
  const range = selection as Record<string, unknown>;
  if (!['start_line', 'start_column', 'end_line', 'end_column'].every(key => typeof range[key] === 'number')) return undefined;
  const filename = path.split('/').filter(Boolean).at(-1) || path;
  return `${filename} · ${range.start_line}:${range.start_column}–${range.end_line}:${range.end_column}`;
}

function MessageAttachments({ attachments, references = [], workspaceReferences = [], annotations = [], onOpen, onOpenReference, onOpenWorkspaceReference, onOpenAnnotation }: {
  attachments: AgentAttachment[];
  references?: AgentConversationReference[];
  workspaceReferences?: AgentWorkspaceReference[];
  annotations?: AgentConversationAnnotation[];
  onOpen?: (attachment: AgentAttachment) => void;
  onOpenReference?: (reference: AgentConversationReference) => void;
  onOpenWorkspaceReference?: (reference: AgentWorkspaceReference) => void;
  onOpenAnnotation?: (annotation: AgentConversationAnnotation) => void;
}) {
  if (!attachments.length && !references.length && !workspaceReferences.length && !annotations.length) return null;
  return <div className="conversation-message-attachments" aria-label="消息附件">
    {attachments.map(attachment => <button
      type="button"
      key={attachment.path}
      className="conversation-message-attachment"
      title={`查看附件：${attachment.filename}`}
      onClick={() => onOpen?.(attachment)}
    >
      <FileText size={16}/><span><b>{attachment.filename}</b><small>{attachment.mime_type || '文件'}{attachmentSize(attachment.byte_size) ? ` · ${attachmentSize(attachment.byte_size)}` : ''}</small></span><Eye size={13}/>
    </button>)}
    {references.map((reference, index) => <button type="button" key={`${reference.event_id}:${reference.content}`} className="conversation-message-attachment conversation-message-reference" aria-label={`查看会话引用 ${index + 1}`} title="查看引用内容" onClick={() => onOpenReference?.(reference)}>
      <Quote size={16}/><span><b>{`会话引用 ${index + 1}`}</b><small>已添加到本条消息</small></span>
      <PanelRightOpen size={13}/>
    </button>)}
    {workspaceReferences.map(reference => <button type="button" key={`${reference.path}:${JSON.stringify(reference.selection ?? {})}`} className="conversation-message-attachment conversation-message-workspace-reference" title={reference.path} onClick={() => { onOpenWorkspaceReference?.(reference); window.dispatchEvent(new CustomEvent('flowweave:open-workspace-selection', { detail: reference })); }}>
      <FileText size={16}/><span><b>{reference.display_name}</b><small>{workspaceReferenceLabel(reference)}</small></span>
    </button>)}
    {annotations.map((annotation, index) => {
      const fileLabel = annotationFileLabel(annotation);
      const label = fileLabel ?? `会话引用 ${index + 1}`;
      return <button type="button" key={annotation.id} className="conversation-message-attachment conversation-message-annotation" title={fileLabel ?? '查看会话注释'} onClick={() => onOpenAnnotation?.(annotation)}>
        {fileLabel ? <FileText size={16}/> : <Quote size={16}/>}<span><b>{label}</b><small>{annotation.comment || (fileLabel ? '文件内容注释' : '会话文本注释')}</small></span><PanelRightOpen size={13}/>
      </button>;
    })}
  </div>;
}

function ConversationReferencePreview({ reference, onClose, onLocate }: {
  reference: AgentConversationReference;
  onClose: () => void;
  onLocate: () => void;
}) {
  useEscapeClose(onClose);
  return <div className="conversation-reference-preview-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="conversation-reference-preview" role="dialog" aria-modal="true" aria-label="会话引用内容">
      <header><div><span className="eyebrow">CONVERSATION REFERENCE</span><h2>所选文本</h2></div><button type="button" className="ghost" aria-label="关闭会话引用内容" onClick={onClose}>关闭</button></header>
      <pre>{reference.content || '引用内容不可用'}</pre>
      <footer><button type="button" className="secondary" onClick={onLocate}>定位原消息</button><button type="button" className="primary" onClick={onClose}>完成</button></footer>
    </section>
  </div>;
}

const ConversationMarkdown = lazy(() => import('./ConversationMarkdown').then(module => ({ default: module.ConversationMarkdown })));

function MessageMarkdown({ children, onOpenWorkspaceFile, onOpenImage }: { children: string; onOpenWorkspaceFile?: (href: string) => boolean; onOpenImage?: (src: string, alt?: string) => void }) {
  return <Suspense fallback={<div className="conversation-markdown-loading">正在渲染消息…</div>}><ConversationMarkdown onOpenWorkspaceFile={onOpenWorkspaceFile} onOpenImage={onOpenImage}>{children}</ConversationMarkdown></Suspense>;
}

interface CandidateOutput { fieldKey: string; artifactType: 'URL' | 'FILE'; value: string }

const OUTPUT_BLOCK_MARKER = '---FLOWWEAVE_OUTPUTS---';

interface CandidateOutputMessage {
  businessConclusion: string;
  outputs?: CandidateOutput[];
}

function candidateOutputMessage(content: string): CandidateOutputMessage {
  const marker = content.lastIndexOf(OUTPUT_BLOCK_MARKER);
  const businessConclusion = marker >= 0 ? content.slice(0, marker).trim() : '';
  let raw = (marker >= 0 ? content.slice(marker + OUTPUT_BLOCK_MARKER.length) : content).trim();
  if (raw.startsWith('```')) raw = raw.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  let value: unknown;
  try { value = JSON.parse(raw); } catch { return { businessConclusion: marker >= 0 ? businessConclusion : content }; }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { businessConclusion: marker >= 0 ? businessConclusion : content };
  const outputs = (value as Record<string, unknown>).outputs;
  if (!outputs || typeof outputs !== 'object' || Array.isArray(outputs)) return { businessConclusion: marker >= 0 ? businessConclusion : content };
  const parsed: CandidateOutput[] = [];
  for (const [fieldKey, rawOutput] of Object.entries(outputs)) {
    if (!rawOutput || typeof rawOutput !== 'object' || Array.isArray(rawOutput)) return { businessConclusion: marker >= 0 ? businessConclusion : content };
    const output = rawOutput as Record<string, unknown>;
    const artifactType = output.artifact_type;
    const outputValue = artifactType === 'URL' ? output.uri : artifactType === 'FILE' ? output.path : undefined;
    if ((artifactType !== 'URL' && artifactType !== 'FILE') || typeof outputValue !== 'string' || !outputValue.trim()) return { businessConclusion: marker >= 0 ? businessConclusion : content };
    if (artifactType === 'URL') {
      try { if (!['http:', 'https:'].includes(new URL(outputValue).protocol)) return { businessConclusion: marker >= 0 ? businessConclusion : content }; } catch { return { businessConclusion: marker >= 0 ? businessConclusion : content }; }
    }
    if (artifactType === 'FILE' && (
      outputValue.startsWith('/')
      || outputValue.includes('\\')
      || outputValue.split('/').some(part => !part || part === '.' || part === '..')
    )) return { businessConclusion: marker >= 0 ? businessConclusion : content };
    parsed.push({ fieldKey, artifactType, value: outputValue.trim() });
  }
  return { businessConclusion, outputs: parsed.length ? parsed : undefined };
}

function CandidateOutputReply({ outputs, onPreviewFile }: {
  outputs: CandidateOutput[];
  onPreviewFile?: (output: CandidateOutput) => void;
}) {
  return <section className="conversation-candidate-outputs" aria-label="Agent 候选交付物"><header><b>执行 Agent 已提交 {outputs.length} 个候选交付物</b><small>此卡片由平台从 Agent 的结构化输出中解析生成，不是 Agent 发送的独立消息；文件预览仅限当前节点的授权范围。</small></header><div>{outputs.map(output => {
    return <article key={output.fieldKey}>{output.artifactType === 'URL' ? <Link size={15}/> : <FileText size={15}/>}<span><b>{output.fieldKey}</b><small>{output.artifactType === 'URL' ? 'URL 候选产物' : '文件候选产物'}</small><p>{output.artifactType === 'URL' ? output.value : output.value.split('/').at(-1)}</p></span>{output.artifactType === 'URL' ? <a href={output.value} target="_blank" rel="noopener noreferrer"><ExternalLink size={13}/>打开</a> : onPreviewFile ? <button type="button" onClick={() => onPreviewFile(output)}><PanelRightOpen size={13}/>预览</button> : null}</article>;
  })}</div></section>;
}

const PAUSE_INTERRUPTION_CONTENT = 'Tool call interrupted before completion. The conversation was paused.';

function isPauseInterruptionEvent(event: OpenHandsConversationEvent): boolean {
  return event.event_type === 'ERROR'
    && String(event.payload.source_type ?? '') === 'AgentErrorEvent'
    && String(event.payload.content ?? '') === PAUSE_INTERRUPTION_CONTENT;
}

function isConversationTerminalError(event: OpenHandsConversationEvent): boolean {
  return event.event_type === 'ERROR'
    && String(event.payload.source_type ?? '') === 'ConversationErrorEvent';
}

function itemsFor(event: OpenHandsConversationEvent): Item[] {
  const content = typeof event.payload.content === 'string' ? event.payload.content : '';
  const thought = typeof event.payload.thought === 'string' ? event.payload.thought : '';
  const eventName = String(event.payload.event_name || event.event_type);
  if (event.event_type === 'MESSAGE') {
    const source = String(event.payload.source ?? '').toLowerCase();
    const isUser = source === 'user' || source === 'human';
    const displayContent = typeof event.payload.display_content === 'string'
      ? event.payload.display_content
      : content;
    if (isUser) return [{ event, kind: 'user', title: '', content: displayContent }];
    if (isOpenHandsAgentReply(event)) return [{ event, kind: 'assistant', title: '', content }];
    if (isOpenHandsEmptyResponseRecovery(event)) return [];
    // OpenHands persists an empty Agent Message immediately before its
    // environment corrective nudge. It is neither a reply nor a terminal
    // event. Other framework messages remain visible as process information,
    // but never masquerade as a final Agent reply.
    return content.trim() ? [{ event, kind: 'thought', title: eventName, content }] : [];
  }
  // THOUGHT is the native, non-terminal progress event. Render its safe
  // content as ordinary text, without exposing an implementation tool name.
  if (event.event_type === 'THOUGHT') {
    return [{ event, kind: 'thought', title: '', content: thought || content }];
  }
  if (event.event_type === 'CONDENSATION_REQUESTED') return [{ event, kind: 'condensation', title: '开始压缩上下文', content: '' }];
  if (event.event_type === 'CONDENSATION_COMPLETED') return [{ event, kind: 'condensation', title: '压缩完成', content: '' }];
  if (event.event_type === 'TOOL_CALL') return [{ event, kind: 'tool', title: eventName, content: thought || content }];
  // Its native observation only confirms that text was logged, so rendering it
  // as a generic tool result creates a redundant "Think · 已完成" row.
  if (event.event_type === 'TOOL_RESULT' && eventName === 'ThinkObservation') return [];
  if (event.event_type === 'TOOL_RESULT') return [{ event, kind: 'tool', title: eventName, content }];
  if (event.event_type === 'ERROR') {
    if (!isConversationTerminalError(event)) return [];
    return [{ event, kind: 'error', title: '本轮未能完成', content }];
  }
  if (event.event_type === 'COMPLETED') {
    // OpenHands has two formal final-response paths: an assistant MessageEvent
    // and FinishAction.message. A FinishAction may also carry top-level
    // commentary, so expand that one formal event into process + final UI rows.
    if (eventName !== 'FinishAction') return [];
    return [
      ...(thought ? [{ event, kind: 'thought' as const, title: '分析', content: thought }] : []),
      ...(content ? [{ event, kind: 'assistant' as const, title: '', content }] : []),
    ];
  }
  // STATE is transport progress rather than conversation content. Other empty
  // protocol frames are similarly excluded from the product transcript.
  return content ? [{ event, kind: 'thought', title: eventName, content }] : [];
}

function userAncestorId(
  event: OpenHandsConversationEvent,
  byId: Map<string, OpenHandsConversationEvent>,
): string | undefined {
  const visited = new Set<string>();
  let current: OpenHandsConversationEvent | undefined = event;
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    const source = String(current.payload.source ?? '').toLowerCase();
    if (current.event_type === 'MESSAGE' && (source === 'user' || source === 'human')) return current.id;
    const parentId: string | null | undefined = current.payload.parent_id;
    current = parentId ? byId.get(parentId) : undefined;
  }
  return undefined;
}

function isHistoricalAutoTitleError(
  event: OpenHandsConversationEvent,
  events: OpenHandsConversationEvent[],
): boolean {
  if (event.event_type !== 'ERROR' || event.payload.error_code !== 'NotFoundError') return false;
  // OpenHands 1.42 emitted this exact auxiliary title-generation failure as a
  // generic ConversationErrorEvent. It is only safe to suppress when a normal
  // assistant response is already present; other 404s remain visible.
  const detail = String(event.payload.content ?? '');
  const isKnownTitleProtocolFailure = detail.includes('litellm.NotFoundError')
    && detail.includes('OpenAIException')
    && detail.includes('Error code: 404');
  if (isKnownTitleProtocolFailure) {
    return events.some(candidate => {
      const assistantMessage = isOpenHandsAgentReply(candidate);
      const finishResponse = candidate.event_type === 'COMPLETED'
        && candidate.payload.event_name === 'FinishAction';
      return (assistantMessage || finishResponse) && Boolean(candidate.payload.content);
    });
  }
  const byId = new Map(events.map(candidate => [candidate.id, candidate]));
  const root = userAncestorId(event, byId);
  if (!root) return false;
  return events.some(candidate => {
    return isOpenHandsAgentReply(candidate)
      && userAncestorId(candidate, byId) === root;
  });
}

function turnsFor(events: OpenHandsConversationEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | undefined;
  const ordered = orderOpenHandsConversationEvents(events);
  for (const event of ordered) {
    if (isHistoricalAutoTitleError(event, ordered)) continue;
    for (const item of itemsFor(event)) {
      if (item.kind === 'user') {
        const renderKey = typeof item.event.payload._flowweave_render_key === 'string'
          ? item.event.payload._flowweave_render_key
          : item.event.id;
        current = { id: item.event.id, renderKey, user: item, activity: [] };
        turns.push(current);
        continue;
      }
      if (!current) {
        current = { id: item.event.id, renderKey: item.event.id, activity: [] };
        turns.push(current);
      }
      if (item.kind === 'assistant') current.assistant = item;
      else current.activity.push(item);
    }
  }
  return turns;
}

function fileChangesForTurn(
  events: OpenHandsConversationEvent[],
  turn: Turn,
): WorkspaceFileChange[] {
  if (!turn.user) return workspaceFileChanges(turn.activity.map(item => item.event));
  const byId = new Map(events.map(event => [event.id, event]));
  // A tool result can arrive after the main reply or after a nested task's
  // events have been projected. Its formal parent chain is the durable source
  // of turn ownership; the rendered activity list is only a presentation
  // order and must not determine whether a completed reply shows its changes.
  const ownedEvents = events.filter(event => userAncestorId(event, byId) === turn.user!.event.id);
  return workspaceFileChanges(ownedEvents.length ? ownedEvents : turn.activity.map(item => item.event));
}

function detailText(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, 500) : '';
}

function detailContent(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, 12_000) : '';
}

function workspacePath(value: string, workspaceRoot?: string | null): string {
  return workspaceRelativePath(value, workspaceRoot);
}

function workspaceRelativeText(value: string, workspaceRoot?: string | null): string {
  if (!workspaceRoot) return value;
  const containerPrefix = '/runtime/workspace/project/';
  const normalizedRoot = workspaceRoot.replaceAll('\\', '/');
  const relativeRoot = normalizedRoot.startsWith(containerPrefix) ? normalizedRoot.slice(containerPrefix.length) : normalizedRoot.replace(/^\/+/, '');
  const absoluteRoot = normalizedRoot.replace(/\/+$/, '');
  const prefixes = [absoluteRoot, containerPrefix + relativeRoot, '/' + relativeRoot]
    .filter(Boolean)
    .sort((left, right) => right.length - left.length);
  return prefixes.reduce((text, prefix) => text.split(prefix).join('.'), value);
}

function compactCommand(value: string, workspaceRoot?: string | null): string {
  const compact = workspaceRelativeText(value, workspaceRoot).replace(/\s+/g, ' ').trim();
  return compact.length > 110 ? `${compact.slice(0, 107)}...` : compact;
}

function messageSummary(value: string): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  if (!compact) return '空消息';
  return compact.length > 72 ? `${compact.slice(0, 69)}...` : compact;
}

function groupedActivities(items: Item[]): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  const actionsById = new Map<string, ActivityEntry>();
  const actionsByToolCall = new Map<string, ActivityEntry>();
  const condensationRequests = new Map<string, ActivityEntry>();
  for (const item of items) {
    if (item.kind === 'condensation' && item.event.event_type === 'CONDENSATION_REQUESTED') {
      condensationRequests.set(item.event.id, { id: item.event.id, item, action: item, results: [] });
      continue;
    }
    if (item.kind !== 'tool' || item.event.event_type !== 'TOOL_CALL') continue;
    const entry = { id: item.event.id, item, action: item, results: [] } satisfies ActivityEntry;
    actionsById.set(item.event.id, entry);
    const toolCallId = detailText(item.event.payload.tool_call_id);
    if (toolCallId) actionsByToolCall.set(toolCallId, entry);
  }
  const emitted = new Set<ActivityEntry>();
  for (const item of items) {
    if (item.kind === 'condensation') {
      if (item.event.event_type === 'CONDENSATION_REQUESTED') {
        const entry = condensationRequests.get(item.event.id)!;
        if (!emitted.has(entry)) { entries.push(entry); emitted.add(entry); }
        continue;
      }
      const requestId = detailText(item.event.payload.condensation_request_event_id) || detailText(item.event.payload.parent_id);
      const entry = requestId ? condensationRequests.get(requestId) : undefined;
      if (entry) {
        entry.results.push(item);
        if (!emitted.has(entry)) { entries.push(entry); emitted.add(entry); }
      } else entries.push({ id: item.event.id, item, results: [item] });
      continue;
    }
    if (item.kind === 'tool' && item.event.event_type === 'TOOL_CALL') {
      const entry = actionsById.get(item.event.id)!;
      if (!emitted.has(entry)) { entries.push(entry); emitted.add(entry); }
      continue;
    }
    if (item.kind === 'tool' && item.event.event_type === 'TOOL_RESULT') {
      const actionId = detailText(item.event.payload.action_id);
      const toolCallId = detailText(item.event.payload.tool_call_id);
      const entry = (actionId ? actionsById.get(actionId) : undefined)
        ?? (toolCallId ? actionsByToolCall.get(toolCallId) : undefined);
      if (entry) {
        entry.results.push(item);
        if (!emitted.has(entry)) { entries.push(entry); emitted.add(entry); }
        continue;
      }
    }
    entries.push({ id: item.event.id, item, results: item.event.event_type === 'TOOL_RESULT' ? [item] : [] });
  }
  return entries;
}

function progressText(item: Item): string {
  return item.content.trim().slice(0, 2_000);
}

function isNativeOperation(entry: ActivityEntry): boolean {
  return ['TerminalAction', 'FileEditorAction'].includes(String(entry.action?.event.payload.event_name ?? ''));
}

function entryHasProgress(entry: ActivityEntry): boolean {
  return entry.item.kind === 'thought' || Boolean(isNativeOperation(entry) && progressText(entry.action!));
}

function actionBelongsToProgress(entry: ActivityEntry, progress: Item, ownedEventIds: ReadonlySet<string>): boolean {
  const action = entry.action;
  if (!action || !isNativeOperation(entry)) return false;
  if (action.event.id === progress.event.id) return true;

  const progressResponseId = detailText(progress.event.payload.llm_response_id);
  const actionResponseId = detailText(action.event.payload.llm_response_id);
  if (progressResponseId && actionResponseId) return progressResponseId === actionResponseId;
  return ownedEventIds.has(detailText(action.event.payload.parent_id));
}

function activityRows(entries: ActivityEntry[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  for (let index = 0; index < entries.length;) {
    const entry = entries[index];
    if (!entryHasProgress(entry)) {
      rows.push({ kind: 'entry', entry });
      index += 1;
      continue;
    }

    const progress = entry.item;
    const grouped: ActivityEntry[] = [];
    const ownedEventIds = new Set([progress.event.id]);
    let cursor = entry.item.kind === 'thought' ? index + 1 : index;
    while (cursor < entries.length) {
      const candidate = entries[cursor];
      if (candidate.item.kind === 'thought' || !actionBelongsToProgress(candidate, progress, ownedEventIds)) break;
      grouped.push(candidate);
      ownedEventIds.add(candidate.action!.event.id);
      for (const result of candidate.results) ownedEventIds.add(result.event.id);
      cursor += 1;
    }
    if (!grouped.length) {
      rows.push({ kind: 'entry', entry });
      index += 1;
      continue;
    }
    rows.push({
      kind: 'progress-group',
      group: { id: progress.event.id, progress, entries: grouped },
    });
    index = cursor;
  }
  return rows;
}

interface ActivityPresentation {
  title: string;
  status: string;
  thought?: string;
  command?: string;
  path?: string;
  operation?: string;
  fileOperation?: FileOperationKind;
  fileKind?: FileKind;
  exitCode?: string;
  actionDetails?: Record<string, unknown>;
  resultDetails?: Record<string, unknown>;
  resultTimestamp?: string;
}

type ToolVisualKind = 'terminal' | 'file' | 'task-tracker' | 'skill' | 'browser' | 'mcp' | 'subagent' | 'workflow' | 'generic';
type FileOperationKind = 'read' | 'create' | 'edit' | 'undo' | 'generic';
type FileKind = 'code' | 'config' | 'data' | 'markdown' | 'text' | 'generic';

function fileOperationKind(command: string): FileOperationKind {
  if (command === 'view') return 'read';
  if (command === 'create' || command === 'write') return 'create';
  if (command === 'undo_edit') return 'undo';
  if (command === 'str_replace' || command === 'insert' || command === 'append') return 'edit';
  return 'generic';
}

function fileKindForPath(path?: string): FileKind {
  const fileName = path?.replaceAll('\\', '/').split('/').at(-1)?.toLowerCase() ?? '';
  if (/^(\.env|\.gitignore|\.dockerignore|\.npmrc|\.editorconfig)(\..*)?$/.test(fileName)
    || /\.(properties|ini|toml|conf|cfg)$/.test(fileName)) return 'config';
  if (/\.(json|jsonc|yaml|yml|xml|csv)$/.test(fileName)) return 'data';
  if (/\.(md|mdx|rst)$/.test(fileName)) return 'markdown';
  if (/\.(txt|log)$/.test(fileName)) return 'text';
  if (/\.(c|cc|cpp|cxx|cs|go|h|java|js|jsx|kt|php|py|rb|rs|sh|sql|swift|ts|tsx|vue|svelte|bash|zsh)$/.test(fileName)) return 'code';
  return 'generic';
}

function fileToolIcon(presentation: ActivityPresentation) {
  if (presentation.fileOperation === 'create') return FilePlus2;
  if (presentation.fileOperation === 'edit' || presentation.fileOperation === 'undo') return FilePenLine;
  if (presentation.fileKind === 'code') return FileCode2;
  if (presentation.fileKind === 'config') return FileCog;
  if (presentation.fileKind === 'data') return FileJson;
  if (presentation.fileKind === 'markdown') return FileType2;
  if (presentation.fileOperation === 'read') return Eye;
  return FileText;
}

function toolVisualPresentation(eventName: string, toolName?: string): ToolVisualKind {
  const normalizedEventName = eventName.toLowerCase();
  const normalizedToolName = toolName?.toLowerCase() ?? '';
  if (normalizedEventName.includes('terminal')) return 'terminal';
  if (normalizedEventName.includes('fileeditor')) return 'file';
  if (normalizedEventName.includes('tasktracker')) return 'task-tracker';
  if (normalizedEventName.includes('invokeskill')) return 'skill';
  if (normalizedEventName.includes('browser')) return 'browser';
  if (normalizedEventName.includes('mcp') || normalizedToolName.startsWith('mcp_')) return 'mcp';
  if (normalizedEventName.includes('workflow') || normalizedToolName === 'workflow') return 'workflow';
  if (normalizedEventName === 'taskaction' || normalizedEventName === 'taskobservation') return 'subagent';
  return 'generic';
}

function activityPresentation(entry: ActivityEntry, active: boolean, workspaceRoot?: string | null, paused = false, parentFailed = false): ActivityPresentation {
  const item = entry.action ?? entry.item;
  if (item.kind === 'condensation') return { title: entry.results.some(result => result.event.event_type === 'CONDENSATION_COMPLETED') || item.event.event_type === 'CONDENSATION_COMPLETED' ? '压缩完成' : '开始压缩上下文', status: '' };
  if (item.kind === 'thought') {
    return {
      title: active ? '正在分析' : '分析',
      status: active ? '分析中' : '已完成',
      thought: item.content.slice(0, 2_000) || undefined,
    };
  }
  if (item.kind === 'error') return { title: '执行遇到问题', status: '失败' };
  const details = item.event.payload.details ?? {};
  const result = entry.results.at(-1);
  const resultDetails = result?.event.payload.details ?? {};
  const eventName = String(item.event.payload.event_name ?? '');
  const resultName = String(result?.event.payload.event_name ?? '');
  const path = detailText(details.path) || detailText(resultDetails.path) || detailText(details.file_path) || detailText(details.filename);
  const command = detailContent(details.command) || detailContent(resultDetails.command);
  const completed = entry.results.length > 0 || item.event.event_type === 'TOOL_RESULT';
  const failed = Boolean(resultDetails.is_error) || (typeof resultDetails.exit_code === 'number' && resultDetails.exit_code !== 0);
  const thought = entry.action?.content ? entry.action.content.slice(0, 2_000) : undefined;
  const summary = detailText(entry.action?.event.payload.summary);
  const actionTitle = (fallback: string) => summary || fallback;
  const exitCode = typeof resultDetails.exit_code === 'number' ? String(resultDetails.exit_code) : undefined;
  if (eventName === 'TerminalAction' || eventName === 'TerminalObservation' || resultName === 'TerminalObservation') {
    const verb = failed ? '运行失败' : completed ? '已运行' : '正在运行';
    return {
      title: command ? `${verb} ${compactCommand(command, workspaceRoot)}` : actionTitle(completed ? '命令已执行' : '正在运行命令'),
      status: failed ? '终端 · 失败' : completed ? '终端 · 已完成' : '终端',
      command: workspaceRelativeText(command, workspaceRoot), thought, exitCode, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'FileEditorAction' || eventName === 'FileEditorObservation' || resultName === 'FileEditorObservation') {
    const operation = command.toLowerCase();
    const fileOperation = fileOperationKind(operation);
    const fileKind = fileKindForPath(path);
    const verb = operation === 'view' ? (failed ? '读取失败' : completed ? '已读取' : '正在读取')
      : ['create', 'write'].includes(operation) ? (failed ? '创建失败' : completed ? '已创建' : '正在创建')
        : operation === 'undo_edit' ? (failed ? '撤销失败' : completed ? '已撤销编辑' : '正在撤销编辑')
          : ['str_replace', 'insert', 'append'].includes(operation) ? (failed ? '编辑失败' : completed ? '已编辑' : '正在编辑')
            : failed ? '文件操作失败' : completed ? '已完成文件操作' : '正在处理文件';
    const displayPath = path ? workspacePath(path, workspaceRoot) : '';
    return {
      title: displayPath ? `${verb} ${displayPath}` : actionTitle(verb),
      status: failed ? '文件编辑器 · 失败' : completed ? '文件编辑器 · 已完成' : '文件编辑器',
      path: displayPath || undefined, operation: workspaceRelativeText(command, workspaceRoot) || undefined, fileOperation, fileKind, thought, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'TaskTrackerAction') {
    return {
      title: completed ? (command === 'plan' ? '任务列表已更新' : '任务列表已读取') : actionTitle(command === 'plan' ? '正在更新任务列表' : '正在查看任务列表'),
      status: completed ? '任务跟踪 · 已完成' : '任务跟踪', thought, actionDetails: details, resultDetails, resultTimestamp: typeof result?.event.payload.timestamp === 'string' ? result.event.payload.timestamp : typeof item.event.payload.timestamp === 'string' ? item.event.payload.timestamp : undefined,
    };
  }
  if (eventName === 'TaskTrackerObservation') {
    return {
      title: command === 'plan' ? '任务列表已更新' : '任务列表已读取',
      status: completed ? '任务跟踪 · 已完成' : '任务跟踪', resultTimestamp: typeof item.event.payload.timestamp === 'string' ? item.event.payload.timestamp : undefined,
    };
  }
  if (eventName === 'InvokeSkillAction') return { title: completed ? `${actionTitle('技能调用')} · 已完成` : actionTitle('正在使用已启用技能'), status: completed ? '技能 · 已完成' : '技能', thought, actionDetails: details, resultDetails };
  if (eventName === 'InvokeSkillObservation') return { title: '技能调用已完成', status: completed ? '已完成' : '处理中' };
  if (eventName.includes('Browser')) return { title: completed ? `${actionTitle('浏览器操作')} · 已完成` : actionTitle('正在操作浏览器'), status: completed ? '浏览器 · 已完成' : '浏览器', thought, actionDetails: details, resultDetails };
  if (eventName.includes('MCP')) return { title: completed ? `${actionTitle('MCP 工具调用')} · 已完成` : actionTitle('正在调用 MCP 工具'), status: completed ? 'MCP · 已完成' : 'MCP', thought, actionDetails: details, resultDetails };
  if (eventName.includes('Workflow')) return { title: completed ? `${actionTitle('工作流')} · 已完成` : actionTitle('正在执行工作流'), status: completed ? '工作流 · 已完成' : '工作流', thought, actionDetails: details, resultDetails };
  if (eventName === 'TaskAction') {
    const runtimeTask = item.event.payload.runtime_task;
    const agentType = typeof runtimeTask?.subagent_type === 'string' && runtimeTask.subagent_type.trim()
      ? runtimeTask.subagent_type.trim()
      : 'general-purpose';
    const description = typeof runtimeTask?.description === 'string' ? runtimeTask.description.trim() : '';
    const label = description || actionTitle(`子智能体 ${agentType}`);
    const interrupted = paused && !completed;
    const unavailable = parentFailed && !completed;
    return {
      title: completed ? `子智能体 ${agentType} · ${label} · 已完成` : interrupted ? `子智能体 ${agentType} · ${label} · 已暂停` : unavailable ? `子智能体 ${agentType} · ${label} · 主会话异常结束` : `子智能体 ${agentType} · ${label}`,
      status: completed ? '子智能体 · 已完成' : interrupted ? '子智能体 · 已暂停，结果未返回' : unavailable ? '子智能体 · 主会话异常结束，结果未返回' : '子智能体 · 运行中',
      thought, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'TaskObservation') return { title: '子任务已完成', status: completed ? '已完成' : '处理中' };
  const toolName = eventName.replace(/(?:Action|Observation)$/, '') || '工具';
  return {
    title: completed ? `${actionTitle(toolName)} · 已完成` : actionTitle(`正在使用 ${toolName}`),
    status: completed ? '工具 · 已完成' : '工具',
    thought, actionDetails: details, resultDetails,
  };
}

function displayDetails(details: Record<string, unknown>, workspaceRoot?: string | null): string {
  const visible = Object.fromEntries(Object.entries(details).filter(([key]) => !['content', 'old_content', 'new_content'].includes(key)));
  return Object.keys(visible).length ? workspaceRelativeText(JSON.stringify(visible, null, 2), workspaceRoot).slice(0, 12_000) : '';
}

function ToolDetailPanel({ presentation, eventName, toolName, toolVisual, results, workspaceRoot }: {
  presentation: ActivityPresentation;
  eventName: string;
  toolName?: string;
  toolVisual: ToolVisualKind;
  results: Item[];
  workspaceRoot?: string | null;
}) {
  const details = presentation.actionDetails ?? {};
  const resultDetails = presentation.resultDetails ?? {};
  const isTerminal = toolVisual === 'terminal';
  const isFile = toolVisual === 'file';
  const isTaskTracker = toolVisual === 'task-tracker';
  const hasResultOutput = results.some(result => typeof result.content === 'string' && result.content.trim().length > 0);
  const hasDetail = Boolean(
    isTaskTracker || presentation.command || hasResultOutput || presentation.exitCode
    || Object.keys(details).length || Object.keys(resultDetails).length,
  );
  if (!hasDetail) return null;
  return <div className="conversation-tool-detail-panel"><ToolDetailContent
    details={details}
    resultDetails={resultDetails}
    results={results}
    presentation={presentation}
    isTerminal={isTerminal}
    isFile={isFile}
    isTaskTracker={isTaskTracker}
    eventName={eventName}
    toolName={toolName}
    workspaceRoot={workspaceRoot}
  /></div>;
}

function ToolDetailContent({ details, resultDetails, results, presentation, isTerminal, isFile, isTaskTracker, eventName, toolName, workspaceRoot }: {
  details: Record<string, unknown>;
  resultDetails: Record<string, unknown>;
  results: Item[];
  presentation: ActivityPresentation;
  isTerminal: boolean;
  isFile: boolean;
  isTaskTracker: boolean;
  eventName: string;
  toolName?: string;
  workspaceRoot?: string | null;
}) {
  const taskSnapshot = isTaskTracker ? taskListSnapshot(details, resultDetails, presentation.resultTimestamp) : undefined;
  const structured = displayDetails(details, workspaceRoot);
  const structuredResult = displayDetails(resultDetails, workspaceRoot);
  const fileText = detailContent(details.file_text);
  const oldText = detailContent(details.old_str);
  const newText = detailContent(details.new_str);
  const output = workspaceRelativeText(results.map(result => detailContent(result.content)).filter(Boolean).join('\n\n').slice(0, 12_000), workspaceRoot);
  return <>
      <b>{isTaskTracker ? '任务列表' : isTerminal ? 'Shell' : isFile ? '文件操作' : '工具调用'}</b>
      <dl className="conversation-tool-provenance">
        <dt>事件类型</dt><dd>{eventName || '未知事件'}</dd>
        {toolName && <><dt>工具名</dt><dd>{toolName}</dd></>}
      </dl>
      {taskSnapshot && <>
        <div className="conversation-task-list-summary">
          <span>{taskSnapshot.command === 'plan' ? '已更新的计划' : '当前计划快照'}</span>
          <small>{`${taskSnapshot.items.filter(item => item.status === 'done').length} / ${taskSnapshot.items.length} 已完成`}</small>
        </div>
        <TaskListItems items={taskSnapshot.items} source={taskSnapshot.timestamp ? `OpenHands 原生任务事件 · ${formatMessageTime(taskSnapshot.timestamp)}` : 'OpenHands 原生任务事件'}/>
      </>}
      {isTerminal && presentation.command && <pre><code>{`$ ${presentation.command}`}</code></pre>}
      {isFile && <dl>
        {presentation.operation && <><dt>操作</dt><dd>{presentation.operation}</dd></>}
        {presentation.path && <><dt>路径</dt><dd>{presentation.path}</dd></>}
        {Array.isArray(details.view_range) && <><dt>行范围</dt><dd>{details.view_range.join(' - ')}</dd></>}
        {typeof details.insert_line === 'number' && <><dt>插入行</dt><dd>{details.insert_line}</dd></>}
      </dl>}
      {fileText && <><small>写入内容</small><pre><code>{fileText}</code></pre></>}
      {oldText && <><small>替换前</small><pre><code>{oldText}</code></pre></>}
      {newText && <><small>替换后</small><pre><code>{newText}</code></pre></>}
      {!isTerminal && !isFile && !isTaskTracker && structured && <><small>原始操作</small><pre><code>{structured}</code></pre></>}
      {!isTaskTracker && output && <><small>执行结果</small><pre><code>{output}</code></pre></>}
      {!isTerminal && !isFile && !isTaskTracker && structuredResult && <><small>结果信息</small><pre><code>{structuredResult}</code></pre></>}
      {presentation.exitCode && <small>退出码 {presentation.exitCode}</small>}
    </>;
}

function TaskTrackerCard({ entry, presentation, running }: { entry: ActivityEntry; presentation: ActivityPresentation; running: boolean }) {
  const action = entry.action ?? entry.item;
  const result = entry.results.at(-1);
  const snapshot = taskListSnapshot(
    action.event.payload.details ?? {},
    result?.event.payload.details ?? {},
    result?.event.payload.timestamp ?? action.event.payload.timestamp,
  );
  const completed = snapshot?.items.filter(task => task.status === 'done').length ?? 0;
  const loading = !result && action.event.event_type === 'TOOL_CALL';
  const progress = snapshot ? `${completed} / ${snapshot.items.length} 已完成` : undefined;
  const status = loading ? '正在更新' : [snapshot?.command === 'plan' ? '已更新' : '当前快照', progress].filter(Boolean).join(' · ');
  return <details className={`conversation-activity-row tool conversation-tool-detail task-tracker${running ? ' running' : ''}`} aria-label={`任务列表：${presentation.title}`}>
    <summary><ClipboardList size={13}/><div><b>{presentation.title}</b><small>{status}</small></div><ChevronRight className="conversation-expand-arrow" size={12}/></summary>
    <div className="conversation-tool-detail-panel conversation-task-tracker-body">
      {snapshot ? <><div className="conversation-task-list-summary"><span>{snapshot.command === 'plan' ? '任务清单' : '任务清单快照'}</span><small>{progress}</small></div><TaskListItems items={snapshot.items} source={snapshot.timestamp ? `OpenHands 原生任务事件 · ${formatMessageTime(snapshot.timestamp)}` : 'OpenHands 原生任务事件'}/></> : <p className="conversation-task-tracker-note">正在读取任务清单…</p>}
    </div>
  </details>;
}

function SkillLoadRow({ entry, running }: { entry: ActivityEntry; running: boolean }) {
  const action = entry.action ?? entry.item;
  const result = entry.results.at(-1);
  const actionSkill = action.event.payload.runtime_skill;
  const resultSkill = result?.event.payload.runtime_skill;
  const skillName = resultSkill?.skill_name || actionSkill?.skill_name
    || detailText(result?.event.payload.details?.skill_name) || detailText(action.event.payload.details?.name) || '未命名 Skill';
  const phase = resultSkill?.phase ?? actionSkill?.phase ?? (result ? 'LOADED' : 'INVOKED');
  const failed = phase === 'ERROR' || result?.event.payload.details?.is_error === true;
  const status = failed ? '加载失败' : phase === 'LOADED' ? '已加载' : '加载中';
  return <article className={`conversation-activity-row tool skill-load${failed ? ' error' : running ? ' running' : ''}`} aria-label={`加载 Skill：${skillName}`}>
    <BookOpen size={13}/><div><b>{`加载 Skill ${skillName}`}</b><small>{status}</small></div>
  </article>;
}

function eventTime(item?: Item): number | undefined {
  return parsedEventTime(item?.event.payload.timestamp);
}

function parsedEventTime(raw: unknown): number | undefined {
  return parseOpenHandsEventTime(raw);
}

function turnProcessBlocks(
  items: Item[],
  startedAt: number | undefined,
  finishedAt: number | undefined,
  active: boolean,
): TurnProcessBlock[] {
  return [{ kind: 'activity', id: 'activity-0', items, startedAt, finishedAt, active }];
}

/** Format the wall-clock time attached to an actual OpenHands message event. */
function formatMessageTime(raw: unknown): string | undefined {
  const value = parseOpenHandsEventTime(raw);
  if (value === undefined) return undefined;
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(value);
}

function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}小时${minutes ? `${minutes}分钟` : ''}${remainder ? `${remainder}秒` : ''}`;
  if (minutes) return `${minutes}分钟${remainder ? `${remainder}秒` : ''}`;
  return `${remainder}秒`;
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Some embedded or permission-restricted browsers still allow the
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

function elementForNode(node: Node | null): Element | null {
  if (node instanceof Element) return node;
  return node?.parentElement ?? null;
}

function isolatedUserSelection(selection: Selection): { content: HTMLElement; text: string } | undefined {
  if (!selection.rangeCount) return undefined;
  const anchor = elementForNode(selection.anchorNode);
  const focus = elementForNode(selection.focusNode);
  const content = anchor?.closest<HTMLElement>('.conversation-message.user .conversation-message-content');
  if (!content || !focus) return undefined;
  const range = selection.getRangeAt(0);
  if (content.contains(range.startContainer) && content.contains(range.endContainer)) {
    return { content, text: selection.toString() };
  }
  // A browser selection that starts in a user bubble may accidentally continue
  // into the following turn as the surface updates. Keep the copy operation
  // faithful to the message the user started selecting, not its descendants.
  return { content, text: content.innerText };
}

function elapsedSeconds(startedAt: number | undefined, finishedAt: number | undefined): number | undefined {
  if (startedAt === undefined || finishedAt === undefined) return undefined;
  return Math.max(0, (finishedAt - startedAt) / 1000);
}

function LiveElapsed({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <span className="conversation-live-elapsed">已耗时 {formatDuration(Math.max(0, (now - startedAt) / 1000))}</span>;
}

function activeToolLabel(eventName: string, toolName?: string, summary?: string, details?: Record<string, unknown>): string {
  const description = typeof details?.description === 'string' ? details.description.trim() : '';
  const explicitTool = summary?.trim() || toolName?.trim();
  if (eventName.includes('Terminal')) return '正在后台执行命令';
  if (eventName.includes('FileEditor')) return '正在处理文件';
  if (eventName.includes('Browser')) return '正在执行浏览器操作';
  if (eventName.includes('MCP')) return '正在调用 MCP 工具';
  if (eventName.includes('Workflow')) return '正在执行工作流';
  if (eventName.includes('Skill')) return '正在加载 Skill';
  if (eventName.includes('Task')) return description ? `子智能体正在执行：${description}` : '子智能体正在执行';
  const normalized = explicitTool || eventName.replace(/(?:Action|Observation)$/, '');
  return normalized && normalized !== 'TOOL_CALL' ? `正在执行 ${normalized}` : '正在执行工具';
}

function activeActivityLabel(entries: ActivityEntry[], requestSubmitting: boolean): string {
  if (requestSubmitting) return '正在提交消息';
  const pendingTool = [...entries].reverse().find(entry => entry.action?.kind === 'tool' && entry.results.length === 0)?.action;
  if (pendingTool) return activeToolLabel(
    String(pendingTool.event.payload.event_name ?? ''),
    typeof pendingTool.event.payload.tool_name === 'string' ? pendingTool.event.payload.tool_name : undefined,
    typeof pendingTool.event.payload.summary === 'string' ? pendingTool.event.payload.summary : undefined,
    pendingTool.event.payload.details,
  );
  const latest = entries.at(-1);
  if (latest?.item.kind === 'condensation' && latest.item.event.event_type === 'CONDENSATION_REQUESTED' && latest.results.length === 0) return '正在压缩上下文';
  return '正在思考';
}

type ConversationConnectionState = 'checking' | 'connected' | 'recovering' | 'unavailable';

function staleActivityLabel(
  fallback: string,
  monitoring: AgentActivitySummary | undefined,
  connectionState: ConversationConnectionState,
): string {
  if (connectionState === 'recovering') return '实时输出连接中断，正在恢复并补读会话事件';
  if (connectionState === 'checking' && !monitoring?.possibly_stuck) return '正在建立 OpenHands 实时输出连接';
  if (!monitoring?.possibly_stuck) return fallback;
  if (connectionState === 'unavailable') return '暂时无法读取 OpenHands 会话状态，正在重试连接';
  if (connectionState === 'checking') return '正在检查 OpenHands 会话连接';
  return '后台长时间未产生可确认进展。可暂停后继续以重新建立调用。';
}

function retryLabel(status: ModelRetryStatus): string {
  if (status.subject === 'condensation') return '↳ 上下文压缩未完成，本轮已停止';
  const isModelFailure = status.subject !== 'execution';
  const prefix = isModelFailure ? (status.modelRole === 'fallback' ? '备用模型' : '模型') : '本轮执行';
  const reason: Record<string, string> = {
    timeout: '响应超时', connection: '连接失败', service_unavailable: '服务暂不可用',
    empty_response: '返回空响应', rate_limit: '受到速率限制', auth: '凭据无效',
    quota: '额度不足', config: '配置不兼容', context_limit: '上下文超限',
    content_policy: '内容安全策略拒绝', internal: '内部异常', unknown: '调用失败',
  };
  const suffix = status.attempt !== undefined && status.maxAttempts !== undefined
    ? ` ${status.attempt}/${status.maxAttempts}`
    : '';
  if (status.final) {
    return isModelFailure
      ? `↳ ${prefix}${reason[status.failureKind] ?? '调用失败'}，本轮已停止${suffix}`
      : `↳ ${prefix}失败，本轮已停止`;
  }
  if (status.failureKind === 'connection') return `↳ 正在重新连接${prefix}服务${suffix}`;
  return `↳ ${prefix}${reason[status.failureKind] ?? '调用失败'}，正在重试${suffix}`;
}

function RetryStatus({ status }: { status: ModelRetryStatus }) {
  const detail: Record<string, string> = {
    timeout: '模型响应超时。', connection: '无法连接模型网关。', service_unavailable: '模型服务返回了暂时不可用的响应。',
    empty_response: '模型没有返回完整的可用响应。', rate_limit: '模型服务暂时限制了请求速率。', auth: '模型凭据无效或权限不足。',
    quota: '模型账户额度不足。', config: '模型或请求参数不兼容。', context_limit: '请求超过模型上下文窗口。',
    content_policy: '模型内容安全策略拒绝了请求。', internal: '模型调用发生内部异常。', unknown: '模型调用发生未知异常。',
    no_eligible_events: '没有可安全压缩的事件区间。', insufficient_progress: '可压缩范围不足以满足最小进度要求。',
    summary_model_failed: '上下文摘要模型调用未完成。', condensation_unknown: '上下文压缩未能完成。',
  };
  return <details className={`conversation-model-retry${status.final ? ' final' : ''}`}>
    <summary role="status" aria-label={retryLabel(status)}><ChevronRight className="conversation-expand-arrow" size={13}/><span>{retryLabel(status)}</span>{!status.final && <span className="conversation-turn-status-dots" aria-hidden="true"><i/><i/><i/></span>}</summary>
    <p>{status.subject === 'execution' ? '执行过程中发生了不可恢复错误。' : detail[status.failureKind] ?? detail.unknown}{status.errorCode ? ` · 错误码：${status.errorCode}` : ''}</p>
  </details>;
}

function CurrentTurnStatus({ items, requestSubmitting, statusOverride, modelRetryStatus, monitoring, connectionState = 'connected' }: {
  items: Item[];
  requestSubmitting: boolean;
  statusOverride?: string;
  modelRetryStatus?: ModelRetryStatus;
  monitoring?: AgentActivitySummary;
  connectionState?: ConversationConnectionState;
}) {
  if (modelRetryStatus) return <RetryStatus status={modelRetryStatus}/>;
  const activityLabel = activeActivityLabel(groupedActivities(items), requestSubmitting);
  const label = statusOverride ?? (requestSubmitting
    ? activityLabel
    : staleActivityLabel(activityLabel, monitoring, connectionState));
  const stalled = !statusOverride && !requestSubmitting && monitoring?.possibly_stuck
    && connectionState === 'connected';
  return <div className={`conversation-turn-status${stalled ? ' stalled' : ''}`} role="status" aria-label={label}>
    {stalled && <CircleAlert className="conversation-turn-status-alert" role="img" aria-label="会话正在运行但后台长时间未产生可确认进展" size={14}/>}
    <span>{label}</span>
    {!stalled && <span className="conversation-turn-status-dots" aria-hidden="true"><i/><i/><i/></span>}
  </div>;
}

function taskAvatarStatus(entry: ActivityEntry, item: Item, paused = false, parentFailed = false): 'running' | 'paused' | 'completed' | 'error' {
  const phases = [item.event, ...entry.results.map(result => result.event)]
    .map(event => event.payload.runtime_task?.phase);
  if (phases.includes('ERROR')) return 'error';
  if (phases.includes('COMPLETED')) return 'completed';
  if (parentFailed) return 'error';
  return paused ? 'paused' : 'running';
}

function ActivityEntryRow({ entry, active, paused = false, parentFailed = false, hideThought = false, avatarSlots, workspaceRoot }: {
  entry: ActivityEntry;
  active: boolean;
  paused?: boolean;
  parentFailed?: boolean;
  hideThought?: boolean;
  avatarSlots: ReadonlyMap<string, SubagentAvatarSlot>;
  workspaceRoot?: string | null;
}) {
  const item = entry.action ?? entry.item;
  const Icon = item.kind === 'error' ? CircleAlert : item.kind === 'thought' || item.kind === 'condensation' ? Sparkles : Wrench;
  const eventName = String(item.event.payload.event_name ?? '');
  const toolName = detailText(item.event.payload.tool_name);
  const toolVisual = toolVisualPresentation(eventName, toolName);
  const avatarSlot = eventName === 'TaskAction' || eventName === 'TaskObservation'
    ? subagentAvatarSlotForEvent(item.event, avatarSlots)
    : undefined;
  const presentation = activityPresentation(entry, active, workspaceRoot, paused, parentFailed);
  const ToolIcon = toolVisual === 'terminal' ? SquareTerminal : toolVisual === 'file' ? fileToolIcon(presentation) : toolVisual === 'mcp' ? PlugZap : toolVisual === 'workflow' ? Workflow : Icon;
  const taskAvatar = avatarSlot && <SubagentAvatar slot={avatarSlot} status={taskAvatarStatus(entry, item, paused, parentFailed)} size={13}/>;
  const toolDetail = item.kind === 'tool'
    ? <ToolDetailPanel presentation={presentation} eventName={eventName} toolName={toolName || undefined} toolVisual={toolVisual} results={entry.results} workspaceRoot={workspaceRoot}/>
    : null;
  const condensationRunning = active && !paused && !parentFailed && item.kind === 'condensation' && item.event.event_type === 'CONDENSATION_REQUESTED' && entry.results.length === 0;
  const isNativeThink = item.event.event_type === 'THOUGHT';
  const referenceableThought = item.kind === 'thought' || (item.kind === 'tool' && Boolean(presentation.thought));
  const thoughtAttributes = referenceableThought ? { 'data-conversation-event-id': item.event.id } : {};
  const toolRunning = active && !paused && !parentFailed && entry.results.length === 0 && item.event.event_type === 'TOOL_CALL';
  if (item.kind === 'thought') return <article {...thoughtAttributes} className={`conversation-activity-row thought${isNativeThink ? ' native-think' : ''}`}>
    <MessageMarkdown>{presentation.thought ?? item.content}</MessageMarkdown>
  </article>;
  if (item.kind === 'condensation') return <article className={`conversation-activity-row tool condensation${condensationRunning ? ' running' : ''}`} role="status" aria-label={presentation.title}>
    <Sparkles size={13}/><div><b>{presentation.title}</b></div>
  </article>;
  if (eventName === 'TaskTrackerAction' || eventName === 'TaskTrackerObservation') return <div className={`conversation-tool-entry semantic tool-${toolVisual}`}>
    {!hideThought && presentation.thought && <article {...thoughtAttributes} className={`conversation-activity-row thought tool-thought tool-${toolVisual}`}><MessageMarkdown>{presentation.thought}</MessageMarkdown></article>}
    <TaskTrackerCard entry={entry} presentation={presentation} running={toolRunning}/>
  </div>;
  if (eventName === 'InvokeSkillAction' || eventName === 'InvokeSkillObservation') return <div className={`conversation-tool-entry semantic tool-${toolVisual}`}>
    {!hideThought && presentation.thought && <article {...thoughtAttributes} className={`conversation-activity-row thought tool-thought tool-${toolVisual}`}><MessageMarkdown>{presentation.thought}</MessageMarkdown></article>}
    <SkillLoadRow entry={entry} running={toolRunning}/>
  </div>;
  if (item.kind === 'tool' && toolDetail) return <div className={`conversation-tool-entry tool-${toolVisual}`}>
    {!hideThought && presentation.thought && <article {...thoughtAttributes} className={`conversation-activity-row thought tool-thought tool-${toolVisual}`}>
      <MessageMarkdown>{presentation.thought}</MessageMarkdown>
    </article>}
    <details className={`conversation-activity-row tool conversation-tool-detail tool-${toolVisual}${toolRunning ? ' running' : ''}`} data-tool-kind={toolVisual} data-file-operation={toolVisual === 'file' ? presentation.fileOperation : undefined} data-file-kind={toolVisual === 'file' ? presentation.fileKind : undefined}>
      <summary aria-label={`查看执行详情：${presentation.title}`}>{taskAvatar ?? <ToolIcon size={13}/>}<div><b title={presentation.title}>{presentation.title}</b></div><ChevronRight className="conversation-expand-arrow" size={12}/></summary>
      {toolDetail}
    </details>
  </div>;
  return <article className={`conversation-activity-row ${item.kind}`}>
    {taskAvatar ?? <ToolIcon size={13}/>}<div className="conversation-activity-content"><b title={presentation.title}>{presentation.title}</b><small>{presentation.status}</small>
      {presentation.thought && <span className="conversation-activity-thought"><MessageMarkdown>{presentation.thought}</MessageMarkdown></span>}
    </div>
  </article>;
}

function ProgressActivity({ group, active, paused, parentFailed, avatarSlots, workspaceRoot }: {
  group: ProgressActivityGroup;
  active: boolean;
  paused: boolean;
  parentFailed: boolean;
  avatarSlots: ReadonlyMap<string, SubagentAvatarSlot>;
  workspaceRoot?: string | null;
}) {
  const pendingEntries = group.entries.filter(entry => entry.action && entry.results.length === 0);
  const running = active && pendingEntries.length > 0;
  const [open, setOpen] = useState(false);
  const wasRunning = useRef(running);
  useLayoutEffect(() => {
    if (wasRunning.current && !running) setOpen(false);
    wasRunning.current = running;
  }, [running]);
  const currentEntry = pendingEntries.at(-1);
  const currentTitle = currentEntry
    ? activityPresentation(currentEntry, true, workspaceRoot, paused, parentFailed).title
    : undefined;
  const operationIcons = group.entries.flatMap(entry => {
    const operation = entry.action;
    if (!operation) return [];
    const presentation = activityPresentation(entry, active, workspaceRoot, paused, parentFailed);
    const visual = toolVisualPresentation(String(operation.event.payload.event_name ?? ''), detailText(operation.event.payload.tool_name));
    const OperationIcon = visual === 'terminal' ? SquareTerminal : visual === 'file' ? fileToolIcon(presentation) : visual === 'mcp' ? PlugZap : visual === 'workflow' ? Workflow : Wrench;
    return [{ id: entry.id, Icon: OperationIcon, label: presentation.title }];
  });
  const visibleOperationIcons = operationIcons.slice(0, 3);
  const hiddenOperationCount = operationIcons.length - visibleOperationIcons.length;
  const label = progressText(group.progress);
  const summaryLabel = currentTitle ? `${label}，${currentTitle}` : label;
  return <details className={`conversation-progress-group${running ? ' active' : ''}`} open={open} onToggle={event => setOpen(event.currentTarget.open)} data-progress-event-id={group.progress.event.id}>
    <summary aria-label={`查看执行过程：${summaryLabel}`}>
      <span className="conversation-progress-summary-content"><b>{label}</b>{running && currentTitle && <small className="conversation-progress-current" role="status">{currentTitle}</small>}<span className="conversation-progress-tail"><span className="conversation-progress-icons" aria-label={`包含 ${operationIcons.length} 个操作`}>{visibleOperationIcons.map(({ id, Icon: OperationIcon, label: operationLabel }) => <OperationIcon key={id} size={12} aria-label={operationLabel}/>)}{hiddenOperationCount > 0 && <small className="conversation-progress-overflow" aria-label={`另有 ${hiddenOperationCount} 个操作`}>{`+${hiddenOperationCount}`}</small>}</span><ChevronRight className="conversation-expand-arrow" size={12}/></span></span>
    </summary>
    <div className="conversation-progress-group-list">
      {group.entries.map((entry, index) => <ActivityEntryRow key={entry.id} entry={entry} active={active} paused={paused} parentFailed={parentFailed} hideThought={index === 0 && entry.action?.event.id === group.progress.event.id} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/>)}
    </div>
  </details>;
}

interface ActivityGroupProps {
  items: Item[];
  active: boolean;
  completionConfirmed?: boolean;
  paused?: boolean;
  parentFailed?: boolean;
  startedAt?: number;
  finishedAt?: number;
  avatarSlots: ReadonlyMap<string, SubagentAvatarSlot>;
  workspaceRoot?: string | null;
}

function sameActivityItems(left: Item[], right: Item[]): boolean {
  return left.length === right.length && left.every((item, index) => (
    item.event === right[index]?.event && item.kind === right[index]?.kind
  ));
}

const ActivityGroup = memo(function ActivityGroup({ items, active, completionConfirmed = false, paused = false, parentFailed = false, startedAt, finishedAt, avatarSlots, workspaceRoot }: ActivityGroupProps) {
  const elapsed = elapsedSeconds(startedAt, finishedAt);
  const entries = groupedActivities(items);
  const rows = activityRows(entries);
  const itemCount = entries.length;
  const hasUnfinishedTask = entries.some(entry => {
    const item = entry.action ?? entry.item;
    return String(item.event.payload.event_name ?? '') === 'TaskAction' && entry.results.length === 0;
  });
  // A delayed readiness response can briefly make an active turn appear idle.
  // Preserve the visible details through that recovery; only a formal
  // reply/error together with a native terminal state may auto-collapse.
  const [open, setOpen] = useState(active);
  const hasBeenActive = useRef(active);
  useLayoutEffect(() => {
    if (active) {
      hasBeenActive.current = true;
      return;
    }
    if (hasBeenActive.current && completionConfirmed) setOpen(false);
  }, [active, completionConfirmed]);
  const label = paused
    ? '已暂停，结果未返回'
    : parentFailed && hasUnfinishedTask ? '本轮异常结束，结果未返回'
      : elapsed === undefined ? '工作过程' : `耗时 ${formatDuration(elapsed)}`;
  const summary = <><ChevronRight size={14}/><span>{active && startedAt !== undefined ? <LiveElapsed startedAt={startedAt}/> : active ? '处理中' : label}</span>{itemCount > 0 && <small>{itemCount} 项</small>}<span className={`conversation-activity-spinner-slot${active ? ' active' : ''}`} aria-hidden="true"><LoaderCircle className="conversation-activity-spin" size={13}/></span></>;
  const hasDetails = itemCount > 0;
  if (!hasDetails) return <div className="conversation-activity-group summary-only"><div className="conversation-activity-summary">{summary}</div></div>;
  return <details className={`conversation-activity-group${active ? ' active' : ''}`} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{summary}</summary>
    <div className="conversation-activity-list">
      {rows.map(row => row.kind === 'progress-group'
        ? <ProgressActivity key={row.group.id} group={row.group} active={active} paused={paused} parentFailed={parentFailed} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/>
        : <ActivityEntryRow key={row.entry.id} entry={row.entry} active={active} paused={paused} parentFailed={parentFailed} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/>)}
    </div>
  </details>;
}, (previous, next) => (
  previous.active === next.active
  && previous.completionConfirmed === next.completionConfirmed
  && previous.paused === next.paused
  && previous.parentFailed === next.parentFailed
  && previous.startedAt === next.startedAt
  && previous.finishedAt === next.finishedAt
  && previous.workspaceRoot === next.workspaceRoot
  && sameActivityItems(previous.items, next.items)
));

function AnnotationReplyContent({ content, annotations, onLocateAnnotation, onOpenWorkspaceFile, onOpenImage }: {
  content: string;
  annotations: AgentConversationAnnotation[];
  onLocateAnnotation?: (annotation: AgentConversationAnnotation) => void;
  onOpenWorkspaceFile?: (href: string) => boolean;
  onOpenImage?: (src: string, alt?: string) => void;
}) {
  const annotationById = useMemo(() => new Map(annotations.map(annotation => [annotation.id, annotation])), [annotations]);
  const parts = useMemo(() => {
    const marker = /::flowweave-annotation\{id="([^"]+)"\}/g;
    const values: Array<{ content: string; annotation?: AgentConversationAnnotation }> = [];
    let cursor = 0;
    for (let matched = marker.exec(content); matched; matched = marker.exec(content)) {
      if (matched.index > cursor) values.push({ content: content.slice(cursor, matched.index) });
      const annotation = annotationById.get(matched[1]);
      values.push(annotation ? { content: '', annotation } : { content: matched[0] });
      cursor = matched.index + matched[0].length;
    }
    if (cursor < content.length) values.push({ content: content.slice(cursor) });
    return values;
  }, [annotationById, content]);
  return <>
    {parts.map((part, index) => part.annotation ? <button
      key={`${part.annotation.id}:${index}`}
      type="button"
      className="conversation-annotation-marker"
      onPointerUp={event => event.stopPropagation()}
      onClick={() => onLocateAnnotation?.(part.annotation!)}
    ><Quote size={12}/><span>注释 {annotations.findIndex(annotation => annotation.id === part.annotation!.id) + 1}</span></button> : part.content && <MessageMarkdown key={index} onOpenWorkspaceFile={onOpenWorkspaceFile} onOpenImage={onOpenImage}>{part.content}</MessageMarkdown>)}
  </>;
}

function AgentReply({ event, content, changes = [], onFork, onPreviewCandidateFile, onReviewChanges, onOpenWorkspaceFile, onOpenImage, workspaceRoot, annotations = [], onLocateAnnotation }: {
  event: OpenHandsConversationEvent;
  content: string;
  changes?: WorkspaceFileChange[];
  onFork?: () => void;
  onPreviewCandidateFile?: (fieldKey: string, relativePath: string) => void;
  onReviewChanges?: (changes: WorkspaceFileChange[]) => void;
  onOpenWorkspaceFile?: (href: string) => boolean;
  onOpenImage?: (src: string, alt?: string) => void;
  workspaceRoot?: string | null;
  annotations?: AgentConversationAnnotation[];
  onLocateAnnotation?: (annotation: AgentConversationAnnotation) => void;
}) {
  const eventId = event.id;
  const timestamp = formatMessageTime(event.payload.timestamp);
  // Candidate outputs are a presentation aid for the agent's structured
  // response. A provider can emit that response as an assistant MessageEvent
  // or a FinishAction. Do not make the card disappear merely because the
  // formal completion event has not yet been projected: the orchestration
  // layer separately verifies the native completion identity before
  // registering an Artifact.
  const candidateMessage = candidateOutputMessage(content);
  return <article className="conversation-message assistant" data-conversation-event-id={eventId} data-turn-terminal="true" data-event-id={eventId}>
    {candidateMessage.businessConclusion ? <AnnotationReplyContent content={candidateMessage.businessConclusion} annotations={annotations} onLocateAnnotation={onLocateAnnotation} onOpenWorkspaceFile={onOpenWorkspaceFile} onOpenImage={onOpenImage}/> : !candidateMessage.outputs && content ? <AnnotationReplyContent content={content} annotations={annotations} onLocateAnnotation={onLocateAnnotation} onOpenWorkspaceFile={onOpenWorkspaceFile} onOpenImage={onOpenImage}/> : null}
    {candidateMessage.outputs && <CandidateOutputReply outputs={candidateMessage.outputs} onPreviewFile={onPreviewCandidateFile ? output => onPreviewCandidateFile(output.fieldKey, output.value) : undefined}/>}
    {!candidateMessage.businessConclusion && !candidateMessage.outputs && !content && <span className="conversation-typing"><i/><i/><i/></span>}
    {changes.length > 0 && <section className="conversation-file-changes" aria-label={`本轮编辑了 ${changes.length} 个文件`}>
      <button type="button" onClick={() => onReviewChanges?.(changes)}><FileText size={15}/><span><b>{`已编辑 ${changes.length} 个文件`}</b><small><ins>{`+${changes.reduce((total, change) => total + change.additions, 0)}`}</ins><del>{`-${changes.reduce((total, change) => total + change.deletions, 0)}`}</del></small></span><PanelRightOpen size={14}/></button>
      <div>{changes.map(change => <button type="button" key={change.id} onClick={() => onReviewChanges?.([change])}><span title={workspaceRelativePath(change.path, workspaceRoot)}>{workspaceRelativePath(change.path, workspaceRoot)}</span><ins>{`+${change.additions}`}</ins><del>{`-${change.deletions}`}</del></button>)}</div>
    </section>}
    {(timestamp || onFork) && <footer className="conversation-message-meta assistant">
      {timestamp && <time dateTime={typeof event.payload.timestamp === 'string' ? event.payload.timestamp : undefined}>{timestamp}</time>}
      {onFork && <button type="button" className="conversation-message-fork" onClick={onFork}><GitFork size={12}/>从此处分叉会话</button>}
    </footer>}
  </article>;
}

function conversationAnnotationForSelection(selection: Selection, surface: HTMLElement): ConversationAnnotationReference | undefined {
  if (!selection.rangeCount) return undefined;
  const content = selection.toString().trim();
  if (!content) return undefined;
  const range = selection.getRangeAt(0);
  const start = elementForNode(range.startContainer);
  const end = elementForNode(range.endContainer);
  const message = start?.closest<HTMLElement>('[data-conversation-event-id]');
  if (!message || !end || !surface.contains(message) || !message.contains(range.startContainer) || !message.contains(range.endContainer)) return undefined;
  const eventId = message.dataset.conversationEventId;
  if (!eventId) return undefined;
  // Keep this positional hint in the native user-message metadata alongside
  // the quote. Unlike a DOM node or rendered element index it remains stable
  // across syntax / Markdown spans, and the quote remains the compatibility
  // fallback for messages created before the hint existed.
  const before = document.createRange();
  before.selectNodeContents(message);
  before.setEnd(range.startContainer, range.startOffset);
  return { eventId, content, compactStart: before.toString().replace(/\s+/g, '').length };
}

function conversationHighlightRects(range: Range, root: HTMLElement): ConversationHighlightRect[] {
  const rootRect = root.getBoundingClientRect();
  const rects = Array.from(range.getClientRects())
    .filter(rect => rect.width > 0 && rect.height > 0)
    .map(rect => ({
      left: rect.left - rootRect.left,
      top: rect.top - rootRect.top,
      width: rect.width,
      height: rect.height,
    }));
  return rects.reduce<ConversationHighlightRect[]>((merged, rect) => {
    const previous = merged.at(-1);
    if (previous && Math.abs(previous.top - rect.top) < 2 && Math.abs(previous.height - rect.height) < 2 && rect.left <= previous.left + previous.width + 3) {
      previous.width = Math.max(previous.width, rect.left + rect.width - previous.left);
    } else {
      merged.push(rect);
    }
    return merged;
  }, []);
}

function conversationQuoteRange(root: HTMLElement, quote: string, compactStart?: number): Range | undefined {
  const compactQuote = quote.replace(/\s+/g, '');
  if (!compactQuote) return undefined;
  const characters: Array<{ node: Text; offset: number; value: string }> = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    for (let offset = 0; offset < node.data.length; offset += 1) {
      const value = node.data[offset];
      if (!/\s/.test(value)) characters.push({ node, offset, value });
    }
  }
  const compactText = characters.map(character => character.value).join('');
  const requestedOffset = typeof compactStart === 'number' && Number.isInteger(compactStart) && compactStart >= 0
    ? compactStart
    : undefined;
  const offset = requestedOffset !== undefined && compactText.slice(requestedOffset, requestedOffset + compactQuote.length) === compactQuote
    ? requestedOffset
    : compactText.indexOf(compactQuote);
  if (offset < 0) return undefined;
  const start = characters[offset];
  const end = characters[offset + compactQuote.length - 1];
  if (!start || !end) return undefined;
  const range = document.createRange();
  range.setStart(start.node, start.offset);
  range.setEnd(end.node, end.offset + 1);
  return range;
}

function isPauseInterruption(item: Item): boolean {
  return isPauseInterruptionEvent(item.event);
}

function isManualTaskInterruption(item: Item, taskControl: RuntimeTaskControlSnapshot[]): boolean {
  if (item.event.event_type !== 'ERROR') return false;
  const toolCallId = typeof item.event.payload.tool_call_id === 'string' ? item.event.payload.tool_call_id : '';
  if (!toolCallId) return false;
  return taskControl.some(control => control.tool_call_id === toolCallId && [
    'INTERRUPT_CONFIRMING',
    'INTERRUPT_CONFIRMED',
    'INTERRUPT_CONFIRMATION_FAILED',
    'RUNTIME_REPLACING',
    'RECOVERED',
  ].includes(control.control_state));
}

function terminalRetryStatus(item: Item): ModelRetryStatus {
  const code = String(item.event.payload.error_code ?? '');
  const safeCode = /^(?:[A-Z][A-Za-z0-9]*(?:Error|Exception)|[A-Z][A-Z0-9_]{2,80})$/.test(code)
    ? code
    : '';
  const modelErrorCodes = new Set([
    'BadGatewayError', 'GatewayTimeoutError', 'ServiceUnavailableError', 'ReadTimeout',
    'HTTPStatusError', 'RequestError', 'CloudflareError', 'OpenAIError', 'APIError',
    'BaseLLMException', 'AnthropicError', 'OpenRouterException', 'OllamaError',
  ]);
  const isModelError = code.startsWith('LLM') || modelErrorCodes.has(code);
  const classification = item.event.payload.classification;
  const kind = classification && typeof classification === 'object'
    ? String((classification as Record<string, unknown>).kind ?? '')
    : '';
  const condensationReason = classification && typeof classification === 'object'
    ? String((classification as Record<string, unknown>).reason ?? '')
    : '';
  if (code === 'NoCondensationAvailableException' && kind === 'condensation') {
    return {
      failureKind: new Set(['no_eligible_events', 'insufficient_progress', 'summary_model_failed']).has(condensationReason)
        ? condensationReason
        : 'condensation_unknown',
      final: true,
      modelRole: 'primary',
      subject: 'condensation',
      errorCode: safeCode || undefined,
    };
  }
  const failureKind = code === 'LLMTimeoutError' ? 'timeout'
    : code === 'LLMNoResponseError' ? 'empty_response'
    : code === 'LLMAuthenticationError' ? 'auth'
    : code === 'LLMRateLimitError' ? 'rate_limit'
    : code === 'LLMContextWindowExceedError' ? 'context_limit'
    : code === 'LLMContentPolicyViolationError' ? 'content_policy'
    : code === 'LLMBadRequestError' ? 'config'
    : code === 'LLMServiceUnavailableError' ? 'service_unavailable'
    : kind === 'quota' ? 'quota'
    : kind === 'internal' ? 'internal'
    : kind === 'config' ? 'config'
    : kind === 'auth' ? 'auth'
    : kind === 'rate_limit' ? 'rate_limit'
    : kind === 'transient' ? 'service_unavailable'
    : 'unknown';
  return {
    failureKind,
    final: true,
    modelRole: 'primary',
    subject: isModelError ? 'model' : 'execution',
    errorCode: safeCode || undefined,
  };
}

function ConversationFailure({ item, taskControl = [], retryStatus }: { item: Item; taskControl?: RuntimeTaskControlSnapshot[]; retryStatus?: ModelRetryStatus }) {
  if (isPauseInterruption(item)) return null;
  if (isManualTaskInterruption(item, taskControl)) {
    return <article className="conversation-interruption" data-turn-terminal="true" data-event-id={item.event.id} role="status">
      <Check size={15}/><div><b>本轮已按你的操作停止</b><p>你主动停止了当前 Agent 执行，部分子智能体尚未返回结果，本轮不会继续生成回复。</p><small>子智能体结果未确认</small></div>
    </article>;
  }
  const code = typeof item.event.payload.error_code === 'string' ? item.event.payload.error_code : '';
  // OpenHands 1.42 emitted failed auto-title metadata as a regular terminal
  // ConversationErrorEvent. The runtime patch prevents new events; this is a
  // final rendering safeguard for already-persisted history regardless of the
  // event order or branch projection returned by an older runtime.
  const isLegacyAutoTitleFailure = code === 'NotFoundError'
    && item.content.includes('litellm.NotFoundError')
    && item.content.includes('OpenAIException')
    && item.content.includes('Error code: 404');
  if (isLegacyAutoTitleFailure) return null;
  return <div data-turn-terminal="true" data-event-id={item.event.id}><RetryStatus status={retryStatus ?? terminalRetryStatus(item)}/></div>;
}

export interface ConversationHistoryPrepend {
  id: number;
  scope: string;
  phase: 'capture' | 'restore';
}

export const ConversationSurface = memo(function ConversationSurface({ events, isGenerating, isPaused = false, emptyResponseRecoveryActive = false, modelRetryStatus, historyPending = false, conversationScope, historyPrepend, onHistoryAnchorCaptured, onHistoryAnchorRestored, requestStartedAt, requestSubmitting = false, rewritePending = false, condensationPending = false, condensationStartedAt, onRewrite, onFork, onOpenAttachment, onOpenWorkspaceReference, onPreviewCandidateFile, onReviewChanges, onOpenWorkspaceFile, onOpenImage, workspaceRoot, annotations = [], onCreateAnnotation, onLocateAnnotation, taskControl = [], monitoring, connectionState }: {
  events: OpenHandsConversationEvent[];
  isGenerating: boolean;
  /** Formal native conversation pause state, used only to label unfinished Task actions. */
  isPaused?: boolean;
  /** Transient UI only; the persisted corrective event never enters history. */
  emptyResponseRecoveryActive?: boolean;
  /** Actual non-persistent transport retry progress for this binding only. */
  modelRetryStatus?: ModelRetryStatus;
  /** Older native pages are being inserted above the current latest window. */
  historyPending?: boolean;
  /** Binding identity that owns this transcript viewport. */
  conversationScope?: string;
  /** Explicitly brackets one scoped historical prepend. */
  historyPrepend?: ConversationHistoryPrepend;
  onHistoryAnchorCaptured?: (transaction: ConversationHistoryPrepend) => void;
  onHistoryAnchorRestored?: (transaction: ConversationHistoryPrepend) => void;
  requestStartedAt?: number;
  requestSubmitting?: boolean;
  rewritePending?: boolean;
  condensationPending?: boolean;
  condensationStartedAt?: number;
  onRewrite?: (eventId: string, content: string) => void;
  onFork?: (eventId: string) => void;
  onOpenAttachment?: (attachment: AgentAttachment) => void;
  onOpenWorkspaceReference?: (reference: AgentWorkspaceReference) => void;
  onPreviewCandidateFile?: (fieldKey: string, relativePath: string) => void;
  onReviewChanges?: (changes: WorkspaceFileChange[]) => void;
  /** Returns true only when a Markdown link was handled by the file preview. */
  onOpenWorkspaceFile?: (href: string) => boolean;
  onOpenImage?: (src: string, alt?: string) => void;
  workspaceRoot?: string | null;
  annotations?: AgentConversationAnnotation[];
  onCreateAnnotation?: (anchor: { event_id: string; quote: string; compact_start: number }) => void;
  onLocateAnnotation?: (annotation: AgentConversationAnnotation) => void;
  taskControl?: RuntimeTaskControlSnapshot[];
  monitoring?: AgentActivitySummary;
  connectionState?: ConversationConnectionState;
}) {
  const surface = useRef<HTMLElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const shell = useRef<HTMLDivElement>(null);
  const initialPositioned = useRef(false);
  const followLatest = useRef(true);
  const userScrolledAway = useRef(false);
  const scrollInteractionStartY = useRef<number | null>(null);
  const scrollInteractionTowardLatest = useRef(false);
  const automaticScrollFrame = useRef<number | undefined>(undefined);
  const messageNavigation = useRef<HTMLElement>(null);
  const messageNavigationAtLatest = useRef(true);
  const messageNavigationScope = useRef<string | undefined>(undefined);
  const messageNavigationFrame = useRef<number | undefined>(undefined);
  const messageNavigationAutoScrollFrame = useRef<number | undefined>(undefined);
  const messageNavigationPreviewIndex = useRef<number | undefined>(undefined);
  const messageNavigationPointerY = useRef<number | undefined>(undefined);
  const messageNavigationDragPointerId = useRef<number | undefined>(undefined);
  const messageNavigationDragCapture = useRef<HTMLElement | undefined>(undefined);
  const messageNavigationDragStartY = useRef<number | undefined>(undefined);
  const messageNavigationDragMoved = useRef(false);
  const messageNavigationSuppressClick = useRef(false);
  const messageNavigationLocatedIndex = useRef<number | undefined>(undefined);
  const messageNavigationStyledButtons = useRef<Set<HTMLButtonElement>>(new Set());
  const selectionReferenceFrame = useRef<number | undefined>(undefined);
  const historyAnchor = useRef<{
    id: number;
    scope: string;
    bottomOffset: number;
    element?: HTMLElement;
    offset?: number;
  } | undefined>(undefined);
  const previousContentSignal = useRef('');
  const copyResetTimer = useRef<number | undefined>(undefined);
  const referenceHighlightTimer = useRef<number | undefined>(undefined);
  const referenceLocationPending = useRef(false);
  const rewriteEditor = useRef<HTMLTextAreaElement>(null);
  const [isAtLatest, setIsAtLatest] = useState(true);
  const [editingEventId, setEditingEventId] = useState<string>();
  const [editingContent, setEditingContent] = useState('');
  const [copiedEventId, setCopiedEventId] = useState<string>();
  const [condensationElapsed, setCondensationElapsed] = useState(0);
  const [messagePreview, setMessagePreview] = useState<{ id: string; content: string; index: number; top: number }>();
  const [selectedReference, setSelectedReference] = useState<{ reference: ConversationAnnotationReference; left: number; top: number }>();
  const [viewingReference, setViewingReference] = useState<AgentConversationReference>();
  const [highlightedReference, setHighlightedReference] = useState<ConversationTextHighlight>();
  const [referenceHighlightRects, setReferenceHighlightRects] = useState<ConversationHighlightRect[]>([]);
  useLayoutEffect(() => {
    if (!editingEventId) return;
    const editor = rewriteEditor.current;
    if (!editor) return;
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  }, [editingEventId]);

  // Pausing a tool makes OpenHands emit one synthetic AgentErrorEvent. Keep
  // the authoritative event for recovery and audit, but it is neither an
  // execution failure nor useful conversation content.
  const visibleEvents = useMemo(
    () => events.filter(event => !isPauseInterruptionEvent(event)),
    [events],
  );
  const turns = useMemo(() => turnsFor(visibleEvents), [visibleEvents]);
  const visibleEventIds = useMemo(() => visibleEvents.map(event => event.id).join('\u001f'), [visibleEvents]);
  const contentGrowthSignal = visibleEventIds;
  const avatarSlots = useMemo(() => subagentAvatarSlots(visibleEvents), [visibleEvents]);
  const userMessageNavigation = useMemo<UserMessageNavigationItem[]>(() => turns.flatMap(turn => turn.user ? [{
    id: turn.user.event.id,
    content: turn.user.content,
  }] : []), [turns]);
  useLayoutEffect(() => {
    const navigation = messageNavigation.current;
    if (!navigation) return;
    if (messageNavigationScope.current !== conversationScope) {
      messageNavigationScope.current = conversationScope;
      messageNavigationAtLatest.current = true;
    }
    const alignWithLatestMessage = () => {
      if (messageNavigationAtLatest.current) navigation.scrollTop = navigation.scrollHeight;
    };
    alignWithLatestMessage();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(alignWithLatestMessage);
    observer.observe(navigation);
    const list = navigation.firstElementChild;
    if (list) observer.observe(list);
    return () => observer.disconnect();
  }, [conversationScope, userMessageNavigation.length]);
  const handleMessageNavigationScroll = useCallback(() => {
    const navigation = messageNavigation.current;
    if (!navigation) return;
    messageNavigationAtLatest.current = navigation.scrollHeight - navigation.scrollTop - navigation.clientHeight <= 1;
  }, []);
  const alignWithLatest = useCallback(() => {
    const element = surface.current;
    if (!element) return;
    const target = Math.max(0, element.scrollHeight - element.clientHeight);
    if (Math.abs(element.scrollTop - target) <= 1) return;
    // Direct assignment is immediate and does not inherit page-level smooth
    // scrolling. Automatic transcript updates must not start an animation
    // that can compete with a user who starts reading history.
    element.scrollTop = target;
  }, []);
  const scrollToLatest = useCallback(() => {
    if (automaticScrollFrame.current !== undefined) {
      window.cancelAnimationFrame(automaticScrollFrame.current);
      automaticScrollFrame.current = undefined;
    }
    userScrolledAway.current = false;
    followLatest.current = true;
    setIsAtLatest(true);
    // This is an explicit user action, but it must still be immediate. A
    // smooth journey emits intermediate scroll events; those look exactly
    // like a user leaving the bottom and can stop a long transcript halfway.
    alignWithLatest();
    // Keep the bottom anchor after the click-triggered layout settles. This
    // also covers Markdown/image layout that changes in the same frame.
    automaticScrollFrame.current = window.requestAnimationFrame(() => {
      automaticScrollFrame.current = undefined;
      if (followLatest.current && !userScrolledAway.current) alignWithLatest();
    });
  }, [alignWithLatest]);
  const updateScrollPosition = useCallback(() => {
    const element = surface.current;
    if (!element) return;
    const atLatest = element.scrollHeight - element.scrollTop - element.clientHeight <= 16;
    // Scroll events also occur when layout and direct scrollTop assignments
    // settle. They do not establish reading intent. Only the capture handlers
    // below can leave or resume follow mode.
    if (atLatest && (!userScrolledAway.current || scrollInteractionTowardLatest.current)) {
      userScrolledAway.current = false;
      followLatest.current = true;
      setIsAtLatest(true);
      return;
    }
    if (!followLatest.current) setIsAtLatest(false);
  }, []);
  const stopFollowingLatest = useCallback(() => {
    if (automaticScrollFrame.current !== undefined) {
      window.cancelAnimationFrame(automaticScrollFrame.current);
      automaticScrollFrame.current = undefined;
    }
    const element = surface.current;
    if (element) {
      // Cancel a pending browser smooth-scroll before it can retake the
      // viewport while the user is trying to move upward.
      element.scrollTo({ top: element.scrollTop, behavior: 'auto' });
    }
    userScrolledAway.current = true;
    followLatest.current = false;
    setIsAtLatest(false);
  }, []);
  const highlightReferenceText = useCallback((highlight: ConversationTextHighlight) => {
    if (referenceHighlightTimer.current) window.clearTimeout(referenceHighlightTimer.current);
    referenceHighlightTimer.current = undefined;
    referenceLocationPending.current = true;
    setReferenceHighlightRects([]);
    setHighlightedReference(highlight);
  }, []);
  const updateReferenceHighlight = useCallback(() => {
    const element = surface.current;
    const highlightRoot = content.current;
    if (!highlightedReference || !element || !highlightRoot) return false;
    const target = Array.from(element.querySelectorAll<HTMLElement>('[data-conversation-event-id]'))
      .find(item => item.dataset.conversationEventId === highlightedReference.eventId);
    if (!target) return false;
    for (let parent = target.parentElement?.closest('details'); parent; parent = parent.parentElement?.closest('details')) parent.open = true;
    const range = conversationQuoteRange(target, highlightedReference.quote, highlightedReference.compactStart);
    if (!range) return false;
    const rects = conversationHighlightRects(range, highlightRoot);
    if (!rects.length) return false;
    setReferenceHighlightRects(rects);
    if (referenceLocationPending.current) {
      const sourceRect = range.getBoundingClientRect();
      const surfaceRect = element.getBoundingClientRect();
      const top = sourceRect.top - surfaceRect.top + element.scrollTop - 28;
      element.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
      referenceLocationPending.current = false;
    }
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    if (!referenceHighlightTimer.current) {
      referenceHighlightTimer.current = window.setTimeout(() => {
        setHighlightedReference(current => current === highlightedReference ? undefined : current);
        setReferenceHighlightRects([]);
        if (window.getSelection()?.toString() === highlightedReference.quote) window.getSelection()?.removeAllRanges();
        referenceHighlightTimer.current = undefined;
      }, 1_500);
    }
    return true;
  }, [highlightedReference]);
  useEffect(() => () => {
    if (referenceHighlightTimer.current) window.clearTimeout(referenceHighlightTimer.current);
  }, []);
  useLayoutEffect(() => {
    updateReferenceHighlight();
  }, [events, updateReferenceHighlight]);
  useEffect(() => {
    if (!highlightedReference || !content.current) return;
    const highlightRoot = content.current;
    let updateFrame: number | undefined;
    const scheduleUpdate = () => {
      if (updateFrame !== undefined) return;
      updateFrame = window.requestAnimationFrame(() => {
        updateFrame = undefined;
        updateReferenceHighlight();
      });
    };
    const mutationObserver = new MutationObserver(mutations => {
      if (mutations.some(mutation => !(mutation.target instanceof Element ? mutation.target : mutation.target.parentElement)?.closest('.conversation-reference-highlights'))) scheduleUpdate();
    });
    mutationObserver.observe(highlightRoot, { childList: true, subtree: true, characterData: true });
    const resizeObserver = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(scheduleUpdate);
    resizeObserver?.observe(highlightRoot);
    window.addEventListener('resize', scheduleUpdate);
    return () => {
      mutationObserver.disconnect();
      resizeObserver?.disconnect();
      window.removeEventListener('resize', scheduleUpdate);
      if (updateFrame !== undefined) window.cancelAnimationFrame(updateFrame);
    };
  }, [highlightedReference, updateReferenceHighlight]);
  const locateTextAnnotation = useCallback((annotation: AgentConversationAnnotation) => {
    // Text anchors belong to this transcript. Resolving them here avoids the
    // FlowNode route's outer grid from becoming an accidental scroll target.
    const eventId = annotation.anchor.event_id;
    if (typeof eventId !== 'string' || !surface.current) return false;
    const quote = typeof annotation.anchor.quote === 'string' ? annotation.anchor.quote.trim() : '';
    const compactStart = typeof annotation.anchor.compact_start === 'number' ? annotation.anchor.compact_start : undefined;
    stopFollowingLatest();
    highlightReferenceText({ eventId, quote, compactStart });
    return true;
  }, [highlightReferenceText, stopFollowingLatest]);
  const locateAnnotation = useCallback((annotation: AgentConversationAnnotation) => {
    if (annotation.anchor_kind === 'CONVERSATION_TEXT' && locateTextAnnotation(annotation)) return;
    stopFollowingLatest();
    onLocateAnnotation?.(annotation);
  }, [locateTextAnnotation, onLocateAnnotation, stopFollowingLatest]);
  useEffect(() => {
    const locate = (event: Event) => {
      const annotation = (event as CustomEvent<AgentConversationAnnotation>).detail;
      if (annotation?.anchor_kind === 'CONVERSATION_TEXT') locateTextAnnotation(annotation);
    };
    window.addEventListener('flowweave:locate-conversation-annotation', locate);
    return () => window.removeEventListener('flowweave:locate-conversation-annotation', locate);
  }, [locateTextAnnotation]);
  const scrollToUserMessage = useCallback((eventId: string, behavior: ScrollBehavior = 'smooth') => {
    const element = surface.current;
    const target = element?.querySelectorAll<HTMLElement>('[data-user-event-id]');
    const message = Array.from(target ?? []).find(item => item.dataset.userEventId === eventId);
    if (!element || !message) return;
    const top = message.getBoundingClientRect().top - element.getBoundingClientRect().top + element.scrollTop - 18;
    userScrolledAway.current = true;
    followLatest.current = false;
    element.scrollTo({ top: Math.max(0, top), behavior });
  }, []);
  const showMessagePreview = useCallback((message: UserMessageNavigationItem, index: number, target: HTMLElement) => {
    const shellBounds = shell.current?.getBoundingClientRect();
    const targetBounds = target.getBoundingClientRect();
    if (!shellBounds) return;
    setMessagePreview({
      id: message.id,
      content: message.content,
      index,
      top: targetBounds.top - shellBounds.top + targetBounds.height / 2,
    });
  }, []);
  const clearMessageNavigationPreview = useCallback(() => {
    if (messageNavigationFrame.current !== undefined) {
      window.cancelAnimationFrame(messageNavigationFrame.current);
      messageNavigationFrame.current = undefined;
    }
    messageNavigationPreviewIndex.current = undefined;
    messageNavigationPointerY.current = undefined;
    messageNavigationStyledButtons.current.forEach(button => {
      button.style.removeProperty('--message-index-proximity');
      button.style.removeProperty('--message-index-proximity-percent');
      button.style.removeProperty('--message-index-width');
      button.style.removeProperty('--message-index-opacity');
      button.style.removeProperty('--message-index-halo');
    });
    messageNavigationStyledButtons.current.clear();
    setMessagePreview(undefined);
  }, []);
  const updateMessageNavigationPreview = useCallback((clientY: number) => {
    const buttons = Array.from(messageNavigation.current?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    if (!buttons.length) return undefined;
    const firstBounds = buttons[0].getBoundingClientRect();
    const lastBounds = buttons.at(-1)?.getBoundingClientRect() ?? firstBounds;
    const firstCenter = firstBounds.top + firstBounds.height / 2;
    const lastCenter = lastBounds.top + lastBounds.height / 2;
    const pointerIndex = buttons.length === 1 || firstCenter === lastCenter
      ? 0
      : Math.max(0, Math.min(buttons.length - 1, (clientY - firstCenter) / (lastCenter - firstCenter) * (buttons.length - 1)));
    const styledButtons = new Set<HTMLButtonElement>();
    const firstStyledIndex = Math.max(0, Math.ceil(pointerIndex - 2));
    const lastStyledIndex = Math.min(buttons.length - 1, Math.floor(pointerIndex + 2));
    for (let index = firstStyledIndex; index <= lastStyledIndex; index += 1) {
      const button = buttons[index];
      const proximity = Math.max(0, 1 - Math.abs(index - pointerIndex) / 2);
      if (!proximity) continue;
      styledButtons.add(button);
      button.style.setProperty('--message-index-proximity', proximity.toFixed(3));
      button.style.setProperty('--message-index-proximity-percent', `${Math.round(proximity * 100)}%`);
      button.style.setProperty('--message-index-width', `${8 + 7 * proximity}px`);
      button.style.setProperty('--message-index-opacity', `${0.62 + 0.38 * proximity}`);
      button.style.setProperty('--message-index-halo', `${3 * proximity}px`);
    }
    messageNavigationStyledButtons.current.forEach(button => {
      if (styledButtons.has(button)) return;
      button.style.removeProperty('--message-index-proximity');
      button.style.removeProperty('--message-index-proximity-percent');
      button.style.removeProperty('--message-index-width');
      button.style.removeProperty('--message-index-opacity');
      button.style.removeProperty('--message-index-halo');
    });
    messageNavigationStyledButtons.current = styledButtons;
    const previewIndex = Math.max(0, Math.min(userMessageNavigation.length - 1, Math.round(pointerIndex)));
    const previewTarget = buttons[previewIndex];
    const previewMessage = userMessageNavigation[previewIndex];
    if (!previewTarget || !previewMessage) return undefined;
    if (messageNavigationPreviewIndex.current !== previewIndex) {
      messageNavigationPreviewIndex.current = previewIndex;
      showMessagePreview(previewMessage, previewIndex, previewTarget);
    }
    return previewIndex;
  }, [showMessagePreview, userMessageNavigation]);
  const updateMessageNavigationPointer = useCallback((clientY: number) => {
    const index = updateMessageNavigationPreview(clientY);
    if (index === undefined || !messageNavigationDragMoved.current || messageNavigationLocatedIndex.current === index) return;
    const message = userMessageNavigation[index];
    if (!message) return;
    messageNavigationLocatedIndex.current = index;
    scrollToUserMessage(message.id, 'auto');
  }, [scrollToUserMessage, updateMessageNavigationPreview, userMessageNavigation]);
  const scheduleMessageNavigationPointerUpdate = useCallback(() => {
    if (messageNavigationFrame.current !== undefined) return;
    messageNavigationFrame.current = window.requestAnimationFrame(() => {
      messageNavigationFrame.current = undefined;
      const clientY = messageNavigationPointerY.current;
      if (clientY !== undefined) updateMessageNavigationPointer(clientY);
    });
  }, [updateMessageNavigationPointer]);
  const startMessageNavigationAutoScroll = useCallback(() => {
    if (messageNavigationAutoScrollFrame.current !== undefined) return;
    const step = () => {
      messageNavigationAutoScrollFrame.current = undefined;
      const navigation = messageNavigation.current;
      const clientY = messageNavigationPointerY.current;
      if (!navigation || clientY === undefined || messageNavigationDragPointerId.current === undefined || !messageNavigationDragMoved.current) return;
      const bounds = navigation.getBoundingClientRect();
      const edgeSize = bounds.height / 4;
      const topStrength = edgeSize > 0 ? Math.max(0, Math.min(1, (bounds.top + edgeSize - clientY) / edgeSize)) : 0;
      const bottomStrength = edgeSize > 0 ? Math.max(0, Math.min(1, (clientY - (bounds.bottom - edgeSize)) / edgeSize)) : 0;
      const delta = bottomStrength > 0 ? Math.max(1, bottomStrength * 12) : topStrength > 0 ? -Math.max(1, topStrength * 12) : 0;
      if (!delta) return;
      const previousScrollTop = navigation.scrollTop;
      navigation.scrollTop += delta;
      updateMessageNavigationPointer(clientY);
      if (navigation.scrollTop !== previousScrollTop) messageNavigationAutoScrollFrame.current = window.requestAnimationFrame(step);
    };
    messageNavigationAutoScrollFrame.current = window.requestAnimationFrame(step);
  }, [updateMessageNavigationPointer]);
  const handleMessageNavigationPointerMove = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    messageNavigationPointerY.current = event.clientY;
    if (messageNavigationDragPointerId.current === event.pointerId && messageNavigationDragStartY.current !== undefined
      && Math.abs(event.clientY - messageNavigationDragStartY.current) > 2) {
      messageNavigationDragMoved.current = true;
    }
    scheduleMessageNavigationPointerUpdate();
    if (messageNavigationDragMoved.current) startMessageNavigationAutoScroll();
  }, [scheduleMessageNavigationPointerUpdate, startMessageNavigationAutoScroll]);
  const handleMessageNavigationPointerDown = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    messageNavigationDragPointerId.current = event.pointerId;
    messageNavigationDragCapture.current = event.currentTarget;
    messageNavigationDragStartY.current = event.clientY;
    messageNavigationDragMoved.current = false;
    messageNavigationSuppressClick.current = false;
    messageNavigationLocatedIndex.current = undefined;
    messageNavigationPointerY.current = event.clientY;
    event.currentTarget.setPointerCapture(event.pointerId);
    scheduleMessageNavigationPointerUpdate();
  }, [scheduleMessageNavigationPointerUpdate]);
  const finishMessageNavigationDrag = useCallback((event: ReactPointerEvent<HTMLElement>, cancelled = false) => {
    if (messageNavigationDragPointerId.current !== event.pointerId) return;
    const capture = messageNavigationDragCapture.current;
    if (capture?.hasPointerCapture(event.pointerId)) capture.releasePointerCapture(event.pointerId);
    messageNavigationSuppressClick.current = !cancelled && messageNavigationDragMoved.current;
    messageNavigationDragPointerId.current = undefined;
    messageNavigationDragCapture.current = undefined;
    messageNavigationDragStartY.current = undefined;
    messageNavigationDragMoved.current = false;
    messageNavigationLocatedIndex.current = undefined;
    if (messageNavigationAutoScrollFrame.current !== undefined) {
      window.cancelAnimationFrame(messageNavigationAutoScrollFrame.current);
      messageNavigationAutoScrollFrame.current = undefined;
    }
    window.setTimeout(() => { messageNavigationSuppressClick.current = false; }, 0);
  }, []);
  const handleMessageNavigationPointerLeave = useCallback(() => {
    if (messageNavigationDragPointerId.current === undefined) clearMessageNavigationPreview();
  }, [clearMessageNavigationPreview]);
  const handleScroll = useCallback(() => {
    updateScrollPosition();
    scrollInteractionTowardLatest.current = false;
  }, [updateScrollPosition]);
  const scheduleLatestAlignment = useCallback(() => {
    if (!followLatest.current || userScrolledAway.current) return;
    if (automaticScrollFrame.current !== undefined) window.cancelAnimationFrame(automaticScrollFrame.current);
    automaticScrollFrame.current = window.requestAnimationFrame(() => {
      automaticScrollFrame.current = undefined;
      if (followLatest.current && !userScrolledAway.current) alignWithLatest();
    });
  }, [alignWithLatest]);
  const handleNativeContentToggle = useCallback(() => {
    // Native <details> changes do not necessarily re-render React. Preserve
    // the latest anchor in the layout event itself; a reader's explicit lock
    // still takes precedence over this programmatic height change.
    if (followLatest.current && !userScrolledAway.current) {
      alignWithLatest();
      scheduleLatestAlignment();
    }
  }, [alignWithLatest, scheduleLatestAlignment]);
  useEffect(() => {
    const observedContent = content.current;
    if (!observedContent) return;
    observedContent.addEventListener('toggle', handleNativeContentToggle, true);
    return () => observedContent.removeEventListener('toggle', handleNativeContentToggle, true);
  }, [handleNativeContentToggle]);
  useLayoutEffect(() => {
    const element = surface.current;
    if (!element || !historyPrepend || historyPrepend.scope !== conversationScope) return;
    if (historyPrepend.phase === 'capture') {
      if (historyAnchor.current?.id === historyPrepend.id) return;
      const surfaceBounds = element.getBoundingClientRect();
      const anchorElement = Array.from(element.querySelectorAll<HTMLElement>('[data-conversation-event-id], .conversation-activity-row'))
        .find(candidate => candidate.getBoundingClientRect().bottom > surfaceBounds.top);
      historyAnchor.current = {
        id: historyPrepend.id,
        scope: historyPrepend.scope,
        bottomOffset: element.scrollHeight - element.scrollTop,
        element: anchorElement,
        offset: anchorElement
          ? anchorElement.getBoundingClientRect().top - surfaceBounds.top
          : undefined,
      };
      onHistoryAnchorCaptured?.(historyPrepend);
      return;
    }
    const anchor = historyAnchor.current;
    if (!anchor || anchor.id !== historyPrepend.id || anchor.scope !== historyPrepend.scope) return;
    if (followLatest.current && !userScrolledAway.current) {
      alignWithLatest();
    } else if (anchor.element?.isConnected && anchor.offset !== undefined) {
      // A live event can append while this historical page is in flight. An
      // element anchor isolates the prepend correction from that tail growth,
      // keeping the exact row the reader was looking at in the same place.
      const surfaceBounds = element.getBoundingClientRect();
      element.scrollTop += anchor.element.getBoundingClientRect().top - surfaceBounds.top - anchor.offset;
    } else {
      // A replaced event is unusual, but retaining the previous offset is a
      // better fallback than leaving the prepend to move the reader.
      element.scrollTop = Math.max(0, element.scrollHeight - anchor.bottomOffset);
    }
    historyAnchor.current = undefined;
    onHistoryAnchorRestored?.(historyPrepend);
  }, [alignWithLatest, conversationScope, historyPrepend, onHistoryAnchorCaptured, onHistoryAnchorRestored]);
  // A REST reconciliation may replace event objects without adding visible
  // content. Only event identities or appended live text may move the viewport;
  // readiness, status, animation, and ResizeObserver updates never write it.
  useLayoutEffect(() => {
    const contentChanged = previousContentSignal.current !== contentGrowthSignal;
    previousContentSignal.current = contentGrowthSignal;
    if (!initialPositioned.current && (turns.length || isGenerating)) {
      initialPositioned.current = true;
      alignWithLatest();
    } else if (contentChanged && followLatest.current && !userScrolledAway.current) {
      alignWithLatest();
    }
  }, [alignWithLatest, contentGrowthSignal, isGenerating, turns.length]);
  useLayoutEffect(() => {
    const observedContent = content.current;
    if (!observedContent || typeof ResizeObserver === 'undefined' || !contentGrowthSignal) return;
    const targets = observedContent.querySelectorAll<HTMLElement>('.conversation-message[data-conversation-event-id]');
    const latestContent = targets.item(targets.length - 1);
    if (!latestContent) return;
    const observer = new ResizeObserver(() => {
      if (!followLatest.current || userScrolledAway.current) return;
      scheduleLatestAlignment();
    });
    observer.observe(latestContent);
    return () => observer.disconnect();
  }, [contentGrowthSignal, scheduleLatestAlignment]);

  useEffect(() => () => {
    if (copyResetTimer.current) window.clearTimeout(copyResetTimer.current);
  }, []);
  useEffect(() => {
    if (!condensationPending) {
      setCondensationElapsed(0);
      return;
    }
    const startedAt = condensationStartedAt ?? Date.now();
    const update = () => setCondensationElapsed(Math.max(0, Date.now() - startedAt));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [condensationPending, condensationStartedAt]);
  useEffect(() => () => {
    if (messageNavigationFrame.current !== undefined) window.cancelAnimationFrame(messageNavigationFrame.current);
    if (messageNavigationAutoScrollFrame.current !== undefined) window.cancelAnimationFrame(messageNavigationAutoScrollFrame.current);
  }, []);
  useEffect(() => () => {
    if (selectionReferenceFrame.current !== undefined) {
      window.cancelAnimationFrame(selectionReferenceFrame.current);
    }
  }, [conversationScope]);
  useEffect(() => {
    const onCopy = (event: ClipboardEvent) => {
      const selection = window.getSelection();
      if (!selection || !surface.current) return;
      const isolated = isolatedUserSelection(selection);
      if (!isolated || !surface.current.contains(isolated.content)) return;
      event.preventDefault();
      event.clipboardData?.setData('text/plain', isolated.text);
    };
    document.addEventListener('copy', onCopy);
    return () => document.removeEventListener('copy', onCopy);
  }, []);
  const copyUserMessage = useCallback((eventId: string, content: string) => {
    void copyText(content).then(() => {
      setCopiedEventId(eventId);
      if (copyResetTimer.current) window.clearTimeout(copyResetTimer.current);
      copyResetTimer.current = window.setTimeout(() => setCopiedEventId(current => current === eventId ? undefined : current), 1_500);
    }).catch(() => {
      // Native selection copy remains available when the browser rejects programmatic clipboard access.
    });
  }, []);
  const offerSelectedReference = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (!onCreateAnnotation || !surface.current) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const pointerTarget = event.target instanceof Node ? elementForNode(event.target) : undefined;
    if (pointerTarget?.closest('a,button,input,textarea,select,summary,[contenteditable="true"]')) return;
    const pointerMessageId = pointerTarget?.closest<HTMLElement>('[data-conversation-event-id]')?.dataset.conversationEventId;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) {
      setSelectedReference(undefined);
      return;
    }
    if (selectionReferenceFrame.current !== undefined) window.cancelAnimationFrame(selectionReferenceFrame.current);
    selectionReferenceFrame.current = window.requestAnimationFrame(() => {
      selectionReferenceFrame.current = undefined;
      const currentSurface = surface.current;
      const currentSelection = window.getSelection();
      if (!currentSurface || !currentSelection || currentSelection.isCollapsed || !currentSelection.rangeCount) {
        setSelectedReference(undefined);
        return;
      }
      const reference = conversationAnnotationForSelection(currentSelection, currentSurface);
      if (!reference || pointerMessageId !== reference.eventId) {
        setSelectedReference(undefined);
        return;
      }
      const bounds = currentSelection.getRangeAt(0).getBoundingClientRect();
      if (!bounds.width && !bounds.height) {
        setSelectedReference(undefined);
        return;
      }
      setSelectedReference({
        reference,
        left: Math.min(Math.max(12, bounds.left), Math.max(12, window.innerWidth - 172)),
        top: Math.min(bounds.bottom + 8, Math.max(12, window.innerHeight - 44)),
      });
    });
  }, [onCreateAnnotation]);
  const locateReferenceSource = useCallback(() => {
    if (!viewingReference || !surface.current) return;
    stopFollowingLatest();
    setViewingReference(undefined);
    highlightReferenceText({ eventId: viewingReference.event_id, quote: viewingReference.content });
  }, [highlightReferenceText, stopFollowingLatest, viewingReference]);
  const lastUserEventId = useMemo(() => [...turns].reverse().find(turn => turn.user)?.user?.event.id, [turns]);
  if (!turns.length && !isGenerating) return <div className="conversation-surface-empty"><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  const showJumpToLatest = !isAtLatest && Boolean(turns.length || isGenerating);
  return <div ref={shell} className="conversation-surface-shell">
    {userMessageNavigation.length > 0 && <nav ref={messageNavigation} className="conversation-message-index" aria-label="用户消息导航" onScroll={handleMessageNavigationScroll} onPointerDown={handleMessageNavigationPointerDown} onPointerMove={handleMessageNavigationPointerMove} onPointerUp={finishMessageNavigationDrag} onPointerCancel={event => finishMessageNavigationDrag(event, true)} onPointerLeave={handleMessageNavigationPointerLeave}>
      <div className="conversation-message-index-list">
        {userMessageNavigation.map((message, index) => <button
          type="button"
          key={message.id}
          aria-label={`定位到用户消息：${messageSummary(message.content)}`}
          aria-describedby={messagePreview?.id === message.id ? 'conversation-message-preview' : undefined}
          onFocus={event => showMessagePreview(message, index, event.currentTarget)}
          onBlur={() => setMessagePreview(current => current?.id === message.id ? undefined : current)}
          onClick={() => {
            if (messageNavigationSuppressClick.current) {
              messageNavigationSuppressClick.current = false;
              return;
            }
            scrollToUserMessage(message.id);
          }}
        >
          <span className="conversation-message-index-tick" aria-hidden="true"/>
        </button>)}
      </div>
    </nav>}
    {messagePreview && <aside id="conversation-message-preview" className="conversation-message-index-tooltip" role="tooltip" style={{ top: messagePreview.top }}><span>{messagePreview.content || '（空消息）'}</span></aside>}
    <section ref={surface} className="conversation-surface" aria-live="polite" onScroll={() => { handleScroll(); setSelectedReference(undefined); }} onClickCapture={event => {
      if (event.target instanceof Element && event.target.closest('summary')) scheduleLatestAlignment();
    }} onWheelCapture={event => {
      const element = surface.current;
      if (event.deltaY < 0 && (element?.scrollTop ?? 0) > 0) stopFollowingLatest();
      else if (event.deltaY > 0) scrollInteractionTowardLatest.current = true;
    }} onPointerDownCapture={event => {
      // Touch drags always belong to the viewport. For a mouse, only track
      // the native scrollbar itself so text selection cannot disable follow.
      if (event.pointerType === 'touch' || event.target === surface.current) {
        scrollInteractionStartY.current = event.clientY;
        scrollInteractionTowardLatest.current = false;
      }
    }} onPointerMoveCapture={event => {
      if (scrollInteractionStartY.current === null) return;
      if (event.clientY - scrollInteractionStartY.current > 3 && (surface.current?.scrollTop ?? 0) > 0) stopFollowingLatest();
      else if (scrollInteractionStartY.current - event.clientY > 3) scrollInteractionTowardLatest.current = true;
    }} onPointerUp={event => { scrollInteractionStartY.current = null; offerSelectedReference(event); }} onPointerCancel={() => { scrollInteractionStartY.current = null; }} onKeyDown={event => {
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) && (surface.current?.scrollTop ?? 0) > 0) stopFollowingLatest();
      else if (['ArrowDown', 'PageDown', 'End'].includes(event.key)) scrollInteractionTowardLatest.current = true;
    }}>
      <div ref={content} className="conversation-surface-content">
      {referenceHighlightRects.length > 0 && <div className="conversation-reference-highlights" aria-hidden="true">{referenceHighlightRects.map((rect, index) => <i key={`${rect.left}:${rect.top}:${index}`} style={rect}/>)}</div>}
      {historyPending && <div className="conversation-history-loading" role="status"><LoaderCircle size={14}/>正在载入更早的会话记录…</div>}
      {turns.map((turn, index) => {
        const isLatest = index === turns.length - 1;
        const isCurrent = isLatest && isGenerating;
        const isCurrentPaused = isLatest && isPaused;
        const errors = turn.activity.filter(item => item.kind === 'error');
        const recoveredErrorEventIds = new Set(turn.assistant || isCurrent ? errors.map(item => item.event.id) : []);
        const failures = errors.filter(item => !recoveredErrorEventIds.has(item.event.id));
        const parentFailed = failures.length > 0;
        // A submitted turn can render before its formal OpenHands user event
        // replaces the prior active branch. During that bounded hand-off the
        // browser submission time is the only truthful current-turn anchor;
        // never briefly inherit a much older user event's timestamp.
        const startedAt = isCurrent && requestStartedAt !== undefined
          ? requestStartedAt
          : eventTime(turn.user);
        const finishedAt = eventTime(turn.assistant ?? failures.at(-1));
        const processBlocks = turnProcessBlocks(
          turn.activity.filter(item => item.kind !== 'error'),
          startedAt,
          finishedAt,
          isCurrent && !turn.assistant && !failures.length,
        );
        const completionConfirmed = !isGenerating && Boolean(turn.assistant || failures.length);
        const fileChanges = fileChangesForTurn(events, turn);
        const userTimestamp = turn.user ? formatMessageTime(turn.user.event.payload.timestamp) : undefined;
        const projectionState = turn.user?.event.payload._flowweave_projection_state;
        const userDeliveryStatus = projectionState === 'queued'
          ? '等待发送'
          : projectionState === 'ambiguous'
            ? '发送结果待确认'
            : turn.user && typeof turn.user.event.payload._flowweave_delivery_status === 'string'
              ? turn.user.event.payload._flowweave_delivery_status
              : undefined;
        return <section className="conversation-turn" key={turn.renderKey} data-conversation-turn={turn.id}>
          {turn.user && <div className="conversation-user-message">{editingEventId === turn.user.event.id
            ? <form className="conversation-message-edit" onSubmit={event => { event.preventDefault(); if (editingContent.trim()) onRewrite?.(turn.user!.event.id, editingContent.trim()); }}><textarea ref={rewriteEditor} aria-label="编辑已发送消息" value={editingContent} disabled={rewritePending} onChange={event => setEditingContent(event.target.value)} onKeyDown={event => {
              if (event.nativeEvent.isComposing || event.keyCode === 229) return;
              if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                setEditingEventId(undefined);
                return;
              }
              if (event.key !== 'Enter' || event.shiftKey) return;
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }}/><footer><button type="button" onClick={() => setEditingEventId(undefined)}>取消</button><button type="submit" disabled={!editingContent.trim() || rewritePending}>重新思考</button></footer></form>
            : <article data-user-event-id={turn.user.event.id} data-conversation-event-id={turn.user.event.id} className="conversation-message user">{turn.user.content && <div className="conversation-message-content"><MessageMarkdown>{turn.user.content}</MessageMarkdown></div>}<MessageAttachments attachments={eventAttachments(turn.user.event)} references={turn.user.event.payload.conversation_references} workspaceReferences={turn.user.event.payload.workspace_references} annotations={eventAnnotations(turn.user.event)} onOpen={onOpenAttachment} onOpenReference={setViewingReference} onOpenWorkspaceReference={onOpenWorkspaceReference} onOpenAnnotation={locateAnnotation}/><footer className="conversation-message-meta user">{userDeliveryStatus && <small className="conversation-message-delivery-status" role="status">{userDeliveryStatus}</small>}{userTimestamp && <time dateTime={typeof turn.user.event.payload.timestamp === 'string' ? turn.user.event.payload.timestamp : undefined}>{userTimestamp}</time>}<div className={`conversation-message-actions${lastUserEventId === turn.user.event.id ? ' can-rewrite' : ''}`}><button type="button" className="conversation-message-copy" aria-label={copiedEventId === turn.user.event.id ? '消息已复制' : '复制消息'} title={copiedEventId === turn.user.event.id ? '已复制' : '复制消息'} onClick={() => copyUserMessage(turn.user!.event.id, turn.user!.content)}>{copiedEventId === turn.user.event.id ? <Check size={13}/> : <Copy size={13}/>}</button>{lastUserEventId === turn.user.event.id && <button type="button" className="conversation-message-rewrite" aria-label="编辑并重新思考" title="编辑并重新思考" onClick={() => { setEditingEventId(turn.user!.event.id); setEditingContent(turn.user!.content); }}><Pencil size={13}/></button>}</div></footer></article>}</div>}
          {processBlocks.map(block => <ActivityGroup
            key={block.id}
            items={block.items}
            active={block.active}
            completionConfirmed={completionConfirmed}
            paused={isCurrentPaused && !block.active}
            parentFailed={parentFailed && !block.active}
            startedAt={block.startedAt}
            finishedAt={block.finishedAt}
            avatarSlots={avatarSlots}
            workspaceRoot={workspaceRoot}
          />)}
          {isCurrent && !turn.assistant && !failures.length && (
            <CurrentTurnStatus items={turn.activity} requestSubmitting={requestSubmitting} statusOverride={emptyResponseRecoveryActive ? '模型返回空响应，OpenHands 正在自动重试' : undefined} modelRetryStatus={modelRetryStatus} monitoring={monitoring} connectionState={connectionState}/>
          )}
          {processBlocks.length > 0 && turn.assistant && <div className="conversation-process-divider" role="separator" aria-label="工作过程结束"/>}
          {turn.assistant && <AgentReply event={turn.assistant.event} content={turn.assistant.content} changes={fileChanges} onFork={!isGenerating ? () => onFork?.(turn.assistant!.event.id) : undefined} onPreviewCandidateFile={onPreviewCandidateFile} onReviewChanges={onReviewChanges} onOpenWorkspaceFile={onOpenWorkspaceFile} onOpenImage={onOpenImage} workspaceRoot={workspaceRoot} annotations={annotations} onLocateAnnotation={locateAnnotation}/>}
          {failures.map(item => <ConversationFailure key={item.event.id} item={item} taskControl={taskControl} retryStatus={isLatest ? modelRetryStatus : undefined}/>)}
        </section>;
      })}
      {turns.length === 0 && isGenerating && !condensationPending && <><ActivityGroup items={[]} active startedAt={requestStartedAt} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/><CurrentTurnStatus items={[]} requestSubmitting={requestSubmitting} statusOverride={emptyResponseRecoveryActive ? '模型返回空响应，OpenHands 正在自动重试' : undefined} modelRetryStatus={modelRetryStatus} monitoring={monitoring} connectionState={connectionState}/></>}
      {condensationPending && <article className="conversation-condensation-progress" role="status" aria-label="正在压缩上下文">
        <LoaderCircle className="conversation-activity-spin" size={16}/>
        <div><header><b>正在压缩上下文</b><time>{formatDuration(condensationElapsed / 1_000)}</time></header><p>{condensationElapsed < 2_000 ? '请求已接受，正在等待 OpenHands 开始压缩。' : 'Condenser 正在生成较早上下文的结构化摘要。你仍可编辑消息，发送后将进入队列。'}</p></div>
      </article>}
      </div>
    </section>
    {viewingReference && <ConversationReferencePreview reference={viewingReference} onClose={() => setViewingReference(undefined)} onLocate={locateReferenceSource}/>}
    {selectedReference && <span className="conversation-add-reference" style={{ left: selectedReference.left, top: selectedReference.top }}>
      <button type="button" onPointerDown={event => event.preventDefault()} onClick={() => { onCreateAnnotation?.({ event_id: selectedReference.reference.eventId, quote: selectedReference.reference.content, compact_start: selectedReference.reference.compactStart }); window.getSelection()?.removeAllRanges(); setSelectedReference(undefined); }}><Quote size={14}/>添加到会话</button>
    </span>}
    {showJumpToLatest && <button
      type="button"
      className={`conversation-jump-latest${isGenerating ? ' generating' : ''}`}
      aria-label={isGenerating ? '跳转到正在生成的最新回复' : '跳转到最新回复'}
      title={isGenerating ? '查看正在生成的最新回复' : '查看最新回复'}
      onClick={() => scrollToLatest()}
    >
      {isGenerating ? (
        <span className="conversation-jump-dots" aria-hidden="true"><i/><i/><i/></span>
      ) : (
        <ChevronDown size={19}/>
      )}
    </button>}
  </div>;
});
