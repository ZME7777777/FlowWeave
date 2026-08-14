import { Activity, AlertTriangle, BrainCircuit, GitFork, Radar, ShieldCheck, WalletCards } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { AgentConversation, RuntimeDiagnosticQuery, RuntimeSubagentTask } from '../types';

interface Props {
  conversation: AgentConversation;
  subagents: RuntimeSubagentTask[];
  streamStatus: 'connecting' | 'live' | 'recovering' | 'disabled';
  onRefresh: () => void;
}

const usd = (value: number) => `$${value.toFixed(4)}`;

export function RuntimeGovernancePanel({ conversation, subagents, streamStatus, onRefresh }: Props) {
  const qc = useQueryClient();
  const [objective, setObjective] = useState('');
  const [maxIterations, setMaxIterations] = useState(10);
  const [maxTokens, setMaxTokens] = useState('');
  const [maxCostUsd, setMaxCostUsd] = useState('');
  const [question, setQuestion] = useState('');
  const [diagnostic, setDiagnostic] = useState<RuntimeDiagnosticQuery>();
  const diagnosticQuery = useQuery({
    queryKey: ['runtime-diagnostic', diagnostic?.id],
    queryFn: () => api.diagnosticQuery(diagnostic!.id),
    enabled: Boolean(diagnostic?.id),
    refetchInterval: query => {
      const state = query.state.data?.state ?? diagnostic?.state;
      return state && ['SUCCEEDED', 'FAILED'].includes(state) ? false : 1500;
    },
  });
  const visibleDiagnostic = diagnosticQuery.data ?? diagnostic;
  const goal = useMutation({
    mutationFn: (body: { action: 'START' | 'STOP' | 'RESUME'; objective?: string; max_iterations?: number; max_tokens?: number | null; max_cost_usd?: number | null }) => api.controlConversationGoal(conversation.id, conversation.state_version, body),
    onSuccess: () => { setObjective(''); onRefresh(); },
  });
  const ask = useMutation({
    mutationFn: () => api.askAgent(conversation.id, question.trim()),
    onSuccess: result => { setQuestion(''); setDiagnostic(result); qc.setQueryData(['runtime-diagnostic', result.id], result); },
  });
  const taskUsages = subagents.flatMap(task => task.usage ? [task.usage] : []);
  const taskCost = taskUsages.reduce((sum, usage) => sum + usage.accumulated_cost_usd, 0);
  const taskTokens = taskUsages.reduce((sum, usage) => sum + usage.prompt_tokens + usage.completion_tokens + usage.reasoning_tokens, 0);
  const goalStatus = conversation.latest_goal_status;

  return <div className="runtime-governance">
    <section><h3><GitFork size={14}/>分支与 Runtime HEAD</h3><dl><dt>分支类型</dt><dd>{conversation.fork_kind || 'ROOT'}</dd><dt>源会话</dt><dd>{conversation.source_conversation_id || '—'}</dd><dt>源 Runtime event</dt><dd>{conversation.source_runtime_event_id || '—'}</dd><dt>指标处理</dt><dd>{conversation.metrics_reset == null ? '根会话' : conversation.metrics_reset ? '分支重新计量' : '继承累计指标'}</dd></dl>{conversation.fork_kind === 'SEMANTIC' && <p className="governance-warning"><AlertTriangle size={13}/>仅复制可见文本，不继承 Tool/Observation、Agent state、Skill 激活、Condensation、usage 或 Runtime HEAD。</p>}<small>Navigate 当前主链未开放；不会把客户端文本历史伪装成原生 HEAD。</small></section>
    <section><h3><WalletCards size={14}/>费用与预算</h3><div className="governance-metrics"><span><b>{usd(taskCost)}</b><small>Task 子 Agent 累计费用</small></span><span><b>{taskTokens.toLocaleString()}</b><small>Task Token</small></span><span><b>{taskUsages.filter(item => item.budget_state === 'EXCEEDED').length}</b><small>超预算 Task</small></span></div><small>父 Conversation/Run/Attempt 全量账本与 Trace 已在 T6.09–T6.12 跳过；这里不把子 Agent 成本重复合入不存在的父级总计。</small></section>
    <section><h3><Activity size={14}/>实时连接与恢复</h3><dl><dt>WebSocket</dt><dd className={`stream-${streamStatus}`}>{streamStatus}</dd><dt>耐久事实</dt><dd>REST cursor + PostgreSQL 投影</dd><dt>当前 Runtime cursor</dt><dd>{conversation.last_message?.runtime_cursor || '等待耐久事件'}</dd><dt>连接状态</dt><dd>{conversation.connection_status?.phase || conversation.state}</dd></dl><small>WebSocket 只提供低延迟 delta 和唤醒；断线时 REST poll 继续补偿，连接灯不代表执行成功或失败。</small></section>
    <section><h3><BrainCircuit size={14}/>Critic 与 Goal</h3>{conversation.critic_evaluations?.length ? conversation.critic_evaluations.map(item => <article className="critic-row" key={item.runtime_event_id}><b>{item.score.toFixed(2)}</b><span>{item.message || item.source_type}</span></article>) : <p className="governance-empty">尚无 Critic 评分。</p>}{goalStatus && <dl><dt>Goal 状态</dt><dd>{goalStatus.status} · {goalStatus.iteration}/{goalStatus.max_iterations}</dd><dt>目标</dt><dd>{goalStatus.objective || '—'}</dd></dl>}{conversation.kind === 'HUMAN_CREATED' && conversation.state === 'IDLE' && <div className="goal-controls">{!goalStatus?.active && goalStatus?.status !== 'interrupted' && <><textarea value={objective} maxLength={20000} onChange={event => setObjective(event.target.value)} placeholder="输入原生 Goal objective"/><div className="goal-budget-fields"><label>最大迭代<input type="number" min="1" max="20" value={maxIterations} onChange={event => setMaxIterations(Number(event.target.value))}/></label><label>Token 上限<input type="number" min="1" value={maxTokens} placeholder="不限制" onChange={event => setMaxTokens(event.target.value)}/></label><label>费用上限 USD<input type="number" min="0.0001" step="0.01" value={maxCostUsd} placeholder="不限制" onChange={event => setMaxCostUsd(event.target.value)}/></label></div><button disabled={!objective.trim() || goal.isPending || maxIterations < 1 || maxIterations > 20} onClick={() => goal.mutate({ action: 'START', objective: objective.trim(), max_iterations: maxIterations, max_tokens: maxTokens ? Number(maxTokens) : null, max_cost_usd: maxCostUsd ? Number(maxCostUsd) : null })}>启动 Goal</button></>}{goalStatus?.active && <button disabled={goal.isPending} onClick={() => goal.mutate({ action: 'STOP' })}>停止 Goal</button>}{goalStatus?.status === 'interrupted' && <button disabled={goal.isPending} onClick={() => goal.mutate({ action: 'RESUME' })}>恢复 Goal</button>}</div>}{goal.error && <p className="error">{goal.error.message}</p>}</section>
    <section><h3><Radar size={14}/>只读诊断</h3><textarea value={question} maxLength={20000} onChange={event => setQuestion(event.target.value)} placeholder="询问当前 Agent 状态；不写入消息树"/><button className="secondary" disabled={!question.trim() || ask.isPending || conversation.kind !== 'HUMAN_CREATED'} onClick={() => ask.mutate()}>运行 ask_agent</button>{visibleDiagnostic && <article className="diagnostic-result"><b>{visibleDiagnostic.state}</b>{visibleDiagnostic.response_text && <p>{visibleDiagnostic.response_text}</p>}<small>{visibleDiagnostic.cost_usd != null ? usd(visibleDiagnostic.cost_usd) : '费用待回写'} · {(visibleDiagnostic.prompt_tokens ?? 0) + (visibleDiagnostic.completion_tokens ?? 0)} tokens · {visibleDiagnostic.output_classification}</small></article>}{ask.error && <p className="error">{ask.error.message}</p>}</section>
    <section><h3><ShieldCheck size={14}/>能力兼容矩阵</h3><div className="governance-capabilities"><span className="available"><b>Tool Policy / Profile</b><small>不可变 Version 与 Snapshot 生效</small></span><span className="blocked"><b>Browser / 直接 Runtime API</b><small>当前主链 SKIP；无授权操作入口</small></span><span className="blocked"><b>ACP / VSCode / Desktop</b><small>当前主链 SKIP；不接受任意 command</small></span><span className="upstream"><b>子 Agent 单 Task 控制</b><small>UPSTREAM_BLOCKED；仅支持受管 Runtime 整体清理</small></span></div></section>
  </div>;
}
