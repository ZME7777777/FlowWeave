import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Clock3, LoaderCircle, MessageSquarePlus, Play, Plus, Quote, Send, Square } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { api, ApiError, randomId, subscribeToAgentWorkspaceStream } from '../../api/client';
import { SideConversationSurface } from './SideConversationSurface';
import { useSideConversationHydration } from './useSideConversationHydration';
import { useSideConversationSubmission } from './useSideConversationSubmission';
import { FileTypeIcon } from '../FileTypeIcon';
import type { AgentAttachment, AgentConversationReference } from '../../types';
import { AgentComposerInput, type ComposerSuggestion } from './AgentComposerInput';
import { transferredFiles, type ComposerHandle } from './agentComposerInputUtils';
import { ComposerModelMenu } from './ComposerModelMenu';
import { conversationComposerSuggestions, defaultConversationModel } from './conversation-composer';

interface SidebarConversationPaneProps {
  workspaceId: string;
  sourceBindingId: string;
  sidebarBindingId?: string;
  initialReference?: AgentConversationReference;
  draft?: SidebarConversationDraft;
  onDraftChange: (draft: SidebarConversationDraft) => void;
  onBindingCreated: (bindingId: string) => void;
  onOpenConversationReference: (reference: AgentConversationReference) => void;
  onPreviewAttachment: (attachment: AgentAttachment, bindingId?: string) => void;
}

export interface SidebarConversationDraft {
  content: string;
  attachments?: AgentAttachment[];
  draftConversationId?: string;
  reference?: AgentConversationReference;
}

function sidebarDraftStorageKey(workspaceId: string, sourceBindingId: string): string {
  return `flowweave:agent-sidebar-draft:v1:${workspaceId}:${sourceBindingId}`;
}

function readSidebarDraft(workspaceId: string, sourceBindingId: string): SidebarConversationDraft | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(sidebarDraftStorageKey(workspaceId, sourceBindingId)) ?? 'null') as Partial<SidebarConversationDraft> | null;
    if (!value || typeof value.content !== 'string' || value.content.length > 200_000) return undefined;
    const reference = value.reference;
    if (reference !== undefined && (
      typeof reference.event_id !== 'string'
      || typeof reference.content !== 'string'
      || typeof reference.source_binding_id !== 'string'
    )) return undefined;
    const attachments = value.attachments;
    if (attachments !== undefined && (!Array.isArray(attachments) || attachments.some(attachment => (
      !attachment || typeof attachment !== 'object'
      || typeof attachment.filename !== 'string'
      || typeof attachment.mime_type !== 'string'
      || typeof attachment.byte_size !== 'number'
      || typeof attachment.path !== 'string'
    )))) return undefined;
    const draftConversationId = value.draftConversationId;
    if (draftConversationId !== undefined && (typeof draftConversationId !== 'string' || !draftConversationId)) return undefined;
    return {
      content: value.content,
      ...(attachments ? { attachments: attachments as AgentAttachment[] } : {}),
      ...(draftConversationId ? { draftConversationId } : {}),
      ...(reference ? { reference } : {}),
    };
  } catch {
    return undefined;
  }
}

function writeSidebarDraft(workspaceId: string, sourceBindingId: string, draft: SidebarConversationDraft): void {
  try {
    const key = sidebarDraftStorageKey(workspaceId, sourceBindingId);
    if (draft.content || draft.reference || draft.attachments?.length) localStorage.setItem(key, JSON.stringify(draft));
    else localStorage.removeItem(key);
  } catch {
    // Draft recovery must not block normal side-chat interaction.
  }
}

export function clearSidebarDraft(workspaceId: string, sourceBindingId: string): void {
  try {
    localStorage.removeItem(sidebarDraftStorageKey(workspaceId, sourceBindingId));
  } catch {
    // Explicit close must still complete when browser storage is unavailable.
  }
}

interface PendingSidebarAttachment {
  id: string;
  file: File;
  previewUrl?: string;
  progress: number;
  state: 'uploading' | 'failed';
}

