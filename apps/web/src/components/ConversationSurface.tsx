import { ChevronDown, ChevronRight, CircleAlert, GitFork, LoaderCircle, Pencil, Wrench } from 'lucide-react';
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

function itemFor(event: OpenHandsConversationEvent): Item | undefined {
  const content = typeof event.payload.content === 'string' ? event.payload.content : '';
  const eventName = String(event.payload.event_name || event.event_type);
  if (event.event_type === 'MESSAGE') {
    const source = String(event.payload.source ?? '').toLowerCase();
    return { event, kind: source === 'user' || source === 'human' ? 'user' : 'assistant', title: '', content };
  }
  if (event.event_type === 'THOUGHT') return { event, kind: 'thought', title: '分析', content };
  if (event.event_type === 'CONDENSATION_REQUESTED') return { event, kind: 'condensation', title: '正在自动压缩上下文', content: '' };
  if (event.event_type === 'CONDENSATION_COMPLETED') return { event, kind: 'condensation', title: '已自动压缩上下文', content: '' };
  if (event.event_type === 'TOOL_CALL') return { event, kind: 'tool', title: eventName, content };
  if (event.event_type === 'TOOL_RESULT') return { event, kind: 'tool', title: eventName, content };
  if (event.event_type === 'ERROR') return { event, kind: 'error', title: '执行遇到问题', content };
  // STATE is transport progress rather than conversation content. Other empty
  // protocol frames are similarly excluded from the product transcript.
  return content ? { event, kind: 'thought', title: eventName, content } : undefined;
}

function orderedItems(items: Item[]): Item[] {
  // REST and live frames can arrive in a different order.  Event identity is
  // authoritative: preserve the stable API order between unrelated events,
  // but always render a parent before its descendants.
  const byId = new Map(items.map(item => [item.event.id, item]));
  const children = new Map<string, Item[]>();
  const roots: Item[] = [];
  for (const item of items) {
    const parentId = item.event.payload.parent_id;
    if (parentId && byId.has(parentId)) {
      const bucket = children.get(parentId) ?? [];
      bucket.push(item);
      children.set(parentId, bucket);
    } else roots.push(item);
  }
  const output: Item[] = [];
  const seen = new Set<string>();
  const visit = (item: Item) => {
    if (seen.has(item.event.id)) return;
    seen.add(item.event.id);
    output.push(item);
    for (const child of children.get(item.event.id) ?? []) visit(child);
  };
  for (const item of roots) visit(item);
  for (const item of items) visit(item);
  return output;
}

