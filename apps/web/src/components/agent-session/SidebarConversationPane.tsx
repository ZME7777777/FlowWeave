import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleAlert, Clock3, FileText, LoaderCircle, MessageSquarePlus, Play, Plus, Quote, Send, Square } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { api, ApiError, randomId, subscribeToAgentWorkspaceStream } from '../../api/client';
import { ConversationSurface } from '../ConversationSurface';
import type { AgentAttachment, AgentConversationReference, AgentSessionCapability, CapabilityAsset, ModelProvider, OpenHandsConversationEvent } from '../../types';
import { AgentComposerInput, type ComposerSuggestion } from './AgentComposerInput';
import { transferredFiles, type ComposerHandle } from './agentComposerInputUtils';
import { ComposerModelMenu } from './ComposerModelMenu';

interface SidebarConversationPaneProps {
  workspaceId: string;
  sourceBindingId: string;
  initialReference?: AgentConversationReference;
  sidebarBindingId?: string;
  onBindingCreated: (bindingId: string) => void;
  onPreviewAttachment: (attachment: AgentAttachment, bindingId?: string) => void;
}

interface PendingSidebarAttachment {
  id: string;
  file: File;
  previewUrl?: string;
  progress: number;
  state: 'uploading' | 'failed';
}

function defaultModel(providers: ModelProvider[], sourceProviderId?: string | null, sourceModelName?: string | null, sourceReasoningEffort?: string | null) {
  const source = providers.find(provider => provider.id === sourceProviderId && provider.connection_state === 'CONNECTED');
  const provider = source ?? providers.find(candidate => candidate.connection_state === 'CONNECTED');
  const model = provider?.models.find(candidate => candidate.enabled && candidate.model_name === sourceModelName)
    ?? provider?.models.find(candidate => candidate.enabled && candidate.is_default)
    ?? provider?.models.find(candidate => candidate.enabled);
  return provider && model ? { providerId: provider.id, modelName: model.model_name, reasoningEffort: sourceReasoningEffort ?? model.default_reasoning_effort ?? null } : undefined;
}

