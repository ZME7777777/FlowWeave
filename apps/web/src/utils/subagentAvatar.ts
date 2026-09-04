import type { OpenHandsConversationEvent } from '../types';

export const SUBAGENT_AVATAR_SLOTS = ['orbit', 'branch', 'grid', 'spark', 'search', 'terminal'] as const;
export type SubagentAvatarSlot = typeof SUBAGENT_AVATAR_SLOTS[number];

/**
 * Assign visual identities by formal TaskAction request order, not by agent
 * type.  `action_event_id` is the lifecycle key shared by request and result
 * events, so every projection of the same task receives the same avatar.
 */
export function subagentAvatarSlots(events: OpenHandsConversationEvent[]): Map<string, SubagentAvatarSlot> {
  const requested = new Map<string, { id: string; startedAt: string }>();
  for (const event of events) {
    const task = event.payload.runtime_task;
    if (!task || task.phase !== 'REQUESTED') continue;
    const id = task.action_event_id || event.id;
    if (!id || requested.has(id)) continue;
    requested.set(id, { id, startedAt: typeof event.payload.timestamp === 'string' ? event.payload.timestamp : '' });
  }
  return new Map([...requested.values()]
    .sort((left, right) => left.startedAt.localeCompare(right.startedAt) || left.id.localeCompare(right.id))
    .map((task, index) => [task.id, SUBAGENT_AVATAR_SLOTS[index % SUBAGENT_AVATAR_SLOTS.length]]));
}

export function subagentAvatarSlotForEvent(
  event: OpenHandsConversationEvent,
  slots: ReadonlyMap<string, SubagentAvatarSlot>,
): SubagentAvatarSlot | undefined {
  const task = event.payload.runtime_task;
  const taskId = task?.action_event_id;
  return taskId ? slots.get(taskId) : undefined;
}
