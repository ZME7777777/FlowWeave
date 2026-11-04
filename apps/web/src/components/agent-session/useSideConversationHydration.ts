import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { AgentStreamEvent } from '../../api/client';
import type { AgentConversationHydration, OpenHandsConversationEventBatch } from '../../types';
import { mergeConversationBatch, mergeConversationEvents } from './conversation-event-cache';

export interface SideConversationReader {
  hydration(signal: AbortSignal, cursor?: string): Promise<AgentConversationHydration>;
  history?(cursor: string, signal: AbortSignal): Promise<OpenHandsConversationEventBatch>;
  subscribe?(onEvent: (event: AgentStreamEvent) => void, onReconnect: () => void): () => void;
}

type Selection = { scope: string; reader: SideConversationReader };
type Snapshot = { scope: string; data?: AgentConversationHydration; loading: boolean; error?: string };
type ReadKind = 'latest' | 'incremental' | 'history';

/** One selected sidebar surface, one read in flight, and no persisted native event copies. */
export function useSideConversationHydration(selection: Selection | undefined, enabled = true) {
  const selected = useRef<Selection | undefined>(undefined);
  const mounted = useRef(false);
  const controller = useRef<AbortController | undefined>(undefined);
  const snapshots = useRef(new Map<string, AgentConversationHydration>());
  const exhaustedHistory = useRef(new Map<string, Set<string>>());
  const inFlight = useRef<Promise<void> | undefined>(undefined);
  const pending = useRef<ReadKind | undefined>(undefined);
  const lastLatest = useRef(0);
  const needsLatest = useRef(true);
  const runRef = useRef<(kind: ReadKind) => Promise<void>>(async () => undefined);
  const [snapshot, setSnapshot] = useState<Snapshot>({ scope: '', loading: false });

  const run = useCallback((kind: ReadKind): Promise<void> => {
    const target = selected.current;
    const signal = controller.current?.signal;
    if (!target || !mounted.current || !signal || signal.aborted) return Promise.resolve();
    if (needsLatest.current) kind = 'latest';
    if (inFlight.current) {
      // A foreground request always wins over history, including signals
      // received while an incremental read is still outstanding.
      if (pending.current !== 'latest' && (kind !== 'history' || !pending.current)) pending.current = kind;
      return inFlight.current;
    }
    const prior = snapshots.current.get(target.scope);
    const historyCursor = prior?.events.history_cursor;
    if (kind === 'history' && (!historyCursor || !target.reader.history
      || exhaustedHistory.current.get(target.scope)?.has(historyCursor))) return Promise.resolve();
    pending.current = undefined;
    const request = Promise.resolve().then(async () => {
      try {
        let data: AgentConversationHydration;
        if (kind === 'history' && historyCursor && target.reader.history && prior) {
          const older = await target.reader.history(historyCursor, signal);
          const checked = exhaustedHistory.current.get(target.scope) ?? new Set<string>();
          checked.add(historyCursor);
          exhaustedHistory.current.set(target.scope, checked);
          data = { ...prior, events: { ...prior.events,
            events: mergeConversationEvents(older.events, prior.events.events), history_cursor: older.history_cursor } };
        } else {
          const cursor = kind === 'incremental' ? prior?.events.next_cursor ?? undefined : undefined;
          const incoming = await target.reader.hydration(signal, cursor);
          data = { ...incoming, events: mergeConversationBatch(prior?.events, incoming.events, [], incoming.incremental_from) };
          if (kind === 'latest') lastLatest.current = Date.now();
        }
        if (signal.aborted || !mounted.current) return;
        snapshots.current.set(target.scope, data);
        if (selected.current?.scope === target.scope && (kind === 'latest' || !needsLatest.current)) {
          needsLatest.current = false;
          setSnapshot({ scope: target.scope, data, loading: false });
        }
      } catch (reason) {
        if (!signal.aborted && mounted.current && selected.current?.scope === target.scope) {
          setSnapshot(current => ({ scope: target.scope,
            data: current.scope === target.scope ? current.data : undefined,
            loading: false, error: reason instanceof Error ? reason.message : '会话暂时不可用' }));
        }
      } finally {
        inFlight.current = undefined;
        const next = pending.current;
        pending.current = undefined;
        if (next && mounted.current && !signal.aborted) void runRef.current(next);
      }
    });
    inFlight.current = request;
    return request;
  }, []);
  useLayoutEffect(() => { runRef.current = run; }, [run]);
  useEffect(() => {
    mounted.current = true;
    controller.current = new AbortController();
    return () => { mounted.current = false; controller.current?.abort(); pending.current = undefined; };
  }, []);

  const scope = selection?.scope;
  const reader = selection?.reader;
  useLayoutEffect(() => {
    selected.current = enabled && scope && reader ? { scope, reader } : undefined;
    pending.current = undefined;
    needsLatest.current = true;
    if (enabled && scope && reader) setSnapshot({ scope, loading: true });
  }, [enabled, reader, scope]);
  useEffect(() => {
    if (!enabled || !scope || !reader) return;
    const timer = window.setTimeout(() => void run('latest'), 120);
    return () => window.clearTimeout(timer);
  }, [enabled, reader, run, scope]);

  const data = snapshot.scope === scope ? snapshot.data : undefined;
  const loading = Boolean(enabled && scope && (snapshot.scope !== scope || snapshot.loading));
  useEffect(() => {
    if (!enabled || !scope || loading) return;
    const running = ['running', 'starting', 'executing', 'stopping'].includes(data?.readiness.execution_status ?? '');
    const timer = window.setTimeout(() => {
      void run(Date.now() - lastLatest.current >= 30_000 ? 'latest' : 'incremental');
    }, running ? 4_000 : 15_000);
    return () => window.clearTimeout(timer);
  }, [data, enabled, loading, run, scope, snapshot.error]);
  useEffect(() => {
    if (!enabled || !reader?.history || !scope || loading || !data?.events.history_cursor) return;
    const timer = window.setTimeout(() => void run('history'), 1_500);
    return () => window.clearTimeout(timer);
  }, [data, enabled, loading, reader, run, scope]);
  const hydrated = Boolean(data);
  useEffect(() => {
    if (!enabled || !scope || !reader?.subscribe || loading || !hydrated) return;
    return reader.subscribe(event => {
      // The StreamContext text channel never supplies message content.
      if (event.type !== 'delta' && event.type !== 'model_retry') void run('latest');
    }, () => void run('latest'));
  }, [enabled, hydrated, loading, reader, run, scope]);
  useEffect(() => {
    if (!enabled || !scope) return;
    let timer: number | undefined;
    const refresh = () => {
      if (document.visibilityState !== 'visible' || timer !== undefined) return;
      timer = window.setTimeout(() => { timer = undefined; void run('latest'); }, 0);
    };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); if (timer !== undefined) window.clearTimeout(timer); };
  }, [enabled, run, scope]);
  return { data, loading, error: snapshot.scope === scope ? snapshot.error : undefined, refresh: useCallback(() => run('latest'), [run]) };
}
