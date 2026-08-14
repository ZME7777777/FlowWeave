import { useMutation, useQuery } from '@tanstack/react-query';
import { GitCompareArrows, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api/client';
import type { AgentProfileSwitchResult, FlowRun, SnapshotFlowNode } from '../types';

interface Props {
  run: FlowRun;
  node: SnapshotFlowNode;
  disabled: boolean;
  onSwitched: (result: AgentProfileSwitchResult) => void;
}

const shown = (value: unknown) => value === undefined ? '—' : typeof value === 'string' ? value : JSON.stringify(value);

export function AgentProfileSwitchPanel({ run, node, disabled, onSwitched }: Props) {
  const profiles = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities });
  const candidates = (profiles.data ?? []).filter(item => item.capability_type === 'AGENT_PROFILE');
  const current = node.asset.capabilities.find(item => item.capability_type === 'AGENT_PROFILE');
  const [targetId, setTargetId] = useState('');
  const preview = useQuery({
    queryKey: ['agent-profile-switch-preview', run.id, node.instance_key, targetId],
    queryFn: () => api.previewAgentProfileSwitch(run.id, node.instance_key, targetId),
    enabled: Boolean(targetId),
  });
  const activate = useMutation({
    mutationFn: () => api.switchAgentProfile(run.id, {
      expected_active_version: run.active_snapshot_version,
      flow_node_key: node.instance_key,
      profile_version_id: preview.data!.target_profile_version_id,
      source_profile_version_id: preview.data!.source_profile_version_id ?? null,
      expected_profile_digest: preview.data!.target_profile_digest,
      model_cost_comparison: {},
    }),
    onSuccess: onSwitched,
  });

  return <section className="agent-profile-switch-panel"><h4><ShieldCheck size={14}/>Agent Profile 激活</h4><p>当前：{current ? `${current.capability_key} · ${current.capability_id?.slice(0, 8)}` : '未绑定'}。切换会生成新 Snapshot/Attempt；既有执行保持原版本。</p>
    <select aria-label="目标 Agent Profile Version" value={targetId} onChange={event => setTargetId(event.target.value)}><option value="">选择不可变 Profile Version</option>{candidates.map(item => <option key={item.id} value={item.id}>{item.capability_key} · rev {item.revision_number} · {item.content_hash.slice(0, 10)}</option>)}</select>
    {preview.isLoading && <p className="field-hint">生成切换预览…</p>}{preview.error && <p className="error">{preview.error.message}</p>}
    {preview.data && <><div className="agent-profile-switch-facts"><span><b>新 Snapshot</b><small>v{preview.data.active_snapshot_version + 1}</small></span><span><b>旧 Attempt</b><small>{preview.data.existing_attempts_unchanged ? '保持不变' : '不可切换'}</small></span><span><b>目标 digest</b><small>{preview.data.target_profile_digest.slice(0, 12)}</small></span></div><details><summary><GitCompareArrows size={13}/>查看 {Object.keys(preview.data.changes).length} 项字段差异</summary><dl>{Object.entries(preview.data.changes).map(([key, change]) => <div key={key}><dt>{key}</dt><dd>{shown(change.from)} → {shown(change.to)}</dd></div>)}</dl></details><button className="secondary full" disabled={disabled || activate.isPending} onClick={() => activate.mutate()}>{activate.isPending ? '正在创建新 Snapshot…' : '确认切换并创建新 Attempt'}</button></>}
    {activate.error && <p className="error">{activate.error.message}</p>}
  </section>;
}
