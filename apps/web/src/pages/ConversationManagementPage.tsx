import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArchiveX, CheckSquare, ChevronDown, Clock3, Folder, MessageSquareText, RefreshCw, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Pagination } from '../components/Pagination';
import { api } from '../api/client';
import type { AgentConversationManagementDeleteResult, AgentConversationManagementPage, ManagedAgentConversation } from '../types';

const DAY = 24 * 60 * 60 * 1000;
const PAGE_SIZE = 20;
const DELETE_BATCH_SIZE = 100;

interface DeleteSelection {
  workspaceId: string;
  workDirectoryId?: string;
  bindingIds: string[];
}

type InactivePeriod = 'all' | '30' | '90';

const PERIOD_OPTIONS: { value: InactivePeriod; label: string }[] = [
  { value: 'all', label: '全部时间' },
  { value: '30', label: '30 天前' },
  { value: '90', label: '90 天前' },
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
  const [period, setPeriod] = useState<InactivePeriod>('all');
  const [includePinned, setIncludePinned] = useState(false);
  const [includeUnread, setIncludeUnread] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [page, setPage] = useState(1);
  const [selectedWorkDirectoryId, setSelectedWorkDirectoryId] = useState<string | undefined>();
  const [deletionProgress, setDeletionProgress] = useState({ processed: 0, total: 0 });

  const workspace = useQuery({ queryKey: ['agent-workspace', 'default'], queryFn: ({ signal }) => api.defaultAgentWorkspace(signal) });
  const workDirectories = useQuery({
    queryKey: ['agent-work-directories', workspace.data?.id],
    queryFn: () => api.agentWorkDirectories(workspace.data!.id),
    enabled: Boolean(workspace.data?.id),
  });
  const selectedWorkspaceLabel = selectedWorkDirectoryId
    ? workDirectories.data?.items.find(item => item.id === selectedWorkDirectoryId)?.display_name ?? '工作区'
    : workDirectories.data?.root.display_name ?? '根工作区';
  const conversations = useQuery({
    queryKey: ['agent-conversation-management', workspace.data?.id, selectedWorkDirectoryId ?? 'root'],
    queryFn: () => api.agentConversationManagement(workspace.data!.id, selectedWorkDirectoryId),
    enabled: Boolean(workspace.data?.id),
  });

  useEffect(() => {
    if (selectedWorkDirectoryId && !workDirectories.data?.items.some(item => item.id === selectedWorkDirectoryId)) {
      setSelectedWorkDirectoryId(undefined);
    }
  }, [selectedWorkDirectoryId, workDirectories.data?.items]);

  useEffect(() => {
    setSelectedIds(new Set());
    setConfirming(false);
    setPage(1);
  }, [selectedWorkDirectoryId]);

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

  const currentPage = Math.min(page, Math.max(1, Math.ceil(visible.length / PAGE_SIZE)));
  const paged = useMemo(() => visible.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE), [visible, currentPage]);

  const visibleIds = useMemo(() => new Set(visible.map(item => item.id)), [visible]);
  const selectedVisible = visible.filter(item => selectedIds.has(item.id));
  const selectAllVisible = visible.length > 0 && visible.every(item => selectedIds.has(item.id));

  useEffect(() => {
    setSelectedIds(current => new Set([...current].filter(id => visibleIds.has(id))));
  }, [visibleIds]);

  const remove = useMutation({
    retry: false,
    onMutate: selection => setDeletionProgress({ processed: 0, total: selection.bindingIds.length }),
    mutationFn: async (selection: DeleteSelection) => {
      const result: AgentConversationManagementDeleteResult = { deleted_ids: [], skipped: [] };
      for (let offset = 0; offset < selection.bindingIds.length; offset += DELETE_BATCH_SIZE) {
        const batch = selection.bindingIds.slice(offset, offset + DELETE_BATCH_SIZE);
        let response: AgentConversationManagementDeleteResult;
        try {
          response = await api.deleteManagedAgentConversations(selection.workspaceId, batch, selection.workDirectoryId);
        } catch (error) {
          const message = error instanceof Error ? error.message : '删除请求失败，请稍后重试。';
          throw new Error(`已删除 ${result.deleted_ids.length} 个，跳过 ${result.skipped.length} 个；本批请求失败，未完成的会话仍保留选中。${message}`);
        }
        result.deleted_ids.push(...response.deleted_ids);
        result.skipped.push(...response.skipped);
        const deleted = new Set(response.deleted_ids);
        const processed = new Set([...response.deleted_ids, ...response.skipped.map(item => item.id)]);
        client.setQueryData<AgentConversationManagementPage>(
          ['agent-conversation-management', selection.workspaceId, selection.workDirectoryId ?? 'root'],
          current => current ? { ...current, items: current.items.filter(item => !deleted.has(item.id)) } : current,
        );
        setSelectedIds(current => new Set([...current].filter(id => !processed.has(id))));
        setDeletionProgress({ processed: offset + batch.length, total: selection.bindingIds.length });
      }
      return result;
    },
    onSuccess: result => {
      setSelectedIds(new Set());
      setConfirming(false);
      if (result.skipped.length) {
        window.alert(`已删除 ${result.deleted_ids.length} 个会话；${result.skipped.length} 个会话因正在运行、已删除或状态变化而跳过。`);
      }
    },
    onSettled: (_result, _error, selection) => Promise.all([
      client.invalidateQueries({ queryKey: ['agent-conversation-management', selection.workspaceId] }),
      client.invalidateQueries({ queryKey: ['agent-session'] }),
    ]),
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
      <h1>会话管理</h1>
      <div className="conversation-management-metrics"><span><MessageSquareText size={15}/>{conversations.data?.items.length ?? 0} 个会话</span><button className="secondary" disabled={conversations.isFetching || remove.isPending} onClick={() => void conversations.refetch()}><RefreshCw size={14} className={conversations.isFetching ? 'spin' : ''}/>刷新</button></div>
    </header>
    {conversations.data?.truncated && <div className="conversation-management-notice warning"><Clock3 size={17}/><span>当前仅显示最近 500 个会话。请先完成本次清理，再继续管理更早的会话。</span></div>}
    <section className="conversation-management-layout">
      <aside className="conversation-management-workspaces" aria-label="Agent 工作区">
        <header><b>工作区</b></header>
        <div className="conversation-management-workspace-list">
          <button type="button" className={!selectedWorkDirectoryId ? 'active' : ''} aria-pressed={!selectedWorkDirectoryId} disabled={remove.isPending} onClick={() => setSelectedWorkDirectoryId(undefined)}>
            <Folder size={16}/><span>{workDirectories.data?.root.display_name ?? '根工作区'}</span>
          </button>
          {workDirectories.data?.items.map(directory => <button key={directory.id} type="button" className={selectedWorkDirectoryId === directory.id ? 'active' : ''} aria-pressed={selectedWorkDirectoryId === directory.id} disabled={remove.isPending} onClick={() => setSelectedWorkDirectoryId(directory.id)}>
            <Folder size={16}/><span>{directory.display_name}</span>
          </button>)}
        </div>
      </aside>
      <section className="conversation-management-panel">
      <div className="conversation-management-toolbar">
        <label>搜索会话<input value={query} onChange={event => setQuery(event.target.value)} placeholder={`搜索 ${selectedWorkspaceLabel} 会话`}/></label>
        <label>最后活动时间<PeriodDropdown value={period} onChange={setPeriod}/></label>
        <label className="conversation-management-check"><input type="checkbox" checked={includePinned} onChange={event => setIncludePinned(event.target.checked)}/><span>置顶</span></label>
        <label className="conversation-management-check"><input type="checkbox" checked={includeUnread} onChange={event => setIncludeUnread(event.target.checked)}/><span>未读</span></label>
      </div>
      {conversations.isLoading || workspace.isLoading || workDirectories.isLoading ? <div className="empty">正在读取会话…</div> : conversations.isError || workspace.isError || workDirectories.isError ? <div className="empty error">{conversations.error?.message ?? workspace.error?.message ?? workDirectories.error?.message}</div> : <>
        <div className="conversation-management-selection"><label><input type="checkbox" aria-label="选择当前筛选结果中的全部会话" checked={selectAllVisible} ref={input => { if (input) input.indeterminate = !selectAllVisible && selectedVisible.length > 0; }} onChange={toggleVisible}/><CheckSquare size={15}/><span>选择当前结果</span></label><span>已选 {selectedVisible.length} / {visible.length} 个会话</span><button className="danger" disabled={selectedVisible.length === 0 || remove.isPending} onClick={() => { remove.reset(); setConfirming(true); }}><Trash2 size={14}/>删除已选会话</button></div>
        <div className="conversation-management-table-wrap"><table className="conversation-management-table"><thead><tr><th>选择</th><th>会话</th><th>最后活动</th><th>创建时间</th><th>状态</th></tr></thead><tbody>{paged.map(item => <tr key={item.id}><td><input type="checkbox" aria-label={`选择会话 ${conversationTitle(item)}`} checked={selectedIds.has(item.id)} onChange={() => toggle(item.id)}/></td><td><b>{conversationTitle(item)}</b></td><td>{formatTime(activityTime(item))}</td><td>{formatTime(item.created_at)}</td><td><span className="conversation-management-status">{item.pinned && '置顶'}{item.pinned && item.unread && ' · '}{item.unread && '未读'}{!item.pinned && !item.unread && '普通'}</span></td></tr>)}</tbody></table>{visible.length === 0 && <div className="empty compact"><ArchiveX size={25}/><b>没有符合当前条件的会话</b><span>调整筛选条件，或等待正在运行的会话完成。</span></div>}</div>
        <Pagination page={currentPage} pageSize={PAGE_SIZE} total={visible.length} onPageChange={setPage}/>
      </>}
      </section>
    </section>
    {confirming && <div className="modal-backdrop"><section className="modal conversation-management-confirm" role="dialog" aria-modal="true" aria-label="确认删除会话"><header><div><span className="eyebrow">PERMANENT DELETION</span><h2>删除 {remove.isPending ? deletionProgress.total : selectedVisible.length} 个会话？</h2></div><button className="ghost" disabled={remove.isPending} onClick={() => setConfirming(false)}>取消</button></header><p>此操作不可恢复。系统会在删除时再次检查会话是否仍在运行；状态变化的会话将跳过，不会被强制中断。</p>{remove.isPending && <p role="status">已处理 {deletionProgress.processed} / {deletionProgress.total} 个会话…</p>}{remove.isError && <p role="alert" className="notice error">{remove.error.message}</p>}<footer><button className="secondary" disabled={remove.isPending} onClick={() => setConfirming(false)}>返回</button><button className="danger" disabled={remove.isPending || selectedVisible.length === 0} onClick={() => remove.mutate({ workspaceId: workspace.data!.id, workDirectoryId: selectedWorkDirectoryId, bindingIds: selectedVisible.map(item => item.id) })}><Trash2 size={14}/>{remove.isPending ? '删除中…' : '确认永久删除'}</button></footer></section></div>}
  </section>;
}
