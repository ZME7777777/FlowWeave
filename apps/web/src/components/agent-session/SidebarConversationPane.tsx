import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Clock3, LoaderCircle, MessageSquarePlus, Paperclip, Play, Send, Square } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError, randomId, subscribeToAgentWorkspaceStream } from '../../api/client';
import { ConversationSurface } from '../ConversationSurface';
import type { AgentAttachment, AgentConversationReference, ModelProvider, OpenHandsConversationEvent } from '../../types';
import { ComposerModelMenu } from './ComposerModelMenu';

interface SidebarConversationPaneProps {
  workspaceId: string;
  sourceBindingId: string;
  initialReference?: AgentConversationReference;
  onBindingCreated: (bindingId: string) => void;
}

function defaultModel(providers: ModelProvider[], sourceProviderId?: string | null, sourceModelName?: string | null, sourceReasoningEffort?: string | null) {
  const source = providers.find(provider => provider.id === sourceProviderId && provider.connection_state === 'CONNECTED');
  const provider = source ?? providers.find(candidate => candidate.connection_state === 'CONNECTED');
  const model = provider?.models.find(candidate => candidate.enabled && candidate.model_name === sourceModelName)
    ?? provider?.models.find(candidate => candidate.enabled && candidate.is_default)
    ?? provider?.models.find(candidate => candidate.enabled);
  return provider && model ? { providerId: provider.id, modelName: model.model_name, reasoningEffort: sourceReasoningEffort ?? model.default_reasoning_effort ?? null } : undefined;
}

