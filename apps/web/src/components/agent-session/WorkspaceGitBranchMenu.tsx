import { useMutation, useQuery } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import { LaunchOptionMenu } from '../LaunchOptionMenu';
import type { WorkspaceGitBranches, WorkspaceGitRepository, WorkspaceGitSwitchWrite } from '../../types';

export function WorkspaceGitBranchMenu({ repository, queryScope, disabled, loadBranches, switchBranch, onSwitched }: {
  repository: WorkspaceGitRepository;
  queryScope: readonly unknown[];
  disabled: boolean;
  loadBranches: () => Promise<WorkspaceGitBranches>;
  switchBranch: (body: WorkspaceGitSwitchWrite) => Promise<WorkspaceGitRepository>;
  onSwitched: () => Promise<void>;
}) {
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const branches = useQuery({ queryKey: [...queryScope, 'branches'], queryFn: loadBranches, staleTime: 0 });
  const switching = useMutation({
    mutationFn: async (ref: string) => {
      const current = branches.data?.repository;
      if (!current?.head) throw new Error('请先刷新分支列表');
      await switchBranch({ ref, expected_head: current.head, expected_ref: current.branch ? `refs/heads/${current.branch}` : '' });
    },
    onSuccess: async () => {
      if (mounted.current) await Promise.all([branches.refetch(), onSwitched()]);
    },
    onError: () => { void branches.refetch(); },
  });
  const current = branches.data?.repository ?? repository;
  return <div className="agent-git-branch-menu">
    <LaunchOptionMenu label="切换 Git 分支" popoverClassName="agent-git-branch-popover" value={current.branch ? `refs/heads/${current.branch}` : ''}
      placeholder={current.branch ?? '分离 HEAD'} disabled={disabled || switching.isPending || branches.isFetching || !branches.data}
      options={(branches.data?.branches ?? []).map(branch => ({ value: branch.ref, label: `${branch.remote ? '远程 · ' : ''}${branch.name}` }))}
      onChange={ref => { if (ref !== `refs/heads/${current.branch}`) switching.mutate(ref); }}/>
    {branches.isError && <button type="button" className="agent-git-branch-retry" onClick={() => void branches.refetch()}>重试分支列表</button>}
    {switching.isPending && <span role="status">正在切换分支…</span>}
    {switching.isError && <p role="alert"><span>{switching.error instanceof Error ? switching.error.message : '分支切换失败'}</span><button type="button" aria-label="关闭分支切换错误" onClick={() => switching.reset()}><X size={12}/></button></p>}
  </div>;
}