function sidebarComposerSuggestions(capabilities: AgentSessionCapability[] | undefined, catalogItems: CapabilityAsset[]): ComposerSuggestion[] {
  const catalog = new Map(catalogItems.map(item => [item.id, item]));
  const seen = new Set<string>();
  return (capabilities ?? []).flatMap<ComposerSuggestion>(reference => {
    const capability = catalog.get(reference.id);
    const description = capability?.description || reference.capability_key;
    if (reference.capability_type === 'SKILL') {
      const id = `skill:${reference.id}`;
      if (seen.has(id)) return [];
      seen.add(id);
      return [{ id, kind: 'SKILL' as const, token: `$${reference.capability_key}`, label: reference.capability_key, detail: description }];
    }
    if (reference.capability_type === 'MCP') {
      const id = `mcp:${reference.id}`;
      if (seen.has(id)) return [];
      seen.add(id);
      return [{ id, kind: 'MCP' as const, token: `使用 MCP「${reference.capability_key}」：`, label: reference.capability_key, detail: `${description} · 以自然语言说明要执行的操作` }];
    }
    const contributions = capability?.document.contributions;
    const commands = contributions && typeof contributions === 'object' && Array.isArray((contributions as Record<string, unknown>).commands)
      ? ((contributions as Record<string, unknown>).commands as unknown[]).filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
      : [];
    const skills = contributions && typeof contributions === 'object' && Array.isArray((contributions as Record<string, unknown>).skills)
      ? ((contributions as Record<string, unknown>).skills as unknown[]).filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
      : [];
    return [
      ...commands.map(command => ({ id: `command:${reference.id}:${command}`, kind: 'COMMAND' as const, token: `/${reference.capability_key}:${command}`, label: command, detail: `${reference.capability_key} 命令 · ${description}` })),
      ...skills.map(skill => ({ id: `plugin-skill:${reference.id}:${skill}`, kind: 'SKILL' as const, token: `$${skill}`, label: skill, detail: `${reference.capability_key} 提供的技能 · ${description}` })),
    ].filter(item => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
  });
}

export function SidebarConversationPane({ workspaceId, sourceBindingId, initialReference, sidebarBindingId, onBindingCreated, onPreviewAttachment }: SidebarConversationPaneProps) {
  const queryClient = useQueryClient();
  const attachmentInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<ComposerHandle>(null);
  const removedPendingAttachmentIds = useRef(new Set<string>());
  const [bindingId, setBindingId] = useState<string | undefined>(sidebarBindingId);
  const bindingIdRef = useRef(bindingId);
  const [draftConversationId] = useState(randomId);
  const [reference, setReference] = useState<AgentConversationReference | undefined>(initialReference);
  const [content, setContent] = useState('');
  const [attachments, setAttachments] = useState<AgentAttachment[]>([]);
  const [pendingAttachments, setPendingAttachments] = useState<PendingSidebarAttachment[]>([]);
  const [error, setError] = useState<string>();
  const [sending, setSending] = useState(false);
  const [condensing, setCondensing] = useState(false);
  const [optimisticMessage, setOptimisticMessage] = useState<OpenHandsConversationEvent>();
  const [selectedModelKey, setSelectedModelKey] = useState<string>();
  const sourceQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-source', workspaceId, sourceBindingId],
    queryFn: () => api.agentConversation(workspaceId, sourceBindingId),
  });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const capabilitiesQuery = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const models = useMemo(() => (providersQuery.data ?? []).flatMap(provider => provider.connection_state === 'CONNECTED'
    ? provider.models.filter(model => model.enabled).map(model => ({ providerId: provider.id, modelName: model.model_name, reasoningEffort: model.default_reasoning_effort ?? null, label: `${provider.name} · ${model.model_name}` }))
    : []), [providersQuery.data]);
  const fallback = defaultModel(providersQuery.data ?? [], sourceQuery.data?.model_provider_id, sourceQuery.data?.model_name, sourceQuery.data?.reasoning_effort);
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
  const hydrationQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-hydration', workspaceId, bindingId],
    queryFn: () => api.agentConversationHydration(workspaceId, bindingId!),
    enabled: Boolean(bindingId),
    refetchInterval: expired ? false : 2_000,
  });
  const events = useMemo<OpenHandsConversationEvent[]>(() => {
    const formal = hydrationQuery.data?.events.events ?? [];
    if (!optimisticMessage || formal.some(event => event.id === optimisticMessage.id || event.payload._flowweave_submission_id === optimisticMessage.id)) return formal;
    return [...formal, optimisticMessage];
  }, [hydrationQuery.data?.events.events, optimisticMessage]);
  useEffect(() => {
    if (!optimisticMessage) return;
    const formal = hydrationQuery.data?.events.events ?? [];
    if (formal.some(event => event.id === optimisticMessage.id || event.payload._flowweave_submission_id === optimisticMessage.id)) setOptimisticMessage(undefined);
  }, [hydrationQuery.data?.events.events, optimisticMessage]);
  useEffect(() => {
    if (!bindingId || expired) return;
    return subscribeToAgentWorkspaceStream(workspaceId, bindingId, event => {
      if (event.type === 'event' || event.type === 'message_complete') {
        void queryClient.invalidateQueries({ queryKey: ['agent-session', 'sidebar-hydration', workspaceId, bindingId] });
      }
    });
  }, [bindingId, expired, queryClient, workspaceId]);
  const currentModel = conversationQuery.data
    ? defaultModel(providersQuery.data ?? [], conversationQuery.data.model_provider_id, conversationQuery.data.model_name, conversationQuery.data.reasoning_effort)
    : selectedModel;
  const readiness = hydrationQuery.data?.readiness;
  const running = readiness?.execution_status === 'running' || readiness?.execution_status === 'starting';
  const paused = readiness?.execution_status === 'paused';
  const canCondense = Boolean(bindingId && !expired && !sending && readiness?.ready && (readiness.execution_status === 'idle' || paused));
  const invalidate = async (id: string) => {
    await queryClient.invalidateQueries({ queryKey: ['agent-session', 'sidebar-hydration', workspaceId, id] });
  };
  const markExpired = () => queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, bindingId], (current: Record<string, unknown> | undefined) => ({ ...current, expired: true }));
  const send = async () => {
    const message = composerInput.current?.value().trim() ?? content.trim();
    if ((!message && !attachments.length) || sending || pendingAttachments.length > 0 || !selectedModel || expired || running) return;
    const submissionId = randomId();
    setSending(true);
    setError(undefined);
    setOptimisticMessage({ id: submissionId, event_type: 'MESSAGE', payload: { source: 'user', content: message, attachments, conversation_references: reference ? [reference] : [], _flowweave_submission_id: submissionId, _flowweave_projection_state: 'submitting' } });
    try {
      if (!bindingId) {
        const result = await api.createAgentSidebarConversation(
          workspaceId, sourceBindingId, draftConversationId, message, attachments, reference ? [reference] : [],
        );
        bindingIdRef.current = result.conversation.id;
        setBindingId(result.conversation.id);
        onBindingCreated(result.conversation.id);
        setContent('');
        composerInput.current?.replace('');
        setAttachments([]);
        setReference(undefined);
        queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, result.conversation.id], {
          binding_id: result.conversation.id, source_binding_id: sourceBindingId, expires_at: result.expires_at, expired: false,
        });
        await invalidate(result.conversation.id);
      } else {
        await api.sendAgentMessage(workspaceId, bindingId, message, attachments);
        setContent('');
        composerInput.current?.replace('');
        setAttachments([]);
        await invalidate(bindingId);
      }
    } catch (reason) {
      setOptimisticMessage(undefined);
      if (reason instanceof ApiError && reason.code === 'AGENT_SIDEBAR_CONVERSATION_EXPIRED') markExpired();
      setError(reason instanceof Error ? reason.message : '侧边聊天暂时不可用');
    } finally {
      setSending(false);
    }
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
    if (!bindingId || expired || sending) return;
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
      if (!removed) setAttachments(current => [...current, added]);
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
  useEffect(() => setReference(initialReference), [initialReference]);
  useEffect(() => () => {
    if (!bindingIdRef.current) void api.deleteAgentWorkspaceDraftAttachments(workspaceId, draftConversationId).catch(() => undefined);
  }, [draftConversationId, workspaceId]);
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
    ...sidebarComposerSuggestions(sourceQuery.data?.capabilities, capabilitiesQuery.data ?? []),
  ], [bindingId, canCondense, capabilitiesQuery.data, sourceQuery.data?.capabilities]);
  const composerDisabled = expired || sending || condensing || !currentModel || running;
  const updateDraft = useCallback((_scope: string | undefined, value: string) => setContent(value), []);
  const updateContentPresence = useCallback(() => undefined, []);
  const persistDraft = useCallback(() => undefined, []);
  return <section className="agent-sidebar-conversation" aria-label="侧边聊天">
    <div className="agent-sidebar-chat-content">
      {hydrationQuery.isLoading && bindingId ? <div className="conversation-surface-empty"><LoaderCircle className="conversation-activity-spin" size={16}/><b>正在加载会话</b></div>
        : bindingId ? <ConversationSurface events={events} isGenerating={running || sending} liveTextReveal={running} isPaused={paused} conversationScope={bindingId} requestSubmitting={sending} condensationPending={condensing} onOpenAttachment={attachment => onPreviewAttachment(attachment, bindingId)} taskControl={hydrationQuery.data?.events.task_control} monitoring={hydrationQuery.data?.events.monitoring} connectionState={hydrationQuery.isError ? 'unavailable' : hydrationQuery.isFetching && !hydrationQuery.data ? 'checking' : 'connected'}/>
          : <div className="agent-workbench-empty"><MessageSquarePlus size={28}/><b>向主会话追问</b><span>这是临时聊天：关闭后会删除，最长保留一小时。</span></div>}
      {expired && <section className="agent-sidebar-expired" role="status"><Clock3 size={16}/><span>侧边聊天会话已过期。已显示的内容仍可阅读，但不能继续发送消息。</span></section>}
      {error && <section className="agent-workbench-error" role="alert"><CircleAlert size={17}/><div><b>操作未完成</b><span>{error}</span></div></section>}
    </div>
    <div className="agent-sidebar-chat-composer">
      <div className="agent-composer agent-sidebar-composer">
        <div className="agent-composer-attachments">
          {reference && <div className="agent-attachments agent-conversation-references" aria-label="已添加的会话引用"><span><span className="agent-attachment-open" title={reference.content}><Quote size={14}/><em>会话引用</em></span><button type="button" className="agent-attachment-remove" onClick={() => setReference(undefined)} aria-label="移除引用">×</button></span></div>}
          {(attachments.length > 0 || pendingAttachments.length > 0) && <div className="agent-attachments" aria-label="已添加的附件">{attachments.map(item => <span key={item.path}><button type="button" className="agent-attachment-open" title={`预览附件：${item.filename}`} onClick={() => onPreviewAttachment(item, bindingId)}>{item.image_data_url && <img src={item.image_data_url} alt=""/>}<em>{item.filename}</em></button><button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(current => current.filter(candidate => candidate.path !== item.path))}>×</button></span>)}{pendingAttachments.map(item => <span key={item.id} className={`agent-pending-attachment ${item.state}`} title={item.state === 'failed' ? '附件上传失败，请重试或删除。' : `正在上传 ${item.progress}%`}><span className="agent-attachment-open">{item.previewUrl ? <img src={item.previewUrl} alt=""/> : <FileText size={14}/>}<em>{item.file.name}</em>{item.state === 'uploading' && <small>{item.progress}%</small>}{item.state === 'failed' && <span className="agent-attachment-retry-overlay"><button type="button" onClick={() => retryPendingAttachment(item.id)}>重试</button></span>}</span>{item.state === 'uploading' && <i className="agent-attachment-progress" style={{ '--upload-progress': `${item.progress}%` } as CSSProperties}/>}<button type="button" className="agent-attachment-remove" aria-label={`移除附件 ${item.file.name}`} onClick={() => removePendingAttachment(item.id)}>×</button></span>)}</div>}
        </div>
        <AgentComposerInput ref={composerInput} ariaLabel="发送侧边聊天消息" initialDraft={content} scope={bindingId ?? draftConversationId} suggestions={composerSuggestions} disabled={composerDisabled} placeholder={expired ? '侧边聊天会话已过期' : running ? '当前回复完成后可继续发送' : '给 Agent 发消息…'} onDraftChange={updateDraft} onContentPresenceChange={updateContentPresence} onDraftPersist={persistDraft} onPaste={event => {
          const files = transferredFiles(event.clipboardData);
          if (!files.length) return;
          event.preventDefault();
          files.forEach(startAttachmentUpload);
        }} onDropFiles={files => files.forEach(startAttachmentUpload)} onSubmit={() => void send()} onDirectSubmit={() => void send()} onNativeAction={action => { if (action === 'CONDENSE') void condense(); }}/>
        <footer><div className="agent-composer-context"><input ref={attachmentInput} aria-label="上传侧边聊天附件" type="file" multiple hidden onChange={event => { Array.from(event.currentTarget.files ?? []).forEach(startAttachmentUpload); event.currentTarget.value = ''; }}/><button type="button" aria-label="添加附件" disabled={composerDisabled} onClick={() => attachmentInput.current?.click()}><Plus size={17}/></button></div><div className="agent-composer-actions"><ComposerModelMenu providers={connectedProviders} providerId={currentModel?.providerId ?? ''} modelName={currentModel?.modelName ?? ''} models={availableModels} efforts={efforts} effort={currentModel?.reasoningEffort ?? ''} disabled={composerDisabled || pendingAttachments.length > 0} onProviderChange={providerId => { const nextProvider = connectedProviders.find(item => item.id === providerId); const nextModel = nextProvider?.models.find(item => item.enabled && item.is_default) ?? nextProvider?.models.find(item => item.enabled); if (nextModel) { if (bindingId) void updateModel(providerId, nextModel.model_name, nextModel.default_reasoning_effort ?? null); else setSelectedModelKey(`${providerId}:${nextModel.model_name}`); } }} onModelChange={modelName => { const model = availableModels.find(item => item.model_name === modelName); if (bindingId && currentModel) void updateModel(currentModel.providerId, modelName, model?.default_reasoning_effort ?? null); else if (currentModel) setSelectedModelKey(`${currentModel.providerId}:${modelName}`); }} onEffortChange={effort => { if (bindingId && currentModel) void updateModel(currentModel.providerId, currentModel.modelName, effort || null); }}/>{bindingId && (running || paused) && <button type="button" className="agent-interrupt" aria-label={running ? '暂停侧边聊天 Agent' : '继续侧边聊天 Agent'} disabled={sending || expired} onClick={() => void control()}>{running ? <Square size={10} fill="currentColor"/> : <Play size={12} fill="currentColor"/>}</button>}<button type="button" className="agent-send" aria-label="发送侧边聊天消息" disabled={(!content.trim() && !attachments.length) || composerDisabled || pendingAttachments.length > 0} onClick={() => void send()}>{sending ? <LoaderCircle className="conversation-activity-spin" size={15}/> : <Send size={16}/>}</button></div></footer>
      </div>
      <div className="agent-sidebar-composer-bottom" aria-hidden="true"/>
    </div>
  </section>;
}
