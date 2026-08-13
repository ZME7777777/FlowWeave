import { useMutation } from '@tanstack/react-query';
import { AlertTriangle, Check, ShieldAlert, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type { NodeAttempt, RuntimeConfirmationAction } from '../types';

const riskLabel = (risk: string) => ({
  UNKNOWN: '风险待评估', LOW: '低风险', MEDIUM: '中风险', HIGH: '高风险', CRITICAL: '严重风险',
}[risk.toUpperCase()] ?? risk);

function actionArguments(action: RuntimeConfirmationAction): string {
  return JSON.stringify(action.arguments, null, 2);
}

export function RuntimeConfirmationPanel({ attempt, onResolved }: { attempt: NodeAttempt; onResolved: () => void }) {
  const [reason, setReason] = useState('');
  const batch = useMemo(() => attempt.runtime_confirmation_batches.find(item => item.state === 'PENDING' || item.state === 'DECIDING'), [attempt.runtime_confirmation_batches]);
  useEffect(() => setReason(''), [batch?.id]);
  const mutation = useMutation({
    mutationFn: ({ accept }: { accept: boolean }) => {
      if (!batch) throw new Error('当前没有待处理的 OpenHands 确认批次。');
      return api.decideRuntimeConfirmation(batch.id, accept, reason.trim());
    },
    onSuccess: onResolved,
  });

  if (!batch) return <section className="runtime-confirmation-panel missing"><AlertTriangle size={16}/><div><b>确认批次尚未投影</b><p>系统正在从 OpenHands 恢复待确认动作，请稍后刷新。普通消息不会被当作批准。</p></div></section>;
  const deciding = batch.state === 'DECIDING';
  return <section className="runtime-confirmation-panel" aria-label="OpenHands 工具确认批次">
    <header><ShieldAlert size={17}/><div><b>OpenHands 请求确认整个动作批次</b><small>{batch.action_count} 个动作 · 批次 {batch.pending_actions_digest.slice(0, 12)}</small></div></header>
    <p className="runtime-confirmation-warning">本次决定会应用于下列全部动作。OpenHands 1.40.0 不支持只批准其中一个动作。</p>
    <div className="runtime-confirmation-actions">{batch.pending_actions.map((action, index) => <article key={action.digest || action.action_id}>
      <header><span>{index + 1}</span><div><b>{action.tool_name || '未知工具'}</b><small>{action.summary || action.tool_call_id || action.action_id}</small></div><em className={action.security_risk.toLowerCase()}>{riskLabel(action.security_risk)}</em></header>
      <pre>{actionArguments(action)}</pre>
    </article>)}</div>
    <label>决定理由<textarea value={reason} maxLength={4000} disabled={deciding || mutation.isPending} placeholder="说明批准用途或拒绝原因" onChange={event => setReason(event.target.value)}/></label>
    <div className="runtime-confirmation-buttons"><button type="button" className="danger" disabled={!reason.trim() || deciding || mutation.isPending} onClick={() => mutation.mutate({ accept: false })}><X size={14}/>拒绝整批</button><button type="button" className="primary" disabled={!reason.trim() || deciding || mutation.isPending} onClick={() => mutation.mutate({ accept: true })}><Check size={14}/>{deciding ? '正在提交决定…' : '批准整批'}</button></div>
    {mutation.error && <p className="error"><AlertTriangle size={14}/>{mutation.error.message}</p>}
  </section>;
}
