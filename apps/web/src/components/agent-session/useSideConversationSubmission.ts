import { useCallback, useMemo, useState } from 'react';
import type { OpenHandsConversationEvent } from '../../types';

type Submission = { scope: string; submissionId: string; eventId?: string; event: OpenHandsConversationEvent };

/** API acknowledgement claims the existing local bubble by its formal event ID. */
export function useSideConversationSubmission(scope: string, formal: OpenHandsConversationEvent[]) {
  const [submission, setSubmission] = useState<Submission>();
  const begin = useCallback((targetScope: string, submissionId: string, event: OpenHandsConversationEvent) => {
    setSubmission({ scope: targetScope, submissionId, event: { ...event, payload: {
      ...event.payload, _flowweave_render_key: submissionId,
    } } });
  }, []);
  const acknowledge = useCallback((targetScope: string, submissionId: string, eventId?: string | null) => {
    if (!eventId) return;
    setSubmission(current => current?.scope === targetScope && current.submissionId === submissionId
      ? { ...current, eventId, event: { ...current.event, id: eventId } } : current);
  }, []);
  const discard = useCallback((targetScope: string, submissionId: string) => {
    setSubmission(current => current?.scope === targetScope && current.submissionId === submissionId ? undefined : current);
  }, []);
  const events = useMemo(() => {
    if (!submission || submission.scope !== scope) return formal;
    let claimed = false;
    const durable = formal.map(event => {
      if (event.id !== submission.eventId && event.payload._flowweave_submission_id !== submission.submissionId) return event;
      claimed = true;
      return { ...event, payload: { ...event.payload, _flowweave_render_key: submission.submissionId } };
    });
    return claimed ? durable : [...durable, submission.event];
  }, [formal, scope, submission]);
  return { events, begin, acknowledge, discard };
}
