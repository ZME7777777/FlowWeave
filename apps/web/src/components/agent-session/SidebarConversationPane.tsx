import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Clock3, LoaderCircle, MessageSquarePlus, Send, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api, ApiError, randomId } from '../../api/client';
import { ConversationSurface } from '../ConversationSurface';
import type { AgentConversationReference, ModelProvider, OpenHandsConversationEvent } from '../../types';

interface SidebarConversationPaneProps {
  workspaceId: string;
  sourceBindingId: string;
  sourceTitle?: string | null;
  initialReference?: AgentConversationReference;
  onClose: () => void;
}

function defaultModel(providers: ModelProvider[], sourceProviderId?: string | null, sourceModelName?: string | null) {
  const source = providers.find(provider => provider.id === sourceProviderId && provider.connection_state === 'CONNECTED');
  const provider = source ?? providers.find(candidate => candidate.connection_state === 'CONNECTED');
  const model = provider?.models.find(candidate => candidate.enabled && candidate.model_name === sourceModelName)
    ?? provider?.models.find(candidate => candidate.enabled && candidate.is_default)
    ?? provider?.models.find(candidate => candidate.enabled);
  return provider && model ? { providerId: provider.id, modelName: model.model_name } : undefined;
}

export function SidebarConversationPane({ workspaceId, sourceBindingId, sourceTitle, initialReference, onClose }: SidebarConversationPaneProps) {
  const queryClient = useQueryClient();
  const [bindingId, setBindingId] = useState<string>();
  const [reference, setReference] = useState<AgentConversationReference | undefined>(initialReference);
  const [content, setContent] = useState('');
  const [error, setError] = useState<string>();
  const [sending, setSending] = useState(false);
  const sourceQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-source', workspaceId, sourceBindingId],
    queryFn: () => api.agentConversation(workspaceId, sourceBindingId),
  });
  const providersQuery = useQuery({ queryKey: ['model-providers'], queryFn: api.providers });
  const fallback = defaultModel(providersQuery.data ?? [], sourceQuery.data?.model_provider_id, sourceQuery.data?.model_name);
  const selectedModel = fallback;
  const sidebarQuery = useQuery({
    queryKey: ['agent-session', 'sidebar', workspaceId, bindingId],
    queryFn: () => api.agentSidebarConversation(workspaceId, bindingId!),
    enabled: Boolean(bindingId),
    refetchInterval: query => query.state.data?.expired ? false : 15_000,
  });
  const expired = sidebarQuery.data?.expired === true;
  const hydrationQuery = useQuery({
    queryKey: ['agent-session', 'sidebar-hydration', workspaceId, bindingId],
    queryFn: () => api.agentConversationHydration(workspaceId, bindingId!),
    enabled: Boolean(bindingId),
    refetchInterval: expired ? false : 4_000,
  });
  const events = useMemo<OpenHandsConversationEvent[]>(
    () => hydrationQuery.data?.events.events ?? [],
    [hydrationQuery.data?.events.events],
  );
  const send = async () => {
    const message = content.trim();
    if (!message || sending || !selectedModel || expired) return;
    setSending(true);
    setError(undefined);
    try {
      if (!bindingId) {
        const conversationId = randomId();
        const result = await api.createAgentSidebarConversation(
          workspaceId,
          sourceBindingId,
          conversationId,
          selectedModel.providerId,
          selectedModel.modelName,
          message,
          reference ? [reference] : [],
        );
        setBindingId(result.conversation.id);
        setContent('');
        setReference(undefined);
        queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, result.conversation.id], {
          binding_id: result.conversation.id,
          source_binding_id: sourceBindingId,
          expires_at: result.expires_at,
          expired: false,
        });
        await queryClient.invalidateQueries({ queryKey: ['agent-session', 'sidebar-hydration', workspaceId, result.conversation.id] });
      } else {
        await api.sendAgentMessage(workspaceId, bindingId, message);
        setContent('');
        await queryClient.invalidateQueries({ queryKey: ['agent-session', 'sidebar-hydration', workspaceId, bindingId] });
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'AGENT_SIDEBAR_CONVERSATION_EXPIRED') {
        queryClient.setQueryData(['agent-session', 'sidebar', workspaceId, bindingId], (current: Record<string, unknown> | undefined) => ({ ...current, expired: true }));
      }
      setError(reason instanceof Error ? reason.message : '侧边聊天暂时不可用');
    } finally {
      setSending(false);
    }
  };
  useEffect(() => setReference(initialReference), [initialReference]);
  return <aside className="agent-sidebar-conversation open" aria-label="侧边聊天">
    <header className="agent-workbench-header">
      <div><span className="eyebrow">TEMPORARY SIDEBAR CHAT</span><h2>侧边聊天</h2><small className="agent-session-provider">关联主会话：{sourceTitle || '当前会话'}</small></div>
      <button type="button" className="agent-sidebar-close" aria-label="关闭侧边聊天" title="关闭侧边聊天" onClick={onClose}><X size={16}/></button>
    </header>
    <div className="agent-workbench-content">
      {hydrationQuery.isLoading && bindingId ? <div className="conversation-surface-empty"><LoaderCircle className="conversation-activity-spin" size={16}/><b>正在加载侧边聊天</b></div>
        : bindingId ? <ConversationSurface
          events={events}
          isGenerating={hydrationQuery.data?.readiness.execution_status === 'running'}
          isPaused={hydrationQuery.data?.readiness.execution_status === 'paused'}
          conversationScope={bindingId}
          requestSubmitting={sending}
          taskControl={hydrationQuery.data?.events.task_control}
          monitoring={hydrationQuery.data?.events.monitoring}
          connectionState={hydrationQuery.isError ? 'unavailable' : 'connected'}
        /> : <div className="agent-workbench-empty"><MessageSquarePlus size={28}/><b>向主会话追问</b><span>此临时聊天可读取关联主会话的基本元数据；一小时后会自动过期。</span></div>}
      {expired && <section className="agent-sidebar-expired" role="status"><Clock3 size={16}/><span>侧边聊天会话已过期。已显示的内容仍可阅读，但不能继续发送消息。</span></section>}
      {error && <p className="agent-workbench-error">{error}</p>}
    </div>
    <div className="agent-composer-dock">
      <div className="agent-composer">
        {reference && <div className="agent-sidebar-reference"><span>引用主会话内容</span><button type="button" onClick={() => setReference(undefined)} aria-label="移除引用">×</button><p>{reference.content}</p></div>}
        <textarea aria-label="发送侧边聊天消息" value={content} disabled={expired || sending || !selectedModel} placeholder={expired ? '侧边聊天会话已过期' : '向侧边聊天提问…'} onChange={event => setContent(event.target.value)} onKeyDown={event => {
          if (event.nativeEvent.isComposing || event.keyCode === 229 || event.key !== 'Enter' || event.shiftKey) return;
          event.preventDefault();
          void send();
        }}/>
        <footer><span>{expired ? '已过期' : selectedModel ? '临时会话将在一小时后清理' : '正在读取可用模型…'}</span><button type="button" className="agent-send" aria-label="发送侧边聊天消息" disabled={!content.trim() || sending || expired || !selectedModel} onClick={() => void send()}>{sending ? <LoaderCircle className="conversation-activity-spin" size={15}/> : <Send size={16}/>}</button></footer>
      </div>
    </div>
  </aside>;
}
