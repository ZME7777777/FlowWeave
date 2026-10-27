import { ArrowDown, ArrowUp, LoaderCircle, Search, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import type { OpenHandsConversationEvent } from '../../types';
import { isFlowWeaveInternalProtocolMessage, isOpenHandsAgentReply } from '../conversationEvents';

interface SearchHit {
  eventId: string;
  content: string;
}

function searchableContent(event: OpenHandsConversationEvent): SearchHit | undefined {
  if (event.event_type !== 'MESSAGE' && event.event_type !== 'COMPLETED') return undefined;
  const source = String(event.payload.source ?? '').toLowerCase();
  const content = event.event_type === 'MESSAGE'
    ? (typeof event.payload.display_content === 'string' ? event.payload.display_content : event.payload.content)
    : event.payload.content;
  if (typeof content !== 'string' || !content.trim()) return undefined;
  if (event.event_type === 'MESSAGE' && (source === 'user' || source === 'human')) {
    return isFlowWeaveInternalProtocolMessage(content) ? undefined : { eventId: event.id, content };
  }
  return event.event_type === 'COMPLETED' || isOpenHandsAgentReply(event)
    ? { eventId: event.id, content }
    : undefined;
}

export function InConversationSearch({ events, onLocate }: {
  events: OpenHandsConversationEvent[];
  onLocate: (eventId: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const [searching, setSearching] = useState(false);
  const searchTimer = useRef<number | undefined>(undefined);
  const hits = useMemo(() => {
    const normalizedQuery = submittedQuery.toLocaleLowerCase();
    if (!normalizedQuery) return [];
    const knownIds = new Set<string>();
    return events.flatMap(event => {
      const hit = searchableContent(event);
      if (!hit || knownIds.has(hit.eventId) || !hit.content.toLocaleLowerCase().includes(normalizedQuery)) return [];
      knownIds.add(hit.eventId);
      return [hit];
    });
  }, [events, submittedQuery]);
  useEffect(() => () => {
    if (searchTimer.current !== undefined) window.clearTimeout(searchTimer.current);
  }, []);
  useEffect(() => {
    setActiveIndex(index => hits.length ? Math.min(index, hits.length - 1) : 0);
  }, [hits.length]);
  const submit = (event?: FormEvent<HTMLFormElement>) => {
    event?.preventDefault();
    const nextQuery = query.trim();
    if (!nextQuery) {
      setSubmittedQuery('');
      setActiveIndex(0);
      return;
    }
    setSearching(true);
    if (searchTimer.current !== undefined) window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(() => {
      setSubmittedQuery(nextQuery);
      setActiveIndex(0);
      setSearching(false);
    }, 180);
  };
  const move = (direction: -1 | 1) => {
    if (!hits.length) return;
    const nextIndex = (activeIndex + direction + hits.length) % hits.length;
    setActiveIndex(nextIndex);
    onLocate(hits[nextIndex].eventId);
  };
  const clear = () => {
    if (searchTimer.current !== undefined) window.clearTimeout(searchTimer.current);
    setQuery('');
    setSubmittedQuery('');
    setActiveIndex(0);
    setSearching(false);
  };
  const hasQuery = Boolean(submittedQuery);
  return <form className={`agent-in-conversation-search${hasQuery ? ' has-results' : ''}`} role="search" onSubmit={submit}>
    {searching ? <LoaderCircle className="agent-in-conversation-search-spinner" size={15} aria-label="正在搜索"/> : <Search size={15}/>}<input aria-label="搜索当前会话" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索聊天…" onKeyDown={event => {
      if (event.key === 'Escape' && (query || submittedQuery)) {
        event.preventDefault();
        clear();
      }
    }}/>
    {hasQuery && <output aria-live="polite">{hits.length ? `${activeIndex + 1} 个结果，共 ${hits.length} 个结果` : '0 个结果'}</output>}
    {hasQuery && <span className="agent-in-conversation-search-navigation"><button type="button" aria-label="上一个搜索结果" title="上一个结果" disabled={!hits.length} onClick={() => move(-1)}><ArrowUp size={15}/></button><button type="button" aria-label="下一个搜索结果" title="下一个结果" disabled={!hits.length} onClick={() => move(1)}><ArrowDown size={15}/></button></span>}
    <button type="button" className="agent-in-conversation-search-close" aria-label={hasQuery || query ? '清除搜索' : '关闭搜索'} onClick={clear}><X size={14}/></button>
  </form>;
}
