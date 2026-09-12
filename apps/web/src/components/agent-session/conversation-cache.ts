import type { AgentConversationContext, AgentConversationInputReadiness, OpenHandsConversationEvent, OpenHandsConversationEventBatch } from '../../types';

/**
 * Browser-only cache policy for the Agent workbench.  A snapshot is a bounded
 * presentation projection, never an event-log replica or a command input.
 * Complete branches remain in React Query memory and are always replaced by a
 * formal OpenHands hydration before a cached screen becomes authoritative.
 */
export const INACTIVE_CONVERSATION_CACHE_LIMIT = 5;
export const INACTIVE_CONVERSATION_CACHE_TTL_MS = 5 * 60 * 1000;

const CACHE_IDENTITY_KEY = 'flowweave:agent-session-cache-identity.v1';
const SNAPSHOT_KEY_PREFIX = 'flowweave:agent-session-shell.v1:';
const SNAPSHOT_VERSION = 1;
const MAX_SNAPSHOT_EVENTS = 16;
const MAX_SNAPSHOT_TEXT_LENGTH = 8_000;

export interface ConversationShellSnapshot {
  version: number;
  savedAt: number;
  events: OpenHandsConversationEventBatch;
  context?: AgentConversationContext;
  readiness?: AgentConversationInputReadiness;
}

export interface LogicalConversationCacheEntry {
  bindingId: string;
  lastAccessedAt: number;
}

export interface LogicalConversationCacheReconciliation {
  retained: LogicalConversationCacheEntry[];
  evictedBindingIds: string[];
  nextExpiresAt?: number;
}

function safeStorage(): Storage | undefined {
  try { return window.sessionStorage; } catch { return undefined; }
}

function identity(): string | undefined {
  return safeStorage()?.getItem(CACHE_IDENTITY_KEY) ?? undefined;
}

function snapshotKey(hostId: string, workspaceId: string, bindingId: string): string | undefined {
  const userIdentity = identity();
  if (!userIdentity) return undefined;
  return `${SNAPSHOT_KEY_PREFIX}${encodeURIComponent(userIdentity)}:${encodeURIComponent(hostId)}:${encodeURIComponent(workspaceId)}:${encodeURIComponent(bindingId)}`;
}

/** Set only after authentication; changing identity removes all old tab snapshots. */
export function setAgentSessionCacheIdentity(userId: string): void {
  const storage = safeStorage();
  if (!storage) return;
  if (storage.getItem(CACHE_IDENTITY_KEY) !== userId) {
    clearAgentSessionCacheStorage();
    storage.setItem(CACHE_IDENTITY_KEY, userId);
  }
}

/** Remove every session shell snapshot on logout or identity replacement. */
export function clearAgentSessionCacheStorage(): void {
  const storage = safeStorage();
  if (!storage) return;
  for (const key of Object.keys(storage)) {
    if (key === CACHE_IDENTITY_KEY || key.startsWith(SNAPSHOT_KEY_PREFIX)) storage.removeItem(key);
  }
}

function snapshotText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  return value.length > MAX_SNAPSHOT_TEXT_LENGTH
    ? `${value.slice(0, MAX_SNAPSHOT_TEXT_LENGTH)}\n\n[首屏快照已截断，正在同步完整历史]`
    : value;
}

function snapshotEvent(event: OpenHandsConversationEvent): OpenHandsConversationEvent | undefined {
  // Tool observations can contain terminal stdout/stderr, diffs, image data
  // and file bodies.  They are intentionally excluded from durable shells.
  if (!['MESSAGE', 'ERROR', 'COMPLETED'].includes(event.event_type)) return undefined;
  const payload = event.payload;
  return {
    id: event.id,
    event_type: event.event_type,
    payload: {
      source: typeof payload.source === 'string' ? payload.source : undefined,
      source_type: typeof payload.source_type === 'string' ? payload.source_type : undefined,
      parent_id: typeof payload.parent_id === 'string' ? payload.parent_id : undefined,
      event_name: typeof payload.event_name === 'string' ? payload.event_name : undefined,
      timestamp: typeof payload.timestamp === 'string' ? payload.timestamp : undefined,
      content: snapshotText(payload.content),
      display_content: snapshotText(payload.display_content),
      thought: snapshotText(payload.thought),
      summary: snapshotText(payload.summary),
    },
  };
}

