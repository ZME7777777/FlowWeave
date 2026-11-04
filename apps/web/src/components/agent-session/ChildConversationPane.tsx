import { useEffect, useMemo, useRef } from 'react';
import { api, subscribeToChildSessionStream } from '../../api/client';
import type { AgentAttachment, AgentChildConversation, AgentConversationReference } from '../../types';
import { SideConversationSurface } from './SideConversationSurface';
import { useSideConversationHydration } from './useSideConversationHydration';

export function ChildConversationPane({ hostId, parentId, child, active, onRead, onPreviewAttachment, onOpenConversationReference }: {
  hostId: string;
  parentId?: string;
  child?: AgentChildConversation;
  active: boolean;
  onRead: (childId: string) => void;
  onPreviewAttachment: (attachment: AgentAttachment, bindingId?: string) => void;
  onOpenConversationReference: (reference: AgentConversationReference) => void;
}) {
  const childId = child?.id;
  const scope = parentId && childId ? `${hostId}:${parentId}:${childId}` : undefined;
  const reader = useMemo(() => parentId && childId ? {
    hydration: (signal: AbortSignal, cursor?: string) => api.agentChildHydration(parentId, childId, signal, cursor),
    history: (cursor: string, signal: AbortSignal) => api.agentChildEvents(parentId, childId, cursor, signal),
    subscribe: (onEvent: Parameters<typeof subscribeToChildSessionStream>[2], onReconnect: () => void) => {
      let connected = false;
      return subscribeToChildSessionStream(parentId, childId, onEvent, status => {
        if (status === 'live' && connected) onReconnect();
        if (status === 'live') connected = true;
      });
    },
  } : undefined, [childId, parentId]);
  const hydration = useSideConversationHydration(scope && reader ? { scope, reader } : undefined, active);
  const acknowledged = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!active) acknowledged.current = undefined;
    if (!active || !scope || !childId || hydration.loading || !hydration.data || acknowledged.current === scope) return;
    acknowledged.current = scope;
    onRead(childId);
  }, [active, childId, hydration.data, hydration.loading, onRead, scope]);
  if (!child) return null;
  const readiness = hydration.data?.readiness;
  return <div className={`agent-child-conversation-panel${active ? ' active' : ''}`} data-child-binding-id={childId}>
    <SideConversationSurface label={`子会话 ${child.display_title || '未命名子会话'}`} loading={hydration.loading}
      error={hydration.error}
      conversation={hydration.data ? {
        events: hydration.data.events.events,
        conversationScope: scope,
        isGenerating: ['running', 'starting', 'executing'].includes(readiness?.execution_status ?? ''),
        isPaused: readiness?.execution_status === 'paused',
        taskControl: hydration.data.events.task_control,
        monitoring: hydration.data.events.monitoring,
        onOpenAttachment: attachment => onPreviewAttachment(attachment, childId),
        onOpenConversationReference,
      } : undefined}/>
  </div>;
}
