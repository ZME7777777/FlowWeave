import { useQuery } from '@tanstack/react-query';
import { CircleAlert, LoaderCircle } from 'lucide-react';
import { api } from '../../api/client';
import type { AgentConversation } from '../../types';

const phaseLabels = {
  CHILDREN: '停止并清理关联子会话',
  PARENT: '清理父会话',
  FINAL: '复核子会话与迟到操作',
  RECORD: '清理会话私有记录',
  COMPLETE: '正在确认删除结果',
};

export function ConversationDeletionPanel({ conversation, hostId, pending, onRetry }: {
  conversation: AgentConversation;
  hostId: string;
  pending: boolean;
  onRetry: () => void;
}) {
  const deletion = conversation.deletion;
  const failed = deletion?.state === 'DEAD';
  const children = useQuery({
    queryKey: ['agent-family-deletion-children', hostId, conversation.id],
    queryFn: ({ signal }) => api.agentChildConversations(conversation.id, signal),
    refetchInterval: failed ? false : 4_000,
    retry: false,
  });
  const ids = deletion?.child_binding_ids ?? children.data?.map(child => child.id) ?? [];
  return <section className="agent-conversation-deletion" aria-label="父子会话删除进度" role="status">
    {failed ? <CircleAlert size={22}/> : <LoaderCircle className="conversation-activity-spin" size={22}/>}
    <h2>{failed ? '父子会话删除未完成' : '正在删除父子会话'}</h2>
    <p>{deletion ? phaseLabels[deletion.phase] ?? '等待清理任务更新' : '正在读取清理进度'}</p>
    <p>已确认清理 {deletion?.completed_children ?? 0} / {ids.length} 个子会话</p>
    <ul aria-label="关联子会话清单">{ids.map(id => <li key={id}>
      {children.data?.find(child => child.id === id)?.display_title || '子会话'}<small>{id}</small>
    </li>)}</ul>
    {children.isError && <p>子会话标题暂时不可用，清理范围仍以原任务清单为准。</p>}
    {deletion?.failure_code && <p>失败代码 {deletion.failure_code}</p>}
    <p>父会话入口会保留到清理完成。当前会话及关联子会话暂时无法发送消息或执行控制。</p>
    {failed && <button type="button" className="secondary" disabled={pending} onClick={onRetry}>
      {pending ? '正在重试' : '重试原清理任务'}
    </button>}
  </section>;
}
