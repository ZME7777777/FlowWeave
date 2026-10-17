import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArchiveX, CheckSquare, ChevronDown, Clock3, MessageSquareText, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Pagination } from '../components/Pagination';
import { api } from '../api/client';
import type { ManagedAgentConversation } from '../types';

const DAY = 24 * 60 * 60 * 1000;
const PAGE_SIZE = 20;

type InactivePeriod = 'all' | '30' | '90';

const PERIOD_OPTIONS: { value: InactivePeriod; label: string }[] = [
  { value: '30', label: '30 天前' },
  { value: '90', label: '90 天前' },
  { value: 'all', label: '全部时间' },
];

function conversationTitle(item: ManagedAgentConversation) {
  return item.display_title?.trim() || '未命名会话';
}

function activityTime(item: ManagedAgentConversation) {
  return item.last_message_at ?? item.updated_at;
}

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
}

function PeriodDropdown({ value, onChange }: { value: InactivePeriod; onChange: (v: InactivePeriod) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const label = PERIOD_OPTIONS.find(o => o.value === value)?.label ?? value;

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  return (
    <div className="cm-period-dropdown" ref={ref}>
      <button
        type="button"
        className="cm-period-trigger secondary"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
      >
        <span>{label}</span>
        <ChevronDown size={12} className={open ? 'rotated' : ''} />
      </button>
      {open && (
        <ul className="cm-period-menu" role="listbox" aria-label="最后活动时间">
          {PERIOD_OPTIONS.map(opt => (
            <li key={opt.value} role="option" aria-selected={opt.value === value}>
              <button
                type="button"
                onClick={() => { onChange(opt.value); setOpen(false); }}
              >
                {opt.value === value && <span className="cm-period-check">✓</span>}
                {opt.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ConversationManagementPage() {
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const [period, setPeriod] = useState<InactivePeriod>('30');
  const [includePinned, setIncludePinned] = useState(false);
  const [includeUnread, setIncludeUnread] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [page, setPage] = useState(1);

  const workspace = useQuery({ queryKey: ['agent-workspace', 'default'], queryFn: ({ signal }) => api.defaultAgentWorkspace(signal) });
  const conversations = useQuery({
    queryKey: ['agent-conversation-management', workspace.data?.id],
    queryFn: () => api.agentConversationManagement(workspace.data!.id),
    enabled: Boolean(workspace.data?.id),
  });

  const visible = useMemo(() => {
    const cutoff = period === 'all' ? undefined : Date.now() - Number(period) * DAY;
    const term = query.trim().toLocaleLowerCase();
    return (conversations.data?.items ?? []).filter(item => {
      if (!includePinned && item.pinned) return false;
      if (!includeUnread && item.unread) return false;
      if (cutoff && new Date(activityTime(item)).getTime() > cutoff) return false;
      return !term || conversationTitle(item).toLocaleLowerCase().includes(term);
    });
  }, [conversations.data?.items, includePinned, includeUnread, period, query]);

  // Reset page when filters change
  useEffect(() => { setPage(1); }, [query, period, includePinned, includeUnread]);

  const paged = useMemo(() => visible.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE), [visible, page]);

  const visibleIds = useMemo(() => new Set(visible.map(item => item.id)), [visible]);
  const selectedVisible = visible.filter(item => selectedIds.has(item.id));
  const selectAllVisible = visible.length > 0 && visible.every(item => selectedIds.has(item.id));

  useEffect(() => {
    setSelectedIds(current => new Set([...current].filter(id => visibleIds.has(id))));
  }, [visibleIds]);

  const remove = useMutation({
    mutationFn: () => api.deleteManagedAgentConversations(workspace.data!.id, selectedVisible.map(item => item.id)),
    onSuccess: async result => {
      setSelectedIds(new Set());
      setConfirming(false);
      await Promise.all([
        client.invalidateQueries({ queryKey: ['agent-conversation-management', workspace.data?.id] }),
        client.invalidateQueries({ queryKey: ['agent-session'] }),
      ]);
      if (result.skipped.length) {
        window.alert(`已删除 ${result.deleted_ids.length} 个会话；${result.skipped.length} 个会话因正在运行、已删除或状态变化而跳过。`);
      }
    },
  });

  const toggle = (id: string) => setSelectedIds(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const toggleVisible = () => setSelectedIds(current => {
    const next = new Set(current);
    if (selectAllVisible) visible.forEach(item => next.delete(item.id));
    else visible.forEach(item => next.add(item.id));
    return next;
  });

  return <section className="page conversation-management-page">
    <header className="conversation-management-header">
      <div><span className="eyebrow">AGENT CONVERSATION MANAGEMENT</span><h1>会话管理</h1><p>仅在你确认后删除已选择的会话。系统不会自动清理，运行中的会话会被安全跳过。</p></div>
      <div className="conversation-management-metrics"><span><MessageSquareText size={15}/>{conversations.data?.items.length ?? 0} 个会话</span><button className="secondary" disabled={conversations.isFetching} onClick={() => void conversations.refetch()}><RefreshCw size={14} className={conversations.isFetching ? 'spin' : ''}/>刷新</button></div>
    </header>
    <div className="conversation-management-notice"><ShieldCheck size={17}/><span>删除会永久移除会话、其专属附件和关联记录；共享工作区文件、工作目录和流程运行记录不会受影响。</span></div>
    {conversations.data?.truncated && <div className="conversation-management-notice warning"><Clock3 size={17}/><span>当前仅显示最近 500 个会话。请先完成本次清理，再继续管理更早的会话。</span></div>}
    <section className="conversation-management-panel">
      <div className="conversation-management-toolbar">
        <label>搜索会话<input value={query} onChange={event => setQuery(event.target.value)} placeholder="按会话标题搜索"/></label>
        <label>最后活动时间<PeriodDropdown value={period} onChange={setPeriod}/></label>
        <label className="conversation-management-check"><input type="checkbox" checked={includePinned} onChange={event => setIncludePinned(event.target.checked)}/><span>包含置顶会话</span></label>
        <label className="conversation-management-check"><input type="checkbox" checked={includeUnread} onChange={event => setIncludeUnread(event.target.checked)}/><span>包含未读会话</span></label>
      </div>
      {conversations.isLoading || workspace.isLoading ? <div className="empty">正在读取会话…</div> : conversations.isError || workspace.isError ? <div className="empty error">{conversations.error?.message ?? workspace.error?.message}</div> : <>
        <div className="conversation-management-selection"><label><input type="checkbox" aria-label="选择当前筛选结果中的全部会话" checked={selectAllVisible} ref={input => { if (input) input.indeterminate = !selectAllVisible && selectedVisible.length > 0; }} onChange={toggleVisible}/><CheckSquare size={15}/><span>选择当前结果</span></label><span>已选 {selectedVisible.length} / {visible.length} 个会话</span><button className="danger" disabled={selectedVisible.length === 0 || remove.isPending} onClick={() => setConfirming(true)}><Trash2 size={14}/>删除已选会话</button></div>
        <div className="conversation-management-table-wrap"><table className="conversation-management-table"><thead><tr><th>选择</th><th>会话</th><th>最后活动</th><th>创建时间</th><th>状态</th></tr></thead><tbody>{paged.map(item => <tr key={item.id}><td><input type="checkbox" aria-label={`选择会话 ${conversationTitle(item)}`} checked={selectedIds.has(item.id)} onChange={() => toggle(item.id)}/></td><td><b>{conversationTitle(item)}</b><small>{item.last_message_at ? '有消息记录' : '未发送消息'}</small></td><td>{formatTime(activityTime(item))}</td><td>{formatTime(item.created_at)}</td><td><span className="conversation-management-status">{item.pinned && '置顶'}{item.pinned && item.unread && ' · '}{item.unread && '未读'}{!item.pinned && !item.unread && '普通'}</span></td></tr>)}</tbody></table>{visible.length === 0 && <div className="empty compact"><ArchiveX size={25}/><b>没有符合当前条件的会话</b><span>调整筛选条件，或等待正在运行的会话完成。</span></div>}</div>
        <Pagination page={page} pageSize={PAGE_SIZE} total={visible.length} onPageChange={setPage}/>
      </>}
    </section>
    {confirming && <div className="modal-backdrop"><section className="modal conversation-management-confirm" role="dialog" aria-modal="true" aria-label="确认删除会话"><header><div><span className="eyebrow">PERMANENT DELETION</span><h2>删除 {selectedVisible.length} 个会话？</h2></div><button className="ghost" disabled={remove.isPending} onClick={() => setConfirming(false)}>取消</button></header><p>此操作不可恢复。系统会在删除时再次检查会话是否仍在运行；状态变化的会话将跳过，不会被强制中断。</p>{remove.isError && <p className="notice error">{remove.error.message}</p>}<footer><button className="secondary" disabled={remove.isPending} onClick={() => setConfirming(false)}>返回</button><button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}><Trash2 size={14}/>{remove.isPending ? '删除中…' : '确认永久删除'}</button></footer></section></div>}
  </section>;
}
