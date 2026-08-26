import { ChevronDown, ChevronRight, CircleAlert, GitFork, LoaderCircle, Wrench } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
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

function turnsFor(items: Item[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | undefined;
  for (const item of items) {
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

function ActivityGroup({ items, active }: { items: Item[]; active: boolean }) {
  if (!items.length) return null;
  return <details className="conversation-activity-group" open={active}>
    <summary><ChevronRight size={14}/><span>{active ? '正在处理' : '工作过程'}</span><small>{items.length} 项</small>{active && <LoaderCircle className="conversation-activity-spin" size={13}/>}</summary>
    <div className="conversation-activity-list">
      {items.map(item => {
        const isResult = item.event.event_type === 'TOOL_RESULT';
        const Icon = item.kind === 'error' ? CircleAlert : Wrench;
        return <article className={`conversation-activity-row ${item.kind}`} key={item.event.id}>
          <Icon size={14}/><div><b>{item.title}</b><small>{item.kind === 'error' ? '失败' : isResult ? '已完成' : item.kind === 'thought' ? '分析中' : '调用工具'}</small>
            {item.content && <ReactMarkdown>{item.content}</ReactMarkdown>}
            {item.event.payload.details && <pre>{JSON.stringify(item.event.payload.details, null, 2)}</pre>}
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

export function ConversationSurface({ events, liveText, isGenerating, rewritePending = false, onRewrite, onFork }: {
  events: OpenHandsConversationEvent[];
  liveText: string;
  isGenerating: boolean;
  rewritePending?: boolean;
  onRewrite?: (eventId: string, content: string) => void;
  onFork?: (eventId: string) => void;
}) {
  const tail = useRef<HTMLDivElement>(null);
  const initialPositioned = useRef(false);
  const wasGenerating = useRef(isGenerating);
  const [editingEventId, setEditingEventId] = useState<string>();
  const [editingContent, setEditingContent] = useState('');
  const turns = useMemo(() => turnsFor(events.map(itemFor).filter((item): item is Item => Boolean(item))), [events]);
  const scrollToLatest = (behavior: ScrollBehavior = 'smooth') => {
    tail.current?.scrollIntoView({ block: 'end', behavior });
  };
  useEffect(() => {
    if (!initialPositioned.current && (turns.length || liveText || isGenerating)) {
      initialPositioned.current = true;
      scrollToLatest('auto');
    } else if (!wasGenerating.current && isGenerating) {
      scrollToLatest('smooth');
    }
    wasGenerating.current = isGenerating;
  }, [isGenerating, liveText, turns.length]);
  const lastUserEventId = useMemo(() => [...turns].reverse().find(turn => turn.user)?.user?.event.id, [turns]);
  if (!turns.length && !liveText && !isGenerating) return <div className="conversation-surface-empty"><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  return <section className="conversation-surface" aria-live="polite">
    {turns.map((turn, index) => {
      const isCurrent = index === turns.length - 1 && isGenerating;
      const condensations = turn.activity.filter(item => item.kind === 'condensation');
      const activity = turn.activity.filter(item => item.kind !== 'condensation');
      return <section className="conversation-turn" key={turn.id}>
        {turn.user && (editingEventId === turn.user.event.id ? <form className="conversation-message user conversation-message-edit" onSubmit={event => { event.preventDefault(); if (editingContent.trim()) onRewrite?.(turn.user!.event.id, editingContent.trim()); }}><textarea aria-label="编辑已发送消息" value={editingContent} disabled={rewritePending} onChange={event => setEditingContent(event.target.value)}/><footer><button type="button" onClick={() => setEditingEventId(undefined)}>取消</button><button type="submit" disabled={!editingContent.trim() || rewritePending}>重新思考</button></footer></form> : <article className="conversation-message user"><ReactMarkdown>{turn.user.content}</ReactMarkdown>{lastUserEventId === turn.user.event.id && <button type="button" className="conversation-message-rewrite" onClick={() => { setEditingEventId(turn.user!.event.id); setEditingContent(turn.user!.content); }}>编辑并重新思考</button>}</article>)}
        {condensations.map(item => <div className="conversation-condensation" key={item.event.id}><span>↻</span>{item.title}</div>)}
        {turn.assistant && <AgentReply content={turn.assistant.content} onFork={!isGenerating ? () => onFork?.(turn.assistant!.event.id) : undefined}/>}
        {activity.length > 0 && <ActivityGroup items={activity} active={isCurrent && !turn.assistant}/>}
        {isCurrent && !turn.assistant && <AgentReply content={liveText} streaming/>}
      </section>;
    })}
    {turns.length === 0 && (liveText || isGenerating) && <AgentReply content={liveText} streaming/>}
    <div ref={tail}/>
    <button
      type="button"
      className={`conversation-jump-latest${isGenerating ? ' generating' : ''}`}
      aria-label={isGenerating ? '跳转到正在生成的最新回复' : '跳转到最新回复'}
      title={isGenerating ? '查看正在生成的最新回复' : '查看最新回复'}
      onClick={() => scrollToLatest()}
    >
      {isGenerating ? <span className="conversation-jump-dots" aria-hidden="true"><i/><i/><i/></span> : <ChevronDown size={19}/>}
    </button>
  </section>;
}
