import { useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { agentWorkspaceSessionGateway } from '../api/agent-session-gateway';
import { AgentSessionWorkbench } from '../components/agent-session/AgentSessionWorkbench';
import { caseSessionHost } from '../components/agent-session/session-host';
import type { Case } from '../types';

/**
 * A Case coordination conversation is an ordinary Agent conversation scoped to
 * the Case work directory. It renders inside the Case workspace; this component
 * only supplies the Case host/gateway and never owns conversation UI.
 */
export function CaseSessionPage({
  caseId,
  bindingId,
  onNavigate,
  onReturn,
}: {
  caseId: string;
  bindingId: string;
  onNavigate: (path: string, replace?: boolean) => void;
  onReturn: () => void;
}) {
  const queryClient = useQueryClient();
  const item = useQuery({ queryKey: ['case', caseId], queryFn: () => api.case(caseId) });
  const host = useMemo(() => caseSessionHost(caseId), [caseId]);
  const gateway = useMemo(() => ({
    ...agentWorkspaceSessionGateway,
    id: `case:${caseId}`,
    features: {
      ...agentWorkspaceSessionGateway.features,
      conversationDeletion: item.data?.control_conversation_binding_id === bindingId,
    },
    api: {
      ...agentWorkspaceSessionGateway.api,
      deleteConversation: async (_workspaceId: string, candidateBindingId: string) => {
        if (candidateBindingId !== bindingId || item.data?.control_conversation_binding_id !== candidateBindingId) throw new Error('只能删除当前 Case 助手会话；历史轮次会保留。');
        await api.deleteCaseControlConversation(caseId, candidateBindingId);
        queryClient.setQueryData<Case | undefined>(['case', caseId], current => current ? { ...current, control_conversation_binding_id: null } : current);
        await queryClient.invalidateQueries({ queryKey: ['cases'] });
      },
    },
  }), [bindingId, caseId, item.data?.control_conversation_binding_id, queryClient]);
  if (item.isLoading) return <section className="auth-loading" role="status"><span>正在加载 Case Agent 会话…</span></section>;
  if (item.error) return <section className="empty"><b>无法打开 Case Agent 会话</b><span>{item.error.message}</span><button className="secondary" onClick={onReturn}>返回场景</button></section>;
  const belongsToCase = item.data?.control_conversation_binding_id === bindingId
    || item.data?.control_rounds?.some(round => round.state === 'STARTED' && round.binding_id === bindingId);
  if (!belongsToCase || !item.data?.control_work_directory_id) {
    return <section className="empty"><b>该会话不属于当前场景</b><span>请从场景工作区进入其 Case Agent 会话。</span><button className="secondary" onClick={onReturn}>返回场景</button></section>;
  }
  return <AgentSessionWorkbench
    gateway={gateway}
    host={host}
    onNavigate={onNavigate}
    onReturnToSource={onReturn}
    scopeWorkDirectoryId={item.data.control_work_directory_id}
    embedded
    onHostStateChanged={() => { void queryClient.invalidateQueries({ queryKey: ['cases'] }); void queryClient.invalidateQueries({ queryKey: ['case', caseId] }); }}
  />;
}