export function SidebarConversationPane({ workspaceId, sourceBindingId, sidebarBindingId, initialReference, draft, onDraftChange, onBindingCreated, onOpenConversationReference, onPreviewAttachment }: SidebarConversationPaneProps) {
  const queryClient = useQueryClient();
  const onDraftChangeRef = useRef(onDraftChange);
  onDraftChangeRef.current = onDraftChange;
  const attachmentInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<ComposerHandle>(null);
  const removedPendingAttachmentIds = useRef(new Set<string>());
  const [bindingId, setBindingId] = useState<string | undefined>(sidebarBindingId);
  const bindingIdRef = useRef(bindingId);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    if (!sidebarBindingId || bindingIdRef.current) return;
    bindingIdRef.current = sidebarBindingId;
    setBindingId(sidebarBindingId);
  }, [sidebarBindingId]);
  const recoveredDraft = useState(() => draft ?? readSidebarDraft(workspaceId, sourceBindingId))[0];
  const [draftConversationId] = useState(() => recoveredDraft?.draftConversationId ?? randomId());
  const [reference, setReference] = useState<AgentConversationReference | undefined>(() => recoveredDraft?.reference ?? initialReference);
  const [content, setContent] = useState(() => recoveredDraft?.content ?? '');
  const [attachments, setAttachments] = useState<AgentAttachment[]>(() => recoveredDraft?.attachments ?? []);
  const referenceRef = useRef(reference);
  referenceRef.current = reference;
  const contentRef = useRef(content);
  contentRef.current = content;
  const attachmentsRef = useRef(attachments);
  attachmentsRef.current = attachments;
  const [pendingAttachments, setPendingAttachments] = useState<PendingSidebarAttachment[]>([]);
  const [error, setError] = useState<string>();
  const [sending, setSending] = useState(false);
  // The message being submitted: kept so a cancelled send can return to the composer.
  const pendingSubmission = useRef<{ id: string; bindingId?: string; message: string; attachments: AgentAttachment[]; reference?: AgentConversationReference; cancelled: boolean } | undefined>(undefined);
  const [cancelling, setCancelling] = useState(false);
  const [condensing, setCondensing] = useState(false);
  const [selectedModelKey, setSelectedModelKey] = useState<string>();
  const persistDraft = useCallback((next: SidebarConversationDraft) => {
    const snapshot: SidebarConversationDraft = {
      ...next,
      attachments: next.attachments ?? attachmentsRef.current,
      draftConversationId,
    };
    onDraftChangeRef.current(snapshot);
    writeSidebarDraft(workspaceId, sourceBindingId, snapshot);
  }, [draftConversationId, sourceBindingId, workspaceId]);
  useEffect(() => {
    if (!initialReference) return;
    setReference(initialReference);
    persistDraft({ content: composerInput.current?.value() ?? contentRef.current, reference: initialReference });
  }, [initialReference, persistDraft]);
  const sourceQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-source', workspaceId, sourceBindingId],
    queryFn: () => api.agentConversation(workspaceId, sourceBindingId),
  });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const capabilitiesQuery = useQuery({ queryKey: ['user-configurable-capabilities'], queryFn: api.userConfigurableCapabilities });
  const models = useMemo(() => (providersQuery.data ?? []).flatMap(provider => provider.connection_state === 'CONNECTED'
    ? provider.models.filter(model => model.enabled).map(model => ({ providerId: provider.id, modelName: model.model_name, reasoningEffort: model.default_reasoning_effort ?? null, label: `${provider.name} · ${model.model_name}` }))
    : []), [providersQuery.data]);
  const fallback = defaultConversationModel(providersQuery.data ?? [], sourceQuery.data?.model_provider_id, sourceQuery.data?.model_name, sourceQuery.data?.reasoning_effort);
  const selectedModel = useMemo(() => {
    const key = selectedModelKey;
    return key ? models.find(model => `${model.providerId}:${model.modelName}` === key) ?? fallback : fallback;
  }, [fallback, models, selectedModelKey]);
  const sidebarQuery = useQuery({
    queryKey: ['agent-session', 'sidebar', workspaceId, bindingId],
    queryFn: () => api.agentSidebarConversation(workspaceId, bindingId!),
    enabled: Boolean(bindingId),
    refetchInterval: query => query.state.data?.expired ? false : 15_000,
  });
  const conversationQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-conversation', workspaceId, bindingId],
    queryFn: () => api.agentConversation(workspaceId, bindingId!),
    enabled: Boolean(bindingId),
  });
  const expired = sidebarQuery.data?.expired === true;
  const reader = useMemo(() => bindingId ? {
    hydration: (signal: AbortSignal, cursor?: string) => api.agentConversationHydration(workspaceId, bindingId, signal, cursor),
    history: (cursor: string, signal: AbortSignal) => api.agentConversationEvents(workspaceId, bindingId, undefined, cursor, undefined, signal),
    subscribe: (onEvent: Parameters<typeof subscribeToAgentWorkspaceStream>[2], onReconnect: () => void) => {
      let connected = false;
      return subscribeToAgentWorkspaceStream(workspaceId, bindingId, onEvent, status => {
        if (status === 'live' && connected) onReconnect();
        if (status === 'live') connected = true;
      });
    },
  } : undefined, [bindingId, workspaceId]);
  const hydrationQuery = useSideConversationHydration(reader && bindingId
    ? { scope: `temporary:${workspaceId}:${sourceBindingId}:${bindingId}`, reader } : undefined, !expired);
  const submissionScope = `temporary:${workspaceId}:${sourceBindingId}:${draftConversationId}`;
  const submission = useSideConversationSubmission(submissionScope, hydrationQuery.data?.events.events ?? []);
  const events = submission.events;
  const currentModel = conversationQuery.data
    ? defaultConversationModel(providersQuery.data ?? [], conversationQuery.data.model_provider_id, conversationQuery.data.model_name, conversationQuery.data.reasoning_effort)
    : selectedModel;
  const readiness = hydrationQuery.data?.readiness;
  const running = readiness?.execution_status === 'running' || readiness?.execution_status === 'starting';
  const paused = readiness?.execution_status === 'paused';
  const canCondense = Boolean(bindingId && !expired && !sending && readiness?.ready && (readiness.execution_status === 'idle' || paused));
  const invalidate = async (id: string) => {
    if (id === bindingIdRef.current) await hydrationQuery.refresh();
  };
  const markExpired = () => queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, bindingId], (current: Record<string, unknown> | undefined) => ({ ...current, expired: true }));
  const restoreSubmission = (current: NonNullable<typeof pendingSubmission.current>) => {
    setContent(current.message);
    composerInput.current?.replace(current.message);
    setAttachments(current.attachments);
    setReference(current.reference);
    persistDraft({ content: current.message, attachments: current.attachments, reference: current.reference });
  };
  const send = async () => {
    const message = composerInput.current?.value().trim() ?? content.trim();
    if ((!message && !attachments.length) || sending || pendingAttachments.length > 0 || !selectedModel || expired || Boolean(bindingId && (hydrationQuery.loading || !hydrationQuery.data))) return;
    const submissionId = randomId();
    const currentSubmission = { id: submissionId, bindingId, message, attachments, reference, cancelled: false };
    pendingSubmission.current = currentSubmission;
    setSending(true);
    setError(undefined);
    setContent('');
    composerInput.current?.replace('');
    setAttachments([]);
    setReference(undefined);
    persistDraft({ content: '', attachments: [] });
    submission.begin(submissionScope, submissionId, { id: submissionId, event_type: 'MESSAGE', payload: { source: 'user', content: message, attachments, conversation_references: reference ? [reference] : [], _flowweave_submission_id: submissionId, _flowweave_projection_state: 'submitting' } });
    try {
      if (!bindingId) {
        const result = await api.createAgentSidebarConversation(
          workspaceId, sourceBindingId, draftConversationId, message, attachments, reference ? [reference] : [],
        );
        if (!mounted.current) return;
        submission.acknowledge(submissionScope, submissionId, result.cursor);
        bindingIdRef.current = result.conversation.id;
        setBindingId(result.conversation.id);
        onBindingCreated(result.conversation.id);
        queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, result.conversation.id], {
          binding_id: result.conversation.id, source_binding_id: sourceBindingId, expires_at: result.expires_at, expired: false,
        });
        await invalidate(result.conversation.id);
      } else {
        const result = await api.sendAgentMessage(workspaceId, bindingId, message, attachments, reference ? [reference] : [], [], [], submissionId);
        submission.acknowledge(submissionScope, submissionId, result.cursor);
        await invalidate(bindingId);
      }
    } catch (reason) {
      const cancelled = reason instanceof ApiError && reason.code === 'AGENT_MESSAGE_CANCELLED';
      const ambiguous = reason instanceof ApiError && reason.code === 'AGENT_MESSAGE_DELIVERY_AMBIGUOUS';
      if (!ambiguous) {
        submission.discard(submissionScope, submissionId);
        if (pendingSubmission.current?.id === submissionId) restoreSubmission(currentSubmission);
      }
      if (cancelled) return;
      if (reason instanceof ApiError && reason.code === 'AGENT_SIDEBAR_CONVERSATION_EXPIRED') markExpired();
      setError(reason instanceof Error ? reason.message : '侧边聊天暂时不可用');
    } finally {
      pendingSubmission.current = undefined;
      setCancelling(false);
      setSending(false);
    }
  };
  const cancelSend = async () => {
    const current = pendingSubmission.current;
    if (!current || cancelling) return;
    setCancelling(true);
    try {
      const result = current.bindingId
        ? await api.cancelAgentMessageSubmission(workspaceId, current.bindingId, current.id)
        : await api.cancelAgentConversationBootstrap(workspaceId, draftConversationId);
      current.cancelled = result.cancelled;
      // A send already delivered cannot be retracted: fall back to a formal pause.
      if (!result.cancelled && current.bindingId) await api.interruptAgentConversation(workspaceId, current.bindingId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消发送失败');
    }
    setCancelling(false);
  };
  const condense = async () => {
    if (!bindingId || !canCondense) return;
    setCondensing(true);
    setError(undefined);
    try {
      await api.condenseAgentConversation(workspaceId, bindingId);
      await invalidate(bindingId);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'AGENT_SIDEBAR_CONVERSATION_EXPIRED') markExpired();
      setError(reason instanceof Error ? reason.message : '压缩上下文失败');
    } finally {
      setCondensing(false);
    }
  };
  const control = async () => {
    if (!bindingId || expired || sending || hydrationQuery.loading || !hydrationQuery.data) return;
    setSending(true);
    setError(undefined);
    try {
      if (running) await api.interruptAgentConversation(workspaceId, bindingId);
      else if (paused) await api.resumeAgentConversation(workspaceId, bindingId);
      await invalidate(bindingId);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'AGENT_SIDEBAR_CONVERSATION_EXPIRED') markExpired();
      setError(reason instanceof Error ? reason.message : '会话控制操作失败');
    } finally {
      setSending(false);
    }
  };
  const upload = async (request: PendingSidebarAttachment) => {
    if (expired || sending) return;
    setError(undefined);
    try {
      const ownerId = bindingIdRef.current ?? draftConversationId;
      const added = bindingIdRef.current
        ? await api.uploadAgentAttachment(workspaceId, bindingIdRef.current, request.file, progress => setPendingAttachments(current => current.map(item => item.id === request.id ? { ...item, progress } : item)))
        : await api.uploadAgentWorkspaceAttachment(workspaceId, request.file, undefined, ownerId, progress => setPendingAttachments(current => current.map(item => item.id === request.id ? { ...item, progress } : item)));
      const removed = removedPendingAttachmentIds.current.delete(request.id);
      setPendingAttachments(current => current.filter(item => item.id !== request.id));
      if (!removed) {
        const nextAttachments = [...attachmentsRef.current, added];
        setAttachments(nextAttachments);
        persistDraft({ content: composerInput.current?.value() ?? contentRef.current, attachments: nextAttachments, reference: referenceRef.current });
      }
      if (request.previewUrl) URL.revokeObjectURL(request.previewUrl);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '附件上传失败');
      setPendingAttachments(current => current.map(item => item.id === request.id ? { ...item, progress: 100, state: 'failed' } : item));
    }
  };
  const startAttachmentUpload = (file: File) => {
    if (expired || sending) return;
    const request: PendingSidebarAttachment = {
      id: randomId(),
      file,
      previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
      progress: 0,
      state: 'uploading',
    };
    setPendingAttachments(current => [...current, request]);
    void upload(request);
  };
  const retryPendingAttachment = (id: string) => {
    const request = pendingAttachments.find(item => item.id === id);
    if (!request) return;
    const next = { ...request, progress: 0, state: 'uploading' as const };
    setPendingAttachments(current => current.map(item => item.id === id ? next : item));
    void upload(next);
  };
  const removePendingAttachment = (id: string) => {
    removedPendingAttachmentIds.current.add(id);
    setPendingAttachments(current => {
      const request = current.find(item => item.id === id);
      if (request?.previewUrl) URL.revokeObjectURL(request.previewUrl);
      return current.filter(item => item.id !== id);
    });
  };
  const updateModel = async (providerId: string, modelName: string, reasoningEffort: string | null) => {
    if (!bindingId) return;
    try {
      await api.switchAgentConversationModel(workspaceId, bindingId, providerId, modelName, reasoningEffort);
      await queryClient.invalidateQueries({ queryKey: ['agent-session', 'sidebar-conversation', workspaceId, bindingId] });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存模型设置失败');
    }
  };
  const connectedProviders = (providersQuery.data ?? []).filter(provider => provider.connection_state === 'CONNECTED');
  const provider = connectedProviders.find(item => item.id === currentModel?.providerId);
  const availableModels = provider?.models.filter(item => item.enabled) ?? [];
  const efforts = availableModels.find(item => item.model_name === currentModel?.modelName)?.supported_reasoning_efforts ?? [];
  const composerSuggestions = useMemo<ComposerSuggestion[]>(() => [
    ...(bindingId ? [{
      id: 'native:condense', kind: 'NATIVE' as const, token: '/condense', label: '压缩上下文',
      detail: canCondense ? '调用 OpenHands 原生 condenser，不会发送会话消息' : '当前会话处理完成后可调用',
      available: canCondense, nativeAction: 'CONDENSE' as const,
    }] : []),
    ...conversationComposerSuggestions(sourceQuery.data?.capabilities, capabilitiesQuery.data ?? []),
  ], [bindingId, canCondense, capabilitiesQuery.data, sourceQuery.data?.capabilities]);
  const composerDisabled = expired || sending || condensing || !currentModel || Boolean(bindingId && (hydrationQuery.loading || !hydrationQuery.data));
  const composerControl = bindingId && (paused || (running && !content.trim() && !attachments.length))
    ? { label: running ? '暂停侧边聊天 Agent' : '继续侧边聊天 Agent', action: running ? 'interrupt' as const : 'resume' as const }
    : undefined;
  const updateDraft = useCallback((_scope: string | undefined, value: string) => {
    setContent(value);
    persistDraft({ content: value, reference: referenceRef.current });
  }, [persistDraft]);
  const updateContentPresence = useCallback(() => undefined, []);
  const persistComposerDraft = useCallback(() => {
    persistDraft({ content: composerInput.current?.value() ?? contentRef.current, reference: referenceRef.current });
  }, [persistDraft]);
  const addConversationReference = useCallback(({ bindingId: sourceBindingId, eventId, title }: { bindingId: string; eventId: string; title: string }) => {
    if (!eventId) return;
    setReference(current => {
      const next = current?.source_binding_id === sourceBindingId && current.event_id === eventId
        ? current
        : { source_binding_id: sourceBindingId, event_id: eventId, content: title };
      persistDraft({ content: composerInput.current?.value() ?? contentRef.current, reference: next });
      return next;
    });
  }, [persistDraft]);
  return <SideConversationSurface label="侧边聊天" loading={Boolean(bindingId && hydrationQuery.loading)}
    conversation={bindingId || events.length ? {
      historyPrepend: hydrationQuery.historyPrepend, onHistoryAnchorCaptured: hydrationQuery.onHistoryAnchorCaptured, onHistoryAnchorRestored: hydrationQuery.onHistoryAnchorRestored,
      events, isGenerating: running || sending, isPaused: paused, conversationScope: bindingId ?? draftConversationId,
      requestSubmitting: sending, condensationPending: condensing,
      onOpenAttachment: attachment => onPreviewAttachment(attachment, bindingId),
      onOpenConversationReference, taskControl: hydrationQuery.data?.events.task_control,
      monitoring: hydrationQuery.data?.events.monitoring,
      connectionState: hydrationQuery.error ? 'unavailable' : 'connected',
    } : undefined}
    empty={<div className="agent-workbench-empty"><MessageSquarePlus size={28}/><b>向主会话追问</b><span>这是临时聊天：关闭后会删除，最长保留一小时。</span></div>}
    notice={expired && <section className="agent-sidebar-expired" role="status"><Clock3 size={16}/><span>侧边聊天会话已过期。已显示的内容仍可阅读，但不能继续发送消息。</span></section>}
    error={error ?? hydrationQuery.error}
    composer={
    <div className="agent-sidebar-chat-composer">
      <div className="agent-composer agent-sidebar-composer">
        <div className="agent-composer-attachments">
          {reference && <div className="agent-attachments agent-conversation-references" aria-label="已添加的会话引用"><span><button type="button" className="agent-attachment-open" aria-label="定位引用来源会话" title={reference.content} onClick={() => onOpenConversationReference(reference)}><Quote size={14}/><em>会话引用</em></button><button type="button" className="agent-attachment-remove" onClick={() => { setReference(undefined); persistDraft({ content: composerInput.current?.value() ?? contentRef.current }); }} aria-label="移除引用">×</button></span></div>}
          {(attachments.length > 0 || pendingAttachments.length > 0) && <div className="agent-attachments" aria-label="已添加的附件">{attachments.map(item => <span key={item.path}><button type="button" className="agent-attachment-open" title={`预览附件：${item.filename}`} onClick={() => onPreviewAttachment(item, bindingId)}>{item.image_data_url ? <img src={item.image_data_url} alt=""/> : <FileTypeIcon filename={item.filename} mimeType={item.mime_type} size={16}/>}<em>{item.filename}</em></button><button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.filename}`} onClick={() => { const nextAttachments = attachmentsRef.current.filter(candidate => candidate.path !== item.path); setAttachments(nextAttachments); persistDraft({ content: composerInput.current?.value() ?? contentRef.current, attachments: nextAttachments, reference: referenceRef.current }); }}>×</button></span>)}{pendingAttachments.map(item => <span key={item.id} className={`agent-pending-attachment ${item.state}`} title={item.state === 'failed' ? '附件上传失败，请重试或删除。' : `正在上传 ${item.progress}%`}><span className="agent-attachment-open">{item.previewUrl ? <img src={item.previewUrl} alt=""/> : <FileTypeIcon filename={item.file.name} mimeType={item.file.type} size={16}/>}<em>{item.file.name}</em>{item.state === 'uploading' && <small>{item.progress}%</small>}{item.state === 'failed' && <span className="agent-attachment-retry-overlay"><button type="button" onClick={() => retryPendingAttachment(item.id)}>重试</button></span>}</span>{item.state === 'uploading' && <i className="agent-attachment-progress" style={{ '--upload-progress': `${item.progress}%` } as CSSProperties}/>}<button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.file.name}`} onClick={() => removePendingAttachment(item.id)}>×</button></span>)}</div>}
        </div>
        <AgentComposerInput ref={composerInput} ariaLabel="发送侧边聊天消息" initialDraft={content} scope={bindingId ?? draftConversationId} suggestions={composerSuggestions} disabled={composerDisabled} placeholder={expired ? '侧边聊天会话已过期' : '给 Agent 发消息…'} onDraftChange={updateDraft} onContentPresenceChange={updateContentPresence} onDraftPersist={persistComposerDraft} onPaste={event => {
          const files = transferredFiles(event.clipboardData);
          if (!files.length) return;
          event.preventDefault();
          files.forEach(startAttachmentUpload);
        }} onDropFiles={files => files.forEach(startAttachmentUpload)} onDropConversationReference={transfer => addConversationReference(transfer)} onSubmit={() => void send()} onDirectSubmit={() => void send()} onNativeAction={action => { if (action === 'CONDENSE') void condense(); }}/>
        <footer><div className="agent-composer-context"><input ref={attachmentInput} aria-label="上传侧边聊天附件" type="file" multiple hidden onChange={event => { Array.from(event.currentTarget.files ?? []).forEach(startAttachmentUpload); event.currentTarget.value = ''; }}/><button type="button" aria-label="添加附件" disabled={composerDisabled} onClick={() => attachmentInput.current?.click()}><Plus size={17}/></button></div><div className="agent-composer-actions"><ComposerModelMenu providers={connectedProviders} providerId={currentModel?.providerId ?? ''} modelName={currentModel?.modelName ?? ''} models={availableModels} efforts={efforts} effort={currentModel?.reasoningEffort ?? ''} disabled={composerDisabled || pendingAttachments.length > 0} onSelect={(providerId, modelName) => { const model = connectedProviders.find(item => item.id === providerId)?.models.find(item => item.enabled && item.model_name === modelName); if (!model) return; if (bindingId) void updateModel(providerId, modelName, model.default_reasoning_effort ?? null); else setSelectedModelKey(`${providerId}:${modelName}`); }} onEffortChange={effort => { if (bindingId && currentModel) void updateModel(currentModel.providerId, currentModel.modelName, effort || null); }}/><button type="button" className={`agent-send${composerControl?.action === 'resume' ? ' resume' : ''}`} aria-label={sending ? (cancelling ? '正在取消发送' : '取消发送') : composerControl?.label ?? '发送侧边聊天消息'} disabled={sending ? cancelling || expired : composerControl ? expired : (!content.trim() && !attachments.length) || composerDisabled || pendingAttachments.length > 0} onClick={() => { if (sending) void cancelSend(); else if (composerControl) void control(); else void send(); }}>{sending || composerControl?.action === 'interrupt' ? <Square size={10} fill="currentColor"/> : composerControl?.action === 'resume' ? <Play size={12} fill="currentColor"/> : <Send size={16}/>}</button></div></footer>
      </div>
      <div className="agent-sidebar-composer-bottom" aria-hidden="true"/>
    </div>}
  />;
}
