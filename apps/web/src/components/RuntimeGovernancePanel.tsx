import { Activity, AlertTriangle, History, RefreshCw, ShieldCheck } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { api } from '../api/client';
import type { FlowRunRuntimeOverview } from '../types';

interface Props {
  runId: string;
  runtime: FlowRunRuntimeOverview;
  streamStatus: 'connecting' | 'live' | 'recovering' | 'disabled';
  onRefresh: () => void;
}

export function RuntimeGovernancePanel({ runId, runtime, streamStatus, onRefresh }: Props) {
  const replacement = useMutation({
    mutationFn: () => {
      if (runtime.active_generation == null || runtime.session_row_version == null) throw new Error('当前没有可替换的 active generation。');
      return api.replaceRuntime(runId, runtime.active_generation, runtime.session_row_version);
    },
    onSuccess: onRefresh,
  });
  return <div className="runtime-governance">
    <section><h3><Activity size={14}/>连接与健康</h3><dl><dt>逻辑状态</dt><dd>{runtime.connection_state}</dd><dt>实时连接</dt><dd className={`stream-${streamStatus}`}>{streamStatus}</dd><dt>active generation</dt><dd>{runtime.active_generation ?? '—'}</dd><dt>replacement generation</dt><dd>{runtime.replacement_generation ?? '—'}</dd></dl>{runtime.diagnostic_code && <p className="governance-warning"><AlertTriangle size={13}/>{runtime.diagnostic_code} · {runtime.diagnostic_summary}</p>}<small>客户端仅持 FlowRun 和 Conversation locator；每次连接由 FlowWeave 授权代理解析当前 generation。</small></section>
    <section><h3><RefreshCw size={14}/>替换操作</h3><button className="secondary" disabled={!runtime.active_generation || runtime.status === 'REPLACING' || runtime.status === 'RECONNECTING' || replacement.isPending} onClick={() => replacement.mutate()}>{replacement.isPending ? '已提交…' : '替换 active generation'}</button>{replacement.error && <p className="error">{replacement.error.message}</p>}<small>替换会先冻结新写入，保留原 Conversation ID、事件树和 Workspace。</small></section>
    <section><h3><History size={14}/>generation 审计</h3>{runtime.generations.length ? <div className="governance-capabilities">{runtime.generations.map(item => <span key={item.generation} className={item.state === 'READY' ? 'available' : item.state === 'FAILED' ? 'blocked' : 'upstream'}><b>generation {item.generation}</b><small>{item.state}{item.failure_code ? ` · ${item.failure_code}` : ''}</small></span>)}</div> : <p className="governance-empty">尚未分配 Runtime。</p>}</section>
    <section><h3><ShieldCheck size={14}/>保留与删除</h3><dl><dt>保留范围</dt><dd>FlowRun 生命周期</dd><dt>替换期间</dt><dd>Workspace 与 OpenHands state 保留</dd><dt>物理删除</dt><dd>仅删除 FlowRun</dd></dl><small>Conversation locator 不单独删除物理 Runtime；永久删除入口位于运行详情。</small></section>
  </div>;
}
