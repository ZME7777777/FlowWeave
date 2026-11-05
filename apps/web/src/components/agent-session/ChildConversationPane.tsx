import { useQuery, useQueryClient } from '@tanstack/react-query';
import { LoaderCircle, Play, Plus, Quote, Send, Settings, Square, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError, childSessionFileUrl, randomId, subscribeToChildSessionStream } from '../../api/client';
import type {
  AgentAttachment,
  AgentChildConversation,
  AgentConversation,
  AgentConversationReference,
  OpenHandsConversationEvent,
} from '../../types';
import { type ConversationScrollSnapshot } from '../ConversationSurface';
import { isOpenHandsAgentReply } from '../conversationEvents';
import { AgentComposerInput, type ComposerSuggestion } from './AgentComposerInput';
import { type ComposerHandle, transferredFiles } from './agentComposerInputUtils';
import { ComposerModelMenu } from './ComposerModelMenu';
import { SideConversationSurface } from './SideConversationSurface';
import { getAgentSessionCacheIdentity } from './conversation-cache';
import { conversationComposerSuggestions, defaultConversationModel } from './conversation-composer';
import {
  emptyChildMessage,
  readChildComposer,
  writeChildComposer,
  type ChildComposerDraft,
  type ChildQueuedMessage,
} from './child-conversation-drafts';
import { useSideConversationHydration } from './useSideConversationHydration';
import { workspaceMarkdownFileHref, workspaceMarkdownLinkPath } from './workspace-file-links';
import { useSideConversationSubmission } from './useSideConversationSubmission';

const emptyEvents: OpenHandsConversationEvent[] = [];

type PendingUpload = { id: string; scope: string; parentId: string; childId: string; file: File; failed: boolean };
type ResultReference = { source_binding_id: string; source_event_id: string; content: string };