function shellEvents(events: OpenHandsConversationEvent[]): OpenHandsConversationEvent[] {
  return events
    .flatMap(event => {
      const snapshot = snapshotEvent(event);
      return snapshot ? [snapshot] : [];
    })
    .slice(-MAX_SNAPSHOT_EVENTS);
}

export function writeConversationShellSnapshot(
  hostId: string,
  workspaceId: string,
  bindingId: string,
  hydration: { events: OpenHandsConversationEventBatch; context: AgentConversationContext; readiness: AgentConversationInputReadiness },
): void {
  const storage = safeStorage();
  const key = snapshotKey(hostId, workspaceId, bindingId);
  if (!storage || !key) return;
  const snapshot: ConversationShellSnapshot = {
    version: SNAPSHOT_VERSION,
    savedAt: Date.now(),
    events: {
      events: shellEvents(hydration.events.events),
      result: hydration.events.result,
    },
    context: hydration.context,
    readiness: hydration.readiness,
  };
  try { storage.setItem(key, JSON.stringify(snapshot)); } catch {
    // Quota/storage failures only lose the optional fast shell. The formal
    // hydration path remains available and is deliberately unaffected.
  }
}

export function readConversationShellSnapshot(
  hostId: string, workspaceId: string, bindingId: string,
): ConversationShellSnapshot | undefined {
  const storage = safeStorage();
  const key = snapshotKey(hostId, workspaceId, bindingId);
  if (!storage || !key) return undefined;
  try {
    const value: unknown = JSON.parse(storage.getItem(key) ?? 'null');
    if (!value || typeof value !== 'object') return undefined;
    const snapshot = value as Partial<ConversationShellSnapshot>;
    if (snapshot.version !== SNAPSHOT_VERSION || typeof snapshot.savedAt !== 'number'
      || !snapshot.events || !Array.isArray(snapshot.events.events)) return undefined;
    return snapshot as ConversationShellSnapshot;
  } catch {
    return undefined;
  }
}

/**
 * Return the inactive full-history entries that fit the fixed TTL/LRU budget.
 * The active conversation is intentionally excluded: it stays live for as
 * long as this workbench is mounted, while its shell can survive a refresh.
 */
export function reconcileLogicalConversationCache(
  entries: Iterable<LogicalConversationCacheEntry>,
  activeBindingId: string | undefined,
  now: number,
): LogicalConversationCacheReconciliation {
  const deduplicated = new Map<string, LogicalConversationCacheEntry>();
  for (const entry of entries) {
    if (!entry.bindingId || entry.bindingId === activeBindingId) continue;
    const prior = deduplicated.get(entry.bindingId);
    if (!prior || prior.lastAccessedAt < entry.lastAccessedAt) deduplicated.set(entry.bindingId, entry);
  }
  const ordered = [...deduplicated.values()].sort((left, right) => right.lastAccessedAt - left.lastAccessedAt);
  const retained: LogicalConversationCacheEntry[] = [];
  const evictedBindingIds: string[] = [];
  let nextExpiresAt: number | undefined;
  for (const entry of ordered) {
    const expiresAt = entry.lastAccessedAt + INACTIVE_CONVERSATION_CACHE_TTL_MS;
    if (expiresAt <= now || retained.length >= INACTIVE_CONVERSATION_CACHE_LIMIT) {
      evictedBindingIds.push(entry.bindingId);
      continue;
    }
    retained.push(entry);
    nextExpiresAt = nextExpiresAt === undefined ? expiresAt : Math.min(nextExpiresAt, expiresAt);
  }
  return { retained, evictedBindingIds, nextExpiresAt };
}
