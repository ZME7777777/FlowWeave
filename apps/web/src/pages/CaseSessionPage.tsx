import { useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { agentWorkspaceSessionGateway } from '../api/agent-session-gateway';
import { AgentSessionWorkbench } from '../components/agent-session/AgentSessionWorkbench';
import { caseSessionHost } from '../components/agent-session/session-host';
import type { Case } from '../types';

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
  if (item.isLoading) return <main className="auth-loading" role="status"><span>正在加载事项协调会话…</span></main>;
  if (item.error) return <main className="empty"><b>无法打开事项协调会话</b><span>{item.error.message}</span><button className="secondary" onClick={onReturn}>返回事项</button></main>;
  const belongsToCase = item.data?.control_conversation_binding_id === bindingId
    || item.data?.control_rounds?.some(round => round.state === 'STARTED' && round.binding_id === bindingId);
  if (!belongsToCase || !item.data?.control_work_directory_id) {
    return <main className="empty"><b>该会话不属于当前事项</b><span>请从事项工作区进入其协调会话。</span><button className="secondary" onClick={onReturn}>返回事项</button></main>;
  }
  return <AgentSessionWorkbench
    gateway={gateway}
    host={host}
    onNavigate={onNavigate}
    onReturnToSource={onReturn}
    scopeWorkDirectoryId={item.data.control_work_directory_id}
  />;
}
