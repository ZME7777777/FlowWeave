import { ChevronLeft, ChevronRight, CircleAlert, LoaderCircle, MessageSquareText } from 'lucide-react';
import { useState } from 'react';
import type { AgentChildActivity, AgentChildConversation } from '../../types';

export function ChildConversationIndicator({ child, activity }: { child: AgentChildConversation; activity?: AgentChildActivity }) {
  const running = ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(activity?.execution_status ?? '');
  const abnormal = child.unread && child.unread_origin === 'SYSTEM' && (activity?.failed || activity?.possibly_stuck);
  const label = abnormal ? '子会话需要关注' : running ? '子会话正在运行' : child.unread ? '子会话未读' : '子会话';
  return <span className={`agent-child-indicator${running ? ' running' : ''}${abnormal ? ' attention' : child.unread ? ' unread' : ''}`} role="img" aria-label={label}>
    {running ? <LoaderCircle size={14}/> : abnormal ? <CircleAlert size={14}/> : <MessageSquareText size={14}/>}
  </span>;
}

export function ChildConversationSummary({ children, activity, onSelect }: {
  children: AgentChildConversation[];
  activity: AgentChildActivity[];
  onSelect: (child: AgentChildConversation) => void;
}) {
  const [page, setPage] = useState(0);
  const running = activity.filter(item => ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(item.execution_status)).length;
  const unread = children.filter(child => child.unread).length;
  const pageSize = 2;
  const lastPage = Math.max(0, Math.ceil(children.length / pageSize) - 1);
  const visiblePage = Math.min(page, lastPage);
  return <article className="agent-workspace-children"><MessageSquareText size={16}/><div>
    <div className="agent-child-summary-heading"><small>子会话</small><span>{running > 0 ? `${running} 个运行中 · ` : ''}{children.length} 个{unread > 0 && <em> · {unread} 条未读</em>}</span></div>
    <div className="agent-workspace-source-list agent-child-list" role="list" aria-label="子会话列表">
      {children.slice(visiblePage * pageSize, (visiblePage + 1) * pageSize).map(child => {
        const state = activity.find(item => item.id === child.id);
        const status = child.lifecycle === 'DELETE_PENDING' ? '正在删除' : child.lifecycle === 'PROVISIONING' ? '正在创建' : state?.condensing ? '正在压缩上下文' : state?.failed || state?.possibly_stuck ? '需要关注' : state?.execution_status === 'paused' ? '已暂停' : ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(state?.execution_status ?? '') ? '运行中' : state?.execution_status === 'finished' || state?.execution_status === 'idle' ? '可继续' : '待同步';
        const title = child.display_title || '未命名子会话';
        return <div role="listitem" key={child.id}><button type="button" data-child-binding-id={child.id} disabled={child.lifecycle === 'DELETE_PENDING' || child.lifecycle === 'PROVISIONING'} onClick={() => onSelect(child)} title={title}>
          <span className="agent-workspace-source-glyph"><ChildConversationIndicator child={child} activity={state}/></span>
          <span className="agent-child-list-copy"><b>{title}</b><em>{status}</em></span><ChevronRight size={12}/>
        </button></div>;
      })}
    </div>
    {lastPage > 0 && <nav className="agent-child-pagination" aria-label="子会话分页"><button type="button" aria-label="上一页子会话" disabled={visiblePage === 0} onClick={() => setPage(visiblePage - 1)}><ChevronLeft size={12}/></button><span>{visiblePage + 1} / {lastPage + 1}</span><button type="button" aria-label="下一页子会话" disabled={visiblePage === lastPage} onClick={() => setPage(visiblePage + 1)}><ChevronRight size={12}/></button></nav>}
  </div></article>;
}