export function SidebarConversationPane({ workspaceId, sourceBindingId, initialReference, onBindingCreated }: SidebarConversationPaneProps) {
  const queryClient = useQueryClient();
  const attachmentInput = useRef<HTMLInputElement>(null);
  const [bindingId, setBindingId] = useState<string>();
  const [reference, setReference] = useState<AgentConversationReference | undefined>(initialReference);
  const [content, setContent] = useState('');
  const [attachments, setAttachments] = useState<AgentAttachment[]>([]);
  const [error, setError] = useState<string>();
  const [sending, setSending] = useState(false);
  const [selectedModelKey, setSelectedModelKey] = useState<string>();
  const sourceQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-source', workspaceId, sourceBindingId],
    queryFn: () => api.agentConversation(workspaceId, sourceBindingId),
  });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
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
  const events = useMemo<OpenHandsConversationEvent[]>(
    () => hydrationQuery.data?.events.events ?? [],
    [hydrationQuery.data?.events.events],
  );
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
  const invalidate = async (id: string) => {
    await queryClient.invalidateQueries({ queryKey: ['agent-session', 'sidebar-hydration', workspaceId, id] });
  };
  const markExpired = () => queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, bindingId], (current: Record<string, unknown> | undefined) => ({ ...current, expired: true }));
  const send = async () => {
    const message = content.trim();
    if ((!message && !attachments.length) || sending || !selectedModel || expired || running) return;
    setSending(true);
    setError(undefined);
    try {
      if (!bindingId) {
        if (attachments.length) throw new Error('请先发送首条文本消息，再添加附件。');
        const conversationId = randomId();
        const result = await api.createAgentSidebarConversation(
          workspaceId, sourceBindingId, conversationId, selectedModel.providerId, selectedModel.modelName, selectedModel.reasoningEffort, message, reference ? [reference] : [],
        );
        setBindingId(result.conversation.id);
        onBindingCreated(result.conversation.id);
        setContent('');
        setReference(undefined);
        queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, result.conversation.id], {
          binding_id: result.conversation.id, source_binding_id: sourceBindingId, expires_at: result.expires_at, expired: false,
        });
        await invalidate(result.conversation.id);
      } else {
        await api.sendAgentMessage(workspaceId, bindingId, message, attachments);
        setContent('');
        setAttachments([]);
        await invalidate(bindingId);
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'AGENT_SIDEBAR_CONVERSATION_EXPIRED') markExpired();
      setError(reason instanceof Error ? reason.message : '侧边聊天暂时不可用');
    } finally {
      setSending(false);
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
  const upload = async (files: File[]) => {
    if (!bindingId || expired) {
      setError('请先发送首条文本消息，再添加附件。');
      return;
    }
    try {
      const added = await Promise.all(files.map(file => api.uploadAgentAttachment(workspaceId, bindingId, file)));
      setAttachments(current => [...current, ...added]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '附件上传失败');
    }
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
  const connectedProviders = (providersQuery.data ?? []).filter(provider => provider.connection_state === 'CONNECTED');
  const provider = connectedProviders.find(item => item.id === currentModel?.providerId);
  const availableModels = provider?.models.filter(item => item.enabled) ?? [];
  const efforts = availableModels.find(item => item.model_name === currentModel?.modelName)?.supported_reasoning_efforts ?? [];
  const composerDisabled = expired || sending || !currentModel || running;
  return <section className="agent-sidebar-conversation" aria-label="侧边聊天">
    <div className="agent-sidebar-chat-content">
      {hydrationQuery.isLoading && bindingId ? <div className="conversation-surface-empty"><LoaderCircle className="conversation-activity-spin" size={16}/><b>正在加载会话</b></div>
        : bindingId ? <ConversationSurface events={events} isGenerating={running} liveTextReveal={running} isPaused={paused} conversationScope={bindingId} requestSubmitting={sending} taskControl={hydrationQuery.data?.events.task_control} monitoring={hydrationQuery.data?.events.monitoring} connectionState={hydrationQuery.isError ? 'unavailable' : 'connected'}/>
          : <div className="agent-workbench-empty"><MessageSquarePlus size={28}/><b>向主会话追问</b><span>这是临时聊天：关闭后会删除，最长保留一小时。</span></div>}
      {expired && <section className="agent-sidebar-expired" role="status"><Clock3 size={16}/><span>侧边聊天会话已过期。已显示的内容仍可阅读，但不能继续发送消息。</span></section>}
      {error && <p className="agent-workbench-error">{error}</p>}
    </div>
    <div className="agent-sidebar-chat-composer agent-composer-dock">
      {reference && <div className="agent-sidebar-reference"><span>引用主会话内容</span><button type="button" onClick={() => setReference(undefined)} aria-label="移除引用">×</button><p>{reference.content}</p></div>}
      {attachments.length > 0 && <div className="agent-attachments">{attachments.map(item => <span key={item.path}><em>{item.filename}</em><button type="button" aria-label={`移除附件 ${item.filename}`} onClick={() => setAttachments(current => current.filter(candidate => candidate.path !== item.path))}>×</button></span>)}</div>}
      <div className="agent-composer">
        <textarea aria-label="发送侧边聊天消息" value={content} disabled={composerDisabled} placeholder={expired ? '侧边聊天会话已过期' : running ? '当前回复完成后可继续发送' : '向侧边聊天提问…'} onChange={event => setContent(event.target.value)} onKeyDown={event => {
          if (event.nativeEvent.isComposing || event.keyCode === 229 || event.key !== 'Enter' || event.shiftKey) return;
          event.preventDefault();
          void send();
        }}/>
        <footer><div className="agent-composer-context"><input ref={attachmentInput} type="file" multiple hidden onChange={event => { void upload(Array.from(event.currentTarget.files ?? [])); event.currentTarget.value = ''; }}/><button type="button" aria-label="添加附件" disabled={!bindingId || composerDisabled} onClick={() => attachmentInput.current?.click()}><Paperclip size={17}/></button></div><div className="agent-composer-actions"><ComposerModelMenu providers={connectedProviders} providerId={currentModel?.providerId ?? ''} modelName={currentModel?.modelName ?? ''} models={availableModels} efforts={efforts} effort={currentModel?.reasoningEffort ?? ''} disabled={composerDisabled} onProviderChange={providerId => { const nextProvider = connectedProviders.find(item => item.id === providerId); const nextModel = nextProvider?.models.find(item => item.enabled && item.is_default) ?? nextProvider?.models.find(item => item.enabled); if (nextModel) { if (bindingId) void updateModel(providerId, nextModel.model_name, nextModel.default_reasoning_effort ?? null); else setSelectedModelKey(`${providerId}:${nextModel.model_name}`); } }} onModelChange={modelName => { const model = availableModels.find(item => item.model_name === modelName); if (bindingId && currentModel) void updateModel(currentModel.providerId, modelName, model?.default_reasoning_effort ?? null); else if (currentModel) setSelectedModelKey(`${currentModel.providerId}:${modelName}`); }} onEffortChange={effort => { if (bindingId && currentModel) void updateModel(currentModel.providerId, currentModel.modelName, effort || null); }}/>{bindingId && (running || paused) && <button type="button" className="agent-interrupt" aria-label={running ? '暂停侧边聊天 Agent' : '继续侧边聊天 Agent'} disabled={sending || expired} onClick={() => void control()}>{running ? <Square size={10} fill="currentColor"/> : <Play size={12} fill="currentColor"/>}</button>}<button type="button" className="agent-send" aria-label="发送侧边聊天消息" disabled={(!content.trim() && !attachments.length) || composerDisabled} onClick={() => void send()}>{sending ? <LoaderCircle className="conversation-activity-spin" size={15}/> : <Send size={16}/>}</button></div></footer>
      </div>
    </div>
  </section>;
}
