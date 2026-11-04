import { ChevronLeft, ChevronRight, CircleAlert, LoaderCircle, MessageSquareText, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { AgentChildActivity, AgentChildConversation } from '../../types';
import { useEscapeClose } from '../useEscapeClose';

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
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number }>();
  const [page, setPage] = useState(0);
  const close = () => setPosition(undefined);
  useEscapeClose(close, Boolean(position));
  useEffect(() => {
    if (!position) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !panel.current?.contains(event.target) && !trigger.current?.contains(event.target)) close();
    };
    const dismiss = () => close();
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', dismiss);
    return () => { document.removeEventListener('pointerdown', outside); window.removeEventListener('resize', dismiss); };
  }, [position]);
  const running = activity.filter(item => ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(item.execution_status)).length;
  const unread = children.filter(child => child.unread).length;
  const pageSize = 6;
  const lastPage = Math.max(0, Math.ceil(children.length / pageSize) - 1);
  const visiblePage = Math.min(page, lastPage);
  return <article className="agent-workspace-children"><MessageSquareText size={16}/><div><small>子会话</small>
    <button ref={trigger} type="button" aria-label="查看子会话" aria-expanded={Boolean(position)} onClick={() => {
      if (position) { close(); return; }
      const bounds = trigger.current!.getBoundingClientRect();
      setPage(0);
      setPosition({ left: Math.max(12, Math.min(bounds.left, window.innerWidth - 372)), top: Math.max(12, Math.min(bounds.bottom + 8, window.innerHeight - 410)) });
    }}><span><b>{running ? `${running} 个运行中` : `${children.length} 个会话`}</b>{unread > 0 && <em>{unread} 条未读</em>}</span><span className="agent-child-summary-count">{children.length}<ChevronRight size={13}/></span></button>
  </div>{position && createPortal(<div ref={panel} className="agent-child-picker" role="dialog" aria-label="子会话列表" style={position}>
    <header><div><b>子会话</b><small>{running ? '子会话仍在运行' : '独立历史与持续会话'}</small></div><button type="button" aria-label="关闭子会话列表" onClick={close}><X size={15}/></button></header>
    <div>{children.slice(visiblePage * pageSize, (visiblePage + 1) * pageSize).map(child => {
      const state = activity.find(item => item.id === child.id);
      const status = child.lifecycle === 'DELETE_PENDING' ? '正在删除' : state?.condensing ? '正在压缩上下文' : state?.failed || state?.possibly_stuck ? '需要关注' : state?.execution_status === 'paused' ? '已暂停' : ['running', 'starting', 'executing', 'stopping', 'waiting_for_confirmation'].includes(state?.execution_status ?? '') ? '运行中' : state?.execution_status === 'finished' || state?.execution_status === 'idle' ? '可继续' : '待同步';
      return <button type="button" key={child.id} data-child-binding-id={child.id} className="agent-child-picker-row" disabled={child.lifecycle === 'DELETE_PENDING' || child.lifecycle === 'PROVISIONING'} onClick={() => { onSelect(child); close(); }}>
        <ChildConversationIndicator child={child} activity={state}/><span><b>{child.display_title || '未命名子会话'}</b><small>{status}</small></span>{child.unread && <i aria-label="未读"/>}<ChevronRight size={14}/>
      </button>;
    })}</div>
    {lastPage > 0 && <footer><button type="button" aria-label="上一页子会话" disabled={visiblePage === 0} onClick={() => setPage(visiblePage - 1)}><ChevronLeft size={14}/></button><span>{visiblePage + 1} / {lastPage + 1}</span><button type="button" aria-label="下一页子会话" disabled={visiblePage === lastPage} onClick={() => setPage(visiblePage + 1)}><ChevronRight size={14}/></button></footer>}
  </div>, document.body)}</article>;
}
