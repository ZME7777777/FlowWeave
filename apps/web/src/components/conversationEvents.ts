import type { OpenHandsConversationEvent } from '../types';

const EMPTY_RESPONSE_CORRECTIVE_NUDGE = 'Your last response did not include a function call or a message. Please use a tool to proceed with the task.';

export function parseOpenHandsEventTime(raw: unknown): number | undefined {
  if (typeof raw !== 'string' || !raw) return undefined;
  // OpenHands 1.47 serializes UTC event timestamps without a timezone suffix.
  const normalized = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(raw) ? raw : `${raw}Z`;
  const value = Date.parse(normalized);
  return Number.isFinite(value) ? value : undefined;
}

export function orderOpenHandsConversationEvents(events: OpenHandsConversationEvent[]): OpenHandsConversationEvent[] {
  // REST and live frames can arrive in a different order. Event identity is
  // authoritative: preserve the stable API order between unrelated events,
  // but always place a parent before its descendants.
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

export function isOpenHandsAgentReply(event: OpenHandsConversationEvent): boolean {
  if (event.event_type !== 'MESSAGE') return false;
  const source = String(event.payload.source ?? '').trim().toLowerCase();
  return ['agent', 'assistant'].includes(source)
    && typeof event.payload.content === 'string'
    && event.payload.content.trim().length > 0;
}

export function isOpenHandsEmptyResponseRecovery(event: OpenHandsConversationEvent): boolean {
  return event.event_type === 'MESSAGE'
    && String(event.payload.source ?? '').trim().toLowerCase() === 'environment'
    && String(event.payload.content ?? '').trim() === EMPTY_RESPONSE_CORRECTIVE_NUDGE;
}
