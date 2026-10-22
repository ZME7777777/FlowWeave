import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitBranch, LoaderCircle, Pencil, RefreshCw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../../api/client';

export function AgentWorkspaceSettings({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient();
  const queryKey = ['agent-workspace-git-settings', workspaceId];
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<{ author: string; email: string }>();
  const [saved, setSaved] = useState(false);
  const [draftChanged, setDraftChanged] = useState(false);
  const query = useQuery({ queryKey, queryFn: () => api.agentGitIdentity(workspaceId), refetchOnMount: 'always', refetchOnWindowFocus: !editing, refetchInterval: editing ? false : 5000, retry: false });
  const profile = useQuery({ queryKey: ['current-user-profile'], queryFn: api.currentUserProfile, enabled: editing && !draft, refetchOnWindowFocus: false, retry: false });
  useEffect(() => {
    if (!editing || draft || profile.isPending) return;
    setDraft({ author: profile.data?.display_name ?? '', email: profile.data?.email ?? '' });
  }, [draft, editing, profile.data, profile.isPending]);
  const save = useMutation({
    mutationFn: () => api.saveAgentGitIdentity(workspaceId, { author: draft!.author.trim(), email: draft!.email.trim() }),
    onSuccess: data => { client.setQueryData(queryKey, data); setEditing(false); setDraft(undefined); setDraftChanged(false); setSaved(data.configured); },
  });
  const busy = save.isPending || (editing && !draft);
  const actual = query.isFetchedAfterMount && !query.isError && query.data?.configured ? query.data : undefined;
  const values = editing ? draft : actual;
  const edit = () => {
    setSaved(false);
    save.reset();
    const legacyDefault = actual?.author === 'openhands' && actual.email === 'openhands@all-hands.dev';
    if (!draftChanged) setDraft(actual && !legacyDefault ? { author: actual.author, email: actual.email } : undefined);
    setEditing(true);
  };
  return <section className="agent-workspace-settings" aria-label="工作区设置">
    <header><h2>设置</h2></header>
    <div className="agent-workspace-settings-layout">
      <nav aria-label="设置分类"><button type="button" aria-current="page"><GitBranch size={16}/>Git 配置</button></nav>
      <main>
        <h3>Git 配置</h3>
        <p>查看当前用户根目录的实际 Git 身份；保存后也用于子目录仓库。子仓库自定义身份优先。</p>
        <div className="agent-git-settings-status"><b>{editing ? '编辑配置' : query.isFetching ? '正在读取目录' : query.isError ? '读取失败' : actual ? '已配置 · 目录实读' : '未配置'}</b>{!editing && <button type="button" className="ghost" disabled={query.isFetching} onClick={() => { setSaved(false); void query.refetch(); }}><RefreshCw size={14}/>重新读取</button>}</div>
        {query.isError && <p role="alert">无法读取 Git 配置，请重试。</p>}
        {busy && <p role="status"><LoaderCircle size={14} className="conversation-activity-spin"/>{save.isPending ? '正在保存并验证目录配置…' : '正在读取账户资料…'}</p>}
        <form onSubmit={event => { event.preventDefault(); if (editing && draft && !busy) save.mutate(); }}>
          <label>作者<input aria-label="Git 作者" value={values?.author ?? ''} maxLength={200} required readOnly={!editing} disabled={busy} placeholder={!editing ? '目录未配置作者' : '填写作者姓名'} onChange={event => { setDraftChanged(true); setDraft(current => ({ author: event.target.value, email: current?.email ?? '' })); setSaved(false); }}/></label>
          <label>邮箱<input aria-label="Git 邮箱" type="email" value={values?.email ?? ''} maxLength={320} required readOnly={!editing} disabled={busy} placeholder={!editing ? '目录未配置邮箱' : '填写邮箱'} onChange={event => { setDraftChanged(true); setDraft(current => ({ author: current?.author ?? '', email: event.target.value })); setSaved(false); }}/></label>
          <footer>{editing ? <><button type="button" className="secondary" disabled={busy} onClick={() => { setEditing(false); save.reset(); }}>取消</button><button type="submit" className="primary" disabled={busy || !draft?.author.trim() || !draft?.email.trim()}>保存</button></> : <button type="button" className="primary" disabled={query.isFetching || query.isError} onClick={edit}><Pencil size={14}/>编辑</button>}{saved && <span role="status">已保存并从目录确认</span>}</footer>
          {editing && save.isError && <p role="alert">保存失败：{save.error.message}</p>}
          {editing && profile.isError && !actual && <p>未能读取账户资料，可手动填写。</p>}
        </form>
      </main>
    </div>
  </section>;
}