export function ChildConversationPane({
  hostId,
  parentId,
  child,
  active,
  onRead,
  onPreviewAttachment,
  onOpenConversationReference,
  onQuoteParent,
  onVisualComplete,
}: {
  hostId: string;
  parentId?: string;
  child?: AgentChildConversation;
  active: boolean;
  onRead: (childId: string) => void;
  onPreviewAttachment: (
    attachment: AgentAttachment,
    bindingId?: string,
    parentId?: string,
    workingDirectory?: string,
  ) => void;
  onOpenConversationReference: (reference: AgentConversationReference) => void;
  onQuoteParent: (parentId: string, reference: ResultReference) => void;
  onVisualComplete: (childId: string, complete: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const childId = child?.id;
  const user = getAgentSessionCacheIdentity();
  const scope = parentId && childId ? `${user ?? 'memory'}:${hostId}:${parentId}:${childId}` : undefined;
  const selectedScope = useRef(scope);
  selectedScope.current = active ? scope : undefined;
  const container = useRef<HTMLDivElement>(null);
  const input = useRef<ComposerHandle>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const drafts = useRef(new Map<string, ChildComposerDraft>());
  const scrolls = useRef(new Map<string, ConversationScrollSnapshot>());
  const inFlight = useRef(new Set<string>());
  const removedUploads = useRef(new Set<string>());
  const forceSend = useRef(new Set<string>());
  const acknowledged = useRef<string | undefined>(undefined);
  const [revision, setRevision] = useState(0);
  const [uploads, setUploads] = useState<PendingUpload[]>([]);
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [controls, setControls] = useState<Record<string, boolean>>({});
  const [completed, setCompleted] = useState<Record<string, boolean>>({});
  const [condensation, setCondensation] = useState<
    Record<string, { taskId?: string; eventIds: string[]; startedAt: number }>
  >({});
  const [escapeArmed, setEscapeArmed] = useState<string>();
  const escapeTimer = useRef<number | undefined>(undefined);
  const [configurationScope, setConfigurationScope] = useState<string>();
  const [credentialIds, setCredentialIds] = useState<string[]>([]);
  const [confirmationReason, setConfirmationReason] = useState('');
  const [referenceLocation, setReferenceLocation] = useState<AgentConversationReference>();

  const readDraft = useCallback((target: string) => {
    let value = drafts.current.get(target);
    if (!value) {
      value = readChildComposer(target);
      drafts.current.set(target, value);
    }
    return value;
  }, []);
  const updateDraft = useCallback(
    (target: string, change: (value: ChildComposerDraft) => ChildComposerDraft) => {
      const next = change(readDraft(target));
      drafts.current.set(target, next);
      if (user) writeChildComposer(next);
      if (mounted.current) setRevision((value) => value + 1);
    },
    [readDraft, user],
  );
  const draft = scope ? readDraft(scope) : undefined;
  const fail = useCallback((target: string, reason: unknown) => {
    if (mounted.current)
      setErrors((current) => ({ ...current, [target]: reason instanceof Error ? reason.message : '子会话操作未完成' }));
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (escapeTimer.current !== undefined) window.clearTimeout(escapeTimer.current);
    };
  }, []);

  const reader = useMemo(
    () =>
      parentId && childId && scope
        ? {
            hydration: (signal: AbortSignal, cursor?: string) =>
              api.agentChildHydration(parentId, childId, signal, cursor),
            history: (cursor: string, signal: AbortSignal) => api.agentChildEvents(parentId, childId, cursor, signal),
            subscribe: (onEvent: Parameters<typeof subscribeToChildSessionStream>[2], onReconnect: () => void) => {
              let connected = false;
              return subscribeToChildSessionStream(
                parentId,
                childId,
                (event) => {
                  if (
                    selectedScope.current === scope &&
                    (event.type === 'stream_closed' ||
                      (event.type === 'event' && event.event && isOpenHandsAgentReply(event.event)))
                  ) {
                    setCompleted((current) => ({ ...current, [scope]: true }));
                    onVisualComplete(childId, true);
                  }
                  onEvent(event);
                },
                (status) => {
                  if ((status === 'live' && connected) || status === 'disabled') onReconnect();
                  if (status === 'live') connected = true;
                },
              );
            },
          }
        : undefined,
    [childId, onVisualComplete, parentId, scope],
  );
  const hydration = useSideConversationHydration(
    scope && reader ? { scope, reader } : undefined,
    active && child?.lifecycle === 'ACTIVE',
  );
  const formal = hydration.data?.events.events ?? emptyEvents;
  const submission = useSideConversationSubmission(scope ?? '', formal);
  const detailsKey = useMemo(() => ['agent-child', scope, 'details'], [scope]);
  const details = useQuery({
    queryKey: detailsKey,
    queryFn: ({ signal }) => api.agentChildConversation(parentId!, childId!, signal),
    enabled: Boolean(active && scope && hydration.data),
    refetchOnWindowFocus: false,
    retry: false,
  });
  const providers = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const model = defaultConversationModel(
    providers.data ?? [],
    details.data?.model_provider_id,
    details.data?.model_name,
    details.data?.reasoning_effort,
  );
  const provider = providers.data?.find((item) => item.id === model?.providerId);
  const models = provider?.models.filter((item) => item.enabled) ?? [];
  const efforts = models.find((item) => item.model_name === model?.modelName)?.supported_reasoning_efforts ?? [];
  const readiness = hydration.data?.readiness;
  const nativeBusy = ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(
    readiness?.execution_status ?? '',
  );
  const paused = readiness?.execution_status === 'paused';
  const currentTask = hydration.data?.events.condensation_task;
  const latestCondensation = [...formal]
    .reverse()
    .find((event) => event.event_type === 'CONDENSATION_REQUESTED' || event.event_type === 'CONDENSATION_COMPLETED');
  const condensing = Boolean(
    scope &&
      (condensation[scope] ||
        ['PENDING', 'RUNNING', 'RETRY'].includes(currentTask?.state ?? '') ||
        (latestCondensation?.event_type === 'CONDENSATION_REQUESTED' && currentTask?.state !== 'DEAD')),
  );
  const writable = Boolean(
    active &&
      child?.lifecycle === 'ACTIVE' &&
      details.data?.write_available &&
      hydration.data &&
      !hydration.loading &&
      !hydration.error,
  );
  const mutating = Boolean(scope && controls[scope]);
  const running = nativeBusy && !completed[scope ?? ''] && !condensing;
  const canDispatch =
    writable && Boolean(model) && Boolean(readiness?.ready) && !nativeBusy && !condensing && !mutating;
  const currentUploads = uploads.filter((upload) => upload.scope === scope);

  useEffect(() => {
    if (!active) acknowledged.current = undefined;
    if (!active || !scope || !childId || hydration.loading || !hydration.data || acknowledged.current === scope) return;
    acknowledged.current = scope;
    onRead(childId);
  }, [active, childId, hydration.data, hydration.loading, onRead, scope]);
  useEffect(() => {
    if (!scope || !condensation[scope]) return;
    const pending = condensation[scope];
    const finished = formal.some(
      (event) => event.event_type === 'CONDENSATION_COMPLETED' && !pending.eventIds.includes(event.id),
    );
    if (!finished && !(pending.taskId && currentTask?.task_id === pending.taskId && currentTask.state === 'DEAD'))
      return;
    setCondensation((current) => {
      const next = { ...current };
      delete next[scope];
      return next;
    });
    if (!finished) fail(scope, new Error('上下文压缩未完成，请检查正式记录后重试。'));
  }, [condensation, currentTask, fail, formal, scope]);
  useEffect(() => {
    if (!scope || !draft?.admissionEventId || !hydration.data) return;
    const admitted = formal.some((event) => event.id === draft.admissionEventId);
    const elapsed = Date.now() - (draft.admittedAt ?? Date.now());
    if (admitted) updateDraft(scope, (current) => ({ ...current, admissionEventId: undefined, admittedAt: undefined }));
    else if (elapsed > 60_000 && !errors[scope])
      fail(scope, new Error('消息已受理，正式记录仍待同步。请检查后再发送队列。'));
  }, [draft?.admissionEventId, draft?.admittedAt, errors, fail, formal, hydration.data, scope, updateDraft]);

  const dispatch = useCallback(
    async (id: string, confirmed = false) => {
      if (
        !scope ||
        !parentId ||
        !childId ||
        selectedScope.current !== scope ||
        !canDispatch ||
        inFlight.current.has(scope)
      )
        return;
      const current = readDraft(scope);
      if (current.admissionEventId && !confirmed) return;
      const message = current.queue.find((item) => item.id === id);
      if (!message) return;
      inFlight.current.add(scope);
      forceSend.current.delete(id);
      updateDraft(scope, (value) => ({
        ...value,
        queue: value.queue.map((item) => (item.id === id ? { ...item, state: 'sending', error: undefined } : item)),
      }));
      setErrors((value) => ({ ...value, [scope]: undefined }));
      setCompleted((value) => ({ ...value, [scope]: false }));
      onVisualComplete(childId, false);
      submission.begin(scope, id, {
        id,
        event_type: 'MESSAGE',
        payload: {
          source: 'user',
          content: message.content,
          attachments: message.attachments,
          conversation_references: message.references,
          workspace_references: message.workspaceReferences,
          collaboration_annotations: message.annotations,
          _flowweave_submission_id: id,
          _flowweave_projection_state: 'submitting',
        },
      });
      try {
        const result = await api.sendAgentChildMessage(
          parentId,
          childId,
          id,
          message.content,
          message.attachments,
          message.references,
          message.workspaceReferences,
          message.annotations,
        );
        submission.acknowledge(scope, id, result.cursor);
        updateDraft(scope, (value) => ({
          ...value,
          queue: value.queue.filter((item) => item.id !== id),
          admissionEventId: result.cursor,
          admittedAt: Date.now(),
        }));
        if (selectedScope.current === scope) await hydration.refresh();
      } catch (reason) {
        submission.discard(scope, id);
        updateDraft(scope, (value) => ({
          ...value,
          queue: value.queue.map((item) =>
            item.id === id
              ? { ...item, state: 'failed', error: reason instanceof Error ? reason.message : '消息发送结果待确认' }
              : item,
          ),
        }));
        fail(scope, reason);
        if (selectedScope.current === scope) void hydration.refresh();
      } finally {
        inFlight.current.delete(scope);
        if (mounted.current) setRevision((value) => value + 1);
      }
    },
    [canDispatch, childId, fail, hydration, onVisualComplete, parentId, readDraft, scope, submission, updateDraft],
  );
  useEffect(() => {
    const first = draft?.queue[0];
    if (
      !first ||
      first.state !== 'queued' ||
      (!draft?.autoSend && !forceSend.current.has(first.id)) ||
      !canDispatch ||
      draft.admissionEventId
    )
      return;
    void dispatch(first.id);
  }, [canDispatch, dispatch, draft, revision]);

  const control = useCallback(
    async (operation: 'interrupt' | 'resume' | 'condense') => {
      if (!scope || !parentId || !childId || !writable || mutating || (operation === 'interrupt' && condensing)) return;
      setControls((current) => ({ ...current, [scope]: true }));
      if (operation === 'condense')
        setCondensation((current) => ({
          ...current,
          [scope]: { eventIds: formal.map((event) => event.id), startedAt: Date.now() },
        }));
      try {
        const result = await api.controlAgentChild(parentId, childId, operation);
        if (operation === 'condense')
          setCondensation((current) =>
            current[scope] ? { ...current, [scope]: { ...current[scope], taskId: result.task_id } } : current,
          );
        if (operation === 'resume') {
          setCompleted((current) => ({ ...current, [scope]: false }));
          onVisualComplete(childId, false);
        }
        if (selectedScope.current === scope) await hydration.refresh();
      } catch (reason) {
        if (operation === 'condense' && reason instanceof ApiError && reason.status < 500)
          setCondensation((current) => {
            const next = { ...current };
            delete next[scope];
            return next;
          });
        fail(scope, reason);
      } finally {
        if (mounted.current) setControls((current) => ({ ...current, [scope]: false }));
      }
    },
    [childId, condensing, fail, formal, hydration, mutating, onVisualComplete, parentId, scope, writable],
  );
  const enqueue = (direct = false) => {
    if (!scope || !draft || !writable || currentUploads.length) return;
    const content = input.current?.value() ?? draft.content;
    if (
      !content.trim() &&
      !draft.attachments.length &&
      !draft.references.length &&
      !draft.workspaceReferences.length &&
      !draft.annotations.length
    )
      return;
    const message: ChildQueuedMessage = {
      content,
      attachments: draft.attachments,
      references: draft.references,
      workspaceReferences: draft.workspaceReferences,
      annotations: draft.annotations,
      id: randomId(),
      state: 'queued',
    };
    updateDraft(scope, (current) => ({
      ...current,
      ...emptyChildMessage(),
      queue: direct ? [message, ...current.queue] : [...current.queue, message],
    }));
    input.current?.replace('');
    if (canDispatch && !draft.admissionEventId) void dispatch(message.id, true);
    else if (direct && !condensing) {
      forceSend.current.add(message.id);
      if (nativeBusy) void control('interrupt');
    }
  };
  useEffect(() => {
    if (!active || !scope) return;
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || !running || mutating || condensing) return;
      const target = event.target instanceof Element ? event.target : document.activeElement;
      if (
        !target ||
        (!container.current?.contains(target) &&
          !target.closest(`[data-agent-child-tab="${CSS.escape(childId ?? '')}"]`))
      )
        return;
      event.preventDefault();
      if (escapeArmed === scope) {
        if (escapeTimer.current !== undefined) window.clearTimeout(escapeTimer.current);
        setEscapeArmed(undefined);
        void control('interrupt');
      } else {
        setEscapeArmed(scope);
        escapeTimer.current = window.setTimeout(() => setEscapeArmed(undefined), 1_500);
      }
    };
    window.addEventListener('keydown', onEscape);
    return () => window.removeEventListener('keydown', onEscape);
  }, [active, childId, condensing, control, escapeArmed, mutating, running, scope]);
  useEffect(() => {
    setEscapeArmed(undefined);
    if (escapeTimer.current !== undefined) window.clearTimeout(escapeTimer.current);
  }, [scope, active]);

  useEffect(() => {
    setConfirmationReason('');
    setCredentialIds([]);
    setReferenceLocation(undefined);
    setCompleted({});
  }, [scope, active]);
  useEffect(() => {
    const pending = scope ? condensation[scope] : undefined;
    if (!scope || !pending || pending.taskId) return;
    const timer = window.setTimeout(
      () => {
        setCondensation((current) => {
          const next = { ...current };
          delete next[scope];
          return next;
        });
        fail(scope, new Error('压缩受理结果待确认，请检查正式记录后重试。'));
        if (selectedScope.current === scope) void hydration.refresh();
      },
      Math.max(0, pending.startedAt + 60_000 - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [condensation, fail, hydration, scope]);

  const upload = async (job: PendingUpload) => {
    setUploads((current) => current.map((item) => (item.id === job.id ? { ...item, failed: false } : item)));
    try {
      const attachment = await api.uploadAgentChildAttachment(job.parentId, job.childId, job.file);
      if (removedUploads.current.delete(job.id))
        await api.deleteAgentChildAttachment(job.parentId, job.childId, attachment.path);
      else
        updateDraft(job.scope, (current) => ({
          ...current,
          attachments: [
            ...current.attachments,
            { ...attachment, url: childSessionFileUrl(job.parentId, job.childId, attachment.path) },
          ],
        }));
      if (mounted.current) setUploads((current) => current.filter((item) => item.id !== job.id));
    } catch (reason) {
      if (mounted.current)
        setUploads((current) => current.map((item) => (item.id === job.id ? { ...item, failed: true } : item)));
      fail(job.scope, reason);
    }
  };
  const startUpload = (file: File) => {
    if (!scope || !parentId || !childId || !writable) return;
    if (!file.size || file.size > 25 * 1024 * 1024) {
      fail(scope, new Error('附件不能为空，且应小于 25 MiB。'));
      return;
    }
    const job = { id: randomId(), scope, parentId, childId, file, failed: false };
    setUploads((current) => [...current, job]);
    void upload(job);
  };
  const removeAttachment = async (attachment: AgentAttachment) => {
    if (!scope || !parentId || !childId) return;
    const target = scope;
    try {
      if (!draft?.queue.some((message) => message.attachments.some((item) => item.path === attachment.path)))
        await api.deleteAgentChildAttachment(parentId, childId, attachment.path);
      updateDraft(target, (current) => ({
        ...current,
        attachments: current.attachments.filter((item) => item.path !== attachment.path),
      }));
    } catch (reason) {
      fail(target, reason);
    }
  };
  const changeModel = async (providerId: string, modelName: string, effort: string | null) => {
    if (!scope || !parentId || !childId || !writable || nativeBusy || condensing || mutating) return;
    const target = scope;
    setControls((current) => ({ ...current, [target]: true }));
    try {
      const result = await api.switchAgentChildModel(parentId, childId, providerId, modelName, effort);
      queryClient.setQueryData<AgentConversation>(detailsKey, (current) =>
        current ? { ...current, ...result } : current,
      );
    } catch (reason) {
      fail(target, reason);
    } finally {
      if (mounted.current) setControls((current) => ({ ...current, [target]: false }));
    }
  };
  const quoteParent = async (eventId: string) => {
    if (!scope || !parentId || !childId) return;
    try {
      onQuoteParent(parentId, await api.agentChildResultReference(parentId, childId, eventId));
    } catch (reason) {
      fail(scope, reason);
    }
  };
  const suggestions = useMemo<ComposerSuggestion[]>(
    () => [
      {
        id: 'native:condense',
        kind: 'NATIVE',
        token: '/condense',
        label: '压缩上下文',
        detail: '压缩当前子会话上下文',
        available: writable && Boolean(readiness?.ready) && !nativeBusy && !condensing && !mutating,
        nativeAction: 'CONDENSE',
      },
      ...conversationComposerSuggestions(details.data?.capabilities, capabilities.data ?? []),
    ],
    [capabilities.data, condensing, details.data?.capabilities, mutating, nativeBusy, readiness?.ready, writable],
  );
  const pendingConfirmation = useQuery({
    queryKey: ['agent-child', scope, 'confirmation'],
    queryFn: () => api.agentChildPendingConfirmation(parentId!, childId!),
    enabled: Boolean(active && scope && readiness?.execution_status === 'waiting_for_confirmation'),
    refetchInterval: active && readiness?.execution_status === 'waiting_for_confirmation' ? 4_000 : false,
    retry: false,
  });
  const decideConfirmation = async (accept: boolean) => {
    if (
      !scope ||
      !parentId ||
      !childId ||
      !confirmationReason.trim() ||
      !pendingConfirmation.data?.pending_actions_digest
    )
      return;
    setControls((current) => ({ ...current, [scope]: true }));
    try {
      await api.decideAgentChildConfirmation(
        parentId,
        childId,
        pendingConfirmation.data.pending_actions_digest,
        accept,
        confirmationReason.trim(),
      );
      if (selectedScope.current === scope) setConfirmationReason('');
      await queryClient.invalidateQueries({ queryKey: ['agent-child', scope, 'confirmation'] });
      if (selectedScope.current === scope) await hydration.refresh();
    } catch (reason) {
      fail(scope, reason);
    } finally {
      if (mounted.current) setControls((current) => ({ ...current, [scope]: false }));
    }
  };
  const credentialQuery = useQuery({
    queryKey: ['agent-child', scope, 'credentials'],
    queryFn: () => api.agentChildCredentials(parentId!, childId!),
    enabled: Boolean(active && scope && configurationScope === scope),
    retry: false,
  });
  useEffect(() => {
    if (configurationScope === scope && credentialQuery.data)
      setCredentialIds(
        credentialQuery.data.credentials.filter((item) => item.sync_state !== 'NEW').map((item) => item.id),
      );
  }, [configurationScope, credentialQuery.data, scope]);
  const synchronizeCredentials = async () => {
    if (!scope || !parentId || !childId || !writable || mutating) return;
    setControls((current) => ({ ...current, [scope]: true }));
    try {
      const result = await api.syncAgentChildCredentials(parentId, childId, credentialIds);
      queryClient.setQueryData(['agent-child', scope, 'credentials'], result);
    } catch (reason) {
      fail(scope, reason);
    } finally {
      if (mounted.current) setControls((current) => ({ ...current, [scope]: false }));
    }
  };
  const openReference = (reference: AgentConversationReference) => {
    if (!reference.source_binding_id || reference.source_binding_id === childId) setReferenceLocation(reference);
    else if (reference.source_binding_id === parentId) onOpenConversationReference(reference);
  };
  const preview = (attachment: AgentAttachment) =>
    onPreviewAttachment(attachment, childId, parentId, details.data?.working_directory ?? undefined);
  const workspacePath = (href: string) => {
    try {
      const url = new URL(href, window.location.origin);
      const current = new URL(childSessionFileUrl(parentId!, childId!, '/'));
      if (url.origin === current.origin && url.pathname === current.pathname)
        return url.searchParams.get('path') ?? undefined;
    } catch {
      /* Relative Markdown paths are resolved against the frozen directory below. */
    }
    return workspaceMarkdownLinkPath(
      href.replace(/^sandbox:(?=\/)/i, ''),
      details.data?.working_directory ?? undefined,
    );
  };
  if (!child || !scope || !draft) return null;
  return (
    <div
      ref={container}
      className={`agent-child-conversation-panel${active ? ' active' : ''}`}
      data-child-binding-id={childId}
    >
      <SideConversationSurface
        label={`子会话 ${child.display_title || '未命名子会话'}`}
        loading={hydration.loading}
        error={errors[scope] ?? hydration.error ?? (details.error instanceof Error ? details.error.message : undefined)}
        notice={child.lifecycle !== 'ACTIVE' ? <p role="status">子会话当前不可编辑。</p> : undefined}
        conversation={
          hydration.data
            ? {
                historyPrepend: hydration.historyPrepend ? { ...hydration.historyPrepend, scope: childId! } : undefined,
                onHistoryAnchorCaptured: hydration.onHistoryAnchorCaptured,
                onHistoryAnchorRestored: hydration.onHistoryAnchorRestored,
                events: submission.events,
                conversationScope: childId,
                isGenerating: running || inFlight.current.has(scope),
                isPaused: paused,
                requestSubmitting: inFlight.current.has(scope),
                condensationPending: condensing,
                taskControl: hydration.data.events.task_control,
                monitoring: hydration.data.events.monitoring,
                onOpenAttachment: preview,
                attachmentImageUrl: (attachment) =>
                  attachment.image_data_url ?? childSessionFileUrl(parentId!, childId!, attachment.path),
                onOpenConversationReference: openReference,
                onQuoteReply: (eventId) => void quoteParent(eventId),
                initialScrollSnapshot: scrolls.current.get(childId!),
                onScrollSnapshotChange: (target, snapshot) => scrolls.current.set(target, snapshot),
                referenceLocation: referenceLocation?.source_binding_id === childId ? referenceLocation : undefined,
                onCreateAnnotation: writable
                  ? (anchor) =>
                      updateDraft(scope, (current) => ({
                        ...current,
                        annotations: [
                          ...current.annotations,
                          {
                            id: randomId(),
                            anchor_kind: 'CONVERSATION_TEXT',
                            anchor: { ...anchor, source_binding_id: childId },
                            comment: '',
                          },
                        ],
                      }))
                  : undefined,
                workspaceRoot: details.data?.working_directory,
                onOpenWorkspaceReference: (reference) =>
                  preview({ filename: reference.display_name, path: reference.path, mime_type: '', byte_size: 0 }),
                onOpenWorkspaceFile: (href) => {
                  const path = workspacePath(href);
                  if (path) {
                    preview({ filename: path.split('/').at(-1) || '文件', path, mime_type: '', byte_size: 0 });
                    return true;
                  }
                  if (workspaceMarkdownFileHref(href)) {
                    fail(scope, new Error('链接目标不在当前子会话工作目录中。'));
                    return true;
                  }
                  return false;
                },
                workspaceImageUrl: (href) => {
                  const path = workspacePath(href);
                  return path ? childSessionFileUrl(parentId!, childId!, path) : undefined;
                },
                onOpenImage: (url, alt) => {
                  const path = workspacePath(url);
                  if (path)
                    preview({
                      filename: alt || path.split('/').at(-1) || '图片',
                      path,
                      mime_type: 'image/png',
                      byte_size: 0,
                    });
                  else if (/^https?:/i.test(url))
                    preview({
                      filename: alt || '图片',
                      path: url,
                      mime_type: 'image/png',
                      byte_size: 0,
                      image_data_url: url,
                    });
                },
              }
            : undefined
        }
        composer={
          <div className="agent-sidebar-chat-composer">
            {pendingConfirmation.data?.pending && (
              <section className="agent-confirmation" aria-label="子会话工具执行确认">
                <b>工具正在等待确认</b>
                {pendingConfirmation.data.actions?.map((action) => (
                  <p key={action.digest}>{action.summary || action.tool_name}</p>
                ))}
                <textarea
                  aria-label="子会话工具确认理由"
                  value={confirmationReason}
                  onChange={(event) => setConfirmationReason(event.target.value)}
                  maxLength={2000}
                />
                <button
                  type="button"
                  disabled={mutating || !confirmationReason.trim()}
                  onClick={() => void decideConfirmation(false)}
                >
                  拒绝整批
                </button>
                <button
                  type="button"
                  disabled={mutating || !confirmationReason.trim()}
                  onClick={() => void decideConfirmation(true)}
                >
                  批准整批
                </button>
              </section>
            )}
            {configurationScope === scope && (
              <section className="agent-child-configuration" aria-label="子会话配置">
                <header>
                  <b>子会话配置</b>
                  <button type="button" aria-label="关闭子会话配置" onClick={() => setConfigurationScope(undefined)}>
                    <X size={14} />
                  </button>
                </header>
                <small>已冻结的能力</small>
                <p>{details.data?.capabilities?.map((item) => item.capability_key).join('、') || '暂无额外能力'}</p>
                <small>认证</small>
                {credentialQuery.isLoading ? (
                  <p>正在读取认证</p>
                ) : credentialQuery.isError ? (
                  <p role="alert">认证暂时不可用</p>
                ) : (
                  credentialQuery.data?.credentials.map((item) => (
                    <label key={item.id}>
                      <input
                        type="checkbox"
                        checked={credentialIds.includes(item.id)}
                        onChange={(event) =>
                          setCredentialIds((current) =>
                            event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id),
                          )
                        }
                      />
                      {item.name}
                      <small>
                        {item.sync_state === 'CURRENT' ? '已同步' : item.sync_state === 'NEW' ? '未注入' : '待同步'}
                      </small>
                    </label>
                  ))
                )}
                <button
                  type="button"
                  disabled={!writable || nativeBusy || mutating || !credentialQuery.data}
                  onClick={() => void synchronizeCredentials()}
                >
                  同步认证
                </button>
              </section>
            )}
            {(draft.queue.length > 0 || !draft.autoSend) && (
              <section className="agent-queued-messages agent-child-message-queue" aria-label="子会话消息队列">
                <header>
                  <b title="自动发送在当前子会话标签可见时执行">消息队列</b>
                  <button
                    type="button"
                    onClick={() => updateDraft(scope, (current) => ({ ...current, autoSend: !current.autoSend }))}
                  >
                    {draft.autoSend ? '关闭自动发送' : '启用自动发送'}
                  </button>
                </header>
                {draft.queue.map((message) => (
                  <article key={message.id}>
                    <span>
                      {message.content || '附件与引用'}
                      {message.error && <small>{message.error}</small>}
                    </span>
                    <button
                      type="button"
                      disabled={!canDispatch || message.state === 'sending'}
                      aria-label={message.state === 'failed' ? '使用原提交身份重试子会话消息' : '发送这条子会话消息'}
                      onClick={() => void dispatch(message.id, true)}
                    >
                      {message.state === 'sending' ? '正在发送' : message.state === 'failed' ? '重试' : '发送'}
                    </button>
                    <button
                      type="button"
                      aria-label="移除排队的子会话消息"
                      disabled={message.state === 'sending'}
                      onClick={() =>
                        updateDraft(scope, (current) => ({
                          ...current,
                          queue: current.queue.filter((item) => item.id !== message.id),
                        }))
                      }
                    >
                      <X size={12} />
                    </button>
                  </article>
                ))}
              </section>
            )}
            <div className="agent-composer agent-sidebar-composer">
              <div className="agent-composer-attachments">
                {(draft.attachments.length > 0 || currentUploads.length > 0) && (
                  <div className="agent-attachments" aria-label="子会话附件">
                    {draft.attachments.map((attachment) => (
                      <span key={attachment.path}>
                        <button type="button" className="agent-attachment-open" onClick={() => preview(attachment)}>
                          {attachment.filename}
                        </button>
                        <button
                          type="button"
                          className="agent-attachment-remove"
                          aria-label={`移除子会话附件 ${attachment.filename}`}
                          onClick={() => void removeAttachment(attachment)}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    {currentUploads.map((job) => (
                      <span key={job.id}>
                        <em>{job.file.name}</em>
                        {job.failed ? (
                          <button type="button" onClick={() => void upload(job)}>
                            重试
                          </button>
                        ) : (
                          <LoaderCircle className="conversation-activity-spin" size={12} />
                        )}
                        <button
                          type="button"
                          aria-label={`移除上传 ${job.file.name}`}
                          onClick={() => {
                            removedUploads.current.add(job.id);
                            setUploads((current) => current.filter((item) => item.id !== job.id));
                          }}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                {draft.references.map((reference) => (
                  <div className="agent-attachments" key={`${reference.source_binding_id}:${reference.event_id}`}>
                    <span>
                      <button type="button" onClick={() => openReference(reference)}>
                        <Quote size={12} />
                        会话引用
                      </button>
                      <button
                        type="button"
                        aria-label="移除子会话引用"
                        onClick={() =>
                          updateDraft(scope, (current) => ({
                            ...current,
                            references: current.references.filter((item) => item !== reference),
                          }))
                        }
                      >
                        ×
                      </button>
                    </span>
                  </div>
                ))}
                {draft.workspaceReferences.map((reference) => (
                  <div className="agent-attachments" key={reference.path}>
                    <span>
                      <button
                        type="button"
                        onClick={() =>
                          preview({
                            filename: reference.display_name,
                            path: reference.path,
                            byte_size: 0,
                            mime_type: '',
                          })
                        }
                      >
                        {reference.display_name}
                      </button>
                      <button
                        type="button"
                        aria-label={`移除工作区引用 ${reference.display_name}`}
                        onClick={() =>
                          updateDraft(scope, (current) => ({
                            ...current,
                            workspaceReferences: current.workspaceReferences.filter((item) => item !== reference),
                          }))
                        }
                      >
                        ×
                      </button>
                    </span>
                  </div>
                ))}
                {draft.annotations.map((annotation) => (
                  <div className="agent-child-annotation" key={annotation.id}>
                    <button
                      type="button"
                      title={String(annotation.anchor.quote)}
                      onClick={() =>
                        setReferenceLocation({
                          event_id: String(annotation.anchor.event_id),
                          content: String(annotation.anchor.quote),
                          source_binding_id: childId,
                        })
                      }
                    >
                      <Quote size={12} />
                      {String(annotation.anchor.quote).slice(0, 80)}
                    </button>
                    <button
                      type="button"
                      aria-label="移除子会话注释"
                      onClick={() =>
                        updateDraft(scope, (current) => ({
                          ...current,
                          annotations: current.annotations.filter((item) => item.id !== annotation.id),
                        }))
                      }
                    >
                      <X size={12} />
                    </button>
                    <textarea
                      aria-label="子会话注释"
                      value={annotation.comment}
                      maxLength={2000}
                      placeholder="为选中内容添加说明"
                      onChange={(event) =>
                        updateDraft(scope, (current) => ({
                          ...current,
                          annotations: current.annotations.map((item) =>
                            item.id === annotation.id ? { ...item, comment: event.target.value } : item,
                          ),
                        }))
                      }
                    />
                  </div>
                ))}
              </div>
              <AgentComposerInput
                ref={input}
                ariaLabel="发送子会话消息"
                scope={scope}
                initialDraft={draft.content}
                suggestions={suggestions}
                disabled={!writable}
                placeholder={
                  hydration.loading
                    ? '正在加载子会话'
                    : condensing
                      ? '压缩期间发送的消息会进入队列'
                      : nativeBusy
                        ? '输入消息，Enter 加入子会话队列'
                        : '给子会话发消息…'
                }
                onDraftChange={(target, content) => {
                  if (target) updateDraft(target, (current) => ({ ...current, content }));
                }}
                onDraftPersist={(target) => {
                  if (target && user) writeChildComposer(readDraft(target));
                }}
                onContentPresenceChange={() => undefined}
                onPaste={(event) => {
                  const files = transferredFiles(event.clipboardData);
                  if (files.length) {
                    event.preventDefault();
                    files.forEach(startUpload);
                  }
                }}
                onDropFiles={(files) => files.forEach(startUpload)}
                onDropWorkspaceFiles={(paths) =>
                  updateDraft(scope, (current) => ({
                    ...current,
                    workspaceReferences: [
                      ...current.workspaceReferences,
                      ...paths
                        .filter((path) => !current.workspaceReferences.some((item) => item.path === path))
                        .map((path) => ({ path, kind: 'file' as const, display_name: path.split('/').at(-1) || path })),
                    ],
                  }))
                }
                onDropConversationReference={(reference) => {
                  if (![parentId, childId].includes(reference.bindingId) || !reference.eventId) {
                    fail(scope, new Error('请选择父会话或当前子会话中的正式消息。'));
                    return;
                  }
                  updateDraft(scope, (current) => ({
                    ...current,
                    references: [
                      ...current.references,
                      { source_binding_id: reference.bindingId, event_id: reference.eventId, content: reference.title },
                    ],
                  }));
                }}
                onSubmit={() => enqueue()}
                onDirectSubmit={() => enqueue(true)}
                onNativeAction={(action) => {
                  if (action === 'CONDENSE' && suggestions[0].available) void control('condense');
                }}
              />
              <footer>
                <div className="agent-composer-context">
                  <input
                    ref={attachmentInput}
                    type="file"
                    multiple
                    hidden
                    aria-label="上传子会话附件"
                    onChange={(event) => {
                      Array.from(event.currentTarget.files ?? []).forEach(startUpload);
                      event.currentTarget.value = '';
                    }}
                  />
                  <button
                    type="button"
                    aria-label="添加子会话附件"
                    disabled={!writable}
                    onClick={() => attachmentInput.current?.click()}
                  >
                    <Plus size={16} />
                  </button>
                  <button
                    type="button"
                    aria-label="子会话配置"
                    disabled={!details.data}
                    onClick={() => setConfigurationScope((current) => (current === scope ? undefined : scope))}
                  >
                    <Settings size={15} />
                  </button>
                </div>
                <div className="agent-composer-actions">
                  <ComposerModelMenu
                    providers={(providers.data ?? []).filter((item) => item.connection_state === 'CONNECTED')}
                    providerId={model?.providerId ?? ''}
                    modelName={model?.modelName ?? ''}
                    models={models}
                    efforts={efforts}
                    effort={model?.reasoningEffort ?? ''}
                    disabled={!writable || nativeBusy || condensing || mutating}
                    onProviderChange={(id) => {
                      const selected = providers.data?.find((item) => item.id === id);
                      const next =
                        selected?.models.find((item) => item.enabled && item.is_default) ??
                        selected?.models.find((item) => item.enabled);
                      if (next) void changeModel(id, next.model_name, next.default_reasoning_effort ?? null);
                    }}
                    onModelChange={(name) => {
                      if (model)
                        void changeModel(
                          model.providerId,
                          name,
                          models.find((item) => item.model_name === name)?.default_reasoning_effort ?? null,
                        );
                    }}
                    onEffortChange={(effort) => {
                      if (model) void changeModel(model.providerId, model.modelName, effort || null);
                    }}
                  />
                  <button
                    type="button"
                    className={`agent-send${paused ? ' resume' : ''}`}
                    aria-label={running ? '暂停子会话 Agent' : paused ? '继续子会话 Agent' : '发送子会话消息'}
                    disabled={
                      !writable ||
                      mutating ||
                      (!running &&
                        !paused &&
                        (currentUploads.length > 0 ||
                          (!draft.content.trim() &&
                            !draft.attachments.length &&
                            !draft.references.length &&
                            !draft.workspaceReferences.length &&
                            !draft.annotations.length)))
                    }
                    onClick={() => {
                      if (running) void control('interrupt');
                      else if (paused) void control('resume');
                      else enqueue();
                    }}
                  >
                    {running ? (
                      escapeArmed === scope ? (
                        <small>Esc</small>
                      ) : (
                        <Square size={10} fill="currentColor" />
                      )
                    ) : paused ? (
                      <Play size={12} fill="currentColor" />
                    ) : (
                      <Send size={16} />
                    )}
                  </button>
                </div>
              </footer>
            </div>
            <div className="agent-sidebar-composer-bottom" aria-hidden="true" />
          </div>
        }
      />
    </div>
  );
}
