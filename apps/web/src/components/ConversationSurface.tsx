import { Check, ChevronDown, ChevronRight, CircleAlert, Copy, ExternalLink, FileText, GitFork, Link, LoaderCircle, PanelRightOpen, Pencil, Quote, Sparkles, SquareTerminal, Wrench } from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type RefObject } from 'react';
import type { AgentActivitySummary, AgentAttachment, AgentConversationReference, AgentWorkspaceReference, OpenHandsConversationEvent, RuntimeTaskControlSnapshot } from '../types';
import { SubagentAvatar } from './SubagentAvatar';
import { useEscapeClose } from './useEscapeClose';
import { subagentAvatarSlotForEvent, subagentAvatarSlots, type SubagentAvatarSlot } from '../utils/subagentAvatar';
import { workspaceFileChanges, workspaceRelativePath, type WorkspaceFileChange } from './agent-session/fileChanges';
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
  user?: Item;
  assistant?: Item;
  activity: Item[];
}

interface UserMessageNavigationItem {
  id: string;
  content: string;
}

export interface ConversationReference {
  eventId: string;
  content: string;
}

interface ActivityEntry {
  id: string;
  item: Item;
  action?: Item;
  results: Item[];
}

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

export function ConversationTaskPlan({ events, isGenerating }: { events: OpenHandsConversationEvent[]; isGenerating: boolean }) {
  const currentEvents = useMemo(() => currentTurnEvents(events), [events]);
  const snapshot = useMemo(() => latestCurrentTaskList(currentEvents), [currentEvents]);
  const [expanded, setExpanded] = useState(false);
  const planBodyRef = useRef<HTMLDivElement>(null);
  const currentTaskRef = useRef<HTMLLIElement>(null);
  const currentTaskIndex = snapshot?.items.findIndex(item => item.status === 'in_progress') ?? -1;
  const completed = snapshot?.items.filter(item => item.status === 'done').length ?? 0;
  const showPlan = isGenerating && snapshot && snapshot.items.some(item => item.status !== 'done');

  useLayoutEffect(() => {
    if (!expanded || currentTaskIndex < 0 || !planBodyRef.current || !currentTaskRef.current) return;
    const body = planBodyRef.current;
    const task = currentTaskRef.current;
    body.scrollTop = Math.max(0, task.offsetTop - (body.clientHeight - task.offsetHeight) / 2);
  }, [currentTaskIndex, expanded, snapshot]);

  if (!showPlan || !snapshot) return null;
  return <details className="conversation-live-task-plan" aria-label="当前任务计划" open={expanded} onMouseEnter={() => setExpanded(true)} onMouseLeave={() => setExpanded(false)} onFocusCapture={() => setExpanded(true)} onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setExpanded(false); }}>
    <summary aria-label={`当前计划：${completed} / ${snapshot.items.length} 已完成`} onClick={event => event.preventDefault()} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') event.preventDefault(); }}>
      <Check size={15}/><span><b>当前计划</b><small>{`${completed} / ${snapshot.items.length} 已完成`}</small></span><ChevronRight size={14}/>
    </summary>
    <div ref={planBodyRef} className="conversation-live-task-plan-body">
      <TaskListItems items={snapshot.items} currentTaskIndex={currentTaskIndex >= 0 ? currentTaskIndex : undefined} currentTaskRef={currentTaskRef} source={snapshot.timestamp ? `OpenHands 原生任务事件 · ${formatMessageTime(snapshot.timestamp)}` : 'OpenHands 原生任务事件'}/>
    </div>
  </details>;
}

type TurnProcessBlock =
  | { kind: 'activity'; id: string; items: Item[]; startedAt?: number; finishedAt?: number; active: boolean }
  | { kind: 'condensation'; id: string; items: Item[] };

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

function MessageAttachments({ attachments, references = [], workspaceReferences = [], onOpen, onOpenReference, onOpenWorkspaceReference }: {
  attachments: AgentAttachment[];
  references?: AgentConversationReference[];
  workspaceReferences?: AgentWorkspaceReference[];
  onOpen?: (attachment: AgentAttachment) => void;
  onOpenReference?: (reference: AgentConversationReference) => void;
  onOpenWorkspaceReference?: (reference: AgentWorkspaceReference) => void;
}) {
  if (!attachments.length && !references.length && !workspaceReferences.length) return null;
  return <div className="conversation-message-attachments" aria-label="消息附件">
    {attachments.map(attachment => <button
      type="button"
      key={attachment.path}
      className="conversation-message-attachment"
      title={`查看附件：${attachment.filename}`}
      onClick={() => onOpen?.(attachment)}
    >
      <FileText size={16}/><span><b>{attachment.filename}</b><small>{attachment.mime_type || '文件'}{attachmentSize(attachment.byte_size) ? ` · ${attachmentSize(attachment.byte_size)}` : ''}</small></span><PanelRightOpen size={13}/>
    </button>)}
    {references.map((reference, index) => <button type="button" key={`${reference.event_id}:${reference.content}`} className="conversation-message-attachment conversation-message-reference" aria-label={`查看会话引用 ${index + 1}`} title="查看引用内容" onClick={() => onOpenReference?.(reference)}>
      <Quote size={16}/><span><b>{`会话引用 ${index + 1}`}</b><small>已添加到本条消息</small></span>
      <PanelRightOpen size={13}/>
    </button>)}
    {workspaceReferences.map(reference => <button type="button" key={`${reference.path}:${JSON.stringify(reference.selection ?? {})}`} className="conversation-message-attachment conversation-message-workspace-reference" title={reference.path} onClick={() => { onOpenWorkspaceReference?.(reference); window.dispatchEvent(new CustomEvent('flowweave:open-workspace-selection', { detail: reference })); }}>
      <FileText size={16}/><span><b>{reference.display_name}</b><small>{workspaceReferenceLabel(reference)}</small></span>
    </button>)}
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
const LARGE_MARKDOWN_THRESHOLD = 6_000;
const LARGE_MARKDOWN_PREVIEW_LENGTH = 2_000;

function MessageMarkdown({ children }: { children: string }) {
  const [expanded, setExpanded] = useState(() => children.length <= LARGE_MARKDOWN_THRESHOLD);
  useEffect(() => { setExpanded(children.length <= LARGE_MARKDOWN_THRESHOLD); }, [children]);
  if (!expanded) return <div className="conversation-markdown-preview">
    <pre>{children.slice(0, LARGE_MARKDOWN_PREVIEW_LENGTH)}</pre>
    <small>{`此消息共 ${children.length.toLocaleString()} 个字符；完整 Markdown、代码块和图片将在展开后解析。`}</small>
    <button type="button" onClick={() => setExpanded(true)}>渲染完整消息</button>
  </div>;
  return <Suspense fallback={<div className="conversation-markdown-loading">正在渲染消息…</div>}><ConversationMarkdown>{children}</ConversationMarkdown></Suspense>;
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
    return [{ event, kind: isUser ? 'user' : 'assistant', title: '', content: isUser ? displayContent : content }];
  }
  if (event.event_type === 'THOUGHT') return [{ event, kind: 'thought', title: '分析', content: thought || content }];
  if (event.event_type === 'CONDENSATION_REQUESTED') return [{ event, kind: 'condensation', title: '正在自动压缩上下文', content: '' }];
  if (event.event_type === 'CONDENSATION_COMPLETED') return [{ event, kind: 'condensation', title: '已自动压缩上下文', content: '' }];
  if (event.event_type === 'TOOL_CALL') return [{ event, kind: 'tool', title: eventName, content: thought || content }];
  if (event.event_type === 'TOOL_RESULT') return [{ event, kind: 'tool', title: eventName, content }];
  if (event.event_type === 'ERROR') return [{ event, kind: 'error', title: '执行遇到问题', content }];
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

function orderedEvents(events: OpenHandsConversationEvent[]): OpenHandsConversationEvent[] {
  // REST and live frames can arrive in a different order.  Event identity is
  // authoritative: preserve the stable API order between unrelated events,
  // but always render a parent before its descendants.
  const byId = new Map(events.map(event => [event.id, event]));
  const children = new Map<string, OpenHandsConversationEvent[]>();
  const roots: OpenHandsConversationEvent[] = [];
  for (const event of events) {
    const parentId = event.payload.parent_id;
    if (parentId && byId.has(parentId)) {
      const bucket = children.get(parentId) ?? [];
      bucket.push(event);
      children.set(parentId, bucket);
    } else roots.push(event);
  }
  const output: OpenHandsConversationEvent[] = [];
  const seen = new Set<string>();
  const visit = (event: OpenHandsConversationEvent) => {
    if (seen.has(event.id)) return;
    seen.add(event.id);
    output.push(event);
    for (const child of children.get(event.id) ?? []) visit(child);
  };
  for (const event of roots) visit(event);
  for (const event of events) visit(event);
  return output;
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
      const assistantMessage = candidate.event_type === 'MESSAGE'
        && !['user', 'human'].includes(String(candidate.payload.source ?? '').toLowerCase());
      const finishResponse = candidate.event_type === 'COMPLETED'
        && candidate.payload.event_name === 'FinishAction';
      return (assistantMessage || finishResponse) && Boolean(candidate.payload.content);
    });
  }
  const byId = new Map(events.map(candidate => [candidate.id, candidate]));
  const userAncestor = (candidate: OpenHandsConversationEvent): string | undefined => {
    const visited = new Set<string>();
    let current: OpenHandsConversationEvent | undefined = candidate;
    while (current && !visited.has(current.id)) {
      visited.add(current.id);
      const source = String(current.payload.source ?? '').toLowerCase();
      if (current.event_type === 'MESSAGE' && (source === 'user' || source === 'human')) {
        return current.id;
      }
      const parentId: string | undefined = current.payload.parent_id ?? undefined;
      current = parentId ? byId.get(parentId) : undefined;
    }
    return undefined;
  };
  const root = userAncestor(event);
  if (!root) return false;
  return events.some(candidate => {
    if (candidate.event_type !== 'MESSAGE') return false;
    const source = String(candidate.payload.source ?? '').toLowerCase();
    return source !== 'user' && source !== 'human'
      && Boolean(candidate.payload.content)
      && userAncestor(candidate) === root;
  });
}

