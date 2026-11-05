import { ChevronRight, CircleAlert, LoaderCircle, MessagesSquare } from 'lucide-react';
import type { AgentChildActivity, AgentChildConversation } from '../../types';

export function ChildConversationIndicator({ child, activity }: { child: AgentChildConversation; activity?: AgentChildActivity }) {
  const running = ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(activity?.execution_status ?? '');
  const abnormal = child.unread && child.unread_origin === 'SYSTEM' && (activity?.failed || activity?.possibly_stuck);
  const label = abnormal ? '子会话需要关注' : running ? '子会话正在运行' : child.unread ? '子会话未读' : '子会话';
  return <span className={`agent-child-indicator${running ? ' running' : ''}${abnormal ? ' attention' : child.unread ? ' unread' : ''}`} role="img" aria-label={label}>
    {running ? <LoaderCircle size={14}/> : abnormal ? <CircleAlert size={14}/> : <MessagesSquare size={14}/>}
  </span>;
}

type ChildConversationListProps = {
  children: AgentChildConversation[];
  activity: AgentChildActivity[];
  onSelect: (child: AgentChildConversation) => void;
};

export function ChildConversationList({ children, activity, onSelect, compact = false }: ChildConversationListProps & { compact?: boolean }) {
  return <div className={compact ? 'agent-workspace-source-list agent-child-list' : 'agent-sources-review agent-child-details'} role="list" aria-label={compact ? '子会话列表' : '全部子会话列表'}>
    {children.map(child => {
      const state = activity.find(item => item.id === child.id);
      const status = child.lifecycle === 'DELETE_PENDING' ? '正在删除' : child.lifecycle === 'PROVISIONING' ? '正在创建' : state?.condensing ? '正在压缩上下文' : state?.failed || state?.possibly_stuck ? '需要关注' : state?.execution_status === 'paused' ? '已暂停' : ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(state?.execution_status ?? '') ? '运行中' : state?.execution_status === 'finished' || state?.execution_status === 'idle' ? '可继续' : '待同步';
      const title = child.display_title || '未命名子会话';
      return <div role="listitem" key={child.id}><button type="button" data-child-binding-id={child.id} disabled={child.lifecycle === 'DELETE_PENDING' || child.lifecycle === 'PROVISIONING'} onClick={() => onSelect(child)} title={title}>
        <span className={compact ? 'agent-child-preview-icon' : 'agent-source-icon'}><ChildConversationIndicator child={child} activity={state}/></span>
        <span className="agent-child-list-copy"><b>{title}</b><em>{status}{!compact && child.unread ? ' · 未读' : ''}</em></span><ChevronRight size={compact ? 12 : 15}/>
      </button></div>;
    })}
  </div>;
}

export function ChildConversationSummary({ children, activity, onSelect, onViewAll }: ChildConversationListProps & { onViewAll: () => void }) {
  const running = activity.filter(item => ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(item.execution_status)).length;
  const unread = children.filter(child => child.unread).length;
  const visible = children.slice(0, 2);
  return <article className="agent-workspace-children"><MessagesSquare size={16}/><div>
    <div className="agent-child-summary-heading"><small>子会话</small><span>{running > 0 ? `${running} 个运行中 · ` : ''}{children.length} 个{unread > 0 && <em> · {unread} 条未读</em>}</span></div>
    <ChildConversationList children={visible} activity={activity} onSelect={onSelect} compact/>
    {children.length > visible.length && <button type="button" className="agent-workspace-view-all-sources" aria-label="查看全部子会话" onClick={onViewAll}><MessagesSquare size={12}/><span>查看全部</span></button>}
  </div></article>;
}
