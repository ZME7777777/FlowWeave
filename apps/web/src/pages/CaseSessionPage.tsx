import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { AgentSessionWorkbench } from '../components/agent-session/AgentSessionWorkbench';
import { caseSessionHost } from '../components/agent-session/session-host';

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
  const item = useQuery({ queryKey: ['case', caseId], queryFn: () => api.case(caseId) });
  const host = useMemo(() => caseSessionHost(caseId), [caseId]);
  if (item.isLoading) return <main className="auth-loading" role="status"><span>正在加载事项协调会话…</span></main>;
  if (item.error) return <main className="empty"><b>无法打开事项协调会话</b><span>{item.error.message}</span><button className="secondary" onClick={onReturn}>返回事项</button></main>;
  if (item.data?.control_conversation_binding_id !== bindingId || !item.data.control_work_directory_id) {
    return <main className="empty"><b>该会话不属于当前事项</b><span>请从事项工作区进入其协调会话。</span><button className="secondary" onClick={onReturn}>返回事项</button></main>;
  }
  return <AgentSessionWorkbench
    host={host}
    onNavigate={onNavigate}
    onReturnToSource={onReturn}
    scopeWorkDirectoryId={item.data.control_work_directory_id}
  />;
}
