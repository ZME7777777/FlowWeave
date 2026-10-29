import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Archive, ArchiveRestore, ArchiveX, Clock3, Folder, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { LaunchOptionMenu } from '../components/LaunchOptionMenu';
import { useProductDialog } from '../components/ProductDialogContext';
import { Pagination } from '../components/Pagination';
import './conversation-management.css';
import { api } from '../api/client';
import type { AgentConversationManagementArchiveResult, AgentConversationManagementDeleteResult, AgentConversationManagementPage, ManagedAgentConversation } from '../types';

const DAY = 24 * 60 * 60 * 1000;
const PAGE_SIZE = 20;
const DELETE_BATCH_SIZE = 100;
const ARCHIVE_BATCH_SIZE = 20;

interface ConversationSelection {
  workspaceId: string;
  workDirectoryId?: string;
  bindingIds: string[];
}

type InactivePeriod = 'all' | '7' | '30';
type ArchiveFilter = 'all' | 'active' | 'archived';

const PERIOD_OPTIONS: { value: InactivePeriod; label: string }[] = [
  { value: 'all', label: '全部时间' },
  { value: '7', label: '7 天前' },
  { value: '30', label: '30 天前' },
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

export function ConversationManagementPage({ onRestored, onArchived, onDirectoryDeleted }: { onRestored?: (selection: { bindingId?: string; workDirectoryId?: string }) => void; onArchived?: (selection: { workspaceId: string; workDirectoryId?: string; bindingIds: string[] }) => void; onDirectoryDeleted?: (selection: { workspaceId: string; workDirectoryId: string; bindingIds: string[] }) => void } = {}) {
  const client = useQueryClient();
  const dialog = useProductDialog();
  const [query, setQuery] = useState('');
  const [period, setPeriod] = useState<InactivePeriod>('all');
  const [archiveFilter, setArchiveFilter] = useState<ArchiveFilter>('all');
  const [includePinned, setIncludePinned] = useState(false);
  const [includeUnread, setIncludeUnread] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [archivePreparing, setArchivePreparing] = useState(false);
  const [archiveProgress, setArchiveProgress] = useState({ processed: 0, total: 0 });
  const [archiveFeedback, setArchiveFeedback] = useState<{ message: string; error?: boolean }>();
  const [page, setPage] = useState(1);
  const [selectedWorkDirectoryId, setSelectedWorkDirectoryId] = useState<string | undefined>();
  const tableRef = useRef<HTMLDivElement>(null);
  const [deletionProgress, setDeletionProgress] = useState({ processed: 0, total: 0 });

  const workspace = useQuery({ queryKey: ['agent-workspace', 'default'], queryFn: ({ signal }) => api.defaultAgentWorkspace(signal) });
  const workDirectories = useQuery({
    queryKey: ['agent-work-directories', workspace.data?.id, 'including-archived'],
    queryFn: () => api.agentWorkDirectories(workspace.data!.id, true),
    enabled: Boolean(workspace.data?.id),
  });
  const selectedDirectory = workDirectories.data?.items.find(item => item.id === selectedWorkDirectoryId);
  const selectedWorkspaceLabel = selectedWorkDirectoryId
    ? selectedDirectory?.display_name ?? '工作区'
    : workDirectories.data?.root.display_name ?? '根工作区';
  const conversations = useQuery({
    queryKey: ['agent-conversation-management', workspace.data?.id, selectedWorkDirectoryId ?? 'root', archiveFilter],
    queryFn: () => api.agentConversationManagement(workspace.data!.id, selectedWorkDirectoryId, archiveFilter === 'all' ? undefined : archiveFilter === 'archived'),
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
    setArchiveFeedback(undefined);
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
  useEffect(() => { setPage(1); }, [query, period, archiveFilter, includePinned, includeUnread]);

  const currentPage = Math.min(page, Math.max(1, Math.ceil(visible.length / PAGE_SIZE)));
  const paged = useMemo(() => visible.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE), [visible, currentPage]);

  useEffect(() => { if (tableRef.current) tableRef.current.scrollTop = 0; }, [currentPage, selectedWorkDirectoryId, query, period, archiveFilter, includePinned, includeUnread]);

  const visibleIds = useMemo(() => new Set(visible.map(item => item.id)), [visible]);
  const selectedVisible = visible.filter(item => selectedIds.has(item.id));
  const selectAllVisible = visible.length > 0 && visible.every(item => selectedIds.has(item.id));

  useEffect(() => {
    if (!conversations.data) return;
    setSelectedIds(current => new Set([...current].filter(id => visibleIds.has(id))));
  }, [visibleIds, conversations.data]);

  const remove = useMutation({
    retry: false,
    onMutate: selection => setDeletionProgress({ processed: 0, total: selection.bindingIds.length }),
    mutationFn: async (selection: ConversationSelection) => {
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
        client.setQueriesData<AgentConversationManagementPage>(
          { queryKey: ['agent-conversation-management', selection.workspaceId, selection.workDirectoryId ?? 'root'] },
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

  const archive = useMutation({
    retry: false,
    onMutate: (selection: ConversationSelection) => { setArchiveFeedback(undefined); setArchiveProgress({ processed: 0, total: selection.bindingIds.length }); },
    mutationFn: async (selection: ConversationSelection) => {
      const result: AgentConversationManagementArchiveResult = { archived: [], skipped: [] };
      for (let offset = 0; offset < selection.bindingIds.length; offset += ARCHIVE_BATCH_SIZE) {
        const batch = selection.bindingIds.slice(offset, offset + ARCHIVE_BATCH_SIZE);
        let response: AgentConversationManagementArchiveResult;
        try { response = await api.archiveManagedAgentConversations(selection.workspaceId, batch, selection.workDirectoryId); }
        catch (error) { throw new Error(`已归档 ${result.archived.length} 个，跳过 ${result.skipped.length} 个；本批请求失败，未完成的会话保留选中，可重试。${error instanceof Error ? error.message : ''}`); }
        result.archived.push(...response.archived); result.skipped.push(...response.skipped);
        const archived = new Map(response.archived.map(item => [item.id, item.archived_at]));
        const prefix = ['agent-conversation-management', selection.workspaceId, selection.workDirectoryId ?? 'root'];
        await client.cancelQueries({ queryKey: prefix });
        for (const cached of client.getQueryCache().findAll({ queryKey: prefix })) {
          const state = cached.queryKey[3];
          client.setQueryData<AgentConversationManagementPage>(cached.queryKey, current => current && ({
            ...current, items: current.items.flatMap(item => {
              const archivedAt = archived.get(item.id);
              return !archivedAt ? [item] : state === 'active' ? [] : [{ ...item, archived_at: archivedAt, updated_at: archivedAt }];
            }),
          }));
        }
        setSelectedIds(current => new Set([...current].filter(id => !archived.has(id))));
        if (archived.size) onArchived?.({ workspaceId: selection.workspaceId, workDirectoryId: selection.workDirectoryId, bindingIds: [...archived.keys()] });
        setArchiveProgress({ processed: offset + batch.length, total: selection.bindingIds.length });
      }
      return result;
    },
    onSuccess: result => setArchiveFeedback({ message: `已归档 ${result.archived.length} 个会话。${result.skipped.length ? `${result.skipped.length} 个因运行中或状态变化未归档，已保留选择，可稍后重试。` : ''}` }),
    onError: error => setArchiveFeedback({ message: error.message, error: true }),
    onSettled: (_result, _error, selection) => Promise.all([
      client.invalidateQueries({ queryKey: ['agent-conversation-management', selection.workspaceId] }),
      client.invalidateQueries({ queryKey: ['agent-session'] }),
    ]),
  });

  const restore = useMutation({
    mutationFn: (bindingId: string) => api.restoreAgentConversation(workspace.data!.id, bindingId),
    onSuccess: async (conversation, bindingId) => {
      onRestored?.({ bindingId, workDirectoryId: conversation.work_directory_id ?? selectedWorkDirectoryId });
      await Promise.all([
        client.invalidateQueries({ queryKey: ['agent-conversation-management', workspace.data?.id] }),
        client.invalidateQueries({ queryKey: ['agent-work-directories', workspace.data?.id] }),
        client.invalidateQueries({ queryKey: ['agent-session'] }),
      ]);
    },
  });
  const restoreDirectory = useMutation({
    mutationFn: (directoryId: string) => api.restoreAgentWorkDirectory(workspace.data!.id, directoryId),
    onSuccess: async (_directory, workDirectoryId) => {
      onRestored?.({ workDirectoryId });
      await Promise.all([
        client.invalidateQueries({ queryKey: ['agent-work-directories', workspace.data?.id] }),
        client.invalidateQueries({ queryKey: ['agent-conversation-management', workspace.data?.id] }),
        client.invalidateQueries({ queryKey: ['agent-session'] }),
      ]);
    },
  });

  const [directoryDeletePreparing, setDirectoryDeletePreparing] = useState(false);
  const deleteDirectory = useMutation({
    retry: false,
    mutationFn: (selection: { workspaceId: string; workDirectoryId: string; bindingIds: string[] }) => api.deleteAgentWorkDirectory(selection.workspaceId, selection.workDirectoryId),
    onSuccess: async (_result, selection) => {
      onDirectoryDeleted?.(selection);
      await Promise.all([
        client.cancelQueries({ queryKey: ['agent-work-directories', selection.workspaceId] }),
        client.cancelQueries({ queryKey: ['agent-conversation-management', selection.workspaceId] }),
      ]);
      client.setQueriesData<import('../types').AgentWorkDirectoryList>(
        { queryKey: ['agent-work-directories', selection.workspaceId] },
        current => current && ({ ...current, items: current.items.filter(item => item.id !== selection.workDirectoryId) }),
      );
      client.removeQueries({ queryKey: ['agent-conversation-management', selection.workspaceId, selection.workDirectoryId] });
      setSelectedWorkDirectoryId(undefined);
      setSelectedIds(new Set());
      setConfirming(false);
      await Promise.all([
        client.invalidateQueries({ queryKey: ['agent-work-directories', selection.workspaceId] }),
        client.invalidateQueries({ queryKey: ['agent-conversation-management', selection.workspaceId] }),
        client.invalidateQueries({ queryKey: ['agent-session'] }),
      ]);
    },
  });
  const requestDirectoryDelete = async () => {
    if (!workspace.data || !selectedDirectory) return;
    const directory = selectedDirectory;
    const selection = { workspaceId: workspace.data.id, workDirectoryId: directory.id, bindingIds: conversations.data?.items.map(item => item.id) ?? [] };
    setDirectoryDeletePreparing(true);
    deleteDirectory.reset();
    try {
      if (await dialog.confirm({ title: `删除工作区“${directory.display_name}”？`, message: '将永久删除此工作区及所有版本目录中的文件、其中全部会话与上传附件，无法恢复。', confirmLabel: '确认删除工作区', tone: 'danger' })) deleteDirectory.mutate(selection);
    } finally { setDirectoryDeletePreparing(false); }
  };

  const operationBusy = directoryDeletePreparing || deleteDirectory.isPending || remove.isPending || archive.isPending || archivePreparing || restore.isPending || restoreDirectory.isPending;
  const archiveCandidates = selectedVisible.filter(item => !item.archived_at);
  const requestArchive = async () => {
    if (!workspace.data || operationBusy || !archiveCandidates.length) return;
    const selection = { workspaceId: workspace.data.id, workDirectoryId: selectedWorkDirectoryId, bindingIds: archiveCandidates.map(item => item.id) };
    setArchivePreparing(true);
    try {
      if (await dialog.confirm({ title: `归档 ${selection.bindingIds.length} 个会话？`, message: '仅归档所选会话，工作区保持不变。运行中的会话会跳过，归档后可在“已归档”中恢复。', confirmLabel: '确认归档' })) archive.mutate(selection);
    } finally { setArchivePreparing(false); }
  };

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

  const filtersActive = Boolean(query || period !== 'all' || archiveFilter !== 'all' || includePinned || includeUnread);
  const loading = conversations.isLoading || workspace.isLoading || workDirectories.isLoading;
  const readError = conversations.error ?? workspace.error ?? workDirectories.error;

  return <section className="page conversation-management-page compact-conversation-management">
    <section className="conversation-management-layout">
      <aside className="conversation-management-workspaces" aria-label="Agent 工作区">
        <header><Folder size={15}/><b>工作区</b></header>
        <div className="conversation-management-workspace-list">
          <button type="button" className={!selectedWorkDirectoryId ? 'active' : ''} aria-pressed={!selectedWorkDirectoryId} disabled={operationBusy} onClick={() => setSelectedWorkDirectoryId(undefined)}>
            <Folder size={16}/><span>{workDirectories.data?.root.display_name ?? '根工作区'}</span>
          </button>
          {workDirectories.data?.items.map(directory => <div key={directory.id} className={`conversation-management-workspace-item${directory.state === 'ARCHIVED' ? ' archived' : ''}`}>
            <button type="button" className={selectedWorkDirectoryId === directory.id ? 'active' : ''} aria-pressed={selectedWorkDirectoryId === directory.id} aria-description={directory.state === 'ARCHIVED' ? '已归档工作区' : undefined} disabled={operationBusy} onClick={() => setSelectedWorkDirectoryId(directory.id)}>
              <Folder size={16}/><span title={directory.display_name}>{directory.display_name}</span>
            </button>
          </div>)}
        </div>
      </aside>
      <section className="conversation-management-panel">
        <header className="conversation-management-header">
          <div><h1>会话管理</h1><span className="conversation-management-directory-name" title={selectedWorkspaceLabel}>{selectedWorkspaceLabel}</span><span className="conversation-management-count">{conversations.data?.items.length ?? 0} 个会话</span></div>
          <button className="ghost" disabled={conversations.isFetching || operationBusy} onClick={() => void conversations.refetch()}><RefreshCw size={14} className={conversations.isFetching ? 'spin' : ''}/>刷新</button>
        </header>
        <div className="conversation-management-toolbar">
          <label className="conversation-management-search"><Search size={14}/><input aria-label="搜索会话" disabled={operationBusy} value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索会话名称…"/>{query && <button type="button" aria-label="清除搜索" disabled={operationBusy} onClick={() => setQuery('')}><X size={13}/></button>}</label>
          <LaunchOptionMenu disabled={operationBusy} label="会话状态" value={archiveFilter} options={[{ value: 'all', label: '全部' }, { value: 'active', label: '未归档' }, { value: 'archived', label: '已归档' }]} onChange={value => setArchiveFilter(value as ArchiveFilter)}/>
          <LaunchOptionMenu disabled={operationBusy} label="最后活动时间" value={period} options={PERIOD_OPTIONS} onChange={value => setPeriod(value as InactivePeriod)}/>
          <label className="conversation-management-check"><input type="checkbox" disabled={operationBusy} checked={includePinned} onChange={event => setIncludePinned(event.target.checked)}/><span>包含置顶</span></label>
          <label className="conversation-management-check"><input type="checkbox" disabled={operationBusy} checked={includeUnread} onChange={event => setIncludeUnread(event.target.checked)}/><span>包含未读</span></label>
          {filtersActive && <button className="ghost conversation-management-reset" disabled={operationBusy} onClick={() => { setQuery(''); setPeriod('all'); setArchiveFilter('all'); setIncludePinned(false); setIncludeUnread(false); }}>重置</button>}
          <div className="conversation-management-actions" role="group" aria-label="工作区与会话操作">
            {selectedDirectory?.state === 'ARCHIVED' && <button type="button" className="secondary conversation-management-restore" aria-label={`恢复工作区 ${selectedDirectory.display_name}`} disabled={operationBusy} onClick={async () => {
              const directory = selectedDirectory;
              if (await dialog.confirm({ title: `恢复工作区“${directory.display_name}”？`, message: '将恢复此工作区及其中所有已归档会话，恢复后会重新显示在工作台列表中。', confirmLabel: '确认恢复' })) restoreDirectory.mutate(directory.id);
            }}><ArchiveRestore size={14}/>{restoreDirectory.isPending ? '恢复中…' : '恢复工作区'}</button>}
            {selectedDirectory?.state === 'ARCHIVED' && <button type="button" className="danger conversation-management-delete" aria-label={`删除工作区 ${selectedDirectory.display_name}`} disabled={operationBusy || loading || Boolean(readError)} onClick={() => void requestDirectoryDelete()}><Trash2 size={14}/>{deleteDirectory.isPending ? '删除工作区中…' : '删除工作区'}</button>}
            <button type="button" className="secondary conversation-management-archive" disabled={!archiveCandidates.length || operationBusy || loading || Boolean(readError)} onClick={() => void requestArchive()}><Archive size={14}/>{archive.isPending ? `归档中 ${archiveProgress.processed}/${archiveProgress.total}` : '归档已选会话'}</button>
            <button className="danger conversation-management-delete" disabled={selectedVisible.length === 0 || operationBusy || loading || Boolean(readError)} onClick={() => { remove.reset(); setConfirming(true); }}><Trash2 size={14}/>删除已选会话</button>
          </div>
        </div>
        {deleteDirectory.error && <div className="conversation-management-notice warning" role="alert">{deleteDirectory.error.message}</div>}
        {archive.isPending && <div className="conversation-management-notice" role="status">正在归档会话：{archiveProgress.processed} / {archiveProgress.total}</div>}
        {archiveFeedback && !archive.isPending && <div className={`conversation-management-notice${archiveFeedback.error ? ' warning' : ''}`} role={archiveFeedback.error ? 'alert' : 'status'}>{archiveFeedback.message}</div>}
        {conversations.data?.truncated && <div className="conversation-management-notice warning"><Clock3 size={14}/><span>当前仅显示最近 500 个会话。请先完成本次清理，再继续管理更早的会话。</span></div>}
        {(restore.isError || restoreDirectory.isError) && <div className="conversation-management-notice warning" role="alert">{restore.error?.message ?? restoreDirectory.error?.message}</div>}
        {loading ? <div className="empty">正在读取会话…</div> : readError ? <div className="empty error" role="alert"><span>{readError.message}</span><button className="secondary" onClick={() => void Promise.all([workspace.refetch(), workDirectories.refetch(), conversations.refetch()])}>重试</button></div> : <>
          <div className="conversation-management-table-wrap" ref={tableRef}><table className="conversation-management-table"><thead><tr><th scope="col"><input type="checkbox" aria-label="选择当前筛选结果中的全部会话" title="选择全部匹配结果（含其他页）" checked={selectAllVisible} disabled={!visible.length || operationBusy} ref={input => { if (input) input.indeterminate = !selectAllVisible && selectedVisible.length > 0; }} onChange={toggleVisible}/></th><th scope="col">会话名称</th><th scope="col">最后活动</th><th scope="col">创建时间</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead><tbody>{paged.map(item => <tr key={item.id} className={selectedIds.has(item.id) ? 'selected' : ''} onClick={event => { if (!operationBusy && !(event.target as HTMLElement).closest('button, input, label, a')) toggle(item.id); }}><td><input type="checkbox" aria-label={`选择会话 ${conversationTitle(item)}`} checked={selectedIds.has(item.id)} disabled={operationBusy} onChange={() => toggle(item.id)}/></td><td><b title={conversationTitle(item)}>{conversationTitle(item)}</b></td><td>{formatTime(activityTime(item))}</td><td>{formatTime(item.created_at)}</td><td><span className="conversation-management-status">{item.archived_at ? '已归档' : item.pinned ? '置顶' : item.unread ? '未读' : '普通'}</span></td><td>{item.archived_at && <button type="button" className="secondary" aria-label={`恢复会话 ${conversationTitle(item)}`} disabled={operationBusy} onClick={() => restore.mutate(item.id)}><ArchiveRestore size={14}/>恢复</button>}</td></tr>)}</tbody></table>{visible.length === 0 && <div className="empty compact"><ArchiveX size={25}/><b>没有符合当前条件的会话</b><span>调整筛选条件，或等待正在运行的会话完成。</span></div>}</div>
          <footer className="conversation-management-footer"><span aria-live="polite">已选 {selectedVisible.length} / {visible.length} 个会话</span><Pagination page={currentPage} pageSize={PAGE_SIZE} total={visible.length} onPageChange={setPage}/></footer>
        </>}
      </section>
    </section>
    {confirming && <div className="modal-backdrop"><section className="modal conversation-management-confirm" role="dialog" aria-modal="true" aria-label="确认删除会话"><header><div><span className="eyebrow">PERMANENT DELETION</span><h2>删除 {remove.isPending ? deletionProgress.total : selectedVisible.length} 个会话？</h2></div><button className="ghost" disabled={operationBusy} onClick={() => setConfirming(false)}>取消</button></header><p>此操作不可恢复。系统会在删除时再次检查会话是否仍在运行；状态变化的会话将跳过，不会被强制中断。</p>{remove.isPending && <p role="status">已处理 {deletionProgress.processed} / {deletionProgress.total} 个会话…</p>}{remove.isError && <p role="alert" className="notice error">{remove.error.message}</p>}<footer><button className="secondary" disabled={operationBusy} onClick={() => setConfirming(false)}>返回</button><button className="danger" disabled={remove.isPending || selectedVisible.length === 0} onClick={() => remove.mutate({ workspaceId: workspace.data!.id, workDirectoryId: selectedWorkDirectoryId, bindingIds: selectedVisible.map(item => item.id) })}><Trash2 size={14}/>{remove.isPending ? '删除中…' : '确认永久删除'}</button></footer></section></div>}
  </section>;
}
