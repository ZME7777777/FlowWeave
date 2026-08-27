import { Check, ChevronDown, ChevronRight, CircleAlert, Copy, FileText, GitFork, LoaderCircle, Pencil, Sparkles, SquareTerminal, Wrench } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import type { OpenHandsConversationEvent } from '../types';
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

interface ActivityEntry {
  id: string;
  item: Item;
  action?: Item;
  results: Item[];
}

function itemsFor(event: OpenHandsConversationEvent): Item[] {
  const content = typeof event.payload.content === 'string' ? event.payload.content : '';
  const thought = typeof event.payload.thought === 'string' ? event.payload.thought : '';
  const eventName = String(event.payload.event_name || event.event_type);
  if (event.event_type === 'MESSAGE') {
    const source = String(event.payload.source ?? '').toLowerCase();
    return [{ event, kind: source === 'user' || source === 'human' ? 'user' : 'assistant', title: '', content }];
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

function turnsFor(events: OpenHandsConversationEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | undefined;
  for (const event of orderedEvents(events)) {
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

function workspacePath(value: string): string {
  return value.replace(/^\/runtime\/workspace\/project\/?/, '工作区/');
}

function compactCommand(value: string): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > 110 ? `${compact.slice(0, 107)}...` : compact;
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
  output?: string;
  exitCode?: string;
  actionDetails?: Record<string, unknown>;
  resultDetails?: Record<string, unknown>;
}

function activityPresentation(entry: ActivityEntry): ActivityPresentation {
  const item = entry.action ?? entry.item;
  if (item.kind === 'condensation') {
    return { title: item.title, status: item.event.event_type === 'CONDENSATION_COMPLETED' ? '已完成' : '处理中' };
  }
  if (item.kind === 'thought') {
    return { title: '正在分析', status: '分析中', thought: item.content.slice(0, 2_000) || undefined };
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
  const output = entry.results.map(value => detailContent(value.content)).filter(Boolean).join('\n\n').slice(0, 12_000) || undefined;
  const exitCode = typeof resultDetails.exit_code === 'number' ? String(resultDetails.exit_code) : undefined;
  if (eventName === 'TerminalAction' || eventName === 'TerminalObservation' || resultName === 'TerminalObservation') {
    const verb = failed ? '运行失败' : completed ? '已运行' : '正在运行';
    return {
      title: command ? `${verb} ${compactCommand(command)}` : actionTitle(completed ? '命令已执行' : '正在运行命令'),
      status: failed ? '终端 · 失败' : completed ? '终端 · 已完成' : '终端',
      command, thought, output, exitCode, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'FileEditorAction' || eventName === 'FileEditorObservation' || resultName === 'FileEditorObservation') {
    const operation = command.toLowerCase();
    const verb = operation === 'view' ? (failed ? '读取失败' : completed ? '已读取' : '正在读取')
      : ['create', 'write'].includes(operation) ? (failed ? '创建失败' : completed ? '已创建' : '正在创建')
        : operation === 'undo_edit' ? (failed ? '撤销失败' : completed ? '已撤销编辑' : '正在撤销编辑')
          : ['str_replace', 'insert', 'append'].includes(operation) ? (failed ? '编辑失败' : completed ? '已编辑' : '正在编辑')
            : failed ? '文件操作失败' : completed ? '已完成文件操作' : '正在处理文件';
    const displayPath = path ? workspacePath(path) : '';
    return {
      title: displayPath ? `${verb} ${displayPath}` : actionTitle(verb),
      status: failed ? '文件编辑器 · 失败' : completed ? '文件编辑器 · 已完成' : '文件编辑器',
      path: displayPath || undefined, operation: command || undefined, thought, output, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'TaskTrackerAction') {
    return {
      title: completed ? (command === 'plan' ? '任务列表已更新' : '任务列表已读取') : actionTitle(command === 'plan' ? '正在更新任务列表' : '正在查看任务列表'),
      status: completed ? '任务跟踪 · 已完成' : '任务跟踪', thought, output, actionDetails: details, resultDetails,
    };
  }
  if (eventName === 'TaskTrackerObservation') {
    return {
      title: command === 'plan' ? '任务列表已更新' : '任务列表已读取',
      status: completed ? '任务跟踪 · 已完成' : '任务跟踪',
    };
  }
  if (eventName === 'InvokeSkillAction') return { title: completed ? `${actionTitle('技能调用')} · 已完成` : actionTitle('正在使用已启用技能'), status: completed ? '技能 · 已完成' : '技能', thought, output, actionDetails: details, resultDetails };
  if (eventName === 'InvokeSkillObservation') return { title: '技能调用已完成', status: completed ? '已完成' : '处理中' };
  if (eventName.includes('Browser')) return { title: completed ? `${actionTitle('浏览器操作')} · 已完成` : actionTitle('正在操作浏览器'), status: completed ? '浏览器 · 已完成' : '浏览器', thought, output, actionDetails: details, resultDetails };
  if (eventName.includes('MCP')) return { title: completed ? `${actionTitle('MCP 工具调用')} · 已完成` : actionTitle('正在调用 MCP 工具'), status: completed ? 'MCP · 已完成' : 'MCP', thought, output, actionDetails: details, resultDetails };
  if (eventName === 'TaskAction') return { title: completed ? `${actionTitle('子任务')} · 已完成` : actionTitle('正在处理子任务'), status: completed ? '子任务 · 已完成' : '子任务', thought, output, actionDetails: details, resultDetails };
  if (eventName === 'TaskObservation') return { title: '子任务已完成', status: completed ? '已完成' : '处理中' };
  const toolName = eventName.replace(/(?:Action|Observation)$/, '') || '工具';
  return {
    title: completed ? `${actionTitle(toolName)} · 已完成` : actionTitle(`正在使用 ${toolName}`),
    status: completed ? '工具 · 已完成' : '工具',
    thought, output, actionDetails: details, resultDetails,
  };
}

function displayDetails(details: Record<string, unknown>): string {
  const visible = Object.fromEntries(Object.entries(details).filter(([key]) => !['content', 'old_content', 'new_content'].includes(key)));
  return Object.keys(visible).length ? JSON.stringify(visible, null, 2).slice(0, 12_000) : '';
}

function ToolDetailPanel({ presentation, eventName }: { presentation: ActivityPresentation; eventName: string }) {
  const details = presentation.actionDetails ?? {};
  const resultDetails = presentation.resultDetails ?? {};
  const isTerminal = eventName.includes('Terminal');
  const isFile = eventName.includes('FileEditor');
  const structured = displayDetails(details);
  const structuredResult = displayDetails(resultDetails);
  const fileText = detailContent(details.file_text);
  const oldText = detailContent(details.old_str);
  const newText = detailContent(details.new_str);
  const hasDetail = Boolean(presentation.command || presentation.output || structured || structuredResult || fileText || oldText || newText);
  if (!hasDetail) return null;
  return <div className="conversation-tool-detail-panel">
      <b>{isTerminal ? 'Shell' : isFile ? '文件操作' : '工具调用'}</b>
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
      {!isTerminal && !isFile && structured && <><small>原始操作</small><pre><code>{structured}</code></pre></>}
      {presentation.output && <><small>执行结果</small><pre><code>{presentation.output}</code></pre></>}
      {!isTerminal && !isFile && structuredResult && <><small>结果信息</small><pre><code>{structuredResult}</code></pre></>}
      {presentation.exitCode && <small>退出码 {presentation.exitCode}</small>}
    </div>;
}

function eventTime(item?: Item): number | undefined {
  const raw = item?.event.payload.timestamp;
  if (typeof raw !== 'string' || !raw) return undefined;
  // OpenHands 1.42.0 creates Event.timestamp with datetime.now().isoformat().
  // The Runtime container runs in UTC, but that value has no timezone suffix.
  // Browsers otherwise interpret it as local time and inflate an active turn by
  // the local UTC offset. Preserve explicitly zoned timestamps as-is.
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`;
  const value = Date.parse(normalized);
  return Number.isFinite(value) ? value : undefined;
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

function ActivityGroup({ items, active, liveText, startedAt, finishedAt, waiting, requestSubmitting }: {
  items: Item[];
  active: boolean;
  liveText?: string;
  startedAt?: number;
  finishedAt?: number;
  waiting?: boolean;
  requestSubmitting?: boolean;
}) {
  const elapsedSeconds = useElapsedSeconds(startedAt, finishedAt, active);
  const entries = groupedActivities(items);
  const itemCount = entries.length + (liveText ? 1 : 0);
  const label = active
    ? `正在处理${elapsedSeconds === undefined ? '' : ` · 已耗时 ${formatDuration(elapsedSeconds)}`}`
    : elapsedSeconds === undefined ? '工作过程' : `耗时 ${formatDuration(elapsedSeconds)}`;
  const summary = <><ChevronRight size={14}/><span>{label}</span>{itemCount > 0 && <small>{itemCount} 项</small>}{active && <LoaderCircle className="conversation-activity-spin" size={13}/>}</>;
  const hasDetails = itemCount > 0 || waiting;
  if (!hasDetails) return <div className="conversation-activity-group summary-only"><div className="conversation-activity-summary">{summary}</div></div>;
  return <details className="conversation-activity-group" open={active} onToggle={event => { if (active && !event.currentTarget.open) event.currentTarget.open = true; }}>
    <summary>{summary}</summary>
    <div className="conversation-activity-list">
      {entries.map(entry => {
        const item = entry.action ?? entry.item;
        const Icon = item.kind === 'error' ? CircleAlert : item.kind === 'thought' || item.kind === 'condensation' ? Sparkles : Wrench;
        const eventName = String(item.event.payload.event_name ?? '');
        const ToolIcon = eventName.includes('Terminal') ? SquareTerminal : eventName.includes('FileEditor') ? FileText : Icon;
        const presentation = activityPresentation(entry);
        const toolDetail = item.kind === 'tool' ? <ToolDetailPanel presentation={presentation} eventName={eventName}/> : null;
        if (item.kind === 'tool' && toolDetail) return <div className="conversation-tool-entry" key={entry.id}>
          {presentation.thought && <article className="conversation-activity-row thought"><Sparkles size={14}/><div><ReactMarkdown>{presentation.thought}</ReactMarkdown></div></article>}
          <details className="conversation-activity-row tool conversation-tool-detail">
            <summary><ToolIcon size={14}/><div><b>{presentation.title}</b><small>{presentation.status}</small></div><ChevronRight className="conversation-tool-chevron" size={13}/></summary>
            {toolDetail}
          </details>
        </div>;
        return <article className={`conversation-activity-row ${item.kind}`} key={entry.id}>
          <ToolIcon size={14}/><div><b>{presentation.title}</b><small>{presentation.status}</small>
            {presentation.thought && <ReactMarkdown>{presentation.thought}</ReactMarkdown>}
          </div>
        </article>;
      })}
      {liveText && <article className="conversation-activity-row live-text"><Sparkles size={14}/><div><b>正在生成回复</b><small>模型输出</small><ReactMarkdown>{liveText}</ReactMarkdown></div></article>}
      {waiting && <ResponseWait startedAt={startedAt} submitting={Boolean(requestSubmitting)}/>}
    </div>
  </details>;
}

function AgentReply({ eventId, content, onFork }: { eventId: string; content: string; onFork?: () => void }) {
  return <article className="conversation-message assistant" data-turn-terminal="true" data-event-id={eventId}>
    {content ? <ReactMarkdown>{content}</ReactMarkdown> : <span className="conversation-typing"><i/><i/><i/></span>}
    {onFork && <button type="button" className="conversation-message-fork" onClick={onFork}><GitFork size={12}/>从此处分叉会话</button>}
  </article>;
}

function ResponseWait({ startedAt, submitting }: { startedAt?: number; submitting: boolean }) {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  useEffect(() => {
    if (!startedAt) { setElapsedSeconds(0); return; }
    const update = () => setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  const delayed = elapsedSeconds >= 8;
  const title = submitting
    ? '正在提交消息…'
    : delayed ? '仍在等待模型响应' : '消息已发送，正在等待模型响应';
  const detail = submitting
    ? '正在将请求交给 Agent。'
    : delayed
      ? `已等待 ${formatDuration(elapsedSeconds)}。模型服务排队、响应较慢或额度不足时，原因会显示在这里。`
      : '收到首个文本或工具进度后，会在这里实时显示。';
  return <article className={`conversation-response-wait${delayed ? ' delayed' : ''}`} role="status">
    <LoaderCircle size={16}/><div><b>{title}</b><p>{detail}</p></div>
  </article>;
}

function ConversationFailure({ item }: { item: Item }) {
  const code = typeof item.event.payload.error_code === 'string' ? item.event.payload.error_code : '';
  const content = code === 'LLMRateLimitError'
    ? '模型服务拒绝了这次请求：当前配置的账户可用额度已用尽。请选择有可用额度的模型配置后，编辑并重新思考此消息。'
    : item.content || 'OpenHands 未能完成这一轮，请检查模型配置后重试。';
  return <article className="conversation-failure" data-turn-terminal="true" data-event-id={item.event.id} role="status">
    <CircleAlert size={15}/><div><b>本轮没有生成回复</b><p>{content}</p>{code && <small>{code}</small>}</div>
  </article>;
}

export function ConversationSurface({ events, liveText, isGenerating, requestStartedAt, requestSubmitting = false, rewritePending = false, onRewrite, onFork }: {
  events: OpenHandsConversationEvent[];
  liveText: string;
  isGenerating: boolean;
  requestStartedAt?: number;
  requestSubmitting?: boolean;
  rewritePending?: boolean;
  onRewrite?: (eventId: string, content: string) => void;
  onFork?: (eventId: string) => void;
}) {
  const surface = useRef<HTMLElement>(null);
  const initialPositioned = useRef(false);
  const followLatest = useRef(true);
  const wasGenerating = useRef(isGenerating);
  const copyResetTimer = useRef<number | undefined>(undefined);
  const [isAtLatest, setIsAtLatest] = useState(true);
  const [editingEventId, setEditingEventId] = useState<string>();
  const [editingContent, setEditingContent] = useState('');
  const [copiedEventId, setCopiedEventId] = useState<string>();
  const turns = useMemo(() => turnsFor(events), [events]);
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
  const scrollToTerminalStart = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const terminals = surface.current?.querySelectorAll<HTMLElement>('[data-turn-terminal="true"]');
    const terminal = terminals?.[terminals.length - 1];
    if (!terminal) return scrollToLatest(behavior);
    terminal.scrollIntoView({ block: 'start', behavior });
    window.requestAnimationFrame(updateScrollPosition);
  }, [scrollToLatest, updateScrollPosition]);
  const currentHasTerminal = isGenerating && Boolean(turns.at(-1)?.assistant || turns.at(-1)?.activity.some(item => item.kind === 'error'));
  useEffect(() => {
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
  useEffect(() => () => {
    if (copyResetTimer.current) window.clearTimeout(copyResetTimer.current);
  }, []);
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
  const lastUserEventId = useMemo(() => [...turns].reverse().find(turn => turn.user)?.user?.event.id, [turns]);
  if (!turns.length && !liveText && !isGenerating) return <div className="conversation-surface-empty"><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  const showJumpToLatest = !isAtLatest && Boolean(turns.length || liveText || isGenerating);
  return <div className="conversation-surface-shell">
    <section ref={surface} className="conversation-surface" aria-live="polite" onScroll={updateScrollPosition}>
      {turns.map((turn, index) => {
        const isCurrent = index === turns.length - 1 && isGenerating;
        const failures = turn.activity.filter(item => item.kind === 'error');
        const processItems = turn.activity;
        const waitingForProgress = isCurrent && !turn.assistant && !liveText && !processItems.length;
        const startedAt = eventTime(turn.user) ?? (isCurrent ? requestStartedAt : undefined);
        const finishedAt = eventTime(turn.assistant ?? failures.at(-1));
        return <section className="conversation-turn" key={turn.id}>
          {turn.user && (editingEventId === turn.user.event.id ? <form className="conversation-message user conversation-message-edit" onSubmit={event => { event.preventDefault(); if (editingContent.trim()) onRewrite?.(turn.user!.event.id, editingContent.trim()); }}><textarea aria-label="编辑已发送消息" value={editingContent} disabled={rewritePending} onChange={event => setEditingContent(event.target.value)}/><footer><button type="button" onClick={() => setEditingEventId(undefined)}>取消</button><button type="submit" disabled={!editingContent.trim() || rewritePending}>重新思考</button></footer></form> : <article className="conversation-message user"><div className="conversation-message-content"><ReactMarkdown>{turn.user.content}</ReactMarkdown></div><div className="conversation-message-actions"><button type="button" className="conversation-message-copy" aria-label={copiedEventId === turn.user.event.id ? '消息已复制' : '复制消息'} title={copiedEventId === turn.user.event.id ? '已复制' : '复制消息'} onClick={() => copyUserMessage(turn.user!.event.id, turn.user!.content)}>{copiedEventId === turn.user.event.id ? <Check size={13}/> : <Copy size={13}/>}</button>{lastUserEventId === turn.user.event.id && <button type="button" className="conversation-message-rewrite" aria-label="编辑并重新思考" title="编辑并重新思考" onClick={() => { setEditingEventId(turn.user!.event.id); setEditingContent(turn.user!.content); }}><Pencil size={13}/></button>}</div></article>)}
          <ActivityGroup items={processItems} active={isCurrent && !turn.assistant && !failures.length} liveText={isCurrent ? liveText : undefined} startedAt={startedAt} finishedAt={finishedAt} waiting={waitingForProgress} requestSubmitting={requestSubmitting}/>
          {turn.assistant && <AgentReply eventId={turn.assistant.event.id} content={turn.assistant.content} onFork={!isGenerating ? () => onFork?.(turn.assistant!.event.id) : undefined}/>}
          {failures.map(item => <ConversationFailure key={item.event.id} item={item}/>)}
        </section>;
      })}
      {turns.length === 0 && (liveText || isGenerating) && <ActivityGroup items={[]} active liveText={liveText} startedAt={requestStartedAt} waiting={!liveText} requestSubmitting={requestSubmitting}/>}
    </section>
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
