import type { AgentAttachment, OpenHandsConversationEvent, OpenHandsConversationEventBatch } from '../../types';

export function mergeConversationEvent(
  current: OpenHandsConversationEvent,
  incoming: OpenHandsConversationEvent,
): OpenHandsConversationEvent {
  const currentAttachments = Array.isArray(current.payload.attachments) ? current.payload.attachments : [];
  const incomingAttachments = Array.isArray(incoming.payload.attachments) ? incoming.payload.attachments : [];
  const attachments = [...currentAttachments, ...incomingAttachments].reduce<AgentAttachment[]>((items, attachment) => {
    const index = items.findIndex(item => item.path === attachment.path);
    if (index < 0) return [...items, attachment];
    const next = [...items];
    next[index] = { ...items[index], ...attachment, image_data_url: attachment.image_data_url ?? items[index].image_data_url };
    return next;
  }, []);
  const details = { ...current.payload.details, ...incoming.payload.details };
  const finalReply = current.payload.final_reply === true || incoming.payload.final_reply === true;
  const merged = {
    ...current,
    ...incoming,
    payload: {
      ...current.payload,
      ...incoming.payload,
      ...(finalReply ? { final_reply: true } : {}),
      ...(attachments.length ? { attachments } : {}),
      ...(Object.keys(details).length ? { details } : {}),
    },
  };
  return JSON.stringify(current) === JSON.stringify(merged) ? current : merged;
}

export function mergeConversationEvents(
  durable: OpenHandsConversationEvent[],
  transient: OpenHandsConversationEvent[],
): OpenHandsConversationEvent[] {
  const merged = new Map(durable.map(event => [event.id, event]));
  for (const event of transient) {
    const current = merged.get(event.id);
    merged.set(event.id, current ? mergeConversationEvent(current, event) : event);
  }
  return [...merged.values()];
}

export function latestHistoryCursor(
  current: OpenHandsConversationEventBatch,
  incoming: OpenHandsConversationEventBatch,
  prefetched: OpenHandsConversationEventBatch[] = [],
): string | null | undefined {
  const anchor = incoming.history_cursor;
  // A moving latest window normally points into events already held in RAM.
  // Preserve the older pagination chain in that case; only an unknown formal
  // event identity starts a new chain (including after an exhausted cursor).
  if (!anchor || anchor === current.history_cursor
    || current.events.some(event => event.id === anchor)
    || prefetched.some(page => page.events.some(event => event.id === anchor))) {
    return current.history_cursor ?? anchor;
  }
  return anchor;
}

function sameConversationPresentation(
  current: OpenHandsConversationEventBatch,
  next: OpenHandsConversationEventBatch,
): boolean {
  if (current.events.length !== next.events.length
    || current.events.some((event, index) => event !== next.events[index])) return false;
  return JSON.stringify({
    next_cursor: current.next_cursor,
    history_cursor: current.history_cursor,
    result: current.result,
    task_usage: current.task_usage,
    task_control: current.task_control,
    possibly_stuck: current.monitoring?.possibly_stuck,
  }) === JSON.stringify({
    next_cursor: next.next_cursor,
    history_cursor: next.history_cursor,
    result: next.result,
    task_usage: next.task_usage,
    task_control: next.task_control,
    possibly_stuck: next.monitoring?.possibly_stuck,
  });
}

export function retainConversationPresentation(
  current: OpenHandsConversationEventBatch,
  next: OpenHandsConversationEventBatch,
): OpenHandsConversationEventBatch {
  return sameConversationPresentation(current, next) ? current : next;
}

export function mergeConversationBatch(
  current: OpenHandsConversationEventBatch | undefined,
  incoming: OpenHandsConversationEventBatch,
  prefetchedHistory: OpenHandsConversationEventBatch[] = [],
  incrementalFrom?: string,
): OpenHandsConversationEventBatch {
  if (!current) return incoming;
  if (incrementalFrom) {
    return {
      ...current,
      ...incoming,
      events: mergeConversationEvents(current.events, incoming.events),
      history_cursor: current.history_cursor,
      // An unchanged native leaf has no new completion event in its delta.
      result: incoming.events.length ? incoming.result : incoming.result ?? current.result,
    };
  }
  // A full recovery can follow native navigate/rewrite. Retain only cached
  // ancestors proven by formal parent IDs, never a detached old descendant.
  const known = new Map([...prefetchedHistory.flatMap(page => page.events), ...current.events].map(event => [event.id, event]));
  const incomingIds = new Set(incoming.events.map(event => event.id));
  const ancestors: OpenHandsConversationEvent[] = [];
  let parent = incoming.events[0]?.payload.parent_id;
  const seen = new Set<string>();
  while (typeof parent === 'string' && !incomingIds.has(parent) && !seen.has(parent)) {
    seen.add(parent);
    const event = known.get(parent);
    if (!event) break;
    ancestors.unshift(event);
    parent = event.payload.parent_id;
  }
  const mergedEvents = mergeConversationEvents(ancestors, incoming.events);
  return retainConversationPresentation(current, {
    ...incoming,
    events: mergedEvents,
    history_cursor: ancestors.length ? current.history_cursor : incoming.history_cursor,
  });
}
