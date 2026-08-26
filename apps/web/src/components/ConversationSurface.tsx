import { Bot, BrainCircuit, CheckCircle2, CircleAlert, LoaderCircle, UserRound, Wrench } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import type { OpenHandsConversationEvent } from '../types';
import './conversation-surface.css';

type MessageKind = 'user' | 'assistant' | 'thought' | 'tool' | 'error';

interface Item {
  event: OpenHandsConversationEvent;
  kind: MessageKind;
  title: string;
  content: string;
}

function itemFor(event: OpenHandsConversationEvent): Item | undefined {
  const content = typeof event.payload.content === 'string' ? event.payload.content : '';
  const eventName = String(event.payload.event_name || event.event_type);
  if (event.event_type === 'MESSAGE') {
    const source = String(event.payload.source ?? '').toLowerCase();
    return { event, kind: source === 'user' || source === 'human' ? 'user' : 'assistant', title: source === 'user' || source === 'human' ? '你' : 'Agent', content };
  }
  if (event.event_type === 'THOUGHT') return { event, kind: 'thought', title: '正在分析', content };
  if (event.event_type === 'TOOL_CALL') return { event, kind: 'tool', title: eventName, content };
  if (event.event_type === 'TOOL_RESULT') return { event, kind: 'tool', title: eventName, content };
  if (event.event_type === 'ERROR') return { event, kind: 'error', title: '执行遇到问题', content };
  // State frames only carry protocol progress. Empty frames do not belong in the conversation.
  if (!content) return undefined;
  return { event, kind: 'thought', title: eventName, content };
}

function ActivityCard({ item }: { item: Item }) {
  const isResult = item.event.event_type === 'TOOL_RESULT';
  const Icon = item.kind === 'thought' ? BrainCircuit : item.kind === 'error' ? CircleAlert : isResult ? CheckCircle2 : Wrench;
  const details = item.event.payload.details;
  const status = item.kind === 'error' ? '失败' : isResult ? '已完成' : item.kind === 'thought' ? '处理中' : '正在调用';
  return <details className={`conversation-activity ${item.kind}`} open={item.kind === 'error'}>
    <summary><Icon size={15}/><span><b>{item.title}</b><small>{status}</small></span>{item.kind === 'thought' && <LoaderCircle className="conversation-activity-spin" size={14}/>}</summary>
    <div className="conversation-activity-body">
      {item.content && <ReactMarkdown>{item.content}</ReactMarkdown>}
      {details && <pre>{JSON.stringify(details, null, 2)}</pre>}
      {!item.content && !details && <span>Agent 正在处理这一步。</span>}
    </div>
  </details>;
}

export function ConversationSurface({ events, liveText, isGenerating }: {
  events: OpenHandsConversationEvent[];
  liveText: string;
  isGenerating: boolean;
}) {
  const tail = useRef<HTMLDivElement>(null);
  const items = useMemo(() => events.map(itemFor).filter((item): item is Item => Boolean(item)), [events]);
  useEffect(() => { tail.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }); }, [items.length, liveText]);
  if (!items.length && !liveText && !isGenerating) return <div className="conversation-surface-empty"><Bot size={30}/><b>会话已就绪</b><span>发送第一条消息，开始与 Agent 协作。</span></div>;
  return <section className="conversation-surface" aria-live="polite">
    {items.map(item => item.kind === 'user' || item.kind === 'assistant' ? <article className={`conversation-message ${item.kind}`} key={item.event.id}>
      <span className="conversation-avatar">{item.kind === 'user' ? <UserRound size={15}/> : <Bot size={15}/>}</span>
      <div className="conversation-message-body"><b>{item.title}</b>{item.content && <ReactMarkdown>{item.content}</ReactMarkdown>}</div>
    </article> : <ActivityCard key={item.event.id} item={item}/>)}
    {(liveText || isGenerating) && <article className="conversation-message assistant streaming">
      <span className="conversation-avatar"><Bot size={15}/></span>
      <div className="conversation-message-body"><b>Agent <i>正在回复</i></b>{liveText ? <ReactMarkdown>{liveText}</ReactMarkdown> : <span className="conversation-typing"><i/><i/><i/></span>}</div>
    </article>}
    <div ref={tail}/>
  </section>;
}
