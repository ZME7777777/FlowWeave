import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitBranch, LoaderCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../../api/client';

export function AgentWorkspaceSettings({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient();
  const queryKey = ['agent-workspace-git-settings', workspaceId];
  const query = useQuery({ queryKey, queryFn: () => api.agentGitIdentity(workspaceId), refetchOnMount: 'always', refetchOnWindowFocus: false, retry: false });
  const [author, setAuthor] = useState('');
  const [email, setEmail] = useState('');
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (query.isFetching || !query.data) return;
    setAuthor(query.data.configured ? query.data.author : '');
    setEmail(query.data.configured ? query.data.email : '');
  }, [query.data, query.isFetching]);
  const save = useMutation({
    mutationFn: () => api.saveAgentGitIdentity(workspaceId, { author: author.trim(), email: email.trim() }),
    onSuccess: data => { client.setQueryData(queryKey, data); setSaved(data.configured); },
  });
  const busy = query.isFetching || save.isPending;
  return <section className="agent-workspace-settings" aria-label="工作区设置">
    <header><h2>设置</h2></header>
    <div className="agent-workspace-settings-layout">
      <nav aria-label="设置分类"><button type="button" aria-current="page"><GitBranch size={16}/>Git 配置</button></nav>
      <main>
        <h3>Git 配置</h3>
        <p>设置当前用户工作区及子目录中仓库的默认提交身份。仓库单独设置的身份优先使用。</p>
        {query.isFetching && <p role="status"><LoaderCircle size={14} className="conversation-activity-spin"/>正在读取工作区配置…</p>}
        {query.isError && <div role="alert"><p>无法读取 Git 配置，请重试。</p><button type="button" className="secondary" onClick={() => void query.refetch()}>重新读取</button></div>}
        <form onSubmit={event => { event.preventDefault(); if (!busy && !query.isError) save.mutate(); }}>
          <label>作者<input aria-label="Git 作者" value={author} maxLength={200} required disabled={busy || query.isError} onChange={event => { setAuthor(event.target.value); setSaved(false); }}/></label>
          <label>邮箱<input aria-label="Git 邮箱" type="email" value={email} maxLength={320} required disabled={busy || query.isError} onChange={event => { setEmail(event.target.value); setSaved(false); }}/></label>
          <footer><button type="submit" className="primary" disabled={busy || query.isError || !author.trim() || !email.trim()}>{save.isPending ? '正在保存…' : '保存'}</button>{saved && <span role="status">已保存并生效</span>}</footer>
          {save.isError && <p role="alert">保存失败：{save.error.message}</p>}
        </form>
      </main>
    </div>
  </section>;
}