function turnsFor(events: OpenHandsConversationEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | undefined;
  const ordered = orderedEvents(events);
  for (const event of ordered) {
    if (isHistoricalAutoTitleError(event, ordered)) continue;
    for (const item of itemsFor(event)) {
      if (item.kind === 'user') {
        current = { id: item.event.id, user: item, activity: [] };
        turns.push(current);
        continue;
      }
      if (!current) {
        current = { id: item.event.id, activity: [] };
        turns.push(current);
      }
      // A manual condensation can be requested while the Agent is idle, after
      // the preceding turn already reached a formal assistant/error terminal.
      // Keep that audit record standalone instead of attaching it to the old
      // turn and inventing elapsed work after the terminal. Automatic
      // condensation during a running turn remains in that turn.
      if (item.kind === 'condensation' && (current.assistant || current.activity.some(value => value.kind === 'error'))) {
        current = { id: item.event.id, activity: [item] };
        turns.push(current);
        continue;
      }
      if (item.kind === 'assistant') current.assistant = item;
      else current.activity.push(item);
    }
  }
  return turns;
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
  for (const item of items) {
    if (item.kind !== 'tool' || item.event.event_type !== 'TOOL_CALL') continue;
    const entry = { id: item.event.id, item, action: item, results: [] } satisfies ActivityEntry;
    actionsById.set(item.event.id, entry);
    const toolCallId = detailText(item.event.payload.tool_call_id);
    if (toolCallId) actionsByToolCall.set(toolCallId, entry);
  }
  const emitted = new Set<ActivityEntry>();
  for (const item of items) {
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

interface ActivityPresentation {
  title: string;
  status: string;
  thought?: string;
  command?: string;
  path?: string;
  operation?: string;
  exitCode?: string;
  actionDetails?: Record<string, unknown>;
  resultDetails?: Record<string, unknown>;
  resultTimestamp?: string;
}

interface ActivityStage {
  id: string;
  title?: string;
  entries: ActivityEntry[];
}

interface ActivityOperationGroup {
  id: string;
  entries: ActivityEntry[];
}

type ActivityStageRow =
  | { kind: 'entry'; entry: ActivityEntry }
  | { kind: 'operation-group'; group: ActivityOperationGroup };

function taskBoundaryEventIds(entries: ActivityEntry[]): ReadonlySet<string> {
  const ids = new Set<string>();
  const parentIds = new Map<string, string>();
  for (const entry of entries) {
    for (const item of [entry.action ?? entry.item, ...entry.results]) {
      const parentId = detailText(item.event.payload.parent_id);
      if (parentId) parentIds.set(item.event.id, parentId);
      if (item.event.payload.runtime_task) ids.add(item.event.id);
    }
  }
  for (const eventId of parentIds.keys()) {
    let current = parentIds.get(eventId);
    const seen = new Set<string>();
    while (current && !seen.has(current)) {
      if (ids.has(current)) { ids.add(eventId); break; }
      seen.add(current);
      current = parentIds.get(current);
    }
  }
  return ids;
}

function hasFailedOperationResult(entry: ActivityEntry): boolean {
  return entry.results.some(result => {
    const details = result.event.payload.details;
    if (details?.is_error === true) return true;
    const exitCode = details?.exit_code;
    return typeof exitCode === 'number' && Number.isFinite(exitCode) && exitCode !== 0;
  });
}

function isAggregateableOperation(entry: ActivityEntry, taskBoundaryIds: ReadonlySet<string>): boolean {
  const item = entry.action ?? entry.item;
  if (item.kind !== 'tool' || item.event.event_type !== 'TOOL_CALL') return false;
  if (item.event.payload.runtime_task || taskBoundaryIds.has(item.event.id)) return false;
  if (hasFailedOperationResult(entry)) return false;
  const risk = String(item.event.payload.security_risk ?? 'UNKNOWN');
  if (risk === 'MEDIUM' || risk === 'HIGH') return false;
  return ['TerminalAction', 'FileEditorAction'].includes(String(item.event.payload.event_name ?? ''));
}

function operationFollows(previous: ActivityEntry, next: ActivityEntry): boolean {
  const parentId = detailText((next.action ?? next.item).event.payload.parent_id);
  if (!parentId) return false;
  if (parentId === (previous.action ?? previous.item).event.id) return true;
  return previous.results.some(result => result.event.id === parentId);
}

function sameOperationBatch(previous: ActivityEntry, next: ActivityEntry): boolean {
  const previousItem = previous.action ?? previous.item;
  const nextItem = next.action ?? next.item;
  const previousResponse = detailText(previousItem.event.payload.llm_response_id);
  const nextResponse = detailText(nextItem.event.payload.llm_response_id);
  return Boolean(previousResponse && previousResponse === nextResponse) || operationFollows(previous, next);
}

function activityStageRows(entries: ActivityEntry[]): ActivityStageRow[] {
  const rows: ActivityStageRow[] = [];
  const taskBoundaryIds = taskBoundaryEventIds(entries);
  for (let index = 0; index < entries.length;) {
    const first = entries[index];
    if (!isAggregateableOperation(first, taskBoundaryIds)) {
      rows.push({ kind: 'entry', entry: first });
      index += 1;
      continue;
    }
    const group = [first];
    let cursor = index + 1;
    while (cursor < entries.length && isAggregateableOperation(entries[cursor], taskBoundaryIds) && sameOperationBatch(group.at(-1)!, entries[cursor])) {
      group.push(entries[cursor]);
      cursor += 1;
    }
    if (group.length < 2) rows.push({ kind: 'entry', entry: first });
    else rows.push({ kind: 'operation-group', group: { id: group.map(entry => entry.id).join(':'), entries: group } });
    index = group.length < 2 ? index + 1 : cursor;
  }
  return rows;
}

function operationGroupSummary(entries: ActivityEntry[]): string {
  let read = 0;
  let created = 0;
  let edited = 0;
  let commands = 0;
  for (const entry of entries) {
    const item = entry.action ?? entry.item;
    const eventName = String(item.event.payload.event_name ?? '');
    if (eventName === 'TerminalAction') { commands += 1; continue; }
    const command = detailContent(item.event.payload.details?.command).toLowerCase();
    if (command === 'view') read += 1;
    else if (command === 'create' || command === 'write') created += 1;
    else edited += 1;
  }
  const parts = [
    read ? `已读取 ${read} 个文件` : '',
    created ? `已创建 ${created} 个文件` : '',
    edited ? `已编辑 ${edited} 个文件` : '',
    commands ? `已运行 ${commands} 条命令` : '',
  ].filter(Boolean);
  return parts.join('，并') || `已完成 ${entries.length} 项操作`;
}

function operationGroupIsComplete(entries: ActivityEntry[]): boolean {
  return entries.every(entry => entry.results.length > 0);
}

function latestRunningOperation(entries: ActivityEntry[], workspaceRoot?: string | null): string {
  const runningEntry = [...entries].reverse().find(entry => entry.results.length === 0);
  return runningEntry
    ? activityPresentation(runningEntry, true, workspaceRoot).title
    : operationGroupSummary(entries);
}

function activityPresentation(entry: ActivityEntry, active: boolean, workspaceRoot?: string | null): ActivityPresentation {
  const item = entry.action ?? entry.item;
  if (item.kind === 'condensation') {
    return { title: item.title, status: item.event.event_type === 'CONDENSATION_COMPLETED' ? '已完成' : '处理中' };
  }
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
    const verb = operation === 'view' ? (failed ? '读取失败' : completed ? '已读取' : '正在读取')
      : ['create', 'write'].includes(operation) ? (failed ? '创建失败' : completed ? '已创建' : '正在创建')
        : operation === 'undo_edit' ? (failed ? '撤销失败' : completed ? '已撤销编辑' : '正在撤销编辑')
          : ['str_replace', 'insert', 'append'].includes(operation) ? (failed ? '编辑失败' : completed ? '已编辑' : '正在编辑')
            : failed ? '文件操作失败' : completed ? '已完成文件操作' : '正在处理文件';
    const displayPath = path ? workspacePath(path, workspaceRoot) : '';
    return {
      title: displayPath ? `${verb} ${displayPath}` : actionTitle(verb),
      status: failed ? '文件编辑器 · 失败' : completed ? '文件编辑器 · 已完成' : '文件编辑器',
      path: displayPath || undefined, operation: workspaceRelativeText(command, workspaceRoot) || undefined, thought, actionDetails: details, resultDetails,
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
  if (eventName === 'TaskAction') {
    const runtimeTask = item.event.payload.runtime_task;
    const agentType = typeof runtimeTask?.subagent_type === 'string' && runtimeTask.subagent_type.trim()
      ? runtimeTask.subagent_type.trim()
      : 'general-purpose';
    const description = typeof runtimeTask?.description === 'string' ? runtimeTask.description.trim() : '';
    const label = description || actionTitle(`子智能体 ${agentType}`);
    return {
      title: completed ? `子智能体 ${agentType} · ${label} · 已完成` : `子智能体 ${agentType} · ${label}`,
      status: completed ? '子智能体 · 已完成' : '子智能体 · 运行中',
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

function activityStageTitle(entry: ActivityEntry): string | undefined {
  const item = entry.action ?? entry.item;
  const eventName = String(item.event.payload.event_name ?? '');
  if (eventName !== 'TaskTrackerAction' && eventName !== 'TaskTrackerObservation') return undefined;
  const snapshot = taskListSnapshot(
    item.event.payload.details ?? {},
    entry.results.at(-1)?.event.payload.details ?? {},
    entry.results.at(-1)?.event.payload.timestamp ?? item.event.payload.timestamp,
  );
  if (!snapshot) return undefined;
  return snapshot.items.find(task => task.status === 'in_progress')?.title
    ?? snapshot.items.find(task => task.status === 'todo')?.title;
}

function activityStages(entries: ActivityEntry[]): ActivityStage[] {
  const stages: ActivityStage[] = [];
  let current: ActivityStage | undefined;
  for (const entry of entries) {
    // A TaskTracker observation is the only native event that explicitly
    // declares the current plan. Its in-progress task is therefore safe to
    // show as a stage label; never infer stages from tool order or command text.
    const title = activityStageTitle(entry);
    if (title) {
      current = { id: entry.id, title, entries: [entry] };
      stages.push(current);
      continue;
    }
    if (!current) {
      current = { id: 'ungrouped', entries: [] };
      stages.push(current);
    }
    current.entries.push(entry);
  }
  return stages;
}

function displayDetails(details: Record<string, unknown>, workspaceRoot?: string | null): string {
  const visible = Object.fromEntries(Object.entries(details).filter(([key]) => !['content', 'old_content', 'new_content'].includes(key)));
  return Object.keys(visible).length ? workspaceRelativeText(JSON.stringify(visible, null, 2), workspaceRoot).slice(0, 12_000) : '';
}

function ToolDetailPanel({ presentation, eventName, results, workspaceRoot }: { presentation: ActivityPresentation; eventName: string; results: Item[]; workspaceRoot?: string | null }) {
  const [expanded, setExpanded] = useState(false);
  const details = presentation.actionDetails ?? {};
  const resultDetails = presentation.resultDetails ?? {};
  const isTerminal = eventName.includes('Terminal');
  const isFile = eventName.includes('FileEditor');
  const isTaskTracker = eventName === 'TaskTrackerAction' || eventName === 'TaskTrackerObservation';
  const hasResultOutput = results.some(result => typeof result.content === 'string' && result.content.trim().length > 0);
  const hasDetail = Boolean(
    isTaskTracker || presentation.command || hasResultOutput || presentation.exitCode
    || Object.keys(details).length || Object.keys(resultDetails).length,
  );
  if (!hasDetail) return null;
  return <div className="conversation-tool-detail-panel" data-expanded={expanded || undefined}>
      <button type="button" className="conversation-tool-detail-toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
        {expanded ? '收起详情' : hasResultOutput ? '查看详情与输出' : '查看详情'}
      </button>
      {expanded && <ToolDetailContent
        details={details}
        resultDetails={resultDetails}
        results={results}
        presentation={presentation}
        isTerminal={isTerminal}
        isFile={isFile}
        isTaskTracker={isTaskTracker}
        workspaceRoot={workspaceRoot}
      />}
    </div>;
}

function ToolDetailContent({ details, resultDetails, results, presentation, isTerminal, isFile, isTaskTracker, workspaceRoot }: {
  details: Record<string, unknown>;
  resultDetails: Record<string, unknown>;
  results: Item[];
  presentation: ActivityPresentation;
  isTerminal: boolean;
  isFile: boolean;
  isTaskTracker: boolean;
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

function eventTime(item?: Item): number | undefined {
  return parsedEventTime(item?.event.payload.timestamp);
}

function parsedEventTime(raw: unknown): number | undefined {
  if (typeof raw !== 'string' || !raw) return undefined;
  // OpenHands 1.42.0 creates Event.timestamp with datetime.now().isoformat().
  // The Runtime container runs in UTC, but that value has no timezone suffix.
  // Browsers otherwise interpret it as local time and inflate an active turn by
  // the local UTC offset. Preserve explicitly zoned timestamps as-is.
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`;
  const value = Date.parse(normalized);
  return Number.isFinite(value) ? value : undefined;
}

function condensationTriggeredAt(item: Item): number | undefined {
  return parsedEventTime(item.event.payload.condensation_triggered_at) ?? eventTime(item);
}

function condensationCompletedAt(item: Item): number | undefined {
  return parsedEventTime(item.event.payload.condensation_completed_at) ?? eventTime(item);
}

function turnProcessBlocks(
  items: Item[],
  startedAt: number | undefined,
  finishedAt: number | undefined,
  active: boolean,
): TurnProcessBlock[] {
  const hasCondensation = items.some(item => item.kind === 'condensation');
  if (!hasCondensation) {
    return [{ kind: 'activity', id: 'activity-0', items, startedAt, finishedAt, active }];
  }
  if (items.every(item => item.kind === 'condensation')) {
    return [{ kind: 'condensation', id: 'condensation-0', items }];
  }

  const blocks: TurnProcessBlock[] = [];
  let activityItems: Item[] = [];
  let condensationItems: Item[] = [];
  let segmentStartedAt = startedAt;
  let compressionPending = false;
  let sequence = 0;
  const flushActivity = (segmentFinishedAt: number | undefined, force = false) => {
    if (!activityItems.length && !force) return;
    blocks.push({
      kind: 'activity',
      id: `activity-${sequence++}`,
      items: activityItems,
      startedAt: segmentStartedAt,
      finishedAt: segmentFinishedAt,
      active: false,
    });
    activityItems = [];
  };
  const flushCondensation = () => {
    if (!condensationItems.length) return;
    blocks.push({
      kind: 'condensation',
      id: `condensation-${sequence++}`,
      items: condensationItems,
    });
    condensationItems = [];
  };

  for (const item of items) {
    if (item.kind !== 'condensation') {
      activityItems.push(item);
      continue;
    }
    if (item.event.event_type === 'CONDENSATION_REQUESTED') {
      flushActivity(condensationTriggeredAt(item), true);
      condensationItems.push(item);
      compressionPending = true;
      segmentStartedAt = undefined;
      continue;
    }

    if (!compressionPending) {
      flushActivity(condensationTriggeredAt(item), true);
    }
    condensationItems.push(item);
    flushCondensation();
    compressionPending = false;
    segmentStartedAt = condensationCompletedAt(item);
  }

  if (compressionPending) {
    flushCondensation();
  } else {
    flushActivity(finishedAt, true);
    const latestActivity = [...blocks].reverse().find(
      (block): block is Extract<TurnProcessBlock, { kind: 'activity' }> => block.kind === 'activity',
    );
    if (latestActivity) latestActivity.active = active;
  }
  return blocks;
}

function formatEventTime(raw: unknown): string {
  if (typeof raw !== 'string' || !raw) return '时间未知';
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : raw + 'Z';
  const value = Date.parse(normalized);
  if (!Number.isFinite(value)) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(value);
}

/** Format the wall-clock time attached to an actual OpenHands message event. */
function formatMessageTime(raw: unknown): string | undefined {
  if (typeof raw !== 'string' || !raw) return undefined;
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`;
  const value = Date.parse(normalized);
  if (!Number.isFinite(value)) return undefined;
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(value);
}

function condensationReason(item: Item): string {
  const detail = item.event.payload.condensation_reason_detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (item.event.event_type === 'CONDENSATION_REQUESTED') {
    return 'OpenHands 收到显式压缩请求，正在整理较早的上下文。';
  }
  return 'OpenHands 自动上下文保护已触发；原生事件未保存更细的触发原因。';
}

function CondensationNotices({ items }: { items: Item[] }) {
  if (!items.length) return null;
  const requestIds = new Set(items
    .filter(item => item.event.event_type === 'CONDENSATION_REQUESTED')
    .map(item => item.event.id));
  return <div className="conversation-condensation-timeline" aria-label="上下文压缩记录">
    {items.flatMap(item => {
      if (item.event.event_type === 'CONDENSATION_REQUESTED') {
        return [<article className="conversation-condensation-notice triggered" key={item.event.id} role="status">
          <CircleAlert size={17}/><div><header><b>已触发上下文压缩</b><time>{formatEventTime(item.event.payload.timestamp)}</time></header><p>{condensationReason(item)}</p></div>
        </article>];
      }
      const requestId = typeof item.event.payload.condensation_request_event_id === 'string'
        ? item.event.payload.condensation_request_event_id
        : undefined;
      const needsRecoveredStart = !requestId || !requestIds.has(requestId);
      const forgotten = Array.isArray(item.event.payload.forgotten_event_ids)
        ? item.event.payload.forgotten_event_ids.length
        : undefined;
      const completedText = forgotten
        ? '已完成摘要并从模型上下文中移除 ' + forgotten + ' 个较早事件；完整事件记录仍然保留。'
        : '已完成较早上下文的摘要；完整事件记录仍然保留。';
      return [
        ...(needsRecoveredStart ? [<article className="conversation-condensation-notice triggered" key={item.event.id + '-triggered'} role="status">
          <CircleAlert size={17}/><div><header><b>已触发上下文压缩</b><time>{formatEventTime(item.event.payload.condensation_triggered_at ?? item.event.payload.timestamp)}</time></header><p>{condensationReason(item)}</p></div>
        </article>] : []),
        <article className="conversation-condensation-notice completed" key={item.event.id} role="status">
          <Check size={17}/><div><header><b>上下文压缩已完成</b><time>{formatEventTime(item.event.payload.condensation_completed_at ?? item.event.payload.timestamp)}</time></header><p>{completedText}</p></div>
        </article>,
      ];
    })}
  </div>;
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

function useElapsedSeconds(startedAt: number | undefined, finishedAt: number | undefined, active: boolean): number | undefined {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || startedAt === undefined) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);
  if (startedAt === undefined) return undefined;
  return Math.max(0, ((finishedAt ?? now) - startedAt) / 1000);
}

function activeToolLabel(eventName: string, toolName?: string, summary?: string, details?: Record<string, unknown>): string {
  const description = typeof details?.description === 'string' ? details.description.trim() : '';
  const explicitTool = summary?.trim() || toolName?.trim();
  if (eventName.includes('Terminal')) return '正在后台执行命令';
  if (eventName.includes('FileEditor')) return '正在处理文件';
  if (eventName.includes('Browser')) return '正在执行浏览器操作';
  if (eventName.includes('MCP')) return '正在调用 MCP 工具';
  if (eventName.includes('Skill')) return '正在使用技能';
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
  const latest = entries.at(-1)?.item;
  if (latest?.kind === 'condensation' && latest.event.event_type === 'CONDENSATION_REQUESTED') return '正在压缩上下文';
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

  const stalledSubagent = monitoring.active_subagents.find(task => task.possibly_stuck);
  if (stalledSubagent) return '子智能体仍在运行，等待其返回';
  if (fallback === '正在思考') return 'OpenHands 会话连接正常，等待响应';
  return fallback;
}

function CurrentTurnStatus({ items, liveText, requestSubmitting, monitoring, connectionState = 'connected' }: {
  items: Item[];
  liveText: string;
  requestSubmitting: boolean;
  monitoring?: AgentActivitySummary;
  connectionState?: ConversationConnectionState;
}) {
  const activityLabel = liveText
    ? '正在生成回复'
    : activeActivityLabel(groupedActivities(items), requestSubmitting);
  const label = liveText || requestSubmitting
    ? activityLabel
    : staleActivityLabel(activityLabel, monitoring, connectionState);
  return <div className="conversation-turn-status" role="status" aria-label={label}>
    <span>{label}</span>
    <span className="conversation-turn-status-dots" aria-hidden="true"><i/><i/><i/></span>
  </div>;
}

function taskAvatarStatus(entry: ActivityEntry, item: Item): 'running' | 'completed' | 'error' {
  const phases = [item.event, ...entry.results.map(result => result.event)]
    .map(event => event.payload.runtime_task?.phase);
  if (phases.includes('ERROR')) return 'error';
  return phases.includes('COMPLETED') ? 'completed' : 'running';
}

function ActivityEntryRow({ entry, active, avatarSlots, workspaceRoot }: {
  entry: ActivityEntry;
  active: boolean;
  avatarSlots: ReadonlyMap<string, SubagentAvatarSlot>;
  workspaceRoot?: string | null;
}) {
  const item = entry.action ?? entry.item;
  const Icon = item.kind === 'error' ? CircleAlert : item.kind === 'thought' || item.kind === 'condensation' ? Sparkles : Wrench;
  const eventName = String(item.event.payload.event_name ?? '');
  const avatarSlot = eventName === 'TaskAction' || eventName === 'TaskObservation'
    ? subagentAvatarSlotForEvent(item.event, avatarSlots)
    : undefined;
  const ToolIcon = eventName.includes('Terminal') ? SquareTerminal : eventName.includes('FileEditor') ? FileText : Icon;
  const taskAvatar = avatarSlot && <SubagentAvatar slot={avatarSlot} status={taskAvatarStatus(entry, item)} size={14}/>;
  const presentation = activityPresentation(entry, active, workspaceRoot);
  const toolDetail = item.kind === 'tool' ? <ToolDetailPanel presentation={presentation} eventName={eventName} results={entry.results} workspaceRoot={workspaceRoot}/> : null;
  if (item.kind === 'thought') return <article className="conversation-activity-row thought">
    <MessageMarkdown>{presentation.thought ?? item.content}</MessageMarkdown>
  </article>;
  if (item.kind === 'tool' && toolDetail) return <div className="conversation-tool-entry">
    {presentation.thought && <article className="conversation-activity-row thought">
      <MessageMarkdown>{presentation.thought}</MessageMarkdown>
    </article>}
    <details className="conversation-activity-row tool conversation-tool-detail">
      <summary aria-label={`查看执行详情：${presentation.title}`}>{taskAvatar ?? <ToolIcon size={14}/>}<div><b title={presentation.title}>{presentation.title}</b></div></summary>
      {toolDetail}
    </details>
  </div>;
  return <article className={`conversation-activity-row ${item.kind}`}>
    {taskAvatar ?? <ToolIcon size={14}/>}<div className="conversation-activity-content"><b title={presentation.title}>{presentation.title}</b><small>{presentation.status}</small>
      {presentation.thought && <span className="conversation-activity-thought"><MessageMarkdown>{presentation.thought}</MessageMarkdown></span>}
    </div>
  </article>;
}

function OperationGroup({ group, active, avatarSlots, workspaceRoot }: {
  group: ActivityOperationGroup;
  active: boolean;
  avatarSlots: ReadonlyMap<string, SubagentAvatarSlot>;
  workspaceRoot?: string | null;
}) {
  const completed = operationGroupIsComplete(group.entries);
  const completedCount = group.entries.filter(entry => entry.results.length > 0).length;
  const summary = completed ? operationGroupSummary(group.entries) : latestRunningOperation(group.entries, workspaceRoot);
  const [open, setOpen] = useState(!completed);
  const wasCompleted = useRef(completed);
  useEffect(() => {
    if (!wasCompleted.current && completed) setOpen(false);
    wasCompleted.current = completed;
  }, [completed]);
  const progress = completed
    ? `${group.entries.length} 项原生操作`
    : `${completedCount} / ${group.entries.length} 已完成`;
  return <details className={`conversation-operation-group${completed ? '' : ' active'}`} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary aria-label={`查看操作批次：${summary}`}><Wrench size={14}/><span><b>{summary}</b><small>{progress}</small></span>{!completed && <LoaderCircle className="conversation-operation-group-spinner" size={13}/>}<ChevronRight size={13}/></summary>
    <div className="conversation-operation-group-list">
      {group.entries.map(entry => <ActivityEntryRow key={entry.id} entry={entry} active={active} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/>)}
    </div>
  </details>;
}

function ActivityGroup({ items, active, liveText, startedAt, finishedAt, avatarSlots, workspaceRoot }: {
  items: Item[];
  active: boolean;
  liveText?: string;
  startedAt?: number;
  finishedAt?: number;
  avatarSlots: ReadonlyMap<string, SubagentAvatarSlot>;
  workspaceRoot?: string | null;
}) {
  const elapsedSeconds = useElapsedSeconds(startedAt, finishedAt, active);
  const entries = groupedActivities(items);
  const stages = activityStages(entries);
  const itemCount = entries.length + (liveText ? 1 : 0);
  const [open, setOpen] = useState(active);
  useEffect(() => { setOpen(active); }, [active]);
  const label = active
    ? elapsedSeconds === undefined ? '处理中' : `已耗时 ${formatDuration(elapsedSeconds)}`
    : finishedAt === undefined || elapsedSeconds === undefined ? '工作过程' : `耗时 ${formatDuration(elapsedSeconds)}`;
  const summary = <><ChevronRight size={14}/><span>{label}</span>{itemCount > 0 && <small>{itemCount} 项</small>}{active && <LoaderCircle className="conversation-activity-spin" size={13}/>}</>;
  const hasDetails = itemCount > 0;
  if (!hasDetails) return <div className="conversation-activity-group summary-only"><div className="conversation-activity-summary">{summary}</div></div>;
  return <details className={`conversation-activity-group${active ? ' active' : ''}`} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{summary}</summary>
    <div className="conversation-activity-list">
      {stages.map(stage => <section className={`conversation-activity-stage${stage.title ? '' : ' unlabelled'}`} key={stage.id}>
        {stage.title && <header><span>阶段</span><b>{stage.title}</b></header>}
        {activityStageRows(stage.entries).map(row => row.kind === 'operation-group'
          ? <OperationGroup key={row.group.id} group={row.group} active={active} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/>
          : <ActivityEntryRow key={row.entry.id} entry={row.entry} active={active} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/>)}
      </section>)}
      {liveText && <article className="conversation-activity-row thought live-text"><MessageMarkdown>{liveText}</MessageMarkdown></article>}
    </div>
  </details>;
}

function AgentReply({ event, content, changes = [], onFork, onPreviewCandidateFile, onReviewChanges, workspaceRoot, highlightReferenceSource = false }: {
  event: OpenHandsConversationEvent;
  content: string;
  changes?: WorkspaceFileChange[];
  onFork?: () => void;
  onPreviewCandidateFile?: (fieldKey: string, relativePath: string) => void;
  onReviewChanges?: (changes: WorkspaceFileChange[]) => void;
  workspaceRoot?: string | null;
  highlightReferenceSource?: boolean;
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
  return <article className={`conversation-message assistant${highlightReferenceSource ? ' conversation-reference-source-highlight' : ''}`} data-conversation-event-id={eventId} data-turn-terminal="true" data-event-id={eventId}>
    {candidateMessage.businessConclusion ? <MessageMarkdown>{candidateMessage.businessConclusion}</MessageMarkdown> : !candidateMessage.outputs && content ? <MessageMarkdown>{content}</MessageMarkdown> : null}
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

function conversationReferenceForSelection(selection: Selection, surface: HTMLElement): ConversationReference | undefined {
  if (!selection.rangeCount) return undefined;
  const content = selection.toString().trim();
  if (!content) return undefined;
  const range = selection.getRangeAt(0);
  const start = elementForNode(range.startContainer);
  const end = elementForNode(range.endContainer);
  const message = start?.closest<HTMLElement>('[data-conversation-event-id]');
  if (!message || !end || !surface.contains(message) || !message.contains(range.startContainer) || !message.contains(range.endContainer)) return undefined;
  const eventId = message.dataset.conversationEventId;
  return eventId ? { eventId, content } : undefined;
}

interface FailurePresentation {
  title: string;
  content: string;
}

function presentConversationFailure(code: string, detail: string): FailurePresentation {
  const normalizedCode = code.toLowerCase();
  const normalizedDetail = detail.toLowerCase();
  const hasCode = (...names: string[]) => names.some(name => normalizedCode === name.toLowerCase());
  const contains = (...terms: string[]) => terms.some(term => normalizedDetail.includes(term.toLowerCase()));

  if (hasCode('BadGatewayError', 'GatewayTimeoutError', 'LLMServiceUnavailableError', 'ServiceUnavailableError') || contains('bad gateway', 'gateway timeout', '502', '503', '504', 'tengine')) {
    return {
      title: '模型服务暂时不可用',
      content: '模型服务暂时没有响应。本轮已停止，请稍后重试或切换模型。',
    };
  }
  if (hasCode('LLMNoResponseError') && contains('ResponseIncompleteEvent', 'response incomplete', 'incomplete response')) {
    return {
      title: '模型返回不完整响应',
      content: '模型服务已开始返回结果，但没有发送可确认完成的响应。本轮已在重试后停止；请稍后重试，或切换模型配置。',
    };
  }
  if (hasCode('LLMNoResponseError') || contains('without a completed response', 'response choices is less than 1', 'empty response')) {
    return {
      title: '模型没有返回有效内容',
      content: '模型调用结束后未收到可用回复。这通常是模型服务或网关返回空响应，不代表浏览器网络已断开；请稍后重试或切换模型配置。',
    };
  }
  if (hasCode('LLMTimeoutError', 'LiteLLMTimeout', 'ReadTimeout', 'TimeoutError') || contains('timed out', 'timeout')) {
    return {
      title: '模型响应超时',
      content: '模型服务未能在允许时间内完成响应。本轮已停止；请稍后重试，或改用响应更快的模型配置。',
    };
  }
  if (contains('service unavailable', 'upstream unavailable')) {
    return {
      title: '模型服务暂不可用',
      content: '当前模型服务或其上游网关暂时不可用。这是模型服务侧故障，不是浏览器页面断线；请稍后重试或切换模型配置。',
    };
  }
  if (hasCode('APIConnectionError', 'ConnectError', 'ConnectionRefused', 'RequestError') || contains('connection refused', 'connection reset', 'connection failed', 'connect error')) {
    return {
      title: '模型网关连接失败',
      content: '运行时无法连接当前模型服务的网关。请检查模型服务连通性或切换模型配置；FlowWeave 会话本身不一定断开。',
    };
  }
  if (hasCode('LLMRateLimitError', 'RateLimitError', 'UsageLimitReachedError') || contains('rate limit', 'usage limit', 'quota', 'insufficient quota')) {
    return {
      title: '模型账户额度或速率受限',
      content: '模型服务拒绝了本次请求：当前账户额度不足或调用频率受限。请等待额度恢复，或选择有可用额度的模型配置后重新思考。',
    };
  }
  if (hasCode('AuthenticationError', 'UnauthorizedError', 'PermissionDeniedError', 'InvalidAPIKeyError') || contains('invalid api key', 'authentication', 'unauthorized', 'forbidden')) {
    return {
      title: '模型凭据无效或无权限',
      content: '当前模型配置的凭据无效、已过期或没有调用权限。请在模型配置中重新测试或更新授权后重试。',
    };
  }
  if (hasCode('LLMContextWindowExceededError', 'ContextWindowExceededError') || contains('context window', 'maximum context length', 'too many tokens')) {
    return {
      title: '请求超出模型上下文限制',
      content: '当前会话上下文超过了模型可接受的长度。请压缩上下文、拆分问题，或切换到上下文窗口更大的模型配置。',
    };
  }
  if (hasCode('BadRequestError', 'InvalidRequestError', 'UnsupportedParamsError') || contains('invalid request', 'unsupported parameter', 'unsupported model')) {
    return {
      title: '模型请求不被接受',
      content: '模型服务拒绝了本次请求的参数、模型或协议格式。请检查该模型配置与当前能力是否兼容后重试。',
    };
  }
  if (hasCode('ContentPolicyViolationError', 'SafetyError') || contains('content policy', 'safety policy')) {
    return {
      title: '模型安全策略拒绝了请求',
      content: '模型服务因其安全策略拒绝处理本次输入。请调整请求内容后再试。',
    };
  }
  return {
    title: '本轮未能完成',
    content: '模型服务未能完成本次请求。请稍后重试；若反复出现，可切换模型。',
  };
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

function ConversationFailure({ item, taskControl = [] }: { item: Item; taskControl?: RuntimeTaskControlSnapshot[] }) {
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
  const presentation = presentConversationFailure(code, item.content);
  return <article className="conversation-failure" data-turn-terminal="true" data-event-id={item.event.id} role="status">
    <CircleAlert size={15}/><div><b>{presentation.title}</b><p>{presentation.content}</p></div>
  </article>;
}

export function ConversationSurface({ events, liveText, isGenerating, isPaused: _isPaused = false, requestStartedAt, requestSubmitting = false, rewritePending = false, condensationStatus, onRetryCondensation, onRewrite, onFork, onOpenAttachment, onOpenWorkspaceReference, onPreviewCandidateFile, onReviewChanges, workspaceRoot, onAddReference, taskControl = [], monitoring, connectionState }: {
  events: OpenHandsConversationEvent[];
  liveText: string;
  isGenerating: boolean;
  /** Compatibility-only input; presentation follows OpenHands terminal events. */
  isPaused?: boolean;
  requestStartedAt?: number;
  requestSubmitting?: boolean;
  rewritePending?: boolean;
  condensationStatus?: { state: 'running' | 'failed'; startedAt: number; message?: string };
  onRetryCondensation?: () => void;
  onRewrite?: (eventId: string, content: string) => void;
  onFork?: (eventId: string) => void;
  onOpenAttachment?: (attachment: AgentAttachment) => void;
  onOpenWorkspaceReference?: (reference: AgentWorkspaceReference) => void;
  onPreviewCandidateFile?: (fieldKey: string, relativePath: string) => void;
  onReviewChanges?: (changes: WorkspaceFileChange[]) => void;
  workspaceRoot?: string | null;
  onAddReference?: (reference: ConversationReference) => void;
  taskControl?: RuntimeTaskControlSnapshot[];
  monitoring?: AgentActivitySummary;
  connectionState?: ConversationConnectionState;
}) {
  // Kept only while older workbench callers still provide this field.
  // Rendering never derives a timeout, pause, or retry decision from it.
  void _isPaused;
  const surface = useRef<HTMLElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const shell = useRef<HTMLDivElement>(null);
  const initialPositioned = useRef(false);
  const followLatest = useRef(true);
  const wasGenerating = useRef(isGenerating);
  const copyResetTimer = useRef<number | undefined>(undefined);
  const referenceHighlightStartTimer = useRef<number | undefined>(undefined);
  const referenceHighlightTimer = useRef<number | undefined>(undefined);
  const [isAtLatest, setIsAtLatest] = useState(true);
  const [editingEventId, setEditingEventId] = useState<string>();
  const [editingContent, setEditingContent] = useState('');
  const [copiedEventId, setCopiedEventId] = useState<string>();
  const [messagePreview, setMessagePreview] = useState<{ id: string; content: string; index: number; top: number }>();
  const [condensationElapsed, setCondensationElapsed] = useState(0);
  const [selectedReference, setSelectedReference] = useState<{ reference: ConversationReference; left: number; top: number }>();
  const [viewingReference, setViewingReference] = useState<AgentConversationReference>();
  const [highlightedReferenceEventId, setHighlightedReferenceEventId] = useState<string>();
  // Pausing a tool makes OpenHands emit one synthetic AgentErrorEvent. Keep
  // the authoritative event for recovery and audit, but it is neither an
  // execution failure nor useful conversation content.
  const visibleEvents = useMemo(
    () => events.filter(event => !isPauseInterruptionEvent(event)),
    [events],
  );
  const turns = useMemo(() => turnsFor(visibleEvents), [visibleEvents]);
  const avatarSlots = useMemo(() => subagentAvatarSlots(visibleEvents), [visibleEvents]);
  const userMessageNavigation = useMemo<UserMessageNavigationItem[]>(() => turns.flatMap(turn => turn.user ? [{
    id: turn.user.event.id,
    content: turn.user.content,
  }] : []), [turns]);
  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    followLatest.current = true;
    setIsAtLatest(true);
    const element = surface.current;
    element?.scrollTo({ top: element.scrollHeight, behavior });
  }, []);
  const updateScrollPosition = useCallback(() => {
    const element = surface.current;
    if (!element) return;
    const atLatest = element.scrollHeight - element.scrollTop - element.clientHeight <= 16;
    followLatest.current = atLatest;
    setIsAtLatest(atLatest);
  }, []);
  const scrollToUserMessage = useCallback((eventId: string) => {
    const element = surface.current;
    const target = element?.querySelectorAll<HTMLElement>('[data-user-event-id]');
    const message = Array.from(target ?? []).find(item => item.dataset.userEventId === eventId);
    if (!element || !message) return;
    const top = message.getBoundingClientRect().top - element.getBoundingClientRect().top + element.scrollTop - 18;
    followLatest.current = false;
    element.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
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
  const handleScroll = useCallback(() => {
    updateScrollPosition();
  }, [updateScrollPosition]);
  const scrollToTerminalStart = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const terminals = surface.current?.querySelectorAll<HTMLElement>('[data-turn-terminal="true"]');
    const terminal = terminals?.[terminals.length - 1];
    if (!terminal) return scrollToLatest(behavior);
    terminal.scrollIntoView({ block: 'start', behavior });
    window.requestAnimationFrame(updateScrollPosition);
  }, [scrollToLatest, updateScrollPosition]);
  const currentHasTerminal = isGenerating && Boolean(turns.at(-1)?.assistant || turns.at(-1)?.activity.some(item => item.kind === 'error'));
  useLayoutEffect(() => {
    if (!initialPositioned.current && (turns.length || liveText || isGenerating)) {
      initialPositioned.current = true;
      scrollToLatest('auto');
    } else if (!wasGenerating.current && isGenerating) {
      scrollToLatest('smooth');
    } else if (wasGenerating.current && !isGenerating && followLatest.current) {
      scrollToTerminalStart('auto');
    } else if (followLatest.current && !currentHasTerminal) {
      scrollToLatest('auto');
    }
    wasGenerating.current = isGenerating;
  }, [currentHasTerminal, isGenerating, liveText, scrollToLatest, scrollToTerminalStart, turns.length]);
  useLayoutEffect(() => {
    const observedContent = content.current;
    if (!observedContent || typeof ResizeObserver === 'undefined') return;
    let frame: number | undefined;
    const observer = new ResizeObserver(() => {
      // Lazy Markdown and content-visibility can make historical rows taller
      // after the initial restoration scroll. Keep following only when the
      // user was already at the latest message; never pull them from history.
      if (!followLatest.current || frame !== undefined) return;
      frame = window.requestAnimationFrame(() => {
        frame = undefined;
        if (followLatest.current) scrollToLatest('auto');
      });
    });
    observer.observe(observedContent);
    return () => {
      observer.disconnect();
      if (frame !== undefined) window.cancelAnimationFrame(frame);
    };
  }, [scrollToLatest]);
  useEffect(() => () => {
    if (copyResetTimer.current) window.clearTimeout(copyResetTimer.current);
    if (referenceHighlightStartTimer.current) window.clearTimeout(referenceHighlightStartTimer.current);
    if (referenceHighlightTimer.current) window.clearTimeout(referenceHighlightTimer.current);
  }, []);
  useEffect(() => {
    if (condensationStatus?.state !== 'running') return;
    const update = () => setCondensationElapsed(Math.max(0, Date.now() - condensationStatus.startedAt));
    update();
    const timer = window.setInterval(update, 1_000);
    if (followLatest.current) window.requestAnimationFrame(() => scrollToLatest('smooth'));
    return () => window.clearInterval(timer);
  }, [condensationStatus, scrollToLatest]);
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
    if (!onAddReference || !surface.current) return;
    const selection = window.getSelection();
    if (!selection) return;
    const reference = conversationReferenceForSelection(selection, surface.current);
    if (!reference) { setSelectedReference(undefined); return; }
    const pointerMessage = event.target instanceof Node
      ? elementForNode(event.target)?.closest<HTMLElement>('[data-conversation-event-id]')
      : undefined;
    if (pointerMessage?.dataset.conversationEventId !== reference.eventId) { setSelectedReference(undefined); return; }
    const bounds = selection.getRangeAt(0).getBoundingClientRect();
    if (!bounds.width && !bounds.height) { setSelectedReference(undefined); return; }
    setSelectedReference({
      reference,
      left: Math.min(Math.max(12, bounds.left), Math.max(12, window.innerWidth - 172)),
      top: Math.min(bounds.bottom + 8, Math.max(12, window.innerHeight - 44)),
    });
  }, [onAddReference]);
  const locateReferenceSource = useCallback(() => {
    if (!viewingReference || !surface.current) return;
    const source = Array.from(surface.current.querySelectorAll<HTMLElement>('[data-conversation-event-id]'))
      .find(item => item.dataset.conversationEventId === viewingReference.event_id);
    setViewingReference(undefined);
    if (!source) return;
    source.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setHighlightedReferenceEventId(undefined);
    if (referenceHighlightStartTimer.current) window.clearTimeout(referenceHighlightStartTimer.current);
    if (referenceHighlightTimer.current) window.clearTimeout(referenceHighlightTimer.current);
    referenceHighlightStartTimer.current = window.setTimeout(() => {
      setHighlightedReferenceEventId(viewingReference.event_id);
      referenceHighlightTimer.current = window.setTimeout(() => {
        setHighlightedReferenceEventId(current => current === viewingReference.event_id ? undefined : current);
      }, 1_800);
    }, 600);
  }, [viewingReference]);
  const lastUserEventId = useMemo(() => [...turns].reverse().find(turn => turn.user)?.user?.event.id, [turns]);
  if (!turns.length && !liveText && !isGenerating && !condensationStatus) return <div className="conversation-surface-empty"><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  const showJumpToLatest = !isAtLatest && Boolean(turns.length || liveText || isGenerating);
  return <div ref={shell} className="conversation-surface-shell">
    {userMessageNavigation.length > 0 && <nav className="conversation-message-index" aria-label="用户消息导航">
      {userMessageNavigation.map((message, index) => <button
        type="button"
        key={message.id}
        aria-label={`定位到用户消息：${messageSummary(message.content)}`}
        aria-describedby={messagePreview?.id === message.id ? 'conversation-message-preview' : undefined}
        onPointerEnter={event => showMessagePreview(message, index, event.currentTarget)}
        onFocus={event => showMessagePreview(message, index, event.currentTarget)}
        onPointerLeave={() => setMessagePreview(current => current?.id === message.id ? undefined : current)}
        onBlur={() => setMessagePreview(current => current?.id === message.id ? undefined : current)}
        onClick={() => scrollToUserMessage(message.id)}
      >
        <span className="conversation-message-index-tick" aria-hidden="true"/>
      </button>)}
    </nav>}
    {messagePreview && <aside id="conversation-message-preview" className="conversation-message-index-tooltip" role="tooltip" style={{ top: messagePreview.top }}><span>{messagePreview.content || '（空消息）'}</span></aside>}
    <section ref={surface} className="conversation-surface" aria-live="polite" onScroll={() => { handleScroll(); setSelectedReference(undefined); }} onPointerUp={offerSelectedReference}>
      <div ref={content} className="conversation-surface-content">
      {turns.map((turn, index) => {
        const isCurrent = index === turns.length - 1 && isGenerating;
        const failures = turn.activity.filter(item => item.kind === 'error');
        const startedAt = eventTime(turn.user) ?? (isCurrent ? requestStartedAt : undefined);
        const finishedAt = eventTime(turn.assistant ?? failures.at(-1));
        const processBlocks = turnProcessBlocks(
          turn.activity.filter(item => item.kind !== 'error'),
          startedAt,
          finishedAt,
          isCurrent && !turn.assistant && !failures.length,
        );
        const fileChanges = workspaceFileChanges(turn.activity.map(item => item.event));
        const userTimestamp = turn.user ? formatMessageTime(turn.user.event.payload.timestamp) : undefined;
        return <section className="conversation-turn" key={turn.id} data-conversation-turn={turn.id}>
          {turn.user && <div className="conversation-user-message">{editingEventId === turn.user.event.id
            ? <form className="conversation-message-edit" onSubmit={event => { event.preventDefault(); if (editingContent.trim()) onRewrite?.(turn.user!.event.id, editingContent.trim()); }}><textarea aria-label="编辑已发送消息" value={editingContent} disabled={rewritePending} onChange={event => setEditingContent(event.target.value)}/><footer><button type="button" onClick={() => setEditingEventId(undefined)}>取消</button><button type="submit" disabled={!editingContent.trim() || rewritePending}>重新思考</button></footer></form>
            : <article data-user-event-id={turn.user.event.id} data-conversation-event-id={turn.user.event.id} className={`conversation-message user${highlightedReferenceEventId === turn.user.event.id ? ' conversation-reference-source-highlight' : ''}`}>{turn.user.content && <div className="conversation-message-content"><MessageMarkdown>{turn.user.content}</MessageMarkdown></div>}<MessageAttachments attachments={eventAttachments(turn.user.event)} references={turn.user.event.payload.conversation_references} workspaceReferences={turn.user.event.payload.workspace_references} onOpen={onOpenAttachment} onOpenReference={setViewingReference} onOpenWorkspaceReference={onOpenWorkspaceReference}/><footer className="conversation-message-meta user">{userTimestamp && <time dateTime={typeof turn.user.event.payload.timestamp === 'string' ? turn.user.event.payload.timestamp : undefined}>{userTimestamp}</time>}<div className="conversation-message-actions"><button type="button" className="conversation-message-copy" aria-label={copiedEventId === turn.user.event.id ? '消息已复制' : '复制消息'} title={copiedEventId === turn.user.event.id ? '已复制' : '复制消息'} onClick={() => copyUserMessage(turn.user!.event.id, turn.user!.content)}>{copiedEventId === turn.user.event.id ? <Check size={13}/> : <Copy size={13}/>}</button>{lastUserEventId === turn.user.event.id && <button type="button" className="conversation-message-rewrite" aria-label="编辑并重新思考" title="编辑并重新思考" onClick={() => { setEditingEventId(turn.user!.event.id); setEditingContent(turn.user!.content); }}><Pencil size={13}/></button>}</div></footer></article>}</div>}
          {processBlocks.map((block, blockIndex) => block.kind === 'condensation'
            ? <CondensationNotices key={block.id} items={block.items}/>
            : <ActivityGroup
              key={block.id}
              items={block.items}
              active={block.active}
              liveText={isCurrent && blockIndex === processBlocks.length - 1 ? liveText : undefined}
              startedAt={block.startedAt}
              finishedAt={block.finishedAt}
              avatarSlots={avatarSlots}
              workspaceRoot={workspaceRoot}
            />)}
          {isCurrent && !turn.assistant && !failures.length && (
            <CurrentTurnStatus items={turn.activity} liveText={liveText} requestSubmitting={requestSubmitting} monitoring={monitoring} connectionState={connectionState}/>
          )}
          {processBlocks.length > 0 && turn.assistant && <div className="conversation-process-divider" role="separator" aria-label="工作过程结束"/>}
          {turn.assistant && <AgentReply event={turn.assistant.event} content={turn.assistant.content} changes={fileChanges} onFork={!isGenerating ? () => onFork?.(turn.assistant!.event.id) : undefined} onPreviewCandidateFile={onPreviewCandidateFile} onReviewChanges={onReviewChanges} workspaceRoot={workspaceRoot} highlightReferenceSource={highlightedReferenceEventId === turn.assistant.event.id}/>}
          {failures.map(item => <ConversationFailure key={item.event.id} item={item} taskControl={taskControl}/>)}
        </section>;
      })}
      {turns.length === 0 && (liveText || isGenerating) && <><ActivityGroup items={[]} active liveText={liveText} startedAt={requestStartedAt} avatarSlots={avatarSlots} workspaceRoot={workspaceRoot}/><CurrentTurnStatus items={[]} liveText={liveText} requestSubmitting={requestSubmitting} monitoring={monitoring} connectionState={connectionState}/></>}
      {condensationStatus && <article className={`conversation-condensation-progress ${condensationStatus.state}`} aria-label={condensationStatus.state === 'running' ? '正在压缩上下文' : '上下文压缩失败'} role="status">
        {condensationStatus.state === 'running' ? <LoaderCircle className="conversation-condensation-spinner" size={16}/> : <CircleAlert size={16}/>}
        <div><header><b>{condensationStatus.state === 'running' ? '正在压缩上下文' : '上下文压缩未完成'}</b>{condensationStatus.state === 'running' && <time>{formatDuration(condensationElapsed / 1_000)}</time>}</header>
          <p>{condensationStatus.state === 'failed'
            ? condensationStatus.message || 'OpenHands 未能完成上下文压缩，请稍后重试。'
            : condensationElapsed < 2_000
              ? '已提交原生压缩请求，正在等待 OpenHands 接收。'
              : condensationElapsed < 20_000
                ? 'Condenser 正在生成较早上下文的结构化摘要。'
                : '正在等待摘要完成，并校验用户目标、已完成事项与待办。'}</p>
          {condensationStatus.state === 'failed' && onRetryCondensation && <button type="button" onClick={onRetryCondensation}>重新压缩</button>}
        </div>
      </article>}
      </div>
    </section>
    {viewingReference && <ConversationReferencePreview reference={viewingReference} onClose={() => setViewingReference(undefined)} onLocate={locateReferenceSource}/>}
    {selectedReference && <button type="button" className="conversation-add-reference" style={{ left: selectedReference.left, top: selectedReference.top }} onPointerDown={event => event.preventDefault()} onClick={() => {
      onAddReference?.(selectedReference.reference);
      window.getSelection()?.removeAllRanges();
      setSelectedReference(undefined);
    }}><Quote size={14}/>添加到会话</button>}
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
}