function turnsFor(items: Item[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | undefined;
  for (const item of orderedItems(items)) {
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
  return turns;
}

function detailText(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, 500) : '';
}

function workspacePath(value: string): string {
  return value.replace(/^\/runtime\/workspace\/project\/?/, '工作区/');
}

function activityPresentation(item: Item): { title: string; status: string; command?: string; path?: string; thought?: string } {
  if (item.kind === 'thought') {
    return { title: '正在分析', status: '分析中', thought: item.content.slice(0, 2_000) || undefined };
  }
  if (item.kind === 'error') return { title: '执行遇到问题', status: '失败' };
  const details = item.event.payload.details ?? {};
  const eventName = String(item.event.payload.event_name ?? '');
  const path = detailText(details.path) || detailText(details.file_path) || detailText(details.filename);
  const command = detailText(details.command);
  const completed = item.event.event_type === 'TOOL_RESULT';
  if (eventName === 'TerminalAction') return { title: '正在运行命令', status: '调用工具', command };
  if (eventName === 'TerminalObservation') return { title: '命令已执行', status: completed ? '已完成' : '处理中' };
  if (eventName === 'FileEditorAction') {
    const operation = command.toLowerCase();
    const title = operation === 'view' ? '正在读取文件'
      : ['create', 'write'].includes(operation) ? '正在创建文件'
        : ['str_replace', 'insert', 'append', 'undo_edit'].includes(operation) ? '正在编辑文件'
          : '正在处理文件';
    return { title, status: '调用工具', path: path ? workspacePath(path) : undefined };
  }
  if (eventName === 'FileEditorObservation') return { title: '文件操作已完成', status: completed ? '已完成' : '处理中', path: path ? workspacePath(path) : undefined };
  if (eventName === 'InvokeSkillAction') return { title: '正在使用已启用技能', status: '调用技能' };
  if (eventName === 'InvokeSkillObservation') return { title: '技能调用已完成', status: completed ? '已完成' : '处理中' };
  if (eventName.includes('Browser')) return { title: completed ? '浏览器操作已完成' : '正在操作浏览器', status: completed ? '已完成' : '调用工具' };
  if (eventName.includes('MCP')) return { title: completed ? '工具调用已完成' : '正在调用已启用工具', status: completed ? '已完成' : '调用工具' };
  if (eventName === 'TaskAction') return { title: '正在处理子任务', status: '处理中' };
  if (eventName === 'TaskObservation') return { title: '子任务已完成', status: completed ? '已完成' : '处理中' };
  return { title: completed ? '工具调用已完成' : '正在使用工具', status: completed ? '已完成' : '调用工具' };
}

function ActivityGroup({ items, active }: { items: Item[]; active: boolean }) {
  if (!items.length) return null;
  return <details className="conversation-activity-group" open={active}>
    <summary><ChevronRight size={14}/><span>{active ? '正在处理' : '工作过程'}</span><small>{items.length} 项</small>{active && <LoaderCircle className="conversation-activity-spin" size={13}/>}</summary>
    <div className="conversation-activity-list">
      {items.map(item => {
        const Icon = item.kind === 'error' ? CircleAlert : Wrench;
        const presentation = activityPresentation(item);
        return <article className={`conversation-activity-row ${item.kind}`} key={item.event.id}>
          <Icon size={14}/><div><b>{presentation.title}</b><small>{presentation.status}</small>
            {presentation.thought && <ReactMarkdown>{presentation.thought}</ReactMarkdown>}
            {presentation.command && <code className="conversation-activity-command">{presentation.command}</code>}
            {presentation.path && <code className="conversation-activity-path">{presentation.path}</code>}
          </div>
        </article>;
      })}
    </div>
  </details>;
}

function AgentReply({ content, streaming = false, onFork }: { content: string; streaming?: boolean; onFork?: () => void }) {
  return <article className={`conversation-message assistant${streaming ? ' streaming' : ''}`}>
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
      ? `已等待 ${elapsedSeconds} 秒。模型服务排队、响应较慢或额度不足时，原因会显示在这里。`
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
  return <article className="conversation-failure" role="status">
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
  const tail = useRef<HTMLDivElement>(null);
  const initialPositioned = useRef(false);
  const followLatest = useRef(true);
  const wasGenerating = useRef(isGenerating);
  const [isAtLatest, setIsAtLatest] = useState(true);
  const [editingEventId, setEditingEventId] = useState<string>();
  const [editingContent, setEditingContent] = useState('');
  const turns = useMemo(() => turnsFor(events.map(itemFor).filter((item): item is Item => Boolean(item))), [events]);
  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    followLatest.current = true;
    setIsAtLatest(true);
    tail.current?.scrollIntoView({ block: 'end', behavior });
  }, []);
  const updateScrollPosition = useCallback(() => {
    const element = surface.current;
    if (!element) return;
    const atLatest = element.scrollHeight - element.scrollTop - element.clientHeight <= 16;
    followLatest.current = atLatest;
    setIsAtLatest(atLatest);
  }, []);
  useEffect(() => {
    if (!initialPositioned.current && (turns.length || liveText || isGenerating)) {
      initialPositioned.current = true;
      scrollToLatest('auto');
    } else if (!wasGenerating.current && isGenerating) {
      scrollToLatest('smooth');
    } else if (followLatest.current) {
      scrollToLatest('auto');
    }
    wasGenerating.current = isGenerating;
  }, [isGenerating, liveText, scrollToLatest, turns.length]);
  const lastUserEventId = useMemo(() => [...turns].reverse().find(turn => turn.user)?.user?.event.id, [turns]);
  if (!turns.length && !liveText && !isGenerating) return <div className="conversation-surface-empty"><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  const showJumpToLatest = !isAtLatest && Boolean(turns.length || liveText || isGenerating);
  return <section ref={surface} className="conversation-surface" aria-live="polite" onScroll={updateScrollPosition}>
    {turns.map((turn, index) => {
      const isCurrent = index === turns.length - 1 && isGenerating;
      const condensations = turn.activity.filter(item => item.kind === 'condensation');
      const failures = turn.activity.filter(item => item.kind === 'error');
      const activity = turn.activity.filter(item => item.kind !== 'condensation' && item.kind !== 'error');
      const waitingForProgress = isCurrent && !turn.assistant && !liveText && !activity.length && !failures.length;
      return <section className="conversation-turn" key={turn.id}>
        {turn.user && (editingEventId === turn.user.event.id ? <form className="conversation-message user conversation-message-edit" onSubmit={event => { event.preventDefault(); if (editingContent.trim()) onRewrite?.(turn.user!.event.id, editingContent.trim()); }}><textarea aria-label="编辑已发送消息" value={editingContent} disabled={rewritePending} onChange={event => setEditingContent(event.target.value)}/><footer><button type="button" onClick={() => setEditingEventId(undefined)}>取消</button><button type="submit" disabled={!editingContent.trim() || rewritePending}>重新思考</button></footer></form> : <article className="conversation-message user"><ReactMarkdown>{turn.user.content}</ReactMarkdown>{lastUserEventId === turn.user.event.id && <button type="button" className="conversation-message-rewrite" aria-label="编辑并重新思考" title="编辑并重新思考" onClick={() => { setEditingEventId(turn.user!.event.id); setEditingContent(turn.user!.content); }}><Pencil size={13}/></button>}</article>)}
        {condensations.map(item => <div className="conversation-condensation" key={item.event.id}><span>↻</span>{item.title}</div>)}
        {turn.assistant && <AgentReply content={turn.assistant.content} onFork={!isGenerating ? () => onFork?.(turn.assistant!.event.id) : undefined}/>}
        {failures.map(item => <ConversationFailure key={item.event.id} item={item}/>)}
        {activity.length > 0 && <ActivityGroup items={activity} active={isCurrent && !turn.assistant}/>}
        {isCurrent && !turn.assistant && (waitingForProgress
          ? <ResponseWait startedAt={requestStartedAt} submitting={requestSubmitting}/>
          : <AgentReply content={liveText} streaming/>)}
      </section>;
    })}
    {turns.length === 0 && (liveText || isGenerating) && (liveText
      ? <AgentReply content={liveText} streaming/>
      : <ResponseWait startedAt={requestStartedAt} submitting={requestSubmitting}/>)}
    <div ref={tail}/>
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
  </section>;
}
