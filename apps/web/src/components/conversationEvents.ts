import type { OpenHandsConversationEvent } from '../types';

const EMPTY_RESPONSE_CORRECTIVE_NUDGE = 'Your last response did not include a function call or a message. Please use a tool to proceed with the task.';

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
