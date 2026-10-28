import type { OpenHandsConversationEvent } from '../types';

const EMPTY_RESPONSE_CORRECTIVE_NUDGE = 'Your last response did not include a function call or a message. Please use a tool to proceed with the task.';

const MESSAGE_CONTEXT_MARKER = /(?:^|\n)---FLOWWEAVE_MESSAGE_CONTEXT_V([2-5])---\n/;
const INTERNAL_PROTOCOL_MARKER = /^\[FLOWWEAVE_[A-Z0-9_]+\](?:\r?\n|$)/;

export function isFlowWeaveInternalProtocolMessage(value: string): boolean {
  // Node startup requests can append gate/result contracts after their real
  // prompt. Only standalone protocol messages start with the marker.
  return INTERNAL_PROTOCOL_MARKER.test(value.trimStart());
}

export function projectFlowWeaveMessageContext(event: OpenHandsConversationEvent): OpenHandsConversationEvent {
  const content = event.payload.content;
  if (event.event_type !== 'MESSAGE' || typeof content !== 'string') return event;
  const marker = MESSAGE_CONTEXT_MARKER.exec(content);
  if (!marker) return event;
  const source = String(event.payload.source ?? '').toLowerCase();
  const displayContent = event.payload.display_content;
  if (
    (source !== 'user' && source !== 'human')
    || (typeof displayContent === 'string' && !isFlowWeaveMessageContext(displayContent))
  ) return event;
  try {
    const parsed: unknown = JSON.parse(content.slice((marker.index ?? 0) + marker[0].length));
    if (!parsed || typeof parsed !== 'object') return event;
    const context = parsed as Record<string, unknown>;
    const current = marker[1] === '5' || marker[1] === '4'
      ? context.current_user_request
      : context.current_message;
    const request = current && typeof current === 'object' ? current as Record<string, unknown> : undefined;
    if (typeof request?.content !== 'string') return event;
    const references = marker[1] === '5' || marker[1] === '4'
      ? context.reference_materials
      : context.references;
    const payload = {
      ...event.payload,
      display_content: request.content.trim(),
      ...(Array.isArray(references) ? { conversation_references: references } : {}),
      ...(Array.isArray(context.workspace_references) ? { workspace_references: context.workspace_references } : {}),
      ...(Array.isArray(context.collaboration_annotations) ? { collaboration_annotations: context.collaboration_annotations } : {}),
    };
    return { ...event, payload };
  } catch {
    return event;
  }
}

export function isFlowWeaveMessageContext(value: string): boolean {
  return value.startsWith('FlowWeave message context (JSON):') || MESSAGE_CONTEXT_MARKER.test(value);
}

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
    && event.payload.final_reply !== false
    && typeof event.payload.content === 'string'
    && event.payload.content.trim().length > 0;
}

export function projectHistoricalAgentReplies(events: OpenHandsConversationEvent[]): OpenHandsConversationEvent[] {
  // Match the API's terminal_assistant_message_ids rule across the browser's
  // merged windows. A stream marks every assistant message non-final; a later
  // bounded REST window can omit the preceding reply entirely. The next formal
  // user event still proves its identity through the native parent chain.
  const byId = new Map(events.map(event => [event.id, event]));
  const replyIds = new Set<string>();
  for (const event of events) {
    if (event.event_type !== 'MESSAGE'
      || !['user', 'human'].includes(String(event.payload.source ?? '').toLowerCase())) continue;
    const visited = new Set<string>();
    let parentId = event.payload.parent_id;
    while (parentId && parentId !== '__root__' && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = byId.get(parentId);
      if (!parent) break;
      if (parent.event_type === 'MESSAGE') {
        const source = String(parent.payload.source ?? '').toLowerCase();
        if (['agent', 'assistant'].includes(source)) {
          replyIds.add(parent.id);
          break;
        }
        if (['user', 'human'].includes(source)) break;
      }
      parentId = parent.payload.parent_id;
    }
  }
  return events.map(event => replyIds.has(event.id) && event.payload.final_reply === false
    ? { ...event, payload: { ...event.payload, final_reply: true } }
    : event);
}

export function isOpenHandsConversationTerminalError(event: OpenHandsConversationEvent): boolean {
  return event.event_type === 'ERROR'
    && String(event.payload.source_type ?? '') === 'ConversationErrorEvent';
}

export function isOpenHandsConversationInterrupt(event: OpenHandsConversationEvent): boolean {
  return event.event_type === 'STATE'
    && String(event.payload.source_type ?? event.payload.event_name ?? '') === 'InterruptEvent';
}

export function isOpenHandsEmptyResponseRecovery(event: OpenHandsConversationEvent): boolean {
  return event.event_type === 'MESSAGE'
    && String(event.payload.source ?? '').trim().toLowerCase() === 'environment'
    && String(event.payload.content ?? '').trim() === EMPTY_RESPONSE_CORRECTIVE_NUDGE;
}
