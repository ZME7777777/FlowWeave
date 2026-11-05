import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitBranch, LoaderCircle, RefreshCw } from 'lucide-react';
import { api } from '../../api/client';

export function AgentWorkspaceSettings({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient();
  const queryKey = ['agent-workspace-git-settings', workspaceId];
  const query = useQuery({ queryKey, queryFn: () => api.agentGitIdentity(workspaceId), refetchOnMount: 'always', refetchOnWindowFocus: true, retry: false });
  const synchronize = useMutation({
    mutationFn: () => api.synchronizeAgentGitIdentity(workspaceId),
    onSuccess: data => client.setQueryData(queryKey, data),
  });
  const actual = query.data?.configured ? query.data : undefined;
  const busy = query.isFetching || synchronize.isPending;
  return <section className="agent-workspace-settings" aria-label="工作区设置">
    <header><h2>设置</h2></header>
    <div className="agent-workspace-settings-layout">
      <nav aria-label="设置分类"><button type="button" aria-current="page"><GitBranch size={16}/>Git 配置</button></nav>
      <main>
        <h3>Git 配置</h3>
        <p>显示当前用户根目录的实际 Git 默认身份。该身份会用于根目录下的 Git 子仓库；子仓库自身配置优先。</p>
        <div className="agent-git-settings-status"><b>{query.isFetching ? '正在读取目录' : query.isError ? '读取失败' : actual ? '已配置 · 目录实读' : '未配置'}</b><button type="button" className="ghost" disabled={busy} onClick={() => void query.refetch()}><RefreshCw size={14}/>重新读取</button></div>
        {query.isError && <p role="alert">无法读取 Git 配置，请重试。</p>}
        {synchronize.isPending && <p role="status"><LoaderCircle size={14} className="conversation-activity-spin"/>正在同步当前用户身份并验证目录配置…</p>}
        <label>作者<input aria-label="Git 作者" value={actual?.author ?? ''} readOnly placeholder="目录未配置作者"/></label>
        <label>邮箱<input aria-label="Git 邮箱" value={actual?.email ?? ''} readOnly placeholder="目录未配置邮箱"/></label>
        <footer>{!actual && !query.isError && <button type="button" className="primary" disabled={busy} onClick={() => synchronize.mutate()}><GitBranch size={14}/>同步当前用户身份</button>}{synchronize.isSuccess && actual && <span role="status">已同步并从目录确认</span>}</footer>
        {synchronize.isError && <p role="alert">同步失败：{synchronize.error.message}</p>}
      </main>
    </div>
  </section>;
}
